# VENDORED — provenance and pin.
#
# This file is Luna's TCSI engine, vendored verbatim so GENESIS runs on a
# machine that has no Luna on it. The bytes below the marker are byte-for-byte
# the upstream file; nothing was edited, reordered or reformatted.
#
#   upstream : ~/Luna/src/luna/tcsi.py
#   sha256   : 765098cbd36b1796ceaccdcd32863a455be8c92d23933221ca92596eacb463e3
#
# The pin is over the upstream bytes, and genesis check re-derives it from this
# file's own body (see src/tcsi.py:body_sha256). When Luna's engine is present
# on the machine it is imported and used in preference to this copy — the
# vendored file is a fallback, never the source. When both are present the
# drift test compares them and fails on any difference.
#
# ---8<--- vendored upstream begins after this line ---8<---

"""TCSI — Temporal-Celestial-Spatial Instant grounding for continuity lines.

Compresses Time, Date, and Moon phase into a single operational-truth stamp
prefixed to every continuity.md line (new and old). The three variables ARE the
Three Pou of the kaupapa made operational as a timestamp:

  Time  (clocktime)  -> Spirit    / carrier  (L1 Origin / L11 Observer)
  Date  (calendar day) -> Science / gate, the bit (L2 Polarity / L9 Law)
  Moon  (lunar phase) -> Creativity / modulation (L4 Branching / L13 Canon)

Together they form the grounding triple ``T(e) = <t, d, mu>``. The presence/
absence pattern of the three fields is a ``pou_signature`` presence vector in
{0,1}^3 — the same interference-band notation as the 14x3 dominance matrix:

  [1,1,1] reinforcement  — fully grounded (clocktime + date + moon all present)
  [1,1,0] void          — moon could not be computed (the handshake space, not
                          a defect — see archive_maincore void_resolution.md)
  [1,0,0] carrier-only  — clocktime alone (the deepest void, still valid)

This module is deliberately dependency-free and side-effect-free. Moon phase
is computed by pure arithmetic (Jean Meeus' low-precision lunar phase
algorithm, ~0.01 rad accuracy, no ephemeris file). The phase is geocentric, so
it is a pure deterministic function of time alone: ``mu = f_moon(t)``. Anyone
with ``t`` can re-derive the moon fields and check them against a stored stamp.

## The Aeon word — the sequence compressed to 8 bits (time included)

The grounding triple compresses to a single byte, the **Aeon** word
(``binary_gate_frequency_pulse.md``: "binary is the gate, 14-layer cycle is
the frequency"). All three Pou values are encoded — Spirit as time, Science as
the gate, Creativity as the moon — each at the timescale the canon assigns it:

  bit  7 6 | 5 4 3 | 2 1 0
       gate    moon    time

  gate (bits 7-6) = pou_signature band, encoded as
                    ``popcount([Spirit, Science, Creativity]) - 1`` — the binary
                    gate / interference band, ordered by groundedness:
                      10  reinforcement  [1,1,1]  all three Pou present
                      01  void           [1,1,0]  moon dark (the handshake space)
                      00  carrier-only   [1,0,0]  clocktime alone (deepest void)
                    (11 reserved.) More Pou present means a higher gate value,
                    i.e. more grounded.
  moon (bits 5-3) = nearest named-phase index (0..7) = the emoji. Creativity
                    modulation, the ~29.53-day lunar cycle. ``mu = f_moon(t)``.
  time (bits 2-0) = diurnal watch = ``floor(hour/3)``, 0..7 — the 24h day
                    quantized to eight three-hour watches. Spirit carrier (the
                    continuous clocktime) quantized to its finest natural cycle.
                    This is the time, included in the compression.

The moon emoji is kept for human readability AND it IS the moon field (bits
5-3): both use nearest-center, so they can never disagree. The gate encodes
groundedness (how many Pou are present); the two cyclic fields (moon + time)
are the frequency — the two real phase wheels (lunar and diurnal), both pure
deterministic functions of ``t``. The full-precision ``t`` and ``d`` stay in
the stamp text as the re-derivation anchor; the 8-bit word is the compressed
operational truth across the gate + the two cycles.

Canon source (read-only): archive_maincore — pou_schema.md, cross_pou_interference.md,
void_resolution.md, binary_gate_frequency_pulse.md; stamp shape cross-referenced
from logic/algorithms/tcsi_temporal_grounding.md (Agent-051).

Sibling of bruce_node/temporal_gatekeeper/gatekeeper.py: the gatekeeper grounds
the INPUT (the model's perception of the moment via clocktime frontmatter);
TCSI grounds the OUTPUT (the continuity record of the moment + day + moon).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional, Tuple

__all__ = [
    "DEFAULT_OBSERVER",
    "now_local",
    "moon_phase",
    "tcsi_snapshot",
    "tcsi_stamp",
    "aeon_word",
    "aeon_decode",
    "aeon_resonance",
    "AEON_WEIGHTS",
    "STAMP_RE",
    "strip_tcsi_stamp",
]

# Zac's fixed observer location (from the continuity iMessage). Stored per the
# TCSI spec sec 3.3 as the no-GPS fallback. The geocentric lunar PHASE does not
# depend on observer location, so this is currently unused for the stamp — it
# exists for future topocentric / ecliptic-position work and for the ledger's
# GPS self-calibration path.
DEFAULT_OBSERVER: Tuple[float, float] = (-37.8386341, 177.5941397)

# Mean synodic month (days) and a known new-moon epoch (Meeus).
SYNODIC_MONTH = 29.530588853
KNOWN_NEW_MOON_JD = 2451550.1  # 2000-01-06 18:14 UTC

# A leading TCSI stamp token: ``<TCSI ...>`` using unicode angle brackets.
# Matches only at the start of a line so mid-sentence angle text is untouched.
STAMP_RE = re.compile(r"^\s*⟨TCSI\b[^⟩]*⟩\s*")

# Moon-phase emoji by cycle fraction (phase_frac encodes waxing vs waning,
# which illuminated-fraction alone cannot). 0..1 = one synodic month; the eight
# named phases sit at the points below, and we pick the nearest (circularly).
_PHASE_POINTS = [
    (0.000, "🌑"),  # New
    (0.125, "🌒"),  # Waxing Crescent
    (0.250, "🌓"),  # First Quarter
    (0.375, "🌔"),  # Waxing Gibbous
    (0.500, "🌕"),  # Full
    (0.625, "🌖"),  # Waning Gibbous
    (0.750, "🌗"),  # Last Quarter
    (0.875, "🌘"),  # Waning Crescent
]


def now_local() -> datetime:
    """Current host clocktime as an offset-aware local datetime.

    Mirrors ``bruce_node/temporal_gatekeeper/gatekeeper.py:now_local``.
    """
    return datetime.now().astimezone()


def _tz_label(when: datetime) -> str:
    label = when.strftime("%Z") or ""
    if not label.strip():
        off = when.utcoffset()
        label = f"UTC{off}" if off is not None else "UTC"
    return label.strip() or "UTC"


def _julian_day(dt: datetime) -> float:
    """Julian Day from an offset-aware datetime (Meeus, ch.7)."""
    y = dt.year
    m = dt.month
    day = dt.day + (dt.hour + dt.minute / 60.0 + (dt.second + dt.microsecond / 1e6) / 3600.0) / 24.0
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + day + b - 1524.5


def _phase_label(age: float) -> str:
    edges = [
        ("New", 0.0, 1.84566),
        ("Waxing Crescent", 1.84566, 5.53699),
        ("First Quarter", 5.53699, 9.22831),
        ("Waxing Gibbous", 9.22831, 12.91963),
        ("Full", 12.91963, 16.61096),
        ("Waning Gibbous", 16.61096, 20.30228),
        ("Last Quarter", 20.30228, 23.99361),
        ("Waning Crescent", 23.99361, 27.68493),
    ]
    for name, lo, hi in edges:
        if lo <= age < hi:
            return name
    return "New"


def _phase_index(phase_frac: float) -> int:
    """Nearest of the eight named-phase centers (circular), 0..7.

    The centers sit at k/8 of the synodic cycle (0=New … 7=Waning Crescent).
    Nearest-point (not floor) is the astronomically correct bucketing: it
    keeps waxing vs waning right at the band edges (a moon at 0.615 is waning
    gibbous, nearest 0.625 — floor-to-eighth would wrongly call it Full).
    """
    def _circ(a: float, b: float) -> float:
        d = abs(a - b) % 1.0
        return min(d, 1.0 - d)
    return min(range(8), key=lambda i: _circ(phase_frac, i / 8.0))


def _phase_emoji(phase_frac: float) -> str:
    return _PHASE_POINTS[_phase_index(phase_frac)][1]


def _diurnal_watch(when: datetime) -> int:
    """The 24h day quantized to eight three-hour watches (0..7).

    A pure deterministic function of ``t`` — the Spirit carrier (continuous
    clocktime) sampled at its finest natural cycle, Earth's diurnal rotation.
    Watch 0 = 00:00-03:00 … watch 7 = 21:00-24:00. This is the time, included
    in the Aeon compression.
    """
    h = when.hour + when.minute / 60.0 + (when.second + when.microsecond / 1e6) / 3600.0
    return int(h / 3.0) % 8


def moon_phase(when: datetime) -> dict:
    """Geocentric lunar phase as a pure function of time (observer-independent).

    Returns ``{phase, phase_angle, age_days, ecliptic_lon, label, emoji}`` where
    ``phase`` is the illuminated fraction in [0,1], ``phase_angle`` in radians
    [0,2pi), ``ecliptic_lon`` in degrees [0,360), ``age_days`` since new moon.
    """
    import math

    jd = _julian_day(when)
    age = (jd - KNOWN_NEW_MOON_JD) % SYNODIC_MONTH
    phase_frac = age / SYNODIC_MONTH
    phase_angle = 2.0 * math.pi * phase_frac
    illum = (1.0 - math.cos(phase_angle)) / 2.0

    # Approximate ecliptic longitude (Meeus, simplified): mean longitude plus
    # the principal elliptic inequality (equation of centre).
    t = (jd - 2451545.0) / 36525.0
    l0 = (218.316 + 13.176396 * t) % 360.0
    m = (134.963 + 13.064993 * t) % 360.0
    ecliptic_lon = (l0 + 6.289 * math.sin(math.radians(m))) % 360.0

    return {
        "phase": round(illum, 3),
        "phase_angle": round(phase_angle, 3),
        "age_days": round(age, 2),
        "ecliptic_lon": round(ecliptic_lon, 1),
        "label": _phase_label(age),
        "emoji": _phase_emoji(phase_frac),
    }


def tcsi_snapshot(when: Optional[datetime] = None, observer: Optional[Tuple[float, float]] = None) -> dict:
    """Full TCSI grounding triple for a clocktime, in the spec's JSON shape.

    ``{t, d, tz, moon, pou_signature}``. Never raises on moon failure: on any
    exception computing the moon, it returns ``moon=None`` and
    ``pou_signature=[1,1,0]`` — a void event, not an error (void_resolution.md:
    the void is the handshake space, not a defect). The chain never breaks on a
    missing celestial reading.
    """
    t = when or now_local()
    d = t.date().isoformat()
    try:
        moon = moon_phase(t)
        sig = [1, 1, 1]
    except Exception:
        moon, sig = None, [1, 1, 0]
    return {
        "t": t.isoformat(timespec="seconds"),
        "d": d,
        "tz": _tz_label(t),
        "moon": moon,
        "pou_signature": sig,
    }


def aeon_word(when: Optional[datetime] = None) -> str:
    """The TCSI sequence (time + date + moon) compressed to one 8-bit byte.

    Gate (bits 7-6) = pou_signature band as ``popcount([Spirit, Science,
                     Creativity]) - 1`` — the binary gate / interference band,
                     ordered by groundedness (10 reinforcement / 01 void /
                     00 carrier-only; 11 reserved).
    Moon (bits 5-3) = nearest named-phase index (0..7) = the emoji. Creativity
                     modulation, the ~29.53-day lunar cycle.
    Time (bits 2-0) = diurnal watch ``floor(hour/3)`` (0..7) — the 24h day in
                     eight three-hour watches. Spirit carrier, quantized.

    The moon phase_frac and the watch are recomputed from the raw clocktime
    (unrounded) so the word is exact, not a rounding of the snapshot's display
    fields. Void event (moon could not be computed) -> gate 01, moon 000; the
    watch still encodes, because the carrier (time) persists through the void.
    """
    t = when or now_local()
    snap = tcsi_snapshot(t)
    sig = snap["pou_signature"]
    gate = (sum(1 for x in sig if x) - 1) & 0x3          # popcount-1, 2 bits
    moon = snap["moon"]
    if moon is None:
        moon_idx = 0
    else:
        jd = _julian_day(t)
        age = (jd - KNOWN_NEW_MOON_JD) % SYNODIC_MONTH
        moon_idx = _phase_index(age / SYNODIC_MONTH)     # 0..7 nearest center
    watch = _diurnal_watch(t)
    word = (gate << 6) | (moon_idx << 3) | watch
    return f"{word:08b}"


def aeon_decode(word: str) -> dict:
    """Inverse of ``aeon_word``: recover the gate, pou_signature, moon-phase
    index, diurnal watch, and emoji index from an 8-bit binary string.

    ``aeon_decode(aeon_word(when))`` recovers the pou_signature band, the named
    moon phase (= emoji), and the diurnal watch. ``emoji_index`` equals
    ``moon_phase_index`` by construction. The reserved band 11 decodes as the
    fully-grounded reinforcement signature.
    """
    try:
        w = int(word, 2)
    except (TypeError, ValueError):
        raise ValueError(f"aeon_decode expects an 8-bit binary string, got {word!r}")
    gate = (w >> 6) & 0x3
    moon_idx = (w >> 3) & 0x7
    watch = w & 0x7
    popcount = gate + 1
    if popcount == 1:
        pou_signature = [1, 0, 0]
    elif popcount == 2:
        pou_signature = [1, 1, 0]
    else:  # 3 (reinforcement) or reserved 11 -> treat as fully grounded
        pou_signature = [1, 1, 1]
    return {
        "word": f"{w:08b}",
        "gate": gate,
        "pou_signature": pou_signature,
        "moon_phase_index": moon_idx,
        "emoji_index": moon_idx,
        "diurnal_watch": watch,
        "watch_hours": f"{watch * 3:02d}:00-{(watch + 1) * 3 % 24:02d}:00",
    }


# Resonance weights for the three Aeon fields. Per tcsi_temporal_grounding.md
# §2.2 the interference beat is between the two real phase wheels — the lunar
# phase (the celestial clock, the one real oscillator) and the diurnal cycle —
# read against the Pou-presence gate. The celestial clock is primary, so moon
# carries the largest share; the gate (groundedness band) is next; the diurnal
# watch is the finest, lowest-weight cycle. The three sum to 1.0.
AEON_WEIGHTS = {"moon": 0.5, "gate": 0.3, "watch": 0.2}


def aeon_resonance(word_a: str, word_b: str) -> float:
    """How strongly two Aeon words resonate, in [0, 1].

    Weighted field agreement (moon 0.5 / gate 0.3 / watch 0.2): each field
    contributes its weight when the two words agree on it. Pure and
    symmetric. Used by Aeon-aware continuity search to rank ledger lines by
    their celestial/groundedness similarity to a reference moment.
    """
    a = aeon_decode(word_a)
    b = aeon_decode(word_b)
    score = 0.0
    if a["moon_phase_index"] == b["moon_phase_index"]:
        score += AEON_WEIGHTS["moon"]
    if a["gate"] == b["gate"]:
        score += AEON_WEIGHTS["gate"]
    if a["diurnal_watch"] == b["diurnal_watch"]:
        score += AEON_WEIGHTS["watch"]
    return score


def tcsi_stamp(when: Optional[datetime] = None) -> str:
    """Compressed single-operational-truth token for a continuity line.

    ``⟨TCSI t|<emoji>|<aeon 8-bit>⟩`` — the clocktime (which carries the date
    inside its ISO form) plus the TCSI sequence (time + moon + gate) compressed
    into one 8-bit Aeon word, with the moon emoji kept for human readability.
    The date (Science/gate) is not duplicated as a separate field — it is
    ``t.date()``, already present in the ISO clocktime. The instant is pinned
    once so the text carrier and the Aeon byte agree to the same moment.

    Deterministic for a fixed ``when`` (``mu = f_moon(t)`` and ``watch = f(t)``
    are both pure functions of time).
    """
    t = when or now_local()
    snap = tcsi_snapshot(t)
    word = aeon_word(t)
    moon = snap["moon"]
    emoji = moon["emoji"] if moon else "∅"
    return f"⟨TCSI {snap['t']}|{emoji}|{word}⟩"


def strip_tcsi_stamp(line: str) -> str:
    """Remove a leading ``⟨TCSI ...⟩`` stamp from a line, idempotently.

    Returns the bare sentence. Used by ``append_continuity`` to prevent
    double-stamping when a line already carries a stamp.
    """
    return STAMP_RE.sub("", line).strip()
