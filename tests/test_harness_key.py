"""GENESIS — the message a new user gets when the key is not there.

**The defect this file exists for.** `src/harness.py:_post` used one
variable for two things: the NAME of the environment variable a backend
reads its key from, and the VALUE of that key. It overwrote the name with
the value before checking the value, so the error a new user saw when the
key was missing read:

    the substrate is unreachable:  is not set. Put it in .../.env and save.

The name was gone. ` is not set` tells a person nothing about which line
to add to which file — and the whole point of the first-run path is that
it says what to do.

These tests make no network call: they ask for a backend whose key is
absent, and read the message that comes back.
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import harness  # noqa: E402


class TestTheMissingKeyMessage(unittest.TestCase):
    """The message must name the variable, not the empty value."""

    def setUp(self):
        # The key is read from `<repo>/.env` first and from the environment
        # second, so a missing key needs BOTH routes closed. The repo is
        # pointed at an empty directory — a machine that has never been
        # configured — and the variable is removed from the environment.
        import tempfile
        empty = tempfile.mkdtemp(prefix="genesis_nokey_")
        self._original_root = harness.ROOT
        harness.ROOT = Path(empty)
        self.addCleanup(lambda: setattr(harness, "ROOT", self._original_root))
        for name in ("MINIMAX_API_KEY", "OPENROUTER_API_KEY",
                     "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            original = os.environ.get(name)
            os.environ.pop(name, None)
            self.addCleanup(
                lambda n=name, o=original: os.environ.__setitem__(n, o)
                if o is not None else None)

    def test_the_message_names_the_variable(self):
        with self.assertRaises(RuntimeError) as caught:
            harness._post({"model": "MiniMax-M3", "messages": []},
                          backend="minimax")
        message = str(caught.exception)
        self.assertIn("MINIMAX_API_KEY", message,
                      f"the message must name the variable: {message!r}")

    def test_the_message_says_where_the_variable_goes(self):
        with self.assertRaises(RuntimeError) as caught:
            harness._post({"model": "z-ai/glm-5.3", "messages": []},
                          backend="openrouter")
        message = str(caught.exception)
        self.assertIn("OPENROUTER_API_KEY", message)
        self.assertIn(".env", message,
                      f"the message must say where it goes: {message!r}")

    def test_the_message_is_not_empty_where_the_name_goes(self):
        """The defect in one assertion: the name used to be overwritten."""
        with self.assertRaises(RuntimeError) as caught:
            harness._post({"model": "claude-haiku-4-5-20251001", "messages": []},
                          backend="anthropic")
        message = str(caught.exception)
        self.assertNotIn(":  is not set", message,
                         "the variable name was overwritten by its value")
        self.assertIn("ANTHROPIC_API_KEY", message)


if __name__ == "__main__":
    unittest.main()
