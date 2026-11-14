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
from datetime import datetime, timezone
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
SPINE_EVENTS_LOG = STATE_DIR / "spine_events.jsonl"
NODE_RECORD = STATE_DIR / "record.md"
AUTH_KEY_FILE = NODE_DIR / "auth.key"

# The node's native events, for the spine. The spine has exactly one
# writer, and it is the spine: the Studio's reducer reads these rows and
# envelopes them into the ledger. So the node writes the event the way
# Bruce writes his ledger rows — natively, with an id of its own — and
# computes neither the ULID nor the chain.
SPINE_SCHEMA = "genesis.node.v1"
SPINE_EVENT_TYPE = "node.heartbeat"
SPINE_CATEGORY = "system"
SPINE_VISIBILITY = "public"

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

def node_profile(path: Optional[Path] = None) -> dict:
    """The node-local profile. Machine-local, never published.

    The path can be given, so that a caller whose HOME is not this
    process's HOME — a test with a temporary home, a tool that resolves
    the location itself — reads the file it means to read, and not the
    operator's.
    """
    target = Path(path) if path else NODE_PROFILE
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
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

    This is the **only** route table: pacman on Arch and Omarchy, apt on
    Debian, brew on macOS. Never a vendored binary, never a `curl | sh`.

    Returned rather than run, so that the caller can look at it first.
    `install_tailscale` is the caller that runs it — and it runs it only
    when the command needs no password, because a password prompt nobody
    asked for is not a step a tool takes.
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

    - Tailscale missing → a **manual step** with the exact command.
    - no auth key file → a **manual step**: the wizard establishes the
      secret, and the wizard is the only thing that asks for it.
    - root needed and no non-interactive sudo → a **manual step** with the
      exact command, rather than a password prompt nobody asked for.
    - otherwise → `tailscale up --auth-key=file:<path>`, which puts a
      **path** in the argument and never the value.

    The result is reported either way. A dead tailnet is never silently
    continued past.

    Whether a join is needed at all is the caller's question: the key is
    single-use and spent once it has been used, so the callers ask
    `tailscale_status` before they call this.
    """
    path = Path(key_file) if key_file else auth_key_file()
    result = {"joined": False, "detail": "", "manual": False,
              "ip": "", "hostname": "", "reason": ""}

    if not tailscale_installed():
        command = install_command()
        result["manual"] = True
        result["reason"] = "not-installed"
        result["detail"] = (
            "Tailscale is not installed on this machine."
            + (f" Install it with: {' '.join(command)}" if command else
               " No known package manager was found."))
        return result

    if not path.exists():
        result["manual"] = True
        result["reason"] = "no-key"
        result["detail"] = (
            f"no tailnet auth key at {path}. The wizard asks for it once "
            "and stores it there; nothing else creates it.")
        return result

    command = tailscale_up_command(path)
    if needs_root() and not sudo_available():
        result["manual"] = True
        result["reason"] = "needs-root"
        result["detail"] = (
            "joining the tailnet needs root, and sudo is not usable without "
            "a password prompt here. Run it yourself:\n  "
            + " ".join(command))
        return result

    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=300, stdin=subprocess.DEVNULL)
    except Exception as exc:
        result["reason"] = "failed"
        result["detail"] = f"{type(exc).__name__}: {exc}"
        return result
    if completed.returncode != 0:
        result["reason"] = "failed"
        result["detail"] = (completed.stderr or completed.stdout or
                            "tailscale up failed").strip()[:300]
        return result

    status = tailscale_status()
    result["joined"] = bool(status.get("running"))
    result["ip"] = status.get("ip", "")
    result["hostname"] = status.get("hostname", "")
    result["reason"] = "joined" if result["joined"] else "failed"
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


# ------------------------------------------------------------------- the host --

# The daemon's identity, in the two places that need it: the launchd label
# and the systemd unit name. One name, so there is one job on a machine.
DAEMON_LABEL = "dev.genesis.node"
DAEMON_UNIT_NAME = "genesis-node.service"


def daemon_kind() -> str:
    """How a node's daemon runs on this host.

    A **user** service, always: a `systemd --user` unit on Linux, a
    `LaunchAgent` on macOS. Never a system one — no sudo, and it survives
    `omarchy-update`, because a machine that has to be re-rooted to keep
    its own node alive is not a node.

    The empty string means this host runs neither, and so cannot host a
    node's daemon.
    """
    if platform.system() == "Darwin":
        return "launchd-agent"
    if Path("/run/systemd/system").exists():
        return "systemd-user"
    return ""


def daemon_unit_path(kind: str = "") -> Path:
    """Where the daemon's unit lives on this host."""
    if kind == "launchd-agent":
        return Path.home() / "Library" / "LaunchAgents" / f"{DAEMON_LABEL}.plist"
    return Path.home() / ".config" / "systemd" / "user" / DAEMON_UNIT_NAME


def daemon_unit_content(kind: str, script: Optional[Path] = None,
                         workdir: Optional[Path] = None) -> str:
    """The unit. The one copy of this text.

    The daemon runs as a **user** service: `systemd --user` on Linux, a
    `LaunchAgent` on macOS. No sudo, and it survives `omarchy-update`.
    """
    daemon = Path(script) if script else (ROOT / "tools" / "node_daemon.py")
    work = Path(workdir) if workdir else ROOT
    if kind == "launchd-agent":
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            "<dict>\n"
            "  <key>Label</key>\n"
            f"  <string>{DAEMON_LABEL}</string>\n"
            "  <key>ProgramArguments</key>\n"
            "  <array>\n"
            f"    <string>{sys.executable}</string>\n"
            f"    <string>{daemon}</string>\n"
            "  </array>\n"
            "  <key>RunAtLoad</key>\n"
            "  <true/>\n"
            "  <key>KeepAlive</key>\n"
            "  <true/>\n"
            "  <key>WorkingDirectory</key>\n"
            f"  <string>{work}</string>\n"
            "</dict>\n"
            "</plist>\n")
    return (
        "[Unit]\n"
        "Description=GENESIS node — the heartbeat daemon\n"
        "\n"
        "[Service]\n"
        "ExecStart=%s %s\n"
        "Restart=on-failure\n"
        "RestartSec=30\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n" % (sys.executable, daemon))


def install_daemon_unit(content: str, path: Path) -> dict:
    """Write the daemon's unit — if it is not already this one.

    A unit that is already ours is left exactly as it is. A unit that is
    NOT ours is never overwritten: something else owns that label, and a
    node that quietly took it over would be a false success. The refusal
    is the honest answer, and it is a conflict, not a failure.
    """
    path = Path(path)
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "path": str(path), "reason": "failed",
                    "detail": f"{type(exc).__name__}: {exc}"}
        if existing == content:
            return {"ok": True, "path": str(path), "reason": "already",
                    "detail": "already there — " + str(path)}
        return {"ok": False, "path": str(path), "reason": "conflict",
                "detail": (f"a different unit is at {path}. it was left alone "
                           f"— remove it, or point the node at this one")}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError as exc:
        return {"ok": False, "path": str(path), "reason": "failed",
                "detail": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "path": str(path), "reason": "written",
            "detail": "written — " + str(path)}


def enable_daemon(path: Path) -> tuple:
    """Enable and start the daemon as a *user* service. Never a system one.

    The launchd branch takes the label first, waits for launchd to release
    it, and then CHECKS that the job left registered is the file we wrote:
    `launchctl load` onto a label that is already taken prints "Load
    failed" and returns 0, so the old job would keep the label and the
    plist just written would be ignored.
    """
    unit_path = Path(path)
    if unit_path.suffix == ".plist":
        label = unit_path.stem
        domain = f"gui/{os.getuid()}/{label}"
        subprocess.run(["launchctl", "bootout", domain],
                       capture_output=True, text=True, timeout=60,
                       stdin=subprocess.DEVNULL)
        # The bootout is ASYNCHRONOUS: launchd releases the label a moment
        # later, and a load issued in that window still finds it taken. So
        # wait for the label to actually be free before loading into it.
        for _ in range(40):
            if subprocess.run(["launchctl", "print", domain],
                              capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL).returncode != 0:
                break
            time.sleep(0.25)
        result = subprocess.run(["launchctl", "load", "-w", str(unit_path)],
                                capture_output=True, text=True, timeout=120,
                                stdin=subprocess.DEVNULL)
        stderr = (result.stderr or "").strip()
        if result.returncode != 0 or "Load failed" in stderr:
            return False, stderr[:200]
        registered = subprocess.run(["launchctl", "print", domain],
                                    capture_output=True, text=True, timeout=60,
                                    stdin=subprocess.DEVNULL)
        if str(unit_path.resolve()) not in (registered.stdout or ""):
            return False, (f"{label} did not take: it is registered from "
                           f"somewhere else")
        return True, "loaded"
    result = subprocess.run(["systemctl", "--user", "enable", "--now",
                             unit_path.name],
                            capture_output=True, text=True, timeout=120,
                            stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        return False, (result.stderr or "").strip()[:200]
    return True, "enabled"


def daemon_state(path: Optional[Path] = None) -> dict:
    """Is the daemon running, and is it running OUR unit?

    The question is asked of the service manager, never of the file: a unit
    on disk is a write report, not a liveness report.
    """
    kind = daemon_kind()
    unit = Path(path) if path else daemon_unit_path(kind)
    if not unit.exists():
        return {"running": False, "detail": "no unit at " + str(unit)}
    if unit.suffix == ".plist":
        label = unit.stem
        domain = f"gui/{os.getuid()}/{label}"
        try:
            result = subprocess.run(["launchctl", "print", domain],
                                    capture_output=True, text=True, timeout=60,
                                    stdin=subprocess.DEVNULL)
        except Exception as exc:
            return {"running": False, "detail": f"{type(exc).__name__}: {exc}"}
        if result.returncode != 0:
            return {"running": False, "detail": "not registered with launchd"}
        if str(unit.resolve()) not in (result.stdout or ""):
            return {"running": False,
                    "detail": f"registered from somewhere else, not {unit}"}
        return {"running": True, "detail": label}
    try:
        result = subprocess.run(["systemctl", "--user", "is-active",
                                 unit.name], capture_output=True, text=True,
                                timeout=60, stdin=subprocess.DEVNULL)
    except Exception as exc:
        return {"running": False, "detail": f"{type(exc).__name__}: {exc}"}
    if result.returncode != 0:
        return {"running": False,
                "detail": (result.stdout or "").strip() or "not active"}
    return {"running": True, "detail": (result.stdout or "").strip() or "active"}


def install_tailscale() -> dict:
    """Install Tailscale with the system package manager. Never vendored.

    `{"installed": bool, "manual": bool, "detail": str}`. The **manual**
    flag means a human still owes the step: the install needs root and
    sudo is not usable without a password prompt here. A tool that opens a
    password prompt nobody asked for is a tool that should not exist, so
    the command is handed back instead, with the reason.
    """
    if tailscale_installed():
        return {"installed": True, "manual": False,
                "detail": "already installed — " + (which("tailscale") or "")}
    command = install_command()
    if not command:
        return {"installed": False, "manual": True,
                "detail": "no known package manager — install Tailscale by hand"}
    if command[0] == "sudo" and not sudo_available():
        return {"installed": False, "manual": True,
                "detail": ("installing Tailscale needs root, and sudo is not "
                           "usable without a password prompt here. Run it "
                           "yourself:\n  " + " ".join(command))}
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=900, stdin=subprocess.DEVNULL)
    except Exception as exc:
        return {"installed": False, "manual": False,
                "detail": f"{type(exc).__name__}: {exc}"}
    if result.returncode != 0:
        return {"installed": False, "manual": False,
                "detail": (f"{' '.join(command)} failed: "
                           f"{(result.stderr or '').strip()[:200]}")}
    if not tailscale_installed():
        return {"installed": False, "manual": False,
                "detail": "the install finished but tailscale is not on PATH"}
    return {"installed": True, "manual": False,
            "detail": "installed — " + (which("tailscale") or "")}


# ---------------------------------------------------------------- the setup --

def setup_node(node_dir: Path, join: bool = True) -> dict:
    """Set this machine up as a node. Returns {"rows": [...], "exit": int}.

    Every row is the result of something that ran, or the middot that says
    a human still owes the step. Nothing here is claimed because a plan
    intended it.

    `join=False` changes **nothing** on this machine: no install, no unit,
    no service, no join. It is the readiness probe's mode, and the
    wizard's own readiness probe calls it — so the promise is kept here,
    in the one place that could break it.

    The exit code is the most specific refusal, and the ladder is the
    project's own: 3 unsupported host, 5 a prerequisite a human owes, 6 a
    conflict, 9 a precondition the wizard owns, 1 something failed, 0 all
    of it worked.
    """
    if not join:
        return {"rows": [], "exit": 0}

    rows = []
    failed = False
    manual_install = False
    unsupported_host = False
    conflict = False
    missing_key = False

    # 1. Tailscale — the system package manager, or it is already there.
    if tailscale_installed():
        rows.append(("tailscale", "✓",
                     "already installed — " + (which("tailscale") or "")))
    else:
        result = install_tailscale()
        if result["installed"]:
            rows.append(("tailscale", "✓", result["detail"]))
        elif result["manual"]:
            rows.append(("tailscale", "·", result["detail"]))
            manual_install = True
        else:
            rows.append(("tailscale", "✗", result["detail"]))
            failed = True

    # 2. The tailnet — the join, with the secret the wizard established.
    #    Whether a join is needed is asked first: the key is single-use,
    #    and a spent key fails for the wrong reason.
    status = tailscale_status()
    if status.get("running"):
        rows.append(("tailnet", "✓",
                     "already on the tailnet — " + (status.get("ip") or
                                                    "no address")))
    else:
        joined = join_tailnet()
        if joined["joined"]:
            rows.append(("tailnet", "✓", joined["detail"]))
        elif joined["manual"]:
            rows.append(("tailnet", "·", joined["detail"]))
            if joined.get("reason") == "no-key":
                missing_key = True
        else:
            rows.append(("tailnet", "✗", joined["detail"]))
            failed = True

    # 3 and 4. The daemon's unit, and the daemon itself.
    kind = daemon_kind()
    if not kind:
        rows.append(("daemon unit", "✗",
                     "this host runs neither systemd --user nor launchd"))
        unsupported_host = True
    else:
        script = Path(node_dir) / "tools" / "node_daemon.py"
        if not script.exists():
            script = ROOT / "tools" / "node_daemon.py"
        workdir = Path(node_dir) if Path(node_dir).exists() else ROOT
        unit = daemon_unit_path(kind)
        written = install_daemon_unit(
            daemon_unit_content(kind, script, workdir), unit)
        if not written["ok"]:
            rows.append(("daemon unit", "✗", written["detail"]))
            if written.get("reason") == "conflict":
                conflict = True
            else:
                failed = True
        else:
            rows.append(("daemon unit", "✓", written["detail"]))
            enabled, detail = enable_daemon(unit)
            state = daemon_state(unit)
            if enabled and state["running"]:
                rows.append(("daemon", "✓", "running — " + state["detail"]))
            elif enabled:
                rows.append(("daemon", "✗", state["detail"]))
                failed = True
            else:
                rows.append(("daemon", "✗", detail))
                failed = True

    # 5. The spine — what this node reports, and where it says so.
    reporting = spine_reporting_state()
    if reporting["reporting"]:
        rows.append(("spine events", "✓", reporting["detail"]))
    else:
        rows.append(("spine events", "·", reporting["detail"]))

    exit_code = 1 if failed else 0
    if manual_install:
        exit_code = exit_code or 5
    if unsupported_host:
        exit_code = 3
    if conflict:
        exit_code = 6
    if missing_key:
        exit_code = 9
    return {"rows": rows, "exit": exit_code}


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
    _trim_log(HEARTBEAT_LOG, limit)


def _trim_log(path: Path, limit: int = DEFAULT_RETENTION) -> None:
    """Keep an append-only log bounded."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= limit:
        return
    keep = lines[-limit:]
    path.write_text("\n".join(keep) + "\n", encoding="utf-8")


# ------------------------------------------------------------- the spine events --

def spine_event(payload: dict) -> dict:
    """One native event, in the shape the spine's reducer envelopes.

    The node computes **neither** the event id nor the chain: the spine
    has exactly one writer, and it is the spine. What the node owns is its
    own event — a stable id of its own, so the reducer can ingest it
    exactly once — and the payload.

    The payload is the heartbeat, unchanged: no credential, no tailnet
    address, no user file contents. That is what makes this event
    `public` rather than `operator`.

    `turn_id` is null and `turn_role` is "ambient": a node cannot know
    whether a beat falls inside a Mac tick's window, so it does not claim
    that linkage. The Studio's reducer decides it, the way it does for
    every other source.
    """
    node = str(payload.get("node") or node_id())
    epoch_ms = int(time.time() * 1000)
    summary = f"node {node} heartbeat {payload.get('seq')}"
    return {
        "node": f"genesis-{node}",
        "timestamp": datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"),
        "category": SPINE_CATEGORY,
        "type": SPINE_EVENT_TYPE,
        "source": node,
        "turn_id": None,
        "turn_role": "ambient",
        "payload": payload,
        "summary": summary[:200],
        "visibility": SPINE_VISIBILITY,
        "permissions_required": [],
        "provenance": {
            "node_id": f"genesis-{node}",
            "local_event_id": f"{epoch_ms}-{payload.get('seq')}",
            "native_schema": SPINE_SCHEMA,
        },
    }


def write_spine_event(payload: dict) -> dict:
    """Append one native event to the node's spine log.

    The log is the node's **native** source — the thing the Studio reads
    when it pulls this node, the way it pulls Bruce's ledger. It is
    append-only, bounded, and node-local; it is not the spine, and
    writing here is not a second writer of the spine's record.

    Returns the event that was written, so that the caller can name it.
    """
    event = spine_event(payload)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass
    with SPINE_EVENTS_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
    try:
        os.chmod(SPINE_EVENTS_LOG, 0o600)
    except OSError:
        pass
    _trim_log(SPINE_EVENTS_LOG)
    return event


def spine_reporting_state() -> dict:
    """Is this node's event log being written? -> {"reporting", "detail"}

    Proved by the log's own mtime, not by the fact that a daemon unit
    exists: a unit on disk is a write report, not a liveness report.
    """
    if not SPINE_EVENTS_LOG.exists():
        return {"reporting": False,
                "detail": "no event log at " + str(SPINE_EVENTS_LOG)}
    try:
        age = time.time() - SPINE_EVENTS_LOG.stat().st_mtime
    except OSError as exc:
        return {"reporting": False, "detail": f"{type(exc).__name__}: {exc}"}
    if age > DEFAULT_INTERVAL * 3:
        return {"reporting": False,
                "detail": (f"the event log has not been written to for "
                           f"{int(age)}s")}
    return {"reporting": True, "detail": str(SPINE_EVENTS_LOG)}


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
