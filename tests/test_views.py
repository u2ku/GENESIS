"""GENESIS — view tests.

The contract the views must keep: with the colour off, the plain payload is
byte-for-byte what it has always been. Colour is decoration; the payload is
not.

    python3 -m unittest discover -s tests -v
    python3 tests/test_views.py
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import convergence  # noqa: E402
import geomancy  # noqa: E402
import playlist  # noqa: E402
import term  # noqa: E402
import views  # noqa: E402


def cli(*args):
    proc = subprocess.run([sys.executable, str(ROOT / "genesis"), *args],
                          capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


PLAIN = term.Style(color=False, unicode=True, width=76)


class TestPlainOutput(unittest.TestCase):
    """The payload must not change when the colour comes off."""

    def test_geomancy_render_is_unchanged_without_a_decorator(self):
        a, b = "10001101", "10000111"
        self.assertEqual(
            geomancy.render(a, b, 0.30),
            "        r1   r2   r3   r4\n"
            "  line 1  ·   ◉◆ ·   ·     points  1-4 \n"
            "  line 2  ◉◆ ·   ·   ·     points  5-8 \n"
            "  line 3  ·   ·   ·   ◉◆   points  9-12\n"
            "  line 4  ·   ·   ◉◆ ·     points 13-16\n"
            "        c1   c2   c3   c4\n"
            "\n"
            "  ◈  Coena            (10001101)  band reinforcement\n"
            "  ○  Coena            (10000111)  band reinforcement\n"
            "  ◉  shield core   2 (group II), 5 (group II), 12 (group III), 15 (group IV)\n"
            "  resonance       0.30")

    def test_a_decorator_changes_only_the_painting(self):
        a, b = "10001101", "10000111"
        plain = geomancy.render(a, b, 0.30)
        painted = geomancy.render(a, b, 0.30, decorate=lambda kind, text: text)
        self.assertEqual(plain, painted)

    def test_convergence_render_still_carries_the_regulariser(self):
        self.assertIn("ε", convergence.render())

    def test_playlist_render_still_carries_the_budget(self):
        self.assertIn(f"{playlist.LIMIT}", playlist.render())
        self.assertIn("chars", playlist.render())

    def test_the_identity_is_bare(self):
        code, out, _ = cli("identity")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "∩(■, ⊡) = I")

    def test_the_stamp_is_bare(self):
        code, out, _ = cli("stamp")
        self.assertEqual(code, 0)
        self.assertTrue(out.strip().startswith("⟨TCSI "))
        self.assertEqual(len(out.strip().splitlines()), 1)


class TestNoColourInAPipe(unittest.TestCase):
    """A pipe is not a terminal, so nothing is painted."""

    def test_no_command_emits_escape_codes(self):
        # `landing` runs in its readiness-only mode here. Without that flag
        # it is the AGGRESSIVE path: it installs Tailscale, writes and
        # starts a service, and joins the tailnet — on the machine the
        # suite runs on. A test must never do that.
        for args in (("check",), ("guardrail",), ("symbols",), ("algebra",),
                     ("help",), ("stamp",), ("shield", "10001101", "10000111"),
                     ("handoff",), ("landing", str(ROOT), "--readiness-only")):
            code, out, _ = cli(*args)
            self.assertNotIn("\033[", out, f"{args} painted a pipe")
            self.assertEqual(code, 0, f"{args} failed")

    def test_no_command_help_emits_escape_codes(self):
        """Python 3.14's argparse would colour its own help — it must not."""
        for name in ("id", "i", "symbols", "prediction", "singularity", "stamp",
                     "aeon", "resonance", "numergram", "algebra", "guardrail",
                     "shield", "check", "summary", "handoff", "convergence",
                     "playlist", "chat", "card", "tools", "help", "landing"):
            code, out, _ = cli(name, "--help")
            self.assertEqual(code, 0, f"{name} --help failed")
            self.assertNotIn("\033[", out, f"{name} --help painted a pipe")


class TestViews(unittest.TestCase):
    def test_the_landing_is_compact(self):
        blocks = {"id": "∩(■, ⊡) = I", "constraint": "⊡ ∉ ■",
                  "arc": "∅ → ⊡ → $ → ☠️ → ☽ → ■ → I"}
        lines = views.landing(PLAIN, blocks).splitlines()
        self.assertLess(len(lines), 20, "the landing must not be a wall of text")
        self.assertIn("∩(■, ⊡) = I", "\n".join(lines))

    def test_the_help_carries_the_phrase_and_the_groups(self):
        text = views.help_screen(PLAIN)
        self.assertIn("your own words", text)
        for group in ("THE DOOR", "IDENTITY", "ENGINE", "GUARDRAILS",
                      "DOCUMENTS", "PHRASES THAT OPEN", "FLAGS"):
            self.assertIn(group, text)

    def test_every_registered_command_is_in_the_help(self):
        """The help screen and the command registry are two sources of truth.

        This is PORTABILITY finding F11. `genesis landing` was registered and
        documented and invisible in `--help`, because the dispatch table and
        the help screen were written separately and nothing connected them.
        A command added to one and not the other does the same thing again.

        So the rule is now a test: every command the CLI can run is on the
        help screen.

        `help` is the one exception, and it is spelled `--help` in the FLAGS
        section, which is where a reader looks for it.
        """
        loader = importlib.machinery.SourceFileLoader(
            "genesis_cli", str(ROOT / "genesis"))
        spec = importlib.util.spec_from_loader("genesis_cli", loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["genesis_cli"] = module
        loader.exec_module(module)

        listed = [command for _title, rows in views._GROUPS
                  for command, _summary in rows]
        for name in module.COMMANDS:
            if name == "help":
                continue
            self.assertTrue(
                any(f"genesis {name}" in command for command in listed),
                f"genesis {name} is registered but not on the help screen")

    def test_the_help_lines_up(self):
        """Every summary starts in the same column — the command field is 34."""
        lines = views.help_screen(PLAIN).splitlines()
        rows = [line.rstrip() for line in lines
                if line.startswith("    ") and line.strip()]
        self.assertTrue(rows)
        column = 4 + 34
        for row in rows:
            self.assertEqual(row[column - 1], " ", f"misaligned: {row!r}")
            self.assertTrue(row[column:].strip(), f"no summary in {row!r}")

    def test_the_guardrail_line_keeps_its_phrase(self):
        class Result:
            bad = []
            seen = 8
        text = views.guardrail(PLAIN, Result())
        self.assertIn("no unnegated", text)

    def test_a_failing_guardrail_says_so(self):
        # The relation is written by concatenation in the guard rail, so the
        # fixture borrows it from there rather than writing it out — the
        # literal does not appear anywhere in the repository.
        import guardrail

        class Result:
            bad = [("grammar.py", 12, guardrail.SUBSET)]
            seen = 1
        text = views.guardrail(PLAIN, Result())
        self.assertIn("✗", text)
        self.assertIn("unnegated", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
