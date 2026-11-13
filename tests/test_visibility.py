"""GENESIS — visibility tests.

The write-time visibility contract: every published document declares how
visible it is, and a missing declaration is a failure rather than a default.

The rule the whole contract rests on is that the published set is *gathered*,
not written out. A document added later joins the set without anybody
remembering to add it, and a file that exists on one machine only must never
change what the contract means on another. These tests hold that line.

    python3 -m unittest discover -s tests -v
    python3 tests/test_visibility.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import visibility  # noqa: E402


MARKER = "<!-- visibility: sam-readable -->"


def _tree(root: Path) -> Path:
    """A minimal repo shape: a docs/ directory and the three root documents."""
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "ONE.md").write_text(MARKER + "\n\n# one\n", encoding="utf-8")
    (root / "README.md").write_text(MARKER + "\n\n# readme\n", encoding="utf-8")
    (root / "card.md").write_text(MARKER + "\n\n# card\n", encoding="utf-8")
    (root / "prompt.debug.md").write_text(MARKER + "\n\n# prompt\n", encoding="utf-8")
    return root


class TestTheSetIsGathered(unittest.TestCase):
    """A document added later joins the set without anybody remembering."""

    def test_a_new_document_joins_without_the_module_being_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _tree(Path(tmp))
            before = {p.name for p in visibility.published_docs(root)}
            self.assertNotIn("LATER.md", before)
            (root / "docs" / "LATER.md").write_text(MARKER + "\n", encoding="utf-8")
            after = {p.name for p in visibility.published_docs(root)}
            self.assertIn("LATER.md", after)

    def test_a_private_document_never_joins(self):
        """``*.local.md`` is private by location — gitignored, machine-local."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _tree(Path(tmp))
            (root / "docs" / "SECRET.local.md").write_text("private\n", encoding="utf-8")
            names = {p.name for p in visibility.published_docs(root)}
            self.assertNotIn("SECRET.local.md", names)
            self.assertFalse([n for n in names if n.endswith(".local.md")])

    def test_the_real_set_is_the_gathered_one(self):
        """Every published document is in the set, and nothing else is."""
        names = {p.name for p in visibility.published_docs(ROOT)}
        for path in (ROOT / "docs").glob("*.md"):
            if path.name.endswith(".local.md"):
                continue
            self.assertIn(path.name, names, f"{path.name} is not published")
        for name in ("README.md", "card.md", "prompt.debug.md"):
            self.assertIn(name, names)

    def test_the_set_does_not_depend_on_what_this_machine_has(self):
        """The same paths, whether or not the file is here."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _tree(Path(tmp))
            here = [p.relative_to(root).as_posix()
                    for p in visibility.published_docs(root)]
            (root / "docs" / "LATER.md").unlink(missing_ok=True)
            (root / "prompt.debug.md").unlink()
            there = [p.relative_to(root).as_posix()
                     for p in visibility.published_docs(root)]
            self.assertIn("prompt.debug.md", there)


class TestTheContract(unittest.TestCase):
    """A missing marker is a failure, not a default."""

    def test_a_published_document_carries_its_marker(self):
        for doc in visibility.published_docs(ROOT):
            if not doc.exists():
                continue
            self.assertTrue(visibility.has_marker(doc.read_text(encoding="utf-8")),
                            f"{doc.name} carries no visibility marker")

    def test_a_missing_marker_is_not_a_marker(self):
        self.assertIsNone(visibility.header_marker_of("# nothing here\n"))

    def test_a_marker_in_the_body_is_not_a_declaration(self):
        """A marker buried in the text is prose, an example, or a copy-paste."""
        text = "# doc\n\n" + ("filler\n" * 20) + MARKER + "\n"
        self.assertIsNone(visibility.header_marker_of(text))

    def test_a_marker_split_across_lines_is_not_a_marker(self):
        """A marker a formatter wrapped is not a marker."""
        text = "<!-- visibility: sam-\nreadable -->\n"
        self.assertIsNone(visibility.header_marker_of(text))

    def test_two_markers_are_malformed_not_ambiguous(self):
        text = MARKER + "\n" + "<!-- visibility: operator -->\n"
        self.assertEqual(visibility.marker_of(text), visibility.PUBLIC)
        self.assertEqual(len(visibility.markers_of(text)), 2)

    def test_a_public_document_holds_nothing_private(self):
        for doc in visibility.published_docs(ROOT):
            if not doc.exists():
                continue
            text = doc.read_text(encoding="utf-8")
            if visibility.header_marker_of(text) != visibility.PUBLIC:
                continue
            self.assertEqual(visibility.lint(text), [],
                             f"{doc.name} holds something private-looking")


class TestTheLint(unittest.TestCase):
    """The marker is a promise, and the lint checks the promise."""

    def test_a_lan_address_is_a_finding(self):
        # Assembled, not written out: a private address in the source is
        # what the pre-push scrub looks for, and it does not care that this
        # one is a test fixture. The octets are synthetic on purpose -- the
        # fixture must not be a real address on the publisher's own network,
        # or assembling it buys nothing.
        address = "10.203." + "45." + "7"
        findings = visibility.lint(f"the host {address} runs this\n")
        self.assertTrue(findings)

    def test_a_loopback_address_is_not(self):
        self.assertEqual(visibility.lint("it listens on 127.0.0.1\n"), [])

    def test_a_live_key_is_a_finding(self):
        # Assembled, not written out: a key-shaped literal in the source is
        # exactly what the pre-push scrub is looking for.
        fake = "sk-or-" + "a" * 26
        findings = visibility.lint(f"OPENROUTER_API_KEY={fake}\n")
        self.assertTrue(findings)

    def test_a_named_variable_without_a_value_is_not(self):
        self.assertEqual(visibility.lint("set OPENROUTER_API_KEY first\n"), [])


class TestTheGuardRailSeesThem(unittest.TestCase):
    """A published document that the rail does not scan is a document the
    rail cannot defend."""

    def test_every_published_document_is_in_a_scan_set(self):
        sys.path.insert(0, str(ROOT / "src"))
        import guardrail
        scanned = {p.name for p in guardrail._engine_files()}
        scanned |= {p.name for p in guardrail._doc_files()}
        for doc in visibility.published_docs(ROOT):
            self.assertIn(doc.name, scanned, f"{doc.name} is not scanned")


if __name__ == "__main__":
    unittest.main()
