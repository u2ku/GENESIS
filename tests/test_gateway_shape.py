"""GENESIS — the gateway SHAPE branch.

The wizard has two shapes to configure, and until this file existed only one
of them had ever been executed:

- **Anthropic-shaped** — the gateway serves `/v1/messages`. Claude Code
  points straight at it, and there is no shim.
- **OpenAI-shaped** — the gateway serves `/v1/chat/completions` and answers
  404 on `/v1/messages`. The shim is *required*, and it is what Claude Code
  has to be pointed at.

Every other test in this suite uses a fake Anthropic-shaped gateway, so the
OpenAI-shaped path — the branch WIZARD.md 1.3 calls "the case the
requirement names" — was never walked by anything. These tests walk it.

What they hold:

- on the OpenAI shape, `ANTHROPIC_BASE_URL` in `~/.claude/settings.json` is
  the **shim's** URL, not the provider's. Pointing Claude Code at an
  OpenAI-shaped provider means it posts the key at `api.anthropic.com` and
  gets a 401 — a machine that looks configured and talks to nothing;
- the shim's unit carries the upstream on **every** platform, the macOS
  LaunchAgent included. A plist without it starts a shim that exits 2 on
  every attempt, under `KeepAlive`, forever;
- Codexify is **detected** by default, and when it is not found it is
  reported as a manual step — never skipped in silence. Codexify is one of
  the three consumers the requirement names.

    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import bootstrap  # noqa: E402
import keydetect  # noqa: E402

# A gateway that speaks Anthropic Messages, and one that speaks only OpenAI.
ANTHROPIC_GATEWAY = keydetect.Endpoint(
    "test-anthropic", keydetect.ANTHROPIC, "https://api.anthropic.com",
    "ANTHROPIC_API_KEY", ("claude-haiku-4-5-20251001",), ("x-api-key",))

OPENAI_GATEWAY = keydetect.Endpoint(
    "test-openai", keydetect.OPENAI, "https://api.openai.com/v1",
    "OPENAI_API_KEY", ("gpt-4.1-mini",), ("bearer",))


class ShapeCase(unittest.TestCase):
    """A throwaway home and a throwaway repository."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.repo = base / "repo"
        self.home.mkdir()
        self.repo.mkdir()
        (self.repo / "genesis").write_text("#!/bin/sh\nexit 0\n")
        (self.repo / "src").mkdir()
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        self._tmp.cleanup()

    def ctx(self, endpoint) -> "bootstrap.Context":
        """A context wired to one gateway, the way the wizard wires it."""
        context = bootstrap.Context()
        context.repo = self.repo
        context.wizard = True
        context.gateway_endpoint = endpoint
        context.provider = endpoint.provider
        context.provider_env_var = endpoint.env_var
        context.backend = endpoint.provider
        context.gateway_shape = endpoint.shape
        context.gateway_url = endpoint.url
        context.claude_base = endpoint.claude_base
        context.model = endpoint.models[0] if endpoint.models else "m1"
        context.key_value = "sk-test-value-long-enough"
        context.key_fingerprint = keydetect.fingerprint(context.key_value)
        return context

    def plan(self, context) -> list:
        return bootstrap.build_plan(context, {"profile": "test"})

    def action(self, actions, action_id) -> dict:
        for item in actions:
            if item.get("id") == action_id:
                return item
        raise AssertionError("no action called " + action_id)


class TestTheClaudeCodeBaseUrl(ShapeCase):
    """F8: the base URL Claude Code is pointed at, per shape."""

    def test_the_anthropic_shape_points_at_the_gateway(self):
        context = self.ctx(ANTHROPIC_GATEWAY)
        settings = json.loads(bootstrap._claude_settings_content(context))
        self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"],
                         "https://api.anthropic.com")

    def test_the_openai_shape_points_at_the_shim(self):
        """The shim is what speaks Anthropic Messages. The provider cannot."""
        context = self.ctx(OPENAI_GATEWAY)
        settings = json.loads(bootstrap._claude_settings_content(context))
        self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"],
                         "http://127.0.0.1:8790")

    def test_the_openai_shape_does_not_point_at_the_provider(self):
        context = self.ctx(OPENAI_GATEWAY)
        settings = json.loads(bootstrap._claude_settings_content(context))
        base = settings["env"]["ANTHROPIC_BASE_URL"]
        self.assertNotIn("openai.com", base)
        self.assertTrue(base.startswith("http://127.0.0.1:"))

    def test_the_base_url_survives_a_custom_port(self):
        context = self.ctx(OPENAI_GATEWAY)
        context.shim_port = 9911
        settings = json.loads(bootstrap._claude_settings_content(context))
        self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"],
                         "http://127.0.0.1:9911")

    def test_a_second_run_sees_the_base_url_as_converged(self):
        """Otherwise the file this run wrote comes back as a change."""
        context = self.ctx(OPENAI_GATEWAY)
        settings = bootstrap.claude_settings_path()
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(bootstrap._claude_settings_content(context),
                            encoding="utf-8")
        self.assertTrue(bootstrap._claude_settings_current(context))


class TestTheShimUnit(ShapeCase):
    """F9: the unit carries the upstream on every platform."""

    def unit_for(self, endpoint, system: str) -> str:
        context = self.ctx(endpoint)
        with mock.patch("platform.system", return_value=system):
            return bootstrap._shim_unit_content(context)

    def test_the_macos_plist_carries_the_upstream(self):
        content = self.unit_for(OPENAI_GATEWAY, "Darwin")
        self.assertIn("GENESIS_SHIM_UPSTREAM", content)
        self.assertIn("https://api.openai.com/v1", content)

    def test_the_macos_plist_is_well_formed_xml(self):
        content = self.unit_for(OPENAI_GATEWAY, "Darwin")
        ET.fromstring(content)          # raises if the plist is broken

    def test_the_systemd_unit_carries_the_upstream(self):
        content = self.unit_for(OPENAI_GATEWAY, "Linux")
        self.assertIn("GENESIS_SHIM_UPSTREAM", content)
        self.assertIn("https://api.openai.com/v1", content)

    def test_both_platforms_name_the_same_variable(self):
        """One name, read from the shim — not spelled twice here."""
        darwin = self.unit_for(OPENAI_GATEWAY, "Darwin")
        linux = self.unit_for(OPENAI_GATEWAY, "Linux")
        self.assertIn(bootstrap.SHIM_UPSTREAM_ENV, darwin)
        self.assertIn(bootstrap.SHIM_UPSTREAM_ENV, linux)

    def test_the_plist_runs_a_launcher_that_carries_the_upstream(self):
        """The plist's own launcher, not the unit's Environment block.

        The launchd plist has no Environment block: the upstream has to be
        inside the command it runs, or the shim starts with no upstream and
        exits 2 — under KeepAlive, forever.
        """
        content = self.unit_for(OPENAI_GATEWAY, "Darwin")
        root = ET.fromstring(content)
        launcher = ""
        for element in root.iter("string"):
            if "GENESIS_SHIM" in (element.text or ""):
                launcher = element.text or ""
        self.assertTrue(launcher, "the plist carries no launcher")
        self.assertIn(bootstrap.SHIM_UPSTREAM_ENV, launcher)
        self.assertIn("https://api.openai.com/v1", launcher)

    def test_the_anthropic_shape_installs_no_shim(self):
        context = self.ctx(ANTHROPIC_GATEWAY)
        actions = self.plan(context)
        ids = [a.get("id") for a in actions]
        self.assertNotIn("shim-service", ids)
        self.assertNotIn("shim-enable", ids)

    def test_the_openai_shape_installs_the_shim(self):
        context = self.ctx(OPENAI_GATEWAY)
        actions = self.plan(context)
        ids = [a.get("id") for a in actions]
        self.assertIn("shim-service", ids)
        self.assertIn("shim-enable", ids)


class TestCodexifyDetection(ShapeCase):
    """F10: `--codexify` defaults to DETECTED, and a miss is reported."""

    def test_an_empty_submodule_directory_is_not_an_installation(self):
        """A fresh clone leaves `codexify-core/` empty. That is a hole."""
        (self.repo / "codexify-core").mkdir()
        context = self.ctx(ANTHROPIC_GATEWAY)
        self.assertIsNone(bootstrap._detect_codexify(context))

    def test_a_populated_codexify_directory_is_detected(self):
        codexify = self.repo / "codexify-core"
        codexify.mkdir()
        (codexify / ".env.template").write_text("OPENAI_API_KEY=\n")
        context = self.ctx(ANTHROPIC_GATEWAY)
        self.assertEqual(bootstrap._detect_codexify(context), codexify)

    def test_a_directory_without_a_marker_is_not_an_installation(self):
        codexify = self.repo / "codexify-core"
        codexify.mkdir()
        (codexify / "README.md").write_text("not an installation\n")
        context = self.ctx(ANTHROPIC_GATEWAY)
        self.assertIsNone(bootstrap._detect_codexify(context))

    def test_codexify_that_is_not_there_is_a_manual_step(self):
        """Not a silence. Codexify is one of the three consumers."""
        context = self.ctx(ANTHROPIC_GATEWAY)
        context.codexify_dir = None
        actions = self.plan(context)
        action = self.action(actions, "codexify-env")
        self.assertEqual(action.get("effect"), "manual")
        self.assertIn("--codexify", action.get("note", ""))

    def test_the_readiness_table_reports_codexify_when_it_is_missing(self):
        context = self.ctx(ANTHROPIC_GATEWAY)
        context.codexify_dir = None
        rows = dict((label, (mark, detail)) for label, mark, detail in
                    bootstrap.readiness(context))
        self.assertIn("Codexify", rows)
        mark, detail = rows["Codexify"]
        self.assertIsNone(mark)
        self.assertIn("--codexify", detail)

    def test_no_codexify_suppresses_the_row_entirely(self):
        context = self.ctx(ANTHROPIC_GATEWAY)
        context.codexify_dir = None
        context.no_codexify = True
        labels = [label for label, _, _ in bootstrap.readiness(context)]
        self.assertNotIn("Codexify", labels)

    def test_a_found_codexify_is_written(self):
        codexify = self.repo / "codexify-core"
        codexify.mkdir()
        (codexify / ".env.template").write_text("OPENAI_API_KEY=\n")
        context = self.ctx(ANTHROPIC_GATEWAY)
        context.codexify_dir = codexify
        actions = self.plan(context)
        action = self.action(actions, "codexify-env")
        self.assertEqual(action.get("effect"), "create")
        self.assertEqual(action.get("env_var"), "OPENAI_API_KEY")


class TestTheReadinessRowsCoverBothShapes(ShapeCase):
    """A consumer that is configured is a consumer that is reported."""

    def test_claude_code_is_reported_on_the_openai_shape(self):
        """The guard was the gateway's base, which the OpenAI shape has not.

        So on the OpenAI-shaped path — the one where the configuration is
        hardest — the whole Claude Code consumer vanished from the table,
        and the table is what a reader trusts.
        """
        context = self.ctx(OPENAI_GATEWAY)
        helper = bootstrap.helper_path()
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text(
            "#!/bin/sh\nprintf '%s' '" + context.key_value + "'\n",
            encoding="utf-8")
        labels = [label for label, _, _ in bootstrap.readiness(context)]
        self.assertIn("Claude Code", labels)
        self.assertIn("Claude Code url", labels)

    def test_claude_code_is_reported_on_the_anthropic_shape(self):
        context = self.ctx(ANTHROPIC_GATEWAY)
        helper = bootstrap.helper_path()
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text(
            "#!/bin/sh\nprintf '%s' '" + context.key_value + "'\n",
            encoding="utf-8")
        labels = [label for label, _, _ in bootstrap.readiness(context)]
        self.assertIn("Claude Code", labels)


class TestTheConvergenceCount(ShapeCase):
    """A manual step does not keep the plan open.

    Codexify's manual step is always in the plan on a machine that has no
    Codexify, so if a manual step counted as work, "already converged,
    nothing to do" would never be said again — and the plan would look
    permanently open on a machine that has converged.
    """

    def test_a_manual_step_is_not_actionable_work(self):
        actions = [{"id": "codexify-env", "kind": "probe",
                    "effect": "manual"}]
        self.assertEqual(bootstrap._plan_counts(actions), (0, 0, 1))

    def test_an_ordinary_action_is_counted(self):
        actions = [{"id": "genesis-env", "kind": "file",
                    "effect": "create"},
                   {"id": "codexify-env", "kind": "probe",
                    "effect": "manual"}]
        self.assertEqual(bootstrap._plan_counts(actions), (1, 1, 1))

    def test_a_converged_plan_is_empty(self):
        actions = [{"id": "genesis-env", "kind": "file", "effect": "none"}]
        self.assertEqual(bootstrap._plan_counts(actions), (0, 0, 0))

    def test_the_summary_says_already_converged_with_a_manual_step(self):
        """The wording is a function, so it can be checked without a gateway.

        This is the line the clean-clone portability run caught a crash in:
        the summary counted a manual step as a list and then took its
        length. A unit test that only ran the ladder would never have seen
        it, because only the wizard's plan has a manual step.
        """
        actions = [{"id": "codexify-env", "kind": "probe", "effect": "manual"}]
        summary = "\n".join(bootstrap._plan_summary(actions))
        self.assertIn("already converged", summary)
        self.assertIn("1 manual step", summary)

    def test_the_summary_names_the_work_when_there_is_any(self):
        actions = [{"id": "genesis-env", "kind": "file", "effect": "create"}]
        summary = "\n".join(bootstrap._plan_summary(actions))
        self.assertIn("1 actions, 1 files", summary)
        self.assertNotIn("already converged", summary)


if __name__ == "__main__":
    unittest.main()
