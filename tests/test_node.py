"""GENESIS — the node contract's tests.

The contract is four rules, and each one is checked here:

- **the heartbeat claims liveness and nothing else** — fixed fields, a
  monotonic sequence, no credential, and no tailnet address (a `100.x` in
  a published record is a private-network address by the visibility
  contract's own reading);
- **the auth key never enters an argument** — `tailscale up` is given a
  *path*, and the path is the only thing in the command;
- **the export is one way and deliberate** — an untagged block is refused,
  and the spine's own record is not a target;
- **a dead tailnet is reported, not continued past** — a machine with no
  Tailscale gets a command and a human, not a silent `sudo`.

    python3 -m unittest discover -s tests -v
    python3 tests/test_node.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import node  # noqa: E402
import spine_export  # noqa: E402


class NodeCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self._saved = (node.STATE_DIR, node.CONFIG_DIR, node.NODE_DIR,
                       node.HEARTBEAT_LOG, node.NODE_RECORD, node.NODE_PROFILE,
                       node.AUTH_KEY_FILE)
        node.STATE_DIR = self.home / ".local" / "state" / "genesis"
        node.CONFIG_DIR = self.home / ".config" / "genesis"
        node.NODE_DIR = self.home / ".config" / "luna-node"
        node.HEARTBEAT_LOG = node.STATE_DIR / "heartbeat.jsonl"
        node.NODE_RECORD = node.STATE_DIR / "record.md"
        node.NODE_PROFILE = node.CONFIG_DIR / "node.json"
        node.AUTH_KEY_FILE = node.NODE_DIR / "auth.key"

    def tearDown(self):
        (node.STATE_DIR, node.CONFIG_DIR, node.NODE_DIR, node.HEARTBEAT_LOG,
         node.NODE_RECORD, node.NODE_PROFILE, node.AUTH_KEY_FILE) = self._saved
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        self._tmp.cleanup()


class TestTheHeartbeat(NodeCase):

    def test_it_carries_the_contract_fields(self):
        payload = node.heartbeat_payload(7)
        for field in ("seq", "node", "hostname", "os", "uptime_s", "services",
                      "ts"):
            self.assertIn(field, payload)
        self.assertEqual(payload["seq"], 7)
        self.assertIn("tailscaled", payload["services"])

    def test_it_carries_no_credential(self):
        """No environment dump, no key, no user file contents."""
        os.environ["A_SECRET_THAT_MUST_NOT_LEAK"] = "sk-ant-supersecret"
        payload = node.heartbeat_payload(1)
        text = json.dumps(payload)
        self.assertNotIn("sk-ant-supersecret", text)
        self.assertNotIn("A_SECRET_THAT_MUST_NOT_LEAK", text)

    def test_it_carries_no_tailnet_address(self):
        """A `100.x` address in a record is a private-network address."""
        payload = node.heartbeat_payload(1)
        text = json.dumps(payload)
        self.assertNotIn("100.", text)
        self.assertNotIn(node.tailscale_status().get("ip", "") or "~~", text)

    def test_the_sequence_is_monotonic(self):
        first = node.heartbeat_payload(1)["seq"]
        second = node.heartbeat_payload(2)["seq"]
        self.assertGreater(second, first)

    def test_it_is_written_to_the_node_local_record(self):
        node.write_heartbeat(node.heartbeat_payload(1))
        node.write_heartbeat(node.heartbeat_payload(2))
        lines = node.HEARTBEAT_LOG.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[-1])["seq"], 2)
        record = node.NODE_RECORD.read_text(encoding="utf-8")
        self.assertIn("visibility: node-private", record)
        self.assertIn("silent node is a dead node", record)


class TestTheJoin(NodeCase):

    def test_a_machine_without_tailscale_gets_a_command_and_a_human(self):
        """No silent sudo behind the operator's back."""
        original = node.which
        node.which = lambda name: None
        try:
            result = node.join_tailnet()
        finally:
            node.which = original
        self.assertFalse(result["joined"])
        self.assertTrue(result["manual"])
        self.assertIn("not installed", result["detail"])

    def test_a_machine_without_a_key_file_is_told_who_makes_one(self):
        original_which = node.which
        original_run = node.subprocess.run
        node.which = lambda name: "/usr/bin/tailscale" if name == "tailscale" else None
        node.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("it must not run tailscale without a key"))
        try:
            result = node.join_tailnet()
        finally:
            node.which = original_which
            node.subprocess.run = original_run
        self.assertFalse(result["joined"])
        self.assertTrue(result["manual"])
        self.assertIn("wizard", result["detail"])

    def test_the_key_is_passed_as_a_path_never_as_a_value(self):
        """`--auth-key=file:<path>` — a path in the argument, not the secret."""
        node.AUTH_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        node.AUTH_KEY_FILE.write_text("tskey-auth-secret-value\n")
        os.chmod(node.AUTH_KEY_FILE, 0o600)

        captured = []

        class Result:
            returncode = 0
            stdout = "{}"
            stderr = ""

        def fake_run(command, **kwargs):
            captured.append(command)
            return Result()

        original_which = node.which
        original_run = node.subprocess.run
        node.which = lambda name: "/usr/bin/tailscale" if name == "tailscale" else None
        node.subprocess.run = fake_run
        try:
            node.join_tailnet()
        finally:
            node.which = original_which
            node.subprocess.run = original_run

        up = [c for c in captured if "up" in c]
        self.assertTrue(up, "tailscale up was never run")
        command = up[0]
        self.assertIn("--auth-key=file:" + str(node.AUTH_KEY_FILE), command)
        self.assertNotIn("tskey-auth-secret-value", " ".join(command))


class TestTheExport(NodeCase):

    def test_an_untagged_block_is_refused(self):
        """Untagged content is readable as written, so it is refused."""
        allowed, why = node.check_export("# a note\n\nno marker here\n")
        self.assertFalse(allowed)
        self.assertIn("visibility marker", why)

    def test_a_tagged_block_is_allowed(self):
        allowed, why = node.check_export(
            "<!-- visibility: node-private -->\n\n# a note\n")
        self.assertTrue(allowed)
        self.assertEqual(why, "node-private")

    def test_an_unknown_marker_is_refused(self):
        allowed, why = node.check_export(
            "<!-- visibility: whoever -->\n\n# a note\n")
        self.assertFalse(allowed)
        self.assertIn("unknown visibility marker", why)

    def test_the_marker_must_be_in_the_header(self):
        """A marker buried in the body is prose, not a declaration."""
        buried = "# a note\n" * 12 + "\n<!-- visibility: node-private -->\n"
        allowed, why = node.check_export(buried)
        self.assertFalse(allowed)


class TestTheArchBranch(NodeCase):
    """The Omarchy/Arch branch, exercised without an Omarchy machine.

    The live guest is not always switched on, and the contract is that it
    does not need to be: the branch is decided by the *signals*, and the
    signals are what these tests fake. What they cannot fake is the
    `pacman` call and the `systemctl --user enable` — those need the
    guest, and the guest is not here.
    """

    def test_the_install_route_is_the_system_package_manager(self):
        original = node.which
        node.which = lambda name: "/usr/bin/pacman" if name == "pacman" else None
        try:
            self.assertEqual(node.package_manager(), "pacman")
            command = node.install_command()
        finally:
            node.which = original
        self.assertEqual(command[0], "sudo")
        self.assertIn("pacman", command)
        self.assertIn("tailscale", command)

    def test_the_install_route_is_never_a_vendored_binary(self):
        """No binary in the repo, no curl-pipe-sh."""
        original = node.which
        node.which = lambda name: "/usr/bin/pacman" if name == "pacman" else None
        try:
            command = node.install_command()
        finally:
            node.which = original
        text = " ".join(command)
        self.assertNotIn("http", text)
        self.assertNotIn("curl", text)
        self.assertNotIn("sh", " ".join(c for c in command if c != "sh"))

    def test_the_brew_route_is_a_cask_and_the_apt_route_is_a_package(self):
        original = node.which
        node.which = lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None
        try:
            self.assertEqual(node.install_command(),
                             ["brew", "install", "--cask", "tailscale"])
            node.which = lambda name: "/usr/bin/apt-get" if name == "apt-get" else None
            self.assertEqual(node.install_command(),
                             ["sudo", "apt-get", "install", "-y", "tailscale"])
        finally:
            node.which = original

    def test_no_package_manager_means_no_command(self):
        original = node.which
        node.which = lambda name: None
        try:
            self.assertEqual(node.install_command(), [])
        finally:
            node.which = original


class TestTheExportTarget(NodeCase):

    def test_the_spines_own_record_is_refused(self):
        """The spine has exactly one writer, and it is the spine."""
        allowed, why = spine_export.inbox_check(Path("/tmp/spine/merged.md"))
        self.assertFalse(allowed)
        self.assertIn("one writer", why)

    def test_an_inbox_is_allowed(self):
        allowed, why = spine_export.inbox_check(Path("/tmp/spine/inbox"))
        self.assertTrue(allowed)
        self.assertEqual(why, "")

    def test_a_path_that_is_not_an_inbox_is_allowed_with_a_warning(self):
        allowed, why = spine_export.inbox_check(Path("/tmp/somewhere"))
        self.assertTrue(allowed)
        self.assertIn("inbox", why)

    def test_a_pushed_block_lands_where_it_was_asked_to(self):
        block = ("<!-- visibility: node-private -->\n\n# a node note\n")
        inbox = self.home / "spine-inbox"
        destination = spine_export.push_local(block, inbox, "node-private")
        self.assertTrue(Path(destination).exists())
        self.assertIn("a node note", Path(destination).read_text())


class TestTheJoinNeedsRoot(NodeCase):
    """F5: `tailscale up` needs root on Linux, and says so when it cannot get it."""

    def test_on_linux_a_non_root_join_runs_with_sudo(self):
        original_system = node.platform.system
        original_geteuid = node.os.geteuid
        node.platform.system = lambda: "Linux"
        node.os.geteuid = lambda: 1000
        try:
            self.assertTrue(node.needs_root())
            command = node.tailscale_up_command(Path("/tmp/auth.key"))
            self.assertEqual(command[0], "sudo")
            self.assertIn("--auth-key=file:/tmp/auth.key", command)
        finally:
            node.platform.system = original_system
            node.os.geteuid = original_geteuid

    def test_on_linux_a_root_join_needs_no_sudo(self):
        original_system = node.platform.system
        original_geteuid = node.os.geteuid
        node.platform.system = lambda: "Linux"
        node.os.geteuid = lambda: 0
        try:
            self.assertFalse(node.needs_root())
            self.assertEqual(node.tailscale_up_command(Path("/tmp/auth.key"))[0],
                             "tailscale")
        finally:
            node.platform.system = original_system
            node.os.geteuid = original_geteuid

    def test_on_macos_no_root_is_needed(self):
        original_system = node.platform.system
        node.platform.system = lambda: "Darwin"
        try:
            self.assertFalse(node.needs_root())
            self.assertEqual(node.tailscale_up_command(Path("/tmp/auth.key"))[0],
                             "tailscale")
        finally:
            node.platform.system = original_system

    def test_the_key_is_still_a_path_with_root(self):
        original_system = node.platform.system
        original_geteuid = node.os.geteuid
        node.platform.system = lambda: "Linux"
        node.os.geteuid = lambda: 1000
        try:
            command = node.tailscale_up_command(Path("/tmp/auth.key"))
            self.assertNotIn("tskey-secretvalue", " ".join(command))
            self.assertIn("--auth-key=file:/tmp/auth.key", command)
        finally:
            node.platform.system = original_system
            node.os.geteuid = original_geteuid

    def test_without_sudo_the_join_is_a_manual_step(self):
        """A password prompt nobody asked for is not a step the tool takes."""
        original_system = node.platform.system
        original_geteuid = node.os.geteuid
        original_sudo = node.sudo_available
        original_which = node.which
        node.platform.system = lambda: "Linux"
        node.os.geteuid = lambda: 1000
        node.sudo_available = lambda: False
        node.which = lambda name: "/usr/bin/tailscale" if name == "tailscale" else None
        try:
            node.AUTH_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
            node.AUTH_KEY_FILE.write_text("tskey-secretvalue\n")
            result = node.join_tailnet()
        finally:
            node.platform.system = original_system
            node.os.geteuid = original_geteuid
            node.sudo_available = original_sudo
            node.which = original_which
        self.assertFalse(result["joined"])
        self.assertTrue(result["manual"])
        self.assertIn("sudo", result["detail"])


class TestTheSequenceIsMonotonic(NodeCase):
    """F6: the heartbeat sequence continues across restarts."""

    def test_the_sequence_comes_from_the_log(self):
        node.HEARTBEAT_LOG.parent.mkdir(parents=True, exist_ok=True)
        node.HEARTBEAT_LOG.write_text(
            '{"seq": 41, "node": "n"}\n{"seq": 42, "node": "n"}\n')
        self.assertEqual(node.last_sequence(), 42)

    def test_an_empty_log_starts_at_zero(self):
        self.assertEqual(node.last_sequence(), 0)

    def test_a_corrupt_line_does_not_stop_the_read(self):
        node.HEARTBEAT_LOG.parent.mkdir(parents=True, exist_ok=True)
        node.HEARTBEAT_LOG.write_text(
            '{"seq": 7}\nnot json at all\n{"seq": 9}\n')
        self.assertEqual(node.last_sequence(), 9)

    def test_the_daemon_continues_where_the_log_left_off(self):
        node.HEARTBEAT_LOG.parent.mkdir(parents=True, exist_ok=True)
        node.HEARTBEAT_LOG.write_text('{"seq": 12, "node": "n"}\n')
        import node_daemon
        payload = node_daemon.run_once(node.last_sequence() + 1)
        self.assertEqual(payload["seq"], 13)


if __name__ == "__main__":
    unittest.main(verbosity=2)
