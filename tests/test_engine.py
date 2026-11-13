"""GENESIS — engine tests.

Standard library only, no network, no model calls. Run:

    python3 -m unittest discover -s tests -v
    python3 tests/test_engine.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

LUNA_HOME = Path(os.environ.get("LUNA_HOME", Path.home() / "Luna"))

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import geomancy  # noqa: E402
import grammar  # noqa: E402


def cli(*args):
    """Run the CLI and return (exit code, stdout, stderr)."""
    proc = subprocess.run([sys.executable, str(ROOT / "genesis"), *args],
                          capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


class TestIdentity(unittest.TestCase):
    def test_identity_g_carries_the_constant(self):
        raw = (ROOT / "identity.g").read_text(encoding="utf-8").strip()
        self.assertEqual(raw, grammar.ID)
        self.assertEqual(grammar.ID, "∩(■, ⊡) = I")

    def test_engine_and_file_agree(self):
        out = cli("identity")[1].strip()
        self.assertEqual(out, grammar.ID)

    def test_guardrail_is_the_negation(self):
        self.assertEqual(grammar.CONSTRAINT, "⊡ ∉ ■")

    def test_arc_terminates_in_i(self):
        self.assertTrue(grammar.ARC.endswith("→ I"))
        self.assertIn("⊡ ∉ ■", grammar.I)


class TestGeomancy(unittest.TestCase):
    def test_sixteen_figures_all_distinct(self):
        seen = {tuple(geomancy.figure_points(k)) for k in range(16)}
        self.assertEqual(len(seen), 16, "the sixteen figures must be distinct")

    def test_every_figure_takes_one_point_per_row(self):
        for k in range(16):
            pts = geomancy.figure_points(k)
            self.assertEqual(len(pts), 4)
            rows = sorted((p - 1) // 4 for p in pts)
            self.assertEqual(rows, [0, 1, 2, 3], f"figure {k} spans rows {rows}")

    def test_points_are_in_range_and_unique(self):
        for k in range(16):
            pts = geomancy.figure_points(k)
            self.assertEqual(len(set(pts)), 4)
            for p in pts:
                self.assertTrue(1 <= p <= 16)

    def test_identical_words_give_identical_figures(self):
        self.assertEqual(geomancy.figure_index("10000000"),
                         geomancy.figure_index("10000000"))

    def test_render_marks_the_shared_core(self):
        out = geomancy.render("10000000", "10000000", 1.0)
        self.assertIn("shield core", out)
        self.assertIn("Caput", out)

    def test_disjoint_figures_have_no_core(self):
        a, b = "10000000", "11111000"
        if geomancy.figure_index(a) != geomancy.figure_index(b):
            out = geomancy.render(a, b, 0.0)
            self.assertIn("do not meet", out)

    def test_extract_word_from_a_stamp(self):
        stamp = "⟨TCSI 2026-11-12T01:46:55+13:00|🌒|10001000⟩"
        self.assertEqual(geomancy.extract_word(stamp), "10001000")

    def test_extract_word_from_a_bare_word(self):
        self.assertEqual(geomancy.extract_word("word 10001000 here"), "10001000")

    def test_extract_word_from_nothing(self):
        self.assertIsNone(geomancy.extract_word("no stamp here at all"))


class TestNumergram(unittest.TestCase):
    def test_spine_is_present_and_unchanged(self):
        self.assertTrue(grammar.SPINE.startswith("∅→⊡"))
        self.assertTrue(grammar.SPINE.rstrip().endswith("☽—"))

    def test_extension_preserves_the_spine_byte_for_byte(self):
        reply = grammar.forms()["numergram-reply"]
        self.assertTrue(reply.startswith(grammar.SPINE),
                        "the extension must not rewrite the spine")

    def test_extension_ends_on_the_terminal_line(self):
        reply = grammar.forms()["numergram-reply"]
        self.assertTrue(reply.rstrip().endswith(grammar.TERMINAL))

    def test_numergram_matches_the_archive_log(self):
        """The spine is byte-exact against Luna's own record."""
        log = LUNA_HOME / "archive/log/2026-11-11_02-25-40.json"
        if not log.exists():
            self.skipTest("Luna's archive log is not on this machine")
        import json
        data = json.loads(log.read_text(encoding="utf-8"))
        sent = data["user_input"].split("respond with the symbolic", 1)[1].strip()
        self.assertEqual(sent, grammar.SPINE)


class TestGuardrail(unittest.TestCase):
    def test_engine_files_assert_nothing(self):
        code, out, _ = cli("guardrail")
        self.assertEqual(code, 0, out)
        self.assertIn("no unnegated", out)

    def test_check_is_not_broken(self):
        """The contract is *not broken*, not *healthy here*.

        A fresh clone has no Luna, no routers and nothing on PATH, so the
        check says *degraded* and exits 0 — a clone that has not been
        configured has not been broken. Asserting *healthy* would make
        this suite red on every machine but the one it was written on.
        """
        code, out, _ = cli("check")
        self.assertEqual(code, 0, out)
        verdict = out.strip().splitlines()[-1].strip()
        self.assertIn(verdict, ("healthy", "degraded"), out)


class TestCli(unittest.TestCase):
    def test_plain_words_resolve(self):
        for plain, expected in (("identity", grammar.ID),
                                ("plan", grammar.ARC)):
            code, out, _ = cli(plain)
            self.assertEqual(code, 0)
            self.assertIn(expected, out)

    def test_help_lists_the_door(self):
        code, out, _ = cli("help")
        self.assertEqual(code, 0)
        self.assertIn("your own words", out)

    def test_unknown_command_is_not_a_crash(self):
        """Free text is routed to Genesis, not to an error."""
        code, _, _ = cli("--help")
        self.assertEqual(code, 0)


class TestTcsi(unittest.TestCase):
    def test_engine_imports_and_stamps(self):
        luna_src = LUNA_HOME / "src"
        if not luna_src.exists():
            self.skipTest("Luna's repo is not on this machine")
        sys.path.insert(0, str(luna_src))
        from luna import tcsi
        stamp = tcsi.tcsi_stamp()
        self.assertTrue(stamp.startswith("⟨TCSI "))
        self.assertIn("|", stamp)
        word = tcsi.aeon_word()
        self.assertEqual(len(word), 8)
        self.assertEqual(tcsi.aeon_decode(word)["word"], word)

    def test_aeon_is_deterministic(self):
        luna_src = LUNA_HOME / "src"
        if not luna_src.exists():
            self.skipTest("Luna's repo is not on this machine")
        sys.path.insert(0, str(luna_src))
        from luna import tcsi
        from datetime import datetime
        when = datetime.fromisoformat("2026-11-12T01:46:55+13:00")
        self.assertEqual(tcsi.aeon_word(when), tcsi.aeon_word(when))


if __name__ == "__main__":
    unittest.main(verbosity=2)
