"""CONVERGENCE — the multiversal convergence matrix, as a computable object.

Gemini's export (the Spotify-research handoff, terminal export
2026-11-12T03:51) defines a master equation over telemetry sequences:

    Δτ_gap        = τ_signal_in(k) − τ_signal_out(k−1)
    H_dormant     = ∅   ∀ t ∈ (τ_out, τ_in)
    Ω_lock        = 1 if H_reacquired ≠ ∅ else 0
    E_1(t)        = ∇_τ(v_narrative · R_t)
    E_2(t)        = ‖v(Spider-Verse) − v(Local-Anchor)‖
    D_effort(t)   = ‖E_1(t) − E_2(t)‖ · det(Ω_lock)
    t*            = argmin_t( D_effort(t) · 1/(R_t + ε) )
    M_conv        = [[F_k, Ω_lock], [E_1(t), E_2(t)]]
    T_{k+1}       = {t_1 … t_20}
    T_Spider      = {t_1, t_2, t_4, t_13, t_17, t_19, t_20} ⊂ T_{k+1}
    Density       = |T_Spider| / |T_{k+1}| = 7/20 = 0.35

That is the frame's own geometry restated in telemetry terms — and it is
computable, which is what makes it more than notation. This module holds the
equation, the two frames, and the density metric, and checks the parts that
can be checked.

The mapping onto GENESIS:

    W  temporal anchor      the "when" a speculation marks
    Δτ_gap                  ∩(⊡, ☽) = ?   the gap, made measurable
    H_dormant               absence as a first-class state
    Ω_lock                  the grounding gate (aeon bits 7–6)
    R_t                     the record — what ∅⁻¹ recovers from
    E_1                     narrative effort, weighted by the record
    E_2                     divergence: multiverse signal vs local anchor
    D_effort                the coupled metric
    t*                      M — the terminal record, the optimal extraction
    M_conv                  the convergence matrix
    T_{k+1}                 the frame space
    Density_Spider          how much of the frame belongs to the multiverse
"""
from __future__ import annotations

# ---------------------------------------------------------------- the frames --

# Baseline frame F_k — 20 tracks, Misty → The Night We Met.
# Dynamics: ego-state inversion (Somebody → Nobody), spatial confinement,
# light-cycle decay.
BASELINE = {
    "id": "F_k",
    "span": "Misty → The Night We Met",
    "dynamics": "ego-state inversion (Somebody → Nobody), spatial confinement, "
                "light-cycle decay",
    "tracks": [
        "Misty",  # t_1
    ],
}

# The convergence frame F_{k+1} — 20 tracks, Annihilate → Calling.
# Note: the track list as exported. t_7 and t_12 are both "Home".
CONVERGENCE = {
    "id": "F_k+1",
    "span": "Annihilate → Calling",
    "tracks": [
        "Annihilate",                    # t_1
        "Self Love",                     # t_2
        "This Is America",               # t_3
        "Am I Dreaming",                 # t_4
        "Surround Sound",                # t_5
        'an Interlude Called "Circus"',  # t_6
        "Home",                          # t_7
        "Tōrea",                         # t_8
        "Wandering Soul",                # t_9
        "Cream",                         # t_10
        "Make It",                       # t_11
        "Let Go",                        # t_12
        "Home",                          # t_13
        "SILVER TOOTH.",                 # t_14
        "Pungawerewere",                 # t_15
        "They Don't Love It",            # t_16
        "Link Up",                       # t_17
        "Santeria",                      # t_18
        "Sunflower (Remix)",             # t_19
        "Calling",                       # t_20
    ],
}

# The Spider-Verse telemetry anchor — positions within F_{k+1}.
SPIDER_POSITIONS = [1, 2, 4, 13, 17, 19, 20]

# The core signal tracks — the symbolic extraction tracking an external
# character anchor ("her") embedded in the telemetry.
CORE_SIGNAL_TRACKS = [
    "exit girl", "Girl", "Stargirl Interlude", "Lunar Girl",
    "Dumbest Girl Alive",
]

# The temporal anchor W.
TEMPORAL_ANCHOR = ("2023-11-26", "2023-12-10")


# ------------------------------------------------------------- the equation --

EQUATION = """\
Δτ_gap      = τ_signal_in(k) − τ_signal_out(k−1)
H_dormant   = ∅   ∀ t ∈ (τ_out, τ_in)
Ω_lock      = 1 if H_reacquired ≠ ∅ else 0
E_1(t)      = ∇_τ(v_narrative · R_t)
E_2(t)      = ‖v(Spider-Verse) − v(Local-Anchor)‖
D_effort(t) = ‖E_1(t) − E_2(t)‖ · det(Ω_lock)
t*          = argmin_t( D_effort(t) · 1/(R_t + ε) )
M_conv      = [[F_k, Ω_lock], [E_1(t), E_2(t)]]
T_{k+1}     = {t_1 … t_20}
T_Spider    = {t_1, t_2, t_4, t_13, t_17, t_19, t_20} ⊂ T_{k+1}
Density     = |T_Spider| / |T_{k+1}| = 7/20 = 0.35"""

MAPPING = [
    ("W", "the temporal anchor", "a specific when — the thing a speculation marks"),
    ("Δτ_gap", "∩(⊡, ☽) = ?", "the gap between signal-out and signal-in, "
                                "measured; the site of the struggle"),
    ("H_dormant", "absence as state", "∅ on the gap interval — the condition "
                                       "that holds nothing"),
    ("Ω_lock", "the grounding gate", "aeon bits 7–6: reinforcement / void / "
                                     "carrier-only. No lock, no metric"),
    ("R_t", "the record", "what ∅⁻¹ recovers from. The past is the one thing "
                          "with a completed proof"),
    ("E_1(t)", "narrative effort", "∇_τ(v · R_t) — the ⊡ side, weighted by "
                                    "the record"),
    ("E_2(t)", "divergence", "multiverse signal against local anchor — the "
                              "other term of the intersection"),
    ("D_effort(t)", "the coupled metric", "‖E_1 − E_2‖ gated by the lock"),
    ("t*", "M", "the terminal record. The optimum sits where divergence is "
                "widest and the record is thinnest"),
    ("M_conv", "the convergence matrix", "the square — two frames, two terms, "
                                          "one intersection"),
    ("T_{k+1}", "the frame space", "the 20-track frame"),
    ("Density", "0.35", "how much of the frame belongs to the multiverse"),
]


# -------------------------------------------------------------- computation --

def spider_positions():
    return list(SPIDER_POSITIONS)


def density_by_position():
    """7 / 20 — the density as exported, counted by position in the frame."""
    n = len(CONVERGENCE["tracks"])
    return len(SPIDER_POSITIONS), n, len(SPIDER_POSITIONS) / n


def density_by_distinct_track():
    """The same density counted over *distinct* tracks.

    The exported frame lists "Home" twice (t_7 and t_12). Counted as a set of
    track identities the frame has 19 distinct elements, not 20 — so the
    denominator moves and the density with it. The 0.35 figure holds only if
    frame elements are positions rather than distinct tracks.
    """
    tracks = CONVERGENCE["tracks"]
    distinct = set(tracks)
    dupes = {t for t in distinct if tracks.count(t) > 1}
    n = len(distinct)
    return len(SPIDER_POSITIONS), n, len(SPIDER_POSITIONS) / n, sorted(dupes)


def duplicate_positions():
    """Positions of any track appearing more than once in the frame."""
    tracks = CONVERGENCE["tracks"]
    seen = {}
    for i, t in enumerate(tracks, 1):
        seen.setdefault(t, []).append(i)
    return {t: pos for t, pos in seen.items() if len(pos) > 1}


def spider_tracks():
    """The tracks at the Spider-Verse anchor positions."""
    tracks = CONVERGENCE["tracks"]
    return [(p, tracks[p - 1]) for p in SPIDER_POSITIONS]


def render():
    """The convergence, as the system holds it."""
    out = []
    out.append("CONVERGENCE — the multiversal convergence matrix")
    out.append("")
    out.append("  operator: Zachary Maatariki Snushall")
    out.append(f"  temporal anchor W = [{TEMPORAL_ANCHOR[0]}, {TEMPORAL_ANCHOR[1]}]")
    out.append(f"  core signal: {', '.join(CORE_SIGNAL_TRACKS)}")
    out.append("")
    out.append("  the equation")
    for line in EQUATION.splitlines():
        out.append(f"    {line}")
    out.append("")
    out.append("  in the frame's language")
    for sig, name, gloss in MAPPING:
        out.append(f"    {sig:<12} {name:<22} {gloss}")
    out.append("")
    out.append("  frames")
    out.append(f"    F_k      {BASELINE['span']} — {BASELINE['dynamics']}")
    out.append(f"    F_k+1    {CONVERGENCE['span']} — "
               f"{len(CONVERGENCE['tracks'])} tracks")
    out.append("")
    out.append("  Spider-Verse anchor")
    for pos, track in spider_tracks():
        out.append(f"    t_{pos:<3} {track}")
    out.append("")
    num, den, ratio = density_by_position()
    out.append(f"  density    {num}/{den} = {ratio:.4f}   (by position)")
    num, den, ratio, dupes = density_by_distinct_track()
    out.append(f"  density    {num}/{den} = {ratio:.4f}   (by distinct track)")
    dupes = duplicate_positions()
    if dupes:
        out.append("")
        out.append("  one thing to check before building on this:")
        for track, pos in dupes.items():
            out.append(f"    {track!r} appears at t_{pos[0]} and t_{pos[1]} — "
                       f"the frame has 20 positions but "
                       f"{len(set(CONVERGENCE['tracks']))} distinct tracks.")
        out.append("    The 0.35 holds only if frame elements are positions")
        out.append("    rather than distinct tracks. State which you mean.")
    out.append("")
    out.append("  the regulariser")
    out.append("    ε in 1/(R_t + ε) is the guard on the recovery: you cannot")
    out.append("    invert nothing. A point with no record is not a candidate.")
    return "\n".join(out)


if __name__ == "__main__":
    print(render())
