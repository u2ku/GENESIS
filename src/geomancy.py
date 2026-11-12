"""Geomancy composition over the TCSI/Aeon encoding.

The classical geomancy square carries sixteen points in four rows of four. A
figure is four points, one taken from each row; sixteen figures are generated
by the stepping rule. Each continuity line carries a TCSI stamp, and the Aeon
word inside it is eight bits — enough to place a figure on the square.

Two continuity lines, put together, give two figures on one square. The points
they share are the shield: where the defence meets the thing it defends
against. The resonance score quantifies the meeting.

  square numbering, point(r, c) = 4r + c + 1

      1   2   3   4
      5   6   7   8
      9  10  11  12
     13  14  15  16

  figure k = 4a + b, a = k // 4, b = k % 4
  column taken from row r is  c_r = (a + r * b) mod 4

  moon (0-7) and watch (0-7) select the figure:  k = (moon * 2 + watch) mod 16
  gate (0-2) is the band: reinforcement / void / carrier-only
"""
from __future__ import annotations

import re

# The sixteen classical figure names, in order.
FIGURES = [
    "Caput", "Puer", "Puella", "Fortuna",
    "Soror", "Miles", "Nupta", "Coena",
    "Infortuna", "Patronus", "Quod Credis", "Peregrinus",
    "Fortuna Minor", "Tabula", "Donna", "Amissus",
]

# The four element groups, each of four points.
GROUPS = {
    "I":   [1, 4, 6, 9],
    "II":  [2, 5, 7, 10],
    "III": [3, 8, 11, 12],
    "IV":  [13, 14, 15, 16],
}

# A TCSI stamp anywhere in a line, or a bare 8-bit word.
STAMP_RE = re.compile(r"⟨TCSI[^⟩]*\|([01]{8})⟩")
WORD_RE = re.compile(r"\b([01]{8})\b")

BANDS = {0: "carrier-only", 1: "void", 2: "reinforcement"}


def extract_word(text: str) -> str | None:
    """Pull the Aeon word out of a continuity line, a stamp, or a bare word."""
    m = STAMP_RE.search(text)
    if m:
        return m.group(1)
    m = WORD_RE.search(text)
    if m:
        return m.group(1)
    return None


def figure_index(word: str) -> int:
    """The figure an Aeon word places on the square.

    gate (bits 7-6) is the band and does not move the figure; moon (bits 5-3)
    and watch (bits 2-0) select it.
    """
    w = int(word, 2)
    moon = (w >> 3) & 0x7
    watch = w & 0x7
    return (moon * 2 + watch) % 16


def figure_points(k: int) -> list[int]:
    """The four point numbers a figure occupies."""
    a, b = k // 4, k % 4
    return [4 * r + ((a + r * b) % 4) + 1 for r in range(4)]


def group_of(point: int) -> str:
    for name, pts in GROUPS.items():
        if point in pts:
            return name
    return "?"


def band_of(word: str) -> str:
    return BANDS.get((int(word, 2) >> 6) & 0x3, "?")


def render(word_a: str, word_b: str, resonance: float) -> str:
    """The shield chart: two figures on one square, and what they share."""
    ka, kb = figure_index(word_a), figure_index(word_b)
    pa, pb = set(figure_points(ka)), set(figure_points(kb))
    shared = pa & pb

    out = []
    out.append("        " + "   ".join(f"r{r+1}" for r in range(4)))
    for r in range(4):
        cells = []
        for c in range(4):
            p = 4 * r + c + 1
            if p in shared:
                cells.append("◉◆")
            elif p in pa:
                cells.append("◈  ")
            elif p in pb:
                cells.append("○  ")
            else:
                cells.append("·  ")
        out.append(f"  line {r+1}  " + " ".join(cells) + f"   points {4*r+1:>2}-{4*r+4:<2}")
    out.append("        " + "   ".join(f"c{c+1}" for c in range(4)))
    out.append("")
    out.append(f"  ◈  {FIGURES[ka]:<16} ({word_a})  band {band_of(word_a)}")
    out.append(f"  ○  {FIGURES[kb]:<16} ({word_b})  band {band_of(word_b)}")
    if shared:
        pts = ", ".join(f"{p} (group {group_of(p)})" for p in sorted(shared))
        out.append(f"  ◉  shield core   {pts}")
    else:
        out.append("  ◉  shield core   none — the figures do not meet")
    out.append(f"  resonance       {resonance:.2f}")
    return "\n".join(out)
