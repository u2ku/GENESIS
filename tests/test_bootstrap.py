"""GENESIS — the ladder's tests.

The ladder is three rungs and a receipt, and the rules it holds are the
rules that make "the user has seen the diff" true:

- **inspect changes nothing.** Not a receipt, not a manifest, not a file.
- **apply without a matching receipt refuses.** Exit 4, and the reason.
- **a wrong key writes nothing.** Exit 8, and the filesystem proves it.
- **a file the tool does not own is a conflict**, not an overwrite. Exit 6.
- **the key never appears** in an output, a receipt, a manifest or a diff.
- **a second run is a no-op.** Already converged, nothing to do.

Every test runs against a throwaway home and a throwaway repository, and
the CLI is stubbed onto PATH, so nothing here touches the machine it
runs on, and nothing here touches the network either.

    python3 -m unittest discover -s tests -v
    python3 tests/test_bootstrap.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: E402
import hermetic  # noqa: E402
import keydetect  # noqa: E402
import receipt as receipt_mod  # noqa: E402


class LadderCase(unittest.TestCase):
    """A throwaway home and a throwaway repository."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.repo = base / "repo"
        self.home.mkdir()
        self.repo.mkdir()
        # The repository needs to look like a repository to the plan: the
        # genesis executable is what the PATH symlink points at.
        (self.repo / "genesis").write_text("#!/bin/sh\nexit 0\n")
        (self.repo / "src").mkdir()
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        # The CLI and Tailscale are stubbed onto PATH, so the plan finds
        # them present and never runs an installer. Without this the
        # apply path reaches `curl -fsSL https://claude.ai/install.sh | sh`
        # on any machine without the CLI — a live download and a remote
        # shell script inside a unit test.
        self._old_path = hermetic.stub_cli_on_path(base / "stub-bin")
        self._old_cwd = os.getcwd()
        os.chdir(self.repo)

    def tearDown(self):
        os.chdir(self._old_cwd)
        hermetic.restore_path(self._old_path)
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        self._tmp.cleanup()

    def run_bootstrap(self, *args) -> tuple:
        """Run the bootstrap, and return (exit code, output).

        The voice is default now, so the ladder tests opt out centrally:
        they assert on their own tree, and the voice's real actions build
        a venv and download a model — network work no hermetic test
        owes. The voice has its own case, its own file, and its own
        test that proves the default and the opt-out.
        """
        args = tuple(a for a in args if a != "--no-voice") + ("--no-voice",)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = bootstrap.main(["--repo", str(self.repo)] + list(args))
        return code, buffer.getvalue()

    def tree_digest(self) -> str:
        """A digest of everything under the home and the repository."""
        parts = []
        for base in (self.home, self.repo):
            for path in sorted(base.rglob("*")):
                if path.is_file():
                    try:
                        parts.append(str(path.relative_to(base)) + "::" +
                                     str(path.stat().st_size))
                    except OSError:
                        continue
        return json.dumps(sorted(parts))


class TestTheRungs(LadderCase):

    def test_inspect_changes_nothing(self):
        before = self.tree_digest()
        code, out = self.run_bootstrap()
        self.assertEqual(code, 0)
        self.assertIn("inspect", out)
        self.assertEqual(self.tree_digest(), before,
                         "inspect wrote something")

    def test_a_dry_run_writes_a_receipt_and_nothing_else(self):
        code, out = self.run_bootstrap("--dry-run")
        self.assertEqual(code, 0)
        receipt = self.repo / ".genesis-bootstrap" / "receipt.json"
        self.assertTrue(receipt.exists())
        self.assertFalse((self.home / ".config" / "genesis").exists(),
                         "the dry run wrote to the machine")

    def test_apply_without_a_receipt_refuses(self):
        code, out = self.run_bootstrap("--apply")
        self.assertEqual(code, bootstrap.EXIT_NO_RECEIPT)
        self.assertIn("no valid dry-run receipt", out)
        self.assertFalse((self.home / ".config" / "genesis").exists())

    def test_the_apply_follows_a_dry_run(self):
        self.run_bootstrap("--dry-run")
        code, out = self.run_bootstrap("--apply")
        self.assertEqual(code, 0)
        self.assertTrue((self.home / ".config" / "genesis" / "node.json").exists())
        self.assertTrue((self.home / ".local" / "bin" / "genesis").is_symlink())

    def test_a_second_run_is_a_no_op(self):
        self.run_bootstrap("--dry-run")
        self.run_bootstrap("--apply")
        code, out = self.run_bootstrap("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("already converged", out)

    def test_a_second_apply_is_a_no_op_not_a_conflict(self):
        """Regression: the manifest must record the hash the check compares.

        The portability test found this — a file this tool wrote was
        recorded under one hash and checked under another, so every second
        apply came back as a conflict.
        """
        self.run_bootstrap("--dry-run")
        self.run_bootstrap("--apply")
        self.run_bootstrap("--dry-run")
        code, out = self.run_bootstrap("--apply")
        self.assertEqual(code, 0, out)
        self.assertNotIn("conflict", out)

    def test_a_file_edited_underneath_is_a_conflict(self):
        """The ownership rule, on a file this tool wrote."""
        self.run_bootstrap("--dry-run")
        self.run_bootstrap("--apply")
        profile = self.home / ".config" / "genesis" / "node.json"
        profile.write_text('{"tampered": true}\n')
        self.run_bootstrap("--dry-run")
        code, out = self.run_bootstrap("--apply")
        self.assertEqual(code, bootstrap.EXIT_CONFLICT)
        self.assertIn("someone else edited it", out)

    def test_aggressive_without_a_rung_is_a_usage_error(self):
        code, out = self.run_bootstrap("--aggressive")
        self.assertEqual(code, bootstrap.EXIT_USAGE)
        self.assertIn("--aggressive needs --dry-run or --apply", out)

    def test_the_receipt_is_bound_to_the_tier(self):
        self.run_bootstrap("--dry-run")
        code, out = self.run_bootstrap("--apply", "--aggressive")
        self.assertEqual(code, bootstrap.EXIT_NO_RECEIPT)
        self.assertIn("receipt", out)

    def test_the_receipt_expires_when_the_plan_changes(self):
        self.run_bootstrap("--dry-run")
        # The machine changed: something this tool would write now exists.
        (self.home / ".config").mkdir(parents=True, exist_ok=True)
        (self.home / ".config" / "genesis").mkdir(parents=True, exist_ok=True)
        (self.home / ".config" / "genesis" / "node.json").write_text("{}\n")
        code, out = self.run_bootstrap("--apply")
        self.assertEqual(code, bootstrap.EXIT_NO_RECEIPT)
        self.assertIn("no longer matches", out)


class TestTheOwnershipRule(LadderCase):

    def test_a_file_the_tool_does_not_own_is_a_conflict(self):
        target = self.home / ".config" / "genesis" / "node.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n")
        self.run_bootstrap("--dry-run")
        code, out = self.run_bootstrap("--apply")
        self.assertEqual(code, bootstrap.EXIT_CONFLICT)
        self.assertIn("conflict", out)
        self.assertEqual(target.read_text(), "{}\n",
                         "the conflict overwrote the file")

    def test_force_own_takes_ownership(self):
        target = self.home / ".config" / "genesis" / "node.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n")
        self.run_bootstrap("--dry-run")
        code, out = self.run_bootstrap("--apply", "--force-own", str(target))
        self.assertEqual(code, 0)
        self.assertNotEqual(target.read_text(), "{}\n")


class TestTheWizard(LadderCase):
    """The wizard's rules, with a gateway that answers the way we need it to."""

    KEY = "sk-test-key-for-the-wizard"

    def test_a_wrong_key_exits_8_and_writes_nothing(self):
        """The rule from WIZARD.md §3.3 and §10, proved by the filesystem."""
        key_file = self._key_file()
        before = self.tree_digest()
        code, out = self.run_bootstrap(
            "--wizard", "--dry-run", "--gateway-url",
            "http://127.0.0.1:1", "--key-file", key_file)
        self.assertEqual(code, bootstrap.EXIT_KEY_REJECTED)
        self.assertIn("did not authenticate", out)
        self.assertEqual(self.tree_digest(), before,
                         "the refusal wrote something")

    def test_the_key_never_appears_in_the_output(self):
        key_file = self._key_file()
        code, out = self.run_bootstrap(
            "--wizard", "--dry-run", "--gateway-url",
            "http://127.0.0.1:1", "--key-file", key_file)
        self.assertNotIn(self.KEY, out)
        self.assertNotIn(self.KEY[:12], out)

    def test_the_mask_masks_a_key_that_would_otherwise_be_printed(self):
        """The diff shows placeholders, never values."""
        masked = bootstrap.keydetect.mask("MINIMAX_API_KEY=" + self.KEY)
        self.assertNotIn(self.KEY, masked)
        self.assertIn("***", masked)

    def test_keep_key_finds_the_stored_credential_by_its_shape(self):
        """The operator must not have to remember which variable it is in."""
        (self.repo / ".env").write_text(
            "# GENESIS — environment\n"
            "MINIMAX_API_KEY=sk-cp-somethinglongenoughtolooklikeakey\n")
        name, value = bootstrap._stored_credential(
            type("C", (), {"repo": self.repo})())
        self.assertEqual(name, "MINIMAX_API_KEY")
        self.assertIn("sk-cp-", value)

    def test_keep_key_ignores_a_variable_that_is_not_a_credential(self):
        (self.repo / ".env").write_text(
            "EDITOR=vim\nPATH=/usr/bin\n")
        name, value = bootstrap._stored_credential(
            type("C", (), {"repo": self.repo})())
        self.assertEqual(name, "")
        self.assertEqual(value, "")

    def test_the_key_file_is_the_way_in_and_never_an_argument(self):
        parser = bootstrap.build_parser()
        flags = {action.dest for action in parser._actions}
        self.assertNotIn("key", flags,
                         "there must be no flag that takes the key itself")
        self.assertIn("key_file", flags)

    def _key_file(self) -> str:
        path = self.repo / "key.txt"
        path.write_text(self.KEY + "\n")
        os.chmod(path, 0o600)
        return str(path)


class TestTheKeyRules(LadderCase):

    def test_the_credential_lands_under_the_right_name(self):
        """A key filed under the wrong variable authenticates nowhere."""
        env = self.repo / ".env"
        env.write_text("OPENROUTER_API_KEY=sk-cp-something\n")
        text = bootstrap._set_env_line(env.read_text(), "MINIMAX_API_KEY",
                                       "sk-cp-something")
        self.assertIn("MINIMAX_API_KEY=sk-cp-something", text)
        self.assertIn("OPENROUTER_API_KEY=sk-cp-something", text)

    def test_only_the_named_line_changes(self):
        env = self.repo / ".env"
        env.write_text("A=1\nB=2\nC=3\n")
        text = bootstrap._set_env_line(env.read_text(), "B", "new")
        self.assertEqual(text, "A=1\nB=new\nC=3\n")

    def test_the_manifest_hash_of_a_credential_hides_the_credential(self):
        text = "MINIMAX_API_KEY=sk-cp-secretvalue\nOTHER=1\n"
        digest = bootstrap._redacted_hash(text, "MINIMAX_API_KEY")
        self.assertNotIn("sk-cp-secretvalue", digest)
        other = bootstrap._redacted_hash(
            "MINIMAX_API_KEY=sk-cp-secretvalue\nOTHER=2\n", "MINIMAX_API_KEY")
        self.assertNotEqual(digest, other,
                            "an edit to another line must be detected")


class TestTheCredentialsAlreadyThere(LadderCase):
    """The rc files are not the wizard's business — unless the operator says so."""

    def write_rc(self, name=".zshrc", body=""):
        path = self.home / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_a_credential_shaped_export_is_found(self):
        self.write_rc(body="export OPENROUTER_API_KEY=sk-or-v1-abcdefghijkl\n")
        found = bootstrap.scan_stale_credentials({"profile": "macos"})
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["var"], "OPENROUTER_API_KEY")
        self.assertNotIn("sk-or-v1", str(found[0]["fingerprint"]))

    def test_an_ordinary_variable_is_not_a_credential(self):
        self.write_rc(body="export EDITOR=vim\nexport PATH=/usr/bin\n")
        found = bootstrap.scan_stale_credentials({"profile": "macos"})
        self.assertEqual(found, [])

    def test_the_value_is_never_reported(self):
        self.write_rc(body="export MINIMAX_API_KEY=sk-cp-supersecretvalue\n")
        found = bootstrap.scan_stale_credentials({"profile": "macos"})
        reported = [{k: str(v) for k, v in f.items() if k != "value"}
                    for f in found]
        self.assertNotIn("supersecretvalue", json.dumps(reported))

    def test_a_credential_that_cannot_be_shown_superseded_is_not_retired(self):
        """The wizard never retires one it cannot show is superseded."""
        self.write_rc(body="export OPENROUTER_API_KEY=sk-or-v1-abcdefghijkl\n")
        found = bootstrap.scan_stale_credentials({"profile": "macos"})
        for item in found:
            self.assertNotEqual(item["fingerprint"],
                                keydetect.fingerprint("sk-ant-other"))
        # nothing is retired unless the operator's word is on it
        ctx = bootstrap.Context()
        ctx.repo = self.repo
        ctx.stale = found
        ctx.retire = []
        self.assertEqual(bootstrap._retire_plan(ctx, {"profile": "macos"}), [])

    def test_a_retirement_removes_the_export_and_backs_the_file_up(self):
        rc = self.write_rc(
            body="export OPENROUTER_API_KEY=sk-or-v1-abcdefghijkl\n"
                 "export EDITOR=vim\n")
        found = bootstrap.scan_stale_credentials({"profile": "macos"})
        ctx = bootstrap.Context()
        ctx.repo = self.repo
        ctx.stale = found
        ctx.retire = found
        actions = bootstrap._retire_plan(ctx, {"profile": "macos"})
        self.assertEqual(len(actions), 1)
        content = actions[0]["content"]
        self.assertNotIn("sk-or-v1-abcdefghijkl", content)
        self.assertIn("retired OPENROUTER_API_KEY", content)
        self.assertIn("export EDITOR=vim", content)

        manifest = {}
        bootstrap.apply_plan(ctx, actions, manifest)
        self.assertNotIn("sk-or-v1-abcdefghijkl", rc.read_text())
        self.assertIn("export EDITOR=vim", rc.read_text())
        retired = manifest.get("retired", [])
        self.assertEqual(len(retired), 1)
        backup = Path(retired[0]["backup"])
        self.assertTrue(backup.exists())
        self.assertIn("sk-or-v1-abcdefghijkl", backup.read_text())
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)


class TestTheOmarchyBranch(LadderCase):
    """The guest is not switched on; the branch still has to be right."""

    def test_an_omarchy_host_is_a_systemd_user_host(self):
        profile = {"profile": "omarchy", "kernel": "Linux",
                   "distro": "Arch Linux", "arch": "aarch64",
                   "shell": "bash", "python": "3.14.8", "js_runtime": None,
                   "package_manager": "pacman", "daemon_kind": "systemd-user",
                   "sudo_noninteractive": True, "omarchy": True,
                   "probe_version": 1}
        ctx = bootstrap.Context()
        ctx.repo = self.repo
        ctx.aggressive = True
        ctx.node = "anchor"
        actions = bootstrap.build_plan(ctx, profile)
        unit = [a for a in actions if a.get("id") == "node-daemon"]
        self.assertEqual(len(unit), 1)
        content = unit[0]["content"]
        self.assertIn("genesis-node.service", unit[0]["target"])
        self.assertIn("node_daemon.py", content)
        self.assertIn("WantedBy=default.target", content)
        self.assertIn("[Install]", content)
        # a user unit, never a system one
        self.assertNotIn("User=", content)
        self.assertNotIn("/etc/systemd/system", unit[0]["target"])

    def _bare_host(self):
        """A host with no claude and no tailscale — like a fresh guest."""
        original = bootstrap._which
        bootstrap._which = lambda name: None
        self.addCleanup(lambda: setattr(bootstrap, "_which", original))

    def test_the_tailscale_action_says_the_system_package_manager(self):
        profile = {"profile": "omarchy", "kernel": "Linux",
                   "distro": "Arch Linux", "arch": "aarch64",
                   "shell": "bash", "python": "3.14.8", "js_runtime": None,
                   "package_manager": "pacman", "daemon_kind": "systemd-user",
                   "sudo_noninteractive": True, "omarchy": True,
                   "probe_version": 1}
        self._bare_host()
        ctx = bootstrap.Context()
        ctx.repo = self.repo
        ctx.aggressive = True
        actions = bootstrap.build_plan(ctx, profile)
        tailscale = [a for a in actions if a.get("id") == "tailscale"][0]
        self.assertIn("pacman", tailscale["note"])
        self.assertIn("never a vendored binary", tailscale["note"])
        self.assertNotIn("curl", tailscale["note"])

    def test_no_javascript_runtime_is_not_a_failure(self):
        """The guest has no node, npm, bun, deno, yarn or pnpm."""
        profile = {"profile": "omarchy", "kernel": "Linux",
                   "distro": "Arch Linux", "arch": "aarch64",
                   "shell": "bash", "python": "3.14.8", "js_runtime": None,
                   "package_manager": "pacman", "daemon_kind": "systemd-user",
                   "sudo_noninteractive": True, "omarchy": True,
                   "probe_version": 1}
        self._bare_host()
        ctx = bootstrap.Context()
        ctx.repo = self.repo
        actions = bootstrap.build_plan(ctx, profile)
        claude = [a for a in actions if a.get("id") == "claude-cli"][0]
        self.assertEqual(claude["effect"], "install")
        self.assertIn("native installer", claude["note"])
        self.assertNotIn("npm", claude["note"])


class TestTheReadinessProbe(LadderCase):
    """F3: readiness must PROVE, not claim.

    Every row is the result of something that ran. A row that says ✓
    because the plan intended it is 482's false success, one layer up.
    """

    def test_the_gateway_row_is_a_real_probe(self):
        ctx = bootstrap.Context()
        ctx.repo = self.repo
        ctx.key_value = "sk-test"
        ctx.gateway_endpoint = keydetect.Endpoint(
            "test", keydetect.ANTHROPIC, "http://127.0.0.1:1", "TEST_API_KEY",
            ("m1",), ("x-api-key",))
        ctx.model = "m1"
        rows = dict((label, mark) for label, mark, _ in
                    bootstrap.readiness(ctx))
        # nothing is listening on that port, so the row must fail
        self.assertFalse(rows["the gateway"])

    def test_the_chat_probe_fails_when_the_call_fails(self):
        """The row follows the call. It does not assert what it hopes for."""
        import harness
        ctx = bootstrap.Context()
        ctx.repo = self.repo
        ctx.backend = "minimax"
        original = harness._post
        try:
            harness._post = lambda payload, backend="mlx": (_ for _ in ()).throw(
                RuntimeError("the gateway refused it"))
            good, detail = bootstrap._chat_probe(ctx)
            self.assertFalse(good)
            self.assertIn("refused", detail)

            harness._post = lambda payload, backend="mlx": {
                "type": "message", "role": "assistant",
                "content": [{"type": "text", "text": "pong"}]}
            good, detail = bootstrap._chat_probe(ctx)
            self.assertTrue(good)
            self.assertIn("minimax", detail)
        finally:
            harness._post = original

    def test_the_chat_probe_states_a_custom_gateway_rather_than_failing(self):
        """F11: a custom gateway is a limitation of the harness, not a
        consumer that failed — and the table says so in those words."""
        ctx = bootstrap.Context()
        ctx.repo = self.repo
        ctx.backend = "not-a-backend"
        good, detail = bootstrap._chat_probe(ctx)
        self.assertIsNone(good)
        self.assertIn("no backend for", detail)
        self.assertIn("genesis chat", detail)

    def test_the_helper_fingerprint_is_what_the_helper_prints(self):
        helper = bootstrap.helper_path()
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text("#!/bin/sh\nprintf '%s' 'sk-secretvalue'\n",
                          encoding="utf-8")
        ctx = bootstrap.Context()
        ctx.repo = self.repo
        found = bootstrap._helper_fingerprint(ctx)
        self.assertEqual(found, keydetect.fingerprint("sk-secretvalue"))

    def test_a_helper_that_prints_nothing_is_not_a_pass(self):
        helper = bootstrap.helper_path()
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        ctx = bootstrap.Context()
        ctx.repo = self.repo
        self.assertIsNone(bootstrap._helper_fingerprint(ctx))

    def test_the_codexify_row_reads_its_own_env(self):
        codexify = self.home / "codexify"
        codexify.mkdir()
        (codexify / ".env").write_text("OPENAI_API_KEY=sk-othervalue\n")
        ctx = bootstrap.Context()
        ctx.repo = self.repo
        ctx.codexify_dir = codexify
        found = bootstrap._codexify_fingerprint(ctx)
        self.assertEqual(found, keydetect.fingerprint("sk-othervalue"))

    def test_no_row_is_a_bare_true(self):
        """Not one row may be marked proved without something having run."""
        ctx = bootstrap.Context()
        ctx.repo = self.repo
        rows = bootstrap.readiness(ctx)
        for label, mark, detail in rows:
            self.assertIn(mark, (True, False, None), label)
            if mark is True:
                self.assertTrue(detail, label + " is true with no detail")


class TestTheKeyChoice(LadderCase):
    """F7: keep, replace, or remove — a choice, not a re-entry."""

    def configure(self, value="sk-cp-somethinglongenoughtolooklikeakey"):
        (self.repo / ".env").write_text(
            "# GENESIS — environment\n"
            "MINIMAX_API_KEY=" + value + "\n"
            "OTHER=keepme\n")

    def test_remove_key_takes_the_credential_out_and_leaves_the_rest(self):
        self.configure()
        code, out = self.run_bootstrap("--wizard", "--remove-key")
        self.assertEqual(code, 0, out)
        text = (self.repo / ".env").read_text()
        self.assertNotIn("MINIMAX_API_KEY", text)
        self.assertIn("OTHER=keepme", text)
        self.assertIn("# GENESIS — environment", text)

    def test_remove_key_on_a_machine_with_no_credential_refuses(self):
        code, out = self.run_bootstrap("--wizard", "--remove-key")
        self.assertEqual(code, bootstrap.EXIT_PRECONDITION)
        self.assertIn("no credential to remove", out)

    def test_a_configured_machine_refuses_to_ask_again_unattended(self):
        """It will not prompt for a key it cannot see, and it will not guess."""
        self.configure()
        code, out = self.run_bootstrap("--wizard", "--dry-run")
        self.assertEqual(code, bootstrap.EXIT_PRECONDITION)
        self.assertIn("already configured", out)
        self.assertIn("--keep-key", out)

    def test_keep_key_uses_the_one_that_is_there(self):
        self.configure()
        code, out = self.run_bootstrap("--wizard", "--dry-run", "--keep-key",
                                       "--gateway-url", "http://127.0.0.1:1")
        # the key does not authenticate anywhere, so it refuses — but it
        # refused at the GATEWAY, not at the prompt
        self.assertEqual(code, bootstrap.EXIT_KEY_REJECTED)
        self.assertNotIn("no key is configured here", out)


class TestTheTailnetKey(LadderCase):
    """F4: the tailnet key is asked for once, and then it is not asked again."""

    def test_a_stored_tailnet_key_is_used_without_a_prompt(self):
        stored = bootstrap.auth_key_path()
        stored.parent.mkdir(parents=True, exist_ok=True)
        stored.write_text("tskey-secretvalue\n")
        ctx = bootstrap.Context()
        ctx.repo = self.repo
        value = bootstrap.read_tailnet_key(ctx)
        self.assertEqual(value, "tskey-secretvalue")
        self.assertTrue(ctx.tailnet_key_stored)

    def test_no_stored_key_and_no_terminal_is_a_precondition_failure(self):
        ctx = bootstrap.Context()
        ctx.repo = self.repo
        with self.assertRaises(bootstrap.Refusal) as caught:
            bootstrap.read_tailnet_key(ctx)
        self.assertEqual(caught.exception.code, bootstrap.EXIT_PRECONDITION)


    def test_switching_providers_is_not_a_conflict(self):
        """A credential file is the wizard's to write; git is the guard."""
        (self.repo / ".env").write_text(
            "MINIMAX_API_KEY=sk-cp-oldvaluehere\nOTHER=keepme\n")
        ctx = bootstrap.Context()
        ctx.repo = self.repo
        ctx.provider = "anthropic"
        ctx.provider_env_var = "ANTHROPIC_API_KEY"
        ctx.key_value = "sk-ant-newvaluehere"
        ctx.key_fingerprint = "abc123"
        manifest = {}
        actions = [bootstrap._action(
            "genesis-env", "file", self.repo / ".env",
            {"present": True}, {"present": True}, "update",
            content=bootstrap._env_content(ctx), credential=True,
            env_var="ANTHROPIC_API_KEY")]
        applied, failed, manual, conflicted = bootstrap.apply_plan(
            ctx, actions, manifest)
        self.assertFalse(conflicted, "switching providers must not conflict")
        self.assertEqual(len(failed), 0)
        text = (self.repo / ".env").read_text()
        self.assertIn("ANTHROPIC_API_KEY=sk-ant-newvaluehere", text)
        self.assertIn("OTHER=keepme", text)


    def test_a_credential_left_behind_in_the_env_is_reported(self):
        """The wizard writes one line and leaves the rest — but not silently."""
        (self.repo / ".env").write_text(
            "OPENROUTER_API_KEY=sk-or-v1-somethinglongenoughtolook\n"
            "MINIMAX_API_KEY=sk-cp-theverifiedkeyvalue\n")
        ctx = bootstrap.Context()
        ctx.repo = self.repo
        ctx.provider_env_var = "MINIMAX_API_KEY"
        found = bootstrap._other_credentials(ctx)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0], "OPENROUTER_API_KEY")
        self.assertNotIn("sk-or-v1-somethinglongenoughtolook", str(found))

    def test_nothing_is_reported_when_the_env_holds_only_this_key(self):
        (self.repo / ".env").write_text(
            "MINIMAX_API_KEY=sk-cp-theverifiedkeyvalue\nOTHER=1\n")
        ctx = bootstrap.Context()
        ctx.repo = self.repo
        ctx.provider_env_var = "MINIMAX_API_KEY"
        self.assertEqual(bootstrap._other_credentials(ctx), [])


class TestThePlanHash(LadderCase):

    def test_the_hash_changes_with_the_plan(self):
        profile = {"profile": "macos"}
        actions = [{"id": "a", "kind": "file", "target": "/x",
                    "desired": {"present": True}, "effect": "create"}]
        first = receipt_mod.plan_hash(profile, actions, "abc")
        same = receipt_mod.plan_hash(profile, actions, "abc")
        self.assertEqual(first, same)
        other_key = receipt_mod.plan_hash(profile, actions, "def")
        self.assertNotEqual(first, other_key)
        other_profile = receipt_mod.plan_hash({"profile": "arch"}, actions, "abc")
        self.assertNotEqual(first, other_profile)

    def test_the_receipt_carries_no_value(self):
        profile = {"profile": "macos"}
        actions = [{"id": "a", "kind": "file", "target": "/x",
                    "desired": {"present": True}, "effect": "create"}]
        receipt = receipt_mod.new_receipt(profile, actions, "standard", "abc123")
        text = json.dumps(receipt)
        self.assertNotIn("/Users/", text)
        self.assertNotIn("sk-", text)
        self.assertEqual(receipt["key_fingerprint"], "abc123")


if __name__ == "__main__":
    unittest.main(verbosity=2)
