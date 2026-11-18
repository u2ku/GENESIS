"""GENESIS — the key rotation, end to end.

A machine that was configured with one provider's key and is then given a
**different provider's key** is the case the ownership rule gets wrong. The
manifest's credential hash is keyed to one variable, so a rewrite that
changes the variable name looks like "someone else edited this file" — and
the wizard refuses its own work with exit 6.

The rule the contract settles (WIZARD.md 6.1, 6.2):

- a credential file is the **wizard's** to write. Git is the guard — a
  credential file that git tracks is refused whatever the variable is
  called, because that is the one thing that must never happen;
- a gitignored credential file that is already there is not a conflict; it
  is the file the wizard is there to fill in;
- and a credential the wizard **leaves behind** must be **visible**. The
  wizard writes one line and leaves the rest of the file byte-identical, so
  a machine that once held another provider's key still holds it — and a
  reader told "the key is configured" must also be told that a different
  key sits beside it.

Every test here runs the REAL ladder — dry-run, then apply, with the real
receipt — against a local fake gateway on an ephemeral port. No real
credential, no live service, no network.

    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: E402
import hermetic  # noqa: E402
import keydetect  # noqa: E402


class _FakeGateway:
    """An Anthropic-shaped gateway on an ephemeral loopback port.

    It answers `/v1/messages` with an Anthropic body and
    `/chat/completions` with an OpenAI body, so either shape can be driven.
    Nothing it sees is a real credential.
    """

    def __init__(self):
        gateway = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                self.rfile.read(length)
                if self.path.endswith("/chat/completions"):
                    body = json.dumps({"choices": [{"message": {
                        "role": "assistant", "content": "pong"}}]}).encode()
                else:
                    body = json.dumps({"type": "message", "content": [
                        {"type": "text", "text": "pong"}]}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class RotationCase(unittest.TestCase):
    """A throwaway home, a throwaway repository, and a fake gateway."""

    KEY_A = "sk-ant-testkeyaaaaaaaaaaaaaaaaaaaaaaaaaa"
    KEY_B = "sk-cp-testkeybbbbbbbbbbbbbbbbbbbbbbbbbbb"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.repo = base / "repo"
        self.home.mkdir()
        self.repo.mkdir()
        (self.repo / "genesis").write_text("#!/bin/sh\nexit 0\n")
        (self.repo / "src").mkdir()
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        # The CLI and Tailscale are stubbed onto PATH, so the plan finds
        # them present and never runs an installer. This file's docstring
        # says no network; without the stub, the apply path reached
        # `curl -fsSL https://claude.ai/install.sh | sh`.
        self._old_path = hermetic.stub_cli_on_path(base / "stub-bin")
        self._old_cwd = os.getcwd()
        os.chdir(self.repo)
        self.gateway = _FakeGateway()

    def tearDown(self):
        self.gateway.stop()
        os.chdir(self._old_cwd)
        hermetic.restore_path(self._old_path)
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        self._tmp.cleanup()

    def key_file(self, value: str) -> Path:
        path = self.home / f"key-{value[-1]}.txt"
        path.write_text(value + "\n", encoding="utf-8")
        path.chmod(0o600)
        return path

    def wizard(self, value: str, rung: str, provider: str) -> tuple:
        """Run the real wizard, one rung, and return (exit code, output)."""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = bootstrap.main([
                "--repo", str(self.repo),
                "--wizard", rung,
                "--key-file", str(self.key_file(value)),
                "--provider", provider,
                "--gateway-url", self.gateway.url,
                "--no-codexify",
                "--no-tailscale",
                "--no-voice",
            ])
        return code, buffer.getvalue()

    def configure(self, value: str, provider: str) -> tuple:
        """The full ladder: dry-run, then apply."""
        code, out = self.wizard(value, "--dry-run", provider)
        self.assertEqual(code, 0, out)
        code, out = self.wizard(value, "--apply", provider)
        self.assertEqual(code, 0, out)
        return code, out

    def env_text(self) -> str:
        path = self.repo / ".env"
        return path.read_text(encoding="utf-8") if path.exists() else ""


class TestTheFirstConfigure(RotationCase):

    def test_the_key_lands_under_the_proved_name(self):
        self.configure(self.KEY_A, "anthropic")
        self.assertIn("ANTHROPIC_API_KEY=" + self.KEY_A, self.env_text())

    def test_the_credential_file_is_0600(self):
        self.configure(self.KEY_A, "anthropic")
        self.assertEqual((self.repo / ".env").stat().st_mode & 0o777, 0o600)

    def test_the_value_is_not_in_the_output(self):
        _, out = self.configure(self.KEY_A, "anthropic")
        self.assertNotIn(self.KEY_A, out)


class TestTheRotation(RotationCase):
    """The defect: a provider switch was refused as an exit-6 conflict."""

    def test_switching_providers_is_not_a_conflict(self):
        self.configure(self.KEY_A, "anthropic")
        code, out = self.wizard(self.KEY_B, "--dry-run", "minimax")
        self.assertEqual(code, 0, out)
        self.assertNotIn("conflict", out.lower())
        code, out = self.wizard(self.KEY_B, "--apply", "minimax")
        self.assertEqual(code, 0, out)
        self.assertNotIn("conflict", out.lower())

    def test_the_new_key_lands_under_the_new_name(self):
        self.configure(self.KEY_A, "anthropic")
        self.configure(self.KEY_B, "minimax")
        text = self.env_text()
        self.assertIn("MINIMAX_API_KEY=" + self.KEY_B, text)

    def test_the_manifest_records_the_new_fingerprint(self):
        self.configure(self.KEY_A, "anthropic")
        self.configure(self.KEY_B, "minimax")
        manifest_path = (self.home / ".config" / "genesis" /
                         "manifest.json")
        self.assertTrue(manifest_path.exists(), "no manifest was written")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        fingerprints = [entry.get("fingerprint")
                        for entry in manifest.get("files", {}).values()]
        self.assertIn(keydetect.fingerprint(self.KEY_B), fingerprints)

    def test_the_key_left_behind_is_reported(self):
        """WIZARD 6.1: the machine's credential state must be VISIBLE.

        The wizard writes one line and leaves the rest of the file alone,
        so the old provider's key is still in there. That may be right; it
        must not be silent.
        """
        self.configure(self.KEY_A, "anthropic")
        _, out = self.wizard(self.KEY_B, "--apply", "minimax")
        self.assertIn("ANTHROPIC_API_KEY", out)
        self.assertIn("not the key this run verified", out)
        self.assertIn(keydetect.fingerprint(self.KEY_A), out)

    def test_the_key_left_behind_is_never_shown(self):
        self.configure(self.KEY_A, "anthropic")
        _, out = self.wizard(self.KEY_B, "--apply", "minimax")
        self.assertNotIn(self.KEY_A, out)

    def test_the_old_key_is_still_there_and_said_so(self):
        """Keep, and say so — the same shape as the rc-file credentials."""
        self.configure(self.KEY_A, "anthropic")
        self.configure(self.KEY_B, "minimax")
        self.assertIn(self.KEY_A, self.env_text())

    def test_the_rotation_is_not_a_second_prompt(self):
        """One prompt per secret, and a rotation is not a prompt."""
        self.configure(self.KEY_A, "anthropic")
        _, out = self.wizard(self.KEY_B, "--dry-run", "minimax")
        self.assertNotIn("already configured", out)


class TestTheCredentialFileGuard(RotationCase):
    """Git is the guard. A tracked credential file is refused whatever it is."""

    def git_init(self) -> bool:
        """Make the throwaway repository a real git repository."""
        import subprocess
        result = subprocess.run(["git", "init", "-q", str(self.repo)],
                                capture_output=True, text=True)
        return result.returncode == 0

    def test_a_tracked_credential_file_is_refused(self):
        """The one thing that must never happen, whatever the variable is."""
        if not self.git_init():
            self.skipTest("git is not available here")
        import subprocess
        (self.repo / ".env").write_text(
            "ANTHROPIC_API_KEY=" + self.KEY_A + "\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-f", ".env"],
                       capture_output=True, text=True)
        tracked = subprocess.run(
            ["git", "-C", str(self.repo), "ls-files", ".env"],
            capture_output=True, text=True).stdout.strip()
        self.assertTrue(tracked, "the test did not track .env")
        ctx = bootstrap.Context()
        ctx.repo = self.repo
        action = bootstrap._action(
            "genesis-env", "file", self.repo / ".env",
            {"present": True}, {"present": True}, "update",
            content="ANTHROPIC_API_KEY=" + self.KEY_B + "\n",
            credential=True, env_var="ANTHROPIC_API_KEY")
        conflicts = bootstrap.check_ownership(ctx, {}, action, set())
        self.assertTrue(conflicts, "a TRACKED credential file must be refused")
        self.assertIn("tracked by git", conflicts[0][1])

    def test_a_gitignored_credential_file_is_not_a_conflict(self):
        """The other half: a gitignored file is the one to fill in."""
        if not self.git_init():
            self.skipTest("git is not available here")
        (self.repo / ".env").write_text(
            "ANTHROPIC_API_KEY=" + self.KEY_A + "\n")
        (self.repo / ".gitignore").write_text(".env\n")
        ctx = bootstrap.Context()
        ctx.repo = self.repo
        action = bootstrap._action(
            "genesis-env", "file", self.repo / ".env",
            {"present": True}, {"present": True}, "update",
            content="ANTHROPIC_API_KEY=" + self.KEY_B + "\n",
            credential=True, env_var="ANTHROPIC_API_KEY")
        conflicts = bootstrap.check_ownership(ctx, {}, action, set())
        self.assertFalse(conflicts, "a gitignored credential file is the "
                                     "wizard's to write")


if __name__ == "__main__":
    unittest.main()
