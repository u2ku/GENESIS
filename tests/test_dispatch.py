"""GENESIS — dispatch tests.

The rule the CLI lives by: an unknown first token is a question for Genesis,
never an error. And a command that takes no bare words matches only when
what follows it is all flags — so "what is I" is a question, not an alias.

    python3 -m unittest discover -s tests -v
    python3 tests/test_dispatch.py
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_cli():
    """The CLI is an extensionless executable; load it as a module.

    It is registered in sys.modules first — dataclasses look the module up
    by name to resolve string annotations and field types.
    """
    loader = importlib.machinery.SourceFileLoader("genesis_cli", str(ROOT / "genesis"))
    spec = importlib.util.spec_from_loader("genesis_cli", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["genesis_cli"] = module
    loader.exec_module(module)
    return module


cli_module = load_cli()


def cli(*args):
    proc = subprocess.run([sys.executable, str(ROOT / "genesis"), *args],
                          capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


class TestClassify(unittest.TestCase):
    def test_commands_are_recognised(self):
        self.assertEqual(cli_module.classify(["identity"]), ("id", []))
        self.assertEqual(cli_module.classify(["plan"]), ("prediction", []))
        self.assertEqual(cli_module.classify(["now"]), ("stamp", []))
        self.assertEqual(cli_module.classify(["safety"]), ("guardrail", []))
        self.assertEqual(cli_module.classify(["check"]), ("check", []))

    def test_flags_after_a_command_still_match(self):
        self.assertEqual(cli_module.classify(["identity", "--explain"]),
                         ("id", ["--explain"]))
        self.assertEqual(cli_module.classify(["check", "--probe"]),
                         ("check", ["--probe"]))

    def test_a_question_is_a_question(self):
        """A command that takes no bare words only matches on flags alone."""
        self.assertEqual(cli_module.classify(["what", "is", "I"]),
                         (None, ["what", "is", "I"]))
        self.assertEqual(cli_module.classify(["who", "are", "you"]),
                         (None, ["who", "are", "you"]))

    def test_unknown_first_token_is_free_text(self):
        self.assertEqual(cli_module.classify(["notacommand"]),
                         (None, ["notacommand"]))
        self.assertEqual(cli_module.classify(["show", "me"]),
                         (None, ["show", "me"]))

    def test_commands_that_take_values_always_match(self):
        self.assertEqual(cli_module.classify(["compare", "A", "B"]),
                         ("shield", ["A", "B"]))
        self.assertEqual(cli_module.classify(["symbols", "numergram"]),
                         ("symbols", ["numergram"]))
        self.assertEqual(cli_module.classify(["handoff", "gemini"]),
                         ("handoff", ["gemini"]))

    def test_help_is_special_cased(self):
        for token in ("help", "?", "--help", "-h"):
            self.assertEqual(cli_module.classify([token]), ("help", []))

    def test_empty_is_the_landing(self):
        self.assertEqual(cli_module.classify([]), (None, []))


class TestGlobals(unittest.TestCase):
    def test_leading_flags_are_taken_off_the_front(self):
        ctx, rest = cli_module.split_globals(["--plain", "--backend", "openrouter",
                                              "id"])
        self.assertTrue(ctx.plain)
        self.assertEqual(ctx.backend, "openrouter")
        self.assertEqual(rest, ["id"])

    def test_free_text_is_not_mangled(self):
        ctx, rest = cli_module.split_globals(["tell", "me", "about", "--plain"])
        self.assertFalse(ctx.plain)
        self.assertEqual(rest, ["tell", "me", "about", "--plain"])


class TestCliContract(unittest.TestCase):
    def test_identity_is_exactly_the_constant(self):
        code, out, _ = cli("identity")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "∩(■, ⊡) = I")

    def test_plan_carries_the_arc(self):
        code, out, _ = cli("plan")
        self.assertEqual(code, 0)
        self.assertIn("∅ → ⊡ → $ → ☠️ → ☽ → ■ → I", out)

    def test_help_carries_the_phrase(self):
        code, out, _ = cli("help")
        self.assertEqual(code, 0)
        self.assertIn("your own words", out)

    def test_check_is_not_broken(self):
        """The contract is *not broken*, not *healthy here*.

        A fresh clone has no Luna, no routers and nothing on PATH, so the
        check says *degraded* and exits 0 — a clone that has not been
        configured has not been broken. Asserting *healthy* would make
        this suite red on every machine but the one it was written on,
        which is the machine-dependence the guard rail's own set rule
        exists to remove.
        """
        code, out, _ = cli("check")
        self.assertEqual(code, 0, out)
        verdict = out.strip().splitlines()[-1].strip()
        self.assertIn(verdict, ("healthy", "degraded"), out)

    def test_guardrail_is_green(self):
        code, out, _ = cli("guardrail")
        self.assertEqual(code, 0, out)
        self.assertIn("no unnegated", out)

    def test_a_symbol_block_is_bare(self):
        code, out, _ = cli("symbols", "constraint")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "⊡ ∉ ■")

    def test_an_unknown_block_is_an_error_not_a_crash(self):
        code, _, err = cli("symbols", "nosuchblock")
        self.assertEqual(code, 2)
        self.assertIn("unknown block", err)

    def test_pipeline_output_is_free_of_colour(self):
        """The tests capture stdout through a pipe; decoration must be absent."""
        code, out, _ = cli("check")
        self.assertNotIn("\033[", out)


class TestGuardrailCoverage(unittest.TestCase):
    """The scan is gathered, not written out — a new file cannot slip past."""

    def test_every_source_file_is_scanned(self):
        sys.path.insert(0, str(ROOT / "src"))
        import guardrail
        scanned = {p.name for p in guardrail._engine_files()}
        for path in (ROOT / "src").glob("*.py"):
            self.assertIn(path.name, scanned, f"{path.name} is not scanned")

    def test_the_entry_point_and_the_constant_are_scanned(self):
        sys.path.insert(0, str(ROOT / "src"))
        import guardrail
        scanned = {p.name for p in guardrail._engine_files()}
        self.assertIn("genesis", scanned)
        self.assertIn("identity.g", scanned)

    def test_every_published_reading_is_scanned(self):
        sys.path.insert(0, str(ROOT / "src"))
        import guardrail
        scanned = {p.name for p in guardrail._doc_files()}
        for path in (ROOT / "docs").glob("*.md"):
            if path.name.endswith(".local.md"):
                # Private by location. It is gitignored and exists only on
                # the machine that wrote it, so including it would make the
                # scan set differ here and on a fresh clone — and a divergence
                # there looks like a flaky test (F12).
                self.assertNotIn(path.name, scanned)
                continue
            self.assertIn(path.name, scanned, f"{path.name} is not scanned")
        self.assertIn("README.md", scanned)
        self.assertIn("card.md", {p.name for p in guardrail._engine_files()})
        self.assertIn("prompt.debug.md",
                      {p.name for p in guardrail._engine_files()})

    def test_the_scan_set_is_the_same_on_every_machine(self):
        """The published set does not depend on what this machine happens to have."""
        sys.path.insert(0, str(ROOT / "src"))
        import guardrail
        import visibility
        names = {p.name for p in guardrail._doc_files()}
        self.assertNotIn("HANDOFF-GEMINI-private.local.md", names)
        # Every published document is in the set, and nothing that is not
        # published is.
        for path in visibility.published_docs(ROOT):
            if path.name in ("card.md", "prompt.debug.md"):
                continue  # held to the engine window instead
            self.assertIn(path.name, names)
        self.assertFalse([n for n in names if n.endswith(".local.md")])


class TestTheDoorAndTheTools(unittest.TestCase):
    """The door takes questions; the engine tool takes commands. Never mixed."""

    def test_an_unknown_backend_is_refused(self):
        code, _, err = cli("--backend", "bogus", "what is I")
        self.assertEqual(code, 2)
        self.assertIn("unknown backend", err)

    def test_command_only_refuses_an_unknown_command(self):
        code, _, err = cli("--command-only", "nosuchcommand")
        self.assertEqual(code, 2)
        self.assertIn("unknown command", err)

    def test_the_engine_tool_never_reaches_the_door(self):
        """A bad sub-command must not become a nested conversation."""
        sys.path.insert(0, str(ROOT / "src"))
        import harness
        out = harness.t_engine({"command": "nosuchcommand"})
        self.assertIn("unknown command", out)
        self.assertNotIn("The term", out)

    def test_a_free_text_question_still_reaches_the_door(self):
        """The opposite: free text is a question, and questions are wanted."""
        self.assertEqual(cli_module.classify(["what", "is", "I"]),
                         (None, ["what", "is", "I"]))


class TestOneSourceOfTruth(unittest.TestCase):
    """The constant, the order and the prompt each live in one place."""

    def test_the_singularity_view_follows_the_canonical_order(self):
        """One list, in grammar.py — the view and the copy both read it."""
        sys.path.insert(0, str(ROOT / "src"))
        import grammar
        blocks = cli_module.load_blocks()
        order = [k for k in grammar.SINGULARITY_ORDER if k in blocks]
        shown = cli("singularity")[1]
        # The block labels are the lines that sit at the left margin and
        # carry no spaces of their own.
        labels = [line.strip() for line in shown.splitlines()
                  if line.startswith("  ") and not line.startswith("   ")
                  and line.strip() and " " not in line.strip()]
        self.assertEqual(labels, order)

    def test_the_playlist_prompt_is_printed_verbatim(self):
        sys.path.insert(0, str(ROOT / "src"))
        import playlist
        code, out, _ = cli("playlist")
        self.assertEqual(code, 0)
        self.assertIn(playlist.PROMPT, out)

    def test_the_chat_reads_the_pinned_constant(self):
        sys.path.insert(0, str(ROOT / "src"))
        import harness
        raw = (ROOT / "identity.g").read_text(encoding="utf-8").strip()
        self.assertEqual(harness.pinned_identity(), raw)


class TestTheEditablePrompt(unittest.TestCase):
    """The chat prompt can be edited as a file, and the edit is live.

    The copy is redirected into a temporary directory, so these tests never
    touch the operator's own file.
    """

    def setUp(self):
        sys.path.insert(0, str(ROOT / "src"))
        import harness
        self.harness = harness
        self.original = harness.PROMPT_DEBUG
        self.tmp = tempfile.TemporaryDirectory()
        harness.PROMPT_DEBUG = Path(self.tmp.name) / "prompt.debug.md"

    def tearDown(self):
        self.harness.PROMPT_DEBUG = self.original
        self.tmp.cleanup()

    def test_without_the_copy_the_prompt_is_the_card(self):
        prompt = self.harness.system_prompt()
        self.assertIn("You are Genesis", prompt)
        self.assertIn("The tools are your hands", prompt)
        self.assertEqual(self.harness.prompt_source(), "card.md + engine")

    def test_the_grounding_marker_is_resolved_in_the_prompt(self):
        prompt = self.harness.system_prompt()
        self.assertNotIn(self.harness.GROUNDING_MARKER, prompt)
        self.assertIn(self.harness.grounding_block(), prompt)

    def test_the_copy_is_written_from_the_source_files(self):
        path = self.harness.write_debug_prompt()
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(self.harness.base_prompt()))
        self.assertIn(self.harness.TOOLS_MARKER, text)
        self.assertIn(self.harness.base_tools(), text)

    def test_regenerating_keeps_the_edits_it_replaces(self):
        self.harness.write_debug_prompt()
        self.harness.PROMPT_DEBUG.write_text("EDITED", encoding="utf-8")
        self.harness.write_debug_prompt()
        backup = self.harness.PROMPT_DEBUG.with_name(
            self.harness.PROMPT_DEBUG.name + ".bak")
        self.assertEqual(backup.read_text(encoding="utf-8"), "EDITED")

    def test_the_copy_carries_the_tool_definitions(self):
        self.harness.write_debug_prompt()
        text = self.harness.PROMPT_DEBUG.read_text(encoding="utf-8")
        self.assertIn(self.harness.TOOLS_MARKER, text)
        self.assertEqual(len(self.harness.tool_specs()), len(self.harness.TOOLS))

    def test_the_tools_section_is_not_part_of_the_system_message(self):
        self.harness.write_debug_prompt()
        self.assertEqual(self.harness.system_prompt(),
                         self.harness.base_prompt())

    def test_an_edited_description_reaches_the_wire(self):
        self.harness.write_debug_prompt()
        text = self.harness.PROMPT_DEBUG.read_text(encoding="utf-8")
        text = text.replace(
            '"description": "The sixteen figures and the sixteen points '
            'of the square."',
            '"description": "EDITED BY THE OPERATOR."')
        self.harness.PROMPT_DEBUG.write_text(text, encoding="utf-8")
        specs = {t["name"]: t for t in self.harness.tool_specs()}
        self.assertEqual(specs["figures"]["description"],
                         "EDITED BY THE OPERATOR.")

    def test_a_malformed_tool_block_stops_the_turn(self):
        self.harness.write_debug_prompt()
        text = self.harness.PROMPT_DEBUG.read_text(encoding="utf-8")
        text = text.replace('"name": "figures"', '"name": "figures",')
        self.harness.PROMPT_DEBUG.write_text(text, encoding="utf-8")
        with self.assertRaises(self.harness.PromptError):
            self.harness.tool_specs()

    def test_a_tool_with_no_engine_behind_it_stops_the_turn(self):
        self.harness.write_debug_prompt()
        text = self.harness.PROMPT_DEBUG.read_text(encoding="utf-8")
        text = text.replace('"name": "figures"', '"name": "figures2"')
        self.harness.PROMPT_DEBUG.write_text(text, encoding="utf-8")
        with self.assertRaises(self.harness.PromptError):
            self.harness.tool_specs()

    def test_the_copy_is_the_live_prompt_when_it_is_there(self):
        self.harness.write_debug_prompt()
        self.harness.PROMPT_DEBUG.write_text("EDITED", encoding="utf-8")
        self.assertEqual(self.harness.system_prompt(), "EDITED")
        self.assertEqual(self.harness.prompt_source(), "prompt.debug.md")

    def test_resetting_takes_the_copy_away(self):
        self.harness.write_debug_prompt()
        self.assertTrue(self.harness.reset_debug_prompt())
        self.assertFalse(self.harness.reset_debug_prompt())
        self.assertEqual(self.harness.prompt_source(), "card.md + engine")

    def test_the_cli_reports_the_live_source(self):
        code, out, _ = cli("prompt", "--source")
        self.assertEqual(code, 0)
        self.assertIn("live:", out)


class TestTheGrounding(unittest.TestCase):
    """The primary signs ride in the prompt, and cannot fall behind the
    engine."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "src"))
        import harness
        self.harness = harness

    def test_the_prompt_carries_the_three_primary_signs(self):
        import grammar
        forms = grammar.forms()
        prompt = self.harness.base_prompt()
        self.assertIn(self.harness.pinned_identity(), prompt)
        self.assertIn(forms["constraint"], prompt)
        self.assertIn(forms["arc"], prompt)

    def test_the_signs_come_from_the_engine_not_from_a_literal(self):
        """Change the engine, and the block follows it."""
        import grammar
        block = self.harness.grounding_block()
        self.assertIn(grammar.forms()["arc"], block)
        self.assertIn(self.harness.pinned_identity(), block)

    def test_a_prompt_without_the_signs_is_reported_as_missing(self):
        self.assertEqual(self.harness.missing_grounding("nothing here"), [
            f"identity ({self.harness.pinned_identity()})",
            f"guard rail ({__import__('grammar').forms()['constraint']})",
            f"arc ({__import__('grammar').forms()['arc']})"])
        self.assertEqual(
            self.harness.missing_grounding(self.harness.base_prompt()), [])

    def test_a_question_about_a_primary_sign_asks_for_the_engine(self):
        """The nudge has to fire on the person's words, not only on jargon."""
        terms = self.harness.GROUNDED_TERMS
        for question in ("what does ⊡ mean?", "what is the constant?",
                         "explain the symbols", "what does ■ stand for",
                         "what is the arc?", "compare these two moments"):
            self.assertTrue(
                any(t in question.lower() for t in terms),
                f"no grounded term in {question!r}")


class TestVoice(unittest.TestCase):
    """The door is silent unless asked, and --no-tts always wins."""

    def _spoken(self, flags):
        sys.path.insert(0, str(ROOT / "src"))
        import harness
        spoken = []
        original_speak, original_talk = harness.speak, harness.talk
        harness.speak = lambda text, voice=None: spoken.append(voice)
        harness.talk = lambda *a, **k: "a reply"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                cli_module.main(flags + ["a question"])
        finally:
            harness.speak, harness.talk = original_speak, original_talk
        return spoken

    def test_the_door_is_silent_by_default(self):
        self.assertEqual(self._spoken([]), [])

    def test_speak_asks_for_the_voice(self):
        self.assertTrue(self._spoken(["--speak"]))

    def test_no_tts_overrides_speak(self):
        self.assertEqual(self._spoken(["--speak", "--no-tts"]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
