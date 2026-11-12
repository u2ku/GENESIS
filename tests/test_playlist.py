"""PLAYLIST — the prompt budget is enforced, not remembered.

The prompt has to carry the maths without carrying the whole document, and
stay under 2000 characters. If the prompt is edited past the budget, this
fails rather than silently shipping.

    python3 tests/test_playlist.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import playlist  # noqa: E402


class TestBudget(unittest.TestCase):
    def test_prompt_is_under_the_limit(self):
        self.assertLessEqual(
            playlist.length(), playlist.LIMIT,
            f"the prompt is {playlist.length()} chars — over the "
            f"{playlist.LIMIT} budget")

    def test_budget_check_passes(self):
        self.assertTrue(playlist.within_budget())

    def test_limit_is_two_thousand(self):
        self.assertEqual(playlist.LIMIT, 2000)

    def test_prompt_is_not_the_whole_document(self):
        """It must stay far shorter than the handoff it draws on."""
        handoff = ROOT / "docs" / "HANDOFF-GEMINI.md"
        if not handoff.exists():
            self.skipTest("no handoff document to compare against")
        self.assertLess(playlist.length(), handoff.stat().st_size // 4)


class TestContent(unittest.TestCase):
    def test_carries_the_frame(self):
        for term in ("Annihilate", "Calling", "F_{k+1}", "20-track"):
            self.assertIn(term, playlist.PROMPT)

    def test_carries_the_anchor_positions(self):
        for pos in ("t_1", "t_2", "t_4", "t_13", "t_17", "t_19", "t_20"):
            self.assertIn(pos, playlist.PROMPT)

    def test_carries_both_densities(self):
        self.assertIn("7/20", playlist.PROMPT)
        self.assertIn("7/19", playlist.PROMPT)

    def test_carries_the_maths(self):
        for term in ("Δτ_gap", "H = ∅", "Ω_lock", "E_1", "E_2",
                     "D_effort", "t*", "ε"):
            self.assertIn(term, playlist.PROMPT)

    def test_carries_the_retrocausal_instruction(self):
        self.assertIn("τ = T − t", playlist.PROMPT)
        self.assertIn("recovered", playlist.PROMPT)

    def test_says_which_home_the_anchor_takes(self):
        self.assertIn('t_13', playlist.PROMPT)
        self.assertIn("second", playlist.PROMPT)

    def test_forbids_padding_the_density(self):
        self.assertIn("Do not add tracks", playlist.PROMPT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
