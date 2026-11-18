"""GENESIS — the backend vocabulary, in one place.

There is one table of backends (`src/harness.py:BACKENDS`) and two ways to
name one: the global `--backend` flag and the `chat` command's own flag.
When they were separate lists, the wizard wrote a node profile naming a
backend the CLI refused — "the wizard names a backend the CLI rejects".

So these tests hold the two lists to the one table, and they hold each
backend's **auth style** to what its endpoint actually wants: a probe that
hard-codes `x-api-key` answers 401 against a perfectly good key and reads
as "the key is wrong".

    python3 -m unittest discover -s tests -v
    python3 tests/test_backends.py
"""
from __future__ import annotations

import importlib.machinery
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import harness  # noqa: E402


class TestTheOneTable(unittest.TestCase):

    def test_the_chat_flag_accepts_every_backend_the_harness_knows(self):
        """F2: the CLI must not refuse a name the wizard writes."""
        result = subprocess.run(
            [sys.executable, str(ROOT / "genesis"), "--plain", "chat",
             "--help"], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in harness.BACKENDS:
            self.assertIn(name, result.stdout,
                          "the chat flag does not offer " + name)

    def test_the_two_lists_are_one_list(self):
        """The global flag and the command flag read the same table."""
        import importlib.util
        spec = importlib.util.spec_from_loader(
            "genesis_cli", importlib.machinery.SourceFileLoader(
                "genesis_cli", str(ROOT / "genesis")))
        module = importlib.util.module_from_spec(spec)
        sys.modules["genesis_cli"] = module
        spec.loader.exec_module(module)
        self.assertEqual(tuple(harness.BACKENDS), module._backend_choices())

    def test_every_backend_names_a_key_variable_and_an_auth_style(self):
        for name, backend in harness.BACKENDS.items():
            self.assertIn("key", backend, name)
            self.assertIn("model", backend, name)
            if backend["key"] == "mlxflex":
                # The keyless sentinel, not a key variable: the local
                # server (mlx) and the tailnet broker behind the luna
                # backend both need no key from this machine.
                continue
            self.assertIn(backend["auth"], ("x-api-key", "bearer"), name)
            self.assertTrue(backend["key"].isupper(), name)

    def test_the_auth_style_is_the_endpoint_s_own(self):
        """The Anthropic-shaped routes take x-api-key; OpenAI-shaped take Bearer.

        The `openai` backend talks to the **shim**, which is Anthropic-
        shaped on this side — so `x-api-key` is right for it. The Bearer
        that OpenAI itself wants is the shim's business, not the harness's.
        """
        expected = {
            "anthropic": "x-api-key",
            "minimax": "bearer",        # measured: MiniMax documents Bearer
            "openrouter": "x-api-key",  # measured: OpenRouter takes either
            "openai": "x-api-key",      # the shim on this side is Anthropic
            "mlx": "x-api-key",
        }
        for name, style in expected.items():
            self.assertEqual(harness.BACKENDS[name]["auth"], style, name)

    def test_the_openai_backend_goes_through_the_shim(self):
        """Claude Code speaks three protocols, and OpenAI is not one of them."""
        backend = harness.BACKENDS["openai"]
        self.assertTrue(backend.get("needs_shim"))
        self.assertIn("127.0.0.1", backend["url"])

    def test_the_provider_names_match_the_backend_names(self):
        """The wizard writes a provider name; the CLI takes a backend name.

        For every provider the wizard can detect, the two are the same
        word — otherwise the node profile names something the CLI does not
        know.
        """
        sys.path.insert(0, str(ROOT / "tools"))
        import keydetect
        for provider in keydetect.PROVIDER_CHOICES:
            self.assertIn(provider, harness.BACKENDS,
                          "the wizard detects " + provider
                          + " but the harness has no such backend")


class TestTheOpenAIShapedBranch(unittest.TestCase):
    """The branch nobody ran, run at last.

    Every key this machine holds is Anthropic-shaped, so the OpenAI-shaped
    branch — where the shim is required — never executed, and two HIGH
    defects lived there: the wizard installed the shim and never pointed
    anything at it, and the macOS unit had no upstream so the shim died on
    birth.

    **A branch nobody runs is a branch that does not work.** These tests
    run it, against a synthetic OpenAI-shaped endpoint, with no network
    beyond the loopback.
    """

    def setUp(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import keydetect
        import bootstrap
        self.keydetect = keydetect
        self.bootstrap = bootstrap
        self.server = _OpenAIServer()
        self.server.start()
        self.addCleanup(self.server.stop)

    def _run_the_wizard(self, tmp_home, tmp_repo):
        import os
        old = os.environ.get("HOME")
        os.environ["HOME"] = str(tmp_home)
        try:
            ctx = self.bootstrap.Context()
            ctx.repo = tmp_repo
            ctx.wizard = True
            ctx.dry_run = True
            ctx.key_file = str(tmp_repo / "key.txt")
            (tmp_repo / "key.txt").write_text("sk-test-openai-key-value\n")
            ctx.gateway_url = f"http://127.0.0.1:{self.server.port}"
            ctx.model = "gpt-4.1-mini"
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                winner = self.bootstrap.verify_key(
                    ctx, "sk-test-openai-key-value")
            self.assertEqual(winner.endpoint.shape, self.keydetect.OPENAI)
            actions = self.bootstrap.build_plan(ctx, {"profile": "macos"})
            return ctx, actions
        finally:
            if old is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old

    def test_claude_code_is_pointed_at_the_shim_on_the_openai_path(self):
        """F8: the shim is installed — something must use it."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = Path(tmp) / "repo"
            home.mkdir()
            repo.mkdir()
            ctx, actions = self._run_the_wizard(home, repo)
            settings = [a for a in actions
                        if a.get("id") == "claude-settings"]
            self.assertEqual(len(settings), 1)
            content = settings[0]["content"]
            self.assertIn("ANTHROPIC_BASE_URL", content)
            self.assertIn(f"http://127.0.0.1:{ctx.shim_port}", content)
            self.assertIn("apiKeyHelper", content)

    def test_the_node_profile_records_the_substrate_it_chose(self):
        """F8's second consequence: substrate was null for OpenAI-shaped."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = Path(tmp) / "repo"
            home.mkdir()
            repo.mkdir()
            ctx, _ = self._run_the_wizard(home, repo)
            profile = self.bootstrap._node_profile_content(
                ctx, {"profile": "macos"})
            self.assertIn("http://127.0.0.1:%d" % ctx.shim_port, profile)
            self.assertNotIn('"substrate": null', profile)

    def test_the_shim_unit_carries_the_upstream_on_both_platforms(self):
        """F9: the systemd unit had it and the macOS plist did not."""
        import tempfile
        for system in ("Darwin", "Linux"):
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp) / "home"
                repo = Path(tmp) / "repo"
                home.mkdir()
                repo.mkdir()
                ctx, _ = self._run_the_wizard(home, repo)
                original = self.bootstrap.platform.system
                self.bootstrap.platform.system = lambda: system
                try:
                    content = self.bootstrap._shim_unit_content(ctx)
                finally:
                    self.bootstrap.platform.system = original
                self.assertIn("GENESIS_SHIM_UPSTREAM", content, system)
                self.assertIn(ctx.gateway_endpoint.base, content, system)

    def test_the_shim_unit_carries_the_port_and_the_credential_variable(self):
        """Sec 4.4: the service carries upstream, port and credential."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = Path(tmp) / "repo"
            home.mkdir()
            repo.mkdir()
            ctx, _ = self._run_the_wizard(home, repo)
            content = self.bootstrap._shim_unit_content(ctx)
            self.assertIn(str(ctx.shim_port), content)
            self.assertIn("GENESIS_SHIM_KEY", content)
            self.assertIn(ctx.provider_env_var, content)


class _OpenAIServer:
    """A gateway that speaks OpenAI and not Anthropic.

    It answers `/v1/chat/completions` with an OpenAI body and 404s
    `/v1/messages` — the shape that requires the shim.
    """

    def __init__(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.json = json
        self.threading = threading
        handler = _make_openai_handler(self.json)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def _make_openai_handler(json_module):
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            path = self.path.split("?")[0].rstrip("/")
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            if path.endswith("/messages"):
                # The Anthropic route does not exist here. That is what
                # makes this endpoint need the shim.
                payload = {"error": "not found"}
                status = 404
            elif path.endswith("/chat/completions"):
                payload = {"choices": [{"message": {"content": "pong"}}]}
                status = 200
            else:
                payload = {"error": "not found"}
                status = 404
            raw = json_module.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return Handler


if __name__ == "__main__":
    unittest.main(verbosity=2)
