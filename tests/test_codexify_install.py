"""The install and update verbs, tested without a network.

The runner is faked, the way the rest of the codexify suites fake it: a
git that answers what the test wants it to answer, so the branch logic —
the refusals, the convergences, the movement — is what is tested, and
nothing ever reaches upstream.

The launcher tests patch `Path.home` so the "Desktop" is a throwaway
directory, and patch the genesis path the launcher script embeds, so the
bundle the test makes is the bundle the machine gets — just pointed at
somewhere disposable.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))

import codexify  # noqa: E402
import codexify_install as ci  # noqa: E402
import codexify_native as native  # noqa: E402


SUBMODULE_UPDATE = ("submodule", "update", "--init", "--recursive",
                    "codexify-core")


class GitAnswers:
    """A fake git keyed by the argument tuple after `git -C <repo>`."""

    def __init__(self, answers=None, fail=(), make_install=None):
        self.answers = answers or {}
        self.fail = set(fail)
        self.make_install = make_install
        self.calls = []

    def __call__(self, args, cwd=None, timeout=300.0, env=None,
                 stream=False):
        key = tuple(args[3:])
        self.calls.append(key)
        if self.make_install and key == SUBMODULE_UPDATE:
            self.make_install()
        if key in self.fail:
            return subprocess.CompletedProcess(
                args, 1, "", f"failed: {' '.join(key)}")
        if key in self.answers:
            rc, out = self.answers[key]
            return subprocess.CompletedProcess(args, rc, out, "")
        return subprocess.CompletedProcess(args, 0, "", "")


class RepoCase(unittest.TestCase):
    """A throwaway repository with a registered submodule slot."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.repo = base / "repo"
        self.sub = self.repo / "codexify-core"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        (self.repo / ".gitmodules").write_text(
            '[submodule "codexify-core"]\n'
            "\tpath = codexify-core\n"
            "\turl = https://example.invalid/Codexify-Core.git\n",
            encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def make_install(self):
        """The submodule directory, as a real install."""
        self.sub.mkdir(exist_ok=True)
        (self.sub / "docker-compose.yml").write_text("", encoding="utf-8")
        (self.sub / ".env.template").write_text("", encoding="utf-8")

    def git_ok(self, answers=None, fail=(), make_install=None):
        """The runner for a machine where the checkout comes back real."""
        merged = {
            ("config", "--file", ".gitmodules",
             "submodule.codexify-core.url"):
                (0, "https://example.invalid/Codexify-Core.git\n"),
            ("rev-parse", "--short", "HEAD"): (0, "abc1234\n"),
            ("rev-parse", "--short", "origin/main"): (0, "abc1234\n"),
        }
        merged.update(answers or {})
        return GitAnswers(merged, fail=fail,
                          make_install=make_install or self.make_install)


class InstallCase(RepoCase):

    def test_install_on_an_empty_machine(self):
        """The whole point: no directory, and the rows come back green."""
        report = ci.install(self.repo, runner=self.git_ok())
        self.assertEqual(report.exit_code, codexify.EXIT_OK)
        self.assertTrue(any(row.label == "the installation"
                            and row.mark == codexify.OK
                            for row in report.rows))

    def test_install_without_git_is_a_prerequisite(self):
        """A directory that is not a checkout has no submodule machinery."""
        (self.repo / ".git").rmdir()
        report = ci.install(self.repo, runner=GitAnswers())
        self.assertEqual(report.exit_code, codexify.EXIT_PREREQUISITE)
        self.assertIn("nothing was installed", report.verdict)

    def test_install_without_the_registration_is_a_prerequisite(self):
        (self.repo / ".gitmodules").unlink()
        report = ci.install(self.repo, runner=GitAnswers())
        self.assertEqual(report.exit_code, codexify.EXIT_PREREQUISITE)

    def test_install_reports_a_failed_fetch(self):
        """The network is a prerequisite, and its failure says so."""
        runner = self.git_ok(fail={("submodule", "foreach",
                                   "git fetch origin main"),
                                   ("fetch", "origin", "main")})
        report = ci.install(self.repo, runner=runner)
        self.assertEqual(report.exit_code, codexify.EXIT_PREREQUISITE)
        self.assertTrue(any("upstream" in row.detail for row in report.rows))

    def test_install_moves_to_the_latest(self):
        runner = self.git_ok(answers={
            ("rev-parse", "--short", "HEAD"): (0, "abc1234\n"),
            ("rev-parse", "--short", "origin/main"): (0, "def5678\n"),
        })
        report = ci.install(self.repo, runner=runner)
        self.assertEqual(report.exit_code, codexify.EXIT_OK)
        self.assertTrue(any("abc1234 → def5678" in row.detail
                            for row in report.rows))

    def test_install_converges_when_already_latest(self):
        """Same commit both sides: green rows, no move reported."""
        report = ci.install(self.repo, runner=self.git_ok())
        self.assertEqual(report.exit_code, codexify.EXIT_OK)
        self.assertFalse(any("→" in row.detail for row in report.rows))

    def test_install_refuses_a_directory_that_is_not_codexify(self):
        """git succeeds, the directory appears, and it is not an install."""
        runner = self.git_ok(make_install=lambda: self.sub.mkdir(exist_ok=True))
        report = ci.install(self.repo, runner=runner)
        self.assertEqual(report.exit_code, codexify.EXIT_CONFLICT)


class VendoredFixCase(RepoCase):
    """The fixes the native path needs, applied to a fresh checkout.

    A fix that lives inside the submodule dies with the next fetch — the
    submodule is upstream's, and install/update overwrite it wholesale.
    So the fixes live in codexify_install and are re-applied after every
    checkout, and these tests prove the re-application works on the code
    upstream actually ships.
    """

    def write_fixture(self, fixed=False):
        """Upstream's files, or the fixed ones: pgdb.py and the hook."""
        self.sub.mkdir(exist_ok=True)
        fixed_pgdb = '''    def table_exists(self, table_name: str) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT to_regclass(%s) AS reg",
                    (f"public.{table_name}",),
                )
                row = cur.fetchone()
                # dict_row rows need the alias, not an index
                return row["reg"] is not None if row else False
'''
        broken_pgdb = fixed_pgdb.replace(" AS reg", "").replace(
            '                # dict_row rows need the alias, not an index\n'
            '                return row["reg"] is not None if row else False',
            "                return row[0] is not None if row else False")
        guardian_core = self.sub / "guardian" / "core"
        guardian_core.mkdir(parents=True, exist_ok=True)
        (guardian_core / "pgdb.py").write_text(
            fixed_pgdb if fixed else broken_pgdb, encoding="utf-8")

        fixed_hook = (
            "  const [mode, setMode] = useState<ThemeMode>(() => {\n"
            '    if (typeof window === "undefined") return "system";\n'
            '    const raw = window.localStorage.getItem("cfy.themeMode");\n'
            "    if (raw == null) {\n"
            "      const fallback = (import.meta.env.VITE_THEME_DEFAULT "
            "as string) || \"\";\n"
            "      return coerceMode(fallback);\n"
            "    }\n"
            "    return coerceMode(raw);\n"
            "  });\n")
        broken_hook = (
            "  const [mode, setMode] = useState<ThemeMode>(() => {\n"
            '    if (typeof window === "undefined") return "system";\n'
            '    const raw = window.localStorage.getItem("cfy.themeMode");\n'
            "    return coerceMode(raw);\n"
            "  });\n")
        hook = self.sub / "frontend" / "src" / "hooks"
        hook.mkdir(parents=True, exist_ok=True)
        (hook / "useThemeController.ts").write_text(
            fixed_hook if fixed else broken_hook, encoding="utf-8")

    def pgdb(self, fixed=False):
        """The old name, kept for the tests that only care about pgdb."""
        self.write_fixture(fixed=fixed)

    def test_the_fix_is_applied_to_upstream_code(self):
        """The exact code upstream ships gets the dict_row fix."""
        self.pgdb(fixed=False)
        ok, note = ci.apply_vendored_fixes(self.sub)
        self.assertTrue(ok, note)
        text = (self.sub / "guardian" / "core" / "pgdb.py").read_text()
        self.assertIn('row["reg"]', text)

    def test_the_fix_is_idempotent(self):
        self.pgdb(fixed=True)
        ok, note = ci.apply_vendored_fixes(self.sub)
        self.assertTrue(ok)
        self.assertIn("already", note)

    def test_a_missing_file_is_reported_not_crashed(self):
        self.sub.mkdir(exist_ok=True)
        ok, note = ci.apply_vendored_fixes(self.sub)
        self.assertFalse(ok)
        self.assertIn("not there", note)

    def test_the_theme_default_fix_applies_to_the_hook(self):
        """The theme hook learns to honour VITE_THEME_DEFAULT as the
        fallback default, and localStorage still wins once set."""
        self.sub.mkdir(exist_ok=True)
        hook = self.sub / "frontend" / "src" / "hooks"
        hook.mkdir(parents=True)
        (hook / "useThemeController.ts").write_text(
            "  const [mode, setMode] = useState<ThemeMode>(() => {\n"
            '    if (typeof window === "undefined") return "system";\n'
            '    const raw = window.localStorage.getItem("cfy.themeMode");\n'
            "    return coerceMode(raw);\n"
            "  });\n", encoding="utf-8")
        ok, note = ci.apply_vendored_fixes(self.sub)
        self.assertTrue(ok, note)
        text = (hook / "useThemeController.ts").read_text()
        self.assertIn("VITE_THEME_DEFAULT", text)
        # localStorage still wins: the fallback only fires when raw is null
        self.assertIn("if (raw == null)", text)

    def test_changed_upstream_code_is_reported_not_forced(self):
        """If upstream rewrote the function, the fix says so instead of
        guessing where to splice."""
        self.sub.mkdir(exist_ok=True)
        (self.sub / "guardian" / "core").mkdir(parents=True)
        (self.sub / "guardian" / "core" / "pgdb.py").write_text(
            "def entirely_different(): pass\n", encoding="utf-8")
        ok, note = ci.apply_vendored_fixes(self.sub)
        self.assertFalse(ok)
        self.assertIn("not there", note)


class UpdateCase(RepoCase):

    def test_update_without_an_install_is_missing(self):
        report = ci.update(self.repo, runner=GitAnswers())
        self.assertEqual(report.exit_code, codexify.EXIT_MISSING_INSTALL)

    def test_update_refuses_a_dirty_tree(self):
        self.make_install()
        runner = self.git_ok(answers={
            ("status", "--porcelain"): (0, " M guardian/x.py\n"),
        })
        report = ci.update(self.repo, runner=runner)
        self.assertEqual(report.exit_code, codexify.EXIT_CONFLICT)
        self.assertIn("local changes", report.verdict)

    def test_update_moves_to_the_latest(self):
        self.make_install()
        runner = self.git_ok(answers={
            ("rev-parse", "--short", "HEAD"): (0, "abc1234\n"),
            ("rev-parse", "--short", "origin/main"): (0, "def5678\n"),
        })
        report = ci.update(self.repo, runner=runner)
        self.assertEqual(report.exit_code, codexify.EXIT_OK)
        self.assertTrue(any("abc1234 → def5678" in row.detail
                            for row in report.rows))

    def test_update_converges_when_already_latest(self):
        self.make_install()
        report = ci.update(self.repo, runner=self.git_ok())
        self.assertEqual(report.exit_code, codexify.EXIT_OK)


class LauncherCase(unittest.TestCase):
    """The launcher bundle, made into a throwaway 'Desktop'."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.install_dir = self.base / "install"
        self.install_dir.mkdir()
        # The fake home: `make_launcher`'s Desktop branch uses Path.home().
        self.fake_home = self.base / "home"
        (self.fake_home / "Desktop").mkdir(parents=True)
        # A fake genesis binary the launcher can embed.
        self.genesis = self.base / "bin" / "genesis"
        self.genesis.parent.mkdir()
        self.genesis.write_text("#!/bin/sh\n", encoding="utf-8")
        self.genesis.chmod(0o755)
        self._real_path = native.Path

        class FakePath(type(Path())):
            @staticmethod
            def home():
                return self.fake_home

        native.Path = FakePath

    def tearDown(self):
        native.Path = self._real_path
        self._tmp.cleanup()

    def test_the_launcher_is_made_on_the_desktop(self):
        made, detail = native.make_launcher(self.install_dir, "desktop",
                                            genesis_bin=str(self.genesis))
        self.assertTrue(made, detail)
        bundle = self.fake_home / "Desktop" / "Codexify.app"
        script = (bundle / "Contents" / "MacOS" / "Codexify").read_text()
        self.assertIn("codexify setup", script)
        self.assertIn("127.0.0.1:8888", script)

    def test_the_launcher_refuses_an_unknown_place(self):
        made, detail = native.make_launcher(self.install_dir, "garage")
        self.assertFalse(made)
        self.assertIn("not 'Garage'", detail)

    def test_the_launcher_refuses_to_overwrite(self):
        desktop = self.fake_home / "Desktop"
        (desktop / "Codexify.app").mkdir()
        made, detail = native.make_launcher(self.install_dir, "desktop",
                                            genesis_bin=str(self.genesis))
        self.assertFalse(made)
        self.assertIn("already there", detail)

    def test_the_launcher_refuses_without_a_genesis(self):
        made, detail = native.make_launcher(
            self.install_dir, "desktop",
            genesis_bin=str(self.base / "bin" / "not-there"))
        self.assertFalse(made)
        self.assertIn("no genesis", detail)


if __name__ == "__main__":
    unittest.main()
