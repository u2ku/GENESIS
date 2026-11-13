"""GENESIS — provider detection tests.

The rule these hold: **a prefix says where to look, and only the gateway
says whether it works.** The detection table is therefore tested as a table
of shortlists, and the probe is tested against a real HTTP server that
answers the ways a gateway answers — including the ways that are not proof.

    python3 -m unittest discover -s tests -v
    python3 tests/test_keydetect.py
"""
from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import keydetect  # noqa: E402


# A gateway that answers the way gateways answer.
class _Handler(BaseHTTPRequestHandler):
    mode = "anthropic"
    seen = []

    def log_message(self, *args):  # silence the test run
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8")
        _Handler.seen.append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": json.loads(body) if body else {},
        })
        mode = _Handler.mode
        if mode == "anthropic":
            payload = {"type": "message", "role": "assistant",
                       "content": [{"type": "text", "text": "pong"}]}
            status = 200
        elif mode == "openai":
            payload = {"choices": [{"message": {"content": "pong"}}]}
            status = 200
        elif mode == "wrong-body":
            # 200 with a body that does not prove a model answered.
            payload = {"ok": True, "note": "hello"}
            status = 200
        elif mode == "refused":
            payload = {"type": "error", "error": {"type": "authentication_error",
                                                  "message": "Invalid API key"}}
            status = 401
        elif mode == "no-route":
            payload = {"error": "not found"}
            status = 404
        else:
            payload = {"error": "unknown"}
            status = 500
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class TestTheFormatTable(unittest.TestCase):
    """The prefix table, and what it is allowed to claim."""

    def test_the_documented_prefixes_route_as_specified(self):
        self.assertEqual(keydetect.sniff("sk-ant-api03-abc").provider, "anthropic")
        self.assertEqual(keydetect.sniff("sk-or-v1-abc").provider, "openrouter")
        self.assertEqual(keydetect.sniff("sk-cp-Yabc").provider, "minimax")
        self.assertEqual(keydetect.sniff("sk-proj-abc").provider, "openai")
        self.assertEqual(keydetect.sniff("sk-abc").provider, "openai")

    def test_the_longest_prefix_wins(self):
        """`sk-ant-` and `sk-or-` both begin with `sk-`."""
        self.assertEqual(keydetect.sniff("sk-ant-api03-abc").prefix, "sk-ant-")
        self.assertEqual(keydetect.sniff("sk-or-v1-abc").prefix, "sk-or-")
        self.assertEqual(keydetect.sniff("sk-cp-Yabc").prefix, "sk-cp-")

    def test_an_unknown_format_asks_for_the_provider(self):
        guess = keydetect.sniff("not-a-key-at-all")
        self.assertEqual(guess.source, "unknown")
        self.assertEqual(guess.candidates, ())
        self.assertIn("no known provider", guess.note)

    def test_the_sk_cp_family_walks_minimax_then_openrouter(self):
        """`sk-cp-` is MiniMax, but the bucket is a family."""
        guess = keydetect.sniff("sk-cp-Yabc")
        providers = [e.provider for e in guess.candidates]
        self.assertEqual(providers[0], "minimax")
        self.assertIn("openrouter", providers)

    def test_the_override_beats_the_format(self):
        guess = keydetect.detect("sk-cp-Yabc", override="openrouter")
        self.assertEqual(guess.source, "override")
        self.assertEqual(guess.provider, "openrouter")
        self.assertEqual({e.provider for e in guess.candidates}, {"openrouter"})

    def test_an_unknown_override_is_refused(self):
        with self.assertRaises(ValueError):
            keydetect.detect("sk-cp-Yabc", override="not-a-provider")

    def test_each_provider_names_the_variable_its_key_belongs_under(self):
        """The wrong name is a 401 that looks like a bad key."""
        self.assertEqual(keydetect.PROVIDERS["anthropic"]["endpoints"][0].env_var,
                         "ANTHROPIC_API_KEY")
        self.assertEqual(keydetect.PROVIDERS["openai"]["endpoints"][0].env_var,
                         "OPENAI_API_KEY")
        self.assertEqual(keydetect.PROVIDERS["openrouter"]["endpoints"][0].env_var,
                         "OPENROUTER_API_KEY")
        self.assertEqual(keydetect.PROVIDERS["minimax"]["endpoints"][0].env_var,
                         "MINIMAX_API_KEY")

    def test_the_routes_are_the_documented_ones(self):
        for provider, entry in keydetect.PROVIDERS.items():
            for endpoint in entry["endpoints"]:
                if endpoint.shape == keydetect.ANTHROPIC:
                    self.assertTrue(endpoint.url.endswith("/v1/messages"),
                                    provider + ": " + endpoint.url)
                else:
                    self.assertTrue(endpoint.url.endswith("/chat/completions"),
                                    provider + ": " + endpoint.url)

    def test_the_claude_code_base_is_what_precedes_the_route(self):
        """`ANTHROPIC_BASE_URL` is everything before `/v1/messages`."""
        endpoint = keydetect.PROVIDERS["minimax"]["endpoints"][0]
        self.assertEqual(endpoint.claude_base, "https://api.minimax.io/anthropic")
        self.assertEqual(endpoint.url,
                         "https://api.minimax.io/anthropic/v1/messages")
        openrouter = keydetect.PROVIDERS["openrouter"]["endpoints"][0]
        self.assertEqual(openrouter.url, "https://openrouter.ai/api/v1/messages")


class TestTheFingerprint(unittest.TestCase):
    def test_it_is_twelve_hex_characters(self):
        print_ = keydetect.fingerprint("sk-ant-api03-secret")
        self.assertEqual(len(print_), 12)
        int(print_, 16)

    def test_the_same_key_gives_the_same_fingerprint(self):
        self.assertEqual(keydetect.fingerprint("sk-cp-abc"),
                         keydetect.fingerprint("sk-cp-abc"))

    def test_different_keys_give_different_fingerprints(self):
        self.assertNotEqual(keydetect.fingerprint("sk-cp-abc"),
                            keydetect.fingerprint("sk-cp-abd"))

    def test_it_does_not_contain_the_key(self):
        key = "sk-ant-api03-supersecretvalue"
        self.assertNotIn(key, keydetect.fingerprint(key))


class TestTheMask(unittest.TestCase):
    def test_a_key_is_masked(self):
        masked = keydetect.mask("the key is sk-ant-api03-abcdefghijklmnop")
        self.assertNotIn("sk-ant-api03", masked)
        self.assertIn("***", masked)

    def test_an_assignment_is_masked(self):
        masked = keydetect.mask("OPENROUTER_API_KEY=abcdef123456")
        self.assertNotIn("abcdef123456", masked)
        self.assertIn("OPENROUTER_API_KEY=***", masked)

    def test_a_secret_assignment_is_masked_even_without_a_key_shape(self):
        masked = keydetect.mask("MY_SERVICE_SECRET=hunter2")
        self.assertNotIn("hunter2", masked)

    def test_ordinary_text_is_untouched(self):
        text = "the node is ready and the tailnet is joined"
        self.assertEqual(keydetect.mask(text), text)


class TestTheProbe(unittest.TestCase):
    """The probe asks a real server, and asserts on the body."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def endpoint(self, shape=keydetect.ANTHROPIC, models=("m1",)):
        base = f"http://127.0.0.1:{self.port}"
        if shape == keydetect.ANTHROPIC:
            base += "/anthropic"
        else:
            base += "/v1"
        return keydetect.Endpoint("test", shape, base, "TEST_API_KEY",
                                  tuple(models), ("x-api-key", "bearer"))

    def test_an_anthropic_body_is_proof(self):
        _Handler.mode = "anthropic"
        result = keydetect.probe("sk-test", self.endpoint())
        self.assertTrue(result.ok)
        self.assertEqual(result.shape, keydetect.ANTHROPIC)

    def test_an_openai_body_is_proof(self):
        _Handler.mode = "openai"
        result = keydetect.probe("sk-test", self.endpoint(keydetect.OPENAI))
        self.assertTrue(result.ok)
        self.assertEqual(result.shape, keydetect.OPENAI)

    def test_a_200_with_the_wrong_body_is_not_proof(self):
        """The rule from WIZARD.md §5.3, held."""
        _Handler.mode = "wrong-body"
        result = keydetect.probe("sk-test", self.endpoint())
        self.assertFalse(result.ok)

    def test_a_401_is_a_refusal_not_a_reachability_failure(self):
        _Handler.mode = "refused"
        result = keydetect.probe("sk-test", self.endpoint())
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 401)
        self.assertIn("Invalid API key", result.detail)

    def test_a_404_is_a_route_that_is_not_there(self):
        _Handler.mode = "no-route"
        result = keydetect.probe("sk-test", self.endpoint())
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 404)

    def test_the_credential_travels_in_a_header_not_the_url(self):
        _Handler.mode = "anthropic"
        _Handler.seen.clear()
        keydetect.probe("sk-test-secret", self.endpoint())
        seen = _Handler.seen[-1]
        self.assertNotIn("sk-test-secret", seen["path"])
        self.assertNotIn("sk-test-secret", json.dumps(seen["body"]))
        self.assertTrue(any("sk-test-secret" in str(v)
                            for v in seen["headers"].values()))

    def test_the_probe_walks_the_candidates_until_one_answers(self):
        _Handler.mode = "anthropic"
        candidates = (self.endpoint(models=("m1",)),)
        results = keydetect.probe_candidates("sk-test", candidates)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok)

    def test_a_refused_candidate_does_not_stop_the_walk(self):
        """The family walk: the first place refuses, the second answers."""
        _Handler.mode = "anthropic"
        results = keydetect.probe_candidates("sk-test", (self.endpoint(),))
        self.assertTrue(results[-1].ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
