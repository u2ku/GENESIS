"""GENESIS — the node contract v0.1, closed.

The three scope items of the v0.1 contract, and the two places where the
code and the contract could drift apart:

- **the heartbeat daemon** — a fixed payload, a monotonic sequence number,
  a node-local record, and nothing that carries a credential;
- **the tailnet join** — the system package manager only, the key passed as
  a path, and a manual step rather than a silent continuation;
- **the one-way export** — a deliberate push, a visibility marker at write
  time, and a refusal to be the spine's second writer.

The drift this file exists to close:

- `uptime_s` is a **contract field**, and `sysctl` lives in `/usr/sbin`,
  which is not in a normal user's PATH. Looking it up by name alone made
  the field read 0 — silently, because a zero is a number and looks like
  an answer;
- the "one writer" rule is a **contract** rule, so it belongs in the
  contract module and not only in the tool that pushes. A rule that only
  the tool knows about is a rule the contract does not have.

    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import node  # noqa: E402


class UptimeCase(unittest.TestCase):
    """The uptime field, with a PATH that does not carry /usr/sbin."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        self._tmp.cleanup()

    def test_the_uptime_is_not_zero_when_sysctl_is_off_the_path(self):
        """The contract names uptime. A zero is a number that looks right.

        `sysctl` is in /usr/sbin on macOS, and /usr/sbin is not in a normal
        user's PATH — so the lookup by name alone found nothing, the
        exception was swallowed, and the heartbeat carried uptime_s: 0.
        """
        original = os.environ.get("PATH", "")
        os.environ["PATH"] = "/usr/bin:/bin"
        try:
            uptime = node.uptime_seconds()
        finally:
            os.environ["PATH"] = original
        self.assertGreater(uptime, 0,
                           "the uptime read 0 — the contract field is broken")

    def test_the_uptime_is_plausible(self):
        uptime = node.uptime_seconds()
        self.assertGreater(uptime, 0)
        # A machine cannot have been up for longer than the epoch.
        self.assertLess(uptime, 10 * 365 * 24 * 3600)


class TheExportRule(unittest.TestCase):
    """The one-writer rule belongs to the contract."""

    def test_the_contract_module_refuses_the_spines_own_record(self):
        allowed, why = node.check_export_target(Path("/tmp/spine/merged.md"))
        self.assertFalse(allowed)
        self.assertIn("one writer", why)

    def test_the_contract_module_allows_an_inbox(self):
        allowed, why = node.check_export_target(Path("/tmp/spine/inbox"))
        self.assertTrue(allowed)
        self.assertEqual(why, "")

    def test_check_export_refuses_the_spines_own_record_when_given_a_target(self):
        block = "<!-- visibility: node-private -->\n\n# a note\n"
        allowed, why = node.check_export(block, Path("/tmp/spine/merged.md"))
        self.assertFalse(allowed)
        self.assertIn("one writer", why)

    def test_check_export_still_allows_a_good_block_and_target(self):
        block = "<!-- visibility: node-private -->\n\n# a note\n"
        allowed, why = node.check_export(block, Path("/tmp/spine/inbox"))
        self.assertTrue(allowed)
        self.assertEqual(why, "node-private")

    def test_an_untagged_block_is_refused_before_the_target_is_looked_at(self):
        allowed, why = node.check_export("# a note\n", Path("/tmp/spine/inbox"))
        self.assertFalse(allowed)
        self.assertIn("visibility marker", why)

    def test_the_tool_delegates_to_the_contract(self):
        """The tool must not have its own copy of the rule."""
        import spine_export
        allowed, why = spine_export.inbox_check(Path("/tmp/spine/merged.md"))
        self.assertFalse(allowed)
        self.assertIn("one writer", why)


class TheDaemon(unittest.TestCase):
    """The daemon writes a heartbeat and nothing else."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        self._tmp.cleanup()

    def run_daemon(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "tools" / "node_daemon.py"), *args],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "HOME": str(self.home)})

    def test_one_beat_is_written_and_nothing_else(self):
        result = self.run_daemon("--once")
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.home / ".local" / "state" / "genesis" / "heartbeat.jsonl"
        self.assertTrue(log.exists(), "no heartbeat was written")
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("seq", lines[0])

    def test_the_sequence_continues_across_restarts(self):
        self.run_daemon("--once")
        self.run_daemon("--once")
        self.run_daemon("--once")
        log = self.home / ".local" / "state" / "genesis" / "heartbeat.jsonl"
        import json
        seqs = [json.loads(line)["seq"] for line in
                log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(seqs, [1, 2, 3], "the sequence is not monotonic")

    def test_the_record_is_not_published(self):
        self.run_daemon("--once")
        record = self.home / ".local" / "state" / "genesis" / "record.md"
        self.assertTrue(record.exists())
        text = record.read_text(encoding="utf-8")
        self.assertIn("visibility: node-private", text)
        self.assertIn("only claim to liveness", text)

    def test_the_heartbeat_carries_no_credential(self):
        self.run_daemon("--once")
        log = self.home / ".local" / "state" / "genesis" / "heartbeat.jsonl"
        text = log.read_text(encoding="utf-8")
        for shape in ("sk-", "tskey-", "API_KEY", "Bearer", "100."):
            self.assertNotIn(shape, text,
                             "the heartbeat carries " + shape)


class TheWizardsJoin(unittest.TestCase):
    """The wizard and the contract must have ONE join, not two.

    `tools/bootstrap.py` used to carry its own copy of the join, and it
    was the weaker one: it built the `tailscale up` command without root
    handling, so on Linux — where `tailscaled` runs as root — the join
    failed with a permission error that reads like a bad key, and the
    operator was given neither the command nor a human. The landing path
    called the contract's join and worked; the wizard — the primary path
    — called the copy and did not.
    """

    def fake_ctx(self, key: Path):
        ctx = type("Ctx", (), {})()
        ctx.tailnet_auth_key_path = str(key)
        return ctx

    def test_the_wizard_and_the_contract_agree_on_a_non_root_linux_box(self):
        import bootstrap

        original_system = node.platform.system
        original_geteuid = node.os.geteuid
        original_sudo = node.sudo_available
        original_which = node.which
        node.platform.system = lambda: "Linux"
        node.os.geteuid = lambda: 1000
        node.sudo_available = lambda: False
        node.which = lambda name: ("/usr/bin/tailscale"
                                   if name == "tailscale" else None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                key = Path(tmp) / "auth.key"
                key.write_text("tskey-not-a-real-key\n")
                joined, detail = bootstrap.join_tailnet(self.fake_ctx(key))
        finally:
            node.platform.system = original_system
            node.os.geteuid = original_geteuid
            node.sudo_available = original_sudo
            node.which = original_which
        self.assertFalse(joined)
        self.assertIn("sudo tailscale up", detail,
                      "the wizard's join did not ask for root")
        self.assertIn("--auth-key=file:", detail)
        self.assertNotIn("permission denied", detail,
                         "the wizard's join still reports the raw permission "
                         "error, which reads like a bad key")

    def test_the_wizard_join_is_the_contract_join(self):
        """One implementation, not two: the wizard calls the contract."""
        import bootstrap

        original = node.join_tailnet
        calls = []

        def spy(key_file):
            calls.append(key_file)
            return {"joined": True, "detail": "joined", "manual": False,
                    "ip": "", "hostname": ""}

        node.join_tailnet = spy
        try:
            with tempfile.TemporaryDirectory() as tmp:
                key = Path(tmp) / "auth.key"
                key.write_text("tskey-not-a-real-key\n")
                joined, detail = bootstrap.join_tailnet(self.fake_ctx(key))
        finally:
            node.join_tailnet = original
        self.assertEqual(len(calls), 1,
                         "the wizard did not call the contract's join")
        self.assertTrue(joined)
        self.assertEqual(detail, "joined")


class OneCopyOfTheMarkerSet(unittest.TestCase):
    """The export gate and the publication lint must agree on the markers.

    `src/node.py` used to spell the marker set out in full while
    `src/visibility.py` owned the same three values. Two spellings of one
    set is how the export gate and the lint drift apart quietly — and the
    export gate is the one that writes.
    """

    def test_the_export_gate_uses_the_visibility_contract_s_set(self):
        import visibility

        self.assertEqual(tuple(node.VISIBILITY_MARKERS),
                         tuple(visibility.KNOWN_VALUES))

    def test_node_does_not_spell_the_set_again(self):
        import visibility

        source = (ROOT / "src" / "node.py").read_text(encoding="utf-8")
        literal = "(" + ", ".join(f'"{v}"'
                                   for v in visibility.KNOWN_VALUES) + ")"
        self.assertNotIn(literal, source,
                         "the marker set is spelled out in src/node.py again "
                         "— it is visibility.KNOWN_VALUES")


if __name__ == "__main__":
    unittest.main()
