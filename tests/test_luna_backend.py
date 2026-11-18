"""GENESIS — the luna backend: chat over the tailnet, via the shim.

`genesis codexify` provisions and records a model server; the `luna`
backend is the chat half riding on that runtime — over the tailnet, not
localhost, because the machine that provisions it is not always the
machine that talks on it. The broker is OpenAI-shaped, so the backend goes
through the shim (src/shim.py) with `shim_upstream` pointing at the Mac
Studio's tailnet address.

Two things must hold, and both are here because neither is obvious from
the table alone:

  * **The note.** A person who chats on `luna` before running
    `genesis codexify` is told so, in the banner and when the substrate
    fails — not left to read "unreachable" as a mystery.
  * **The shim.** One port holds one upstream, and 8790 already belongs
    to whatever the wizard installed. The harness starts its own shim
    pointed at the tailnet broker only when nothing answers on the port;
    a listener that is already there is left alone.

    python3 -m unittest discover -s tests -v
    python3 tests/test_luna_backend.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import chat   # noqa: E402
import harness  # noqa: E402


class TestTheLunaBackend(unittest.TestCase):

    def test_the_upstream_is_the_tailnet_not_localhost(self):
        """"Over Tailscale" is the requirement; the table must say so."""
        backend = harness.BACKENDS["luna"]
        self.assertTrue(backend.get("needs_shim"))
        upstream = backend["shim_upstream"]
        self.assertIn("100.71.144.117", upstream)
        self.assertNotIn("127.0.0.1", upstream)
        self.assertNotIn("localhost", upstream)

    def test_the_upstream_and_model_are_the_codexify_default(self):
        """Without an install, the fallback is codexify's own default —
        one decision, not two."""
        import codexify
        base, model = harness.luna_runtime()
        self.assertEqual(base, codexify.DEFAULT_REMOTE_RUNTIME_ENDPOINT)
        self.assertEqual(model, codexify.DEFAULT_REMOTE_RUNTIME_MODEL)

    def test_the_backend_sends_no_tools(self):
        """"The tune's template collapses under system-plus-tools —
        deterministically, per the probe — so the backend that rides on
        it never offers the shape: no wasted max_tokens generation of
        marker spam, no second generation for the real answer."""
        self.assertTrue(harness.BACKENDS["luna"].get("no_tools"))

    def test_talk_sends_no_tools_on_a_no_tools_backend(self):
        """The flag is honored in the request, and the grounding nudge
        does not fire — a substrate with no tools on offer cannot be
        asked to call one."""
        seen = []

        def fake_post(payload, backend="luna"):
            seen.append(payload)
            return {"content": [{"type": "text",
                                 "text": "The node is ready."}]}

        with unittest.mock.patch.object(harness, "_post",
                                        side_effect=fake_post), \
             unittest.mock.patch.object(harness, "system_prompt",
                                        return_value="the card"), \
             unittest.mock.patch.object(harness, "ensure_shim"):
            out = harness.talk("what does the record say about the arc",
                               backend="luna")
        self.assertEqual(out, "The node is ready.")
        self.assertEqual(len(seen), 1, "the nudge did not fire")
        self.assertNotIn("tools", seen[0])

    def test_a_recorded_runtime_wins_over_the_default(self):
        """The install is context: its `.env` decides where chat talks.
        A recorded endpoint and model must be read, not overridden."""
        import tempfile
        import unittest.mock
        with tempfile.TemporaryDirectory() as tmp:
            install = Path(tmp) / "codexify-core"
            install.mkdir()
            (install / ".env").write_text(
                "CODEXIFY_RUNTIME_ENDPOINT=http://100.64.0.1:8000/v1\n"
                "CODEXIFY_MODEL=other-model\n", encoding="utf-8")
            with unittest.mock.patch.object(harness, "_codexify_candidates",
                                            return_value=[install]):
                base, model = harness.luna_runtime()
        self.assertEqual(base, "http://100.64.0.1:8000/v1")
        self.assertEqual(model, "other-model")


class TestTheNote(unittest.TestCase):

    def setUp(self):
        class _Style:
            soft = staticmethod(lambda s: s)
            def banner(self, *parts):
                return "\n".join(str(p) for p in parts)
        self.style = _Style()

    def _banner(self):
        return chat._banner(self.style, "luna", harness)

    def test_the_note_names_the_command_that_was_skipped(self):
        """The note is a pointer, not a refusal: it names `genesis
        codexify`, and it appears when there is no install."""
        self.assertIn("genesis codexify", harness.CODEXIFY_NOTE)
        with unittest.mock.patch.object(harness, "codexify_installed",
                                        return_value=False):
            self.assertIn(harness.CODEXIFY_NOTE, self._banner())

    def test_no_note_when_codexify_is_installed(self):
        with unittest.mock.patch.object(harness, "codexify_installed",
                                        return_value=True):
            self.assertNotIn(harness.CODEXIFY_NOTE, self._banner())

    def test_no_note_on_other_backends(self):
        """The note is the luna backend's; mlx is nobody's business here."""
        with unittest.mock.patch.object(harness, "codexify_installed",
                                        return_value=False):
            self.assertNotIn(harness.CODEXIFY_NOTE,
                             chat._banner(self.style, "mlx", harness))

    def test_installed_reads_the_real_install(self):
        """The detection follows codexify's own definition — the install
        and its `.env` — in the same candidate order."""
        self.assertEqual(
            harness.codexify_installed(),
            any(
                (p / "docker-compose.yml").is_file()
                and (p / ".env.template").is_file()
                and (p / ".env").is_file()
                for p in (ROOT / "codexify-core", ROOT.parent / "codexify-core",
                          Path.home() / "codexify-core")
            ))


class TestTheShimLifecycle(unittest.TestCase):

    def test_an_existing_listener_is_left_alone(self):
        """8790 already belongs to the wizard's shim on some machines.
        The harness must use it, not kill it and replace it."""
        import http.server
        import threading

        class _Answers(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - base class name
                self.send_response(404)
                self.end_headers()
            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), _Answers)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with unittest.mock.patch.dict(
                    harness.BACKENDS["luna"],
                    {"url": f"http://127.0.0.1:{port}/messages"}):
                harness.ensure_shim("luna")
            self.assertIsNone(harness._SHIM["proc"],
                              "the harness started a shim over a live port")
        finally:
            server.shutdown()

    def test_no_shim_without_a_port(self):
        """A shim started for nothing, at teardown or on a dead backend,
        must never outlive the process that started it."""
        harness._SHIM.update(proc=None, port=None, upstream=None)
        harness.stop_shim()  # nothing started: a no-op, not a crash
        self.assertIsNone(harness._SHIM["proc"])


class TestTemplateCollapse(unittest.TestCase):
    """The supergemma tune on the tailnet broker answers a system-plus-
    tools request with pages of `<|channel>` markers — the probe pinned it
    (tools/luna_probe.py): bare hello ok, system alone ok, one tool ok,
    the full list SPAM. The harness must not return the markers as
    speech."""

    SPAM = ("<|channel>thought\n<channel|>" * 20)
    REPLY = {"content": [{"type": "text", "text": "The node is ready."}]}

    def _talk(self, replies):
        seen = []

        def fake_post(payload, backend="luna"):
            seen.append(payload)
            return replies[min(len(seen) - 1, len(replies) - 1)]()

        with unittest.mock.patch.object(harness, "_post",
                                        side_effect=fake_post), \
             unittest.mock.patch.object(harness, "system_prompt",
                                        return_value="the card"), \
             unittest.mock.patch.object(harness, "ensure_shim"):
            # openai, not luna: this class tests the retry machinery,
            # which belongs to every tools-sending backend. luna is marked
            # no_tools — it never sends the shape that triggers the spam,
            # so it cannot reach the retry.
            out = harness.talk("Hello", backend="openai")
        return out, seen

    def test_marker_spam_is_detected(self):
        self.assertTrue(harness._is_marker_spam(self.SPAM))
        self.assertFalse(harness._is_marker_spam("a plain reply"))
        self.assertFalse(harness._is_marker_spam("one <|channel> in prose"))
        self.assertFalse(harness._is_marker_spam("a<|channel>b<|channel>c"))
        self.assertTrue(harness._is_marker_spam("a<|channel>b<|channel>c<|channel>d"))

    def test_a_spamming_substrate_is_retried_without_tools(self):
        """The first reply loops; the same question goes back out with no
        tools array, and the second reply is returned as speech."""
        out, seen = self._talk([
            lambda: {"content": [{"type": "text", "text": self.SPAM}]},
            lambda: self.REPLY,
        ])
        self.assertEqual(out, "The node is ready.")
        self.assertEqual(len(seen), 2)
        self.assertIn("tools", seen[0])
        self.assertNotIn("tools", seen[1])

    def test_a_substrate_that_spams_without_tools_says_so(self):
        """Both replies loop: the answer is a plain statement, not a page
        of markers handed to the operator as Genesis's words."""
        out, _ = self._talk([
            lambda: {"content": [{"type": "text", "text": self.SPAM}]},
            lambda: {"content": [{"type": "text", "text": self.SPAM}]},
        ])
        self.assertIn("cannot hold its template", out)
        self.assertNotIn("<|channel>", out)

    def test_the_poisoned_turn_never_enters_the_history(self):
        """A marker page left in the transcript poisons every turn after
        it. The retry sends the same conversation, minus nothing — the
        spam reply was never appended."""
        messages = []
        out, seen = self._talk([
            lambda: {"content": [{"type": "text", "text": self.SPAM}]},
            lambda: self.REPLY,
        ])
        self.assertEqual(out, "The node is ready.")
        # The retried request carries exactly the user's turn: the spam
        # reply was dropped, the user message was not duplicated.
        sent = seen[1]["messages"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["role"], "user")


import unittest.mock  # noqa: E402  — used above; kept at the foot with the
                       # stdlib-only posture of the rest of the suite.


if __name__ == "__main__":
    unittest.main()
