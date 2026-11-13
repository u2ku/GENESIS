"""VISIBILITY — the write-time visibility contract.

**Untagged means readable.** There is no read-time filtering, no after-the-fact
pruning, no private mode. A file is either in the published repo or it is
not, and if it is in the published repo then anyone can read it as written.

So the contract has to be enforced at *write* time, and the marker is how it
is declared. Every published document carries one:

    <!-- visibility: sam-readable -->

Three values are known:

| value | meaning |
|---|---|
| `sam-readable` | in the published repo. Readable by anyone who can clone it. |
| `operator` | for the operator of this node. Must live on a `*.local.md` path. |
| `node-private` | this node's business. Must live on a `*.local.md` path. |

**A missing marker is a failure, not a default.** Luna's ruling, tightened at
13:01 from "warn" to "fail": the default must never be silently
world-readable. ``genesis check`` reads this module rather than
re-implementing the rule, and a published document without a marker fails the
check.

**Private is written private, by location.** The second half of the contract:
``operator`` and ``node-private`` content does not belong in the published
repo at all — it belongs on a ``*.local.md`` path, which is gitignored. This
module owns that rule too, and :func:`lint` is the belt to its braces: a
document marked ``sam-readable`` that carries private-looking material is a
finding, because the marker is a promise and the lint checks the promise.

Nothing here writes anything. It is the single definition the rest of the
system asks.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

# The marker. Deliberately permissive about horizontal whitespace and case in
# the value, strict about everything else — a marker that is *nearly* right is
# not a marker. In particular it does not span lines: a prose example of the
# marker, wrapped across two lines by a formatter, must not read as a
# declaration. That is the same trap the guard rail spent a day on — a false
# pass riding an accident.
MARKER_RE = re.compile(
    r"<!--[ \t]*visibility:[ \t]*(?P<value>[A-Za-z0-9_-]+)[ \t]*-->",
    re.IGNORECASE,
)

# The marker is a header. It belongs at the top of the document, where a
# reader meets it, and where it cannot be mistaken for an example.
HEADER_LINES = 10

PUBLIC = "sam-readable"
OPERATOR = "operator"
NODE_PRIVATE = "node-private"

KNOWN_VALUES = (PUBLIC, OPERATOR, NODE_PRIVATE)

# The values that must not sit in the published repo.
PRIVATE_VALUES = (OPERATOR, NODE_PRIVATE)

# Material that has no business in a document marked sam-readable. Each entry
# is (name, pattern). The patterns are deliberately narrow: they match things
# that are *identifying*, not things that are merely technical. A loopback
# address and a masked key are not private; a LAN address and a live key are.
PRIVATE_PATTERNS = (
    ("a private-network address",
     re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
                r"|192\.168\.\d{1,3}\.\d{1,3}"
                r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b")),
    ("a tailnet address",
     re.compile(r"\b100\.(?:\d{1,3}\.\d{1,3}\.\d{1,3})\b")),
    ("a tailnet hostname",
     re.compile(r"\b[\w.-]+\.ts\.net\b")),
    ("an ssh key",
     re.compile(r"\bssh-(?:ed25519|rsa|dss)\b")),
    ("an ssh key path",
     re.compile(r"~/\.ssh/|\.ssh/id_[a-z0-9]+")),
    ("a user's home directory",
     re.compile(r"/Users/[A-Za-z0-9._-]+/")),
    ("a live api key",
     re.compile(r"\b(?:sk-ant-|sk-or-|sk-cp-|tskey-|ghp_|gho_)[A-Za-z0-9_-]{16,}")),
    ("a key in an assignment",
     re.compile(r"\b[A-Z][A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD)"
                r"\s*=\s*[\"']?[A-Za-z0-9._-]{16,}")),
)


def marker_of(text: str) -> str | None:
    """The visibility value a document declares, or None if it declares none.

    A document with *two* markers is malformed rather than ambiguous — the
    first one is returned, and :func:`markers_of` reports the rest.
    """
    found = MARKER_RE.search(text)
    return found.group("value").strip().lower() if found else None


def markers_of(text: str) -> list[str]:
    """Every visibility marker in a document, in order."""
    return [m.group("value").strip().lower() for m in MARKER_RE.finditer(text)]


def header_marker_of(text: str) -> str | None:
    """The marker in the document's header, or None.

    Only the header counts. A marker buried in the body is not a
    declaration — it is prose, an example, or a copy-paste.
    """
    head = "\n".join(text.split("\n")[:HEADER_LINES])
    found = MARKER_RE.search(head)
    return found.group("value").strip().lower() if found else None


def has_marker(text: str) -> bool:
    """Does this document declare a visibility, in its header?"""
    return header_marker_of(text) is not None


def lint(text: str) -> list[str]:
    """Findings in a document that is marked public.

    Returns a list of human-readable findings, empty when the document holds
    nothing private-looking. Only meaningful for a public document: a
    ``*.local.md`` file is *supposed* to hold this material.
    """
    findings = []
    for name, pattern in PRIVATE_PATTERNS:
        match = pattern.search(text)
        if match:
            findings.append(f"{name}: {match.group(0)}")
    return findings


def published_docs(root: Path | None = None) -> list[Path]:
    """The published documents — the set that must carry a marker.

    This is the *published* set, and it is the same on every machine: the
    repository's own markdown, minus the ``*.local.md`` files, which are
    gitignored, machine-local, and private by location. A file that exists
    only on one machine must never change what the contract means on another.

    The set is gathered, not written out, so a document added later joins it
    without anybody remembering to add it. ``tests/test_visibility.py`` holds
    that line: it fails loudly if the set stops being gathered, if a published
    document arrives without a marker, and if a published document is no
    longer in the guard rail's scan set.
    """
    root = root or ROOT
    docs = root / "docs"
    files = [p for p in sorted(docs.glob("*.md"))
             if p.is_file() and not p.name.endswith(".local.md")]
    files.append(root / "README.md")
    files.append(root / "card.md")
    # The editable copy of the chat prompt is an engine input, not a reading:
    # it goes into the model's system message on every turn. It carries the
    # card's own marker because it begins with the card.
    files.append(root / "prompt.debug.md")
    return files
