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


def stub_cli_on_path(directory: Path) -> str | None:
    """Stub the CLI and Tailscale onto PATH.

    Returns the previous value of PATH, or None if it was not set, so the
    caller can put it back exactly as it was.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for name, body in (("claude", _CLAUDE), ("tailscale", _TAILSCALE)):
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
