#!/usr/bin/env python3
"""CODEXIFY SUPERVISOR — the native processes, under one parent.

Docker Compose supervises twenty-two services and gets the order they come
up in right. Without it, something has to. This is that something: it starts
the backend and the workers, keeps their pids and their logs, and stops
everything when it is told to.

    python3 tools/codexify_supervisor.py --install <codexify-root>
    python3 tools/codexify_supervisor.py --install <codexify-root> --with-ui

It is a **user** process, started detached, and it is stopped by a signal
rather than by a terminal closing. It is not a system service and it asks
for no root: a thing that needs root to keep itself alive is a thing that
dies on the next update.

Standard library only. The processes it starts are Codexify's, and they
bring their own dependencies.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

VENV_DIR = "venv"
NATIVE_DIR = ".native"
PID_DIR = NATIVE_DIR + "/pids"
LOG_DIR = NATIVE_DIR + "/logs"

BACKEND_PORT = 8888
FRONTEND_PORT = 5173

# The one worker that answers a message, and the two that keep the memory
# and the runtime warm. Each serves its own queue, and nothing starts them
# automatically.
WORKERS = (
    ("worker-chat", "guardian.workers.chat_worker"),
    ("worker-chat-embed", "guardian.workers.chat_embedding_worker"),
    ("worker-warmup", "guardian.workers.warmup_worker"),
)


def read_env(install: Path) -> dict:
    """The installation's `.env`, as a mapping.

    A dotenv, not a shell script: no `export`, no interpolation. Values that
    are wrapped in quotes have them stripped, because a DSN with quotes
    around it is not a DSN psycopg can open.
    """
    values: dict[str, str] = {}
    path = install / ".env"
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def venv_python(install: Path) -> Path:
    return install / VENV_DIR / "bin" / "python"


def child_env(install: Path) -> dict:
    """The environment every child runs with."""
    env = dict(os.environ)
    env.update(read_env(install))
    env["PYTHONPATH"] = str(install)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def spawn(name: str, args: list[str], install: Path, env: dict) -> subprocess.Popen:
    """Start one process, with its pid and its log.

    Each child gets its own session, so that stopping it stops everything
    it spawned too — pnpm leaves a vite behind when it dies alone, and
    an orphaned vite holds the port forever, which is how a restart ends
    up serving yesterday's environment.
    """
    pid_dir = install / PID_DIR
    log_dir = install / LOG_DIR
    pid_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / f"{name}.log", "ab", buffering=0)
    process = subprocess.Popen(args, cwd=str(install), env=env,
                               stdout=log, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL,
                               start_new_session=True)
    (pid_dir / f"{name}.pid").write_text(f"{process.pid}\n", encoding="utf-8")
    return process


def stop_all(children: list[tuple[str, subprocess.Popen]]) -> None:
    """Stop the children — and their grandchildren — politely, then not."""
    import signal
    for _name, process in children:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass
    deadline = time.time() + 10
    for _name, process in children:
        remaining = max(0.1, deadline - time.time())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="codexify_supervisor.py",
        description="The native Codexify processes, under one parent.")
    parser.add_argument("--install", required=True,
                        help="the Codexify installation to run")
    parser.add_argument("--with-ui", action="store_true",
                        help="also start the interface's dev server")
    parser.add_argument("--backend-port", type=int, default=BACKEND_PORT)
    parser.add_argument("--frontend-port", type=int, default=FRONTEND_PORT)
    args = parser.parse_args(argv)

    install = Path(args.install).expanduser().resolve()
    python = venv_python(install)
    if not python.is_file():
        sys.stderr.write(
            f"codexify-supervisor: no environment at {install / VENV_DIR}. "
            f"Run `genesis codexify setup --native` first.\n")
        return 2

    env = child_env(install)
    children: list[tuple[str, subprocess.Popen]] = []

    backend = spawn("backend", [
        str(python), "-m", "uvicorn", "guardian.guardian_api:app",
        "--host", "127.0.0.1", "--port", str(args.backend_port),
    ], install, env)
    children.append(("backend", backend))

    for name, module in WORKERS:
        children.append((name, spawn(name, [str(python), "-m", module],
                                     install, env)))

    if args.with_ui:
        pnpm = os.environ.get("PNPM") or "pnpm"
        children.append(("interface", spawn("interface", [
            pnpm, "--dir", "frontend/src", "dev",
            "--port", str(args.frontend_port),
        ], install, env)))

    stopping = {"now": False}

    def stop(signum, frame):
        stopping["now"] = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    # The parent stays alive while the children do. It is the thing that
    # holds the group together; when it goes, they go.
    try:
        while not stopping["now"]:
            time.sleep(0.5)
    finally:
        stop_all(children)
    return 0


if __name__ == "__main__":
    sys.exit(main())
