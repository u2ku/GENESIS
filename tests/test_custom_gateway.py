"""GENESIS — the custom gateway, and the two lies it could tell.

**Two defects, both found by running the ladder against a gateway the
operator names (`--gateway-url`), and both proven in a fresh clone of
the published tree.**

**1. A MODEL ERROR, REPORTED AS A KEY ERROR.** The wizard probes with a
model it chose (`gpt-4.1-mini` unless `--model` says otherwise). A
gateway that does not serve that model answers 400 *"model not found"* —
and the wizard's verdict logic reported that as **exit 8, "the key did
not authenticate"**. The key was never judged: the gateway never got as
far as the credential. The run refused a perfectly good key and pointed
the operator at the wrong thing.

`WIZARD.md` §5.3 already states the rule: *a 401 means the key is
wrong, which is a different failure from the gateway being down, and it
has a different fix.* A 400 about the model is a third thing, and it is
now read as what it is — and the operator is told how to proceed
(`--model ID`).

**2. THE SILENT SUBSTITUTION.** The node profile a custom gateway
produces names the backend `"custom"` — a name the harness has no entry
for. The CLI reads that profile to pick a substrate, accepts only the
keys of `harness.BACKENDS`, and therefore **silently keeps its default**.
The default is the local substrate, which answers **without a
credential** and carries the card as its system prompt: the conversation
looks entirely normal while the traffic goes somewhere the operator never
configured.

The fix is **the middot, an honest non-pass** — this project's own rule
is that nothing is claimed which was not proved (`docs/PORTABILITY.md`
§6.6, `docs/WIZARD.md` §3.7): a row that cannot be proved is a row that
failed, or a row that says it does not apply. So the CLI **does not
answer**: it says the middot, exits non-zero, and leaves the operator to
choose a substrate this build can speak. A loud warning in front of a
normal-looking answer would still be a success.

Standard library only. No network beyond the loopback, no real
credential, no live service.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import harness  # noqa: E402
import node as node_contract  # noqa: E402

# A key-shaped fixture that is obviously not a key. The tail says so in
# words, which is the convention the repository's own leak scan uses: a
# real credential's tail is high-entropy, and a fixture that reads like a
# sentence is not one. A literal like this in a tracked file is refused
# by the gate (and by GitHub's push protection), so the shape has to be
# unmistakably fake rather than merely unlikely.
FAKE_KEY = "sk-ant-notrealkeyforaprobe00000000000"


class Gateway(BaseHTTPRequestHandler):
    """An Anthropic-shaped endpoint that can refuse in two ways.

    `mode` decides which: "model" refuses a model it does not serve
    (400 "model not found"), "key" refuses the credential (401).
    """

    mode = "model"
    served = ("fake-served-model",)
    hits = []

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        Gateway.hits.append(self.path)
        if Gateway.mode == "key":
            body = json.dumps({"error": {"type": "authentication_error",
                                         "message": "invalid api key"}}).encode()
            status = 401
        else:
            try:
                asked = json.loads(raw or b"{}").get("model", "")
            except ValueError:
                asked = ""
            if asked not in Gateway.served:
                body = json.dumps({"error": {
                    "type": "invalid_request_error",
                    "message": f"model {asked} not found"}}).encode()
                status = 400
            else:
                body = json.dumps({
                    "id": "msg_fake", "type": "message", "role": "assistant",
                    "model": asked,
                    "content": [{"type": "text", "text": "GENESIS"}],
                    "stop_reason": "end_turn",
                }).encode()
                status = 200
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def load_cli():
    """The CLI is a script with no .py suffix, so it is loaded by hand."""
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader(
        "genesis_cli", str(ROOT / "genesis"))
    spec = importlib.util.spec_from_loader("genesis_cli", loader)
    module = importlib.util.module_from_spec(spec)
    # The dataclasses in the CLI resolve their own module out of
    # sys.modules, so it has to be registered before it runs.
    sys.modules["genesis_cli"] = module
    loader.exec_module(module)
    return module


class GatewayCase(unittest.TestCase):
    """A gateway on an ephemeral port, and a scratch HOME."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="genesis_cg_home_"))
        # A throwaway REPOSITORY as well as a throwaway HOME. The
        # bootstrap defaults `--repo` to its own ROOT, so a wizard run
        # without `--repo` writes into the REAL checkout — and the real
        # checkout's `.env` is a live credential file. Isolating HOME is
        # not enough: the repository has to be isolated too, or the suite
        # writes a fake key into the machine's real `.env` and rewrites
        # the real receipt on every run.
        self.repo = Path(tempfile.mkdtemp(prefix="genesis_cg_repo_"))
        (self.repo / "genesis").write_text("#!/bin/sh\nexit 0\n")
        (self.repo / "src").mkdir()
        self.key = self.home / "key"
        self.key.write_text(FAKE_KEY, encoding="utf-8")
        os.chmod(self.key, 0o600)

        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.server = HTTPServer(("127.0.0.1", self.port), Gateway)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}"
        Gateway.hits = []
        Gateway.mode = "model"

        self.addCleanup(self.server.shutdown)
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def env(self):
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        for name in list(env):
            if "KEY" in name and name != "PATH":
                env.pop(name)
        return env

    def ladder(self, *args, timeout=300):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "bootstrap.py"),
             "--repo", str(self.repo), *args],
            cwd=str(self.repo), env=self.env(), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout)
        return completed.returncode, (completed.stdout or "") + (completed.stderr or "")

    def wizard(self, *extra):
        return self.ladder("--wizard", "--dry-run", "--gateway-url", self.url,
                           "--key-file", str(self.key), *extra)


class TestAModelErrorIsNotAKeyError(GatewayCase):
    """Defect 1: the verdict must say what actually went wrong."""

    def test_a_model_error_is_not_exit_eight(self):
        """Exit 8 means the KEY was refused. A model error is not that."""
        rc, out = self.wizard()
        self.assertNotEqual(rc, 8,
                            f"a model error was reported as a rejected key: "
                            f"{out[-400:]}")
        self.assertEqual(rc, 2, f"exit {rc}: {out[-400:]}")

    def test_the_message_says_it_is_not_the_key(self):
        rc, out = self.wizard()
        self.assertIn("NOT the key", out,
                      f"the message must exonerate the key: {out[-400:]}")
        self.assertNotIn("the key did not authenticate", out,
                         "the key diagnosis must not be used for a model error")

    def test_the_message_names_the_model_flag(self):
        rc, out = self.wizard()
        self.assertIn("--model", out,
                      f"the operator must be told how to proceed: {out[-400:]}")

    def test_the_gateway_is_asked(self):
        """The probe is how the wizard learns the model is wrong."""
        self.wizard()
        self.assertTrue(Gateway.hits, "the gateway was never asked")

    def test_nothing_is_written_when_the_model_is_wrong(self):
        self.wizard()
        self.assertFalse(
            (self.home / ".config" / "genesis" / "node.json").exists(),
            "nothing may be written when the run refuses")

    def test_with_the_right_model_the_run_succeeds(self):
        rc, out = self.wizard("--model", "fake-served-model")
        self.assertEqual(rc, 0, f"exit {rc}: {out[-400:]}")

    def test_the_key_diagnosis_is_reserved_for_a_key_refusal(self):
        """A gateway that says 401 really has refused the key."""
        Gateway.mode = "key"
        rc, out = self.wizard()
        self.assertEqual(rc, 8, f"a refused key must still be exit 8: {out[-400:]}")
        self.assertIn("the key did not authenticate", out)


class TestTheControl(GatewayCase):
    """THE CONTROL: the refusal is specific, and chat still works.

    A fix that stopped the chat answering would also pass the refusal
    test — the middot would appear, the exit code would be non-zero, and
    every assertion about "the operator was told" would hold. So the
    other half has to be pinned as well: with a profile that names a
    backend the harness **can** speak, the conversation must actually
    happen.

    The substrate is a fake gateway on the loopback: no credential, no
    network beyond 127.0.0.1, and the local server is never involved.
    """

    def test_a_speakable_backend_still_converses(self):
        module = load_cli()
        original_url = harness.BACKENDS["mlx"]["url"]
        original_model = harness.BACKENDS["mlx"]["model"]
        original_profile = node_contract.node_profile
        harness.BACKENDS["mlx"]["url"] = self.url
        harness.BACKENDS["mlx"]["model"] = "fake-served-model"
        node_contract.node_profile = lambda: {"backend": "mlx"}
        self.addCleanup(
            lambda: harness.BACKENDS["mlx"].__setitem__("url", original_url))
        self.addCleanup(
            lambda: harness.BACKENDS["mlx"].__setitem__("model", original_model))
        self.addCleanup(
            lambda: setattr(node_contract, "node_profile", original_profile))
        Gateway.hits = []

        class Args:
            message = ["hello"]

        class Ctx:
            backend = "mlx"
            backend_named = False
            plain = True
            tts = False
            speak = False

        code = module.cmd_chat(Args(), Ctx())
        self.assertEqual(code, 0,
                         "the chat must still work on a backend this build "
                         "can speak")
        self.assertTrue(Gateway.hits,
                        "the conversation never reached the gateway — the "
                        "refusal is not specific to an unusable backend")

    def test_the_refusal_does_not_fire_on_a_speakable_backend(self):
        module = load_cli()
        original_profile = node_contract.node_profile
        node_contract.node_profile = lambda: {"backend": "mlx"}
        self.addCleanup(
            lambda: setattr(node_contract, "node_profile", original_profile))

        class Ctx:
            backend = "mlx"
            backend_named = False

        self.assertEqual(module.profile_substrate_note(Ctx()), "",
                         "a speakable backend earns no middot")


class TestTheProfileBackend(unittest.TestCase):
    """Defect 2: a profile may name a backend this build cannot speak."""

    def _load_cli(self):
        return load_cli()

    def _ctx(self, backend_named=False):
        class Ctx:
            pass
        ctx = Ctx()
        ctx.backend = "mlx"
        ctx.backend_named = backend_named
        return ctx

    def _with_profile(self, profile):
        original = node_contract.node_profile
        node_contract.node_profile = lambda: profile
        self.addCleanup(lambda: setattr(node_contract, "node_profile", original))

    def test_custom_is_not_a_backend(self):
        """The premise of the defect: the harness has no 'custom' entry."""
        self.assertNotIn("custom", harness.BACKENDS)

    def test_a_known_backend_says_nothing(self):
        self._with_profile({"backend": "minimax"})
        module = self._load_cli()
        self.assertEqual(module.profile_substrate_note(self._ctx()), "")

    def test_an_unusable_backend_is_named_out_loud(self):
        self._with_profile({"backend": "custom"})
        module = self._load_cli()
        note = module.profile_substrate_note(self._ctx())
        self.assertIn("custom", note,
                      f"the note must name the backend: {note!r}")
        self.assertIn("·", note,
                      f"the note must carry the middot: {note!r}")

    def test_an_operator_who_named_a_backend_is_not_warned(self):
        self._with_profile({"backend": "custom"})
        module = self._load_cli()
        self.assertEqual(module.profile_substrate_note(self._ctx(True)), "")

    def test_naming_a_substrate_on_the_chat_command_is_honoured(self):
        """THE HOLE, and it is in the plumbing.

        `ctx.backend_named` is set by `split_globals` for the flags at the
        FRONT of the line, but the `chat` subcommand has a `--backend` of
        its own, folded in by `merge()`. If `merge()` does not record that
        the operator chose, the refusal fires against **the one person it
        should never fire against**: `genesis chat --backend mlx "hi"` on a
        node whose profile names a substrate this build cannot speak would
        be refused, even though the operator just named a substrate they
        can speak.

        A test that calls `profile_substrate_note` with a hand-made context
        cannot catch this — the context it builds sets the flag the real
        path forgets to set. So this drives the real path.
        """
        module = load_cli()
        self._with_profile({"backend": "custom"})

        class Args:
            backend = "mlx"
            plain = None
            speak = None
            tts = None

        class Ctx:
            backend = "mlx"
            backend_named = False

        ctx = Ctx()
        module.merge(ctx, Args())
        self.assertTrue(ctx.backend_named,
                        "merge() must record that the operator named a "
                        "substrate — otherwise the refusal fires against "
                        "the one person it should never fire against")
        self.assertEqual(module.profile_substrate_note(ctx), "",
                         "a substrate the operator named earns no middot")

    def test_the_operator_can_name_a_substrate_on_the_command_line(self):
        """The same thing, end to end through the real CLI.

        `--backend anthropic` is the discriminator: this machine has no
        `ANTHROPIC_API_KEY` (the only one it ever had was a fake line a
        wizard run wrote into the real `.env`, since repaired), so the
        harness refuses **before any request** — no network, no live
        service. What matters is the mark: the refusal that must NOT fire
        is the middot. The operator named a substrate; the profile does not
        get to overrule it.
        """
        home = Path(tempfile.mkdtemp(prefix="genesis_cg_named_"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        profile = home / ".config" / "genesis"
        profile.mkdir(parents=True)
        (profile / "node.json").write_text(
            json.dumps({"backend": "custom", "schema": 1}), encoding="utf-8")

        env = dict(os.environ)
        env["HOME"] = str(home)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, str(ROOT / "genesis"), "chat", "--backend",
             "anthropic", "hello"],
            cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=180)
        stderr = completed.stderr or ""
        self.assertNotIn("·", stderr,
                         "the operator named a substrate, so the profile's "
                         f"unusable substrate must not refuse. stderr was: "
                         f"{stderr[:400]!r}")

    def test_the_cli_refuses_to_answer_rather_than_substituting(self):
        """The middot is an honest NON-PASS, not a caveat on a success.

        A loud warning in front of a normal-looking answer is still a
        success — the project's own rule is that nothing is claimed that
        was not proved. So the conversation must NOT happen: the CLI says
        the middot, does not answer, and does not exit 0.
        """
        home = Path(tempfile.mkdtemp(prefix="genesis_cg_profile_"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        profile = home / ".config" / "genesis"
        profile.mkdir(parents=True)
        (profile / "node.json").write_text(
            json.dumps({"backend": "custom", "schema": 1}), encoding="utf-8")

        env = dict(os.environ)
        env["HOME"] = str(home)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, str(ROOT / "genesis"), "chat", "hello"],
            cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=180)
        stderr = completed.stderr or ""
        stdout = completed.stdout or ""
        self.assertNotEqual(completed.returncode, 0,
                            "the chat must not report success when it did not "
                            f"talk to the node's substrate. stdout was: "
                            f"{stdout[:200]!r}")
        self.assertIn("·", stderr,
                      f"the middot is the honest mark. stderr was: "
                      f"{stderr[:400]!r}")
        self.assertIn("custom", stderr,
                      f"the operator must be told the node is pointed at a "
                      f"backend this build cannot speak. stderr was: "
                      f"{stderr[:400]!r}")
        self.assertNotIn("GENESIS", stdout,
                         "the local substrate must not answer in its place")


if __name__ == "__main__":
    unittest.main()
