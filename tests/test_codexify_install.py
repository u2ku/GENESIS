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
                    "--progress", "codexify-core")


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

    def test_convergence_still_applies_the_vendored_fixes(self):
        """The Arch box: a submodule that fetched upstream *before* a fix
        existed converges on every run — and the early return meant it
        never applied it. A converged checkout with a pending fix must
        still get the fix, not the green row."""
        self.make_install()
        (self.sub / "pnpm-workspace.yaml").write_text(
            "allowBuilds:\n"
            "  '@tailwindcss/oxide': set this to true or false\n"
            "  esbuild: set this to true or false\n", encoding="utf-8")
        report = ci.update(self.repo, runner=self.git_ok())
        self.assertEqual(report.exit_code, codexify.EXIT_OK)
        text = (self.sub / "pnpm-workspace.yaml").read_text()
        self.assertIn("'@tailwindcss/oxide': true", text)
        self.assertIn("esbuild: true", text)

    def test_every_network_step_gets_a_limit_a_slow_link_can_meet(self):
        """The Arch box timed out of a clone that was still progressing: a
        230 MiB download at a few hundred KiB/s is minutes, and the limit
        was five of them. The fetch step did not even pass its timeout on,
        so it kept git's own default."""
        class Timed(GitAnswers):
            timeouts = {}

            def __call__(self, args, cwd=None, timeout=300.0, env=None,
                         stream=False):
                self.timeouts[tuple(args[3:])] = timeout
                return super().__call__(args, cwd=cwd, timeout=timeout,
                                        env=env, stream=stream)

        plain = self.git_ok()
        runner = Timed(plain.answers, make_install=plain.make_install)
        report = ci.install(self.repo, runner=runner)
        self.assertEqual(report.exit_code, codexify.EXIT_OK)
        for step in (SUBMODULE_UPDATE,
                     ("submodule", "foreach", "git fetch origin main")):
            self.assertGreaterEqual(runner.timeouts[step], 3600.0, step)

    def test_a_timeout_says_how_long_it_waited(self):
        done = ci._run_command(["sleep", "5"], timeout=1, stream=False)
        self.assertEqual(done.returncode, 124)
        self.assertEqual(done.stderr, "timed out after 1s")

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

        fixed_workspace = ("packages:\n"
                           "  - frontend/src\n"
                           "  - edge/cloudflare-worker\n"
                           "allowBuilds:\n"
                           "  '@tailwindcss/oxide': true\n"
                           "  esbuild: true\n")
        broken_workspace = fixed_workspace.replace(
            "'@tailwindcss/oxide': true",
            "'@tailwindcss/oxide': set this to true or false").replace(
            "esbuild: true", "esbuild: set this to true or false")
        (self.sub / "pnpm-workspace.yaml").write_text(
            fixed_workspace if fixed else broken_workspace, encoding="utf-8")

    def test_the_pnpm_policy_placeholders_become_booleans(self):
        """Upstream shipped literal 'set this to true or false' in the
        build-approval map — pnpm 10 parses those values as booleans
        and dies before installing anything. The fix makes them true,
        which the tools also need: their postinstall builds are what
        the dev server runs on."""
        self.write_fixture(fixed=False)
        ok, note = ci.apply_vendored_fixes(self.sub)
        self.assertTrue(ok, note)
        text = (self.sub / "pnpm-workspace.yaml").read_text()
        self.assertIn("'@tailwindcss/oxide': true", text)
        self.assertIn("esbuild: true", text)
        self.assertNotIn("set this to true or false", text)

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
        # make_launcher branches on the platform; this case is the Mac's.
        self._real_platform = native.sys.platform
        native.sys.platform = "darwin"
        self._real_icns = native.LOGO_ICNS
        native.LOGO_ICNS = self.base / "no-such-logo.icns"

    def tearDown(self):
        native.Path = self._real_path
        native.sys.platform = self._real_platform
        native.LOGO_ICNS = self._real_icns
        self._tmp.cleanup()

    def test_the_bundle_carries_the_official_logo(self):
        """The plist names `app` as the icon, and until the logo shipped
        nothing ever wrote that file — the bundle was iconless."""
        logo = self.base / "logo.icns"
        logo.write_bytes(b"icns-bytes")
        native.LOGO_ICNS = logo
        made, detail = native.make_launcher(self.install_dir, "desktop",
                                            genesis_bin=str(self.genesis))
        self.assertTrue(made, detail)
        icon = (self.fake_home / "Desktop" / "Codexify.app" / "Contents"
                / "Resources" / "app.icns")
        self.assertEqual(icon.read_bytes(), b"icns-bytes")

    def test_a_bundle_without_the_logo_is_still_made(self):
        made, detail = native.make_launcher(self.install_dir, "desktop",
                                            genesis_bin=str(self.genesis))
        self.assertTrue(made, detail)
        self.assertFalse((self.fake_home / "Desktop" / "Codexify.app"
                          / "Contents" / "Resources" / "app.icns").exists())

    def test_the_launcher_is_made_on_the_desktop(self):
        made, detail = native.make_launcher(self.install_dir, "desktop",
                                            genesis_bin=str(self.genesis))
        self.assertTrue(made, detail)
        bundle = self.fake_home / "Desktop" / "Codexify.app"
        script = (bundle / "Contents" / "MacOS" / "Codexify").read_text()
        self.assertIn("codexify setup", script)

    def test_the_launcher_opens_the_interface_not_the_api(self):
        """The app is on 5173; 8888 is the API behind it.

        A launcher that opens the API lands a person on a JSON endpoint.
        The interface is what this launcher is for, so that is what it
        opens — and what it waits for, because vite does not answer the
        moment it starts.
        """
        made, detail = native.make_launcher(self.install_dir, "desktop",
                                            genesis_bin=str(self.genesis))
        self.assertTrue(made, detail)
        bundle = self.fake_home / "Desktop" / "Codexify.app"
        script = (bundle / "Contents" / "MacOS" / "Codexify").read_text()
        opened = [line.strip() for line in script.splitlines()
                  if line.strip().startswith("open ")]
        # The interface is what is opened first.
        self.assertEqual(opened[0], "open http://127.0.0.1:5173")
        # The API is only a fallback, for an interface that never came up.
        self.assertEqual(opened[-1], "open http://127.0.0.1:8888")
        self.assertEqual(len(opened), 2)

    def test_the_launcher_starts_the_stack_with_the_interface(self):
        """The interface it opens has to be the interface it starts.

        A setup without `--with-ui` brings up the API and leaves the
        interface down, and then the launcher waits for an interface
        that was never asked for.
        """
        made, detail = native.make_launcher(self.install_dir, "desktop",
                                            genesis_bin=str(self.genesis))
        self.assertTrue(made, detail)
        bundle = self.fake_home / "Desktop" / "Codexify.app"
        script = (bundle / "Contents" / "MacOS" / "Codexify").read_text()
        self.assertIn("codexify setup --with-ui", script)

    def test_the_launcher_waits_for_the_interface_before_opening(self):
        """One probe measures the boot, not the server."""
        made, detail = native.make_launcher(self.install_dir, "desktop",
                                            genesis_bin=str(self.genesis))
        self.assertTrue(made, detail)
        bundle = self.fake_home / "Desktop" / "Codexify.app"
        script = (bundle / "Contents" / "MacOS" / "Codexify").read_text()
        self.assertIn("127.0.0.1:5173", script)
        self.assertIn("sleep 1", script)

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


class DesktopEntryCase(unittest.TestCase):
    """The Linux launcher: a freedesktop entry in the app grid."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.install_dir = self.base / "install"
        self.install_dir.mkdir()
        self.fake_home = self.base / "home"
        (self.fake_home / ".local" / "share" / "applications").mkdir(
            parents=True)
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
        self._real_platform = native.sys.platform
        native.sys.platform = "linux"
        # The icon probe must not find the real machine's frontend.
        self._real_which = native.shutil.which
        native.shutil.which = lambda name: None
        # Nor the repo's real logo: these tests are about the fallbacks
        # until one of them says otherwise.
        self._real_logo = native.LOGO_PNG
        native.LOGO_PNG = self.base / "no-such-logo.png"

    def tearDown(self):
        native.Path = self._real_path
        native.sys.platform = self._real_platform
        native.shutil.which = self._real_which
        native.LOGO_PNG = self._real_logo
        self._tmp.cleanup()

    def icon_dir(self):
        return (self.fake_home / ".local" / "share" / "icons" / "hicolor"
                / "256x256" / "apps")

    def installed_logo(self):
        """The one installed icon — its name carries a hash of its content."""
        found = list(self.icon_dir().glob("codexify-*.png"))
        self.assertEqual(len(found), 1, found)
        return found[0]

    def test_a_changed_logo_gets_a_new_path_and_leaves_no_stale_file(self):
        """The shell caches an icon by path and never notices the file
        behind it changed: the Arch box pulled the new logo, rewrote the
        entry, and kept showing the old one. A new logo has to be a new
        path, and the old file must not pile up beside it."""
        logo = self.base / "logo.png"
        logo.write_bytes(b"first-logo")
        native.LOGO_PNG = logo
        native.make_launcher(self.install_dir, "desktop",
                             genesis_bin=str(self.genesis))
        first = self.installed_logo()
        logo.write_bytes(b"second-logo")
        native.make_launcher(self.install_dir, "desktop",
                             genesis_bin=str(self.genesis))
        second = self.installed_logo()          # exactly one is left
        self.assertNotEqual(first, second)
        self.assertEqual(second.read_bytes(), b"second-logo")
        self.assertFalse(first.exists())
        self.assertIn("Icon=" + str(second), self.entry().read_text())

    def test_an_unchanged_logo_keeps_its_path(self):
        logo = self.base / "logo.png"
        logo.write_bytes(b"same-logo")
        native.LOGO_PNG = logo
        native.make_launcher(self.install_dir, "desktop",
                             genesis_bin=str(self.genesis))
        first = self.installed_logo()
        native.make_launcher(self.install_dir, "desktop",
                             genesis_bin=str(self.genesis))
        self.assertEqual(self.installed_logo(), first)

    def test_the_opener_exists_without_a_launcher_having_been_made(self):
        """The wizard opens the app before — or without — the tile, since
        --no-launcher is a choice, so the opener cannot be the launcher's
        by-product."""
        opener = native.ensure_app_opener(self.install_dir)
        self.assertEqual(opener, self.install_dir / ".native" / "open-app.sh")
        self.assertTrue(opener.stat().st_mode & 0o100)       # runnable
        self.assertIn(str(self.install_dir / ".native" / "browser-profile"),
                      opener.read_text())
        self.assertFalse(self.entry().exists())              # no tile made

    def test_the_entry_matches_the_app_window_but_does_not_wait_for_it(self):
        """The opener's window has class Codexify, so the dock groups it
        under this entry's icon. But the fallback is a plain tab that
        never claims to be Codexify, and a shell told to wait for a window
        shows "launching" until its timeout and then drops it — a launch
        that worked, reading as one that failed."""
        made, detail = native.make_launcher(self.install_dir, "desktop",
                                            genesis_bin=str(self.genesis))
        self.assertTrue(made, detail)
        entry = self.entry().read_text()
        self.assertIn("StartupWMClass=Codexify", entry)
        self.assertIn("StartupNotify=false", entry)

    def test_the_launcher_script_writes_what_it_decides_to_its_log(self):
        """A tile clicked in a grid has no terminal. The Arch box had no
        launcher.log at all, which could not say whether the script never
        ran or ran and had nothing to report."""
        made, detail = native.make_launcher(self.install_dir, "desktop",
                                            genesis_bin=str(self.genesis))
        self.assertTrue(made, detail)
        script = (self.install_dir / ".native" / "launcher.sh").read_text()
        self.assertIn('exec >> "', script)
        self.assertIn("launcher started", script)
        self.assertIn("(exit $?)", script)

    def test_the_official_logo_is_the_entry_icon(self):
        """The logo is copied into the user's icon directory — so the tile
        keeps it when GENESIS is moved or re-cloned — and it wins over the
        frontend's own icon even when both are there."""
        logo = self.base / "logo.png"
        logo.write_bytes(b"official-logo")
        native.LOGO_PNG = logo
        icons = (self.install_dir / "frontend" / "src" / "public" / "icons")
        icons.mkdir(parents=True)
        (icons / "icon-512.png").write_bytes(b"upstream")
        made, detail = native.make_launcher(self.install_dir, "desktop",
                                            genesis_bin=str(self.genesis))
        self.assertTrue(made, detail)
        self.assertEqual(self.installed_logo().read_bytes(), b"official-logo")
        entry = self.entry().read_text()
        self.assertIn("Icon=" + str(self.installed_logo()), entry)
        self.assertNotIn("icon-512", entry)

    def test_the_repo_ships_a_real_logo(self):
        """The assets are vendored, and a checkout that lost them would
        quietly fall back to upstream's icon — so their existence is a
        test, not a hope. The format is checked by its magic bytes."""
        png = self._real_logo.read_bytes()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        icns = self._real_logo.with_name("codexify.icns")
        self.assertEqual(icns.read_bytes()[:4], b"icns")

    def entry(self):
        return (self.fake_home / ".local" / "share" / "applications"
                / native.DESKTOP_ENTRY_FILE)

    def test_the_entry_lands_in_the_app_grid(self):
        made, detail = native.make_launcher(self.install_dir, "desktop",
                                            genesis_bin=str(self.genesis))
        self.assertTrue(made, detail)
        entry = self.entry().read_text()
        self.assertIn("[Desktop Entry]", entry)
        self.assertIn("Name=Codexify", entry)
        self.assertIn("Terminal=false", entry)

    def test_the_entry_runs_a_script_that_converges_and_opens(self):
        """The grid tile is the Mac bundle's behaviour in Linux clothes:
        converge the stack with the interface, then open the interface —
        through the app-window opener, not `open` — with the API as the
        fallback."""
        made, detail = native.make_launcher(self.install_dir, "desktop",
                                            genesis_bin=str(self.genesis))
        self.assertTrue(made, detail)
        script = (self.install_dir / ".native" / "launcher.sh").read_text()
        opener = str(self.install_dir / ".native" / "open-app.sh")
        self.assertIn("codexify setup --with-ui", script)
        self.assertIn(opener + " http://127.0.0.1:5173", script)
        self.assertIn(opener + " http://127.0.0.1:8888", script)
        self.assertIn("GENESIS_NO_BROWSER", script)
        entry = self.entry().read_text()
        self.assertIn("Exec=" + str(self.install_dir / ".native"
                                    / "launcher.sh"), entry)

    def test_a_second_run_converges_the_entry(self):
        """The Mac bundle refuses a remake; the grid entry converges one.
        A wizard that re-runs must refresh the tile to the stack that
        exists now, not refuse and leave a stale entry pointing at an
        install that moved."""
        made, _ = native.make_launcher(self.install_dir, "desktop",
                                       genesis_bin=str(self.genesis))
        self.assertTrue(made)
        made, detail = native.make_launcher(self.install_dir, "desktop",
                                            genesis_bin=str(self.genesis))
        self.assertTrue(made, detail)

    def test_the_entry_uses_the_frontend_icon_when_it_ships(self):
        icons = (self.install_dir / "frontend" / "src" / "public"
                 / "icons")
        icons.mkdir(parents=True)
        (icons / "icon-512.png").write_bytes(b"png")
        made, _ = native.make_launcher(self.install_dir, "desktop",
                                       genesis_bin=str(self.genesis))
        self.assertTrue(made)
        self.assertIn("Icon=" + str(icons / "icon-512.png"),
                      self.entry().read_text())

    def test_the_entry_is_made_without_an_icon(self):
        """No icon shipped is not a failed launcher — the tile is a cog,
        and a working cog beats a missing app."""
        made, detail = native.make_launcher(self.install_dir, "desktop",
                                            genesis_bin=str(self.genesis))
        self.assertTrue(made, detail)
        self.assertNotIn("Icon=", self.entry().read_text())

    def test_the_entry_refuses_without_a_genesis(self):
        made, detail = native.make_launcher(
            self.install_dir, "desktop",
            genesis_bin=str(self.base / "bin" / "not-there"))
        self.assertFalse(made)
        self.assertIn("no genesis", detail)


@unittest.skipUnless(Path("/bin/bash").exists(), "the launcher is a bash script")
class LauncherScriptBehaviourCase(unittest.TestCase):
    """The launcher script, run — not read.

    Every other launcher test asserts that text is present in the script,
    and that is how a script that never opened a browser shipped: filling
    in `$GENESIS` also rewrote `$GENESIS_NO_BROWSER` into a path, which is
    never empty, so the script exited before its opener on every machine.
    These run it, against fake curl / xdg-open / genesis on a private PATH.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.log = self.base / "launcher.log"
        self.up = self.base / "up"
        self.tool("xdg-open", f'echo "$1" >> "{self.base}/opened"')
        self.tool("genesis", f'echo "$@" >> "{self.base}/ran"; touch "{self.up}"')
        self.script = self.base / "launcher.sh"
        self.script.write_text(native.render_launcher_script(
            str(self.bin / "genesis"), self.log, "xdg-open"), encoding="utf-8")
        self.script.chmod(0o755)

    def tearDown(self):
        self._tmp.cleanup()

    def tool(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/bash\n" + body + "\n", encoding="utf-8")
        path.chmod(0o755)

    def run_script(self, stack_up, extra_env=None):
        """Run the launcher. `stack_up` is where the fake curl answers."""
        self.tool("curl", "exit 0" if stack_up else
                  f'[ -e "{self.up}" ] && exit 0 || exit 7')
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.base)}
        env.update(extra_env or {})
        done = subprocess.run([str(self.script)], env=env, timeout=30,
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        opened = self.base / "opened"
        ran = self.base / "ran"
        return (opened.read_text().split() if opened.exists() else [],
                ran.read_text() if ran.exists() else "")

    def test_a_running_stack_is_opened_and_left_alone(self):
        opened, ran = self.run_script(stack_up=True)
        self.assertEqual(opened, ["http://127.0.0.1:5173"])
        self.assertEqual(ran, "")                     # nothing was converged

    def test_a_cold_start_converges_the_stack_then_opens_it(self):
        opened, ran = self.run_script(stack_up=False)
        self.assertIn("codexify setup --with-ui", ran)
        self.assertEqual(opened, ["http://127.0.0.1:5173"])

    def test_the_callers_no_browser_switch_is_still_honoured(self):
        """The contract the substitution broke from the other side: a test
        run or an agent's shell says GENESIS_NO_BROWSER and gets no window."""
        opened, _ = self.run_script(stack_up=True,
                                    extra_env={"GENESIS_NO_BROWSER": "1"})
        self.assertEqual(opened, [])

    def test_the_log_says_what_happened(self):
        self.run_script(stack_up=True)
        text = self.log.read_text()
        self.assertIn("launcher started", text)
        self.assertIn("asked xdg-open to open the interface (exit 0)", text)


@unittest.skipUnless(Path("/bin/bash").exists(), "the opener is a bash script")
class AppWindowOpenerCase(unittest.TestCase):
    """The Linux opener, run against fake browsers on a PATH of its own.

    The PATH holds only the fakes, so a Chrome installed on the machine
    running the suite cannot change what these tests find.
    """

    URL = "http://127.0.0.1:5173"

    def setUp(self):
        import time
        self._time = time
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.profile = self.base / "browser-profile"
        self.script = self.base / "open-app.sh"
        self.script.write_text(native.render_open_script(self.profile),
                               encoding="utf-8")
        self.script.chmod(0o755)
        # The fake browsers sleep, to prove the opener does not wait on them.
        (self.bin / "sleep").symlink_to("/bin/sleep")
        self.tool("xdg-open", f'echo "$@" > "{self.base}/xdg-open.args"')

    def tearDown(self):
        self._tmp.cleanup()

    def tool(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/bash\n" + body + "\n", encoding="utf-8")
        path.chmod(0o755)

    def browser(self, name):
        self.tool(name, f'echo "$@" > "{self.base}/{name}.args"; sleep 3')

    def run_opener(self, env=None):
        started = self._time.monotonic()
        done = subprocess.run([str(self.script), self.URL],
                              env={"PATH": str(self.bin), **(env or {})},
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return self._time.monotonic() - started

    def args(self, name):
        """What a fake was started with — it writes this a moment after
        the opener has already returned, so it is waited for."""
        path = self.base / f"{name}.args"
        for _ in range(40):
            if path.exists() and path.read_text().strip():
                return path.read_text().split()
            self._time.sleep(0.1)
        return None

    def test_a_chromium_browser_gets_an_app_window_of_its_own(self):
        self.browser("chromium")
        self.run_opener()
        args = self.args("chromium")
        self.assertIn(f"--app={self.URL}", args)
        # Its own class, so the dock groups it under Codexify's icon, and its
        # own profile, so it is Codexify's window and not a tab among others.
        self.assertIn("--class=Codexify", args)
        self.assertIn(f"--user-data-dir={self.profile}", args)
        self.assertFalse((self.base / "xdg-open.args").exists())

    def test_the_opener_does_not_wait_for_the_window_to_close(self):
        """A browser window lives until it is closed; the launcher must
        not, or the tile would hang on a window it already opened."""
        self.browser("google-chrome-stable")           # sleeps 3 seconds
        elapsed = self.run_opener()
        self.assertLess(elapsed, 2.0)
        self.assertTrue(self.args("google-chrome-stable"))

    def test_the_first_known_browser_wins_over_the_rest(self):
        self.browser("google-chrome-stable")
        self.browser("chromium")
        self.run_opener()
        self.assertTrue(self.args("google-chrome-stable"))
        self.assertFalse((self.base / "chromium.args").exists())

    def test_a_named_browser_overrides_the_search(self):
        self.browser("google-chrome-stable")
        self.browser("brave")
        self.run_opener(env={"CODEXIFY_BROWSER": "brave"})
        self.assertTrue(self.args("brave"))
        self.assertFalse((self.base / "google-chrome-stable.args").exists())

    def test_with_no_chromium_browser_the_default_one_gets_a_tab(self):
        """Firefox has no app mode, so it is the fallback, not a candidate."""
        self.browser("firefox")
        self.run_opener()
        self.assertEqual(self.args("xdg-open"), [self.URL])
        self.assertFalse((self.base / "firefox.args").exists())


class _FakeTTY:
    """A stdin that says what it is told: a terminal, and lines of it."""

    def __init__(self, lines):
        self._lines = iter(lines)

    def isatty(self):
        return True

    def readline(self):
        return next(self._lines)

    @property
    def encoding(self):
        return "utf-8"


class WhooshdDefaultsCase(unittest.TestCase):
    """The step the installer asks once: which broker, which model.

    The defaults are the Mac Studio's — the `space.codexify.whooshd`
    LaunchAgent serving the tailnet — and hitting enter is how a person
    takes them.
    """

    def test_a_piped_run_takes_the_launchagent_defaults(self):
        """A run without a terminal cannot be asked, and takes the defaults
        it would have been offered."""
        ci.prompt_whooshd_manual_config.no_prompts = True
        try:
            config = ci.prompt_whooshd_manual_config()
        finally:
            del ci.prompt_whooshd_manual_config.no_prompts
        self.assertEqual(config["CODEXIFY_RUNTIME_ENDPOINT"],
                         "http://100.71.144.117:8000/v1")
        self.assertEqual(config["OPENAI_API_BASE"],
                         "http://100.71.144.117:8000/v1")
        self.assertEqual(config["CODEXIFY_MODEL"],
                         "supergemma4-26b-uncensored")

    def test_enter_takes_the_defaults(self):
        """Two empty lines are two enters, and two enters are the defaults."""
        real_stdin = sys.stdin
        sys.stdin = _FakeTTY(["\n", "\n"])
        try:
            config = ci.prompt_whooshd_manual_config()
        finally:
            sys.stdin = real_stdin
        self.assertEqual(config["CODEXIFY_RUNTIME_ENDPOINT"],
                         "http://100.71.144.117:8000/v1")
        self.assertEqual(config["CODEXIFY_MODEL"],
                         "supergemma4-26b-uncensored")

    def test_a_typed_answer_wins(self):
        """A typed endpoint and model are what the operator meant."""
        real_stdin = sys.stdin
        sys.stdin = _FakeTTY(["http://10.0.0.5:11434/v1\n", "gemma\n"])
        try:
            config = ci.prompt_whooshd_manual_config()
        finally:
            sys.stdin = real_stdin
        self.assertEqual(config["CODEXIFY_RUNTIME_ENDPOINT"],
                         "http://10.0.0.5:11434/v1")
        self.assertEqual(config["CODEXIFY_MODEL"], "gemma")


if __name__ == "__main__":
    unittest.main()
