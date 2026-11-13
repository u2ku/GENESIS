"""NODE — what it means for a machine to be a node.

A node is not a second Luna. It is a sensing and execution endpoint with a
heartbeat and a one-way export channel. This module is the connective tissue
between that idea and the machine it runs on.

Three things live here, and they are deliberately separate:

**The join.** Tailscale, installed by the *system package manager* only —
``pacman`` on Arch and Omarchy, ``apt`` on Debian, ``brew`` on macOS. Never
a vendored binary, never a ``curl | sh`` installer: a binary in the repo
would break the machine-independence the whole portability contract rests
on. The auth key is read from a file and passed as
``--auth-key=file:<path>``, so the argument carries a **path** and never the
value.

**The heartbeat.** A small, fixed payload emitted on an interval: node id,
hostname, OS, uptime, the state of the services that matter, and a
monotonic sequence number. No credentials, no environment dump, no user
file contents. It is written to the node-local record, and it is the node's
**only** claim to liveness — a silent node is a dead node, and nothing is
inferred beyond the last heartbeat.

**The export.** One way, on purpose. The node writes its own record; the
spine has exactly one writer, and it is the spine. Export is a deliberate
push of a curated, visibility-tagged block, never a sync, never a backflow,
never a background merge.

Standard library only. Nothing here ever writes a credential into a file it
owns, and nothing here prints one.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent

# `visibility` lives beside this module, so it is found the same way this
# module was found. The path goes in explicitly rather than being relied
# upon: a module's own directory is not on sys.path by itself, and this
# one is imported by tools that put `src` there for their own reasons.
SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import visibility  # noqa: E402

CONFIG_DIR = Path.home() / ".config" / "genesis"
STATE_DIR = Path.home() / ".local" / "state" / "genesis"
NODE_DIR = Path.home() / ".config" / "luna-node"

NODE_PROFILE = CONFIG_DIR / "node.json"
HEARTBEAT_LOG = STATE_DIR / "heartbeat.jsonl"
NODE_RECORD = STATE_DIR / "record.md"
AUTH_KEY_FILE = NODE_DIR / "auth.key"

# The heartbeat interval. Sixty seconds is the contract's default; the
# retention question is still open with Zac, so the log is bounded rather
# than unbounded.
DEFAULT_INTERVAL = 60
DEFAULT_RETENTION = 10000

# The marker every exported block must carry. Untagged content is readable
# as written — there is no read-time filtering — so the tag is decided at
# write time, and an untagged export is refused.
#
# The SET is `visibility`'s, not this module's. The export gate and the
# publication lint have to agree on what a marker is, and the only way
# they agree for as long as the contract lives is if there is one copy.
# Spelling it twice is how the two drift apart quietly.
VISIBILITY_MARKERS = visibility.KNOWN_VALUES


# ------------------------------------------------------------------ the profile --

def node_profile() -> dict:
    """The node-local profile. Machine-local, never published."""
    try:
        data = json.loads(NODE_PROFILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_node_profile(profile: dict) -> None:
    NODE_PROFILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(NODE_PROFILE.parent, 0o700)
    except OSError:
        pass
    NODE_PROFILE.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
    try:
        os.chmod(NODE_PROFILE, 0o600)
    except OSError:
        pass


def node_id() -> str:
    """A stable id for this node: the profile's, or the hostname's."""
    return node_profile().get("node") or socket.gethostname()


# ------------------------------------------------------------------ the tailnet --

def which(name: str) -> Optional[str]:
    return shutil.which(name)


def tailscale_installed() -> bool:
    return bool(which("tailscale"))


def tailscale_status() -> dict:
    """What Tailscale says about this machine.

    `{"installed": bool, "running": bool, "ip": str, "hostname": str,
      "backend": str}` — and nothing that could carry a credential. The
    tailnet address is this node's own address on the tailnet; it is
    recorded locally and is not exported in a heartbeat.
    """
    out = {"installed": tailscale_installed(), "running": False,
           "ip": "", "hostname": "", "backend": ""}
    if not out["installed"]:
        return out
    try:
        result = subprocess.run(["tailscale", "status", "--json"],
                                capture_output=True, text=True, timeout=30)
    except Exception:
        return out
    if result.returncode != 0:
        return out
    try:
        data = json.loads(result.stdout or "{}")
    except ValueError:
        return out
    out["backend"] = str(data.get("BackendState") or "")
    out["running"] = out["backend"] == "Running"
    self_node = data.get("Self") or {}
    addresses = self_node.get("TailscaleIPs") or []
    out["ip"] = str(addresses[0]) if addresses else ""
    out["hostname"] = str(self_node.get("HostName") or "")
    return out


def package_manager() -> str:
    """The system package manager. There is no other install route."""
    if which("pacman"):
        return "pacman"
    if which("brew"):
        return "brew"
    if which("apt-get"):
        return "apt"
    return ""


def install_command() -> list:
    """The command that installs Tailscale the supported way.

    Returned rather than run: installing a system package needs privileges
    the agent does not have, and a tool that shells out to sudo behind the
    operator's back is a tool that should not exist. The caller decides
    whether to run it.
    """
    manager = package_manager()
    if manager == "pacman":
        return ["sudo", "pacman", "-S", "--needed", "--noconfirm", "tailscale"]
    if manager == "brew":
        return ["brew", "install", "--cask", "tailscale"]
    if manager == "apt":
        return ["sudo", "apt-get", "install", "-y", "tailscale"]
    return []


def auth_key_file() -> Path:
    """Where the tailnet auth key lives. Outside every repository."""
    return AUTH_KEY_FILE


def needs_root() -> bool:
    """Does `tailscale up` need root on this machine?

    `tailscaled` runs as root on Linux, so a non-root `tailscale up` fails
    with a permission error that reads like a bad key. On macOS the app
    runs the daemon in the user's own session and no root is needed.
    """
    if platform.system() == "Darwin":
        return False
    try:
        return os.geteuid() != 0
    except AttributeError:
        return False


def sudo_available() -> bool:
    """Is sudo usable without a password prompt?"""
    if not shutil.which("sudo"):
        return False
    try:
        return subprocess.run(["sudo", "-n", "true"], capture_output=True,
                               timeout=15).returncode == 0
    except Exception:
        return False


def tailscale_up_command(key_file: Path) -> list:
    """The command that joins the tailnet.

    The key travels as a **path** — `--auth-key=file:<path>` — never as a
    value, and root is added only when root is what the command needs.
    """
    command = ["tailscale", "up", f"--auth-key=file:{key_file}"]
    if needs_root():
        command = ["sudo"] + command
    return command


def operator_command() -> list:
    """The command that lets this user run `tailscale` without root."""
    if platform.system() == "Darwin":
        return []
    return ["sudo", "tailscale", "set", f"--operator={os.environ.get('USER', '')}"]


def join_tailnet(key_file: Optional[Path] = None) -> dict:
    """Join the tailnet. The trigger for the whole node setup.

    The sequence is honest about its own limits:

    - Tailscale missing → a **manual step** with the exact command. The
      tool does not run `sudo` behind the operator's back.
    - no auth key file → a **manual step**: the wizard establishes the
      secret, and the wizard is the only thing that asks for it.
    - root needed and no non-interactive sudo → a **manual step** with the
      exact command, rather than a password prompt nobody asked for.
    - otherwise → `tailscale up --auth-key=file:<path>`, which puts a
      **path** in the argument and never the value.

    The result is reported either way. A dead tailnet is never silently
    continued past.
    """
    path = Path(key_file) if key_file else auth_key_file()
    result = {"joined": False, "detail": "", "manual": False,
              "ip": "", "hostname": ""}

    if not tailscale_installed():
        command = install_command()
        result["manual"] = True
        result["detail"] = (
            "Tailscale is not installed on this machine."
            + (f" Install it with: {' '.join(command)}" if command else
               " No known package manager was found."))
        return result

    if not path.exists():
        result["manual"] = True
        result["detail"] = (
            f"no tailnet auth key at {path}. The wizard asks for it once "
            "and stores it there; nothing else creates it.")
        return result

    command = tailscale_up_command(path)
    if needs_root() and not sudo_available():
        result["manual"] = True
        result["detail"] = (
            "joining the tailnet needs root, and sudo is not usable without "
            "a password prompt here. Run it yourself:\n  "
            + " ".join(command))
        return result

    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=300)
    except Exception as exc:
        result["detail"] = f"{type(exc).__name__}: {exc}"
        return result
    if completed.returncode != 0:
        result["detail"] = (completed.stderr or completed.stdout or
                            "tailscale up failed").strip()[:300]
        return result

    status = tailscale_status()
    result["joined"] = bool(status.get("running"))
    result["ip"] = status.get("ip", "")
    result["hostname"] = status.get("hostname", "")
    result["detail"] = ("joined as " + (result["ip"] or "an unknown address")) \
        if result["joined"] else "tailscale up returned success but the " \
        "tailnet is not running"

    if result["joined"]:
        profile = node_profile()
        profile["tailnet"] = {
            "ip": result["ip"],
            "hostname": result["hostname"],
            "joined_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        save_node_profile(profile)
    return result


# ---------------------------------------------------------------- the heartbeat --

def service_states() -> dict:
    """The state of the services a node depends on. Names and states only."""
    states = {"tailscaled": "absent", "sshd": "absent", "node-daemon": "running"}
    # The tailnet daemon is asked rather than guessed at: the process is
    # called something different on every platform, but `tailscale status`
    # answers on exactly the machine where it is running.
    status = tailscale_status()
    if status.get("running"):
        states["tailscaled"] = "active"
    elif status.get("installed"):
        states["tailscaled"] = "stopped"
    if platform.system() != "Darwin":
        try:
            completed = subprocess.run(["systemctl", "--user", "is-active",
                                        "sshd"], capture_output=True,
                                       text=True, timeout=20)
            if completed.returncode == 0:
                states["sshd"] = completed.stdout.strip() or "active"
        except Exception:
            pass
    else:
        try:
            completed = subprocess.run(["pgrep", "-x", "sshd"],
                                       capture_output=True, text=True,
                                       timeout=20)
            if completed.returncode == 0:
                states["sshd"] = "active"
        except Exception:
            pass
    return states


def uptime_seconds() -> float:
    """How long this machine has been up.

    On Linux that is `/proc/uptime`. On macOS it is `kern.boottime`, and
    `sysctl` lives in **`/usr/sbin`** — which is NOT in a normal user's
    PATH. Looking it up by name alone made this return 0 on any machine
    whose PATH did not happen to carry /usr/sbin, and the failure was
    silent: a contract field that reads zero is worse than one that is
    absent, because zero is a number and looks like an answer.
    """
    try:
        with open("/proc/uptime", encoding="utf-8") as handle:
            return float(handle.read().split()[0])
    except OSError:
        pass
    for candidate in ("/usr/sbin/sysctl", "/sbin/sysctl", "sysctl"):
        try:
            completed = subprocess.run([candidate, "-n", "kern.boottime"],
                                       capture_output=True, text=True,
                                       timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        text = (completed.stdout or "").strip()
        if completed.returncode != 0 or "sec = " not in text:
            continue
        try:
            boot = float(text.split("sec = ")[1].split(",")[0].strip())
        except (IndexError, ValueError):
            continue
        if boot > 0:
            return max(0.0, time.time() - boot)
    return 0.0


def last_sequence() -> int:
    """The sequence number of the last heartbeat in the log.

    The sequence is monotonic across restarts, not within one: a daemon
    that restarts and starts again at 1 produces two heartbeats with the
    same number, and the record then has two claims to be the same
    moment. So the daemon picks up where the log left off.
    """
    try:
        lines = HEARTBEAT_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    for line in reversed(lines[-100:]):
        line = line.strip()
        if not line:
            continue
        try:
            return int(json.loads(line).get("seq", 0))
        except (ValueError, TypeError, AttributeError):
            continue
    return 0


def heartbeat_payload(sequence: int) -> dict:
    """The heartbeat. Fixed fields, no secrets, no file contents.

    The tailnet address is **not** in the payload: a heartbeat is written
    to a record that may be published, and a `100.x` address in a published
    document is a private-network address by the visibility contract's own
    reading. The node's address lives in the node-local profile.
    """
    return {
        "seq": sequence,
        "node": node_id(),
        "hostname": socket.gethostname(),
        "os": platform.system(),
        "os_release": platform.release(),
        "arch": platform.machine(),
        "uptime_s": int(uptime_seconds()),
        "services": service_states(),
        "tailnet_joined": bool(tailscale_status().get("running")),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def write_heartbeat(payload: dict) -> Path:
    """Append the heartbeat to the node-local record.

    Two records, both node-local: a JSON log for machines, and a small
    markdown record for humans. Neither is published, and neither carries a
    credential.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass
    with HEARTBEAT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    try:
        os.chmod(HEARTBEAT_LOG, 0o600)
    except OSError:
        pass
    _trim_heartbeat_log()

    lines = [
        "<!-- visibility: node-private -->",
        "",
        "# the node's record",
        "",
        f"node: {payload.get('node')}",
        f"host: {payload.get('hostname')}",
        "",
        "## last heartbeat",
        "",
    ]
    for key in ("seq", "ts", "os", "arch", "uptime_s", "tailnet_joined"):
        lines.append(f"- {key}: {payload.get(key)}")
    lines.append("")
    lines.append("## services")
    lines.append("")
    for name, state in sorted((payload.get("services") or {}).items()):
        lines.append(f"- {name}: {state}")
    lines.append("")
    lines.append("The heartbeat is this node's only claim to liveness.")
    lines.append("A silent node is a dead node.")
    lines.append("")
    NODE_RECORD.write_text("\n".join(lines), encoding="utf-8")
    try:
        os.chmod(NODE_RECORD, 0o600)
    except OSError:
        pass
    return HEARTBEAT_LOG


def _trim_heartbeat_log(limit: int = DEFAULT_RETENTION) -> None:
    """Keep the log bounded. The retention number is a default, not a law."""
    try:
        lines = HEARTBEAT_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= limit:
        return
    keep = lines[-limit:]
    HEARTBEAT_LOG.write_text("\n".join(keep) + "\n", encoding="utf-8")


# ------------------------------------------------------------------- the export --

def visibility_marker_of(text: str) -> Optional[str]:
    """The visibility marker a block declares, or None."""
    for line in text.splitlines()[:10]:
        stripped = line.strip()
        if not stripped.startswith("<!--"):
            continue
        if "visibility:" not in stripped:
            continue
        value = stripped.split("visibility:", 1)[1].split("-->", 1)[0].strip()
        return value.lower() or None
    return None


def check_export_target(target) -> tuple:
    """Whether a target may be pushed to. Returns (allowed, why).

    The spine's record has **exactly one writer, and it is the spine**. A
    node that writes there is the second writer the contract forbids, so
    that target is refused outright.

    A path that is not obviously an inbox is allowed with a warning: this
    module cannot know the spine's layout, and a warning is cheaper than
    being wrong about it.

    This rule lives here, in the contract module, and not in the tool that
    pushes: a rule that only the tool knows about is a rule the contract
    does not have.
    """
    path = Path(target)
    name = path.name.lower()
    if name in ("merged.md", "merged.markdown"):
        return False, ("the spine's record has exactly one writer, and it "
                       "is the spine. push to an inbox instead.")
    if "inbox" not in name and "inbox" not in str(path.parent).lower():
        return True, ("this does not look like an inbox — the spine's "
                      "writer reads from an inbox, so check the path.")
    return True, ""


def check_export(block: str, target=None) -> tuple:
    """Whether a block may be exported. Returns (allowed, why).

    Two rules, both from the contract:

    - the block carries a visibility marker — untagged content is readable
      as written, so an untagged export is refused rather than published by
      accident;
    - the target is an **inbox**, not the spine's own file. The spine has
      exactly one writer and it is the spine.

    The target rule needs a target, so it is checked only when one is
    given. `check_export_target` is the rule itself; this is both of them.
    """
    marker = visibility_marker_of(block)
    if not marker:
        return False, ("the block carries no visibility marker. Untagged "
                       "content is readable as written, so it is refused.")
    if marker not in VISIBILITY_MARKERS:
        return False, f"unknown visibility marker: {marker}"
    if target is not None:
        allowed, why = check_export_target(target)
        if not allowed:
            return False, why
    return True, marker
