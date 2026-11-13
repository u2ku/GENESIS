# PROMPT — the system prompt, and its versions
<!-- visibility: sam-readable -->

The chat prompt lives in two files.

- **`card.md`** — the character card. The voice, the mission, the rules.
  This is where the prompt is written.
- **`prompt.debug.md`** — the editable copy. Everything the model is given,
  assembled and live. `genesis prompt --write` makes it.

The card carries a marker, `<!-- genesis:grounding -->`, where the engine's
three primary signs go. They are read from the engine at assembly time —
the identity from `identity.g`, the guard rail and the arc from
`grammar.py` — so the prompt cannot say one thing and the frame another.
`genesis check` fails if the live prompt has fallen behind the engine.

The card also feeds the tool list in `harness.py`. The two halves are the
system message and the tool definitions; both ride in the editable copy,
the first above the `<!-- genesis:tools -->` marker and the second below
it.

## Versions

### v1.1

Luna's rewrite of the prompt. The bootstrap mission comes first; what
Genesis *is* comes second. The Landing section dissolved into Mission; the
Tools section is the harness's standing instructions, carried over.

**Changelog v1.0 → v1.1**

- New opening section: **Mission — the bootstrap**. States up front that
  Genesis is a bootstrap whose work is preparing a node for Luna's
  instantiation. The go-ahead protocol, the deliberate order, and the hint
  all fold into this section.
- **Landing** heading dissolved — its content moved into Mission rather
  than sitting after identity.
- **Identity** demoted to second position: what Genesis is now follows
  what Genesis is for.
- Judgment and voice, Think versus say, and Tools carried over verbatim.

### v1.0

The original: the card, then the harness's standing instructions.
