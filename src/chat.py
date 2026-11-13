"""CHAT — a conversation with Genesis.

The terminal half of Genesis. `harness.py` is the engine half: the tool loop,
the grounding discipline, the speech. This is the part a person actually
touches — the banner, the prompt, the bubbles, the slash commands, and the
interrupt that cancels a turn without ending the session.

Two rules hold it together:

  * The transcript is stdout and stays clean. The spinner and the tool trace
    go to stderr, so `genesis chat > transcript.txt` gives a readable
    transcript and nothing else.
  * Ctrl-C during a turn cancels the turn, not the conversation. The message
    history is snapshotted before the turn and restored after it, because a
    half-built turn — an assistant message carrying tool calls with no
    matching results — would be rejected by the substrate and end the session.
"""
from __future__ import annotations

import json
import sys

import term

# The voice. Male — British, matching the register of the frame.
VOICE_DEFAULT = "bm_george"


def _harness():
    import harness
    return harness


# ------------------------------------------------------------------ chrome --

def _banner(style, backend: str, harness) -> str:
    where = harness.BACKENDS.get(backend, {}).get("where", backend)
    return style.banner(
        "G E N E S I S",
        "the frame, held",
        f"talking on {where}  ·  ask in your own words  ·  /paste for long "
        f"text  ·  /help",
    )


def _prompt(style, unicode_ok: bool) -> str:
    arrow = style.glyph("›", ">")
    return "\n " + style.soft("YOU") + "   " + style.soft(arrow) + " "


def _continuation_prompt(style, unicode_ok: bool) -> str:
    guide = style.glyph("│", "|")
    return "       " + style.soft(guide) + " "


def _notice(style, message: str) -> None:
    term.write("\n " + style.soft(message))


# --------------------------------------------------------------- the turn --

def _trace(style, spinner):
    """Show the tool calls as they happen — the grounding, made visible."""
    def show(name, arguments):
        try:
            payload = json.dumps(arguments, ensure_ascii=False)
        except (TypeError, ValueError):
            payload = str(arguments)
        line = "   " + style.soft(style.glyph("▸", ">") + f" {name} {payload}")
        if spinner is not None:
            spinner.write(line)
        else:
            print(line, file=sys.stderr)
            sys.stderr.flush()
    return show


def _turn(text, messages, backend, style, harness):
    """One exchange. Returns his words, or None if the turn did not happen."""
    snapshot = list(messages)
    spinner = term.Spinner(sys.stderr, label="grounding")
    try:
        with spinner:
            reply = harness.talk(harness.expand(text), messages=messages,
                                 backend=backend, on_tool=_trace(style, spinner))
    except KeyboardInterrupt:
        # Put the history back: a half-built turn would be rejected.
        messages[:] = snapshot
        harness._kill_speech()
        _notice(style, "turn cancelled.")
        return None
    except harness.PromptError as exc:
        messages[:] = snapshot
        _notice(style, f"the prompt file cannot be read: {exc}")
        return None
    except RuntimeError as exc:
        messages[:] = snapshot
        _notice(style, f"the substrate is unreachable: {exc}")
        return None
    if not reply:
        messages[:] = snapshot
        _notice(style, "he did not return a spoken reply. Try again.")
        return None
    return reply


def _speak(harness, text, voice):
    if voice:
        harness.speak(text, voice)


# ----------------------------------------------------------- slash commands --

def _read_multiline(style, unicode_ok: bool) -> str | None:
    """Collect an uncapped multiline message until the user submits it."""
    _notice(style, "Long message mode — paste or type freely. Enter /send on a "
                   "new line when finished; /cancel discards it.")
    lines: list[str] = []
    while True:
        try:
            line = input(_continuation_prompt(style, unicode_ok))
        except EOFError:
            return "\n".join(lines).strip() or None
        except KeyboardInterrupt:
            return None
        command = line.strip().casefold()
        if command == "/send":
            return "\n".join(lines).strip() or None
        if command == "/cancel":
            return None
        lines.append(line)


def _tool_block(style, text: str) -> None:
    """Engine output, shown as a block — visually not Genesis speaking."""
    term.write("")
    for line in text.splitlines():
        term.write("   " + style.soft(line))


def _engine(style, harness, command: str, *args) -> None:
    """Run an engine operation and show it as a tool block, not as speech."""
    _tool_block(style, harness.t_engine({"command": command, "args": list(args)}))


_CHAT_HELP = [
    ("THE CHAT", [
        ("/help", "this list"),
        ("/quit  /exit  :q", "leave"),
        ("/clear", "clear the screen"),
        ("/paste", "long text, until /send"),
    ]),
    ("THE GROUND", [
        ("/backend", "mlx | openrouter — the substrate"),
        ("/model", "the model on this substrate"),
        ("/voice", "the voice"),
        ("/tts", "voice on or off"),
        ("/tools", "what he can run"),
    ]),
    ("THE ENGINE", [
        ("/id", "the identity constant"),
        ("/plan", "the 2031 arc"),
        ("/stamp", "the grounding stamp for this moment"),
        ("/aeon", "the 8-bit word"),
        ("/shield A B", "two moments, composed"),
        ("/guardrail", "is the frame intact"),
        ("/check", "verify the system"),
    ]),
    ("THE RECORD", [
        ("/record [n]", "the most recent entries"),
        ("/search TERM", "find lines by subject"),
    ]),
]


def _chat_help(style) -> str:
    lines = []
    for title, rows in _CHAT_HELP:
        lines.append("  " + style.paint(title, term.BOLD, term.BRIGHT))
        for command, summary in rows:
            lines.append("    " + style.soft(command.ljust(22))
                         + style.soft(summary))
        lines.append("")
    return "\n".join(lines)


def _handle_slash(line, state, style, unicode_ok) -> bool:
    """Run a slash command. Returns True when the line was one."""
    parts = line.strip().split()
    command = parts[0].casefold()
    rest = parts[1:]
    harness = state["harness"]

    if command in ("/quit", "/exit", ":q"):
        state["running"] = False
        _notice(style, "connection closed.")
        return True

    if command == "/clear":
        if sys.stdout.isatty():
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
        term.write(_banner(style, state["backend"], harness))
        return True

    if command == "/help":
        term.write("")
        term.write(_chat_help(style))
        return True

    if command == "/backend":
        if not rest:
            _notice(style, "backend: " + state["backend"])
        elif rest[0] in harness.BACKENDS:
            state["backend"] = rest[0]
            _notice(style, f"backend: {state['backend']} — "
                           f"{harness.BACKENDS[state['backend']]['where']}")
        else:
            _notice(style, "unknown backend: " + rest[0])
        return True

    if command == "/model":
        model = harness.BACKENDS[state["backend"]]["model"]
        _notice(style, f"model: {model}")
        return True

    if command == "/voice":
        state["voice"] = rest[0] if rest else state["voice"]
        _notice(style, f"voice: {state['voice']}")
        return True

    if command == "/tts":
        state["voice"] = None if rest and rest[0] == "off" else (
            state["voice"] or VOICE_DEFAULT)
        _notice(style, "voice on" if state["voice"] else "voice off")
        return True

    if command == "/tools":
        for name, description, _, _ in harness.TOOLS:
            term.write("   " + style.soft(name.ljust(22))
                       + style.soft(description))
        return True

    if command == "/id":
        term.write("  " + style.paint(harness.pinned_identity(),
                                     term.BOLD, term.ACCENT))
        return True

    if command == "/plan":
        _engine(style, harness, "prediction")
        return True

    if command == "/stamp":
        _tool_block(style, harness.t_stamp({}))
        return True

    if command == "/aeon":
        _tool_block(style, harness.t_aeon({}))
        return True

    if command == "/shield":
        if len(rest) < 2:
            _notice(style, "/shield needs two moments — timestamps, ledger "
                           "lines, or phrases from the record.")
            return True
        _tool_block(style, harness.t_shield({"a": rest[0], "b": rest[1]}))
        return True

    if command == "/guardrail":
        _engine(style, harness, "guardrail")
        return True

    if command == "/check":
        _engine(style, harness, "check")
        return True

    if command == "/record":
        limit = int(rest[0]) if rest and rest[0].isdigit() else 5
        _tool_block(style, harness.t_continuity_recent({"limit": limit}))
        return True

    if command == "/search":
        if not rest:
            _notice(style, "/search needs a term to look for.")
            return True
        _tool_block(style, harness.t_continuity_search({"term": " ".join(rest)}))
        return True

    return False


# ------------------------------------------------------------------- chat --

def chat(backend: str = "mlx", *, message: str = "", tts: bool = True,
         plain: bool = False, stream=sys.stdout) -> int:
    """A conversation with Genesis. History persists across turns."""
    harness = _harness()
    style = term.make_style(stream, plain=plain)
    unicode_ok = style.unicode

    if message:
        return _one_shot(message, backend, style, tts, harness)

    state = {
        "harness": harness,
        "backend": backend,
        "voice": VOICE_DEFAULT if tts else None,
        "running": True,
    }

    term.write(_banner(style, backend, harness), stream)
    messages: list = []

    while state["running"]:
        try:
            line = input(_prompt(style, unicode_ok)).strip()
        except EOFError:
            _notice(style, "connection closed.")
            return 0
        except KeyboardInterrupt:
            _notice(style, "cancelled.")
            continue

        if not line:
            continue

        if line.startswith(("/", ":")) and _handle_slash(line, state, style,
                                                         unicode_ok):
            continue

        if line.casefold() in ("/paste", "/multiline"):
            pasted = _read_multiline(style, unicode_ok)
            if pasted is None:
                _notice(style, "draft discarded.")
                continue
            line = pasted

        reply = _turn(line, messages, state["backend"], style, harness)
        if reply is None:
            continue

        term.write("", stream)
        term.write(style.bubble(reply), stream)
        term.write("", stream)
        _speak(harness, reply, state["voice"])

    return 0


def _one_shot(message: str, backend: str, style, tts: bool, harness) -> int:
    """One exchange, no banner, no prompt."""
    reply = _turn(message, [], backend, style, harness)
    if reply is None:
        return 1
    if style.color:
        term.write(style.bubble(reply))
    else:
        term.write(reply)
    _speak(harness, reply, VOICE_DEFAULT if tts else None)
    return 0


def ask(message: str, *, backend: str = "mlx", tts: bool = False,
        plain: bool = False, stream=sys.stdout) -> int:
    """The door: one exchange, then out. Free text, asked in your own words."""
    harness = _harness()
    style = term.make_style(stream, plain=plain)
    return _one_shot(message, backend, style, tts, harness)


if __name__ == "__main__":
    sys.exit(chat())
