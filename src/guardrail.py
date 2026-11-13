"""GUARDRAIL — the one relation the reality refuses.

The engine may never assert that the construct is a subset of the singularity.
This module owns that rule: what the forbidden assertion looks like, which
words count as negating it, which files are held to it, and how it is checked.

It is deliberately self-referential — this file lists itself among the files
it scans, so a copy of the assertion cannot be smuggled in here.

The refused relation is written by concatenation below, so that this file does
not itself carry the literal it forbids. Any module that needs to *display*
the relation imports ``SUBSET`` from here; none of them writes it out.

    genesis guardrail    enforce the rule across the system
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
DOCS = ROOT / "docs"

# The refused relation. Concatenated so this file does not contain it.
SUBSET = "⊡ " + "∈ ■"

# Every spelling of the assertion that is forbidden. Also concatenated.
FORBIDDEN = (
    "⊡ " + "∈ ■",
    "⊡" + "∈■",
    "■ " + "∈ ⊡",
    "■" + "∈⊡",
)

# Words that make the assertion a refusal rather than a claim.
#
# These are patterns, not bare substrings, and the difference is the whole
# point. A bare "not " is satisfied by "cannot ", by "annotate " and by
# "notion " — none of which refuse anything. Every entry below is anchored
# to a word boundary, so only the word itself counts.
NEGATORS = (r"\bnot\b", r"\bnot be\b", r"\bnot absorption\b",
            r"\bnever\b", r"\bnever become\b", r"\bavoid\b", r"\brefus\w*",
            r"\brather than\b", r"\binstead of\b", r"\bmust not\b",
            r"\bforbid\w*", r"⊄", r"∉", r"\bwithout\b", r"\bwarn\w*",
            r"\bfalse\b", r"\bwrong\b", r"\bfailure mode\b", r"\bneither\b")

NEGATOR_RE = re.compile("|".join(f"(?:{p})" for p in NEGATORS), re.IGNORECASE)

# A sentence break: a terminator followed by whitespace and something that
# starts a word. The capital is what keeps "§4.2" and "merged.md" from
# splitting a sentence down the middle.
SENTENCE_BREAK = re.compile(r"[.!?](?=\s+[\"'`\[\(*#]*[A-Z])")

# The engine's own files: held to the strict rule — an occurrence must be
# negated on its own line. The list is gathered, not written out, so a file
# added later cannot slip past unchecked.
def _engine_files() -> list[Path]:
    files = [ROOT / "identity.g", ROOT / "genesis"]
    files.extend(sorted(SRC.glob("*.py")))
    # The card is the character's own prompt — it goes into the model's system
    # message on every turn, so it is part of the engine, not a side note.
    # It is held to the *engine* window, not the document window, because its
    # one reference to the refused relation is self-grounded: the negation
    # ("refuse") sits on the same line as the mention. That was fixed in that
    # order — the card was re-grounded first, and only then was it put under
    # the stricter window.
    files.append(ROOT / "card.md")
    # The editable copy of the chat prompt is the prompt itself, so it is
    # held to the same rule: it may not assert the relation, and the card
    # it is generated from may not either.
    files.append(ROOT / "prompt.debug.md")
    return files


# The readings: held to a wider window, because a document may legitimately
# say the forbidden thing is what must not happen, with the negation in the
# next sentence. Gathered for the same reason.
#
# The set is the *published* set, and that is deliberate. `visibility`
# owns it, because it is the same question the visibility contract asks:
# what does this repo publish? A `*.local.md` file is gitignored, machine
# local and private by location, so it is excluded — otherwise the scan set
# would differ on this machine and on a fresh clone, and a divergence there
# would look like a flaky test. `bin/` is excluded for the same reason: it is
# borrowed local infrastructure and is not shipped, so it is not part of the
# published contract the rail defends.
def _doc_files() -> list[Path]:
    import visibility

    # The engine files are excluded: they are held to the stricter window,
    # and scanning them twice would count every mention twice.
    engine_only = {"card.md", "prompt.debug.md"}
    files = [p for p in visibility.published_docs(ROOT)
             if p.name not in engine_only]
    files.extend(sorted((ROOT / "tests").glob("*.py")))
    return files


@dataclass
class Scan:
    """What the scan found."""

    bad: list[tuple[str, int, str]] = field(default_factory=list)
    seen: int = 0
    files: int = 0

    @property
    def ok(self) -> bool:
        return not self.bad


def _occurrences(text: str):
    """Every forbidden-relation occurrence in the text, as spans."""
    for pattern in FORBIDDEN:
        at = text.find(pattern)
        while at >= 0:
            yield at, at + len(pattern)
            at = text.find(pattern, at + len(pattern))


def _sentence_of(text: str, start: int, end: int) -> str:
    """The sentence that holds the occurrence at start..end.

    A negator counts only if it is in the same sentence as the assertion.
    "This is not merely a metaphor" is a different sentence from the
    assertion it follows, and it refuses nothing — a bare word match cannot
    tell those two apart, and the difference between them is the difference
    between a rail and a rubber band.

    A window is still a window: the sentence may run across lines, so the
    scope is taken over the whole window, not over the one line.
    """
    cut = 0
    for match in SENTENCE_BREAK.finditer(text):
        if match.end() > start:
            break
        cut = match.end()
    stop = len(text)
    match = SENTENCE_BREAK.search(text, end)
    if match:
        stop = match.start()
    return text[cut:stop]


def scan(files: list[Path], window: int) -> Scan:
    """Look for the forbidden assertion, and check it is negated nearby.

    The window says how far the negator may sit from the assertion. What it
    may not do is sit in a different sentence.
    """
    result = Scan()
    for path in files:
        if not path.exists():
            continue
        result.files += 1
        lines = path.read_text(encoding="utf-8").splitlines()
        for n, line in enumerate(lines, 1):
            if not any(pattern in line for pattern in FORBIDDEN):
                continue
            result.seen += 1
            lo = max(0, n - 1 - window)
            hi = min(len(lines), n + window)
            block = lines[lo:hi]
            text = ""
            offsets = []
            for part in block:
                offsets.append(len(text))
                text += part + " "
            here = offsets[n - 1 - lo]
            negated = True
            for start, end in _occurrences(text[here:here + len(line)]):
                scope = _sentence_of(text, here + start, here + end)
                if not NEGATOR_RE.search(scope):
                    negated = False
                    break
            if not negated:
                result.bad.append((path.name, n, line.strip()))
    return result


def check() -> Scan:
    """Enforce the guard rail across the engine and the readings."""
    engine = scan(_engine_files(), 0)
    docs = scan(_doc_files(), 3)
    return Scan(bad=engine.bad + docs.bad,
                seen=engine.seen + docs.seen,
                files=engine.files + docs.files)
