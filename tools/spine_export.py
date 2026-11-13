#!/usr/bin/env python3
"""SPINE EXPORT — one way, on purpose, when you mean it.

The canonical record on the spine has exactly one writer, and it is the
spine. A node does not merge into it, does not sync with it, and does not
pull from it in the background. What a node can do is **push a curated
block** — deliberately, by hand, to an inbox the spine reads.

That is what this tool is. It refuses to be anything else:

- it never syncs, never merges, never pulls. One direction, one block, one
  decision;
- it refuses a block with no visibility marker, because untagged content is
  readable as written and this tool is not the thing that decides that;
- it refuses to write the spine's own record directly. The target must be
  an **inbox** — a place the spine's writer reads from;
- **one push, one file.** Two blocks sharing a file share a header, and
  only the first marker in a header is visible — so a `node-private` block
  pushed after a `sam-readable` one would be published as readable. The
  visibility is decided at write time, so it is decided in the filename
  too.

    python3 tools/spine_export.py --block notes.md --to ~/.local/state/genesis/spine-inbox/
    python3 tools/spine_export.py --block notes.md --ssh luna:~/spine-inbox/

Standard library only. It never carries a credential, and it never prints
one.
"""
from __future__ import annotations

import argparse
import itertools
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import node  # noqa: E402


# The counter is per process; the pid is in the name too, so two NODES
# pushing in the same second are two files and not one.
_PUSHES = itertools.count(1)


def export_name() -> str:
    """The name of the file ONE push lands in.

    **One push, one file.** This is not tidiness. Two blocks that share a
    file share a header, and only the FIRST marker in a header is visible
    (`node.visibility_marker_of` reads the header, not the body) — so a
    block the operator tagged `node-private`, pushed after a
    `sam-readable` one, would be published as readable. The visibility is
    decided at write time and never edited afterwards, which means it has
    to be decided in the FILENAME too.

    The name carries the stamp, the pid and a counter, so two pushes in
    the same second — from this process or from another node — are two
    files and not one.
    """
    stamp = time.strftime("%Y%m%dT%H%M%S")
    return f"node-export-{stamp}-{os.getpid()}-{next(_PUSHES)}.md"


def inbox_check(path: Path) -> tuple:
    """Is this a place the spine reads from, rather than the spine's record?

    The rule belongs to the CONTRACT, so it lives in `node` and this is a
    thin call into it. A rule that only the tool knows about is a rule the
    contract does not have.
    """
    return node.check_export_target(path)


def push_local(block: str, target: Path, marker: str) -> str:
    """Push to a local inbox. One push, one file."""
    target.mkdir(parents=True, exist_ok=True)
    destination = target / export_name()
    destination.write_text(block, encoding="utf-8")
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return str(destination)


def push_ssh(block: str, host: str, remote_dir: str, marker: str) -> str:
    """Push over ssh. The block travels in the command's stdin, not argv.

    **One push, one file — the same rule the local push follows.** The
    block used to be appended to one fixed file, which put two blocks in
    one header: only the first marker was visible, and a `node-private`
    block that followed a `sam-readable` one was published as readable.
    The remote path is quoted, because a path with a space in it is a
    path, not a sentence.

    **The remote file is 0600, like the local one.** The file is created
    by the remote shell, so it gets the remote's umask — 0644 by default,
    which is world-readable. A block the local path protects is published
    to the far end of the tailnet in the clear unless the mode is set
    here, on the far side.
    """
    name = export_name()
    directory = remote_dir.rstrip("/")
    path = f"{directory}/{name}"
    command = ["ssh", host, "mkdir", "-p", shlex.quote(directory), "&&",
               "cat > " + shlex.quote(path), "&&",
               "chmod 600 " + shlex.quote(path)]
    completed = subprocess.run(command, input=block, capture_output=True,
                               text=True, timeout=300)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or "ssh failed").strip()[:300])
    return f"{host}:{path}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="spine_export.py",
        description="Push a curated, visibility-tagged block to the spine.")
    parser.add_argument("--block", required=True,
                        help="the curated block to push")
    parser.add_argument("--to", default=None,
                        help="a local inbox path")
    parser.add_argument("--ssh", default=None,
                        help="HOST:REMOTE_DIR to push to")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be pushed, push nothing")
    args = parser.parse_args(argv)

    source = Path(args.block).expanduser()
    if not source.exists():
        print(f"spine_export: no such block: {source}", file=sys.stderr)
        return 2
    block = source.read_text(encoding="utf-8")

    allowed, why = node.check_export(block)
    if not allowed:
        print("spine_export: refused — " + why, file=sys.stderr)
        return 3

    if bool(args.to) == bool(args.ssh):
        print("spine_export: give exactly one of --to or --ssh.",
              file=sys.stderr)
        return 2

    # The target is resolved and CHECKED before the dry run returns.
    #
    # A dry run is the moment the operator decides: if it says yes to a
    # target the push will refuse, it has told them something false. The
    # rule used to sit after this return, so `--dry-run` against the
    # spine's own record printed "dry run. visibility: sam-readable" and
    # exited 0 — and the push then refused with exit 3. A gate on the
    # wrong side of an early return is a gate that is not there.
    if args.to:
        target = Path(args.to).expanduser()
        host = remote = ""
    else:
        host, _, remote = args.ssh.partition(":")
        if not host or not remote:
            print("spine_export: --ssh wants HOST:REMOTE_DIR.",
                  file=sys.stderr)
            return 2
        target = Path(remote)

    allowed, why_not = node.check_export(block, target)
    if not allowed:
        print("spine_export: refused — " + why_not, file=sys.stderr)
        return 3

    if args.dry_run:
        print(f"spine_export: dry run. visibility: {why}")
        print(f"  block: {source} ({len(block)} bytes)")
        print(f"  target: {args.to or args.ssh}")
        if why_not and why_not != why:
            print("spine_export: " + why_not, file=sys.stderr)
        return 0

    try:
        if why_not and why_not != why:
            print("spine_export: " + why_not, file=sys.stderr)
        if args.to:
            destination = push_local(block, target, why)
        else:
            destination = push_ssh(block, host, remote, why)
    except Exception as exc:
        print(f"spine_export: the push failed — {exc}", file=sys.stderr)
        return 1

    print(f"spine_export: pushed ({why}).")
    print(f"  {destination}")
    print("  the spine's writer merges it. this tool wrote nothing else.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
