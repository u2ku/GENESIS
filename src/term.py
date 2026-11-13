"""TERM — Genesis's terminal kit.

The front end: colour, geometry, and the chrome that carries a turn. Ported
from Luna's own CLI (`~/Luna/luna_chat.py`), retuned for Genesis — quiet, and
near-monochrome, with a single accent for his voice and for the grounding
tokens.

This module is a leaf. It imports nothing outside the standard library and
knows nothing about Genesis: no grammar, no harness, no filesystem. That is
what makes it testable, and what keeps every style decision in one file.

Two rules hold the whole system together:

  * Decoration is applied only to a terminal. Piped or redirected output is
    the raw payload, byte for byte.
  * The accent is used in three places only — the identity constant, a
    grounding stamp, and Genesis's own speech. Everything else is grey.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
import threading
import time
from dataclasses import dataclass
from typing import IO

# ------------------------------------------------------------------ palette --

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

SOFT = "\033[38;5;245m"     # grey      — labels, descriptions, secondary
BRIGHT = "\033[38;5;253m"   # near-white — values, headings
ACCENT = "\033[38;5;110m"   # THE ONE ACCENT — Genesis's voice, grounding
OK = "\033[38;5;114m"       # semantic  — a check that passed
BAD = "\033[38;5;203m"      # semantic  — a check that failed

CLEAR_LINE = "\033[2K"

# The glyphs the front end uses, probed for the terminal's encoding.
_GLYPHS = "⊡■∅☽◐◑◒◓─›◆✓✗▸·"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


# ------------------------------------------------------------- capabilities --

def _isatty(stream: IO[str]) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def supports_color(stream: IO[str] | None = None) -> bool:
    """True when this stream can carry colour.

    A terminal, not a pipe; no NO_COLOR; a terminal that understands the
    codes. Luna's rule, unchanged.
    """
    stream = sys.stdout if stream is None else stream
    return (
        _isatty(stream)
        and not os.environ.get("NO_COLOR")
        and os.environ.get("TERM", "") != "dumb"
    )


def supports_unicode(stream: IO[str] | None = None) -> bool:
    """True when this stream's encoding can carry the front end's glyphs."""
    stream = sys.stdout if stream is None else stream
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        _GLYPHS.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def display_safe(text: str, stream: IO[str] | None = None) -> str:
    """Replace the glyphs a limited terminal encoding cannot represent."""
    stream = sys.stdout if stream is None else stream
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        return text.encode(encoding, errors="replace").decode(encoding)
    except LookupError:
        return text


def write(text: str, stream: IO[str] | None = None, end: str = "\n") -> None:
    """Write to the terminal, degrading instead of crashing on it.

    The constant, the arc and the shield chart all carry glyphs that a
    limited encoding cannot hold. Without this they would raise on the way
    out; with it they arrive as stand-ins.
    """
    stream = sys.stdout if stream is None else stream
    try:
        stream.write(display_safe(text, stream) + end)
        stream.flush()
    except (OSError, ValueError):
        pass


def terminal_width() -> int:
    """The width to lay out to, clamped so the chrome never wraps."""
    return max(36, min(96, shutil.get_terminal_size((76, 24)).columns))


def wrap_message(message: str, width: int) -> list[str]:
    """Wrap prose to width, keeping paragraphs apart and words whole."""
    lines: list[str] = []
    for paragraph in message.splitlines() or [""]:
        if not paragraph:
            lines.append("")
            continue
        lines.extend(
            textwrap.wrap(
                paragraph,
                width=max(12, width),
                replace_whitespace=False,
                drop_whitespace=True,
                break_long_words=False,
                break_on_hyphens=False,
            )
            or [""]
        )
    return lines


def visible_len(text: str) -> int:
    """The width of a string once the colour codes are taken out."""
    return len(_ANSI.sub("", text))


# -------------------------------------------------------------------- style --

@dataclass(frozen=True)
class Style:
    """How the terminal should be painted.

    Frozen and cheap: build one per stream with :func:`make_style` and pass it
    around. With ``color=False`` every paint method returns the text untouched,
    which is what makes the same code serve both a terminal and a pipe.
    """

    color: bool = True
    unicode: bool = True
    width: int = 76

    # -- painting ---------------------------------------------------------

    def paint(self, text: str, *codes: str) -> str:
        if not self.color or not codes or not text:
            return text
        return f"{''.join(codes)}{text}{RESET}"

    def dim(self, text: str) -> str:
        return self.paint(text, DIM)

    def soft(self, text: str) -> str:
        return self.paint(text, SOFT)

    def bright(self, text: str) -> str:
        return self.paint(text, BRIGHT)

    def accent(self, text: str) -> str:
        return self.paint(text, ACCENT)

    def voice(self, text: str) -> str:
        """Genesis speaking. The accent, and nothing else wears it."""
        return self.paint(text, ACCENT)

    def good(self, text: str) -> str:
        return self.paint(text, OK)

    def bad(self, text: str) -> str:
        return self.paint(text, BAD)

    def glyph(self, uni: str, fallback: str) -> str:
        """A glyph, or its plain stand-in when the encoding cannot hold it."""
        return uni if self.unicode else fallback

    # -- chrome -----------------------------------------------------------

    def rule(self, width: int | None = None, char: str | None = None) -> str:
        char = char or self.glyph("─", "-")
        return self.soft(char * (width or self.width))

    def heading(self, text: str, right: str | None = None) -> str:
        """A section heading: the name in white, an optional right-hand note."""
        left = self.paint(text, BOLD, BRIGHT)
        if right is None:
            return "  " + left
        pad = max(1, self.width - visible_len(left) - visible_len(right))
        return "  " + left + " " * pad + self.soft(right)

    def field(self, label: str, value: str, width: int = 18) -> str:
        """An aligned label/value row."""
        return f"  {self.soft(label.ljust(width))} {value}"

    def note(self, text: str) -> str:
        return self.soft(text)

    def wrap(self, text: str, width: int | None = None) -> list[str]:
        return wrap_message(text, width or self.width)

    # -- speech -----------------------------------------------------------

    def bubble(self, message: str, label: str = "GENESIS",
               marker: str = "◆") -> str:
        """One turn of Genesis's speech, wrapped and hanging-indented.

        The body is not painted — the terminal's own foreground is already the
        right colour for prose. The label carries the accent.
        """
        marker = self.glyph(marker, ">")
        prefix = f" {self.paint(label, BOLD, ACCENT)}  {self.paint(marker, ACCENT)} "
        hang = " " * visible_len(prefix)
        lines = wrap_message(message, max(12, self.width - visible_len(prefix)))
        if not lines:
            return prefix.rstrip()
        out = [prefix + lines[0]]
        out.extend(hang + line for line in lines[1:])
        return "\n".join(out)

    def banner(self, title: str, subtitle: str | None = None,
               hint: str | None = None) -> str:
        """The opening chrome: a title, a subtitle, a rule, a hint."""
        width = self.width
        out = ["", "  " + self.paint(title.center(width - 2), BOLD, ACCENT)]
        if subtitle:
            out.append("  " + self.soft(subtitle.center(width - 2)))
        out.append(self.rule(width))
        if hint:
            out.append("  " + self.soft(hint))
        return "\n".join(out)


def make_style(stream: IO[str] | None = None, *, plain: bool = False,
               width: int | None = None) -> Style:
    """The style for this stream."""
    stream = sys.stdout if stream is None else stream
    return Style(
        color=False if plain else supports_color(stream),
        unicode=supports_unicode(stream),
        width=width or terminal_width(),
    )


# ------------------------------------------------------------------ spinner --

class Spinner:
    """A single-line spinner that leaves the transcript untouched.

    It writes to stderr, so a redirected stdout still yields a clean
    transcript. It self-clears on the way out, and only runs where there is a
    terminal to run on.
    """

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    PLAIN_FRAMES = "|/-\\"

    def __init__(self, stream: IO[str] = sys.stderr, *, label: str = "working",
                 enabled: bool | None = None, color: bool | None = None,
                 unicode: bool | None = None):
        self.stream = stream
        self.label = label
        self.enabled = _isatty(stream) if enabled is None else enabled
        self.color = supports_color(stream) if color is None else color
        self.unicode = supports_unicode(stream) if unicode is None else unicode
        self.frames = self.FRAMES if self.unicode else self.PLAIN_FRAMES
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = 0.0
        self._width = 0
        self._lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> "Spinner":
        if not self.enabled or self._thread is not None:
            return self
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._animate, name="genesis-spinner",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=0.5)
        self._thread = None
        with self._lock:
            if self.color:
                self.stream.write("\r" + CLEAR_LINE)
            else:
                self.stream.write("\r" + (" " * self._width) + "\r")
            self.stream.flush()

    def __enter__(self) -> "Spinner":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- the loop ---------------------------------------------------------

    def _animate(self) -> None:
        index = 0
        while not self._stop.is_set():
            elapsed = int(time.monotonic() - self._started)
            frame = self.frames[index % len(self.frames)]
            if self.color:
                frame = f"{ACCENT}{frame}{RESET}"
            label = self.label
            if self.color:
                label = f"{SOFT}{label}{RESET}"
            suffix = f"  {elapsed}s" if elapsed else ""
            text = f" {frame}  {label}{suffix}"
            with self._lock:
                self._width = max(self._width, visible_len(text))
                try:
                    self.stream.write("\r" + text)
                    self.stream.flush()
                except (OSError, ValueError):
                    return
            index += 1
            self._stop.wait(0.12)

    def write(self, text: str) -> None:
        """Put a line above the spinner, without the two of them colliding."""
        with self._lock:
            try:
                if self.enabled:
                    self.stream.write("\r" + CLEAR_LINE)
                self.stream.write(text + "\n")
                self.stream.flush()
            except (OSError, ValueError):
                pass


# ------------------------------------------------------------------ helpers --

def strip_ansi(text: str) -> str:
    """Remove the colour codes, for measuring or for copying."""
    return _ANSI.sub("", text)
