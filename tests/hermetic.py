"""GENESIS — the suite's hermetic helpers.

**Why this file exists.** The wizard's plan installs the Claude Code CLI
when it is absent, and it installs it the way the vendor says to:
``curl -fsSL https://claude.ai/install.sh | sh``. A test that reaches the
apply path therefore reached a live download and a remote shell script —
on any machine that does not already have the CLI on PATH, which is every
fresh clone, and which is exactly the machine the wizard is for.

More than one file in this suite claimed, in its own docstring, that it
touches no network and no live service. That claim was false. This is
the fix: the CLI is stubbed onto PATH, so the plan finds it present,
short-circuits to "already installed", and never runs the installer.

Tailscale is stubbed for the same reason. On the aggressive tier the
wizard installs it with the **system package manager** — `sudo pacman`,
`sudo apt-get`, `brew install --cask` — and a unit test must never be
able to install a system package.

launchctl is stubbed for the same reason, one step further along. The
wizard's plan installs the shim as a **user LaunchAgent** and then enables
it, and `launchctl load -w` registers a job that **keeps running after the
test exits** — under KeepAlive, pointing at a plist in a tmpdir pytest has
already deleted. The suite was leaving a live service in the operator's
session on every single run.

The launchctl stub is a **fake, not a no-op**: it remembers the job it was
told to load and answers `print` from that record, so the wizard's
enable path can be walked honestly — including the case the real
launchctl hides, where a load onto a label that is already taken prints
"Load failed" and **returns 0**.

Nothing here changes what the wizard does for a real operator. It only
stops a unit test from doing what an operator would do.
"""
from __future__ import annotations

import os
from pathlib import Path

# The CLI stub answers `--version` and does nothing else. The wizard asks
# the CLI two questions: is it on PATH, and what version is it.
_CLAUDE = """#!/bin/sh
if [ "$1" = "--version" ]; then
  echo '1.0.0 (stub)'
fi
exit 0
"""

# The Tailscale stub answers `status --json` with nothing useful, so the
# probe sees an installed-but-not-running tailnet and never the real one.
_TAILSCALE = """#!/bin/sh
if [ "$1" = "status" ] && [ "$2" = "--json" ]; then
  echo '{"BackendState":"Stopped"}'
fi
exit 0
"""

# The launchctl stub is a FAKE launchd, not a no-op. It keeps a record of
# the job it was told to load, and answers `print` from that record — so
# the wizard's enable path can be walked, and its verification (that the
# job registered is the file the wizard wrote) can be checked.
#
# Two modes make the real launchctl's two lies reproducible:
#   GENESIS_STUB_LAUNCHCTL_MODE=fail   the load prints "Load failed: 5" on
#       stderr and returns 0 — what the real launchctl does when the label
#       is already taken.
#   GENESIS_STUB_LAUNCHCTL_MODE=taken  the label is held by a job that
#       cannot be taken back: bootout does nothing, and the load says
#       nothing and changes nothing.
_LAUNCHCTL = r'''#!/usr/bin/env python3
"""A launchctl that touches no live service, and remembers what it was told."""
import os
import sys

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     ".launchctl-state")
MODE = os.environ.get("GENESIS_STUB_LAUNCHCTL_MODE", "")


def read():
    try:
        with open(STATE, encoding="utf-8") as fh:
            return [line.rstrip("\n") for line in fh if line.strip()]
    except OSError:
        return []


def write(lines):
    with open(STATE, "w", encoding="utf-8") as fh:
        fh.write("".join(line + "\n" for line in lines))


def label_of(target):
    """The job label for a plist PATH or a DOMAIN.

    `gui/501/dev.genesis.shim` names the job `dev.genesis.shim` — the last
    component, whole. `.../dev.genesis.shim.plist` is the FILE that job is
    registered from, and its label is the stem. Stripping the extension
    off the domain would turn both into `dev.genesis`, and the job that
    was loaded would never be the job that was asked for.
    """
    base = os.path.basename(target)
    if base.endswith(".plist"):
        return base[:-len(".plist")]
    return base


def find(label):
    for line in read():
        parts = line.split("\t")
        if parts[0] == label:
            return parts[1] if len(parts) > 1 else ""
    return None


def set_job(label, path):
    lines = [line for line in read() if line.split("\t")[0] != label]
    lines.append(label + "\t" + path)
    write(lines)


def drop_job(label):
    write([line for line in read() if line.split("\t")[0] != label])


args = sys.argv[1:]
verb = args[0] if args else ""
targets = [a for a in args[1:] if not a.startswith("-")]

if verb == "load":
    if MODE == "fail":
        # What the real launchctl does onto a label that is taken.
        sys.stderr.write("Load failed: 5: Input/output error\n")
        sys.exit(0)
    for path in targets:
        if MODE == "taken" and find(label_of(path)):
            continue
        # launchd stores the resolved path, and prints that back.
        set_job(label_of(path), os.path.realpath(path))
    sys.exit(0)

if verb in ("unload", "bootout"):
    for target in targets:
        if MODE == "taken":
            continue
        drop_job(label_of(target))
    sys.exit(0)

if verb == "print":
    target = targets[0] if targets else ""
    path = find(label_of(target))
    if path:
        print("path = " + path)
        sys.exit(0)
    sys.stderr.write("Could not find service " + repr(label_of(target)) + "\n")
    sys.exit(113)

if verb == "list":
    for line in read():
        print("-\t0\t" + line.split("\t")[0])
    sys.exit(0)

sys.exit(0)
'''


def stub_cli_on_path(directory: Path) -> str | None:
    """Stub the CLI, Tailscale and launchctl onto PATH.

    Returns the previous value of PATH, or None if it was not set, so the
    caller can put it back exactly as it was.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for name, body in (("claude", _CLAUDE), ("tailscale", _TAILSCALE),
                       ("launchctl", _LAUNCHCTL)):
        stub = directory / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)
    previous = os.environ.get("PATH")
    os.environ["PATH"] = str(directory) + os.pathsep + (previous or "")
    return previous


def restore_path(previous: str | None) -> None:
    """Put PATH back exactly as it was."""
    if previous is None:
        os.environ.pop("PATH", None)
    else:
        os.environ["PATH"] = previous
