"""The continuous wizard, tested at its seams.

The wizard's phases are the codexify modules it calls; the tests swap
them for fakes with the same names, drive `_codexify_wizard` directly,
and assert the contract it owes:

- it walks install → setup without exiting between the phases, and
  prints as it goes;
- it finishes on a machine that is done, instead of looping forever;
- it makes the launcher once the stack is up, and only then;
- it names the failed steps and exits when a retry is declined;
- a machine that fails the same way twice is a stop, not a spin;
- a non-interactive run makes no prompts — one pass, the honest code.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import term  # noqa: E402


def load_cli():
    """The CLI is an extensionless executable; load it as a module."""
    loader = importlib.machinery.SourceFileLoader(
        "genesis_cli", str(ROOT / "genesis"))
    spec = importlib.util.spec_from_loader("genesis_cli", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["genesis_cli"] = module
    loader.exec_module(module)
    return module


cli_module = load_cli()


class Row:
    def __init__(self, label, mark, detail=""):
        self.label, self.mark, self.detail = label, mark, detail


class Report:
    def __init__(self, rows=None, exit_code=0, verdict="", heading="FAKE"):
        self.rows = rows or []
        self.exit_code = exit_code
        self.verdict = verdict
        self.heading = heading


OK, BAD, MANUAL = "✓", "✗", "·"
EXIT_OK, EXIT_PARTIAL, EXIT_MISSING, EXIT_PREREQ = 0, 1, 3, 5


class WizardCase(unittest.TestCase):
    """A fake engine, wired into the wizard's own import seams."""

    def setUp(self):
        self.find_install = [None]         # what find_install returns
        self.setup_done = [False]          # what is_setup_done returns
        self.install_exit = [EXIT_OK]      # the install phase's report
        self.setup_exit = [EXIT_OK]        # the setup phase's report
        self.launcher = [None]             # what make_launcher returns
        self.installed_d = SimpleNamespace()   # no-op install object

        codexify = SimpleNamespace(
            EXIT_OK=EXIT_OK, EXIT_PARTIAL=EXIT_PARTIAL,
            EXIT_MISSING_INSTALL=EXIT_MISSING, EXIT_PREREQUISITE=EXIT_PREREQ,
            EXIT_CONFLICT=6, OK=OK, BAD=BAD, MANUAL=MANUAL,
            Row=Row, Report=Report,
            find_install=lambda root, explicit=None: self.find_install[0],
            is_setup_done=lambda install, want_ui=None: self.setup_done[0])

        def fake_install(root, explicit=None, stream=False):
            return Report(exit_code=self.install_exit[0],
                          rows=[Row("the installation", OK, "faked")])

        def fake_setup(install, **kwargs):
            return Report(exit_code=self.setup_exit[0],
                          rows=[Row("the stack", OK, "faked")])

        def fake_launcher(install, where):
            return self.launcher[0]

        self.patches = [
            mock.patch.dict(sys.modules, {
                "codexify": codexify,
                "codexify_install": SimpleNamespace(install=fake_install),
                "codexify_native": SimpleNamespace(setup=fake_setup,
                                                   make_launcher=fake_launcher),
            }),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

        # The wizard's last act is to open a browser, and a test must
        # never do that for real: a window that opens on someone's desk
        # during a test run is the suite being rude. The seam is patched
        # for the whole case, so no test can reach the real `open` by
        # forgetting to — the opener is recorded, and the recording is
        # what the tests assert on.
        self.opened = []
        shutil_patcher = mock.patch.object(cli_module, "shutil")
        subprocess_patcher = mock.patch.object(cli_module, "subprocess")
        self.shutil_mock = shutil_patcher.start()
        self.subprocess_mock = subprocess_patcher.start()
        self.addCleanup(subprocess_patcher.stop)
        self.addCleanup(shutil_patcher.stop)
        self.shutil_mock.which.return_value = "/usr/bin/open"
        self.subprocess_mock.Popen.side_effect = \
            lambda argv, **kw: self.opened.append(argv)

        self.out = io.StringIO()

    def wizard(self, tty=False, answer=None):
        args = SimpleNamespace(python=None, preset=None, model=None,
                               allow_cloud=False, with_ui=None,
                               dry_run=False, launcher=None,
                               no_launcher=False, wizard=True,
                               codexify=None)
        ctx = SimpleNamespace(style=term.make_style(plain=True))
        if answer is not None:
            sys.stdin = io.StringIO(answer + "\n")
            self.addCleanup(lambda: setattr(sys, "stdin", sys.__stdin__))
        with mock.patch("builtins.print"):
            pass  # print goes to the real stdout inside emit; silence is
        code = None
        with mock.patch.object(cli_module, "emit",
                               side_effect=lambda text: self.out.write(text)):
            with mock.patch.object(cli_module, "_wizard_retry",
                                   wraps=cli_module._wizard_retry):
                code = cli_module._codexify_wizard(
                    args, ctx, sys.modules["codexify"], None)
        return code


class TestTheFlow(WizardCase):

    def test_the_empty_machine_walks_install_then_setup(self):
        """No install: phase 1 runs, then phase 2, without exiting."""
        state = {"found": False}

        def fake_find(root, explicit=None):
            # None until the install phase has run, a path after.
            return self.installed_d if state["found"] else None

        def fake_install(root, explicit=None, stream=False):
            state["found"] = True
            return Report(exit_code=EXIT_OK,
                          rows=[Row("the installation", OK, "faked")])

        def fake_setup(install, **kwargs):
            self.setup_done[0] = True
            return Report(exit_code=EXIT_OK, rows=[Row("the stack", OK, "up")])

        def fake_launcher(install, where):
            return True, "~/Desktop/Codexify.app"

        sys.modules["codexify"].find_install = fake_find
        sys.modules["codexify_install"].install = fake_install
        sys.modules["codexify_native"].setup = fake_setup
        sys.modules["codexify_native"].make_launcher = fake_launcher

        captured = io.StringIO()
        with mock.patch("sys.stdout", captured):
            code = self.wizard()
        self.assertEqual(code, EXIT_OK)
        # Both halves ran, in order: the install phase, then setup.
        self.assertIn("phase 2", captured.getvalue())
        self.assertIn("the installation", self.out.getvalue())
        self.assertIn("the stack", self.out.getvalue())

    def test_a_done_machine_finishes_without_looping(self):
        self.find_install[0] = self.installed_d
        self.setup_done[0] = True
        calls = []
        sys.modules["codexify_install"].install = \
            lambda root, explicit=None, stream=False: (
                calls.append("install"), Report(exit_code=EXIT_OK))[-1]
        code = self.wizard()
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(calls, [])             # nothing ran; it was done

    def test_the_launcher_is_made_once_the_stack_is_up(self):
        self.find_install[0] = self.installed_d
        self.launcher[0] = (True, "~/Desktop/Codexify.app")

        def fake_setup(install, **kwargs):
            self.setup_done[0] = True
            return Report(exit_code=EXIT_OK, rows=[Row("the stack", OK, "up")])
        sys.modules["codexify_native"].setup = fake_setup

        code = self.wizard()
        self.assertEqual(code, EXIT_OK)
        self.assertIn("launcher", self.out.getvalue())

    def test_no_launcher_when_the_operator_said_no(self):
        self.find_install[0] = self.installed_d
        self.launcher[0] = (True, "~/Desktop/Codexify.app")

        def fake_setup(install, **kwargs):
            self.setup_done[0] = True
            self.assertEqual(kwargs.get("launcher"), None)
            return Report(exit_code=EXIT_OK, rows=[Row("the stack", OK, "up")])
        sys.modules["codexify_native"].setup = fake_setup

        args = SimpleNamespace(python=None, preset=None, model=None,
                               allow_cloud=False, with_ui=False,
                               dry_run=False, launcher=None,
                               no_launcher=True, wizard=True,
                               codexify=None)
        ctx = SimpleNamespace(style=term.make_style(plain=True))
        with mock.patch.object(cli_module, "emit",
                               side_effect=lambda t: self.out.write(t)):
            code = cli_module._codexify_wizard(
                args, ctx, sys.modules["codexify"], None)
        self.assertEqual(code, EXIT_OK)

    def test_a_failed_setup_names_the_steps_and_exits_on_decline(self):
        self.find_install[0] = self.installed_d
        self.setup_exit[0] = EXIT_PARTIAL
        sys.modules["codexify_native"].setup = (
            lambda install, **kwargs: Report(
                exit_code=EXIT_PARTIAL,
                rows=[Row("the database", BAD, "refused")]))
        with mock.patch.object(cli_module, "_wizard_retry",
                               return_value=False):
            code = self.wizard()
        self.assertEqual(code, EXIT_PARTIAL)

    def test_the_same_failure_twice_is_a_stop_not_a_spin(self):
        self.find_install[0] = self.installed_d
        sys.modules["codexify_native"].setup = (
            lambda install, **kwargs: Report(
                exit_code=EXIT_PARTIAL,
                rows=[Row("the database", BAD, "refused")]))
        captured = io.StringIO()
        with mock.patch.object(cli_module, "_wizard_retry",
                               return_value=True):    # always retry
            with mock.patch("sys.stdout", captured):
                code = self.wizard()
        self.assertEqual(code, EXIT_PARTIAL)
        self.assertIn("twice", captured.getvalue())


class TestTheFinish(WizardCase):
    """The wizard's last act: open what the stack is serving."""

    def test_the_wizard_starts_the_interface_by_default(self):
        """The browser opens the interface, so the interface runs."""
        self.find_install[0] = self.installed_d
        self.setup_done[0] = False          # phase 2 must run
        seen = {}

        def fake_setup(install, **kwargs):
            self.setup_done[0] = True
            seen.update(kwargs)
            return Report(exit_code=EXIT_OK, rows=[Row("the stack", OK, "up")])

        sys.modules["codexify_native"].setup = fake_setup
        sys.modules["codexify_native"].make_launcher = (
            lambda install, where: (True, "~/Desktop/Codexify.app"))
        with mock.patch("sys.stdout", io.StringIO()):
            self.wizard()
        self.assertTrue(seen.get("with_ui"))
        self.assertEqual(self.opened,
                         [["/usr/bin/open", "http://127.0.0.1:5173"]])

    def test_no_ui_means_the_api_opens(self):
        self.find_install[0] = self.installed_d
        self.setup_done[0] = True
        args = SimpleNamespace(python=None, preset=None, model=None,
                               allow_cloud=False, with_ui=None, no_ui=True,
                               dry_run=False, launcher=None,
                               no_launcher=False, wizard=True,
                               codexify=None)
        ctx = SimpleNamespace(style=term.make_style(plain=True))
        with mock.patch.object(cli_module, "emit",
                               side_effect=lambda t: None):
            with mock.patch("sys.stdout", io.StringIO()):
                cli_module._codexify_wizard(
                    args, ctx, sys.modules["codexify"], None)
        self.assertEqual(self.opened,
                         [["/usr/bin/open", "http://127.0.0.1:8888"]])
    """The wizard's last act: open what the stack is serving."""

    def test_the_wizard_opens_the_interface_when_it_finishes(self):
        self.find_install[0] = self.installed_d
        self.setup_done[0] = True
        with mock.patch("sys.stdout", io.StringIO()):
            code = self.wizard()
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.opened,
                         [["/usr/bin/open", "http://127.0.0.1:5173"]])

    def test_the_interface_opens_with_ui(self):
        self.find_install[0] = self.installed_d
        self.setup_done[0] = True
        args = SimpleNamespace(python=None, preset=None, model=None,
                               allow_cloud=False, with_ui=True,
                               dry_run=False, launcher=None,
                               no_launcher=False, wizard=True,
                               codexify=None)
        ctx = SimpleNamespace(style=term.make_style(plain=True))
        with mock.patch.object(cli_module, "emit",
                               side_effect=lambda t: None):
            with mock.patch("sys.stdout", io.StringIO()):
                code = cli_module._codexify_wizard(
                    args, ctx, sys.modules["codexify"], None)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.opened,
                         [["/usr/bin/open", "http://127.0.0.1:5173"]])

    def test_no_opener_means_the_url_is_printed(self):
        self.find_install[0] = self.installed_d
        self.setup_done[0] = True
        captured = io.StringIO()
        self.shutil_mock.which.return_value = None
        with mock.patch("sys.stdout", captured):
            self.wizard()
        self.assertIn("http://127.0.0.1:5173", captured.getvalue())
        self.assertEqual(self.opened, [])

    def test_the_suite_never_opens_a_real_browser(self):
        """A window on someone's desk is the suite being rude.

        The wizard's last act really does open a browser — that is what
        it is for — so the seam is what is patched, for every test in
        the case, and what is recorded is the command that would have
        run.
        """
        self.find_install[0] = self.installed_d
        self.setup_done[0] = True
        with mock.patch("sys.stdout", io.StringIO()):
            self.wizard()
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(self.opened[0][0], "/usr/bin/open")


class TestTheRetryPrompt(WizardCase):

    def test_a_failing_report_prompts_in_a_tty(self):
        report = Report(exit_code=EXIT_PREREQ,
                        rows=[Row("the fetch", BAD, "no network")])
        with mock.patch("sys.stdin") as fake_stdin:
            fake_stdin.isatty.return_value = True
            fake_stdin.readline.return_value = "\n"    # default: retry
            # input() reads via readline on a mocked stdin
            with mock.patch("builtins.input", return_value=""):
                self.assertTrue(cli_module._wizard_retry(report))

    def test_no_prompt_when_piped(self):
        report = Report(exit_code=EXIT_PREREQ,
                        rows=[Row("the fetch", BAD, "no network")])
        self.assertFalse(cli_module._wizard_retry(report))


if __name__ == "__main__":
    unittest.main()
