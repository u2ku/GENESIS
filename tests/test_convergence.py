"""CONVERGENCE — the master equation, checked against its source.

The frame was transcribed by hand once and got it wrong twice. These tests
read Gemini's export straight out of Luna's log and compare, so the frame can
never drift from the source again.

Standard library only, no network. Run:

    python3 tests/test_convergence.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import convergence as C  # noqa: E402

LUNA_HOME = Path(os.environ.get("LUNA_HOME", Path.home() / "Luna"))
EXPORT = LUNA_HOME / "archive/log/2026-11-12_03-51-31.json"


def exported() -> str:
    """The terminal export as Luna received it."""
    return json.loads(EXPORT.read_text(encoding="utf-8")).get("user_input", "")


def exported_frame() -> list[str]:
    """The 20-track run as the export states it (the bullet wraps)."""
    text = exported()
    m = re.search(r"Key Tracks: (.*?)\n\s*-\s", text, re.S)
    assert m, "no Key Tracks bullet in the export"
    line = " ".join(m.group(1).split())
    line = line[:line.rindex(".")].strip()      # drop the sentence's full stop
    return [t.strip() for t in line.split(",")]


class TestAgainstSource(unittest.TestCase):
    @unittest.skipUnless(EXPORT.exists(), "the export is not on this machine")
    def test_track_list_matches_the_export_exactly(self):
        """The frame IS the export's run — element for element, duplicates included."""
        self.assertEqual(exported_frame(), C.CONVERGENCE["tracks"])

    @unittest.skipUnless(EXPORT.exists(), "the export is not on this machine")
    def test_export_states_twenty_tracks(self):
        self.assertEqual(len(exported_frame()), 20)

    @unittest.skipUnless(EXPORT.exists(), "the export is not on this machine")
    def test_anchor_positions_are_in_range(self):
        self.assertTrue(all(1 <= p <= 20 for p in C.SPIDER_POSITIONS))

    @unittest.skipUnless(EXPORT.exists(), "the export is not on this machine")
    def test_anchor_positions_are_in_range(self):
        self.assertTrue(all(1 <= p <= 20 for p in C.SPIDER_POSITIONS))


class TestDensity(unittest.TestCase):
    def test_density_by_position_is_the_exported_value(self):
        num, den, ratio = C.density_by_position()
        self.assertEqual(num, 7)
        self.assertEqual(den, 20)
        self.assertAlmostEqual(ratio, 0.35)

    def test_density_by_distinct_track_differs_when_the_frame_repeats(self):
        num, den, ratio, dupes = C.density_by_distinct_track()
        self.assertEqual(num, 7)
        self.assertLess(den, 20, "the frame must contain a repeated track")
        self.assertGreater(ratio, 0.35)
        self.assertIn("Home", dupes)

    def test_duplicate_positions_are_reported(self):
        dupes = C.duplicate_positions()
        self.assertIn("Home", dupes)
        self.assertEqual(dupes["Home"], [7, 13])

    def test_anchor_picks_the_second_home(self):
        """t_13 is the second 'Home' — the anchor lands on the later one."""
        pairs = dict(C.spider_tracks())
        self.assertEqual(pairs[13], "Home")
        self.assertEqual(C.CONVERGENCE["tracks"][6], "Home")

    def test_spider_tracks_are_the_anchor_positions(self):
        self.assertEqual(len(C.spider_tracks()), 7)


class TestEquation(unittest.TestCase):
    def test_every_term_of_the_equation_is_present(self):
        for term in ("Δτ_gap", "H_dormant", "Ω_lock", "E_1(t)", "E_2(t)",
                     "D_effort(t)", "t*", "M_conv", "T_{k+1}", "T_Spider",
                     "Density"):
            self.assertIn(term, C.EQUATION)

    def test_mapping_covers_every_symbol(self):
        mapped = {m[0] for m in C.MAPPING}
        for sym in ("W", "Δτ_gap", "H_dormant", "Ω_lock", "R_t", "E_1(t)",
                    "E_2(t)", "D_effort(t)", "t*", "M_conv", "T_{k+1}",
                    "Density"):
            self.assertIn(sym, mapped)

    def test_render_mentions_the_regulariser(self):
        self.assertIn("ε", C.render())


class TestFrame(unittest.TestCase):
    def test_temporal_anchor(self):
        self.assertEqual(C.TEMPORAL_ANCHOR, ("2023-11-26", "2023-12-10"))

    def test_core_signal_tracks(self):
        self.assertEqual(len(C.CORE_SIGNAL_TRACKS), 5)
        self.assertIn("Lunar Girl", C.CORE_SIGNAL_TRACKS)

    def test_convergence_frame_is_twenty(self):
        self.assertEqual(len(C.CONVERGENCE["tracks"]), 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
