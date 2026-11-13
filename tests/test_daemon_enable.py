"""GENESIS — the daemon-enable path, and the false success inside it.

`launchctl load -w` onto a label that is **already registered** prints
``Load failed: 5: Input/output error`` on stderr and **returns 0**. The
wizard checked only the return code, so it printed ``✓ shim-enable`` for a
service it never installed: the old job kept the label, the plist the
wizard had just written was ignored, the shim never bound its port, and
the readiness probe failed. On a machine with no stale job the very same
code path succeeded, so the defect hid until something had registered the
label first — and the thing that registered it first was **this suite**.

Three things make the enable honest, and these hold all three:

- a load that **failed** is a failure, whatever the exit code said;
- the job left registered is the file the wizard wrote, not a stale one;
- the suite installs **no live service** into the session it runs on.

    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: E402
import hermetic  # noqa: E402

# The REAL launchctl, by absolute path: the stub is first on PATH, and
# asking "is there a live job" must not be answered by the fake.
REAL_LAUNCHCTL = "/bin/launchctl"
LABEL = "dev.genesis.shim"

_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict><key>Label</key><string>dev.genesis.shim</string></dict>
</plist>
"""


def real_job_registered() -> bool:
    """Is the shim's label in the REAL launchd registry?"""
    domain = f"gui/{os.getuid()}/{LABEL}"
    result = subprocess.run([REAL_LAUNCHCTL, "print", domain],
                            capture_output=True, text=True, timeout=60)
    return result.returncode == 0


class DaemonEnableCase(unittest.TestCase):
    """A throwaway home, a throwaway plist, and a launchctl that is a fake."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.home.mkdir()
        self.stub_dir = base / "stub-bin"
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        # The stub carries the CLI, Tailscale and launchctl: the last one
        # so this test cannot install a service even if it wanted to.
        self._old_path = hermetic.stub_cli_on_path(self.stub_dir)
        self.launchctl = self.stub_dir / "launchctl"
        self.plist = (self.home / "Library" / "LaunchAgents"
                      / f"{LABEL}.plist")
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        self.plist.write_text(_PLIST, encoding="utf-8")

    def tearDown(self):
        hermetic.restore_path(self._old_path)
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        self._tmp.cleanup()

    def _stub(self, *args):
        """Run the stub directly, the way the wizard runs it."""
        return subprocess.run([str(self.launchctl), *args],
                              capture_output=True, text=True, timeout=60)

    def _mode(self, mode):
        """Put the stub into one of the launchctl's two lies."""
        os.environ["GENESIS_STUB_LAUNCHCTL_MODE"] = mode
        self.addCleanup(os.environ.pop, "GENESIS_STUB_LAUNCHCTL_MODE", None)

    def test_a_failed_load_is_a_failure_even_though_launchctl_returns_zero(self):
        """THE false success: launchctl says "Load failed" and exits 0.

        A wizard that trusted the exit code here would print
        ``✓ shim-enable`` for a service that was never installed.
        """
        self._mode("fail")
        ok, detail = bootstrap._enable_daemon(str(self.plist))
        self.assertFalse(ok,
                         f"the wizard reported a service it never loaded: "
                         f"{detail!r}")
        self.assertIn("Load failed", detail)

    def test_a_load_that_was_ignored_is_not_reported_as_a_success(self):
        """The load said nothing and changed nothing — that is not a load.

        The label was held by a job from somewhere else, and it could not
        be taken back. The wizard must not call that a success just
        because the command it ran did not complain.
        """
        stale = self.home / "elsewhere" / f"{LABEL}.plist"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(_PLIST, encoding="utf-8")
        self._stub("load", "-w", str(stale))
        self._mode("taken")
        ok, detail = bootstrap._enable_daemon(str(self.plist))
        self.assertFalse(ok,
                         f"the wizard reported a service it never loaded: "
                         f"{detail!r}")
        self.assertIn("did not take", detail)

    def test_a_successful_load_registers_the_file_the_wizard_wrote(self):
        """The other half: the honest path still works.

        A fix that made every enable fail would pass the first test and
        be worthless. The job that ends up registered must be the plist
        the wizard wrote.
        """
        ok, detail = bootstrap._enable_daemon(str(self.plist))
        self.assertTrue(ok, detail)
        printed = self._stub("print", f"gui/{os.getuid()}/{LABEL}")
        self.assertIn(str(self.plist.resolve()), printed.stdout)

    def test_a_stale_job_is_taken_back(self):
        """A label held by an older job is cleared before the new one loads.

        This is the case the suite itself used to cause: every run left a
        job registered, so the next run's load was refused.
        """
        stale = self.home / "elsewhere" / f"{LABEL}.plist"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(_PLIST, encoding="utf-8")
        self._stub("load", "-w", str(stale))
        ok, detail = bootstrap._enable_daemon(str(self.plist))
        self.assertTrue(ok, detail)
        printed = self._stub("print", f"gui/{os.getuid()}/{LABEL}")
        self.assertIn(str(self.plist.resolve()), printed.stdout)
        self.assertNotIn(str(stale.resolve()), printed.stdout)

    def test_the_suite_installs_no_live_service(self):
        """A unit test must never install a service into the user's session.

        Before this, every run of the suite left a **running** LaunchAgent
        registered under this label, pointing at a tmpdir pytest had
        already deleted, holding :8790 open under KeepAlive.
        """
        before = real_job_registered()
        ok, detail = bootstrap._enable_daemon(str(self.plist))
        self.assertTrue(ok, detail)
        self.assertEqual(before, real_job_registered(),
                         "the suite installed a live launchd job")


if __name__ == "__main__":
    unittest.main()
