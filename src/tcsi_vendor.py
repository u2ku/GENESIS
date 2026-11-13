"""TCSI VENDOR — where the grounding engine comes from, and whether it drifted.

The TCSI engine compresses time, date and moon phase into the stamp every
continuity line carries. It is Luna's engine. GENESIS imports it when the
machine has it and falls back to a bundled copy when the machine does not, so
the system runs on a fresh box with no Luna on it.

Three rules, and the order matters:

1. **Luna's live engine wins.** If ``$LUNA_HOME/src/luna/tcsi.py`` exists it is
   imported and used. It is never copied and never edited.
2. **The bundled copy is a fallback, not the source.** When it is used, the
   system says so — ``genesis check`` reports *vendored copy — not verified
   against Luna*. A degraded substrate is a degraded substrate; saying
   otherwise would be a lie.
3. **When both are present, they must agree.** The drift check compares the
   bundled bytes against Luna's and fails on any difference. The pin below is
   over the upstream bytes, so an edit to either side is visible.

Nothing here does I/O beyond reading two files.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

# The provenance marker in the vendored file. The bytes after this line are
# Luna's file, verbatim.
MARKER = b"---8<--- vendored upstream begins after this line ---8<---\n\n"

# sha256 of Luna's src/luna/tcsi.py as of 2026-11-13. Over the upstream bytes,
# excluding nothing — the whole file.
PIN = "765098cbd36b1796ceaccdcd32863a455be8c92d23933221ca92596eacb463e3"

# Where Luna keeps it, relative to her repo root.
UPSTREAM_RELATIVE = Path("src") / "luna" / "tcsi.py"


def luna_home() -> Path:
    """Where Luna's repo is, if it is on this machine at all."""
    return Path(os.environ.get("LUNA_HOME", Path.home() / "Luna"))


def vendored_path() -> Path:
    """The bundled copy, inside this repo."""
    return SRC / "tcsi.py"


def luna_path() -> Path:
    """Luna's engine, as it would be if this machine had it."""
    return luna_home() / UPSTREAM_RELATIVE


def body_sha256(path: Path | None = None) -> str:
    """The sha256 of the vendored file's body — the upstream bytes alone."""
    path = path or vendored_path()
    raw = path.read_bytes()
    if MARKER not in raw:
        # No marker: the whole file is the body (a plain copy).
        return hashlib.sha256(raw).hexdigest()
    tail = raw.split(MARKER, 1)[1]
    return hashlib.sha256(tail).hexdigest()


def pin_ok() -> bool:
    """Does the bundled copy still match the pin?"""
    try:
        return body_sha256() == PIN
    except OSError:
        return False


def drift() -> str | None:
    """Describe how the bundled copy differs from Luna's, or None if they agree.

    Only meaningful when both are present. On a fresh machine Luna's copy is
    absent and there is nothing to drift from — that is a missing file, not a
    drift, and the two are reported differently on purpose.
    """
    luna = luna_path()
    if not luna.exists():
        return None
    try:
        theirs = hashlib.sha256(luna.read_bytes()).hexdigest()
    except OSError as exc:
        return f"cannot read Luna's TCSI engine: {exc}"
    ours = body_sha256()
    if ours == theirs:
        return None
    return (f"the bundled TCSI copy and Luna's have drifted: "
            f"bundled {ours[:12]}…, Luna's {theirs[:12]}…")


def _import_path(path: Path):
    """Import a module from a file path, without touching sys.path."""
    spec = importlib.util.spec_from_file_location("genesis_tcsi_vendored", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve():
    """The TCSI engine, and where it came from.

    Returns ``(module, source, degraded)`` where *source* is ``"luna"`` or
    ``"vendored"``, and *degraded* is True when the bundled copy is in use and
    could not be verified against Luna.
    """
    luna = luna_path()
    if luna.exists():
        try:
            return _import_path(luna), "luna", False
        except Exception:
            # Her engine is there and could not be loaded. Fall through to the
            # bundled copy — but the caller must be told, because a broken
            # upstream is a finding, not a detail.
            pass
    try:
        module = _import_path(vendored_path())
    except Exception as exc:
        raise ImportError(
            f"cannot load the TCSI engine: no vendored copy at {vendored_path()} "
            f"({exc})"
        ) from exc
    degraded = not luna.exists() or drift() is not None
    return module, "vendored", degraded
