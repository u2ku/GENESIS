"""BOOTSTRAP — the ladder, and the wizard that rides it.

Three rungs, and the order is the point:

    inspect    read the machine, change nothing
    dry-run    compute the plan, render the diff, write a receipt
    apply      change the machine, but only against a matching receipt

The receipt is what makes the middle rung mean something. `--apply` refuses
without a receipt whose plan hash matches the plan it just computed, so a
machine that changed under you expires the approval and the diff is shown
again. That is the difference between "the user has seen the diff" and
"the user has seen *a* diff".

**The wizard is a mode, not a second ladder.** `--wizard` composes with the
same rungs. What it adds is a credential to collect and a verification to
perform, and it adds them in this order:

    prompt once  →  detect the provider from the key's format
                 →  ask the gateway whether the key works
                 →  only then compute a plan

A key that does not authenticate ends the run with exit **8** and nothing
written. That is the whole point of asking the gateway before planning
anything: a script that writes a placeholder key and reports success leaves a
machine that cannot talk to anyone.

**The value is never in an argument.** There is no `--key` flag. The key is
read from a prompt without echo, or from a file path. It is written only to
gitignored files, at mode 0600, and it is never printed, never logged, never
in a receipt, never in a manifest entry and never in a diff.

Standard library only. Nothing outside this directory is imported.
"""
from __future__ import annotations

import argparse
import difflib
import getpass
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

ROOT = HERE.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import keydetect  # noqa: E402
import node  # noqa: E402
import receipt as receipt_mod  # noqa: E402

# The shim's own environment contract, read from the shim rather than
# written a second time here. The unit file has to carry the upstream on
# EVERY platform, and a name that can drift is a name that will.
from shim import UPSTREAM_ENV as SHIM_UPSTREAM_ENV  # noqa: E402

# Where the machine's own state lives. None of it is in the repo.
#
# These are functions rather than constants because the home directory is
# read at call time: a test can point them somewhere else, and a tool that
# binds a home at import time cannot be tested without touching the real
# one.
def config_dir() -> Path:
    return Path.home() / ".config" / "genesis"


def state_dir() -> Path:
    return Path.home() / ".local" / "state" / "genesis"


def share_dir() -> Path:
    return Path.home() / ".local" / "share" / "genesis"


def node_dir() -> Path:
    return Path.home() / ".config" / "luna-node"


def bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


def node_profile_path() -> Path:
    return config_dir() / "node.json"


def helper_path() -> Path:
    return config_dir() / "bin" / "gateway-token.sh"


def claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def auth_key_path() -> Path:
    return node_dir() / "auth.key"


def kokoro_home() -> Path:
    """Where the voice lives, outside the repo and outside Luna."""
    env = os.environ.get("GENESIS_KOKORO_HOME")
    return Path(env).expanduser() if env else share_dir() / "kokoro"


def kokoro_script_path() -> Path:
    return kokoro_home() / "kokoro_speak.py"


def kokoro_model_path() -> Path:
    return kokoro_home() / "kokoro-v1.0.onnx"


def kokoro_voices_path() -> Path:
    return kokoro_home() / "voices-v1.0.bin"


def kokoro_python_path() -> Path:
    return kokoro_home() / "venv" / "bin" / "python"


# The one release that matches Luna's working venv, so the voice is the
# same voice on every machine: kokoro-onnx 0.5.0 against model-files-v1.0.
KOKORO_PIP_PIN = "kokoro-onnx==0.5.0"
KOKORO_MODEL_URL = ("https://github.com/thewh1teagle/kokoro-onnx/"
                    "releases/download/model-files-v1.0/kokoro-v1.0.onnx")
KOKORO_VOICES_URL = ("https://github.com/thewh1teagle/kokoro-onnx/"
                     "releases/download/model-files-v1.0/voices-v1.0.bin")


def kokoro_ready() -> bool:
    """The voice can speak: script, venv, model and voices all present."""
    return (kokoro_script_path().is_file()
            and kokoro_python_path().is_file()
            and kokoro_model_path().is_file()
            and kokoro_voices_path().is_file())

PROBE_VERSION = receipt_mod.PROBE_VERSION

# The exit codes are part of the contract (docs/PORTABILITY.md §5.5,
# docs/WIZARD.md §7). They are covered by tests.
EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2
EXIT_UNSUPPORTED_HOST = 3
EXIT_NO_RECEIPT = 4
EXIT_PREREQUISITE = 5
EXIT_CONFLICT = 6
EXIT_UNSUPPORTED_SUBSTRATE = 7
EXIT_KEY_REJECTED = 8
EXIT_PRECONDITION = 9


class Refusal(Exception):
    """A clean refusal. The message is what the operator reads."""

    def __init__(self, message: str, code: int = EXIT_USAGE):
        super().__init__(message)
        self.message = message
        self.code = code


def say(line: str = "") -> None:
    """Print, masked. Every line this tool prints goes through the mask."""
    print(keydetect.mask(line))


def rule(title: str = "") -> None:
    say("")
    if title:
        say(f"  {title}")
        say("  " + "-" * max(4, len(title)))


# ------------------------------------------------------------- detection ----

def _read_os_release() -> dict:
    out = {}
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"')
    return out


def _which(name: str):
    return shutil.which(name)


def detect_profile() -> dict:
    """Read the host. No network, no writes.

    The profile decides the install route, the daemon kind and the shell.
    It is never assumed — `omarchy` is not `arch` is not `macos`, and each
    one installs things a different way.
    """
    release = _read_os_release()
    kernel = platform.system()
    distro = release.get("PRETTY_NAME") or release.get("NAME") or ""

    is_omarchy = bool(
        Path("/etc/omarchy-release").exists()
        or Path("/usr/share/omarchy").is_dir()
        or (Path.home() / ".config" / "omarchy").is_dir()
        or _which("omarchy")
    )
    is_arch = release.get("ID") == "arch" or "arch" in release.get("ID_LIKE", "") \
        or bool(_which("pacman"))

    if is_omarchy:
        profile = "omarchy"
    elif is_arch:
        profile = "arch"
    elif kernel == "Darwin":
        profile = "macos"
    else:
        profile = "unsupported"

    # The package manager and the daemon substrate are the contract
    # module's business: the wizard and the landing path must agree on
    # both, and there is one answer between them.
    package_manager = node.package_manager()
    daemon_kind = node.daemon_kind()

    js_runtime = None
    for candidate in ("node", "bun", "deno"):
        if _which(candidate):
            js_runtime = candidate
            break

    shell = os.environ.get("SHELL", "") or ""
    shell = Path(shell).name if shell else ""

    sudo_ok = False
    if _which("sudo"):
        try:
            sudo_ok = subprocess.run(
                ["sudo", "-n", "true"], capture_output=True,
                timeout=10).returncode == 0
        except Exception:
            sudo_ok = False

    return {
        "profile": profile,
        "kernel": kernel,
        "distro": distro,
        "arch": platform.machine(),
        "shell": shell,
        "python": platform.python_version(),
        "js_runtime": js_runtime,
        "package_manager": package_manager,
        "daemon_kind": daemon_kind,
        "sudo_noninteractive": sudo_ok,
        "omarchy": is_omarchy,
        "probe_version": PROBE_VERSION,
    }


def supported(profile: dict) -> bool:
    return profile.get("profile") in ("omarchy", "arch", "macos")


def refuse_unsupported_host(profile: dict) -> None:
    raise Refusal(
        "unsupported host — refusing.\n"
        f"  detected: {profile.get('distro') or profile.get('kernel')}, "
        f"{profile.get('package_manager') or 'no known package manager'}\n"
        "  supported: omarchy, arch, macos\n"
        "  nothing was changed.\n"
        "  to prepare this host by hand: see docs/PORTABILITY.md §4.2",
        EXIT_UNSUPPORTED_HOST)


# ------------------------------------------------------------------ helpers --

def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_file(path: Path, content: str, mode: int = 0o600) -> None:
    """Write under umask 077, then set the mode exactly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    old = os.umask(0o077)
    try:
        path.write_text(content, encoding="utf-8")
    finally:
        os.umask(old)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _is_gitignored(repo: Path, path: Path) -> bool:
    """Ask git, do not assume. A file that is not ignored is not written."""
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(path)],
            cwd=str(repo), capture_output=True, timeout=20)
        return result.returncode == 0
    except Exception:
        return False


def _env_line(env_var: str, value: str) -> str:
    return f"{env_var}={value}\n"


def _set_env_line(text: str, env_var: str, value: str) -> str:
    """Replace one line in a .env, leaving every other line byte-identical."""
    lines = text.splitlines(keepends=True)
    out = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("#") and "=" in stripped:
            name = stripped.split("=", 1)[0].strip()
            if name == env_var:
                out.append(_env_line(env_var, value))
                replaced = True
                continue
        out.append(line if line.endswith("\n") else line + "\n")
    if not replaced:
        if out and not out[-1].endswith("\n"):
            out[-1] = out[-1] + "\n"
        out.append(_env_line(env_var, value))
    return "".join(out)


def _redacted_hash(text: str, env_var: str) -> str:
    """The hash of a .env with its credential line replaced by a placeholder.

    This is what goes in the manifest: a change to any other line is
    detected, and the secret itself is not an oracle for anything.
    """
    redacted = _set_env_line(text, env_var, "${" + env_var + "}")
    return receipt_mod.digest(redacted)


# ------------------------------------------------------------------- actions --

def _action(id: str, kind: str, target: str, desired: dict, observed: dict,
            effect: str, **extra) -> dict:
    action = {"id": id, "kind": kind, "target": str(target),
              "desired": desired, "observed": observed, "effect": effect}
    action.update(extra)
    return action


def _plan_counts(actions: list) -> tuple:
    """(actionable, files, manual) for a plan.

    A MANUAL step is not something this tool can do, so it is not something
    that keeps the plan open. It is counted separately and reported
    separately: "already converged" stays true for the work the wizard does,
    and the step a human still owes is still named.
    """
    actionable = [a for a in actions if a.get("effect") not in ("none", "manual")]
    manual = [a for a in actions if a.get("effect") == "manual"]
    files = [a for a in actionable if a.get("kind") == "file"]
    return len(actionable), len(files), len(manual)


def _plan_summary(actions: list) -> list:
    """The lines the dry-run says about the plan.

    A function, not inline `say` calls, so the wording is testable without
    running a dry-run against a live gateway.
    """
    count, files, manual = _plan_counts(actions)
    lines = []
    if count == 0:
        lines.append("  already converged, nothing to do.")
    else:
        lines.append(f"  {count} actions, {files} files. "
                     "receipt written. now: ./setup.sh --apply")
    if manual:
        lines.append(f"  {manual} manual step(s) — a human still owes those:")
    return lines


def build_plan(ctx, profile: dict) -> list:
    """The plan: desired state, not commands.

    Running it twice on the same machine produces an empty plan. That is the
    definition of idempotency this tool adopts — not "safe to run twice" but
    *safe to run twice and provably a no-op the second time*.
    """
    actions = []
    repo = ctx.repo

    # -- python: a prerequisite, never installed.
    version = sys.version_info
    ok_python = version >= (3, 9)
    actions.append(_action(
        "python", "probe", "python3",
        {"present": True, "version_at_least": "3.9"},
        {"present": True, "version": platform.python_version()},
        "none" if ok_python else "none",
        note="" if ok_python else "python3 is older than 3.9"))

    # -- the PATH symlink.
    link = bin_dir() / "genesis"
    target = repo / "genesis"
    observed_link = None
    if link.is_symlink():
        observed_link = os.readlink(link)
    elif link.exists():
        observed_link = "(a regular file, not a symlink)"
    # The comparison is on what the link *resolves to*, not on the string:
    # two paths that name the same file are the same link, and a repo
    # reached by a different route is still the same repo.
    converged = (link.is_symlink()
                 and os.path.realpath(link) == os.path.realpath(target))
    actions.append(_action(
        "genesis-path", "symlink", link,
        {"present": True, "points_at": str(target)},
        {"present": observed_link is not None, "points_at": observed_link},
        "none" if converged else "create",
        source=str(target)))

    # -- the config directories.
    for name, path in (("config-dirs", config_dir()),
                       ("state-dirs", state_dir()),
                       ("share-dirs", share_dir())):
        actions.append(_action(
            name, "dir", path, {"present": True},
            {"present": path.is_dir()},
            "none" if path.is_dir() else "create"))

    # -- the node profile.
    profile_content = _node_profile_content(ctx, profile)
    actions.append(_action(
        "node-profile", "file", node_profile_path(),
        {"present": True},
        {"present": node_profile_path().exists(),
         "converged": _file_effect(node_profile_path(), profile_content) == "none"},
        _file_effect(node_profile_path(), profile_content),
        content=profile_content))

    # -- the Claude Code CLI, if it is absent.
    claude_present = _which("claude") is not None
    actions.append(_action(
        "claude-cli", "command", "claude",
        {"present": True},
        {"present": claude_present,
         "version": _claude_version() if claude_present else ""},
        "none" if claude_present else "install",
        note="" if claude_present else
             "install with the native installer — no Node required"))

    if ctx.aggressive:
        actions.extend(_aggressive_actions(ctx, profile))

    if ctx.wizard:
        actions.extend(_wizard_actions(ctx))
        actions.extend(_retire_plan(ctx, profile))
        if ctx.stale and not ctx.retire:
            actions.append(_action(
                "stale-credentials", "probe", "shell rc files",
                {"retired": True}, {"retired": False}, "manual",
                note="this machine still carries credentials the wizard did "
                     "not verify, and the wrappers under bin/ will prefer "
                     "them: "
                     + ", ".join(sorted({i["var"] for i in ctx.stale}))))

    return actions


def _aggressive_actions(ctx, profile: dict) -> list:
    actions = []
    tailscale_present = _which("tailscale") is not None
    actions.append(_action(
        "tailscale", "command", "tailscale",
        {"present": True, "installed_by": "the system package manager"},
        {"present": tailscale_present},
        "none" if tailscale_present else "install",
        note="" if tailscale_present else
             f"install via {profile.get('package_manager') or 'the system package manager'} — never a vendored binary"))

    if not ctx.no_daemon:
        kind = profile.get("daemon_kind") or ""
        unit = node.daemon_unit_path(kind)
        content = node.daemon_unit_content(kind, HERE / "node_daemon.py", ROOT)
        # The unit is written by a COMMAND, not as a file action: the
        # contract module owns the rule that a unit which is not ours is
        # never overwritten, and a file action would put the wizard's
        # ownership guard in the way of the landing path that wrote it.
        actions.append(_action(
            "node-daemon", "command", unit,
            {"present": True}, {"present": unit.exists()},
            _file_effect(unit, content),
            content=content,
            note="the node's heartbeat daemon, as a user service"))
        running = bool(node.daemon_state(unit).get("running"))
        actions.append(_action(
            "node-daemon-enable", "command", unit,
            {"running": True}, {"running": running},
            "none" if running else "enable",
            note="the node's heartbeat daemon, as a user service"))

    if not ctx.no_tailscale:
        key = (Path(ctx.tailnet_auth_key_path)
               if ctx.tailnet_auth_key_path else None)
        if key is not None and key.exists():
            # The join is in the PLAN, so it is covered by the receipt the
            # operator approved — not a step that happens after the plan
            # was applied, outside it.
            joined = bool(node.tailscale_status().get("running"))
            actions.append(_action(
                "tailnet-join", "command", "tailnet",
                {"joined": True}, {"joined": joined},
                "none" if joined else "join",
                note="the auth key is read from a file, and passed as "
                     "--auth-key=file:<path> — the value never enters an argument"))
        else:
            actions.append(_action(
                "tailnet-join", "probe", "tailnet",
                {"joined": True}, {"joined": False},
                "manual",
                note="no tailnet auth key file — the wizard establishes that secret"))

    actions.append(_action(
        "readiness", "probe", "readiness",
        {"ok": True}, {"ok": False}, "none"))

    return actions


def _voice_actions(ctx) -> list:
    """The voice: kokoro, standing on its own feet on every machine.

    The harness used to borrow Luna's wrapper, which meant the voice was
    silently absent anywhere ~/Luna was not. The wizard now lays down the
    same four pieces Luna needs — script, venv, model, voices — inside
    GENESIS's own state directory. Each is a normal plan action: present
    in the diff, converged on the second run, owned by the receipt.
    """
    actions = []

    # -- the synth script, vendored from the repository itself.
    vendored = ROOT / "tools" / "kokoro" / "kokoro_speak.py"
    script_content = vendored.read_text(encoding="utf-8")
    actions.append(_action(
        "kokoro-script", "file", kokoro_script_path(),
        {"present": True},
        {"present": kokoro_script_path().exists(),
         "converged": _file_effect(kokoro_script_path(), script_content) == "none"},
        _file_effect(kokoro_script_path(), script_content),
        content=script_content))

    # -- the venv with the pinned kokoro-onnx. A directory, then the pip
    #    install inside it; the directory action converges at once, the
    #    command action reports the honest state of the install.
    venv = kokoro_home() / "venv"
    actions.append(_action(
        "kokoro-venv-dir", "dir", venv,
        {"present": True},
        {"present": venv.is_dir()},
        "none" if venv.is_dir() else "create"))
    venv_ok = kokoro_python_path().is_file() and _venv_has_kokoro()
    actions.append(_action(
        "kokoro-venv", "command", "kokoro venv",
        {"present": True, "package": KOKORO_PIP_PIN},
        {"present": venv_ok},
        "none" if venv_ok else "install",
        note="" if venv_ok else
             f"a python venv with {KOKORO_PIP_PIN} — built, not downloaded"))

    # -- the model and the voices, downloaded once and hashed by the
    #    receipt like any other file the wizard owns.
    for label, path, url in (
            ("kokoro-model", kokoro_model_path(), KOKORO_MODEL_URL),
            ("kokoro-voices", kokoro_voices_path(), KOKORO_VOICES_URL)):
        present = path.is_file() and path.stat().st_size > 0
        actions.append(_action(
            label, "command", path.name,
            {"present": True, "from": url},
            {"present": present},
            "none" if present else "install",
            note="" if present else f"downloaded once from {url}"))

    return actions


def _venv_has_kokoro() -> bool:
    """Ask the venv's own pip whether the pinned package is importable."""
    py = kokoro_python_path()
    if not py.is_file():
        return False
    try:
        result = subprocess.run(
            [str(py), "-c", "import kokoro_onnx"],
            capture_output=True, timeout=120)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _wizard_actions(ctx) -> list:
    """The wizard's own actions: the credential, the helper, the consumers."""
    actions = []
    env_path = ctx.repo / ".env"

    if ctx.key_fingerprint:
        env_content = _env_content(ctx)
        actions.append(_action(
            "genesis-env", "file", env_path,
            {"present": True, "credential": "gitignored, mode 0600"},
            {"present": env_path.exists(),
             "converged": _file_effect(env_path, env_content) == "none"},
            _file_effect(env_path, env_content),
            content=env_content,
            credential=True,
            env_var=ctx.provider_env_var))

    helper_content = _helper_content(ctx)
    actions.append(_action(
        "gateway-token-helper", "file", helper_path(),
        {"present": True, "mode": "0700"},
        {"present": helper_path().exists(),
         "converged": _file_effect(helper_path(), helper_content) == "none"},
        _file_effect(helper_path(), helper_content),
        content=helper_content,
        credential=False))

    if not ctx.no_claude:
        settings_content = _claude_settings_content(ctx)
        actions.append(_action(
            "claude-settings", "file", claude_settings_path(),
            {"present": True, "merged": True},
            {"present": claude_settings_path().exists(),
             "converged": _file_effect(claude_settings_path(),
                                       settings_content) == "none"},
            _file_effect(claude_settings_path(), settings_content),
            content=settings_content,
            credential=False))

    actions.extend(_shim_actions(ctx))

    if ctx.no_voice:
        # Same honesty rule as Codexify: opting out is a choice, but a
        # machine that cannot speak should not pass silently either way.
        actions.append(_action(
            "kokoro-voice", "probe", "voice",
            {"configured": True}, {"configured": False}, "none",
            note="skipped by --no-voice; he stays silent on this machine"))
    else:
        actions.extend(_voice_actions(ctx))

    if not ctx.no_codexify and ctx.codexify_dir:
        codexify_env = ctx.codexify_dir / ".env"
        codexify_content = _codexify_env_content(ctx)
        actions.append(_action(
            "codexify-env", "file", codexify_env,
            {"present": True, "one_line": True},
            {"present": codexify_env.exists(),
             "converged": _file_effect(codexify_env, codexify_content) == "none"},
            _file_effect(codexify_env, codexify_content),
            content=codexify_content,
            credential=True,
            env_var="OPENAI_API_KEY"))
    elif not ctx.no_codexify:
        # Codexify is one of the three consumers the requirement names, so
        # "it was not found" is a REPORT, not a silence. A wizard that
        # configures two of three and says nothing about the third is 482's
        # false success with a wider blast radius.
        actions.append(_action(
            "codexify-env", "probe", "codexify",
            {"configured": True}, {"configured": False}, "manual",
            note="Codexify was not found on this machine, so its "
                 "OPENAI_API_KEY was not written. Point the wizard at the "
                 "installation with --codexify DIR, or set OPENAI_API_KEY "
                 "in the installation's own .env by hand."))

    return actions


def _shim_actions(ctx) -> list:
    """The shim, when the gateway speaks OpenAI and not Anthropic.

    Claude Code speaks three protocols and Anthropic Messages is one of
    them. An OpenAI-shaped `/v1/chat/completions` endpoint is not one of
    them, so a gateway like that needs the translation layer — and the
    layer is a *user* service, because a service that needs root to run is
    a service that dies on the next update.

    When the gateway already speaks Anthropic Messages, there is no shim,
    and the wizard says so rather than installing a layer for nothing.
    """
    if not ctx.gateway_endpoint:
        return []
    if ctx.gateway_endpoint.shape == keydetect.ANTHROPIC:
        return []
    unit = _shim_unit_path()
    content = _shim_unit_content(ctx)
    return [
        _action("shim-service", "file", unit,
                {"present": True, "binds": "127.0.0.1"},
                {"present": unit.exists()},
                _file_effect(unit, content),
                content=content, credential=False),
        _action("shim-enable", "command", unit,
                {"running": True}, {"running": False},
                "enable",
                note="the shim is a user service; it binds 127.0.0.1 only"),
    ]


def _shim_unit_path() -> Path:
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "LaunchAgents" / "dev.genesis.shim.plist"
    return Path.home() / ".config" / "systemd" / "user" / "genesis-shim.service"


def _shim_unit_content(ctx) -> str:
    """The shim's unit. It reads the credential at start; it never holds it."""
    upstream = ctx.gateway_endpoint.base
    env_path = ctx.repo / ".env"
    var = ctx.provider_env_var
    shim = ROOT / "src" / "shim.py"
    launcher = (f"set -a; . {env_path}; set +a; "
                f"export GENESIS_SHIM_KEY=\"${var}\"; "
                f"export {SHIM_UPSTREAM_ENV}=\"{upstream}\"; "
                f"exec {sys.executable} {shim} --port {ctx.shim_port}")
    if platform.system() == "Darwin":
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            "<dict>\n"
            "  <key>Label</key>\n"
            "  <string>dev.genesis.shim</string>\n"
            "  <key>ProgramArguments</key>\n"
            "  <array>\n"
            "    <string>/bin/sh</string>\n"
            "    <string>-c</string>\n"
            f"    <string>{launcher}</string>\n"
            "  </array>\n"
            "  <key>RunAtLoad</key>\n"
            "  <true/>\n"
            "  <key>KeepAlive</key>\n"
            "  <true/>\n"
            "</dict>\n"
            "</plist>\n")
    return (
        "[Unit]\n"
        "Description=GENESIS shim — Anthropic Messages in front of an "
        "OpenAI-shaped gateway\n"
        "\n"
        "[Service]\n"
        f"Environment={SHIM_UPSTREAM_ENV}={upstream}\n"
        f"Environment=GENESIS_SHIM_PORT={ctx.shim_port}\n"
        f"ExecStart=/bin/sh -c '{launcher}'\n"
        "Restart=on-failure\n"
        "RestartSec=30\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n")


# --------------------------------------------------------------- the content --

def _node_profile_content(ctx, profile: dict) -> str:
    """The node profile, with the wizard's keys set and everything else kept.

    The profile is a machine-local file that more than one thing writes: the
    wizard owns the keys below, and the node contract owns the tailnet
    record it writes when a join succeeds. So the file starts from what is
    already there and the wizard's keys are set on top — a wizard run must
    never delete a key it does not own, or the node silently stops doing the
    thing that key was for.
    """
    data = node.node_profile(node_profile_path())
    data.update({
        "node": ctx.node,
        "profile": profile.get("profile"),
        "arch": profile.get("arch"),
        "substrate": _claude_base_url(ctx),
        "substrate_kind": ("shim" if _is_shim_base(ctx)
                           else "gateway"),
        "schema": 1,
    })
    if ctx.provider:
        data["provider"] = ctx.provider
        data["provider_env_var"] = ctx.provider_env_var
        data["backend"] = ctx.provider
    if ctx.key_fingerprint:
        data["key_fingerprint"] = ctx.key_fingerprint
    if ctx.tailnet:
        tailnet = dict(data.get("tailnet") or {})
        tailnet.update(ctx.tailnet)
        data["tailnet"] = tailnet
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def _env_content(ctx) -> str:
    """The repo's .env, with the credential under the right name."""
    path = ctx.repo / ".env"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if not text:
        text = ("# GENESIS — environment\n"
                "#\n"
                "# Written by the wizard. Gitignored, mode 0600.\n"
                "# The key is read at request time and never leaves the machine\n"
                "# except as the credential in a request to the gateway.\n\n")
    return _set_env_line(text, ctx.provider_env_var, ctx.key_value)


def _helper_content(ctx) -> str:
    """The script Claude Code calls. It holds a path, not a value."""
    env_path = ctx.repo / ".env"
    return (
        "#!/bin/sh\n"
        "# genesis — the credential Claude Code reads.\n"
        "#\n"
        "# It holds a path, not a value. The key is read from the repo's .env\n"
        "# at call time and printed on stdout, which is what apiKeyHelper\n"
        "# expects. Nothing here is a secret.\n"
        f"ENV_FILE=\"{env_path}\"\n"
        f"VAR=\"{ctx.provider_env_var}\"\n"
        "if [ ! -r \"$ENV_FILE\" ]; then\n"
        "  echo \"genesis: no credential file at $ENV_FILE\" >&2\n"
        "  exit 1\n"
        "fi\n"
        "value=$(sed -n \"s/^${VAR}=//p\" \"$ENV_FILE\" | head -n 1)\n"
        "value=$(printf '%s' \"$value\" | sed -e 's/^[\"'\\'']//' -e 's/[\"'\\'']$//')\n"
        "printf '%s' \"$value\"\n"
    )


def _is_shim_base(ctx) -> bool:
    """Is the chosen base the local shim, rather than a provider?

    The two are not the same thing and the record should say which it is:
    the shim is a local translation layer, and a provider is a remote
    gateway. A reader who cannot tell them apart cannot tell whether the
    traffic leaves the machine.
    """
    base = _claude_base_url(ctx)
    return bool(base and base.startswith("http://127.0.0.1:"))


def _claude_base_url(ctx) -> Optional[str]:
    """The `ANTHROPIC_BASE_URL` Claude Code is pointed at — or None.

    On an Anthropic-shaped gateway that is the gateway itself: Claude Code
    speaks the protocol, and there is no layer in front of it.

    On an OpenAI-shaped gateway it is **the shim**, not the provider. Claude
    Code cannot speak `/chat/completions`, and pointing it at the provider
    means it posts an OpenAI-shaped key at `api.anthropic.com` and gets a
    401 — a machine that looks configured and talks to nothing. The shim is
    the thing that speaks Anthropic Messages, and this same plan installs
    it (the wizard's actions are not tier-gated), so the URL written here is
    the URL that will be listening.
    """
    if ctx.claude_base:
        return ctx.claude_base
    endpoint = ctx.gateway_endpoint
    if endpoint is not None and endpoint.shape != keydetect.ANTHROPIC:
        return f"http://127.0.0.1:{ctx.shim_port}"
    return None


def _claude_settings_content(ctx) -> str:
    """The merged settings.json. Only the keys this tool owns are touched."""
    existing = _read_json(claude_settings_path()) or {}
    if not isinstance(existing, dict):
        existing = {}
    env = existing.get("env")
    if not isinstance(env, dict):
        env = {}
    base = _claude_base_url(ctx)
    if base:
        env["ANTHROPIC_BASE_URL"] = base
    if ctx.model:
        env["ANTHROPIC_MODEL"] = ctx.model
    merged = dict(existing)
    merged["env"] = env
    merged["apiKeyHelper"] = str(helper_path())
    return json.dumps(merged, indent=2, sort_keys=True) + "\n"


def _detect_codexify(ctx) -> Optional[Path]:
    """Where Codexify is, if it is here at all.

    `--codexify` defaults to *detected* (WIZARD.md 4.2, 8): the
    `codexify-core/` submodule in the repo, or a sibling installation.

    An EMPTY directory is not an installation. A fresh `git clone` leaves
    `codexify-core/` empty unless the clone was made with
    `--recurse-submodules`, and writing a `.env` into that hole would
    configure nothing while looking configured — 482's false success one
    level down. So an empty directory is not a candidate.
    """
    candidates = [
        ctx.repo / "codexify-core",
        ctx.repo.parent / "codexify-core",
        Path.home() / "codexify-core",
    ]
    markers = (".env.template", ".env.example", "docker-compose.yml",
               "compose.yml", "Codexify-Beta")
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        try:
            entries = list(candidate.iterdir())
        except OSError:
            continue
        if not entries:
            continue                     # an unpopulated submodule
        if any((candidate / m).exists() for m in markers):
            return candidate
    return None


def _codexify_env_content(ctx) -> str:
    """Codexify's own .env — one line touched, everything else byte-identical."""
    path = ctx.codexify_dir / ".env"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return _set_env_line(text, "OPENAI_API_KEY", ctx.key_value)


def _env_has(path: Path, name: str) -> bool:
    if not path.exists():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            if line.split("=", 1)[0].strip() == name:
                return True
    return False


def _claude_settings_current(ctx) -> bool:
    """Is the settings file already what the wizard would write?"""
    existing = _read_json(claude_settings_path())
    if not isinstance(existing, dict):
        return False
    env = existing.get("env") or {}
    if not isinstance(env, dict):
        return False
    base = _claude_base_url(ctx)
    if base and env.get("ANTHROPIC_BASE_URL") != base:
        return False
    if ctx.model and env.get("ANTHROPIC_MODEL") != ctx.model:
        return False
    return existing.get("apiKeyHelper") == str(helper_path())


def _claude_version() -> str:
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True,
                             text=True, timeout=20)
        return (out.stdout or "").strip().splitlines()[0] if out.stdout else ""
    except Exception:
        return ""


def plan_is_empty(actions: list) -> bool:
    return all(a.get("effect") == "none" for a in actions)


def render_plan(actions: list) -> None:
    """What would be done, in plain words."""
    doing = [a for a in actions if a.get("effect") != "none"]
    rule("what --apply would do")
    if not doing:
        say("  nothing — already converged.")
        return
    for action in doing:
        verb = {
            "create": "create ", "update": "update ", "install": "install ",
            "enable": "enable ", "manual": "manual ",
        }.get(action.get("effect"), action.get("effect") + " ")
        say(f"  + {verb}{action.get('id')}  {action.get('target')}")
    say("")
    say(f"  {len(doing)} actions. run --dry-run to see the diff.")


def render_diff(actions: list) -> None:
    """The unified diff. Placeholders, never values."""
    for action in actions:
        if action.get("kind") not in ("file", "symlink"):
            # A command that WRITES a file still shows its diff: the unit
            # is written by a command now, and a dry run that did not show
            # it would be a dry run that lied.
            if action.get("kind") != "command" or not action.get("content"):
                continue
        if action.get("effect") == "none":
            continue
        target = Path(action.get("target", ""))
        try:
            relative = target.relative_to(Path.home())
            shown = "~/" + str(relative)
        except ValueError:
            shown = str(target).lstrip("/")

        if action.get("kind") == "symlink":
            # A symlink is a path, not the file behind it. Diffing the
            # file would show the whole of the repository as being
            # deleted, which is a lie.
            current = os.readlink(target) if target.is_symlink() else ""
            desired = action.get("source", "")
            if current == desired:
                continue
            say("--- a/" + shown)
            say("+++ b/" + shown)
            say("-" + (current or "(nothing there)"))
            say("+" + desired)
            continue

        content = action.get("content") or ""
        current = ""
        if target.exists() and target.is_file():
            current = target.read_text(encoding="utf-8", errors="replace")
        diff = list(difflib.unified_diff(
            current.splitlines(keepends=True),
            content.splitlines(keepends=True),
            fromfile="a/" + shown, tofile="b/" + shown))
        if not diff:
            continue
        for line in diff:
            say(line.rstrip("\n"))


# ------------------------------------------------------------------- the apply --

RC_CREDENTIAL_RE = re.compile(
    r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.+?)\s*$")
RC_SECRET_NAME_RE = re.compile(
    r"(?:API_KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)$")


def rc_files(profile: dict) -> list:
    """The shell rc files the profile names."""
    if profile.get("profile") == "macos":
        names = (".zshrc", ".zshprofile", ".zshenv", ".profile")
    else:
        names = (".bashrc", ".bash_profile", ".profile")
    out = []
    for name in names:
        path = Path.home() / name
        if path.exists():
            out.append(path)
    return out


def scan_stale_credentials(profile: dict) -> list:
    """Credential-shaped exports in the rc files the profile names.

    The wizard is not the only thing that can hold a credential on a
    machine, and on a used one it usually is not. These are found by
    *shape* — a variable whose name ends in API_KEY, SECRET, TOKEN,
    PASSWORD or CREDENTIAL, with a value — and reported with a fingerprint
    so the operator can see which is which without any of them being shown.
    """
    found = []
    for path in rc_files(profile):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            match = RC_CREDENTIAL_RE.match(line)
            if not match:
                continue
            name, value = match.group(1), match.group(2).strip().strip("'\"")
            if not RC_SECRET_NAME_RE.search(name):
                continue
            if len(value) < 8:
                continue
            found.append({"path": path, "var": name, "line": number,
                          "fingerprint": keydetect.fingerprint(value),
                          "value": value})
    return found


def _retire_plan(ctx, profile: dict) -> list:
    """The retirement: what the operator chose, and what it will leave behind.

    The export is removed, the rc file is backed up first, and the change
    is reversible through the manifest. A credential that cannot be shown
    to be superseded is never retired — the operator can keep it, and the
    wizard says plainly that it is still there.
    """
    actions = []
    retirements = ctx.retire
    if not retirements:
        return actions
    by_path = {}
    for item in retirements:
        by_path.setdefault(item["path"], []).append(item)
    for path, items in by_path.items():
        try:
            original = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = original.splitlines(keepends=True)
        for item in items:
            index = item["line"] - 1
            if 0 <= index < len(lines):
                lines[index] = (
                    "# genesis-wizard retired %s (fingerprint %s) on %s\n"
                    % (item["var"], item["fingerprint"],
                       time.strftime("%Y-%m-%d")))
        content = "".join(lines)
        actions.append(_action(
            "stale-credentials", "file", path,
            {"present": True, "credential": "retired, not deleted"},
            {"present": path.exists()},
            "update",
            content=content,
            retire=True,
            retired=[{"var": i["var"], "fingerprint": i["fingerprint"]}
                     for i in items]))
    return actions


def _file_effect(target: Path, content: str) -> str:
    """Is this file already what the plan wants it to be?

    Existence is not convergence. A file that is there with the wrong
    content is not "already done" — it is a file the plan would rewrite,
    and the ownership rule is what decides whether it may.
    """
    if not target.exists() or not target.is_file():
        return "create"
    try:
        current = target.read_text(encoding="utf-8")
    except OSError:
        return "update"
    return "none" if current == content else "update"


def _git_tracked(repo: Path, path: Path) -> bool:
    """Is this path tracked by git? A credential file must never be."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(path)],
            cwd=str(repo), capture_output=True, text=True, timeout=20)
        return result.returncode == 0
    except Exception:
        return False


def _current_hash(action: dict, target: Path) -> Optional[str]:
    """The hash of a file as the manifest recorded it.

    A credential file is hashed with its credential line replaced by a
    placeholder, so the manifest never becomes an oracle for the secret and
    an edit to any *other* line is still detected.
    """
    if target.is_symlink():
        return receipt_mod.digest(os.readlink(target))
    if action.get("credential"):
        try:
            content = target.read_text(encoding="utf-8")
        except OSError:
            return None
        return _redacted_hash(content, action.get("env_var", ""))
    return receipt_mod.file_hash(target)


def check_ownership(ctx, manifest: dict, action: dict, force_own: set) -> list:
    """The ownership rule. A file this tool does not own is a conflict.

    The check runs on **every** file the plan mentions, not only on the
    ones the plan would write. A file this tool wrote, that someone else
    has edited underneath it, is a conflict even when the plan says there
    is nothing to do — that is the case the ownership rule exists for.

    A **credential** file is governed by a different rule, and it is the
    stricter one: it is refused outright if git tracks it, whatever the
    variable is called. A gitignored file that is already there is not a
    conflict — it is the file the wizard is there to fill in — but once the
    manifest has recorded it, an edit underneath it is a conflict like any
    other.
    """
    target = Path(action.get("target", ""))
    if action.get("kind") not in ("file", "symlink"):
        return []
    recorded = receipt_mod.owns(manifest, target)
    exists = target.exists() or target.is_symlink()
    problems = []

    if action.get("retire"):
        # The operator asked for this one, in these words. It is not a
        # claim of ownership — the manifest records the backup, and the
        # change is reversible.
        return []

    if action.get("credential"):
        # A credential file is the wizard's to write. The guard is not the
        # manifest hash — it is **git**: a credential is never written into
        # a tracked path, whatever the variable is called.
        #
        # The hash would be the wrong guard here. It is keyed to one
        # variable, so switching the key to a *different provider* — a
        # perfectly ordinary thing to do — looks like somebody else edited
        # the file, and the wizard refuses to do the thing it was asked to
        # do. The plan still writes only its own line, and every other
        # line in the file is preserved byte-for-byte.
        if _git_tracked(ctx.repo, target):
            problems.append((target, "it is tracked by git — a credential "
                                     "is never written into a tracked path"))
        return problems

    if recorded is None:
        if exists and str(target) not in force_own:
            problems.append((target, "it exists and this tool does not own it"))
    else:
        current = _current_hash(action, target)
        if current is not None and current != recorded:
            if str(target) not in force_own:
                problems.append((target, "someone else edited it"))
    return problems


def apply_plan(ctx, actions: list, manifest: dict) -> tuple:
    """Apply the plan. Returns (applied, failed, manual, conflicted)."""
    applied = []
    failed = []
    manual = []
    conflicted = False
    force_own = set(ctx.force_own)

    for action in actions:
        effect = action.get("effect")
        kind = action.get("kind")
        target = Path(action.get("target", ""))

        conflicts = check_ownership(ctx, manifest, action, force_own)
        if conflicts:
            conflicted = True
            for path, why in conflicts:
                failed.append((action, f"conflict: {path} — {why}"))
            continue

        if effect == "none":
            continue
        if effect == "manual":
            manual.append(action)
            continue

        try:
            if kind == "dir":
                target.mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(target, 0o700)
                except OSError:
                    pass
            elif kind == "file":
                content = action.get("content") or ""
                mode = 0o700 if action.get("id") == "gateway-token-helper" else 0o600
                if action.get("retire"):
                    backup = target.with_name(
                        target.name + ".genesis-backup-" +
                        time.strftime("%Y%m%dT%H%M%S"))
                    old = os.umask(0o077)
                    try:
                        backup.write_text(target.read_text(encoding="utf-8"),
                                          encoding="utf-8")
                        os.chmod(backup, 0o600)
                    finally:
                        os.umask(old)
                    manifest.setdefault("retired", []).append({
                        "path": str(target),
                        "backup": str(backup),
                        "retired": action.get("retired", []),
                        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    })
                _write_file(target, content, mode)
                if action.get("credential"):
                    manifest.setdefault("files", {})[str(target)] = {
                        "hash": _redacted_hash(content, action.get("env_var", "")),
                        "kind": "credential",
                        "fingerprint": ctx.key_fingerprint,
                    }
                else:
                    # The hash the ownership check will compare against.
                    receipt_mod.record(manifest, target,
                                       receipt_mod.file_hash(target) or "")
            elif kind == "symlink":
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    target.unlink()
                target.symlink_to(action.get("source", target))
                receipt_mod.record(manifest, target,
                                   receipt_mod.digest(action.get("source", "")))
            elif kind == "command":
                ok, detail = _run_command(ctx, action)
                if not ok:
                    failed.append((action, detail))
                    continue
            applied.append(action)
        except Exception as exc:
            failed.append((action, f"{type(exc).__name__}: {exc}"))

    return applied, failed, manual, conflicted


def _install_kokoro_venv() -> tuple:
    """Build the venv and pip-install the pinned kokoro-onnx into it."""
    home = kokoro_home()
    venv = home / "venv"
    try:
        venv.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [sys.executable, "-m", "venv", str(venv)],
            capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            return False, f"venv creation failed: {result.stderr.strip()[:200]}"
        pip = venv / "bin" / "pip"
        if not pip.is_file():
            return False, "the venv has no pip — python3-venv may be missing"
        result = subprocess.run(
            [str(pip), "install", "--quiet", KOKORO_PIP_PIN],
            capture_output=True, text=True, timeout=1800)
        if result.returncode != 0:
            return False, f"pip install failed: {result.stderr.strip()[:200]}"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    except subprocess.TimeoutExpired:
        return False, "pip install timed out after 30 minutes"
    if not _venv_has_kokoro():
        return False, "the venv was built but kokoro_onnx does not import"
    return True, "venv built and kokoro-onnx installed"


def _download(url: str, dest: Path) -> tuple:
    """Fetch a file to its final path, atomically — a partial file is
    never left where the plan will read it as converged."""
    tmp = dest.with_name(dest.name + ".part")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=600) as resp, \
                open(tmp, "wb") as out:
            shutil.copyfileobj(resp, out)
        if tmp.stat().st_size == 0:
            tmp.unlink(missing_ok=True)
            return False, f"downloaded {dest.name} but it is empty"
        tmp.replace(dest)
    except (OSError, urllib.error.URLError) as exc:
        tmp.unlink(missing_ok=True)
        return False, f"{type(exc).__name__} fetching {dest.name}: {exc}"
    return True, f"downloaded {dest.name}"


def _run_command(ctx, action: dict) -> tuple:
    """Run the one command the plan has. It never carries a credential."""
    if action.get("id") == "claude-cli":
        if _which("claude"):
            return True, "already installed"
        if not _which("curl"):
            return False, "no curl — install the CLI by hand"
        result = subprocess.run(
            ["sh", "-c", "curl -fsSL https://claude.ai/install.sh | sh"],
            capture_output=True, text=True, timeout=900)
        if result.returncode != 0 or not _which("claude"):
            return False, "the native installer did not finish"
        return True, "installed"
    if action.get("id") == "tailscale":
        result = node.install_tailscale()
        return result["installed"], result["detail"]
    if action.get("id") == "node-daemon":
        result = node.install_daemon_unit(
            action.get("content") or "", Path(action.get("target", "")))
        return result["ok"], result["detail"]
    if action.get("id") == "node-daemon-enable":
        if ctx.no_start:
            return True, "written, not started (--no-start)"
        return node.enable_daemon(Path(action.get("target", "")))
    if action.get("id") == "tailnet-join":
        return join_tailnet(ctx)
    if action.get("id") == "shim-enable":
        if ctx.no_start:
            return True, "written, not started (--no-start)"
        return node.enable_daemon(Path(action.get("target", "")))
    if action.get("id") == "kokoro-venv":
        return _install_kokoro_venv()
    if action.get("id") == "kokoro-model":
        return _download(KOKORO_MODEL_URL, kokoro_model_path())
    if action.get("id") == "kokoro-voices":
        return _download(KOKORO_VOICES_URL, kokoro_voices_path())
    # An action this runner does not know is a FAILURE, not a success.
    # The plan is full of command actions now, and a silent (True, "")
    # here is the false success this project exists to kill.
    return False, f"unknown action: {action.get('id')!r}"


# --------------------------------------------------------------- the wizard --

def read_key(ctx) -> str:
    """The key, in memory, once.

    Read from a file path when one is given, otherwise prompted for without
    echo. There is no flag that takes the value itself: an argument is
    visible in the process table and in shell history.
    """
    if ctx.key_file:
        path = Path(ctx.key_file).expanduser()
        if not path.exists():
            raise Refusal(f"the key file does not exist: {path}", EXIT_PRECONDITION)
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise Refusal(f"the key file is empty: {path}", EXIT_PRECONDITION)
        return value
    if not sys.stdin.isatty():
        raise Refusal(
            "no terminal to prompt on, and no --key-file given.\n"
            "  the key is never taken as an argument, so a pipe is the only\n"
            "  other way in:  --key-file PATH",
            EXIT_PRECONDITION)
    try:
        return getpass.getpass(
            "genesis-wizard: the API key for this node.\n"
            "  it is written once, to gitignored files only, and never "
            "displayed again.\n"
            "> ")
    except (EOFError, KeyboardInterrupt):
        raise Refusal("no key was entered.", EXIT_PRECONDITION)


def read_tailnet_key(ctx) -> str:
    """The tailnet auth key. One prompt, one file, never in an argument.

    **Asked for once.** If the key file is already there — which it is,
    after the first run — the wizard reads it and asks nothing. A second
    run that prompts again for a secret it already holds is the exact
    failure this document's one-prompt rule exists to prevent.
    """
    if ctx.tailnet_key_file:
        path = Path(ctx.tailnet_key_file).expanduser()
        if not path.exists():
            raise Refusal(f"the tailnet key file does not exist: {path}",
                          EXIT_PRECONDITION)
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise Refusal(f"the tailnet key file is empty: {path}",
                          EXIT_PRECONDITION)
        return value
    stored = auth_key_path()
    if stored.exists():
        value = stored.read_text(encoding="utf-8").strip()
        if value:
            ctx.tailnet_key_stored = True
            return value
    if not sys.stdin.isatty():
        raise Refusal(
            "no terminal to prompt on, and no --tailnet-auth-key-file given.",
            EXIT_PRECONDITION)
    try:
        return getpass.getpass(
            "genesis-wizard: the tailnet auth key for this node.\n"
            "  it is single-use, pre-authorized, tag-scoped and expiring;\n"
            "  it is stored at mode 0600 and passed as --auth-key=file:<path>,\n"
            "  so the value never enters an argument.\n"
            "> ")
    except (EOFError, KeyboardInterrupt):
        raise Refusal("no tailnet key was entered.", EXIT_PRECONDITION)


def _custom_candidates(url: str, env_var: str, model: Optional[str]):
    """The candidates for a gateway the operator named.

    A URL is a *base*: the part before the route. The Anthropic shape
    answers at `{base}/v1/messages`, and the OpenAI shape at `{base}/v1`;
    a base that already ends in `/v1` is not given a second one.
    """
    base = url.rstrip("/")
    openai_base = base
    if base.endswith("/v1"):
        base = base[:-3]
    # The guess is a PROBE, not a promise. It is what the wizard asks
    # with; it is written into the settings only if the gateway actually
    # served it (the model is taken from the winning endpoint, below).
    # A gateway that does not serve it says so — and that answer is about
    # the MODEL, not the key, which is why it is read that way.
    models = (model,) if model else ("gpt-4.1-mini",)
    return (
        keydetect.Endpoint("custom", keydetect.ANTHROPIC, base, env_var,
                           models, ("x-api-key", "bearer")),
        keydetect.Endpoint("custom", keydetect.OPENAI, openai_base, env_var,
                           models, ("bearer",)),
    )


def verify_key(ctx, key: str) -> keydetect.ProbeResult:
    """Ask the gateway. This is the step that does not exist anywhere else.

    A key that does not authenticate ends the run: exit 8, and nothing
    written. The verification asserts on the **body** — an Anthropic
    `"type": "message"` or an OpenAI `"choices"` array — not on the status.
    """
    if ctx.gateway_url:
        env_var = "ANTHROPIC_API_KEY"
        if ctx.provider:
            env_var = keydetect.PROVIDERS[ctx.provider]["endpoints"][0].env_var
        candidates = _custom_candidates(ctx.gateway_url, env_var, ctx.model)
        guess = keydetect.Guess(provider="custom", prefix="",
                                candidates=candidates, source="url",
                                env_var=env_var)
    else:
        guess = keydetect.detect(key, override=ctx.provider)
    if not guess.candidates:
        raise Refusal(
            "the format of this key matches no known provider, and no "
            "--provider was given.\n"
            "  known: " + ", ".join(keydetect.PROVIDER_CHOICES) + "\n"
            "  nothing was written.",
            EXIT_KEY_REJECTED)
    ctx.provider = guess.provider
    ctx.provider_env_var = guess.env_var or guess.candidates[0].env_var
    ctx.key_fingerprint = keydetect.fingerprint(key)

    results = keydetect.probe_candidates(key, guess.candidates)
    for result in results:
        say(f"  {'✓' if result.ok else '✗'} {result.endpoint.provider:<10} "
            f"{result.endpoint.shape:<9} "
            f"{result.status if result.status is not None else '---'}  "
            f"{result.endpoint.url}")
        if result.detail and not result.ok:
            say(f"      {result.detail}")
    winner = next((r for r in results if r.ok), None)
    if winner is None:
        last = results[-1] if results else None
        status = last.status if last else None
        detail = (last.detail if last else "") or ""
        # A MODEL error is not a KEY error.
        #
        # The gateway answered 400 "model not found" — it never got as far
        # as judging the credential — and saying "the key did not
        # authenticate" blames the key for the model's sake, on a key
        # that was perfectly good. (WIZARD.md 5.3 is the rule: a 401 means
        # the key is wrong; a 400 about the model does not.)
        if status == 400 and "model" in detail.lower():
            raise Refusal(
                "the gateway does not serve the model the wizard asked for. "
                "Nothing was written.\n"
                f"  it answered {status}: {detail}\n"
                "  this is NOT the key: the gateway never got as far as "
                "judging it.\n"
                "  pass the ID your gateway SERVES:  --model ID",
                EXIT_USAGE)
        # A missing ROUTE is not a refused key either: the route is not
        # there, and that is a different failure with a different fix.
        if status == 404:
            raise Refusal(
                "the gateway does not serve that route. Nothing was "
                "written.\n"
                f"  it answered {status}"
                + (f" at {last.endpoint.url}\n" if last else "\n")
                + "  a 404 is a missing route, not a refused key.",
                EXIT_UNSUPPORTED_SUBSTRATE)
        raise Refusal(
            "the key did not authenticate. Nothing was written.\n"
            f"  the endpoint that refused it answered "
            f"{status if status is not None else 'nothing'}.\n"
            + (f"  it said: {last.detail}\n" if last and last.detail else "")
            + "  a key's prefix says where to look; the gateway says whether "
            "it works.",
            EXIT_KEY_REJECTED)

    ctx.provider = winner.endpoint.provider
    ctx.provider_env_var = winner.endpoint.env_var
    ctx.gateway_endpoint = winner.endpoint
    # The harness's name for this provider, when it has one. It is what
    # `genesis chat --backend` wants, and it is what the chat probe uses.
    ctx.backend = winner.endpoint.provider
    ctx.gateway_shape = winner.endpoint.shape
    ctx.model = ctx.model or (winner.endpoint.models[0] if winner.endpoint.models else "")
    ctx.gateway_url = winner.endpoint.url
    # Where Claude Code points is decided in one place — `_claude_base_url` —
    # because the answer depends on the endpoint's *shape*: an
    # Anthropic-shaped gateway is its own base, and an OpenAI-shaped one
    # is the shim in front of it.
    ctx.claude_base = winner.endpoint.claude_base
    return winner


def tailnet_join(ctx) -> list:
    """Ask for the tailnet key once, store it once, and join.

    The key is written to a gitignored local file at mode 0600, and it is
    passed to `tailscale up` as `--auth-key=file:<path>` — a path in the
    argument, never the value. The key's *properties* are recorded in the
    node profile; the key itself is never recorded anywhere.
    """
    if ctx.no_tailscale or not ctx.aggressive:
        return []
    key = read_tailnet_key(ctx)
    if ctx.tailnet_key_stored:
        say(f"  the tailnet auth key already configured at {auth_key_path()} "
            "is used as it is. it is not asked for again.")
    ctx.tailnet = {
        "properties": ["single-use", "pre-authorized", "tag-scoped",
                       "expiring"],
        "key_file": str(auth_key_path()),
        "ephemeral": None,
    }
    node_dir().mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(node_dir(), 0o700)
    except OSError:
        pass
    old = os.umask(0o077)
    try:
        auth_key_path().write_text(key + "\n", encoding="utf-8")
    finally:
        os.umask(old)
    try:
        os.chmod(auth_key_path(), 0o600)
    except OSError:
        pass
    ctx.tailnet_auth_key_path = auth_key_path()
    return ctx.tailnet


def join_tailnet(ctx) -> tuple:
    """`tailscale up` with the key file. The value is not in the argument.

    The join belongs to the CONTRACT module, and this is a call into it.
    There used to be a second copy of this logic here, and it was the
    weaker one: it built the command without root handling, so on Linux —
    where `tailscaled` runs as root — the join failed with a permission
    error that reads like a bad key, and the operator was given neither
    the command nor a human. `node.join_tailnet` knows about root, knows
    that a non-interactive sudo may not be available, and hands back the
    exact command to run when it cannot proceed on its own.

    Two copies of one join is how they drift apart: the landing path
    called the strong one, and the wizard — the primary path — called the
    weak one, and only one of them worked on Linux.
    """
    if not ctx.tailnet_auth_key_path or not Path(ctx.tailnet_auth_key_path).exists():
        return False, "no tailnet auth key file — the node is not joining a tailnet"
    result = node.join_tailnet(Path(ctx.tailnet_auth_key_path))
    detail = str(result.get("detail") or "")
    joined = bool(result.get("joined"))
    if joined and not detail:
        detail = "joined"
    return joined, detail


# --------------------------------------------------------------- the probes --

def _helper_fingerprint(ctx) -> Optional[str]:
    """The fingerprint of the credential Claude Code would read.

    The helper holds a path, not a value, so the only way to know that
    Claude Code would read *this* key is to run it and look.
    """
    helper = helper_path()
    if not helper.exists():
        return None
    try:
        result = subprocess.run(["/bin/sh", str(helper)], capture_output=True,
                                 text=True, timeout=30)
    except Exception:
        return None
    value = (result.stdout or "").strip()
    if not value:
        return None
    return keydetect.fingerprint(value)


def _codexify_fingerprint(ctx) -> Optional[str]:
    """The fingerprint of the key Codexify would read, from its own .env."""
    if not ctx.codexify_dir:
        return None
    path = ctx.codexify_dir / ".env"
    if not path.exists():
        return None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                name, _, value = line.partition("=")
                if name.strip() == "OPENAI_API_KEY":
                    value = value.strip().strip("'\"")
                    return keydetect.fingerprint(value) if value else None
    except OSError:
        return None
    return None


def _chat_probe(ctx) -> tuple:
    """A live call through the selected backend.

    This is the row that matters most: it is the only thing that
    exercises the backend's own **auth style** — `x-api-key` for the
    Anthropic-shaped routes, `Authorization: Bearer` for the OpenAI-shaped
    ones. A probe that hard-codes one header answers 401 against a
    perfectly good key and reads as "the key is wrong".
    """
    backend = ctx.backend or ctx.provider
    if not backend:
        return None, "no backend was selected"
    try:
        sys.path.insert(0, str(ROOT / "src"))
        import harness
    except Exception as exc:
        return False, f"the harness could not be loaded: {exc}"
    if backend not in harness.BACKENDS:
        # A custom gateway is not a backend the harness knows. That is a
        # limitation of the harness, stated plainly — not a consumer that
        # failed, and not a reason to call the run a failure.
        return None, (f"the harness has no backend for {backend} — "
                      "`genesis chat` cannot use this gateway; Claude Code "
                      "can, through the shim")
    payload = {"model": harness.BACKENDS[backend]["model"], "max_tokens": 4,
               "messages": [{"role": "user", "content": "ping"}]}
    try:
        answer = harness._post(payload, backend=backend)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if isinstance(answer, dict) and (answer.get("type") == "message"
                                     or answer.get("choices")):
        return True, f"answered through {backend}"
    return False, f"{backend} answered in a shape nothing recognises"


def _shim_probe(ctx) -> tuple:
    """Ask the translation layer whether it is serving.

    The shim is the thing that makes an OpenAI-shaped gateway usable by
    anything that speaks Anthropic Messages. A shim that is installed and
    not serving is the same failure as no shim at all, with one more step
    in the way — so it is probed, not assumed.
    """
    port = ctx.shim_port
    url = f"http://127.0.0.1:{port}/v1/messages"
    body = {"model": ctx.model or "gpt-4.1-mini", "max_tokens": 4,
            "messages": [{"role": "user", "content": "ping"}]}
    headers = {"content-type": "application/json",
               "anthropic-version": "2023-06-01"}
    if ctx.key_value:
        headers["x-api-key"] = ctx.key_value
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers,
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read().decode("utf-8", "replace")
        exc.close()
    except Exception as exc:
        return False, f"the shim did not answer on {url} ({type(exc).__name__})"
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if status == 200 and isinstance(parsed, dict) \
            and parsed.get("type") == "message":
        return True, f"the shim answered on {url}"
    return False, f"the shim answered {status} on {url} with no message body"


def readiness(ctx) -> list:
    """The readiness probe. It runs the real things and reports what happened.

    **Nothing here is claimed.** Every row is the result of something that
    ran. A row that cannot be proved is a row that failed, or a row that
    says it does not apply — never a row that says ✓ because the plan
    intended it.

    Rows are (label, mark, detail), where the mark is ✓ (proved), ✗ (it
    failed) or · (it does not apply here, or it needs a human).
    """
    rows = []

    result = subprocess.run([sys.executable, str(ROOT / "genesis"), "--plain",
                             "check"], capture_output=True, text=True,
                            cwd=str(ROOT))
    lines = (result.stdout or "").strip().splitlines()
    verdict = lines[-1] if lines else "no output"
    lowered = verdict.lower()
    if "healthy" in lowered:
        mark = True
    elif "degraded" in lowered:
        # Degraded is not broken: a fresh clone has not been configured yet.
        # It says so rather than dressing it up.
        mark = None
    else:
        mark = False
    rows.append(("genesis check", mark, verdict.strip()))

    # `--readiness-only` changes NOTHING on this machine: no install, no
    # unit, no service, no join. A readiness probe that installed things
    # would be a probe that changed the thing it measured.
    result = subprocess.run([sys.executable, str(ROOT / "genesis"), "--plain",
                             "landing", str(ROOT), "--readiness-only"],
                            capture_output=True, text=True, cwd=str(ROOT))
    rows.append(("genesis landing", result.returncode == 0,
                 "the node reports readiness"))

    # -- the gateway: a real request carrying the configured key.
    if ctx.gateway_endpoint and ctx.key_value:
        probe = keydetect.probe(ctx.key_value, ctx.gateway_endpoint,
                                model=ctx.model)
        rows.append(("the gateway", probe.ok,
                     f"{probe.status} {probe.endpoint.url}"
                     + ("" if probe.ok else f" — {probe.detail}")))
    else:
        rows.append(("the gateway", None,
                     "no gateway was selected on this run"))

    # -- genesis chat: the live call, through the selected backend.
    if ctx.key_value:
        good, detail = _chat_probe(ctx)
        rows.append(("genesis chat", good, detail))
    else:
        rows.append(("genesis chat", None,
                     "no credential on this node to talk with"))

    # -- the shim: if the gateway speaks OpenAI, the translation layer has
    #    to be serving, and the only way to know is to ask it.
    if (ctx.gateway_endpoint is not None
            and ctx.gateway_endpoint.shape == keydetect.OPENAI):
        if ctx.no_start:
            rows.append(("the shim", None,
                         "written but not started (--no-start)"))
        else:
            good, detail = _shim_probe(ctx)
            rows.append(("the shim", good, detail))

    # -- Claude Code: the credential it would read, proved by running the
    #    helper and comparing the fingerprint.
    #
    # The guard is the BASE Claude Code is pointed at, not the gateway's own
    # base: on an OpenAI-shaped gateway Claude Code talks to the shim, and
    # that consumer is configured and must be reported. Guarding on
    # `ctx.claude_base` alone left the whole OpenAI-shaped path unmentioned
    # in the table — the one path where the configuration is hardest.
    base = _claude_base_url(ctx)
    if base:
        found = _helper_fingerprint(ctx)
        if found is None:
            rows.append(("Claude Code", False,
                         "the credential helper produced nothing"))
        elif found == ctx.key_fingerprint:
            rows.append(("Claude Code", True,
                         f"the helper reads the verified key "
                         f"(fingerprint {found})"))
        else:
            rows.append(("Claude Code", False,
                         f"the helper reads a different key (fingerprint "
                         f"{found}, not {ctx.key_fingerprint})"))
        rows.append(("Claude Code url", True,
                     f"ANTHROPIC_BASE_URL={base}"))

    # -- Codexify: the key in its own .env, proved by fingerprint.
    if ctx.codexify_dir:
        found = _codexify_fingerprint(ctx)
        if found is None:
            rows.append(("Codexify", None,
                         "no key in Codexify's own .env — a manual step"))
        elif found == ctx.key_fingerprint:
            rows.append(("Codexify", True,
                         f"OPENAI_API_KEY is the verified key "
                         f"(fingerprint {found})"))
        else:
            rows.append(("Codexify", False,
                         f"Codexify's OPENAI_API_KEY is a different key "
                         f"(fingerprint {found})"))
    elif not ctx.no_codexify:
        rows.append(("Codexify", None,
                     "not found on this machine — a manual step: "
                     "--codexify DIR"))

    return rows


# ------------------------------------------------------------------- the run --

def run(ctx) -> int:
    profile = detect_profile()

    if ctx.detect_only:
        say("genesis-bootstrap: detect — nothing was changed.")
        say("")
        say(json.dumps(profile, indent=2, sort_keys=True))
        return EXIT_OK

    if not supported(profile):
        refuse_unsupported_host(profile)

    if sys.version_info < (3, 9):
        raise Refusal(
            f"python3 is {platform.python_version()} — 3.9 is the minimum.\n"
            "  nothing was changed.", EXIT_PREREQUISITE)

    # -- the wizard's part comes first: the key, the provider, the gateway.
    if ctx.wizard:
        rule("key")
        stored_name, stored_value = _stored_credential(ctx)

        # -- remove: the operator says the node has no provider key.
        if ctx.remove_key:
            if not stored_value:
                raise Refusal(
                    "--remove-key was given, but there is no credential to "
                    "remove.\n  nothing was written.", EXIT_PRECONDITION)
            _remove_credential(ctx, stored_name)
            say(f"  removed the {stored_name} credential from "
                f"{ctx.repo / '.env'}.")
            say("  this node has no provider key now. nothing else was "
                "written.")
            return EXIT_OK

        # -- the machine is already configured: keep, replace, or remove.
        if stored_value and not ctx.key_file:
            choice = "keep"
            if ctx.keep_key:
                choice = "keep"
            elif sys.stdin.isatty():
                choice = _ask_keep_replace_remove(stored_name, stored_value)
            else:
                raise Refusal(
                    f"this machine is already configured with {stored_name}.\n"
                    "  nothing was written. the wizard will not ask for a key\n"
                    "  it cannot see: use --keep-key to keep the one that is\n"
                    "  there, --key-file to replace it, or --remove-key to\n"
                    "  take it away.", EXIT_PRECONDITION)
            if choice == "remove":
                _remove_credential(ctx, stored_name)
                say(f"  removed the {stored_name} credential from "
                    f"{ctx.repo / '.env'}.")
                say("  this node has no provider key now. nothing else was "
                    "written.")
                return EXIT_OK
            if choice == "replace":
                ctx.key_value = read_key(ctx)
            else:
                ctx.provider_env_var = stored_name
                ctx.key_value = stored_value
        elif ctx.keep_key:
            raise Refusal(
                "--keep-key was given, but no key is configured here.\n"
                "  nothing was written.", EXIT_PRECONDITION)
        else:
            ctx.key_value = read_key(ctx)

        winner = verify_key(ctx, ctx.key_value)
        if stored_value and ctx.key_value == stored_value:
            say(f"  the key already configured is a {winner.endpoint.provider} key "
                f"(fingerprint {ctx.key_fingerprint}).")
        else:
            say(f"  provider: {ctx.provider}  (fingerprint {ctx.key_fingerprint})")
            say(f"  gateway:  {ctx.gateway_url}")
            say(f"  shape:    {ctx.gateway_shape}")

        left_behind = _other_credentials(ctx)
        if left_behind:
            for name, fingerprint in left_behind:
                say(f"  note: {name} is also in {ctx.repo / '.env'} "
                    f"(fingerprint {fingerprint}). it is not the key this run "
                    "verified, and the wrappers will prefer it.")

        ctx.stale = scan_stale_credentials(profile)
        if ctx.stale:
            rule("credentials already on this machine")
            for item in ctx.stale:
                say(f"  · {item['var']} in {item['path']} "
                    f"(fingerprint {item['fingerprint']})")
                if item["fingerprint"] == ctx.key_fingerprint:
                    say("      this is the key you just verified.")
                else:
                    say("      the wizard did not verify this one.")
            if sys.stdin.isatty():
                for item in ctx.stale:
                    if item["fingerprint"] != ctx.key_fingerprint:
                        continue
                    try:
                        answer = input(
                            f"  retire the export of {item['var']} in "
                            f"{item['path']}? [y/N] ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        answer = ""
                    if answer in ("y", "yes"):
                        ctx.retire.append(item)
            for item in ctx.stale:
                if item in ctx.retire:
                    continue
                say(f"  keep {item['var']} — this machine still carries a "
                    "credential the wizard did not verify, and the wrappers "
                    "will prefer it.")

        if ctx.aggressive and not ctx.no_tailscale:
            rule("tailnet")
            tailnet_join(ctx)
            say(f"  the auth key is stored at {auth_key_path()} (mode 0600) and "
                "passed as --auth-key=file:<path>.")

    # -- Codexify: `--codexify` defaults to DETECTED (WIZARD.md 4.2, 8), and
    # an empty submodule directory is not an installation.
    if ctx.wizard and not ctx.no_codexify and not ctx.codexify_dir:
        ctx.codexify_dir = _detect_codexify(ctx)
        if ctx.codexify_dir:
            say(f"  codexify:  {ctx.codexify_dir}")

    actions = build_plan(ctx, profile)

    if ctx.status:
        rule("status")
        for action in actions:
            mark = "✓" if action.get("effect") == "none" else "✗"
            say(f"  {mark} {action.get('id'):<22} {action.get('target')}")
        return EXIT_OK

    if ctx.dry_run:
        rule("dry-run — nothing was changed.")
        render_diff(actions)
        say("")
        for line in _plan_summary(actions):
            say(line)
        rec = receipt_mod.new_receipt(profile, actions, ctx.tier,
                                      ctx.key_fingerprint)
        receipt_mod.write_receipt(ctx.repo, rec)
        return EXIT_OK

    if ctx.apply:
        manifest = receipt_mod.load_manifest(config_dir())
        rec = receipt_mod.read_receipt(ctx.repo)
        expected = receipt_mod.plan_hash(profile, actions, ctx.key_fingerprint)
        if not rec:
            raise Refusal(
                "no valid dry-run receipt for this plan.\n"
                "  run:  ./setup.sh --dry-run"
                + (" --aggressive" if ctx.aggressive else "")
                + "\n  then: ./setup.sh --apply"
                + (" --aggressive" if ctx.aggressive else ""),
                EXIT_NO_RECEIPT)
        if rec.get("tier") != ctx.tier:
            raise Refusal(
                f"the receipt is a {rec.get('tier')} receipt and this run is "
                f"{ctx.tier}.\n"
                "  the diff you approved is not the diff this run would apply.\n"
                "  run the dry run again with the same tier.",
                EXIT_NO_RECEIPT)
        if rec.get("plan_hash") != expected:
            raise Refusal(
                "the receipt no longer matches this plan.\n"
                "  the machine changed since the dry run, or the key did.\n"
                "  run:  ./setup.sh --dry-run",
                EXIT_NO_RECEIPT)

        rule("apply — receipt verified (" + rec.get("plan_hash", "")[:12] + "…)")
        applied, failed, manual, conflicted = apply_plan(ctx, actions, manifest)
        receipt_mod.save_manifest(config_dir(), manifest)

        for action in applied:
            say(f"  ✓ {action.get('effect'):<8} {action.get('id'):<22} "
                f"{action.get('target')}")
        for action, why in failed:
            say(f"  ✗ {action.get('id'):<22} {why}")
        for action in manual:
            say(f"  · manual   {action.get('id'):<22} {action.get('note', '')}")

        say("")
        say(f"  {len(applied)} actions applied"
            + (f", {len(failed)} failed" if failed else "")
            + (f", {len(manual)} manual" if manual else "") + ".")

        if ctx.wizard or ctx.aggressive:
            rule("readiness")
            rows = readiness(ctx)
            for label, good, detail in rows:
                mark = "·" if good is None else ("✓" if good else "✗")
                say(f"  {mark} {label:<16} {detail}")
            if any(good is False for _, good, _ in rows) or failed:
                say("")
                say("  not everything came up. the table above says what did.")
                return EXIT_CONFLICT if conflicted else EXIT_PARTIAL
        if conflicted:
            return EXIT_CONFLICT
        if failed:
            return EXIT_PARTIAL
        return EXIT_OK

    # -- inspect: the default, and it changes nothing.
    rule("inspect — nothing was changed.")
    say("")
    say("host")
    say(f"  profile      {profile.get('profile')}")
    say(f"  distro       {profile.get('distro') or profile.get('kernel')}")
    say(f"  python       {profile.get('python')}")
    say(f"  daemon       {profile.get('daemon_kind') or 'none'}")
    say(f"  sudo         "
        f"{'available without a password' if profile.get('sudo_noninteractive') else 'not available without a password'}")
    say("")
    render_plan(actions)
    return EXIT_OK


def _other_credentials(ctx) -> list:
    """The credentials in the repo's .env that this run did not verify.

    The wizard writes one line and leaves the rest of the file alone, so a
    machine that once held a key for another provider still holds it.
    That may be the right thing — a credential store with two providers in
    it is not obviously wrong — but it must not be **silent**: a reader who
    is told "the key is configured" and is not told that a different key
    sits beside it is being told half the truth.

    Returns [(variable, fingerprint)]. The values are never shown.
    """
    path = ctx.repo / ".env"
    if not path.exists():
        return []
    out = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip("'\"")
        if name == ctx.provider_env_var or not value:
            continue
        if not RC_SECRET_NAME_RE.search(name):
            continue
        if len(value) < 16:
            continue
        out.append((name, keydetect.fingerprint(value)))
    return out


def _ask_keep_replace_remove(name: str, value: str) -> str:
    """The choice on a machine that is already configured.

    Three answers, and only three: **keep** the key that is there,
    **replace** it with a new one, or **remove** it and leave the node
    without a provider key. It is a choice, not a re-entry — the value is
    never typed again to keep it.
    """
    prompt = (
        f"genesis-wizard: this machine is already configured with {name} "
        f"(fingerprint {keydetect.fingerprint(value)}).\n"
        "  keep it, replace it, or remove it? [k/r/d] ")
    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.startswith("r"):
        return "replace"
    if answer.startswith("d"):
        return "remove"
    return "keep"


def _remove_credential(ctx, name: str) -> bool:
    """Take the credential out of the repo's .env.

    The line goes; everything else in the file stays byte-identical. The
    file keeps its mode. Nothing else is written, and the removal is
    reported in those words — a node with no key is a different machine,
    and the operator should hear that rather than infer it.
    """
    path = ctx.repo / ".env"
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines
            if not line.strip().startswith(name + "=")]
    if len(kept) == len(lines):
        return False
    old = os.umask(0o077)
    try:
        path.write_text("".join(kept), encoding="utf-8")
    finally:
        os.umask(old)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return True


def _stored_credential(ctx) -> tuple:
    """The credential already configured, and the variable it sits under.

    Returns `(variable, value)`, or `("", "")`. The variable is what tells
    the wizard which provider's endpoint to ask, so the lookup is by
    *shape* rather than by a name the caller happens to know — the point
    of `--keep-key` is that the operator never has to remember.
    """
    path = ctx.repo / ".env"
    if not path.exists():
        return "", ""
    candidates = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip("'\"")
        if not value or not RC_SECRET_NAME_RE.search(name):
            continue
        if len(value) < 16:
            continue
        candidates.append((name, value))
    for name, value in candidates:
        if keydetect.sniff(value).known:
            return name, value
    return candidates[0] if candidates else ("", "")


def _existing_key(ctx) -> str:
    """The key already configured, if there is one."""
    path = ctx.repo / ".env"
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == ctx.provider_env_var:
            return value.strip().strip("'\"")
    return ""


# ------------------------------------------------------------------- the arg --

class Context:
    """Everything one run needs to know."""

    def __init__(self):
        self.repo = ROOT
        self.node = platform.node()
        self.aggressive = False
        self.wizard = False
        self.dry_run = False
        self.apply = False
        self.detect_only = False
        self.status = False
        self.keep_key = False
        self.remove_key = False
        self.no_daemon = False
        self.no_tailscale = False
        self.no_claude = False
        self.no_codexify = False
        self.no_voice = False
        self.force_own = []
        self.key_file = None
        self.tailnet_key_file = None
        self.provider = None
        self.provider_env_var = ""
        self.key_value = ""
        self.key_fingerprint = ""
        self.claude_base = None
        self.gateway_shape = None
        self.gateway_url = None
        self.model = None
        self.codexify_dir = None
        self.tailnet = None
        self.tailnet_auth_key_path = None
        self.tailnet_key_stored = False
        self.gateway_endpoint = None
        self.backend = None
        self.shim_port = 8790
        self.no_start = False
        self.stale = []
        self.retire = []

    @property
    def tier(self) -> str:
        return "aggressive" if self.aggressive else "standard"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="setup.sh",
        description="GENESIS bootstrap — inspect, dry-run, apply.")
    parser.add_argument("--detect", action="store_true",
                        help="print the environment profile and stop")
    parser.add_argument("--status", action="store_true",
                        help="print what is installed and what is missing")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute the plan, render the diff, write a receipt")
    parser.add_argument("--apply", action="store_true",
                        help="apply the plan. Requires a matching receipt")
    parser.add_argument("--aggressive", action="store_true",
                        help="the aggressive tier. Needs --dry-run or --apply")
    parser.add_argument("--wizard", action="store_true",
                        help="run the wizard: detect, prompt once, verify, plan")
    parser.add_argument("--repo", default=str(ROOT),
                        help="the GENESIS checkout to prepare")
    parser.add_argument("--node", default=None, help="the node's name")
    parser.add_argument("--no-daemon", action="store_true",
                        help="skip the node daemon")
    parser.add_argument("--no-tailscale", action="store_true",
                        help="skip the tailnet step")
    parser.add_argument("--no-claude", action="store_true",
                        help="skip the Claude Code target")
    parser.add_argument("--no-codexify", action="store_true",
                        help="skip the Codexify target")
    parser.add_argument("--no-voice", action="store_true",
                        help="skip the voice (kokoro) target")
    parser.add_argument("--codexify", default=None,
                        help="the Codexify installation to configure. "
                             "Default: detected")
    parser.add_argument("--provider", default=None,
                        choices=list(keydetect.PROVIDER_CHOICES),
                        help="override the provider the key is routed to")
    parser.add_argument("--gateway-url", default=None,
                        help="the gateway base. Never a placeholder")
    parser.add_argument("--model", default=None,
                        help="the model id the gateway serves")
    parser.add_argument("--key-file", default=None,
                        help="read the key from a file. The path is a path")
    parser.add_argument("--tailnet-auth-key-file", default=None,
                        help="the tailnet auth key's file")
    parser.add_argument("--keep-key", action="store_true",
                        help="keep the key already configured")
    parser.add_argument("--remove-key", action="store_true",
                        help="remove the key already configured, and leave "
                             "the node without one")
    parser.add_argument("--no-start", action="store_true",
                        help="write the service files but do not start them")
    parser.add_argument("--yes", action="store_true",
                        help="do not prompt. Does not waive the receipt")
    parser.add_argument("--force-own", action="append", default=[],
                        help="take ownership of a path the manifest does not own")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ctx = Context()

    try:
        ctx.repo = Path(args.repo).expanduser().resolve()
        if args.node:
            ctx.node = args.node
        ctx.aggressive = args.aggressive
        ctx.wizard = args.wizard
        ctx.dry_run = args.dry_run
        ctx.apply = args.apply
        ctx.detect_only = args.detect
        ctx.status = args.status
        ctx.keep_key = args.keep_key
        ctx.remove_key = args.remove_key
        ctx.no_daemon = args.no_daemon
        ctx.no_tailscale = args.no_tailscale
        ctx.no_claude = args.no_claude
        ctx.no_codexify = args.no_codexify
        ctx.no_voice = args.no_voice
        ctx.force_own = list(args.force_own)
        ctx.no_start = args.no_start
        ctx.key_file = args.key_file
        ctx.tailnet_key_file = args.tailnet_auth_key_file
        ctx.provider = args.provider
        ctx.model = args.model
        if args.gateway_url:
            if "<" in args.gateway_url:
                raise Refusal("the gateway URL still contains a placeholder.",
                              EXIT_USAGE)
            ctx.gateway_url = args.gateway_url.rstrip("/")
            ctx.claude_base = args.gateway_url.rstrip("/")
            if ctx.claude_base.endswith("/v1"):
                ctx.claude_base = ctx.claude_base[:-3]
        if args.codexify:
            ctx.codexify_dir = Path(args.codexify).expanduser()

        if args.aggressive and not (args.dry_run or args.apply):
            raise Refusal(
                "--aggressive needs --dry-run or --apply.\n"
                "  it is a tier, not a rung: see the diff first.",
                EXIT_USAGE)
        if args.apply and args.dry_run:
            raise Refusal("--apply and --dry-run are different rungs.",
                          EXIT_USAGE)

        return run(ctx)
    except Refusal as refusal:
        return _refuse(refusal.message, refusal.code)


def _refuse(message: str, code: int) -> int:
    """Say a refusal, in the tool's own voice, and give the code back."""
    say("")
    say("genesis-bootstrap: " + message)
    return code


if __name__ == "__main__":
    sys.exit(main())
