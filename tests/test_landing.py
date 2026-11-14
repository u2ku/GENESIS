"""THE LANDING — the trigger that sets a machine up as a node.

`genesis landing <node>` is the contract's trigger: it is what Genesis
calls to prepare a machine, and what the operator runs by hand. It used to
report readiness and join the tailnet, and nothing else. It now installs
Tailscale, joins the tailnet, writes and starts the heartbeat daemon, and
says what actually happened.

**What this file pins.**

1. The readiness mode changes NOTHING on the machine. Not "less" —
   nothing. The bootstrap's own readiness probe calls the landing in that
   mode, so a probe that installed things would be a probe that changed
   the thing it measured.
2. The aggressive path never reaches the real system: no package
   manager, no `tailscale up`, no live service.
3. The rows carry honest marks, and the exit code is the ladder's.

The stubs are the reason the suite can walk this path at all: `sudo`
fails `sudo -n true`, which is the case the contract turns into a manual
step, and the fake launchd answers from its own record.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
import hermetic  # noqa: E402
import node  # noqa: E402


def digest(root: Path) -> str:
    """A digest of every file under root. The proof that nothing changed."""
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            try:
                entries.append((str(path.relative_to(root)),
                                hashlib.sha256(path.read_bytes()).hexdigest()))
            except OSError:
                entries.append((str(path.relative_to(root)), "?"))
    return hashlib.sha256(repr(entries).encode("utf-8")).hexdigest()


def launchctl_labels() -> set[str]:
    """The labels the REAL launchd is holding, right now.

    The label column, and not the whole table. The table carries a PID
    and a status that belong to the machine rather than to this suite: a
    daemon that restarts inside the window changes both, and a
    comparison of the whole thing cannot tell "we registered a service"
    from "the machine moved" — it fails on the second and means the
    first nothing.

    The dump is tab separated — PID, status, label — and a label with a
    space in it comes back quoted, so the quotes come off.

    The stub is first on PATH, so this asks the real one by absolute
    path. Asking "is there a live job" must not be answered by the fake.
    """
    result = subprocess.run(["/bin/launchctl", "list"],
                            capture_output=True, text=True, timeout=60)
    labels = set()
    for line in result.stdout.splitlines()[1:]:
        columns = line.split("\t")
        if len(columns) >= 3 and columns[2].strip():
            labels.add(columns[2].strip().strip('"'))
    return labels


class LandingCase(unittest.TestCase):
    """A landing, walked against stubs and a temporary home."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.stub_dir = base / "stub-bin"
        self.home.mkdir(parents=True)
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.old_path = hermetic.stub_cli_on_path(self.stub_dir)
        hermetic.stub_system_on_path(self.stub_dir)
        # The contract module resolves its paths at import time, so the
        # ones the setup reads are pointed at the temporary home for the
        # length of the test. This is the pattern `test_node.py` uses.
        self._patched = []
        for name, value in (
                ("STATE_DIR", self.home / ".local" / "state" / "genesis"),
                ("SPINE_EVENTS_LOG", self.home / ".local" / "state" /
                 "genesis" / "spine_events.jsonl"),
                ("CONFIG_DIR", self.home / ".config" / "genesis"),
                ("NODE_PROFILE", self.home / ".config" / "genesis" /
                 "node.json"),
                ("AUTH_KEY_FILE", self.home / ".config" / "luna-node" /
                 "auth.key")):
            self._patched.append((name, getattr(node, name)))
            setattr(node, name, value)
        self.addCleanup(self._restore)
        self.addCleanup(self.tmp.cleanup)

    def _restore(self):
        for name, value in self._patched:
            setattr(node, name, value)
        hermetic.restore_path(self.old_path)
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home

    def cli(self, *args):
        """The real CLI, against the stubs and the temporary home."""
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        env["PATH"] = str(self.stub_dir) + os.pathsep + env.get("PATH", "")
        return subprocess.run([sys.executable, str(ROOT / "genesis"), *args],
                              capture_output=True, text=True, timeout=120,
                              env=env, cwd=str(ROOT))

    # 1. the readiness mode changes nothing.

    def test_the_readiness_probe_changes_nothing(self):
        """The probe reads the machine. It must not touch it."""
        before = digest(self.home)
        result = self.cli("--plain", "landing", str(ROOT), "--readiness-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(digest(self.home), before,
                         "the readiness probe changed the machine")

    def test_join_false_is_inert(self):
        """The tool's own switch does the same: nothing happens."""
        before = digest(self.home)
        text = harness.t_landing({"node": str(ROOT), "join": False})
        self.assertEqual(digest(self.home), before,
                         "a readiness-only landing changed the machine")
        self.assertNotIn("setup — this machine as a node", text)

    # 2. the aggressive path never reaches the real system.

    def test_the_aggressive_path_installs_nothing_real(self):
        """No package manager, and no `tailscale up`."""
        result = self.cli("--plain", "landing", str(ROOT))
        self.assertNotEqual(result.returncode, 0,
                            "the landing claimed a setup it did not do")
        for call in hermetic.system_calls(self.stub_dir):
            self.assertNotIn("brew", call)
            self.assertNotIn("pacman", call)
            self.assertNotIn("apt-get", call)
        for call in hermetic.tailscale_calls(self.stub_dir):
            self.assertNotIn("up", call,
                             "the suite ran `tailscale up` for real")

    def test_the_suite_installs_no_live_service(self):
        """The real launchd is asked, and the answer must not change.

        What this could register is a LABEL, so the labels are what is
        diffed, and only a new one this project owns is the leak. The
        daemon that restarts beside the window is the machine moving,
        and a suite that fails on that is measuring the weather.
        """
        before = launchctl_labels()
        self.cli("--plain", "landing", str(ROOT))
        after = launchctl_labels()
        leaked = sorted(label for label in after - before
                        if label.startswith("dev.genesis.")
                        or "codexify" in label)
        self.assertEqual(leaked, [],
                         f"the suite registered a live service: {leaked}")

    # 3. the rows and the exit code.

    def test_a_landing_does_not_look_like_a_failure(self):
        """The harness treats a result starting with "no " as a failure.

        A partial landing has proved rows, and the model must be allowed
        to report them. So the first word is never "no ".
        """
        text = harness.t_landing({"node": str(ROOT)})
        for prefix in ("no ", "tool error", "no such tool"):
            self.assertFalse(text.startswith(prefix),
                             f"a landing reported itself as a failure: {text[:60]}")

    def test_a_bad_node_path_is_a_failure(self):
        """A path that is not there is a failure, and says so."""
        text = harness.t_landing({"node": "/definitely/not/here"})
        self.assertTrue(text.startswith("no "),
                        f"a missing node was reported as a result: {text[:60]}")

    def test_a_machine_without_the_key_is_told_who_makes_one(self):
        """The wizard owns the secret. Landing never asks for it."""
        text = harness.t_landing({"node": str(ROOT)})
        self.assertIn("wizard", text)
        self.assertIn("·", text)

    def test_the_exit_code_is_the_ladders(self):
        """No key file → 9, the wizard's precondition."""
        result = self.cli("--plain", "landing", str(ROOT))
        self.assertEqual(result.returncode, 9,
                         result.stdout + result.stderr)

    def test_the_readiness_mode_returns_zero_on_a_ready_node(self):
        result = self.cli("--plain", "landing", str(ROOT), "--readiness-only")
        self.assertEqual(result.returncode, 0,
                         result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
