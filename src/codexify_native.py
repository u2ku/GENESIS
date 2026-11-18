

"""CODEXIFY NATIVE — the same system, without Docker.

Docker is the reference setup for Codexify, and it is a good one: twenty-two
services, pinned, with compose supervising the order they come up in. But it
is not the only one, and a machine without Docker is still a machine.

This module is the native path. It brings up the same backend, the same
database schema and the same chat worker — as processes on the host instead
of containers — and it proves they work with the same health probes.

**What changes, and why it is not just "the compose file with the words
docker removed":**

- **The addresses change.** Inside a container, the local runtime is at
  `host.docker.internal`. On the host it is at `127.0.0.1`. One `.env`
  cannot serve both, so the native one carries the host addresses.
- **Redis needs naming.** The default `REDIS_URL` is `redis://redis:6379/0`
  — the compose *service name*. Natively that name does not resolve, so it
  has to be said out loud.
- **The config has to be told which config it is.** `CODEXIFY_CONFIG_SOURCE`
  defaults to `strict`, and strict compares the core settings against the
  legacy ones. Their defaults disagree — core says `LLM_PROVIDER=local`,
  legacy says `AI_BACKEND=groq` — and strict turns that disagreement into a
  startup abort. Compose sets `core`; the native path has to as well.
- **The embeddings need a backend that exists.** The lifespan constructs the
  vector store, and the vector store constructs an embedder, and the default
  embedder wants `sentence-transformers` — which wants torch, which is a
  gigabyte. The mock backend wants numpy. The native path starts with the
  mock, and says so.

**What does not change:** Postgres is still the datastore, Redis is still the
queue, `chat_worker` is still the process that answers a message, and the
health endpoints still decide whether any of it worked.
"""

from __future__ import annotations

import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import codexify  # noqa: E402  — the shared types, the masking, the presets

def prompt_and_fix_services(missing: list[str]) -> bool:
    """Offer to install the missing system services, and do it on a yes.

    The offer is a question, and a question is only asked where there is
    someone to answer it. A piped run, a test, a cron'd wizard gets the
    commands printed and the caller's honest prerequisite exit instead —
    never a hang on a prompt nobody can answer.
    """
    hints = get_service_hints(missing)
    if not hints:
        return True

    print(f"\nMissing system services detected: {', '.join(missing)}")
    print(f"Proposed commands:\n  {hints}")
    if (not sys.stdin.isatty()
            or getattr(prompt_and_fix_services, "no_prompts", False)):
        return False
    try:
        choice = input("\nWould you like to execute these commands "
                       "automatically? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if choice in ("y", "yes"):
        print("Executing system configuration...")
        done = subprocess.run(hints, shell=True)
        if done.returncode == 0:
            print("✓ Services installed and started successfully.")
            return True
        print("✗ Command execution failed. Please run them manually.")
        return False
    return False


# --------------------------------------------------------------- the contract --

EXIT_OK = codexify.EXIT_OK
EXIT_PARTIAL = codexify.EXIT_PARTIAL
EXIT_USAGE = codexify.EXIT_USAGE
EXIT_MISSING_INSTALL = codexify.EXIT_MISSING_INSTALL
EXIT_PREREQUISITE = codexify.EXIT_PREREQUISITE
EXIT_CONFLICT = codexify.EXIT_CONFLICT
EXIT_KEY_REJECTED = codexify.EXIT_KEY_REJECTED
EXIT_PRECONDITION = codexify.EXIT_PRECONDITION

OK = codexify.OK
BAD = codexify.BAD
MANUAL = codexify.MANUAL

Row = codexify.Row
Report = codexify.Report

VENV_DIR = "venv"
NATIVE_DIR = ".native"
PID_DIR = NATIVE_DIR + "/pids"
LOG_DIR = NATIVE_DIR + "/logs"
SUPERVISOR_PID = NATIVE_DIR + "/supervisor.pid"
SUPERVISOR = TOOLS / "codexify_supervisor.py"

# Codexify's floor. The container pins 3.11, and the pinned wheels are built
# against it, so 3.11 is what the native path asks for by preference.
PYTHON_MIN = (3, 10)
PYTHON_PREFERRED = (3, 11)

# The one worker that answers a message. The others serve other queues and
# are worth starting, but chat does not wait for them.
WORKERS = ("chat_worker", "chat_embedding_worker", "warmup_worker")

# The packages that are pinned in `requirements.txt` and that the native path
# does **not** want: they are the reason the full install is gigabytes.
# The graph is off by default and the local embeddings use the mock backend,
# so none of these are needed to answer a message.
HEAVY_PACKAGES = (
    "torch", "torchvision", "torchaudio", "sentence-transformers",
    "transformers", "chromadb", "onnxruntime", "opencv-python", "playwright",
    "patchright", "gradio", "datasets", "pandas", "scipy", "matplotlib",
    "sympy", "litellm", "crawl4ai", "kaggle", "mlx", "mlx-lm", "mlx-vlm",
    "langchain", "langchain-core", "langchain-text-splitters", "langsmith",
    "neo4j", "neomodel", "scikit-learn", "openai", "groq",
)

# The packages the pinned files leave out that the backend imports anyway.
# They are small, and without them the app does not start. `psycopg2-binary`
# is a container habit the native path inherits: the backend imports both
# drivers (`guardian/db/postgres_setup.py` wants 2, the pins carry 3), and
# the container gets it from `backend/requirements.txt` — a second pin file
# the native path does not read. `psycopg[binary]` is here for the native
# path's own sake: the database bootstrap below imports driver 3, and a
# machine whose pins never carried it must still be able to make its
# database. `pyjwt` and `python-multipart` are that second file's too: the
# auth middleware imports `jwt`, and the API's form routes make FastAPI
# demand the multipart parser at import time.
BASE_GAPS = ("httpx", "numpy", "bcrypt", "cryptography", "faiss-cpu",
             "huggingface_hub", "psycopg[binary]", "psycopg2-binary",
             "pyjwt", "python-multipart")

# Packages the pinned file carries for Python 3.13, where the stdlib dropped
# them. On 3.11 — what the native path asks for by preference — the stdlib
# still has them, and the backport's wheels refuse to install: they demand
# >=3.13. A container never sees this, because the image *is* one Python;
# the native path has to say it out loud.
PY313_ONLY_PACKAGES = ("audioop-lts",)

# The ports the native stack listens on.
POSTGRES_PORT = 5432
REDIS_PORT = 6379
BACKEND_PORT = codexify.BACKEND_PORT
FRONTEND_PORT = codexify.FRONTEND_PORT

# How long the backend may take to come up before readiness calls it a
# failure. A container's healthcheck has retries; a one-shot probe does not,
# so the probe becomes a wait.
STARTUP_TIMEOUT = 90.0

# How long the interface may take to come up. Vite resolves its dependency
# graph before it binds, and `--force` makes it do that from scratch every
# time — seconds, not milliseconds. The interface gets the same patience
# the backend gets, because it is the same kind of wait.
FRONTEND_WAIT = 90.0


# --------------------------------------------------------------- the waiting --

class _Silent:
    """A spinner that is not there, for a place that cannot show one."""

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _spinner(label: str):
    """A spinner for the long quiet stretches, where there is a terminal.

    The venv build is minutes of `pip --quiet`. The backend takes real
    seconds to bind, and vite takes real seconds to resolve its
    dependency graph before it will answer. A terminal that says nothing
    for a minute and a half is a terminal that looks broken — and a
    wizard that looks broken is a wizard nobody waits for.

    The spinner writes to stderr and only animates where there is a
    terminal, so a redirected run stays a clean transcript.
    """
    try:
        from term import Spinner
    except Exception:
        return _Silent()
    try:
        return Spinner(sys.stderr, label=label)
    except Exception:
        return _Silent()


def _with_spinner(label: str, function, *args, **kwargs):
    """Run a slow thing with a spinner saying what is slow."""
    with _spinner(label):
        return function(*args, **kwargs)

# The brew commands that provide what the native path needs. The wizard
# prints them and does not run them: installing system packages is the
# operator's call, not a wizard's.
BREW_PACKAGES = {
    "postgres": "postgresql@15",
    "redis": "redis",
    "python": "python@3.11",
    "pnpm": "pnpm",
    "git-lfs": "git-lfs",
}
BREW_INSTALL = "brew install postgresql@15 redis python@3.11 pnpm"


def get_service_hints(missing: Iterable[str]) -> str:
    """Return platform-appropriate install and service startup commands for macOS or Arch Linux."""
    if sys.platform == "darwin":
        packages = [BREW_PACKAGES[name] for name in missing if name in BREW_PACKAGES]
        if not packages:
            return ""
        commands = ["brew install " + " ".join(packages)]
        if "postgres" in missing:
            commands.append("brew services start postgresql@15")
        if "redis" in missing:
            commands.append("brew services start redis")
        return " && ".join(commands)
    else:
        # The Arch posture. pacman gets the packages and systemd starts
        # them, but a fresh postgresql needs its cluster initialized
        # first, as the postgres user, in the directory the unit expects.
        # The initdb is guarded — a postgres that is installed and
        # initialized but merely stopped must not have its data directory
        # re-initialized out from under it, and a failing command in the
        # chain would take the systemctl after it down with it.
        commands = []
        if "postgres" in missing:
            commands.append("sudo pacman -S --needed postgresql")
            commands.append("[ -d /var/lib/postgres/data ] || "
                            "sudo -u postgres initdb "
                            "-D /var/lib/postgres/data")
            commands.append("sudo systemctl enable --now postgresql")
        if "redis" in missing:
            commands.append("sudo pacman -S --needed redis")
            commands.append("sudo systemctl enable --now redis")
        if "python" in missing:
            commands.append("sudo pacman -S --needed python python-pip")
        if "pnpm" in missing:
            commands.append("sudo pacman -S --needed pnpm")
        if "git-lfs" in missing:
            commands.append("sudo pacman -S --needed git-lfs")
        return " && ".join(commands)

def brew_hint(missing: Iterable[str]) -> str:
    return get_service_hints(missing)


# The recorded-runtime reader lives in codexify now — the readiness
# ladder needed it too, and this module cannot be imported by the module
# it imports. It is the same function; the alias keeps the native path's
# call sites and tests unchanged.
runtime_endpoint_override = codexify.runtime_endpoint_override


def remote_runtime_overrides(base: str,
                             model: Optional[str] = None) -> dict[str, str]:
    """The `.env` wiring that points the stack at a remote runtime.

    `LOCAL_BASE_URL` is the address the backend's own clients dial, so it
    is the one that has to carry the tailnet address — the preset's
    localhost guess is worth nothing here. `VAULTNODE_BASE_URL` is the
    runtime's root, without the `/v1`. The endpoint is written back under
    its own name too, so the next run reads what this one chose.
    """
    wiring = {
        "LOCAL_BASE_URL": base,
        "LOCAL_DOCKER_FALLBACK_BASE_URL": base,
        "VAULTNODE_BASE_URL": (base[:-3] if base.endswith("/v1") else base),
        "CODEXIFY_RUNTIME_ENDPOINT": base,
        "OPENAI_API_BASE": base,
    }
    if model:
        wiring["CODEXIFY_MODEL"] = model
    return wiring


def prompt_runtime_defaults() -> tuple[str, str]:
    """Ask once for the remote runtime, defaulting to the Mac Studio's.

    Hit enter twice and this machine points at the broker the
    `space.codexify.whooshd` LaunchAgent serves. The answer is written
    into the `.env`, so the question is asked once per machine — not once
    per run.
    """
    endpoint = codexify.DEFAULT_REMOTE_RUNTIME_ENDPOINT
    model = codexify.DEFAULT_REMOTE_RUNTIME_MODEL
    print("\n[Whoosh'd Remote Inference Broker Setup]")
    try:
        endpoint = (input(f"  > Runtime endpoint URL [{endpoint}]: ").strip()
                    or endpoint)
        model = (input(f"  > Model identifier [{model}]: ").strip() or model)
    except (EOFError, KeyboardInterrupt):
        pass
    return endpoint, model


CommandRunner = Callable[..., subprocess.CompletedProcess]
HttpGetter = Callable[[str, float], tuple[int, str]]


def _run_command(args: list[str], cwd: Optional[Path] = None,
                 timeout: float = 120.0,
                 env: Optional[dict] = None,
                 stream: bool = False) -> subprocess.CompletedProcess:
    """Run a command and keep its output. Never raises on a non-zero exit.

    With `stream`, the child prints as it runs and the output is not
    kept — the venv build is minutes of pip's progress, and the wizard
    shows it live rather than holding it hostage behind a progress row.
    """
    try:
        if stream:
            done = subprocess.run(args, cwd=str(cwd) if cwd else None,
                                   text=True, timeout=timeout, env=env)
            return subprocess.CompletedProcess(args, done.returncode, "", "")
        return subprocess.run(args, cwd=str(cwd) if cwd else None,
                              capture_output=True, text=True, timeout=timeout,
                              env=env)
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, 127, "", "not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", "timed out")


# ------------------------------------------------------------------ detection --

def tcp_open(host: str, port: int, timeout: float = 1.5) -> bool:
    """Is something listening on this address?

    A plain socket, because the point is to find out whether the *service* is
    there, and a service that is not there cannot be asked anything.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def get_stable_python() -> str:
    """An interpreter name on PATH that is not the bleeding edge.

    The wizard's own venv is built by `find_python`, which already prefers
    3.12 and 3.11 over a 3.14 that source builds break on. This is the
    same preference for the one child that is started by *name* — the
    supervisor, which is standard-library-only but is still a process
    somebody has to keep alive. On a rolling machine like Arch the only
    `python3` may be the bleeding edge, and then that is what there is.
    """
    for candidate in ("python3.12", "python3.11", "python3.10", "python3"):
        if not shutil.which(candidate):
            continue
        try:
            done = subprocess.run([candidate, "--version"],
                                  capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode != 0:
            continue
        if not any(v in done.stdout for v in ("3.14", "3.15")):
            return candidate
    return "python3"


def find_python(preferred: Optional[str] = None) -> tuple[Optional[Path], str]:
    """The interpreter to build the environment with.

    The operator's choice wins; then an existing venv; then a `python3.11`
    on PATH; then whatever `python3` is. The note says what was found and
    what was wanted, because "the backend did not start" is a much worse
    error message than "this Python is too old".
    """
    if preferred:
        path = Path(preferred).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve(), ""
        return None, f"the python you named is not executable: {path}"
    for candidate in ("python3.11", "python3.12", "python3.13", "python3"):
        found = shutil.which(candidate)
        if not found:
            continue
        version = _python_version(Path(found))
        if version and version >= PYTHON_MIN:
            return Path(found).resolve(), ""
    found = shutil.which("python3")
    if not found:
        return None, "no python3 on PATH"
    version = _python_version(Path(found))
    if version and version < PYTHON_MIN:
        return None, (f"python3 is {version[0]}.{version[1]}, and Codexify "
                      f"needs {PYTHON_MIN[0]}.{PYTHON_MIN[1]} or newer")
    return Path(found).resolve(), ""


def _python_version(python: Path) -> Optional[tuple[int, int]]:
    try:
        done = subprocess.run([str(python), "-c",
                               "import sys;print(sys.version_info[0],"
                               "sys.version_info[1])"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    try:
        major, minor = done.stdout.strip().split()[:2]
        return int(major), int(minor)
    except (ValueError, IndexError):
        return None


def venv_python(install: Path) -> Optional[Path]:
    """The environment's interpreter, if the environment has been made."""
    path = install / VENV_DIR / "bin" / "python"
    return path if path.is_file() and os.access(path, os.X_OK) else None


# ------------------------------------------------------------- the requirements --

def native_requirements(install: Path,
                        python_version: Optional[tuple[int, int]] = None
                        ) -> list[str]:
    """What to install, taken from the pinned file.

    The pinned `requirements.txt` is the tested set, and it is also the
    gigabyte set. Everything the native path does not need is dropped, and
    what `requirements/base.in` leaves out is added back — the app imports
    `httpx`, `numpy`, `bcrypt` and `cryptography` without saying so.

    One more thing is dropped: the pins that exist only for Python 3.13,
    where the stdlib dropped them. On 3.11 the stdlib still has them, and
    the backport's wheels refuse to install — they demand >=3.13. The
    container never sees this, because the image *is* one Python; the
    native path has to ask which one it got.
    """
    if python_version is None:
        python_version = PYTHON_PREFERRED
    lines: list[str] = []
    # The pins have moved: `requirements.txt` at the root was the container
    # habit, and the submodule now carries them under `requirements/all.txt`.
    # Both spellings are honoured, and the one that is there is the one
    # that is read — a submodule that has moved its pins is not a machine
    # with nothing to install.
    candidates = (install / "requirements.txt",
                  install / "requirements" / "all.txt")
    source = next((c for c in candidates if c.is_file()), None)
    if source is not None:
        for raw in source.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            name = re.split(r"[=<>!~\[]", line, maxsplit=1)[0].strip().lower()
            name = name.replace("_", "-")
            if name in HEAVY_PACKAGES:
                continue
            if (python_version < (3, 13)
                    and name in PY313_ONLY_PACKAGES):
                continue
            lines.append(line)
    for name in BASE_GAPS:
        # A gap may name an extra — `psycopg[binary]` — and the lines it is
        # compared against carry their extras stripped by the split above, so
        # the gap is compared by its stem, the package the extra is of.
        stem = name.split("[", 1)[0].strip().lower().replace("_", "-")
        if not any(re.split(r"[=<>!~\[]", line, maxsplit=1)[0].strip().lower()
                   .replace("_", "-") == stem for line in lines):
            lines.append(name)
        # Filter out pyarrow on Python 3.14+ where source builds fail
    if python_version and python_version >= (3, 14):
        lines = [r for r in lines if not any(pkg in r.lower() for pkg in ["pyarrow", "pypika"])]
    return lines


# ------------------------------------------------------------------ the config --

def native_env_overrides(existing: dict[str, str], preset_id: str,
                         model: Optional[str], cloud: bool,
                         db_url: str, redis_url: str,
                         embed_model: Optional[str],
                         install: Optional[Path] = None,
                         theme: Optional[str] = None) -> dict[str, str]:
    """The `.env` values for a native run.

    The differences from the compose one are all forced by the difference
    between a container and a host, and each of them is a real trap:

    - the local runtime is at `127.0.0.1`, not `host.docker.internal`;
    - Redis is at `localhost`, not at the compose service name;
    - libpq must not see the template's `PGHOST=db` — the compose service
      name, which does not resolve on a host. Every `PG*` and `POSTGRES_*`
      variable carries it, and any libpq child (the bootstrap, psql,
      alembic) would try to connect to a name only Docker can resolve.
    - the config source is `core`, or the core and legacy defaults disagree
      and the app refuses to start;
    - the embeddings backend is the mock one, unless the operator asked for
      the real thing, so a first run does not download a model.
    """
    preset = codexify.PRESETS[preset_id]
    model = model or preset.default_model
    db_user, db_password = codexify.db_credentials(db_url)
    db_name = db_url.rsplit("/", 1)[-1].split("?", 1)[0]

    guardian_key = existing.get("GUARDIAN_API_KEY", "")
    if codexify.is_placeholder(guardian_key):
        guardian_key = secrets.token_hex(32)

    session_secret = existing.get("GUARDIAN_SESSION_SECRET", "")
    if codexify.is_placeholder(session_secret):
        session_secret = secrets.token_urlsafe(32)

    overrides = {
        "GUARDIAN_API_KEY": guardian_key,
        "VITE_GUARDIAN_API_KEY": guardian_key,
        "GUARDIAN_SESSION_SECRET": session_secret,
        "GUARDIAN_AUTH_MODE": "local",
        "GUARDIAN_DEV_MODE": "true",

        # The house theme. Vite reads VITE_* at dev-server start, so this
        # is how a deployment ships dark mode as its default — the user's
        # own choice in localStorage still wins once they have made one.
        "VITE_THEME_DEFAULT": theme or "system",

        # The stores. These are the two addresses a container cannot guess.
        "DATABASE_URL": db_url,
        "GUARDIAN_DATABASE_URL": db_url,
        "GUARDIAN_DB_URL": db_url,
        "GUARDIAN_DB_DSN": db_url,
        "REDIS_URL": redis_url,

        # libpq, pointed at the host. The template's PG* block says the
        # service name, which is Docker-only; the bootstrap that makes the
        # role and the database is a libpq child and reads it.
        "PGHOST": "127.0.0.1",
        "PGPORT": str(POSTGRES_PORT),
        "PGUSER": db_user,
        "PGPASSWORD": db_password,
        "PGDATABASE": db_name,
        "POSTGRES_USER": db_user,
        "POSTGRES_PASSWORD": db_password,
        "POSTGRES_DB": db_name,

        # The config trap. Strict compares the core settings against the
        # legacy ones, and their defaults disagree.
        "CODEXIFY_CONFIG_SOURCE": "core",

        # The provider. The host address, because there is no container here.
        "LLM_PROVIDER": "local",
        "LOCAL_RUNTIME_PRESET": preset.id,
        "LOCAL_PROVIDER_DISPLAY_NAME": preset.display_name,
        "LOCAL_PROVIDER_VENDOR": preset.vendor,
        "LOCAL_BASE_URL": preset.base_url,
        "LOCAL_DOCKER_FALLBACK_BASE_URL": preset.base_url,
        "LOCAL_COMPAT_FIRST": "1" if preset.compat_first else "0",
        "LOCAL_ENABLE_OLLAMA_GENERATE_FALLBACK": "0",
        "VAULTNODE_BASE_URL": preset.base_url.rstrip("/v1") if preset.base_url.endswith("/v1")
        else preset.base_url,
        "VAULTNODE_HEALTH_ENDPOINTS": ",".join(preset.health_endpoints),
        "LOCAL_CHAT_MODEL": model,
        "LOCAL_LLM_MODEL": model,
        "LLM_MODEL": model,

        # The vector store. Chroma and faiss are the two the app supports;
        # faiss is the lighter of them and needs no model.
        "CODEXIFY_VECTOR_STORE": "faiss",
        "CODEXIFY_EMBEDDINGS_BACKEND": "mock",
        "EMBEDDING_BACKEND": "mock",
        "CODEXIFY_ALLOW_EMBEDDINGS_FALLBACK": "0",

        # The graph is off by default and the driver is not even imported
        # unless it is asked for.
        "CODEXIFY_ENABLE_GRAPH_WRITES": "false",
        "CODEXIFY_GRAPH_BACKEND": "noop",

        "ENABLE_OUTBOX": "true",
        "ENABLE_CONNECTOR_WORKER": "false",

        # Storage. The app defaults its media root to `/app/data/media` —
        # the container's app root, read-only on a host. On a host the
        # media lives with the rest of the runtime state, inside the
        # install, where one rm -rf still removes everything.
        "STORAGE_BASE_PATH": (str(Path(install) / ".native" / "media")
                              if install else "./.native/media"),
    }

    if embed_model:
        overrides["LOCAL_EMBED_MODEL"] = embed_model

    if cloud:
        overrides["ALLOW_CLOUD_PROVIDERS"] = "true"
        overrides["CODEXIFY_LOCAL_ONLY_MODE"] = "false"
    else:
        overrides["ALLOW_CLOUD_PROVIDERS"] = "false"
        overrides["CODEXIFY_LOCAL_ONLY_MODE"] = "true"

    return overrides


def default_db_url(existing: dict[str, str]) -> str:
    """The Postgres DSN, from the template's own values."""
    user = existing.get("POSTGRES_USER") or "codexify"
    password = existing.get("POSTGRES_PASSWORD") or "codexify"
    name = existing.get("POSTGRES_DB") or "Codexify"
    return f"postgresql://{user}:{password}@127.0.0.1:{POSTGRES_PORT}/{name}"


def default_redis_url(existing: dict[str, str]) -> str:
    """The Redis URL, on the host."""
    return existing.get("REDIS_URL") or f"redis://127.0.0.1:{REDIS_PORT}/0"


# ------------------------------------------------------------------- the rows --

def detect(install: Path, *, python: Optional[str] = None,
           runner: CommandRunner = _run_command,
           http_getter: HttpGetter = None) -> list[Row]:
    """What the host has, for the native path.

    Every row is a fact about this machine, read from the machine.
    """
    rows: list[Row] = []

    found, note = find_python(python)
    if found is None:
        rows.append(Row("python", BAD,
                        note + f" — {brew_hint(['python'])}"))
    else:
        version = _python_version(found)
        detail = f"{found} ({version[0]}.{version[1]})" if version else str(found)
        if version and version < PYTHON_PREFERRED:
            detail += f" — {PYTHON_PREFERRED[0]}.{PYTHON_PREFERRED[1]} is what the images use"
        rows.append(Row("python", OK, detail))

    venv = venv_python(install)
    rows.append(Row("the environment", OK if venv else MANUAL,
                    str(venv) if venv else
                    f"not built yet — {install / VENV_DIR}"))

    if tcp_open("127.0.0.1", POSTGRES_PORT):
        rows.append(Row("postgres", OK, f"answering on :{POSTGRES_PORT}"))
    else:
        rows.append(Row("postgres", BAD,
                        f"nothing on :{POSTGRES_PORT} — run: "
                        f"{brew_hint(['postgres'])}"))

    if tcp_open("127.0.0.1", REDIS_PORT):
        rows.append(Row("redis", OK, f"answering on :{REDIS_PORT}"))
    else:
        rows.append(Row("redis", BAD,
                        f"nothing on :{REDIS_PORT} — run: "
                        f"{brew_hint(['redis'])}"))

    pnpm = shutil.which("pnpm")
    rows.append(Row("the interface", OK if pnpm else MANUAL,
                    f"pnpm {pnpm}" if pnpm else
                    "pnpm is not here — the API works without it — run: "
                    + brew_hint(["pnpm"])))

    override_base, override_model = runtime_endpoint_override(install)
    if override_base:
        # Respect the remote inference node override over Tailscale using a valid preset
        preset_id = "ollama"
        model = (override_model or codexify.DEFAULT_REMOTE_RUNTIME_MODEL)
        base = override_base
    else:
        preset_id, model, base = codexify.detect_runtime(
            http_getter or codexify._http_get)
    if preset_id:
        rows.append(Row("the local runtime", OK,
                        f"{codexify.PRESETS[preset_id].display_name} at {base} "
                        f"serving {model}"))
    else:
        rows.append(Row("the local runtime", BAD,
                        "no OpenAI-compatible runtime answered on "
                        + ", ".join(codexify.PRESETS[p].base_url
                                    for p in codexify.PRESET_ORDER)))
    return rows


# ------------------------------------------------------------------ the steps --

def build_environment(install: Path, python: Optional[str] = None,
                      runner: CommandRunner = _run_command,
                      timeout: float = 1800.0) -> tuple[bool, str]:
    """Make the virtual environment and install into it.

    Contained: the environment lives inside the installation, in a directory
    that is already gitignored, and removing it removes the whole thing.
    """
    interpreter, note = find_python(python)
    if interpreter is None:
        return False, note
    venv_dir = install / VENV_DIR
    if not venv_python(install):
        done = runner([str(interpreter), "-m", "venv", str(venv_dir)],
                      cwd=install, timeout=300.0)
        if done.returncode != 0:
            return False, (done.stderr or done.stdout
                           or "the environment could not be created").strip()[-300:]
    venv_py = venv_python(install)
    if venv_py is None:
        return False, f"{venv_dir / 'bin' / 'python'} is not there"

    venv_env = os.environ.copy()
    venv_env["PATH"] = f"{venv_dir / 'bin'}{os.pathsep}{venv_env.get('PATH', '')}"
    venv_env["CFLAGS"] = "-Wno-error=incompatible-pointer-types"

    boot = runner([str(venv_py), "-m", "pip", "install", "--quiet", "--upgrade",
                   "pip", "setuptools", "wheel", "meson-python", "ninja", "cython"],
                  cwd=install, timeout=300.0, env=venv_env)
    if boot.returncode != 0:
        return False, (boot.stderr or boot.stdout
                       or "the build toolchain could not be installed").strip()[-400:]

    requirements = native_requirements(
        install, python_version=_python_version(interpreter))
    if not requirements:
        return False, "there is no requirements.txt to install from"
    done = runner([str(venv_py), "-m", "pip", "install", "--no-build-isolation",
                   "--prefer-binary", "--quiet", *requirements],
                  cwd=install, timeout=timeout, env=venv_env)
    if done.returncode != 0:
        return False, (done.stderr or done.stdout
                       or "the install failed").strip()[-400:]
    return True, f"installed {len(requirements)} package(s) into {venv_dir}"


def build_frontend(install: Path,
                   runner: CommandRunner = _run_command,
                   timeout: float = 600.0) -> tuple[bool, str]:
    """Install the interface's own dependencies, if they are missing.

    The frontend is a second dependency universe — pnpm's, not pip's —
    and a fresh checkout has a `package.json` with no `node_modules`,
    so the dev server it starts from dies on `vite: command not found`
    before it can serve anything. The container builds this in its image;
    natively, this is the build.

    Idempotent: an install with its `node_modules` is left alone, so a
    converged machine pays nothing but one stat.

    A checkout whose package.json is still an LFS pointer file is refused
    before pnpm runs: pnpm's parse error on the pointer ("expected value
    at line 1 column 1") names neither the file nor git-lfs, and this
    names both.
    """
    frontend = install / "frontend" / "src"
    pending = codexify.lfs_pointers_pending(install)
    if pending:
        hint = ("git -C " + str(install) + " lfs pull"
                + (" — after installing git-lfs: "
                   + codexify.lfs_install_hint()
                   if not shutil.which("git-lfs") else ""))
        return False, (", ".join(pending) + " are pointer files — the "
                       "checkout was made without git-lfs. Run: " + hint)
    if not (frontend / "package.json").is_file():
        return True, "there is no interface to build"
    if (frontend / "node_modules").is_dir():
        return True, "the interface's dependencies are already there"
    pnpm = shutil.which("pnpm") or os.environ.get("PNPM")
    if not pnpm:
        return False, ("pnpm is not there — the interface cannot be "
                       "built without it")
    done = runner([pnpm, "--dir", str(frontend), "install"],
                  cwd=install, timeout=timeout)
    if done.returncode != 0:
        return False, (done.stderr or done.stdout
                       or "the install failed").strip()[-400:]
    return True, "the interface's dependencies are installed"


def migrate(install: Path, runner: CommandRunner = _run_command,
            timeout: float = 600.0) -> tuple[bool, str]:
    """Bring the schema up, and seed it.

    Alembic reads `DATABASE_URL` from the environment and refuses anything
    that is not Postgres. The target is `heads` — plural, deliberately: the
    vendored history is forked (work orders and TTS voice profiles were
    left as two heads), and on a fresh database both branches belong. A
    merge revision would edit the vendored app; `heads` does not.
    The seed is the same script the container runs.
    """
    venv_py = venv_python(install)
    if venv_py is None:
        return False, "there is no environment to migrate from"
    env = _child_env(install)

    done = runner([str(venv_py), "-m", "alembic", "-c",
                   "backend/alembic.ini", "upgrade", "heads"],
                  cwd=install, timeout=timeout, env=env)
    if done.returncode != 0:
        return False, (done.stderr or done.stdout
                       or "the migration failed").strip()[-400:]
    seeded = runner([str(venv_py), "backend/scripts/seed_defaults.py"],
                    cwd=install, timeout=timeout, env=env)
    if seeded.returncode != 0:
        return False, (seeded.stderr or seeded.stdout
                       or "the seed failed").strip()[-400:]
    return True, "the schema is up, and seeded"


def create_database(install: Path, runner: CommandRunner = _run_command,
                    timeout: float = 120.0) -> tuple[bool, str]:
    """Create the role and the database, if they are not there.

    A native Postgres has neither until someone makes them, and the
    migration cannot make them for itself — the migration runs *in* them.
    Compose gets both for free: the `postgres` image reads `POSTGRES_USER`
    and `POSTGRES_PASSWORD` on first boot and creates the role with the
    database. A Homebrew Postgres has to be told, and it is told as the
    superuser it already has — the OS user, peer-trusted on localhost —
    not as the app user that does not exist yet.
    """
    venv_py = venv_python(install)
    if venv_py is None:
        return False, "there is no environment to create it from"
    env = _child_env(install)
    existing = codexify.read_env(install / codexify.ENV_FILE)
    url = existing.get("DATABASE_URL", "")
    if not url:
        return False, "there is no DATABASE_URL to create"
    name = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if not name:
        return False, "the DATABASE_URL carries no database name"
    user, password = codexify.db_credentials(url)
    if not user:
        return False, "the DATABASE_URL carries no user"

    script = (
        "import os\n"
        "import sys\n"
        "import psycopg\n"
        "name, user, password = sys.argv[1:4]\n"
        "host = os.environ.get('PGHOST', '127.0.0.1')\n"
        "port = os.environ.get('PGPORT', '5432')\n"
        "with psycopg.connect(dbname='postgres', user=os.environ['USER'],\n"
        "                      host=host, port=port,\n"
        "                      autocommit=True) as conn:\n"
        "    def q(text):\n"
        "        return \"'\" + text.replace(\"'\", \"''\") + \"'\"\n"
        "    rows = conn.execute(\n"
        "        'SELECT 1 FROM pg_roles WHERE rolname = %s',\n"
        "        (user,)).fetchall()\n"
        "    if not rows:\n"
        "        conn.execute(\n"
        "            'CREATE ROLE \"' + user.replace('\"','') + '\" '\n"
        "            'LOGIN PASSWORD ' + q(password))\n"
        "        print('role created')\n"
        "    else:\n"
        "        conn.execute(\n"
        "            'ALTER ROLE \"' + user.replace('\"','') + '\" '\n"
        "            'LOGIN PASSWORD ' + q(password))\n"
        "        print('role already there')\n"
        "    rows = conn.execute('SELECT 1 FROM pg_database WHERE datname = %s',\n"
        "                        (name,)).fetchall()\n"
        "    if not rows:\n"
        "        conn.execute('CREATE DATABASE \"' + name.replace('\"','') + '\" '\n"
        "                    'OWNER \"' + user.replace('\"','') + '\"')\n"
        "        print('database created')\n"
        "    else:\n"
        "        print('database already there')\n"
    )
    done = runner([str(venv_py), "-c", script, name, user, password],
                  cwd=install, timeout=timeout, env=env)
    if done.returncode != 0:
        return False, (done.stderr or done.stdout
                       or "the database could not be created").strip()[-300:]
    return True, (done.stdout or "").strip() or "the database is there"


def _child_env(install: Path) -> dict:
    """The environment the child processes run with.

    The `.env` is loaded, and the few variables that must point somewhere
    else on a host are set over it.
    """
    env = dict(os.environ)
    env.update(codexify.read_env(install / codexify.ENV_FILE))
    env["PYTHONPATH"] = str(install)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env["LOCAL_EMBED_MODEL"] = env.get("LOCAL_EMBED_MODEL", "")
    return env


# -------------------------------------------------------------- the processes --

def supervisor_running(install: Path) -> Optional[int]:
    """The supervisor's pid, if it is still alive.

    A zombie is not alive. It is a process that has already died and is
    waiting to be reaped: it cannot be stopped, it holds nothing, and
    the next start must not be refused because of it.
    """
    pid_file = install / SUPERVISOR_PID
    if not pid_file.is_file():
        return None
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if not codexify.pid_alive(pid):
        return None
    return pid


def _child_pid(install: Path, name: str) -> Optional[int]:
    """The pid the supervisor recorded for one of its children."""
    pid_file = install / PID_DIR / f"{name}.pid"
    if not pid_file.is_file():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _child_pids(install: Path) -> list[int]:
    """Every pid the supervisor recorded for its children."""
    pid_dir = install / PID_DIR
    if not pid_dir.is_dir():
        return []
    pids = []
    for pid_file in sorted(pid_dir.glob("*.pid")):
        try:
            pids.append(int(pid_file.read_text(encoding="utf-8").strip()))
        except (OSError, ValueError):
            continue
    return pids


def _is_ours(install: Path, pid: int) -> bool:
    """Does this pid belong to this installation?

    A pid file is a claim, and a pid is reused. Before a leftover is
    stopped, the process is asked where it works: a process whose working
    directory is this install is our own orphan, and one that is not is
    somebody else's process that happens to have the same number.
    """
    cwd = codexify.process_cwd(pid)
    if not cwd:
        return False
    try:
        return Path(cwd).resolve().is_relative_to(install.resolve())
    except (OSError, ValueError):
        return False


def _is_orphaned(pid: int) -> bool:
    """Is this process's working directory gone?

    A process whose working directory has been deleted is the last
    survivor of an installation that was removed: the install's pid
    files went with it, so nothing else can stop it, and the port it
    holds will be held until somebody stops it by hand.
    """
    cwd = codexify.process_cwd(pid)
    if not cwd:
        return False
    return not Path(cwd).exists()


def _port_squatter(install: Path, with_ui: bool) -> Optional[str]:
    """Who is holding a port this install needs, if it is not us.

    A port held by somebody else is a port this backend cannot have.
    Starting anyway loses the race silently: the new backend dies on the
    bind, and the health checks that follow answer the process that
    already held the port. So the port is checked before the start, and
    the holder is named — an install that was moved to the trash keeps
    its socket open long after it stopped being the install.
    """
    wanted = [(BACKEND_PORT, "backend")]
    if with_ui:
        wanted.append((FRONTEND_PORT, "the interface"))
    for port, label in wanted:
        holder = codexify.port_holder(port)
        if holder is None:
            continue
        cwd = codexify.process_cwd(holder) or "another installation"
        if _is_ours(install, holder):
            return (f":{port} is held by pid {holder}, which is this "
                    f"install's own leftover and could not be stopped")
        if _is_orphaned(holder):
            return (f":{port} is held by pid {holder}, an orphan of an "
                    f"installation that has been deleted — nothing can stop "
                    f"it by record any more, so stop it by hand and run "
                    f"again")
        return (f":{port} is held by pid {holder} ({cwd}), which is not this "
                f"install's {label} — stop it before starting")
    return None


def _settle_ports(with_ui: bool) -> None:
    """Wait a moment for a port to be released by a process that just died.

    A socket is closed as its process dies, which is a moment after the
    signal. The leftovers are given that moment, so that a port which
    has just been cleared is not called somebody else's.
    """
    ports = [BACKEND_PORT] + ([FRONTEND_PORT] if with_ui else [])
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if all(codexify.port_holder(port) is None for port in ports):
            return
        time.sleep(0.2)


# On a Linux with systemd, the supervisor is a user unit, not a detached
# process. A detached process survives a terminal's HUP but not the
# logout — systemd-logind kills the user scope, and Arch ships
# KillUserProcesses=yes — so a wizard that started one gave Sam a stack
# that died the moment he closed the terminal. A user unit is the
# pattern the heartbeat daemon already uses (node.py): launchd on macOS,
# `systemd --user` on Linux, never a system one, and no root for the
# service itself.
SYSTEMD_UNIT = "codexify-supervisor.service"


def _unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT


def _systemd_present() -> bool:
    """Is this a Linux whose user manager can take the unit?

    A `which` is a PATH scan, not an execution — the hermetic contract
    (systemctl is a SYSTEM stub; the install must never happen) is kept
    by never running it in a test, only asking whether it is there.
    """
    return sys.platform != "darwin" and shutil.which("systemctl") is not None


def supervisor_unit_text(install: Path, with_ui: bool,
                         python: Path) -> str:
    """The user unit that runs the supervisor.

    The unit is rewritten on every start — `--with-ui` is a per-run
    argument, and a unit left over from a previous run is a unit that
    serves yesterday's configuration. Paths are quoted: an install under
    a Desktop path with spaces in the name is still an install.
    """
    exec_args = [str(python), str(SUPERVISOR), "--install", str(install)]
    if with_ui:
        exec_args.append("--with-ui")
    quoted = " ".join(f'"{arg}"' for arg in exec_args)
    return (
        "[Unit]\n"
        "Description=Codexify native supervisor "
        "(the backend, the workers, the interface)\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={install}\n"
        f'Environment="PYTHONPATH={TOOLS}"\n'
        f"ExecStart={quoted}\n"
        "Restart=on-failure\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _linger_enabled(runner: CommandRunner) -> bool:
    """Does this user's manager survive logout?

    A user manager without lingering dies at logout and takes its units
    with it — the very death the unit exists to prevent. A manager that
    cannot be asked (no answer, no loginctl) is not nagged about: the
    note is for a user we could read, not one we could not.
    """
    user = os.environ.get("USER") or ""
    done = runner(["loginctl", "show-user", user,
                   "--property=Linger", "--value"], timeout=15.0,
                  stream=False)
    if done.returncode != 0:
        return True
    return (done.stdout or "").strip().lower() == "yes"


def _start_under_systemd(install: Path, with_ui: bool,
                         runner: CommandRunner, python: Path) -> tuple[bool, str]:
    """Start the supervisor as a user unit, and record its pid.

    The pid file is the contract everything else already speaks —
    `supervisor_running`, the process rows, the stop escalation — so
    systemd's MainPID is written into it and nothing downstream has to
    know a unit from a Popen. `enable` makes it start at boot; `restart`
    converges a unit whose configuration this start just rewrote.
    """
    unit = _unit_path()
    try:
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(supervisor_unit_text(install, with_ui, python),
                        encoding="utf-8")
    except OSError as exc:
        return False, f"the user unit could not be written: {exc}"

    for args in (["systemctl", "--user", "daemon-reload"],
                 ["systemctl", "--user", "enable", SYSTEMD_UNIT],
                 ["systemctl", "--user", "restart", SYSTEMD_UNIT]):
        done = runner(args, cwd=install, timeout=60.0, stream=False)
        if done.returncode != 0:
            return False, (done.stderr or done.stdout or
                           f"{' '.join(args)} failed").strip()[-300:]

    pid = None
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        done = runner(["systemctl", "--user", "show", SYSTEMD_UNIT,
                       "--property=MainPID", "--value"],
                      timeout=15.0, stream=False)
        text = (done.stdout or "").strip()
        if text.isdigit() and int(text) > 0:
            pid = int(text)
            break
        time.sleep(0.5)
    if pid is None:
        return False, ("the unit was started but systemd never reported a "
                       "main pid — try `systemctl --user status "
                       + SYSTEMD_UNIT + "`")

    (install / SUPERVISOR_PID).write_text(f"{pid}\n", encoding="utf-8")
    detail = f"the supervisor is running under systemd --user (pid {pid})"
    if not _linger_enabled(runner):
        detail += (" — it dies at logout until "
                   "`sudo loginctl enable-linger $USER` is run once")
    return True, detail


def start(install: Path, with_ui: bool = False,
          runner: CommandRunner = _run_command) -> tuple[bool, str]:
    """Start the backend and the workers, under one supervisor."""
    if supervisor_running(install):
        return False, "the native supervisor is already running"
    if not venv_python(install):
        return False, "there is no environment to run from"

    # A port is released as its process dies, which is a moment after the
    # signal. A start that follows a stop gives the old socket that
    # moment before it calls the port somebody else's.
    _settle_ports(with_ui)

    squatter = _port_squatter(install, with_ui)
    if squatter:
        return False, squatter

    (install / PID_DIR).mkdir(parents=True, exist_ok=True)
    (install / LOG_DIR).mkdir(parents=True, exist_ok=True)

    # The pid files belong to the run that is starting. Last run's pids
    # are stale claims, and a stale claim read as this run's is how the
    # wrong process gets blamed for holding a port.
    for stale in (install / PID_DIR).glob("*.pid"):
        try:
            stale.unlink()
        except OSError:
            pass
    supervisor_pid_file = install / SUPERVISOR_PID
    if supervisor_pid_file.is_file():
        try:
            supervisor_pid_file.unlink()
        except OSError:
            pass

    # Linux with systemd: the supervisor goes to the user manager, so
    # it survives the terminal that started it. Everything else — the
    # macOS path, a Linux without systemd — keeps the detached process.
    if _systemd_present():
        return _start_under_systemd(install, with_ui, runner,
                                     venv_python(install))

    args = [get_stable_python(), str(SUPERVISOR), "--install", str(install)]
    if with_ui:
        args.append("--with-ui")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(TOOLS)
    try:
        process = subprocess.Popen(args, cwd=str(install), env=env,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL,
                                   start_new_session=True)
    except OSError as exc:
        return False, f"the supervisor could not be started: {exc}"
    (install / SUPERVISOR_PID).write_text(f"{process.pid}\n", encoding="utf-8")
    return True, f"the supervisor is running (pid {process.pid})"


def _stop_child_groups(install: Path, pids: list[int]) -> None:
    """Stop each child's whole process group, politely and then not.

    A child is its own session, so the group is the only thing that
    reaches what it spawned: `pnpm` leaves `vite` behind when it dies
    alone, and an orphaned vite holds the port forever.

    Only this install's own processes are stopped. A pid that belongs to
    another installation is somebody else's business, even when this
    install's pid file names it.
    """
    import signal
    for pid in pids:
        if not codexify.pid_alive(pid):
            continue
        if not _is_ours(install, pid):
            continue
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pid, sig)
            except OSError:
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and codexify.pid_alive(pid):
                time.sleep(0.1)
            if not codexify.pid_alive(pid):
                break


def stop(install: Path, runner: CommandRunner = _run_command) -> tuple[bool, str]:
    """Stop the supervisor, and with it everything it started.

    A supervisor with five children takes real seconds to die — SIGTERM
    is caught, the children are stopped one by one, and only then does
    the parent exit. The stop therefore waits for it: politely first,
    then with SIGKILL, and it reports what actually happened. A stop
    that gives up waiting but says "down" puts the next start in the
    impossible position of refusing a supervisor it was told was gone.

    Under systemd the stop is the unit's stop — the cgroup is the whole
    stack, and the children the supervisor would have reaped one by one
    go with it. The leftovers are still cleaned up by hand after, and
    the ports are still checked, because a cgroup that has been told to
    stop is not a cgroup that has finished stopping.

    The children are stopped whether or not the supervisor is alive. A
    supervisor that was killed before its own cleanup ran leaves them
    behind — each child is its own process group, so an orphaned backend
    keeps the port and the next start loses the race for it without
    saying so.

    The ports are checked last, because the ports are the truth. A stack
    that still holds one of its own ports is not down, whatever the pid
    files say. A port held by somebody else is not our stack's port:
    ours is down, and the stranger is named, because it will stand in
    the way of the next start.
    """
    pid = supervisor_running(install)
    children = _child_pids(install)
    stopped_by_systemd = False
    if _systemd_present() and _unit_path().is_file():
        done = runner(["systemctl", "--user", "stop", SYSTEMD_UNIT],
                      timeout=120.0, stream=False)
        if done.returncode != 0:
            return False, ("the supervisor could not be stopped: "
                          "`systemctl --user stop` failed — "
                          + (done.stderr or done.stdout or "").strip()[-200:])
        stopped_by_systemd = True
        # The cgroup went with the unit; read the pid again before the
        # polite-kill block mistakes a dead man for a runaway.
        pid = supervisor_running(install)
    if pid is None and not children and not stopped_by_systemd:
        return False, "the native supervisor is not running"
    if pid is not None and not stopped_by_systemd:
        try:
            os.kill(pid, 15)
        except OSError as exc:
            return False, f"the supervisor could not be stopped: {exc}"
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and supervisor_running(install):
            time.sleep(0.2)
        if supervisor_running(install):
            # Polite did not work. Force it, and wait again — a kill -9 is
            # instant for the parent, but the children are ours to reap too.
            try:
                os.kill(pid, 9)
            except OSError:
                pass
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and supervisor_running(install):
                time.sleep(0.2)
        if supervisor_running(install):
            return False, (f"the supervisor (pid {pid}) did not stop — "
                           "it may need killing by hand")

    _stop_child_groups(install, children)

    # The ports are checked last, because the ports are the truth — but
    # only about *our* stack. A port held by another installation is not
    # our stack's port: our stack is down, and the stranger is named
    # because it will stand in the way of the next start. A port we
    # cannot read the owner of is treated as ours: a stack that cannot
    # be proven down is not down.
    held = []
    strangers = []
    for port in (BACKEND_PORT, FRONTEND_PORT):
        holder = codexify.port_holder(port)
        if holder is None:
            continue
        cwd = codexify.process_cwd(holder)
        if cwd is None:
            held.append(f":{port} (pid {holder}, its working directory "
                        f"could not be read)")
        elif _is_ours(install, holder) or _is_orphaned(holder):
            held.append(f":{port} (pid {holder}, {cwd})")
        else:
            strangers.append(f":{port} (pid {holder}, {cwd})")
    if held:
        return False, ("the supervisor is down but a port is still held: "
                       + ", ".join(held))
    if strangers:
        return True, ("the stack is down — but a port it needs is held by "
                      "another installation: " + ", ".join(strangers)
                      + "; stop it before starting")
    return True, "the stack is down"


def process_rows(install: Path) -> list[Row]:
    """One row per supervised process, and whether it is alive."""
    rows: list[Row] = []
    pid_dir = install / PID_DIR
    if not pid_dir.is_dir():
        return rows
    for pid_file in sorted(pid_dir.glob("*.pid")):
        name = pid_file.stem
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            rows.append(Row(name, BAD, "the pid file is unreadable"))
            continue
        alive = codexify.pid_alive(pid)
        log = install / LOG_DIR / f"{name}.log"
        detail = f"pid {pid}"
        if not alive:
            detail = f"pid {pid} — not running"
            if log.is_file():
                tail = log.read_text(encoding="utf-8", errors="replace").strip()
                if tail:
                    detail += f": {tail.splitlines()[-1][:120]}"
        rows.append(Row(name, OK if alive else BAD, detail))
    return rows


# ------------------------------------------------------------------ the flow --

def setup(install: Optional[Path], *, explicit: Optional[str] = None,
          python: Optional[str] = None, preset: Optional[str] = None,
          model: Optional[str] = None, cloud: bool = False,
          with_ui: bool = False, dry_run: bool = False,
          launcher: Optional[str] = None, theme: Optional[str] = None,
          runner: CommandRunner = _run_command,
          http_getter: HttpGetter = None,
          stream: bool = False) -> Report:
    """The native path, end to end: detect, build, configure, migrate, run.

    Every step reports what it did, and a step that failed stops the ones
    after it — a schema that did not migrate must not have a worker started
    against it. With `stream`, the minutes-long children (the venv build,
    the pip install, the migration) print live instead of silently holding
    their output until they finish.
    """
    if stream:
        real_runner = runner

        def runner(args, **kwargs):          # noqa: F811 — the streaming twin
            # The stream flag is the wizard's, and the wizard's eye is on
            # the minutes-long builds. It is not for everything: a call
            # whose output is *data* — systemctl's MainPID answer, a
            # loginctl Linger yes/no — streams its answer onto the
            # terminal unread and returns an empty stdout, which is the
            # wizard hearing nothing while the answer is printed on
            # screen. Data calls say `stream=False` and are heard; build
            # calls say nothing and are shown.
            if kwargs.get("stream") is None:
                kwargs["stream"] = True
            return real_runner(args, **kwargs)
    report = Report(heading="CODEXIFY (native)")

    if install is None:
        report.add("the installation", BAD, codexify.install_note(explicit))
        report.exit_code = EXIT_MISSING_INSTALL
        report.verdict = "nothing was written"
        return report

    report.add("the installation", OK, str(install))

    # -- detect ------------------------------------------------------------
    rows = list(detect(install, python=python, runner=runner, http_getter=http_getter))
    missing_services = [r.label for r in rows if r.mark == BAD and r.label in ("postgres", "redis")]
    if missing_services:
        if prompt_and_fix_services(missing_services):
            # Re-run detection once services are installed and started
            rows = list(detect(install, python=python, runner=runner, http_getter=http_getter))
    for row in rows:
        report.add(row.label, row.mark, row.detail)

    blockers = [row for row in report.rows
                if row.mark == BAD and row.label in ("postgres", "redis")]
    if blockers:
        hint = brew_hint([row.label for row in blockers])
        report.exit_code = EXIT_PREREQUISITE
        report.verdict = ("the stores are missing — nothing was written. "
                          "Run the commands above, then run this again.")
        return report

    # -- the interface's dependency ---------------------------------------
    # pnpm is the interface's whole universe, and a run that asked for
    # the interface cannot have it without pnpm. It gets the same offer
    # the stores get — a terminal says yes and it is installed; a piped
    # run is told the command and exits before the venv build spends its
    # minutes on a stack that cannot finish.
    if with_ui and not shutil.which("pnpm"):
        prompt_and_fix_services(["pnpm"])
        if not shutil.which("pnpm"):
            report.add("the interface's dependencies", BAD,
                       "pnpm is not here — run: " + brew_hint(["pnpm"]))
            report.exit_code = EXIT_PREREQUISITE
            report.verdict = ("the interface was asked for and pnpm is "
                              "missing — nothing was built. Run the command "
                              "above, then run this again.")
            return report

    # -- configure ---------------------------------------------------------
    env_path = install / codexify.ENV_FILE
    existing = codexify.read_env(env_path)

    # A machine that was pointed at a remote runtime does not go probing
    # for one on localhost — the endpoint it was given is the runtime, and
    # the model it was given is what the workers get. A `--model` wins over
    # the configured one, as the more deliberate choice; a `--preset` does
    # not, because its localhost addresses are worth nothing against an
    # endpoint that was written down.
    override_base, override_model = runtime_endpoint_override(install)

    preset_id = codexify.resolve_preset(preset)
    if preset is not None and preset_id is None:
        report.add("the local runtime", BAD,
                   f"no runtime is called {preset} — known: "
                   + ", ".join(sorted(codexify.PRESETS)))
        report.exit_code = EXIT_USAGE
        report.verdict = "nothing was written"
        return report
    elif override_base:
        preset_id = "ollama"
        model = (model or override_model
                 or codexify.DEFAULT_REMOTE_RUNTIME_MODEL)
    elif preset_id is None:
        preset_id, detected_model, _ = codexify.detect_runtime(
            http_getter or codexify._http_get)
        model = model or detected_model
        if preset_id is None:
            # Nothing answered and nothing was configured. A machine with
            # no local runtime is not a broken machine — its runtime lives
            # elsewhere. Ask once, defaulting to the Mac Studio's broker;
            # hit enter and the answer is written into the `.env`, so the
            # question is asked once per machine, not once per run. A piped
            # run, a test, a dry run is not asked at all.
            if (http_getter is None and not dry_run
                    and sys.stdin.isatty()
                    and not getattr(prompt_runtime_defaults,
                                    "no_prompts", False)):
                override_base, answered_model = prompt_runtime_defaults()
                model = model or answered_model
    else:
        _, detected_model, _ = codexify.detect_runtime(
            http_getter or codexify._http_get, presets=[preset_id])
        model = model or detected_model
    if preset_id is None:
        preset_id = "ollama"  # Fallback gracefully instead of blocking

    db_url = existing.get("DATABASE_URL") or default_db_url(existing)
    redis_url = default_redis_url(existing)
    overrides = native_env_overrides(existing, preset_id, model, cloud,
                                     db_url, redis_url, None,
                                     install=install, theme=theme)
    if override_base:
        # The wiring the preset cannot know: the runtime is elsewhere, on
        # the tailnet, and every address the stack dials it by — its own
        # clients, the fallback, the health probes — points there.
        overrides.update(remote_runtime_overrides(override_base, model))

    refusal = codexify.verify_writable(install, env_path)
    if refusal:
        report.add("the configuration", BAD, refusal)
        report.exit_code = EXIT_CONFLICT
        report.verdict = "nothing was written"
        return report

    if dry_run:
        report.add("the configuration", MANUAL,
                   "dry run — nothing was written. The diff follows.")
        report.add("the diff", MANUAL, codexify.env_diff(install, overrides))
        report.exit_code = EXIT_OK
        report.verdict = "a plan, and nothing else"
        return report

    _, changed = codexify.write_env(install, overrides)
    report.add("the configuration", OK,
               (f"wrote {env_path} (mode 0600)" if changed
                else f"{env_path} is already what it would be (mode 0600)"))

    # -- build -------------------------------------------------------------
    built, detail = _with_spinner(
        "installing the environment",
        build_environment, install, python=python, runner=runner)
    report.add("the environment", OK if built else BAD, detail)
    if not built:
        report.exit_code = EXIT_PREREQUISITE
        report.verdict = "the environment could not be built"
        return report

    # -- the interface's dependencies --------------------------------------
    # The frontend is pnpm's universe, not pip's, and it is only owed when
    # the interface will run. A machine without pnpm reports it as a
    # prerequisite, the same way the brew rows do — never a crash.
    if with_ui:
        built, detail = build_frontend(install, runner=runner)
        report.add("the interface's dependencies", OK if built else BAD,
                   detail)
        if not built:
            report.exit_code = EXIT_PREREQUISITE
            report.verdict = "the interface could not be built"
            return report

    # -- database ----------------------------------------------------------
    made, detail = create_database(install, runner=runner)
    report.add("the database", OK if made else BAD, detail)
    if not made:
        report.exit_code = EXIT_PREREQUISITE
        report.verdict = "the database could not be made — nothing else ran"
        return report

    migrated, detail = _with_spinner(
        "migrating the schema", migrate, install, runner=runner)
    report.add("the schema", OK if migrated else BAD, detail)
    if not migrated:
        report.exit_code = EXIT_PARTIAL
        report.verdict = "the schema did not migrate — nothing was started"
        return report

    # -- run ---------------------------------------------------------------
    # Setup converges: a supervisor that is already up was started against
    # some earlier configuration, so it is stopped and started again — the
    # stack that ends up running must be this run's, not a previous one's.
    # The stop is waited for, not assumed: a teardown that outlives the
    # patience would otherwise leave the start refusing a supervisor it
    # was told was gone.
    #
    # The stop happens even when the supervisor is gone. A supervisor that
    # was killed leaves its children behind — they are their own process
    # groups — and they keep the ports. A setup that skipped the stop
    # would then start a backend that cannot bind, and every health
    # check would answer the leftovers.
    if supervisor_running(install) or _child_pids(install):
        stopped, detail = _with_spinner("stopping the old stack", stop,
                                         install)
        if not stopped:
            report.add("the processes", BAD, detail)
            report.exit_code = EXIT_PARTIAL
            report.verdict = "the old stack did not stop — nothing was started"
            return report
    started, detail = _with_spinner("starting the stack", start,
                                     install, with_ui=with_ui, runner=runner)
    report.add("the processes", OK if started else BAD, detail)
    if not started:
        report.exit_code = EXIT_PARTIAL
        report.verdict = "the stack did not start"
        return report

    # -- verify ------------------------------------------------------------
    # The backend takes real seconds to come up — the log shows a dozen
    # startup steps between the process starting and uvicorn answering —
    # and a probe fired the instant the supervisor has forked only ever
    # measures the boot. Wait for it to answer, then read the readiness.
    getter = http_getter or codexify._http_get
    deadline = time.monotonic() + STARTUP_TIMEOUT
    backend_pid = None
    with _spinner("waiting for the backend"):
        while time.monotonic() < deadline:
            status, _ = getter(f"{codexify.HEALTH_BACKEND}/ping", 3.0)
            backend_pid = _child_pid(install, "backend")
            if status == 200:
                # A /ping that answers is not proof that *our* backend
                # answered: a stale process left on the port answers just as
                # well, and it answers the health checks too. Waiting cannot
                # fix that — this install's backend will never get the port
                # — so the readiness report below names the holder instead.
                break
            time.sleep(1.0)
    with _spinner("checking the stack"):
        rows = codexify.readiness(install, runner=runner,
                                  http_getter=getter,
                                  skip_docker=True,
                                  skip_frontend=not with_ui,
                                  backend_pid=backend_pid,
                                  frontend_wait=FRONTEND_WAIT,
                                  schema_migrated=migrated)
    for row in rows:
        report.add(row.label, row.mark, row.detail)
    for row in process_rows(install):
        report.add(row.label, row.mark, row.detail)

    # -- the launcher ------------------------------------------------------
    # The last step, and a manual one: the stack is up, and a launcher is
    # how a person who does not live in a terminal reaches it. It is made
    # only when everything before it passed, because a launcher that opens
    # a browser at a dead stack is a false success on the Desktop.
    stack_ok = not any(row.mark == BAD for row in report.rows)
    if launcher and stack_ok:
        made, detail = make_launcher(install, launcher)
        report.add("the launcher", OK if made else MANUAL, detail)
    elif launcher:
        report.add("the launcher", MANUAL,
                   "not made — the stack did not come up, and a launcher "
                   "would open a browser at nothing")

    failed = [row for row in report.rows if row.mark == BAD]
    if failed:
        report.exit_code = EXIT_PARTIAL
        report.verdict = f"{len(failed)} check(s) failed — the stack is not ready"
    else:
        report.exit_code = EXIT_OK
        report.verdict = "every check passed"
    return report


def status(install: Optional[Path], explicit: Optional[str] = None,
           runner: CommandRunner = _run_command,
           http_getter: HttpGetter = None) -> Report:
    """What is true of the native stack right now. Writes nothing."""
    report = Report(heading="CODEXIFY (native)")
    if install is None:
        report.add("the installation", BAD, codexify.install_note(explicit))
        report.exit_code = EXIT_MISSING_INSTALL
        report.verdict = "nothing to report"
        return report
    report.add("the installation", OK, str(install))
    for row in process_rows(install):
        report.add(row.label, row.mark, row.detail)
    # The two stores, because they are the two things that can be missing
    # while everything else looks fine.
    for row in detect(install, runner=runner, http_getter=http_getter):
        if row.label in ("postgres", "redis"):
            report.add(row.label, row.mark, row.detail)
    for row in codexify.readiness(install, runner=runner,
                                   http_getter=http_getter or codexify._http_get,
                                   skip_docker=True,
                                   backend_pid=_child_pid(install, "backend")):
        report.add(row.label, row.mark, row.detail)
    failed = [row for row in report.rows if row.mark == BAD]
    if failed:
        report.exit_code = EXIT_PARTIAL
        report.verdict = f"{len(failed)} of {len(report.rows)} checks failed"
    else:
        report.exit_code = EXIT_OK
        report.verdict = "every check passed"
    return report


def down(install: Optional[Path], explicit: Optional[str] = None) -> Report:
    """Stop the native stack."""
    report = Report(heading="CODEXIFY (native)")
    if install is None:
        report.add("the installation", BAD, codexify.install_note(explicit))
        report.exit_code = EXIT_MISSING_INSTALL
        report.verdict = "nothing was stopped"
        return report
    stopped, detail = _with_spinner("stopping the stack", stop, install)
    report.add("the processes", OK if stopped else BAD, detail)
    report.exit_code = EXIT_OK if stopped else EXIT_PARTIAL
    report.verdict = "the stack is down" if stopped else "the stack is still up"
    return report


# ------------------------------------------------------------- the launcher --

LAUNCHER_APPS = ("Applications", "Desktop")
LAUNCHER_ID = "com.genesis.codexify"
# The official logo, vendored here — not read out of the submodule, whose
# frontend icons are upstream's and die with the next checkout. A drop-in
# replacement at the same two paths is the whole of changing the logo.
LOGO_PNG = ROOT / "assets" / "codexify-icon.png"
LOGO_ICNS = ROOT / "assets" / "codexify.icns"
# One body, two openers: the Mac bundle says `open`, a freedesktop
# entry says `xdg-open`. Everything else — the converge-if-not-answering,
# the GENESIS_NO_BROWSER contract, the wait for vite to bind, the API as
# the fallback — is the platform-independent behaviour of "open the
# app", and lives here once.
LAUNCHER_SCRIPT = """#!/bin/bash
# Made by `genesis codexify setup --with-launcher`. The stack is started
# only if it is not answering; a stack that is up is left alone.
#
# The interface is the thing this launcher is for, so the interface is
# what it asks about and what it starts -- with the interface, not the
# API. Opening the API would land a person on a JSON endpoint, which is
# not the app they came to open.
#
# Everything this does is written to the log. A tile clicked in an app
# grid has no terminal, so a launcher that fails quietly is one nobody
# can debug -- and "the log does not exist" cannot be allowed to mean
# both "it never ran" and "it ran and had nothing to say".
exec >> "@LOG@" 2>&1
echo "--- $(date '+%F %T') launcher started"
if ! curl -s -o /dev/null --max-time 2 http://127.0.0.1:5173/; then
  echo "the interface is not answering -- converging the stack"
  # The launcher owns the opening; the setup it calls never opens one.
  GENESIS_NO_BROWSER=1 "@GENESIS@" codexify setup --with-ui >> "@LOG@" 2>&1
  echo "setup finished (exit $?)"
else
  echo "the interface is already answering"
fi
# The browser is for the person who double-clicked this. A test run or an
# agent's shell says GENESIS_NO_BROWSER, and then the stack still
# converges and no window opens on anyone's desk.
if [ -n "$GENESIS_NO_BROWSER" ]; then
  echo "GENESIS_NO_BROWSER is set -- not opening a browser"
  exit 0
fi
# On a cold start the interface takes a moment to bind -- vite resolves
# its dependency graph before it will answer -- so it is waited for
# rather than judged on the first probe. The API is the fallback: it is
# what is guaranteed to answer.
for _ in {1..20}; do
  if curl -s -o /dev/null --max-time 2 http://127.0.0.1:5173/; then
    @OPENER@ http://127.0.0.1:5173
    echo "asked @OPENER@ to open the interface (exit $?)"
    exit 0
  fi
  sleep 1
done
echo "the interface never answered -- falling back to the API"
@OPENER@ http://127.0.0.1:8888
echo "asked @OPENER@ to open the API (exit $?)"
"""


def render_launcher_script(genesis_bin: str, log: Path, opener: str) -> str:
    """The launcher script, with its three blanks filled in.

    The blanks are `@TOKEN@`s because a shell script is full of `$`
    names, and a `$GENESIS` blank is the prefix of `$GENESIS_NO_BROWSER`:
    filling it rewrote that variable into a path, which made "is the
    browser suppressed?" true on every machine, and the launcher exited
    before opening anything. A token that is not a prefix of anything
    cannot do that.
    """
    return (LAUNCHER_SCRIPT.replace("@GENESIS@", genesis_bin)
                           .replace("@LOG@", str(log))
                           .replace("@OPENER@", opener))


# What the Linux launcher opens the interface with. A Chromium-family
# browser's --app mode is a window with no tabs and no address bar, which
# is what an app is; it gets a profile of its own so the window is
# Codexify's rather than one among the person's tabs, and a class of its
# own so the dock groups it under Codexify's icon. It is started in the
# background, because a browser window lives until it is closed and the
# launcher must not. A machine with no such browser gets a plain tab from
# its default one — Firefox has no app mode to ask for.
OPEN_APP_SCRIPT = """#!/bin/bash
url="$1"
profile="@PROFILE@"
for candidate in "$CODEXIFY_BROWSER" google-chrome-stable google-chrome \\
    chromium chromium-browser brave-browser brave microsoft-edge-stable \\
    vivaldi-stable; do
  [ -n "$candidate" ] || continue
  command -v "$candidate" >/dev/null 2>&1 || continue
  echo "showing $url as an app window with $candidate"
  "$candidate" --app="$url" --class=Codexify --user-data-dir="$profile" \\
      --no-first-run --no-default-browser-check >/dev/null 2>&1 &
  disown
  exit 0
done
echo "no Chromium-family browser found -- showing $url in the default browser"
exec xdg-open "$url"
"""


def render_open_script(profile: Path) -> str:
    return OPEN_APP_SCRIPT.replace("@PROFILE@", str(profile))


def ensure_app_opener(install: Path) -> Optional[Path]:
    """The app-window opener for this install, written if it is not there.

    The tile and the wizard's own last step open the interface the same
    way, so there is one opener and both ask for it here — the wizard
    cannot rely on the launcher having been made, because `--no-launcher`
    is a choice. Rewritten on every call: it is a few lines, and a stale
    copy would be an opener that disagrees with the code that wrote it.
    """
    opener = install / NATIVE_DIR / "open-app.sh"
    try:
        opener.parent.mkdir(parents=True, exist_ok=True)
        _write_executable(opener, render_open_script(
            install / NATIVE_DIR / "browser-profile"))
    except OSError:
        return None
    return opener


def make_launcher(install: Path, where: str,
                  genesis_bin: Optional[str] = None,
                  log_dir: Optional[Path] = None) -> tuple[bool, str]:
    """A double-clickable launcher for the stack.

    On a Mac that is a bundle on the Desktop or in /Applications. On a
    Linux desktop it is a freedesktop entry — the app-grid tile that
    GNOME and KDE and everything freedesktop show a person when they
    look for their apps. The launcher is honest about what it is in
    either shape: a small thing that runs the same `genesis codexify
    setup` a terminal would, converges an already-running stack instead
    of restarting it, and opens the interface in a browser. It is not a
    real app — the stack is processes and a browser tab — and dressing
    it up further would be a claim the stack cannot keep.

    Returns (made, detail). Everything is written atomically: a temp
    file renamed into place, so a half-written launcher never appears
    in anyone's grid or on anyone's Desktop.
    """
    genesis_bin = genesis_bin or str(Path.home() / ".local" / "bin" / "genesis")

    if sys.platform != "darwin":
        return _make_desktop_entry(install, where, genesis_bin, log_dir)

    where = where.capitalize()
    if where not in LAUNCHER_APPS:
        return False, ("the launcher goes in " +
                       " or ".join(LAUNCHER_APPS) + f", not '{where}'")

    if not Path(genesis_bin).is_file():
        return False, f"there is no genesis at {genesis_bin} to launch with"
    base = (Path("/Applications") if where == "Applications"
            else Path.home() / "Desktop")
    if not base.is_dir():
        return False, f"{base} is not there to put a launcher in"

    name = "Codexify.app"
    bundle = base / name
    if bundle.exists():
        return False, (f"{bundle} is already there — remove it first if you "
                       "want it made again")

    logs = Path(log_dir) if log_dir else install / LOG_DIR
    logs.mkdir(parents=True, exist_ok=True)

    import tempfile
    with tempfile.TemporaryDirectory(dir=base) as tmp:
        root = Path(tmp) / name
        (root / "Contents" / "MacOS").mkdir(parents=True)
        (root / "Contents" / "Resources").mkdir(parents=True)
        (root / "Contents" / "Info.plist").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            "<dict>\n"
            "    <key>CFBundleName</key><string>Codexify</string>\n"
            "    <key>CFBundleIdentifier</key><string>"
            + LAUNCHER_ID + "</string>\n"
            "    <key>CFBundleExecutable</key><string>Codexify</string>\n"
            "    <key>CFBundlePackageType</key><string>APPL</string>\n"
            "    <key>CFBundleIconFile</key><string>app</string>\n"
            "</dict>\n"
            "</plist>\n", encoding="utf-8")
        # The plist names `app` as the icon; this is the file it names.
        # A checkout without the logo makes a working bundle, not none.
        if LOGO_ICNS.is_file():
            shutil.copyfile(LOGO_ICNS, root / "Contents" / "Resources"
                            / "app.icns")
        script = (root / "Contents" / "MacOS" / "Codexify")
        script.write_text(
            render_launcher_script(genesis_bin, logs / "launcher.log", "open"),
            encoding="utf-8")
        script.chmod(0o755)
        try:
            root.rename(bundle)
        except OSError as exc:
            return False, f"the launcher could not be put in place: {exc}"
    return True, f"{bundle} — double-click starts (or converges) the stack and opens the interface"


DESKTOP_ENTRY_FILE = "codexify.desktop"


def _desktop_entry_text(install: Path, script: Path, icon: Optional[Path],
                        terminal: bool) -> str:
    """The freedesktop entry that puts Codexify in the app grid.

    `Terminal=false` is the honest setting for the entry a person clicks:
    the launcher converges the stack quietly and opens a browser, and a
    terminal window flashing open for that is theatre. The wizard's own
    runs keep their terminal — this entry is the grid tile, not the
    operator's shell.
    """
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=Codexify",
        "Comment=Start (or converge) the stack and open the interface",
        "Exec=" + str(script),
        "Categories=Utility;Development;",
    ]
    if icon is not None:
        lines.append("Icon=" + str(icon))
    if terminal:
        lines.append("Terminal=true")
    else:
        lines.append("Terminal=false")
    # The window the opener starts is Chromium's --app mode with class
    # Codexify, which is what lets the dock group it under this entry's
    # icon. But the fallback is a plain tab that will never claim to be
    # Codexify, and a shell told to expect a window shows "launching"
    # until its timeout and reads the launch as failed -- so the shell is
    # told not to wait, and the class still matches when there is one.
    lines.append("StartupWMClass=Codexify")
    lines.append("StartupNotify=false")
    lines.append("X-GNOME-UsesNotifications=false")
    return "\n".join(lines) + "\n"


def _write_executable(path: Path, text: str) -> None:
    """Write a script atomically and make it runnable.

    A temp file renamed into place: a launcher clicked mid-write gets the
    old script or the new one, never half of either.
    """
    import tempfile
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False,
                                     encoding="utf-8") as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    tmp_path.chmod(0o755)
    tmp_path.replace(path)


def _install_logo_icon(install: Path) -> Optional[Path]:
    """Put the official logo where the grid can find it, and say where.

    The logo is copied into the user's own icon directory rather than
    pointed at in the clone: the entry must keep its icon when GENESIS is
    moved or re-cloned, and the user icon directory is where the
    freedesktop spec says a user's icons live. The path is absolute, so
    the tile needs no icon-cache refresh to show it.

    A checkout without the logo falls back to the icon the frontend ships,
    and one without that gets no icon — a cog, not a missing launcher.
    """
    if LOGO_PNG.is_file():
        import hashlib
        apps = (Path.home() / ".local" / "share" / "icons" / "hicolor"
                / "256x256" / "apps")
        # The name carries a hash of the picture. A shell caches an icon by
        # its path and never notices that the file behind the path changed,
        # so a new logo under the old name keeps showing the old one until
        # the next login. A new logo under a new name cannot be cached.
        digest = hashlib.sha256(LOGO_PNG.read_bytes()).hexdigest()[:10]
        target = apps / f"codexify-{digest}.png"
        try:
            apps.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(LOGO_PNG, target)
            for stale in apps.glob("codexify*.png"):
                if stale != target:
                    stale.unlink()
            return target
        except OSError:
            pass
    fallback = install / "frontend" / "src" / "public" / "icons" / "icon-512.png"
    return fallback if fallback.is_file() else None


def _make_desktop_entry(install: Path, where: str, genesis_bin: str,
                        log_dir: Optional[Path]) -> tuple[bool, str]:
    """The Linux launcher: a freedesktop entry, and the script behind it.

    `where` is accepted for symmetry with the Mac bundle and ignored —
    there is no Desktop/Applications choice to make on a freedesktop
    desktop; the grid is the grid. The entry goes where the spec says
    user entries go (`~/.local/share/applications`), the script lives
    with the install's own runtime files, and the icon is the official
    logo — an install with no logo and no frontend icon gets an entry
    without one rather than no entry at all.

    Both files are written atomically, and an entry that already exists
    is converged, not refused: the wizard re-runs, the entry points at
    the stack that exists now, and the grid tile never goes stale.
    """
    if not Path(genesis_bin).is_file():
        return False, f"there is no genesis at {genesis_bin} to launch with"
    apps_dir = Path.home() / ".local" / "share" / "applications"
    entry_path = apps_dir / DESKTOP_ENTRY_FILE
    logs = Path(log_dir) if log_dir else install / LOG_DIR
    script = install / NATIVE_DIR / "launcher.sh"

    for target, maker in ((logs, lambda p: p.mkdir(parents=True,
                                                   exist_ok=True)),
                          (install / NATIVE_DIR,
                           lambda p: p.mkdir(parents=True, exist_ok=True))):
        try:
            maker(target)
        except OSError as exc:
            return False, f"the launcher's directory could not be made: {exc}"

    icon = _install_logo_icon(install)

    import tempfile
    # The scripts first: the entry is dead weight until they exist, and a
    # person clicking the tile between the writes must get a working app,
    # not a missing-file error. The opener goes before the launcher that
    # names it, for the same reason.
    opener = ensure_app_opener(install)
    if opener is None:
        return False, "the app-window opener could not be written"
    try:
        _write_executable(script, render_launcher_script(
            genesis_bin, logs / "launcher.log", str(opener)))
    except OSError as exc:
        return False, f"the launcher script could not be written: {exc}"

    entry = _desktop_entry_text(install, script, icon, terminal=False)
    try:
        apps_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=apps_dir,
                                         delete=False,
                                         encoding="utf-8") as tmp:
            tmp.write(entry)
            tmp_path = Path(tmp.name)
        tmp_path.replace(entry_path)
    except OSError as exc:
        return False, f"the desktop entry could not be written: {exc}"

    # The grid does not always watch the directory; the spec's refresh
    # tool is run when the desktop has one, and a desktop without it is
    # not an error — GNOME and KDE re-scan on login regardless.
    refresh = shutil.which("update-desktop-database")
    if refresh:
        subprocess.run([refresh, str(apps_dir)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True, (f"{entry_path} — the app grid entry starts (or "
                  "converges) the stack and opens the interface")
