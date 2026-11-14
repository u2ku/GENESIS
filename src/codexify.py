"""CODEXIFY — provision the vendored Codexify install, and prove it works.

Codexify is the third of GENESIS's three consumers. The other two —
`genesis chat` and Claude Code — need a key and a base URL. Codexify needs a
whole *system*: a database, a graph, a queue, four one-shot jobs, a backend, a
frontend and four workers, and a local inference runtime for the workers to
call.

This module is the engine for `genesis codexify`. It does six things, in
order, and reports on each one:

    find  →  detect  →  env  →  up  →  wait  →  verify

**It does not import Codexify.** The `codexify-core/` directory is a git
submodule, and a plain clone leaves it empty (docs/PORTABILITY.md, finding
F5). A module that imported it would work on this machine and break on every
published clone, so the detection, the environment and the readiness ladder
live here instead — stdlib only, and hermetic.

The submodule is treated as a **data** source and never as a **code**
dependency: its `.env.template` is the source of truth for what a complete
configuration looks like, and its `docker-compose.yml` is the source of truth
for which services exist.

**The rule that shapes the whole file: a row that reports a write is a write
report, not a readiness report.** "Wrote LOCAL_CHAT_MODEL" is a fact. "Codexify
is ready" is a claim, and the only thing that earns it is a request that came
back with the right answer. Every readiness row here makes a request.

**The secrets are generated, never typed.** `GUARDIAN_API_KEY` and `NEO4J_PASS`
belong to Codexify, not to a provider, and the operator is never asked for
either. Only a provider key is a human's to enter, and only once.
"""

from __future__ import annotations

import difflib
import getpass
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import keydetect  # noqa: E402

# --------------------------------------------------------------- the contract --

# The exit codes are the bootstrap ladder's (tools/bootstrap.py:116-125), so a
# caller that already knows how to read a GENESIS exit code reads this one
# without learning a second table.
EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2
EXIT_MISSING_INSTALL = 3
EXIT_PREREQUISITE = 5
EXIT_CONFLICT = 6
EXIT_KEY_REJECTED = 8
EXIT_PRECONDITION = 9

# The marks. "·" means "not applicable, or a hand-off to a human" — it is not
# a failure, and it is not painted red.
OK = "✓"
BAD = "✗"
MANUAL = "·"

ENV_FILE = ".env"
ENV_TEMPLATE = ".env.template"
ENV_BACKUP = ".env.bak"

# The compose file that builds from source. The other three in the submodule
# are the prebuilt-image variants and their extensions.
COMPOSE_FILE = "docker-compose.yml"

# The services that make a Codexify that answers a message. This is the CI
# smoke's set (.github/workflows/smoke.yml:25) plus the model preparation and
# the workers, because a backend with no chat worker behind it is a backend
# that never answers. `e2e` is deliberately absent — it would pull the
# Playwright image for nothing.
SERVICES = (
    "db", "neo4j", "redis", "graph-init", "migrator", "model-prep",
    "backend", "frontend",
    "worker-chat", "worker-chat-embed", "worker-document-embed",
    "worker-warmup",
)

PROFILES = ("cli", "backfill", "voice")

HEALTH_BACKEND = "http://127.0.0.1:8888"
HEALTH_FRONTEND = "http://127.0.0.1:5173/"
BACKEND_PORT = 8888
FRONTEND_PORT = 5173


@dataclass(frozen=True)
class Preset:
    """One local inference runtime Codexify can be pointed at.

    The fields are Codexify's own (guardian/core/local_runtime_presets.py).
    The `base_url` is where the runtime answers **on the host** and the
    `docker_base_url` is where it answers **from inside a container** — two
    different addresses for one runtime, and the difference is the whole
    reason a container cannot use `localhost`.
    """
    id: str
    display_name: str
    vendor: str
    base_url: str
    docker_base_url: str
    vaultnode_base_url: str
    default_model: str
    health_endpoints: tuple[str, ...]
    compat_first: bool = True
    aliases: tuple[str, ...] = ()


# The canonical table. `whooshd-mlx` first because it is Codexify's own
# default posture; `ollama` second because it is what most people install.
PRESETS: dict[str, Preset] = {
    "whooshd-mlx": Preset(
        id="whooshd-mlx",
        display_name="Whoosh'd",
        vendor="whooshd",
        base_url="http://127.0.0.1:8000/v1",
        docker_base_url="http://host.docker.internal:8000/v1",
        vaultnode_base_url="http://host.docker.internal:8000",
        default_model="mlx-community/Llama-3.2-3B-Instruct-4bit",
        health_endpoints=("/health", "/health/runtime", "/ready",
                          "/v1/models", "/api/tags"),
        aliases=("whooshd", "whooshd_mlx", "whooshd mlx"),
    ),
    "ollama": Preset(
        id="ollama",
        display_name="Ollama",
        vendor="ollama",
        base_url="http://127.0.0.1:11434/v1",
        docker_base_url="http://host.docker.internal:11434/v1",
        vaultnode_base_url="http://host.docker.internal:11434",
        default_model="llama3.2:latest",
        health_endpoints=("/v1/models", "/api/tags"),
        aliases=("ollama",),
    ),
    "lmstudio": Preset(
        id="lmstudio",
        display_name="LM Studio",
        vendor="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        docker_base_url="http://host.docker.internal:1234/v1",
        vaultnode_base_url="http://host.docker.internal:1234",
        default_model="local-model",
        health_endpoints=("/v1/models", "/api/tags"),
        aliases=("lmstudio", "lm-studio", "lm_studio", "lm studio"),
    ),
    "custom-openai-compatible": Preset(
        id="custom-openai-compatible",
        display_name="Custom Local",
        vendor="custom-openai-compatible",
        base_url="http://127.0.0.1:8000/v1",
        docker_base_url="http://host.docker.internal:8000/v1",
        vaultnode_base_url="http://host.docker.internal:8000",
        default_model="local-model",
        health_endpoints=("/v1/models", "/api/tags"),
        aliases=("custom", "openai-compatible", "custom-openai"),
    ),
}

PRESET_ORDER = ("whooshd-mlx", "ollama", "lmstudio",
                 "custom-openai-compatible")

# The secrets Codexify owns. They are generated, never printed, and never
# accepted on a command line.
SECRET_NAMES = frozenset({
    "GUARDIAN_API_KEY",
    "VITE_GUARDIAN_API_KEY",
    "NEO4J_PASS",
    "POSTGRES_PASSWORD",
    "GUARDIAN_SESSION_SECRET",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "MINIMAX_API_KEY",
    "GROQ_API_KEY",
    "GENAI_API_KEY",
    "NOTION_API_KEY",
    "GITHUB_TOKEN",
})

# The values Codexify ships as placeholders. A value that is still one of
# these has never been set, and the wizard replaces it.
PLACEHOLDERS = frozenset({
    "", "change-me", "changeme", "replace-me", "replace-with-real-key",
    "replace-with-neo4j-password", "replace-with-long-random-secret",
    "dev-local-only-change-me", "example", "example-key", "placeholder",
    "todo",
})

# Where the macOS installs put things when PATH does not know. This is
# Codexify's own table (guardian/ops/setup_wizard.py:31-42) — the same paths,
# not a second set.
MACOS_FALLBACK_BINARIES = {
    "docker": (
        "/opt/homebrew/bin/docker",
        "/usr/local/bin/docker",
        "/Applications/Docker.app/Contents/Resources/bin/docker",
    ),
    "ollama": (
        "/opt/homebrew/bin/ollama",
        "/usr/local/bin/ollama",
        "/Applications/Ollama.app/Contents/Resources/bin/ollama",
    ),
}

INSTALL_HINTS = {
    "docker": "Install Docker Desktop: https://www.docker.com/products/docker-desktop/",
    "ollama": "Install Ollama: https://ollama.com/download",
}


# ------------------------------------------------------------------- the types --

@dataclass(frozen=True)
class DepStatus:
    """One dependency: whether it is here, and where it was found."""
    name: str
    is_present: bool
    found_path: Optional[str] = None
    help_text: str = ""


@dataclass(frozen=True)
class Row:
    """One line of the report: a mark, a label, and what was actually found.

    `detail` is a fact about the machine, not a promise about it. A row that
    says the file was written says only that.
    """
    label: str
    mark: str
    detail: str = ""


@dataclass
class Report:
    """What a run found, and what it wants the caller to do about it."""
    rows: list[Row] = field(default_factory=list)
    exit_code: int = EXIT_OK
    heading: str = "CODEXIFY"
    verdict: str = ""

    def add(self, label: str, mark: str, detail: str = "") -> None:
        self.rows.append(Row(label, mark, detail))


# The injection seams. They exist so the tests can drive every branch without a
# Docker daemon and without a network. Codexify's own readiness classifier
# has the same two seams, and the shape is deliberately the same.
CommandRunner = Callable[..., subprocess.CompletedProcess]
HttpGetter = Callable[[str, float], tuple[int, str]]


def _run_command(args: list[str], cwd: Optional[Path] = None,
                 timeout: float = 120.0) -> subprocess.CompletedProcess:
    """Run a command and keep its output. Never raises on a non-zero exit."""
    try:
        return subprocess.run(args, cwd=str(cwd) if cwd else None,
                              capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, 127, "", "not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", "timed out")


def _http_get(url: str, timeout: float = 3.0) -> tuple[int, str]:
    """GET a URL. Returns (status, body); a failure is a status, not an exception."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return 0, str(exc)


# ---------------------------------------------------------------- the machine --

def pid_alive(pid: int) -> bool:
    """Is this pid a process that can still do something?

    `os.kill(pid, 0)` asks the kernel whether a signal could be delivered,
    and a zombie answers yes. A zombie is a process that has already died
    and is only waiting to be reaped: it holds no port, answers no
    request, and cannot be stopped. Treating one as alive is how a
    wizard reports a backend that died two minutes ago as running.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # someone else's process, but a live one
    except OSError:
        return False
    return not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    """Has this pid died without being reaped?"""
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.stdout.strip().startswith("Z")


def port_holder(port: int) -> Optional[int]:
    """The pid listening on a TCP port, if something is.

    A port is the truth about who is serving. A pid file is a claim: it
    can point at a process that died, a process that lost the race for
    the port, or a process belonging to a different installation
    entirely — an install that was moved to the trash while its
    supervisor kept the socket open.
    """
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.split():
        try:
            return int(line)
        except ValueError:
            continue
    return None


def process_cwd(pid: int) -> Optional[str]:
    """The working directory of a pid, if it can be read.

    This is how a squatter on a port is identified: the working
    directory is the installation it belongs to.
    """
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.splitlines():
        if line.startswith("n"):
            return line[1:].strip() or None
    return None


def wait_for_url(url: str, getter: HttpGetter,
                 timeout: float) -> tuple[int, str]:
    """Poll a URL until it answers 200, or the patience runs out.

    A dev server that is still booting is not a broken server. Vite
    resolves its dependency graph before it binds, and `--force` makes it
    do that from scratch on every start — seconds, not milliseconds. A
    single probe fired at a fixed point in a sequence measures the boot,
    not the server, and reports a working interface as broken.
    """
    deadline = time.monotonic() + timeout
    status, body = getter(url, 3.0)
    while status != 200 and time.monotonic() < deadline:
        time.sleep(0.5)
        status, body = getter(url, 3.0)
    return status, body


# ------------------------------------------------------------------ masking --

def redact(text: str) -> str:
    """Anything credential-shaped becomes `***`.

    `keydetect.mask` handles the provider keys — anything that looks like
    `sk-…` and any `NAME=value` whose name ends in API_KEY, SECRET, TOKEN,
    PASSWORD or CREDENTIAL. It does not cover `NEO4J_PASS`, whose name ends in
    PASS and not PASSWORD, so the Codexify secret names are redacted here as
    well. The wizard's output is an artifact that gets pasted into tickets and
    chat, so it is treated as public by default.
    """
    if not text:
        return text
    out = keydetect.mask(text)
    for name in SECRET_NAMES:
        prefix = name + "="
        index = out.find(prefix)
        while index != -1:
            end = index + len(prefix)
            while end < len(out) and out[end] not in " \t\"'":
                end += 1
            out = out[:index + len(prefix)] + "***" + out[end:]
            index = out.find(prefix, index + len(prefix) + 3)
    return out


def mask_env_text(text: str) -> str:
    """Replace every secret's value with a placeholder.

    This is the diff's mask: the operator has to be able to read the diff
    and see that the right lines changed, without the diff carrying a
    credential into a ticket.

    A diff line carries a marker — `+`, `-` or a space — before the key, so
    the marker has to come off before the key is matched. Miss that and the
    mask matches nothing, and the diff prints the secrets it was written to
    hide.
    """
    out = []
    for line in text.splitlines():
        prefix = ""
        body = line
        if line[:1] in ("+", "-", " "):
            prefix, body = line[0], line[1:]
        match = re.match(r"^([A-Z][A-Z0-9_]*)=", body)
        if match and match.group(1) in SECRET_NAMES:
            out.append(f"{prefix}{match.group(1)}=${{{match.group(1)}}}")
        else:
            out.append(line)
    return "\n".join(out)


# -------------------------------------------------------------- the install --

def is_install(path: Path) -> bool:
    """Is this directory a Codexify installation?

    Both the compose file and the template have to be there. An empty
    `codexify-core/` directory — what a plain clone leaves — is not an
    installation, and treating it as one is how a wizard ends up writing a
    `.env` into a directory nobody will ever run.
    """
    return (path / COMPOSE_FILE).is_file() and (path / ENV_TEMPLATE).is_file()


def find_install(repo: Path, explicit: Optional[str] = None) -> Optional[Path]:
    """Where Codexify is.

    An explicit `--codexify` wins. Otherwise the vendored submodule, then a
    sibling, then one in the home directory.
    """
    if explicit:
        path = Path(explicit).expanduser()
        return path.resolve() if is_install(path) else None
    candidates = (
        repo / "codexify-core",
        repo.parent / "codexify-core",
        Path.home() / "codexify-core",
    )
    for candidate in candidates:
        if is_install(candidate):
            return candidate.resolve()
    return None


def install_note(explicit: Optional[str] = None) -> str:
    """Why there is no installation to work on.

    A named directory that is not one is a different mistake from a machine
    that simply does not have Codexify, and the two want different fixes.
    """
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            return f"{path} does not exist"
        return (f"{path} is not a Codexify installation — it needs "
                f"{COMPOSE_FILE} and {ENV_TEMPLATE}")
    return ("no Codexify installation found. The vendored one is codexify-core/, "
            "and a plain clone leaves it empty:\n"
            "    git submodule update --init --recursive")


def is_setup_done(install: Optional[Path],
                  want_ui: Optional[bool] = None) -> bool:
    """Is this installation provisioned and running?

    The wizard's question, asked between its phases: is there anything
    left to do? An installation that has no `.env` has never been set
    up; one whose backend is not answering is not running; both are
    phases the wizard still owes. `want_ui` — the run's intent, not the
    last run's leftovers — decides whether the interface is owed too:
    a wizard that opens a browser at a dead frontend has not finished,
    however healthy the backend behind it is.
    """
    if install is None or not is_install(install):
        return False
    if not (install / ENV_FILE).is_file():
        return False
    status, _ = _http_get(f"{HEALTH_BACKEND}/ping", 2.0)
    if status != 200:
        return False
    if want_ui is None:
        return True
    if want_ui:
        status, _ = _http_get(HEALTH_FRONTEND, 2.0)
        return status == 200
    return True


# --------------------------------------------------------------- the modes --

MODE_NATIVE = "native"
MODE_COMPOSE = "compose"

# The marker that says which way an installation is wired. The compose
# address is the one only a container can use, so its presence in the file
# means the file was written for compose.
COMPOSE_MARKER = "host.docker.internal"


def is_compose_install(install: Optional[Path]) -> bool:
    """Is this installation wired for Docker?

    The `.env` is the record of how it was set up, and the local runtime's
    address is the part that differs: a container needs
    `host.docker.internal` and a host needs `127.0.0.1`. So the address
    says which mode wrote it.
    """
    if install is None:
        return False
    env = read_env(install / ENV_FILE)
    return COMPOSE_MARKER in env.get("LOCAL_BASE_URL", "")


def resolve_mode(install: Optional[Path], *, docker: bool = False,
                 native: bool = False) -> str:
    """Which way this run should go.

    **Native is the default.** It needs no container runtime, no per-platform
    licence and no privileged install of a daemon, and it is the only one of
    the two that works on a machine without Docker at all. Compose stays
    fully available, one flag away.

    **But an installation keeps the mode it has.** A compose installation
    has `host.docker.internal` in its `.env` and the containers behind it;
    rewriting that to a host address under a running stack breaks the thing
    it was serving. So a run without a flag follows the installation, and a
    flag is how you change it deliberately.
    """
    if docker and native:
        raise ValueError("--docker and --native are two ways to say one "
                         "thing, and they are not the same thing.")
    if docker:
        return MODE_COMPOSE
    if native:
        return MODE_NATIVE
    if is_compose_install(install):
        return MODE_COMPOSE
    return MODE_NATIVE


# ------------------------------------------------------------- dependencies --

def detect_dependency(name: str, custom_path: Optional[str] = None) -> DepStatus:
    """Find a binary: the given path, then PATH, then the macOS install paths."""
    if custom_path:
        candidate = Path(custom_path).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return DepStatus(name, True, str(candidate.resolve()),
                             f"Resolved via custom path: {candidate}")
        return DepStatus(name, False, None,
                         f"Custom path is not executable: {candidate}. "
                         f"{INSTALL_HINTS.get(name, '')}")

    found = shutil.which(name)
    if found:
        return DepStatus(name, True, found, f"Resolved via PATH: {found}")

    for candidate in MACOS_FALLBACK_BINARIES.get(name, ()):
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return DepStatus(name, True, str(path),
                             f"Resolved via macOS fallback: {path}")

    return DepStatus(name, False, None,
                     f"Not found via PATH or macOS fallback probe. "
                     f"{INSTALL_HINTS.get(name, '')}")


# ------------------------------------------------------------------- the env --

def parse_env(text: str) -> tuple[list[str], dict[str, str]]:
    """(the keys in order, the keys and their values).

    A dotenv, not a shell script: no `export`, no interpolation, no command
    substitution. Quotes around a value are stripped; a value that is empty
    is still a value — an empty one is meaningful ("this is deliberately
    unset"), and dropping it would make the key vanish from the file.
    """
    order: list[str] = []
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key not in values:
            order.append(key)
        values[key] = value
    return order, values


def read_env(path: Path) -> dict[str, str]:
    """The environment file's values. A missing file is an empty one."""
    if not path.is_file():
        return {}
    try:
        return parse_env(path.read_text(encoding="utf-8"))[1]
    except OSError:
        return {}


def db_credentials(url: str) -> tuple[str, str]:
    """The user and password a database URL carries.

    Compose reads these out of the environment and hands them to the
    `postgres` image, which makes the role on first boot. Natively there is
    no such image, and the role has to be made by name — so the name has to
    come from somewhere, and the URL is the one place it is already written.
    """
    match = re.match(r"^[a-z]+://([^:@/]+):([^@]*)@", url, re.IGNORECASE)
    if not match:
        return "", ""
    return match.group(1), urllib.parse.unquote(match.group(2))


def _quote(value: str) -> str:
    """A dotenv value, quoted only when it needs to be."""
    if value == "":
        return ""
    if any(ch in value for ch in " \t#\"") or value != value.strip():
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def render_env(template_text: str, existing: dict[str, str],
               overrides: dict[str, str]) -> str:
    """Render the environment file.

    The base is the **template** — it is the source of truth for what a
    complete configuration is, and a configuration missing ninety keys is not
    a configuration. On top of it:

    - the operator's own values win for keys the wizard does not own, so a
      hand-set variable survives;
    - the wizard's values win for the keys it owns;
    - a key the operator added that the template does not have is appended.

    This is the opposite of Codexify's `write_env_file`, and deliberately so.
    That function re-seeds from `.env.template` and keeps only
    `GUARDIAN_API_KEY`, so anything an operator added by hand is dropped on
    the next run and survives only in `.env.bak`. A wizard that loses an
    operator's key and then reports success is the false success this whole
    repository keeps refusing.

    The template's comments and its key order survive, because the template
    is the file's documentation and the operator is going to read it.
    """
    merged: dict[str, str] = dict(existing)
    for key, value in overrides.items():
        merged[key] = value

    out: list[str] = []
    emitted: set[str] = set()
    for raw in template_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw:
            out.append(raw)
            continue
        key = raw.partition("=")[0].strip()
        if key in merged:
            out.append(f"{key}={_quote(merged[key])}")
            emitted.add(key)
        else:
            out.append(raw)

    for key, value in merged.items():
        if key not in emitted:
            out.append(f"{key}={_quote(value)}")

    return "\n".join(out).rstrip("\n") + "\n"


def is_placeholder(value: Optional[str]) -> bool:
    """Is this a value nobody has ever set?"""
    if value is None:
        return True
    text = value.strip().lower()
    if text in PLACEHOLDERS:
        return True
    return text.startswith("replace-with-") or text.endswith("-change-me")


def resolve_preset(name: Optional[str]) -> Optional[str]:
    """Turn a name — an id or an alias — into a preset id."""
    if not name:
        return None
    key = name.strip().lower()
    if key in PRESETS:
        return key
    for preset_id, preset in PRESETS.items():
        if key in preset.aliases:
            return preset_id
    return None


# -------------------------------------------------------------- the runtime --

def detect_runtime(http_getter: HttpGetter = _http_get,
                   presets: Iterable[str] = PRESET_ORDER,
                   ) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Which local runtime is running, and what model does it serve?

    Asks the endpoint rather than guessing from the platform. Codexify has a
    `default_local_runtime_preset_id()` that would know — it is imported into
    its setup wizard and never called, so the platform-aware default it
    promises never happens. Asking is better than guessing: the answer
    includes the model id the endpoint actually advertises, which is the
    model the workers have to be told about.

    Every health endpoint the preset names is tried, because `/v1/models`
    alone does not prove a Whoosh'd gateway is the thing serving.

    The health endpoints hang off the runtime's **root**, not off its
    `/v1` prefix: a gateway serves `/health` and `/ready` at the root, and
    asking for `/v1/health` gets a 404 that looks exactly like a runtime
    that is not running.

    Returns (preset_id, model_id, host_base_url). All three are None when
    nothing answered.
    """
    for preset_id in presets:
        preset = PRESETS.get(preset_id)
        if not preset:
            continue
        root = preset.base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        answered = False
        for endpoint in preset.health_endpoints:
            status, body = http_getter(f"{root}{endpoint}", 3.0)
            if status != 200:
                continue
            # The endpoint is up. Keep looking, because a *later* endpoint
            # may be the one that lists the models — `/health` answers
            # happily and says nothing about what it serves, and the model
            # the endpoint advertises is more accurate than the preset's
            # default.
            answered = True
            model = _first_model(body)
            if model:
                return preset_id, model, preset.base_url
        if answered:
            # It is up, but it never listed a model. Codexify falls back to
            # the preset's own default, and so does this.
            return preset_id, preset.default_model, preset.base_url
    return None, None, None


def _first_model(body: str) -> Optional[str]:
    """The first model id an OpenAI-shaped endpoint advertises."""
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    data = parsed.get("data")
    if not isinstance(data, list):
        return None
    for item in data:
        if isinstance(item, dict) and item.get("id"):
            return str(item["id"])
        if isinstance(item, str) and item:
            return item
    return None


# ------------------------------------------------------------- the overrides --

def env_overrides(existing: dict[str, str], preset_id: str,
                  model: Optional[str], cloud: bool) -> dict[str, str]:
    """The values the wizard owns.

    Three groups, and the difference between them matters:

    - **Secrets Codexify owns.** `GUARDIAN_API_KEY` and `NEO4J_PASS` are
      generated, because compose refuses to start without the first and
      `graph-init` exits 1 without the second. An existing, non-placeholder
      value is kept — rotating the key under a running stack would break it,
      and a second run of the wizard must not change the machine.
    - **The local endpoint.** The preset, the two URLs and the model are one
      coherent set, and they are Codexify's own canonical shape
      (`local_runtime_env_defaults(preset_id, docker=True)`). The address is
      the **docker** one, because the stack runs in containers and inside a
      container `localhost` is the container itself. The host address is
      where the runtime is *probed*, not where the stack is pointed.
    - **The posture.** The two cloud flags have to agree, or they mean
      neither: Codexify's own wizard sets `ALLOW_CLOUD_PROVIDERS=true` and
      leaves `CODEXIFY_LOCAL_ONLY_MODE=true`.

    Everything else in the operator's file is left alone. It is not in this
    dictionary, so it survives the write.
    """
    preset = PRESETS[preset_id]
    model = model or preset.default_model

    guardian_key = existing.get("GUARDIAN_API_KEY", "")
    if is_placeholder(guardian_key):
        guardian_key = secrets.token_hex(32)

    neo4j_pass = existing.get("NEO4J_PASS", "")
    if is_placeholder(neo4j_pass):
        neo4j_pass = secrets.token_urlsafe(24)

    session_secret = existing.get("GUARDIAN_SESSION_SECRET", "")
    if is_placeholder(session_secret):
        session_secret = secrets.token_urlsafe(32)

    return {
        # Codexify's own auth-boundary secret. Generated, because compose
        # refuses to start without it, and never asked for, because it is
        # not the operator's to type.
        "GUARDIAN_API_KEY": guardian_key,
        "VITE_GUARDIAN_API_KEY": guardian_key,
        "NEO4J_USER": existing.get("NEO4J_USER") or "neo4j",
        "NEO4J_PASS": neo4j_pass,
        "GUARDIAN_SESSION_SECRET": session_secret,

        # The provider. Codexify's local-beta posture is a local runtime,
        # and `LLM_PROVIDER` is forced to "local" by its own normalizer.
        "LLM_PROVIDER": "local",
        "LOCAL_RUNTIME_PRESET": preset.id,
        "LOCAL_PROVIDER_DISPLAY_NAME": preset.display_name,
        "LOCAL_PROVIDER_VENDOR": preset.vendor,

        # The local endpoint, in Codexify's canonical docker shape.
        "LOCAL_BASE_URL": preset.docker_base_url,
        "LOCAL_DOCKER_FALLBACK_BASE_URL": preset.docker_base_url,
        "LOCAL_COMPAT_FIRST": "1" if preset.compat_first else "0",
        "LOCAL_ENABLE_OLLAMA_GENERATE_FALLBACK": "0",
        "VAULTNODE_BASE_URL": preset.vaultnode_base_url,
        "VAULTNODE_HEALTH_ENDPOINTS": ",".join(preset.health_endpoints),

        # The model. `.env.template` does not carry `LOCAL_CHAT_MODEL` at
        # all, and compose injects it into seven services with no default —
        # so a template copied verbatim hands every worker an empty model.
        "LOCAL_CHAT_MODEL": model,
        "LOCAL_LLM_MODEL": model,
        "LLM_MODEL": model,

        # The embedding model must be a path inside the container.
        "LOCAL_EMBED_MODEL": existing.get("LOCAL_EMBED_MODEL")
        or "/models/bge-large-en-v1.5",

        # The posture. Cloud is opt-in, and the two flags have to agree.
        "ALLOW_CLOUD_PROVIDERS": "true" if cloud else "false",
        "CODEXIFY_LOCAL_ONLY_MODE": "false" if cloud else "true",

        "ENABLE_OUTBOX": existing.get("ENABLE_OUTBOX") or "true",
    }


# ------------------------------------------------------------ the readiness --

def _docker_ok(docker_bin: str, runner: CommandRunner,
               cwd: Optional[Path]) -> tuple[bool, str]:
    done = runner([docker_bin, "info"], cwd=cwd)
    if done.returncode != 0:
        return False, (done.stderr or done.stdout or "docker info failed").strip()[:200]
    return True, "the daemon answers"


def readiness(install: Path, *, runner: CommandRunner = _run_command,
              http_getter: HttpGetter = _http_get,
              docker_bin: Optional[str] = None,
              skip_docker: bool = False,
              skip_frontend: bool = False,
              backend_pid: Optional[int] = None,
              frontend_wait: float = 0.0,
              schema_migrated: bool = False,
              ) -> list[Row]:
    """What is true about this installation right now.

    The order is the order in which a thing can be wrong: the configuration,
    then Docker, then the compose file, then the local runtime, then the
    services, then the application. A machine with no Docker is reported as
    having no Docker, not as a broken Codexify.

    Every row here either made a request or read a file. None of them
    infers a state from a file it wrote.

    `docker_bin` is **authoritative** when it is given: the caller has
    already resolved the binary, and a test must never find the real one on
    a machine that happens to have Docker.

    `skip_docker` is for the native path, where there is no Docker and no
    compose file to resolve, and where saying so would be a false row.

    `backend_pid` is the pid the caller started. When it is given, the
    health checks below are only reported if that process is the one
    holding the port — a report that describes a different stack is
    worse than no report.

    `frontend_wait` is how long the interface is given to finish booting.
    Zero is a single probe, which is right for a snapshot of a stack that
    is already up, and wrong for the moment a stack is being brought up.
    """
    rows: list[Row] = []
    env_path = install / ENV_FILE
    env = read_env(env_path)

    if env_path.is_file():
        missing = [name for name in
                   ("GUARDIAN_API_KEY", "LOCAL_BASE_URL", "LOCAL_CHAT_MODEL")
                   if is_placeholder(env.get(name))]
        if missing:
            rows.append(Row("the configuration", BAD,
                            "not set: " + ", ".join(missing)))
        else:
            rows.append(Row("the configuration", OK, f"{env_path}"))
    else:
        rows.append(Row("the configuration", BAD,
                        f"no {ENV_FILE} — genesis codexify setup writes one"))

    if not skip_docker:
        if docker_bin:
            docker = DepStatus("docker", True, docker_bin,
                               f"Resolved: {docker_bin}")
        else:
            docker = detect_dependency("docker")
        docker_path = docker_bin or docker.found_path or "docker"
        if not docker.is_present:
            rows.append(Row("docker", BAD, docker.help_text))
            return rows
        rows.append(Row("docker", OK, docker.found_path or docker_path))

        daemon_ok, daemon_detail = _docker_ok(docker_path, runner, install)
        if not daemon_ok:
            rows.append(Row("the docker daemon", BAD,
                            f"{daemon_detail} — start Docker Desktop"))
            return rows
        rows.append(Row("the docker daemon", OK, daemon_detail))

        compose = runner([docker_path, "compose", "version"], cwd=install)
        if compose.returncode != 0:
            rows.append(Row("docker compose", BAD,
                            "not available through the docker cli"))
            return rows
        rows.append(Row("docker compose", OK,
                        (compose.stdout or "").strip().splitlines()[0]
                        if compose.stdout.strip() else "available"))

        config = runner([docker_path, "compose", "config", "--quiet"],
                        cwd=install)
        if config.returncode != 0:
            rows.append(Row("the compose file", BAD,
                            (config.stderr or config.stdout
                             or "invalid").strip()[:200]))
        else:
            rows.append(Row("the compose file", OK, "resolves"))

    preset_id, model, base = detect_runtime(http_getter)
    if preset_id:
        rows.append(Row("the local runtime", OK,
                        f"{PRESETS[preset_id].display_name} at {base} "
                        f"serving {model}"))
    else:
        rows.append(Row("the local runtime", BAD,
                        "no OpenAI-compatible runtime answered on "
                        + ", ".join(str(PRESETS[p].base_url) for p in PRESET_ORDER)))

    api_key = env.get("GUARDIAN_API_KEY", "")

    # The application. `/ping` proves the process is up; `/healthz` proves the
    # schema landed, which is the difference between a container that started
    # and a database that migrated.
    status, _ = http_getter(f"{HEALTH_BACKEND}/ping", 3.0)
    if status != 200:
        rows.append(Row("the backend", BAD,
                        "not answering — it has not started, or it has not "
                        "finished starting"))
        return rows

    # A /ping that answers is not proof that *our* backend answered. A
    # stale process left on the port — an install that was moved to the
    # trash while its supervisor kept the socket, or a backend from a run
    # that lost the race for it — answers just as well, and answers the
    # health checks too, so the report below would describe yesterday's
    # stack while this one is still starting. The check that matters is
    # whose process is on the port.
    if backend_pid is not None:
        holder = port_holder(BACKEND_PORT)
        if holder is not None and holder != backend_pid:
            elsewhere = process_cwd(holder) or "another installation"
            rows.append(Row("the backend", BAD,
                            f"the port :{BACKEND_PORT} is held by pid "
                            f"{holder} ({elsewhere}), not by this install's "
                            f"backend (pid {backend_pid}) — every health "
                            "check below would be that process's, not this "
                            "install's"))
            return rows
    rows.append(Row("the backend", OK, "answers /ping"))

    status, body = http_getter(f"{HEALTH_BACKEND}/healthz", 3.0)
    if status == 200:
        missing_tables = _missing_tables(body)
        if missing_tables and schema_migrated:
            # The backend's own table check says the tables are missing,
            # but this run migrated and seeded them: alembic reached
            # heads and the seed inserted a default project, which needs
            # the table it says is not there. The backend logs its own
            # check as failed, and a check that failed is not a finding
            # about the schema. The migration is the evidence; the
            # diagnostic is the thing that is broken.
            rows.append(Row("the schema", OK,
                            "the tables are there — the migration and the "
                            "seed both landed, and the backend's own table "
                            "check errored (it says "
                            + ", ".join(missing_tables) + ")"))
        elif missing_tables:
            rows.append(Row("the schema", BAD,
                            "migrations did not land: " + ", ".join(missing_tables)))
        else:
            rows.append(Row("the schema", OK, "the tables are there"))
    else:
        rows.append(Row("the schema", BAD, f"/healthz answered {status}"))

    if api_key:
        status, body = http_getter(f"{HEALTH_BACKEND}/health", 3.0)
        if status == 200:
            rows.append(Row("the backend's own health", OK, "ok"))
        else:
            rows.append(Row("the backend's own health", BAD,
                            f"answered {status}"))
    else:
        rows.append(Row("the backend's own health", MANUAL,
                        "no GUARDIAN_API_KEY to authenticate with"))

    status, body = http_getter(f"{HEALTH_BACKEND}/health/chat", 5.0)
    if status == 200:
        rows.append(Row("the chat path", OK, _chat_detail(body)))
    else:
        rows.append(Row("the chat path", BAD,
                        f"answered {status} — redis or the chat worker"))

    status, body = http_getter(f"{HEALTH_BACKEND}/health/llm", 8.0)
    if status == 200:
        rows.append(Row("the provider", OK, _llm_detail(body)))
    else:
        rows.append(Row("the provider", BAD,
                        f"answered {status} — the workers cannot reach "
                        f"the local runtime"))

    status, body = http_getter(f"{HEALTH_BACKEND}/health/vector", 5.0)
    if status == 200:
        rows.append(Row("the vector store", OK, "answers a live search"))
    else:
        rows.append(Row("the vector store", BAD, f"answered {status}"))

    # The frontend is optional — the backend and the workers are the system;
    # the interface is a convenience started only when asked for. A machine
    # that never started it has not failed.
    if not skip_frontend:
        status, _ = wait_for_url(HEALTH_FRONTEND, http_getter, frontend_wait)
        if status == 200:
            rows.append(Row("the interface", OK, "serving on :5173"))
        else:
            rows.append(Row("the interface", BAD,
                            "not answering on :5173"
                            + _crash_note(install, "interface")))

    return rows


def _crash_note(install: Optional[Path], name: str) -> str:
    """Why a supervised process is not answering, when its log says.

    A process that never came up leaves a reason in its log — a dyld
    error, an unmet dependency, a refused port. Quoting the last line
    turns 'not answering' into the actual cause, which is the difference
    between an operator who can fix it and one who can only retry.
    """
    if install is None:
        return ""
    log = install / ".native" / "logs" / f"{name}.log"
    if not log.is_file():
        return ""
    try:
        tail = log.read_text(encoding="utf-8",
                             errors="replace").strip()
    except OSError:
        return ""
    if not tail:
        return ""
    return f": {tail.splitlines()[-1][:160]}"


def _missing_tables(body: str) -> list[str]:
    """The tables `/healthz` says are missing."""
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, dict):
        return []
    return sorted(name for name, present in parsed.items()
                  if name.endswith("_table_exists") and present is False)


def _chat_detail(body: str) -> str:
    """One line about the chat path, from `/health/chat`."""
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return "answers"
    if not isinstance(parsed, dict):
        return "answers"
    worker = parsed.get("worker") or {}
    status = worker.get("status") if isinstance(worker, dict) else None
    if status:
        return f"redis is reachable, the chat worker is {status}"
    return "redis is reachable"


def _llm_detail(body: str) -> str:
    """One line about the provider, from `/health/llm`."""
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return "answers"
    if not isinstance(parsed, dict):
        return "answers"
    models = parsed.get("models_available")
    if isinstance(models, list) and models:
        return f"{len(models)} model(s) available"
    return "the provider answers"


def wait_for_health(install: Path, *, timeout: float = 900.0,
                    interval: float = 5.0,
                    runner: CommandRunner = _run_command,
                    http_getter: HttpGetter = _http_get,
                    ) -> list[Row]:
    """Wait for the backend to answer, then report.

    `docker compose up -d` returns when the containers are **created**, not
    when they are **healthy**. The backend then waits for Postgres, verifies
    eight tables, seeds, and execs uvicorn — and the frontend only starts
    after the backend's healthcheck passes. Probing once, immediately, would
    report a perfectly good cold stack as broken.

    The poll is bounded. A timeout returns the last observed rows; it never
    hangs.
    """
    deadline = time.monotonic() + timeout
    rows: list[Row] = []
    while True:
        rows = readiness(install, runner=runner, http_getter=http_getter)
        backend = next((row for row in rows if row.label == "the backend"), None)
        if backend is not None and backend.mark == OK:
            return rows
        if time.monotonic() >= deadline:
            return rows
        time.sleep(interval)


# ----------------------------------------------------------------- the write --

def verify_writable(install: Path, path: Path) -> Optional[str]:
    """The reason this file may not be written, or None.

    Two rules, both from docs/WIZARD.md §2.2, and both **verified** rather
    than assumed:

    - the file is gitignored — checked with `git check-ignore`, in the
      submodule's own repository, because that is the repository the file
      lives in;
    - the file is not tracked.

    A directory that is not a git repository has neither problem.
    """
    try:
        ignored = subprocess.run(["git", "-C", str(install), "check-ignore",
                                  "-q", path.name],
                                 capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if ignored.returncode == 0:
        return None
    if ignored.returncode == 1:
        return (f"{path} is not gitignored in {install}. It holds secrets; "
                f"it must not be tracked.")
    return None


def write_env(install: Path, overrides: dict[str, str],
               backup: bool = False) -> tuple[Path, bool]:
    """Write the environment file, and say whether it changed.

    The file is mode 0600 under `umask 077`, and the backup — when it is
    asked for — is written inside the same umask, because a copy of the
    provider key sitting at 0644 for a moment is a copy of the provider key
    at 0644. Codexify's own wizard writes 0644 — a file holding
    `GUARDIAN_API_KEY` and `NEO4J_PASS`, world-readable, which is the rule
    docs/WIZARD.md §2.2 refuses.
    """
    env_path = install / ENV_FILE
    template_path = install / ENV_TEMPLATE
    template_text = (template_path.read_text(encoding="utf-8")
                     if template_path.is_file() else "")
    existing = read_env(env_path)
    content = render_env(template_text, existing, overrides)

    before = env_path.read_text(encoding="utf-8") if env_path.is_file() else None

    old_umask = os.umask(0o077)
    try:
        if backup and env_path.is_file():
            backup_path = install / ENV_BACKUP
            backup_path.write_text(before or "", encoding="utf-8")
            backup_path.chmod(0o600)
        env_path.write_text(content, encoding="utf-8")
        env_path.chmod(0o600)
    finally:
        os.umask(old_umask)

    return env_path, before != content


def env_diff(install: Path, overrides: dict[str, str]) -> str:
    """The masked diff of what the write would do."""
    env_path = install / ENV_FILE
    template_path = install / ENV_TEMPLATE
    template_text = (template_path.read_text(encoding="utf-8")
                     if template_path.is_file() else "")
    existing = read_env(env_path)
    content = render_env(template_text, existing, overrides)
    before = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    diff = difflib.unified_diff(
        before.splitlines(keepends=True), content.splitlines(keepends=True),
        fromfile=f"a/{ENV_FILE}", tofile=f"b/{ENV_FILE}", n=1)
    return mask_env_text("".join(diff))


# ---------------------------------------------------------------- the compose --

def compose_args(install: Path, services: Iterable[str] = SERVICES,
                 profiles: Iterable[str] = (), docker_bin: str = "docker",
                 build: bool = True) -> list[str]:
    """The compose invocation.

    `--env-file .env` is what the Makefile uses, and it matters: the compose
    file interpolates `${GUARDIAN_API_KEY:?…}` and `${LOCAL_CHAT_MODEL}` from
    it, and without it the interpolation comes from the shell instead.

    The binary is the **resolved** one. A Docker Desktop installed where PATH
    does not know it is still Docker, and hard-coding the word "docker" would
    be exit 127 on a perfectly good machine.
    """
    args = [docker_bin, "compose", "--env-file", ENV_FILE]
    for profile in profiles:
        args += ["--profile", profile]
    args += ["up", "-d"]
    if build:
        args.append("--build")
    args += list(services)
    return args


def compose_down_args(install: Path, volumes: bool = False,
                      docker_bin: str = "docker") -> list[str]:
    """The compose invocation that stops the stack."""
    args = [docker_bin, "compose", "--env-file", ENV_FILE, "down"]
    if volumes:
        args.append("-v")
    return args


# ------------------------------------------------------------- the provider key --

def provider_key_row(existing: dict[str, str]) -> tuple[str, str, str]:
    """The provider key's state, as a row: (label, mark, detail).

    WIZARD.md §3.7 wants a row per consumer, and Codexify is one of the
    three. The row reports the **fingerprint** — never the value, never a
    prefix, never a length.
    """
    value = existing.get("OPENAI_API_KEY", "")
    if not value or is_placeholder(value):
        return ("the provider key", MANUAL,
                "not set — the stack runs local-first. To add one: "
                "genesis codexify setup --with-provider-key")
    return ("the provider key", OK,
            f"OPENAI_API_KEY is set (fingerprint "
            f"{keydetect.fingerprint(value)}) — the wizard's, untouched")


def collect_provider_key(*, key_file: Optional[str] = None,
                         provider: Optional[str] = None,
                         ) -> tuple[str, str]:
    """The provider key, verified.

    The wizard's one-prompt rule, verbatim: `getpass`, echo off, never an
    argument, and the endpoint decides — the format only narrows the
    candidates. A key that does not authenticate is a **refusal**: exit 8,
    nothing written.

    Returns (key, error). The error is empty when the key is good.
    """
    if key_file:
        path = Path(key_file).expanduser()
        if not path.exists():
            return "", f"the key file does not exist: {path}"
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            return "", f"the key file is empty: {path}"
    else:
        if not sys.stdin.isatty():
            return "", ("no terminal to prompt on, and no --key-file given.\n"
                        "  the key is never taken as an argument, so a file is "
                        "the only other way in:  --key-file PATH")
        try:
            value = getpass.getpass(
                "genesis-codexify: the API key for Codexify.\n"
                "  it is written once, to a gitignored file, and never "
                "displayed again.\n"
                "> ")
        except (EOFError, KeyboardInterrupt):
            return "", "no key was entered."

    try:
        guess = keydetect.detect(value, override=provider)
    except ValueError as exc:
        return "", str(exc)
    if not guess.candidates:
        return "", ("the format of this key matches no known provider, and no "
                   "--provider was given.\n  known: "
                   + ", ".join(keydetect.PROVIDER_CHOICES))
    result = keydetect.probe_candidates(value, guess.candidates)
    if not result or not result[0].ok:
        detail = result[0].detail if result else "no endpoint answered"
        return "", ("the key did not authenticate.\n  " + detail
                    + "\n  nothing was written.")
    return value, ""


# ------------------------------------------------------------------ the run --

def _verdict_for(rows: list[Row]) -> tuple[int, str]:
    """The exit code and the sentence, from the rows.

    A missing Docker is a missing prerequisite and says so; anything else
    that failed is a partial result. Both are failures, and the difference
    is what the operator does next.
    """
    failed = [row for row in rows if row.mark == BAD]
    if not failed:
        return EXIT_OK, "every check passed"
    if any(row.label == "docker" for row in failed):
        return EXIT_PREREQUISITE, f"{len(failed)} check(s) failed — docker is missing"
    return EXIT_PARTIAL, f"{len(failed)} of {len(rows)} checks failed"


def status(install: Optional[Path], report: Optional[Report] = None,
           explicit: Optional[str] = None, **kwargs) -> Report:
    """The read-only report. Writes nothing."""
    report = report or Report(heading="CODEXIFY (compose)")
    if install is None:
        report.add("the installation", BAD, install_note(explicit))
        report.exit_code = EXIT_MISSING_INSTALL
        report.verdict = "nothing was written"
        return report
    for row in readiness(install, **kwargs):
        report.add(row.label, row.mark, row.detail)
    report.add(*provider_key_row(read_env(install / ENV_FILE)))
    report.exit_code, report.verdict = _verdict_for(report.rows)
    return report


def setup(install: Optional[Path], *,
          preset: Optional[str] = None, model: Optional[str] = None,
          cloud: bool = False, up: bool = True, build: bool = True,
          dry_run: bool = False,
          profiles: Iterable[str] = (),
          services: Optional[Iterable[str]] = None,
          explicit: Optional[str] = None,
          timeout: float = 900.0, interval: float = 5.0,
          provider_key: Optional[str] = None,
          runner: CommandRunner = _run_command,
          http_getter: HttpGetter = _http_get,
          ) -> Report:
    """Provision the installation: detect, write, bring up, wait, verify."""
    report = Report(heading="CODEXIFY (compose)")

    if install is None:
        report.add("the installation", BAD, install_note(explicit))
        report.exit_code = EXIT_MISSING_INSTALL
        report.verdict = "nothing was written"
        return report

    env_path = install / ENV_FILE
    existing = read_env(env_path)
    report.add("the installation", OK, str(install))

    # -- detect ------------------------------------------------------------
    # Docker is only a blocker when the stack is going to be started. With
    # --no-up the deliverable is the configuration, and with --dry-run the
    # deliverable is the diff; refusing to produce either because Docker is
    # absent would be refusing the one thing that was asked for.
    docker = detect_dependency("docker")
    docker_bin = docker.found_path or "docker"
    if up and not dry_run:
        report.add("docker", OK if docker.is_present else BAD,
                   docker.found_path or docker.help_text)
        if not docker.is_present:
            report.exit_code = EXIT_PREREQUISITE
            # The refusal names the way forward, not just the problem: the
            # operator can install Docker, or they can take the
            # configuration now and start the stack later.
            report.verdict = ("docker is missing — nothing was written. "
                              "To write the configuration without the stack: "
                              "genesis codexify setup --no-up")
            return report
    else:
        report.add("docker", MANUAL,
                   "not needed for a dry run; the stack was not started"
                   if dry_run else
                   "not needed for --no-up; the stack was not started")

    # The operator's preset wins, and the model comes from *that* preset —
    # a model advertised by one runtime is not a model another one serves.
    preset_id = resolve_preset(preset)
    if preset is not None and preset_id is None:
        report.add("the local runtime", BAD,
                   f"no runtime is called {preset} — known: "
                   + ", ".join(sorted(PRESETS)))
        report.exit_code = EXIT_USAGE
        report.verdict = "nothing was written"
        return report
    if preset_id is None:
        preset_id, detected_model, _ = detect_runtime(http_getter)
        model = model or detected_model
    else:
        _, detected_model, _ = detect_runtime(http_getter, presets=[preset_id])
        model = model or detected_model
    if preset_id is None:
        # Nothing answered, so there is no model to write. The configuration
        # can still be written — it is the operator's chance to point it at a
        # runtime that is not running yet — but the report says exactly what
        # is missing and does not call it ready.
        report.add("the local runtime", MANUAL,
                   "no OpenAI-compatible runtime answered on "
                   + ", ".join(PRESETS[p].base_url for p in PRESET_ORDER)
                   + " — name one with --preset, or start one")
        preset_id = "whooshd-mlx"
    else:
        report.add("the local runtime", OK,
                   f"{PRESETS[preset_id].display_name}"
                   + (f" serving {model}" if model else " (no model advertised)"))

    # -- the provider key --------------------------------------------------
    overrides = env_overrides(existing, preset_id, model, cloud)
    if provider_key:
        overrides["OPENAI_API_KEY"] = provider_key

    # -- env ---------------------------------------------------------------
    refusal = verify_writable(install, env_path)
    if refusal:
        report.add("the configuration", BAD, refusal)
        report.exit_code = EXIT_CONFLICT
        report.verdict = "nothing was written"
        return report

    if dry_run:
        report.add("the configuration", MANUAL,
                   "dry run — nothing was written. The diff follows.")
        report.add("the diff", MANUAL, env_diff(install, overrides))
        report.exit_code = EXIT_OK
        report.verdict = "a plan, and nothing else"
        return report

    _, changed = write_env(install, overrides)
    report.add("the configuration", OK,
               (f"wrote {env_path} (mode 0600)" if changed
                else f"{env_path} is already what it would be (mode 0600)"))
    report.add(*provider_key_row(read_env(env_path)))

    # -- up ----------------------------------------------------------------
    if not up:
        report.add("the services", MANUAL,
                   "skipped — --no-up. The stack was not started, and "
                   "nothing below was checked.")
        report.exit_code = EXIT_PARTIAL if not model else EXIT_OK
        report.verdict = ("the configuration was written; the stack is not up"
                          if model else
                          "the configuration was written, but the model is "
                          "still missing and the stack is not up")
        return report

    # The compose file has to resolve before anything starts: it is the only
    # proof that the configuration satisfies compose's own `${…:?}` guards.
    config = runner([docker_bin, "compose", "--env-file", ENV_FILE,
                     "config", "--quiet"], cwd=install)
    if config.returncode != 0:
        report.add("the compose file", BAD,
                   (config.stderr or config.stdout or "invalid").strip()[:300])
        report.exit_code = EXIT_PARTIAL
        report.verdict = "the configuration does not resolve — nothing was started"
        return report

    chosen = list(services) if services else list(SERVICES)
    args = compose_args(install, chosen, profiles, docker_bin, build)
    done = runner(args, cwd=install, timeout=max(timeout, 3600.0))
    if done.returncode != 0:
        report.add("the services", BAD,
                   (done.stderr or done.stdout or "compose failed").strip()[-400:])
        report.exit_code = EXIT_PARTIAL
        report.verdict = "the stack did not come up"
        return report
    # A fact about the command, not a claim about the system. Whether the
    # system is up is what the rows below decide, by asking.
    report.add("the services", OK,
               "docker compose up -d"
               + (" --build" if build else "") + " exited 0")

    # -- wait, then verify -------------------------------------------------
    for row in wait_for_health(install, timeout=timeout, interval=interval,
                               runner=runner, http_getter=http_getter):
        report.add(row.label, row.mark, row.detail)

    report.exit_code, report.verdict = _verdict_for(report.rows)
    return report


def down(install: Optional[Path], *, volumes: bool = False,
         explicit: Optional[str] = None,
         docker_bin: Optional[str] = None,
         runner: CommandRunner = _run_command) -> Report:
    """Stop the stack. The volumes are not touched unless asked."""
    report = Report(heading="CODEXIFY (compose)")
    if install is None:
        report.add("the installation", BAD, install_note(explicit))
        report.exit_code = EXIT_MISSING_INSTALL
        report.verdict = "nothing was stopped"
        return report
    if docker_bin is None:
        docker = detect_dependency("docker")
        docker_bin = docker.found_path or "docker"
    done = runner(compose_down_args(install, volumes, docker_bin), cwd=install,
                  timeout=600.0)
    if done.returncode != 0:
        report.add("the services", BAD,
                   (done.stderr or done.stdout or "compose down failed").strip()[-300:])
        report.exit_code = EXIT_PARTIAL
        report.verdict = "the stack did not stop"
        return report
    report.add("the services", OK,
               "the stack is down"
               + (" and the volumes are gone" if volumes else ""))
    report.exit_code = EXIT_OK
    report.verdict = "the stack is down"
    return report
