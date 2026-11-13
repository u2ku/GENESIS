"""GENESIS — the spine export, and the visibility it must not lose.

**The defect this file exists for.** `tools/spine_export.py` pushed every
block by appending to ONE fixed file over ssh
(`cat >> <dir>/node-export-stdin.md`). Two pushes therefore landed in one
file with no boundary between them.

That is not a tidiness issue. The visibility contract decides privacy at
WRITE time and never edits it afterwards, and `node.visibility_marker_of`
reads the FIRST marker in a block's header — so when a `sam-readable`
block and a `node-private` block shared a file, the whole file's
effective visibility was `sam-readable`, and the private block was
published as readable.

The local push was already right (one file per push); the ssh push was
not. The same contract, two routes, two behaviours.

These tests use a stub `ssh` on PATH: no network, no remote host, no
credential, and nothing written outside a temporary directory.
"""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import node  # noqa: E402
import spine_export  # noqa: E402


STUB_SSH = """#!/bin/sh
# $1 is the host; the rest is the remote command.
shift
cmd=""
for a in "$@"; do cmd="$cmd $a"; done
eval "$cmd"
"""


class ExportCase(unittest.TestCase):
    """A temporary inbox, a stub ssh, and nothing real."""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="genesis_spine_"))
        self.stub = self.work / "stub"
        self.stub.mkdir()
        self.ssh = self.stub / "ssh"
        self.ssh.write_text(STUB_SSH, encoding="utf-8")
        self.ssh.chmod(0o755)
        self.remote = self.work / "remote"
        self.inbox = self.remote / "spine-inbox"
        self.inbox.mkdir(parents=True)

    def push_over_ssh(self, block: str) -> str:
        """Push a block through the stub, and return what it wrote."""
        env = dict(os.environ)
        env["PATH"] = f"{self.stub}:{env['PATH']}"
        import subprocess
        completed = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "spine_export.py"),
             "--block", str(self._write_block(block)), "--ssh",
             f"spine:{self.inbox}"],
            env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr)
        return completed.stdout

    def _write_block(self, text: str) -> Path:
        path = self.work / f"block-{len(list(self.work.iterdir()))}.md"
        path.write_text(text, encoding="utf-8")
        return path


class TestTheName(unittest.TestCase):
    """One push, one file — and two pushes never share a name."""

    def test_two_names_in_the_same_second_are_different(self):
        first = spine_export.export_name()
        second = spine_export.export_name()
        self.assertNotEqual(first, second,
                            "two pushes must not land in one file")

    def test_the_name_says_what_it_is(self):
        self.assertTrue(spine_export.export_name().startswith("node-export-"))
        self.assertTrue(spine_export.export_name().endswith(".md"))


class TestOnePushOneFile(ExportCase):
    """The defect, as a test."""

    def test_two_pushes_are_two_files(self):
        self.push_over_ssh("<!-- visibility: sam-readable -->\n\npublic\n")
        self.push_over_ssh("<!-- visibility: node-private -->\n\nprivate\n")
        files = sorted(p for p in self.inbox.iterdir() if p.is_file())
        self.assertEqual(
            len(files), 2,
            f"two pushes landed in {len(files)} file(s): {files}. Two blocks "
            "in one file share a header, and only the first marker in a "
            "header is visible.")

    def test_a_pushed_block_keeps_its_own_visibility(self):
        self.push_over_ssh("<!-- visibility: sam-readable -->\n\npublic\n")
        self.push_over_ssh("<!-- visibility: node-private -->\n\nprivate\n")
        private = [p for p in self.inbox.iterdir()
                   if "node-private" in p.read_text(encoding="utf-8")]
        self.assertEqual(len(private), 1,
                         "the private block must be a file of its own")
        text = private[0].read_text(encoding="utf-8")
        self.assertEqual(node.visibility_marker_of(text), "node-private",
                         "the private block must still read as private")
        self.assertNotIn("public", text,
                         "the public block must not be in the private file")

    def test_the_local_push_is_one_file_per_push_too(self):
        first = spine_export.push_local(
            "<!-- visibility: sam-readable -->\n\npublic\n",
            self.inbox, "sam-readable")
        second = spine_export.push_local(
            "<!-- visibility: node-private -->\n\nprivate\n",
            self.inbox, "node-private")
        self.assertNotEqual(first, second)
        self.assertEqual(len(list(self.inbox.iterdir())), 2)

    def test_the_pushed_file_is_not_world_readable(self):
        destination = spine_export.push_local(
            "<!-- visibility: node-private -->\n\nprivate\n",
            self.inbox, "node-private")
        mode = stat.S_IMODE(os.stat(destination).st_mode)
        self.assertEqual(mode, 0o600, f"the export is mode {oct(mode)}")

    def test_the_remote_file_is_not_world_readable(self):
        """The remote file is made by the remote shell, so its umask
        decides — 0644 unless the mode is set here, on the far side."""
        self.push_over_ssh("<!-- visibility: node-private -->\n\nprivate\n")
        files = [p for p in self.inbox.iterdir() if p.is_file()]
        self.assertEqual(len(files), 1)
        mode = stat.S_IMODE(os.stat(files[0]).st_mode)
        self.assertEqual(mode, 0o600,
                         f"the remote export is mode {oct(mode)} — world-readable")


class TestTheRefusals(ExportCase):
    """The contract's refusals still hold after the change."""

    def test_an_untagged_block_is_refused(self):
        allowed, why = node.check_export("# no marker here\n")
        self.assertFalse(allowed)
        self.assertIn("visibility marker", why)

    def test_the_spines_own_record_is_refused(self):
        allowed, why = spine_export.inbox_check(Path("/tmp/spine/merged.md"))
        self.assertFalse(allowed)
        self.assertIn("one writer", why)


class TestTheDryRun(ExportCase):
    """A dry run must exercise the same rules the push does.

    The target rule used to sit AFTER the dry run's early return, so
    `--dry-run` against the spine's own record printed "dry run.
    visibility: sam-readable" and exited 0 — and the push then refused
    with exit 3. The dry run is the moment the operator decides: if it
    says yes to a target the push will refuse, it has told them
    something false.
    """

    def write_block(self, marker: str = "sam-readable") -> Path:
        block = self.work / "block.md"
        block.write_text(f"<!-- visibility: {marker} -->\n\nbody\n",
                         encoding="utf-8")
        return block

    def test_a_dry_run_against_the_spines_own_record_is_refused(self):
        block = self.write_block()
        rc = spine_export.main(["--block", str(block),
                                "--to", str(self.remote / "merged.md"),
                                "--dry-run"])
        self.assertNotEqual(rc, 0,
                            "the dry run approved a target the push refuses")

    def test_the_dry_run_and_the_push_refuse_the_same_targets(self):
        block = self.write_block()
        spine_record = str(self.remote / "merged.md")
        dry = spine_export.main(["--block", str(block), "--to", spine_record,
                                 "--dry-run"])
        wet = spine_export.main(["--block", str(block), "--to", spine_record])
        self.assertEqual(dry, wet,
                         "the dry run and the push disagree about the target")

    def test_a_dry_run_to_an_inbox_is_allowed(self):
        block = self.write_block()
        rc = spine_export.main(["--block", str(block), "--to", str(self.inbox),
                                "--dry-run"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
