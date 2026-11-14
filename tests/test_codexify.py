"""GENESIS — the Codexify provisioning tests.

`genesis codexify` provisions the vendored Codexify install and then proves
it works. The rules it holds are the rules that make "it is set up" mean
something:

- **an empty `codexify-core/` is not an installation.** A plain clone leaves
  it empty, and a wizard that treats an empty directory as an installation
  writes a `.env` into a directory nobody will ever run.
- **the configuration keeps the operator's own keys.** Codexify's own wizard
  re-seeds from `.env.template` and drops everything the operator added; a
  wizard that loses a key and then reports success is the false success this
  repository keeps refusing.
- **the configuration carries what compose requires.** `LOCAL_CHAT_MODEL` is
  in neither env template, and compose injects it into seven services with
  no default.
- **the LLM URL is the one a container can reach.** Inside a container,
  `localhost` is the container.
- **the file is mode 0600.** It holds `GUARDIAN_API_KEY` and `NEO4J_PASS`.
- **the secrets are generated and never printed.**
- **a missing Docker is reported, not installed**, and nothing is written.
- **a write is not a readiness claim.** Only a row that made a request may
  say a thing works.

Every test runs against a throwaway home and a throwaway installation, with
docker stubbed onto PATH, so nothing here touches a daemon or the network.

    python3 -m unittest discover -s tests -v
    python3 tests/test_codexify.py
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import codexify  # noqa: E402
import hermetic  # noqa: E402

# A minimal .env.template, with the two omissions that matter reproduced:
# no LOCAL_CHAT_MODEL, and a LOCAL_BASE_URL that points at localhost.
TEMPLATE = """\
# Codexify configuration.
GUARDIAN_API_KEY=dev-local-only-change-me
VITE_GUARDIAN_API_KEY=
NEO4J_USER=neo4j
NEO4J_PASS=replace-with-neo4j-password
LLM_PROVIDER=local
ALLOW_CLOUD_PROVIDERS=false
CODEXIFY_LOCAL_ONLY_MODE=true
LOCAL_BASE_URL=http://localhost:8000/v1
LOCAL_LLM_MODEL=library2/ministral-3:8b
LLM_MODEL=library2/ministral-3:8b
LOCAL_EMBED_MODEL=/models/bge-large-en-v1.5
OPENAI_API_KEY=
DATABASE_URL=postgresql://codexify:codexify@db:5432/Codexify
ENABLE_OUTBOX=true
"""

COMPOSE = """\
name: codexify
services:
  db:
    image: postgres:15
"""


class CodexifyCase(unittest.TestCase):
    """A throwaway home, a throwaway repository, and a fake installation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.repo = base / "repo"
        self.home.mkdir()
        self.repo.mkdir()
        self.install = self.repo / "codexify-core"
        self.install.mkdir()
        (self.install / ".env.template").write_text(TEMPLATE, encoding="utf-8")
        (self.install / "docker-compose.yml").write_text(COMPOSE, encoding="utf-8")

        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self._old_path = hermetic.stub_docker_on_path(base / "stub-bin")
        self._old_cwd = os.getcwd()
        os.chdir(self.repo)

    def tearDown(self):
        os.chdir(self._old_cwd)
        hermetic.restore_path(self._old_path)
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        self._tmp.cleanup()

    def env(self) -> dict:
        """The installation's environment file, parsed."""
        return codexify.read_env(self.install / ".env")

    def found(self) -> list:
        """Every docker invocation the stub was given."""
        return hermetic.docker_calls(Path(self.repo).parent / "stub-bin")

    @staticmethod
    def getter(url, timeout=3.0):
        """A fake network.

        The real machine has a Whoosh'd gateway answering on 127.0.0.1:8000,
        so a test that reached the loopback would reach a live service — and
        would pass or fail depending on what the operator happens to be
        running. Every HTTP call in this file goes through a fake.
        """
        if url.endswith("/v1/models"):
            return 200, json.dumps({"data": [{"id": "fake-model"}]})
        if url.endswith("/ping"):
            return 200, "{}"
        if url.endswith("/healthz"):
            return 200, json.dumps({
                "projects_table_exists": True,
                "chat_threads_table_exists": True})
        if url.endswith("/health"):
            return 200, "{}"
        if url.endswith("/health/chat"):
            return 200, json.dumps({"worker": {"status": "fresh"}})
        if url.endswith("/health/llm"):
            return 200, json.dumps({"models_available": ["fake-model"]})
        if url.endswith("/health/vector"):
            return 200, "{}"
        if url.rstrip("/").endswith(":5173"):
            return 200, "<html></html>"
        return 0, ""

    @staticmethod
    def runner(args, cwd=None, timeout=120.0):
        """A fake subprocess: everything succeeds."""
        import subprocess
        return subprocess.CompletedProcess(args, 0, "ok", "")


class TestTheInstallation(CodexifyCase):

    def test_an_empty_submodule_directory_is_not_an_installation(self):
        """A plain clone leaves codexify-core/ empty. That is not an install."""
        empty = self.repo / "codexify-core"
        for path in list(empty.iterdir()):
            path.unlink()
        self.assertFalse(codexify.is_install(empty))
        self.assertIsNone(codexify.find_install(self.repo))

    def test_a_directory_without_a_compose_file_is_not_an_installation(self):
        (self.install / "docker-compose.yml").unlink()
        self.assertFalse(codexify.is_install(self.install))
        self.assertIsNone(codexify.find_install(self.repo))

    def test_the_vendored_one_is_found(self):
        found = codexify.find_install(self.repo)
        self.assertIsNotNone(found)
        self.assertEqual(found, self.install.resolve())

    def test_an_explicit_directory_wins(self):
        other = self.home / "elsewhere"
        other.mkdir()
        (other / ".env.template").write_text(TEMPLATE, encoding="utf-8")
        (other / "docker-compose.yml").write_text(COMPOSE, encoding="utf-8")
        found = codexify.find_install(self.repo, str(other))
        self.assertEqual(found, other.resolve())

    def test_the_note_says_what_is_missing(self):
        note = codexify.install_note(str(self.repo / "nowhere"))
        self.assertIn("does not exist", note)


class TestTheConfiguration(CodexifyCase):

    def test_the_env_keeps_the_operators_own_keys(self):
        """A key the operator added must survive the wizard's write."""
        env_path = self.install / ".env"
        env_path.write_text("OPENAI_API_KEY=sk-or-keepme\n"
                            "MY_OWN_SETTING=do-not-touch\n", encoding="utf-8")
        codexify.write_env(self.install, codexify.env_overrides(
            codexify.read_env(env_path), "whooshd-mlx", "some-model", False))
        written = self.env()
        self.assertEqual(written.get("OPENAI_API_KEY"), "sk-or-keepme")
        self.assertEqual(written.get("MY_OWN_SETTING"), "do-not-touch")

    def test_the_env_carries_the_key_compose_requires(self):
        """LOCAL_CHAT_MODEL is in no template, and compose has no default."""
        self.assertNotIn("LOCAL_CHAT_MODEL", TEMPLATE)
        codexify.write_env(self.install, codexify.env_overrides(
            {}, "whooshd-mlx", "mlx-community/some-model", False))
        written = self.env()
        self.assertEqual(written.get("LOCAL_CHAT_MODEL"), "mlx-community/some-model")
        self.assertEqual(written.get("LOCAL_LLM_MODEL"), "mlx-community/some-model")
        self.assertEqual(written.get("LLM_MODEL"), "mlx-community/some-model")

    def test_the_llm_url_is_the_one_a_container_can_reach(self):
        """Inside a container, localhost is the container itself.

        Codexify's canonical docker shape puts the docker address in
        LOCAL_BASE_URL — the stack runs in containers, and the host address
        is where the runtime is *probed*, not where the stack is pointed.
        """
        codexify.write_env(self.install, codexify.env_overrides(
            {}, "ollama", "llama3.2", False))
        written = self.env()
        self.assertEqual(written.get("LOCAL_BASE_URL"),
                         "http://host.docker.internal:11434/v1")
        self.assertEqual(written.get("LOCAL_DOCKER_FALLBACK_BASE_URL"),
                         "http://host.docker.internal:11434/v1")

    def test_the_canonical_preset_values_are_written(self):
        """worker-warmup reads VAULTNODE_HEALTH_ENDPOINTS to know where to probe."""
        codexify.write_env(self.install, codexify.env_overrides(
            {}, "whooshd-mlx", "some-model", False))
        written = self.env()
        self.assertEqual(written.get("VAULTNODE_BASE_URL"),
                         "http://host.docker.internal:8000")
        self.assertEqual(written.get("VAULTNODE_HEALTH_ENDPOINTS"),
                         "/health,/health/runtime,/ready,/v1/models,/api/tags")
        self.assertEqual(written.get("LOCAL_COMPAT_FIRST"), "1")
        self.assertEqual(written.get("LOCAL_RUNTIME_PRESET"), "whooshd-mlx")
        self.assertEqual(written.get("LOCAL_PROVIDER_DISPLAY_NAME"), "Whoosh'd")
        self.assertEqual(written.get("LOCAL_PROVIDER_VENDOR"), "whooshd")

    def test_the_session_secret_is_not_left_as_a_placeholder(self):
        """The template ships `replace-with-long-random-secret`."""
        codexify.write_env(self.install, codexify.env_overrides(
            {}, "ollama", "m", False))
        value = self.env().get("GUARDIAN_SESSION_SECRET", "")
        self.assertTrue(value)
        self.assertFalse(codexify.is_placeholder(value))

    def test_the_env_is_written_at_0600(self):
        """It holds GUARDIAN_API_KEY and NEO4J_PASS. It is not world-readable."""
        codexify.write_env(self.install, codexify.env_overrides(
            {}, "whooshd-mlx", "m", False))
        mode = (self.install / ".env").stat().st_mode
        self.assertEqual(stat.S_IMODE(mode), 0o600)

    def test_the_secrets_are_generated_and_never_printed(self):
        overrides = codexify.env_overrides({}, "whooshd-mlx", "m", False)
        self.assertEqual(len(overrides["GUARDIAN_API_KEY"]), 64)
        self.assertTrue(overrides["NEO4J_PASS"])
        self.assertEqual(overrides["VITE_GUARDIAN_API_KEY"],
                         overrides["GUARDIAN_API_KEY"])
        # The secrets are not printed, and a line carrying one is masked.
        printed = codexify.redact(
            "GUARDIAN_API_KEY=" + overrides["GUARDIAN_API_KEY"])
        self.assertNotIn(overrides["GUARDIAN_API_KEY"], printed)
        self.assertIn("***", printed)

    def test_neo4j_pass_is_masked_though_its_name_does_not_end_in_password(self):
        """keydetect masks API_KEY and PASSWORD; NEO4J_PASS ends in neither."""
        printed = codexify.redact("NEO4J_PASS=super-secret-value")
        self.assertNotIn("super-secret-value", printed)
        self.assertIn("***", printed)

    def test_a_running_key_is_not_rotated(self):
        """A second run must not change the machine under a running stack."""
        existing = {"GUARDIAN_API_KEY": "a" * 64, "NEO4J_PASS": "keep-me"}
        overrides = codexify.env_overrides(existing, "whooshd-mlx", "m", False)
        self.assertEqual(overrides["GUARDIAN_API_KEY"], "a" * 64)
        self.assertEqual(overrides["NEO4J_PASS"], "keep-me")

    def test_the_existing_file_is_backed_up(self):
        env_path = self.install / ".env"
        env_path.write_text("OPENAI_API_KEY=sk-or-old\n", encoding="utf-8")
        codexify.write_env(self.install, codexify.env_overrides(
            codexify.read_env(env_path), "whooshd-mlx", "m", False),
            backup=True)
        backup = self.install / ".env.bak"
        self.assertTrue(backup.is_file())
        self.assertIn("sk-or-old", backup.read_text(encoding="utf-8"))
        # And the backup is not world-readable either.
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

    def test_a_second_write_changes_nothing(self):
        """A second run must say 'already converged', not 'wrote'."""
        overrides = codexify.env_overrides({}, "ollama", "llama3.2", False)
        codexify.write_env(self.install, overrides)
        _, changed = codexify.write_env(self.install, overrides)
        self.assertFalse(changed)

    def test_the_env_is_not_written_into_a_path_git_watches(self):
        """The file must be gitignored, and that is verified, not assumed."""
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=self.install,
                       capture_output=True)
        # Without a .gitignore, the file is not ignored — and the wizard
        # refuses rather than writing a secret into a tracked path.
        refusal = codexify.verify_writable(self.install, self.install / ".env")
        self.assertIsNotNone(refusal)
        self.assertIn("gitignored", refusal)
        (self.install / ".gitignore").write_text(".env\n.env.*\n",
                                                  encoding="utf-8")
        self.assertIsNone(codexify.verify_writable(self.install,
                                                    self.install / ".env"))

    def test_the_cloud_posture_is_opt_in_and_the_two_flags_agree(self):
        local = codexify.env_overrides({}, "ollama", "m", False)
        self.assertEqual(local["ALLOW_CLOUD_PROVIDERS"], "false")
        self.assertEqual(local["CODEXIFY_LOCAL_ONLY_MODE"], "true")
        cloud = codexify.env_overrides({}, "ollama", "m", True)
        self.assertEqual(cloud["ALLOW_CLOUD_PROVIDERS"], "true")
        self.assertEqual(cloud["CODEXIFY_LOCAL_ONLY_MODE"], "false")


class TestTheMode(CodexifyCase):

    def test_native_is_the_default(self):
        """A fresh install is native: no container runtime, no daemon, no
        licence, and it is the only mode that works without Docker at all."""
        self.assertEqual(codexify.resolve_mode(self.install),
                         codexify.MODE_NATIVE)

    def test_a_compose_install_stays_compose(self):
        """The mode is a property of the installation, not of the run.

        A compose install has the container address in its `.env` and the
        containers behind it. Rewriting that to a host address under a
        running stack breaks the thing it was serving — and a wizard that
        flips modes between runs is not idempotent.
        """
        (self.install / ".env").write_text(
            "LOCAL_BASE_URL=http://host.docker.internal:8000/v1\n",
            encoding="utf-8")
        self.assertTrue(codexify.is_compose_install(self.install))
        self.assertEqual(codexify.resolve_mode(self.install),
                         codexify.MODE_COMPOSE)

    def test_a_native_install_stays_native(self):
        (self.install / ".env").write_text(
            "LOCAL_BASE_URL=http://127.0.0.1:8000/v1\n", encoding="utf-8")
        self.assertFalse(codexify.is_compose_install(self.install))
        self.assertEqual(codexify.resolve_mode(self.install),
                         codexify.MODE_NATIVE)

    def test_a_flag_moves_it_deliberately(self):
        """Changing the mode is a decision, and it is made out loud."""
        (self.install / ".env").write_text(
            "LOCAL_BASE_URL=http://host.docker.internal:8000/v1\n",
            encoding="utf-8")
        self.assertEqual(codexify.resolve_mode(self.install, native=True),
                         codexify.MODE_NATIVE)
        self.assertEqual(codexify.resolve_mode(self.install, docker=True),
                         codexify.MODE_COMPOSE)

    def test_asking_for_both_is_a_usage_error(self):
        with self.assertRaises(ValueError):
            codexify.resolve_mode(self.install, docker=True, native=True)


class TestTheRuntime(CodexifyCase):

    def test_the_model_comes_from_the_endpoint_not_from_a_guess(self):
        """Ask the endpoint what it serves. Codexify's own default is dead code."""
        def getter(url, timeout=3.0):
            if url.endswith("/v1/models"):
                return 200, json.dumps(
                    {"data": [{"id": "mlx-community/real-model"}]})
            return 0, ""

        preset, model, base = codexify.detect_runtime(getter)
        self.assertEqual(preset, "whooshd-mlx")
        self.assertEqual(model, "mlx-community/real-model")
        self.assertEqual(base, "http://127.0.0.1:8000/v1")

    def test_the_health_endpoints_are_asked_at_the_root(self):
        """A gateway serves /health at the root. Asking for /v1/health 404s,
        and a 404 looks exactly like a runtime that is not running."""
        asked = []

        def getter(url, timeout=3.0):
            asked.append(url)
            if url == "http://127.0.0.1:8000/health":
                return 200, json.dumps({"status": "ok"})
            return 0, ""

        preset, model, base = codexify.detect_runtime(getter)
        self.assertEqual(preset, "whooshd-mlx")
        self.assertIn("http://127.0.0.1:8000/health", asked)
        self.assertNotIn("http://127.0.0.1:8000/v1/health", asked)
        # It answered but advertised no model, so the preset's own default
        # is what Codexify falls back to.
        self.assertEqual(model, codexify.PRESETS["whooshd-mlx"].default_model)

    def test_an_advertised_model_beats_the_presets_default(self):
        """/health answers happily and says nothing. /v1/models knows."""
        def getter(url, timeout=3.0):
            if url == "http://127.0.0.1:8000/health":
                return 200, json.dumps({"status": "ok"})
            if url == "http://127.0.0.1:8000/v1/models":
                return 200, json.dumps({"data": [{"id": "the-real-model"}]})
            return 0, ""

        preset, model, base = codexify.detect_runtime(getter)
        self.assertEqual(preset, "whooshd-mlx")
        self.assertEqual(model, "the-real-model")

    def test_nothing_answering_is_not_a_guess(self):
        preset, model, base = codexify.detect_runtime(
            lambda url, timeout=3.0: (0, ""))
        self.assertIsNone(preset)
        self.assertIsNone(model)

    def test_a_preset_alias_resolves(self):
        self.assertEqual(codexify.resolve_preset("ollama"), "ollama")
        self.assertEqual(codexify.resolve_preset("whooshd"), "whooshd-mlx")
        self.assertEqual(codexify.resolve_preset("lm-studio"), "lmstudio")
        self.assertIsNone(codexify.resolve_preset("nonesuch"))


class TestTheReport(CodexifyCase):

    def test_a_missing_docker_is_reported_and_nothing_is_written(self):
        """This machine has no Docker. That is a fact, and it is reported."""
        # Take the stub off PATH, so docker genuinely is not there. The
        # stub goes back before the test ends — PATH belongs to the whole
        # suite, and a test that breaks it breaks every test after it.
        saved = os.environ["PATH"]
        os.environ["PATH"] = "/nonexistent-for-this-test"
        try:
            report = codexify.setup(self.install,
                                    http_getter=self.getter)
        finally:
            os.environ["PATH"] = saved

        self.assertEqual(report.exit_code, codexify.EXIT_PREREQUISITE)
        self.assertTrue(any(row.mark == codexify.BAD and row.label == "docker"
                            for row in report.rows))
        self.assertFalse((self.install / ".env").is_file())

    def test_no_up_does_not_need_docker(self):
        """--no-up means "do not touch Docker". It must not refuse for Docker."""
        saved = os.environ["PATH"]
        os.environ["PATH"] = "/nonexistent-for-this-test"
        try:
            report = codexify.setup(self.install, up=False,
                                    preset="ollama", model="llama3.2",
                                    http_getter=self.getter)
        finally:
            os.environ["PATH"] = saved

        self.assertTrue((self.install / ".env").is_file())
        docker = [row for row in report.rows if row.label == "docker"][0]
        self.assertEqual(docker.mark, codexify.MANUAL)
        # It did what it was asked, and says plainly that the stack is not up.
        self.assertNotIn("ready", report.verdict)
        self.assertIn("not up", report.verdict)
        self.assertEqual(report.exit_code, codexify.EXIT_OK)

    def test_a_row_that_only_wrote_a_file_does_not_claim_readiness(self):
        """A write is a write. The services were not started."""
        report = codexify.setup(self.install, up=False,
                                preset="ollama", model="llama3.2",
                                http_getter=self.getter)
        self.assertTrue((self.install / ".env").is_file())
        written = [row for row in report.rows if row.label == "the configuration"]
        self.assertEqual(written[0].mark, codexify.OK)
        self.assertIn("wrote", written[0].detail)
        # And nothing claims the stack is up.
        services = [row for row in report.rows if row.label == "the services"]
        self.assertEqual(services[0].mark, codexify.MANUAL)
        self.assertNotIn("ready", report.verdict)

    def test_the_services_row_is_a_fact_not_a_claim(self):
        """'compose exited 0' is a fact. 'the stack is up' is a claim."""
        report = codexify.setup(self.install, up=True,
                                preset="ollama", model="llama3.2",
                                http_getter=self.getter)
        services = [row for row in report.rows if row.label == "the services"][0]
        self.assertIn("exited 0", services.detail)
        self.assertNotIn("brought the stack up", services.detail)
        self.assertNotIn("is up", services.detail)

    def test_a_dry_run_writes_nothing(self):
        report = codexify.setup(self.install, dry_run=True,
                                preset="ollama", model="llama3.2",
                                http_getter=self.getter)
        self.assertFalse((self.install / ".env").exists())
        self.assertEqual(report.exit_code, codexify.EXIT_OK)
        diff = [row for row in report.rows if row.label == "the diff"]
        self.assertTrue(diff)
        # The diff carries no secret.
        self.assertNotIn("GUARDIAN_API_KEY=***", diff[0].detail)
        self.assertIn("${GUARDIAN_API_KEY}", diff[0].detail)

    def test_the_provider_key_is_reported_by_fingerprint(self):
        """WIZARD.md 3.7 wants a row per consumer. Never the value."""
        (self.install / ".env").write_text(
            "OPENAI_API_KEY=sk-or-abcdefghijklmnop\n", encoding="utf-8")
        label, mark, detail = codexify.provider_key_row(self.env())
        self.assertEqual(label, "the provider key")
        self.assertEqual(mark, codexify.OK)
        self.assertNotIn("sk-or-", detail)
        self.assertIn("fingerprint", detail)

    def test_setup_waits_for_the_backend_rather_than_probing_once(self):
        """`compose up -d` returns when containers are created, not healthy."""
        answers = []

        def getter(url, timeout=3.0):
            if url.endswith("/ping"):
                answers.append(url)
                if len(answers) < 3:
                    return 503, ""
                return 200, "{}"
            return self.getter(url, timeout)

        report = codexify.setup(self.install, up=True,
                                preset="ollama", model="llama3.2",
                                http_getter=getter, interval=0.0,
                                timeout=30.0)
        self.assertGreaterEqual(len(answers), 3)
        backend = [row for row in report.rows if row.label == "the backend"][0]
        self.assertEqual(backend.mark, codexify.OK)

    def test_the_stack_is_brought_up_with_the_env_file_and_the_services(self):
        # The real runner, so the docker stub sees the command that was
        # built. A fake runner would prove the report and nothing else.
        report = codexify.setup(self.install, up=True,
                                preset="ollama", model="llama3.2",
                                http_getter=self.getter)
        calls = self.found()
        up = [call for call in calls if "up" in call]
        self.assertTrue(up, "compose was never asked to bring the stack up")
        self.assertEqual(up[0][:4], ["compose", "--env-file", ".env", "up"])
        self.assertIn("-d", up[0])
        self.assertIn("--build", up[0])
        for service in ("db", "neo4j", "redis", "migrator", "backend", "frontend"):
            self.assertIn(service, up[0])
        self.assertEqual(report.exit_code, codexify.EXIT_OK)

    def test_a_missing_installation_is_reported_not_crashed(self):
        report = codexify.status(None)
        self.assertEqual(report.exit_code, codexify.EXIT_MISSING_INSTALL)
        self.assertTrue(report.rows)

    def test_the_readiness_rows_are_the_ones_that_prove_the_stack(self):
        """Each row must be a request or a file read — never a claim."""
        codexify.write_env(self.install, codexify.env_overrides(
            {}, "ollama", "llama3.2", False))
        rows = codexify.readiness(self.install, runner=self.runner,
                                   http_getter=self.getter)
        labels = [row.label for row in rows]
        for expected in ("the configuration", "docker", "the compose file",
                         "the local runtime", "the backend", "the schema",
                         "the chat path", "the provider", "the vector store",
                         "the interface"):
            self.assertIn(expected, labels)
        self.assertTrue(all(row.mark != codexify.BAD for row in rows),
                        [row for row in rows if row.mark == codexify.BAD])

    def test_a_missing_table_is_a_failure_not_a_pass(self):
        """/healthz is what separates 'the container started' from 'it migrated'."""
        def getter(url, timeout=3.0):
            if url.endswith("/ping"):
                return 200, "{}"
            if url.endswith("/healthz"):
                return 200, json.dumps({
                    "projects_table_exists": False,
                    "chat_threads_table_exists": True})
            return self.getter(url, timeout)

        codexify.write_env(self.install, codexify.env_overrides(
            {}, "ollama", "llama3.2", False))
        rows = codexify.readiness(self.install, runner=self.runner,
                                   http_getter=getter)
        schema = [row for row in rows if row.label == "the schema"][0]
        self.assertEqual(schema.mark, codexify.BAD)
        self.assertIn("migrations", schema.detail)

    def test_a_crashed_interface_quotes_its_log(self):
        """'not answering' plus the last line of the log — the dyld error,
        the refused port — is a diagnosis, not just a failure."""
        log_dir = self.install / ".native" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "interface.log").write_text(
            "dyld[79949]: Library not loaded: libsimdjson.30.dylib\n",
            encoding="utf-8")

        def getter(url, timeout=3.0):
            if url.startswith("http://127.0.0.1:5173"):
                return 0, ""
            return self.getter(url, timeout)

        rows = [row for row in codexify.readiness(
            self.install, runner=self.runner, http_getter=getter)
            if row.label == "the interface"]
        self.assertEqual(rows[0].mark, codexify.BAD)
        self.assertIn("libsimdjson", rows[0].detail)

    def test_the_down_command_does_not_touch_the_volumes_unless_asked(self):
        codexify.down(self.install)
        calls = self.found()
        down = [call for call in calls if "down" in call]
        self.assertTrue(down)
        self.assertNotIn("-v", down[0])


if __name__ == "__main__":
    unittest.main()
