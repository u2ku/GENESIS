"""The voice, planned and applied.

The wizard provisions kokoro so GENESIS speaks without Luna, and the plan
must obey the rule every other action obeys: an empty machine shows the
work, a converged machine shows nothing, and a broken piece (a venv
without kokoro, a zero-byte model) never passes as present.
"""
from __future__ import annotations

import os
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import bootstrap  # noqa: E402


class VoiceFixture(unittest.TestCase):
    """A home that is not the user's, and a wizard context."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = self._tmp.name
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        self._tmp.cleanup()

    def context(self):
        ctx = bootstrap.Context()
        ctx.wizard = True
        return ctx

    def lay_converged(self):
        """Everything the voice needs, present and correct."""
        home = bootstrap.kokoro_home()
        home.mkdir(parents=True)
        (home / "kokoro_speak.py").write_text(
            (bootstrap.ROOT / "tools" / "kokoro" / "kokoro_speak.py")
            .read_text(encoding="utf-8"))
        py = home / "venv" / "bin" / "python"
        py.parent.mkdir(parents=True, exist_ok=True)
        py.write_text("#!/bin/sh\n")
        for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
            (home / name).write_bytes(b"payload" * 100)


class TestThePlan(VoiceFixture):

    def test_an_empty_machine_shows_five_actions(self):
        actions = bootstrap._voice_actions(self.context())
        self.assertEqual([a["effect"] for a in actions],
                         ["create", "create", "install", "install", "install"])

    def test_a_converged_machine_shows_nothing(self):
        self.lay_converged()
        with unittest.mock.patch.object(
                bootstrap, "_venv_has_kokoro", return_value=True):
            actions = bootstrap._voice_actions(self.context())
        self.assertTrue(all(a["effect"] == "none" for a in actions),
                        [a["id"] for a in actions if a["effect"] != "none"])

    def test_a_venv_without_kokoro_is_not_converged(self):
        self.lay_converged()
        with unittest.mock.patch.object(
                bootstrap, "_venv_has_kokoro", return_value=False):
            effects = {a["id"]: a["effect"]
                       for a in bootstrap._voice_actions(self.context())}
        self.assertEqual(effects["kokoro-venv"], "install")

    def test_a_zero_byte_model_is_absent(self):
        self.lay_converged()
        bootstrap.kokoro_model_path().write_bytes(b"")
        effects = {a["id"]: a["effect"]
                   for a in bootstrap._voice_actions(self.context())}
        self.assertEqual(effects["kokoro-model"], "install")

    def test_no_voice_opt_out_is_a_silent_probe_not_a_secret(self):
        ctx = self.context()
        ctx.no_voice = True
        actions = bootstrap.build_plan(ctx, {"supported": True})
        probes = [a for a in actions if a["id"] == "kokoro-voice"]
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0]["effect"], "none")
        self.assertIn("--no-voice", probes[0].get("note", ""))
        # and none of the real voice actions ride along
        real = {"kokoro-script", "kokoro-venv", "kokoro-model",
                "kokoro-voices"}
        self.assertFalse(real & {a["id"] for a in actions})


class TestTheVenv(VoiceFixture):
    """The venv build, driven with fakes — a NameError here shipped once
    because no test ever reached this code."""

    def test_the_build_runs_to_success(self):
        home = bootstrap.kokoro_home()
        home.mkdir(parents=True)
        ran = []

        def fake_run(cmd, **kwargs):
            ran.append(cmd)
            if "venv" in cmd:
                (home / "venv" / "bin").mkdir(parents=True, exist_ok=True)
                (home / "venv" / "bin" / "pip").write_text("#!/bin/sh\n")
            return unittest.mock.Mock(returncode=0, stderr="", stdout="")

        with unittest.mock.patch.object(bootstrap.subprocess, "run",
                                        side_effect=fake_run), \
             unittest.mock.patch.object(bootstrap, "_venv_has_kokoro",
                                        return_value=True):
            ok, detail = bootstrap._install_kokoro_venv()
        self.assertTrue(ok, detail)
        self.assertEqual(len(ran), 2)  # the venv, then the pip install
        self.assertIn("kokoro-onnx>=0.5.0,<1.0", " ".join(ran[1]))

    def test_a_failed_pip_is_reported_not_crashed(self):
        home = bootstrap.kokoro_home()
        home.mkdir(parents=True)

        def fake_run(cmd, **kwargs):
            if "venv" in cmd:
                (home / "venv" / "bin").mkdir(parents=True, exist_ok=True)
                (home / "venv" / "bin" / "pip").write_text("#!/bin/sh\n")
                return unittest.mock.Mock(returncode=0, stderr="", stdout="")
            return unittest.mock.Mock(returncode=1, stderr="boom",
                                      stdout="")

        with unittest.mock.patch.object(bootstrap.subprocess, "run",
                                        side_effect=fake_run):
            ok, detail = bootstrap._install_kokoro_venv()
        self.assertFalse(ok)
        self.assertIn("pip install failed", detail)


class TestTheDownloads(VoiceFixture):

    def test_a_failed_download_leaves_no_partial_file(self):
        dest = bootstrap.kokoro_model_path()
        boom = OSError("connection reset")
        with unittest.mock.patch.object(
                bootstrap.urllib.request, "urlopen",
                side_effect=boom):
            ok, detail = bootstrap._download(bootstrap.KOKORO_MODEL_URL, dest)
        self.assertFalse(ok)
        self.assertFalse(dest.exists())
        self.assertFalse(dest.with_name(dest.name + ".part").exists())

    def test_a_successful_download_moves_atomically(self):
        dest = bootstrap.kokoro_model_path()
        dest.parent.mkdir(parents=True)

        class FakeResponse:
            """A body that ends, like a real one — copyfileobj drains it."""

            def __init__(self, body):
                self._body = body
                self.headers = {}

            def read(self, n=-1):
                if not self._body:
                    return b""
                chunk, self._body = self._body[:n], self._body[n:]
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with unittest.mock.patch.object(
                bootstrap.urllib.request, "urlopen",
                return_value=FakeResponse(b"model-bytes")):
            ok, detail = bootstrap._download(bootstrap.KOKORO_MODEL_URL, dest)
        self.assertTrue(ok)
        self.assertEqual(dest.read_bytes(), b"model-bytes")
        self.assertFalse(dest.with_name(dest.name + ".part").exists())


if __name__ == "__main__":
    unittest.main()
