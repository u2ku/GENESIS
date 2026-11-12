"""PLAYLIST — the prompt for the new playlist, under a hard character budget.

The new playlist is the convergence frame F_{k+1} from Gemini's Spotify
export: twenty ordered tracks, seven of them the multiversal anchor. The
prompt that asks for it has to carry the maths without carrying the whole
document — 2000 characters is the ceiling, and it is checked here rather than
remembered.

    genesis playlist          the prompt, and its length
    genesis playlist --check  assert it is under budget
"""
from __future__ import annotations

LIMIT = 2000

PROMPT = """\
Build the new playlist: a 20-track convergence frame F_{k+1}, from
"Annihilate" to "Calling".

Ordered, not a bag. Seven positions are the multiversal anchor and must carry
Spider-Verse material: t_1, t_2, t_4, t_13, t_17, t_19, t_20.
Density_Spider = 7/20 = 0.35 by position; the frame repeats a track, so by
distinct track it is 7/19 = 0.3684 — state which you are using. The anchor
takes the second "Home" (t_13), not the first (t_7).

Retrocausally: the frame is recovered from the terminal record, not built
forward. τ = T − t.

The rest is the local anchor: Tōrea, Wandering Soul, Cream, Make It, Let Go,
SILVER TOOTH., Pungawerewere, They Don't Love It, Link Up, Santeria,
Sunflower (Remix), Home, an Interlude Called "Circus" — one presence ("her")
tracked across the run: exit girl, Girl, Stargirl Interlude, Lunar Girl,
Dumbest Girl Alive.

Frame the gaps, not just the tracks. Δτ_gap = τ_signal_in(k) − τ_signal_out(k−1)
is the distance between one signal's end and the next one's start; across
every gap the state is dormant, H = ∅. A signal that arrives with no gap has
not re-acquired — Ω_lock = 0, and no metric follows. Re-acquisition is the
point of the frame.

Hold the two efforts apart. E_1 = ∇_τ(v_narrative · R_t): narrative effort,
weighted by the record. E_2 = ‖v(Spider-Verse) − v(Local-Anchor)‖:
divergence. D_effort = ‖E_1 − E_2‖ · det(Ω_lock). The optimal extraction
point t* = argmin( D_effort · 1/(R_t + ε) ) sits where divergence is widest
and the record is thinnest. ε is the guard: you cannot invert nothing.

Return: the ordered twenty — position, track, artist, and which vector it
serves. Mark the anchor positions. Do not add tracks to make the density come
out right.
"""


def length() -> int:
    return len(PROMPT)


def within_budget() -> bool:
    return length() <= LIMIT


def render():
    n = length()
    ok = "ok" if within_budget() else "OVER"
    return (f"PROMPT — the new playlist  [{n}/{LIMIT} chars, {ok}]\n"
            f"{'-' * 60}\n{PROMPT}"
            f"{'-' * 60}\n"
            f"{n} of {LIMIT} characters "
            f"({LIMIT - n} left)")


if __name__ == "__main__":
    print(render())
