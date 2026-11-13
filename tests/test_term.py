"""GENESIS — terminal kit tests.

Standard library only, no network. Run:

    python3 -m unittest discover -s tests -v
    python3 tests/test_term.py
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import term  # noqa: E402


class FakeTTY(io.StringIO):
    """A stream that claims to be a terminal."""

    encoding = "utf-8"

    def isatty(self) -> bool:
        return True


class TestCapabilities(unittest.TestCase):
    def test_pipe_is_not_a_terminal(self):
        self.assertFalse(term.supports_color(io.StringIO()))

    def test_terminal_is(self):
        self.assertTrue(term.supports_color(FakeTTY()))

    def test_no_color_is_respected(self):
        old = os.environ.get("NO_COLOR")
        os.environ["NO_COLOR"] = "1"
        try:
            self.assertFalse(term.supports_color(FakeTTY()))
        finally:
            if old is None:
                os.environ.pop("NO_COLOR", None)
            else:
                os.environ["NO_COLOR"] = old

    def test_dumb_terminal_has_no_colour(self):
        old = os.environ.get("TERM")
        os.environ["TERM"] = "dumb"
        try:
            self.assertFalse(term.supports_color(FakeTTY()))
        finally:
            if old is None:
                os.environ.pop("TERM", None)
            else:
                os.environ["TERM"] = old

    def test_unicode_probe_follows_the_encoding(self):
        class Latin(io.StringIO):
            encoding = "latin-1"

        self.assertFalse(term.supports_unicode(Latin()))
        self.assertTrue(term.supports_unicode(io.StringIO()))

    def test_display_safe_survives_a_narrow_encoding(self):
        class Latin(io.StringIO):
            encoding = "latin-1"

        self.assertIsInstance(term.display_safe("⊡ ■", Latin()), str)

    def test_write_degrades_instead_of_raising(self):
        """A terminal that cannot hold the glyphs gets stand-ins, not a crash."""
        class Latin(io.StringIO):
            encoding = "latin-1"

        stream = Latin()
        term.write("∩(■, ⊡) = I", stream)
        self.assertTrue(stream.getvalue().strip())

    def test_write_ends_with_a_newline(self):
        stream = io.StringIO()
        term.write("hello", stream)
        self.assertEqual(stream.getvalue(), "hello\n")

    def test_the_stream_is_resolved_when_it_is_used(self):
        """A default of sys.stdout binds at import — redirection must still work."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            term.write("hello")
        self.assertIn("hello", buf.getvalue())

    def test_capability_probes_default_to_stdout(self):
        self.assertFalse(term.supports_color())
        self.assertTrue(term.supports_unicode())


class TestGeometry(unittest.TestCase):
    def test_width_is_clamped(self):
        old = os.environ.get("COLUMNS")
        try:
            os.environ["COLUMNS"] = "200"
            self.assertLessEqual(term.terminal_width(), 96)
            os.environ["COLUMNS"] = "4"
            self.assertGreaterEqual(term.terminal_width(), 36)
        finally:
            if old is None:
                os.environ.pop("COLUMNS", None)
            else:
                os.environ["COLUMNS"] = old

    def test_wrap_keeps_words_whole(self):
        lines = term.wrap_message("supercalifragilistic " * 4, 20)
        self.assertTrue(any("supercalifragilistic" in line for line in lines))

    def test_wrap_keeps_paragraphs_apart(self):
        lines = term.wrap_message("one\n\ntwo", 40)
        self.assertIn("", lines)
        self.assertEqual(lines[0], "one")
        self.assertEqual(lines[-1], "two")

    def test_wrap_of_empty_is_one_blank_line(self):
        self.assertEqual(term.wrap_message("", 40), [""])


class TestStyle(unittest.TestCase):
    def test_paint_is_a_no_op_without_colour(self):
        style = term.Style(color=False)
        self.assertEqual(style.paint("x", term.ACCENT), "x")
        self.assertEqual(style.accent("x"), "x")

    def test_paint_wraps_when_colour_is_on(self):
        style = term.Style(color=True)
        painted = style.accent("x")
        self.assertTrue(painted.startswith(term.ACCENT))
        self.assertTrue(painted.endswith(term.RESET))
        self.assertEqual(term.strip_ansi(painted), "x")

    def test_visible_len_ignores_colour(self):
        style = term.Style(color=True)
        self.assertEqual(term.visible_len(style.accent("hello")), 5)

    def test_bubble_hangs_continuation_under_the_text(self):
        style = term.Style(color=False, width=40)
        text = "one two three four five six seven eight nine ten"
        lines = style.bubble(text).split("\n")
        self.assertGreater(len(lines), 1)
        start = lines[0].index("one")
        for line in lines[1:]:
            self.assertEqual(len(line) - len(line.lstrip()), start)

    def test_field_aligns_the_labels(self):
        style = term.Style(color=False)
        a = style.field("short", "1", width=10)
        b = style.field("longer", "2", width=10)
        self.assertEqual(a.index("1"), b.index("2"))

    def test_heading_puts_the_note_on_the_right(self):
        style = term.Style(color=False, width=40)
        line = style.heading("IDENTITY", "docs/IDENTITY.md")
        self.assertTrue(line.rstrip().endswith("docs/IDENTITY.md"))

    def test_glyph_falls_back_when_unicode_is_off(self):
        style = term.Style(unicode=False)
        self.assertEqual(style.glyph("◆", ">"), ">")
        self.assertEqual(term.Style(unicode=True).glyph("◆", ">"), "◆")


class TestSpinner(unittest.TestCase):
    def test_disabled_spinner_writes_nothing(self):
        stream = io.StringIO()
        with term.Spinner(stream, enabled=False):
            pass
        self.assertEqual(stream.getvalue(), "")

    def test_enabled_spinner_clears_its_line(self):
        stream = FakeTTY()
        spinner = term.Spinner(stream, enabled=True, color=False)
        spinner.start()
        spinner.stop()
        self.assertIn("\r", stream.getvalue())

    def test_spinner_is_off_when_the_stream_is_a_pipe(self):
        spinner = term.Spinner(io.StringIO())
        self.assertFalse(spinner.enabled)


if __name__ == "__main__":
    unittest.main(verbosity=2)
