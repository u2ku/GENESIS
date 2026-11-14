"""THE CONTRACT MODULE'S SETUP — one copy of the rules, and honest refusals.

The wizard used to carry its own copies of the daemon unit, the enable,
and the install route table. Then the landing path could not reach them
without importing the wizard's privates, and the two would have drifted.

So the rules moved to `src/node.py` — the contract module — and the wizard
calls them. This file pins that:

1. There is ONE copy of the unit text and ONE install route table, and
   they are in the contract module.
2. A unit that is not ours is never overwritten.
3. The install never opens a password prompt nobody asked for.
4. The wizard's node profile does not delete a key it does not own.
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: E402
import hermetic  # noqa: E402
import node  # noqa: E402


class NodeSetupCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.stub_dir = base / "stub-bin"
        self.repo = base / "repo"
        self.home.mkdir(parents=True)
        self.repo.mkdir(parents=True)
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.old_path = hermetic.stub_cli_on_path(self.stub_dir)
        hermetic.stub_system_on_path(self.stub_dir)
        self.addCleanup(self._restore)
        self.addCleanup(self.tmp.cleanup)

    def _restore(self):
        hermetic.restore_path(self.old_path)
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home

    # 1. one copy.

    def test_the_unit_text_lives_in_the_contract_module(self):
        """The wizard does not carry its own daemon unit."""
        text = (ROOT / "tools" / "bootstrap.py").read_text(encoding="utf-8")
        self.assertNotIn("dev.genesis.node", text,
                         "the wizard still carries its own unit")
        self.assertNotIn("genesis-node.service", text,
                         "the wizard still carries its own unit")
        self.assertIn("dev.genesis.node", node.daemon_unit_content("launchd-agent"))

    def test_the_install_route_table_lives_in_the_contract_module(self):
        """One route table: pacman, brew, apt. Never a vendored binary."""
        text = (ROOT / "tools" / "bootstrap.py").read_text(encoding="utf-8")
        for route in ("pacman -S", "brew install", "apt-get install"):
            self.assertNotIn(route, text,
                             f"the wizard still carries its own {route!r}")
        self.assertIn("pacman", " ".join(node.install_command()))
        self.assertNotIn("http", " ".join(node.install_command()))
        self.assertNotIn("curl", " ".join(node.install_command()))

    def test_the_wizard_calls_the_contract_for_the_daemon(self):
        """The wizard writes the unit BY the contract's rule."""
        original = node.install_daemon_unit
        seen = []

        def spy(content, path):
            seen.append((content, Path(path)))
            return {"ok": True, "path": str(path), "reason": "written",
                    "detail": "written"}

        node.install_daemon_unit = spy
        try:
            action = {"id": "node-daemon", "kind": "command",
                      "target": str(self.home / "unit.plist"),
                      "content": "the unit"}
            ok, detail = bootstrap._run_command(bootstrap.Context(), action)
        finally:
            node.install_daemon_unit = original
        self.assertTrue(ok, detail)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "the unit")

    # 2. a unit that is not ours.

    def test_a_different_unit_is_never_overwritten(self):
        """Another tool owns that label. A node does not take it over."""
        unit = self.home / "unit.plist"
        unit.write_text("a different unit entirely", encoding="utf-8")
        result = node.install_daemon_unit("ours", unit)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "conflict")
        self.assertEqual(unit.read_text(encoding="utf-8"),
                         "a different unit entirely",
                         "the unit was overwritten")

    def test_our_own_unit_is_left_alone(self):
        unit = self.home / "unit.plist"
        node.install_daemon_unit("ours", unit)
        result = node.install_daemon_unit("ours", unit)
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "already")

    # 3. the install never prompts.

    def test_the_install_never_prompts(self):
        """sudo is not usable without a password here, so it is a manual step.

        The exact command is handed back instead of a prompt: a password
        prompt nobody asked for is not a step a tool takes.

        Tailscale is NOT on this machine. The hermetic PATH carries a
        tailscale STUB, and a stub answers "already installed" — which is
        a different branch, and a green there says nothing about this one.
        So the probe is answered "not here" directly.
        """
        original = node.tailscale_installed
        node.tailscale_installed = lambda: False
        try:
            result = node.install_tailscale()
        finally:
            node.tailscale_installed = original
        self.assertFalse(result["installed"])
        self.assertTrue(result["manual"])
        self.assertIn("sudo", result["detail"])
        for call in hermetic.system_calls(self.stub_dir):
            self.assertNotIn("pacman", call)
            self.assertNotIn("brew", call)
            self.assertNotIn("apt-get", call)

    # 4. the profile keeps the keys the wizard does not own.

    def test_the_wizard_does_not_delete_a_key_it_does_not_own(self):
        """The node profile is written by more than one thing.

        The wizard owns the keys it sets; the contract owns the tailnet
        record it writes when a join succeeds. A wizard run must not
        delete a key it does not own — or the node silently stops doing
        the thing that key was for.
        """
        path = bootstrap.node_profile_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"node": "imac-01",
                                    "spine": {"target": "x:spine-inbox/"}}),
                        encoding="utf-8")
        ctx = bootstrap.Context()
        ctx.node = "imac-01"
        data = json.loads(bootstrap._node_profile_content(ctx, {}))
        self.assertEqual(data.get("spine"), {"target": "x:spine-inbox/"},
                         "the wizard deleted a key it does not own")


if __name__ == "__main__":
    unittest.main()
