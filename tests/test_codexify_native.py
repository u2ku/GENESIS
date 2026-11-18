"""GENESIS — the native Codexify tests.

The native path is the same system without Docker, and it has its own traps.
Docker is not just a different way to run things; it is what hides three
things that a native run has to say out loud:

- **the addresses.** Inside a container the local runtime is at
  `host.docker.internal`. On the host it is at `127.0.0.1`, and one `.env`
  cannot serve both.
- **the Redis name.** The default `REDIS_URL` is `redis://redis:6379/0` —
  the compose *service* name. Natively that name does not resolve.
- **the config source.** `CODEXIFY_CONFIG_SOURCE` defaults to `strict`, and
  strict compares the core settings against the legacy ones, whose defaults
  disagree. Strict turns that disagreement into a startup abort.

And there is the one that is not a Docker problem at all: the lifespan
constructs the vector store, which constructs an embedder, and the default
embedder wants `sentence-transformers` — which wants torch. The native path
starts with the mock backend and says so.

Every test here is hermetic: no Docker, no network, no database.

    python3 -m unittest discover -s tests -v
    python3 tests/test_codexify_native.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import codexify  # noqa: E402
import codexify_native as native  # noqa: E402

TEMPLATE = """\
GUARDIAN_API_KEY=dev-local-only-change-me
VITE_GUARDIAN_API_KEY=
POSTGRES_USER=codexify
POSTGRES_PASSWORD=codexify
POSTGRES_DB=Codexify
DATABASE_URL=postgresql://codexify:codexify@db:5432/Codexify
LLM_PROVIDER=local
ALLOW_CLOUD_PROVIDERS=false
CODEXIFY_LOCAL_ONLY_MODE=true
LOCAL_BASE_URL=http://localhost:8000/v1
LOCAL_LLM_MODEL=library2/ministral-3:8b
LOCAL_EMBED_MODEL=/models/bge-large-en-v1.5
OPENAI_API_KEY=
ENABLE_OUTBOX=true
"""

# A pinned requirements.txt, with the heavy packages in it, the way the real
# one has them.
REQUIREMENTS = """\
fastapi==0.116.1
uvicorn==0.35.0
sqlalchemy==2.0.43
alembic==1.16.4
psycopg-binary==3.2.9
redis==6.4.0
numpy==2.3.4
httpx==0.28.1
bcrypt==4.3.0
cryptography==46.0.3
torch==2.8.0
torchvision==0.23.0
sentence-transformers==5.1.1
transformers==4.57.1
chromadb==1.0.15
faiss-cpu==1.11.0.post1
mlx-vlm==0.2.0
langchain==0.3.27
scikit-learn==1.7.2
pandas==2.3.3
audioop-lts==0.2.1
"""


class NativeCase(unittest.TestCase):
    """A throwaway installation, with no Docker anywhere near it."""

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
        (self.install / "docker-compose.yml").write_text("services: {}\n",
                                                         encoding="utf-8")
        (self.install / "requirements.txt").write_text(REQUIREMENTS,
                                                       encoding="utf-8")
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        self._tmp.cleanup()

    @staticmethod
    def getter(url, timeout=3.0):
        """A fake network. Nothing answers except the model list."""
        if url.endswith("/v1/models"):
            return 200, json.dumps({"data": [{"id": "fake-model"}]})
        return 0, ""


class TestTheRequirements(NativeCase):

    def test_the_heavy_packages_are_left_out(self):
        """They are the reason the full install is gigabytes."""
        names = [line.split("==")[0].strip().lower().replace("_", "-")
                 for line in native.native_requirements(self.install)]
        for heavy in ("torch", "torchvision", "sentence-transformers",
                      "transformers", "chromadb", "mlx-vlm", "langchain",
                      "scikit-learn", "pandas"):
            self.assertNotIn(heavy, names)

    def test_the_light_packages_are_kept(self):
        names = [line.split("==")[0].strip().lower().replace("_", "-")
                 for line in native.native_requirements(self.install)]
        for light in ("fastapi", "uvicorn", "sqlalchemy", "alembic",
                      "psycopg-binary", "redis", "numpy", "httpx", "bcrypt",
                      "cryptography", "faiss-cpu"):
            self.assertIn(light, names)

    def test_the_gaps_base_in_leaves_are_added(self):
        """The app imports httpx, numpy, bcrypt and cryptography anyway."""
        names = [line.split("==")[0].strip().lower().replace("_", "-")
                 for line in native.native_requirements(self.install)]
        for gap in ("httpx", "numpy", "bcrypt", "cryptography"):
            self.assertIn(gap, names)

    def test_the_py313_backports_are_left_out_on_older_pythons(self):
        """audioop-lts's wheels demand >=3.13, and 3.11 has it in the stdlib."""
        names = [line.split("==")[0].strip().lower().replace("_", "-")
                 for line in native.native_requirements(self.install,
                                                        (3, 11))]
        self.assertNotIn("audioop-lts", names)

    def test_the_py313_backports_survive_on_313(self):
        names = [line.split("==")[0].strip().lower().replace("_", "-")
                 for line in native.native_requirements(self.install,
                                                        (3, 13))]
        self.assertIn("audioop-lts", names)


class TestTheConfiguration(NativeCase):

    def overrides(self, existing=None, preset="ollama", model="llama3.2",
                  theme=None):
        return native.native_env_overrides(
            existing or {}, preset, model, False,
            "postgresql://codexify:codexify@127.0.0.1:5432/Codexify",
            "redis://127.0.0.1:6379/0", None, theme=theme)

    def test_the_runtime_url_is_the_host_one(self):
        """Inside a container it is host.docker.internal. On the host it is not."""
        written = self.overrides()
        self.assertEqual(written["LOCAL_BASE_URL"], "http://127.0.0.1:11434/v1")
        self.assertNotIn("host.docker.internal", written["LOCAL_BASE_URL"])

    def test_the_theme_default_is_written_when_asked_for(self):
        """--theme dark ships dark as this route's default."""
        written = self.overrides(theme="dark")
        self.assertEqual(written["VITE_THEME_DEFAULT"], "dark")

    def test_the_theme_defaults_to_system_when_unsaid(self):
        written = self.overrides()
        self.assertEqual(written["VITE_THEME_DEFAULT"], "system")

    def test_redis_is_named_out_loud(self):
        """The default is the compose service name, which does not resolve here."""
        written = self.overrides()
        self.assertEqual(written["REDIS_URL"], "redis://127.0.0.1:6379/0")
        self.assertNotIn("@redis", written["REDIS_URL"])

    def test_the_config_source_is_core(self):
        """Strict compares the core settings to the legacy ones, and their
        defaults disagree — which is a startup abort."""
        written = self.overrides()
        self.assertEqual(written["CODEXIFY_CONFIG_SOURCE"], "core")

    def test_the_embeddings_are_the_mock_ones(self):
        """The default embedder wants sentence-transformers, which wants torch."""
        written = self.overrides()
        self.assertEqual(written["CODEXIFY_EMBEDDINGS_BACKEND"], "mock")
        self.assertEqual(written["CODEXIFY_VECTOR_STORE"], "faiss")

    def test_the_graph_is_off(self):
        """Neo4j is optional, and the driver is not even imported."""
        written = self.overrides()
        self.assertEqual(written["CODEXIFY_ENABLE_GRAPH_WRITES"], "false")
        self.assertEqual(written["CODEXIFY_GRAPH_BACKEND"], "noop")

    def test_the_secrets_are_generated_and_never_typed(self):
        written = self.overrides()
        self.assertEqual(len(written["GUARDIAN_API_KEY"]), 64)
        self.assertEqual(written["VITE_GUARDIAN_API_KEY"],
                         written["GUARDIAN_API_KEY"])
        self.assertTrue(written["GUARDIAN_SESSION_SECRET"])
        self.assertFalse(codexify.is_placeholder(written["GUARDIAN_SESSION_SECRET"]))

    def test_the_database_url_is_the_host_one(self):
        existing = {"POSTGRES_USER": "codexify", "POSTGRES_PASSWORD": "codexify",
                    "POSTGRES_DB": "Codexify"}
        self.assertEqual(native.default_db_url(existing),
                         "postgresql://codexify:codexify@127.0.0.1:5432/Codexify")

    def test_the_redis_url_is_the_host_one(self):
        self.assertEqual(native.default_redis_url({}),
                         "redis://127.0.0.1:6379/0")


class TestTheDetection(NativeCase):

    def test_the_hint_names_only_what_is_missing(self):
        """A machine with python but no stores should not be told to install
        python again."""
        hint = native.brew_hint(["postgres"])
        self.assertIn("brew install postgresql@15", hint)
        self.assertNotIn("redis", hint)
        self.assertNotIn("python@3.11", hint)
        # And it says how to start it, not just how to get it.
        self.assertIn("brew services start postgresql@15", hint)

        hint = native.brew_hint(["postgres", "redis"])
        self.assertIn("brew install postgresql@15 redis", hint)
        self.assertIn("brew services start redis", hint)

        hint = native.brew_hint(["python", "redis"])
        self.assertIn("brew install python@3.11 redis", hint)
        self.assertNotIn("postgresql", hint)

        self.assertEqual(native.brew_hint([]), "")

    def test_a_missing_store_is_a_prerequisite_and_nothing_is_written(self):
        """This machine has no Postgres. That is a fact, and it is reported."""
        def runner(args, cwd=None, timeout=120.0, env=None):
            import subprocess
            return subprocess.CompletedProcess(args, 0, "ok", "")

        # The stores are probed with a REAL socket — `tcp_open` — and not
        # with the runner, so on a machine where Postgres and Redis happen
        # to be running this test would read "answering" and never reach
        # the branch it is about. That is a green that measures the host,
        # not the code. The probe is answered directly instead: this
        # machine has no stores.
        original = native.tcp_open
        native.tcp_open = lambda host, port, timeout=1.5: False
        try:
            report = native.setup(self.install, runner=runner,
                                  http_getter=self.getter)
        finally:
            native.tcp_open = original
        self.assertEqual(report.exit_code, native.EXIT_PREREQUISITE)
        # The command is in the row that names the gap, not only in prose.
        stores = [row for row in report.rows if row.label in ("postgres", "redis")]
        self.assertTrue(stores)
        self.assertTrue(any("brew install" in row.detail for row in stores))
        self.assertIn("nothing was written", report.verdict)
        self.assertFalse((self.install / ".env").exists())

    def test_the_native_readiness_has_no_docker_rows(self):
        """Saying 'docker is missing' on a machine that never wanted docker
        is a false row."""
        def runner(args, cwd=None, timeout=120.0, env=None):
            import subprocess
            return subprocess.CompletedProcess(args, 0, "ok", "")

        codexify.write_env(self.install, self.overrides_for_test())
        rows = codexify.readiness(self.install, runner=runner,
                                   http_getter=self.getter, skip_docker=True)
        labels = [row.label for row in rows]
        self.assertNotIn("docker", labels)
        self.assertNotIn("docker compose", labels)
        self.assertIn("the backend", labels)

    def overrides_for_test(self):
        return native.native_env_overrides(
            {}, "ollama", "llama3.2", False,
            "postgresql://codexify:codexify@127.0.0.1:5432/Codexify",
            "redis://127.0.0.1:6379/0", None)


class TestTheServicePrompt(NativeCase):
    """The Arch offer: install the missing services, or print and step aside."""

    def test_a_piped_run_gets_the_commands_and_runs_nothing(self):
        """A wizard without a terminal is a wizard without answers: the
        commands are printed, nothing is executed, and the caller's
        prerequisite exit decides what happens next."""
        import subprocess
        ran = []
        real_run = subprocess.run
        subprocess.run = lambda *a, **k: (ran.append((a, k)),
                                          subprocess.CompletedProcess((), 0))[1]
        native.prompt_and_fix_services.no_prompts = True
        try:
            self.assertFalse(native.prompt_and_fix_services(["redis"]))
        finally:
            del native.prompt_and_fix_services.no_prompts
            subprocess.run = real_run
        self.assertFalse(ran)

    def test_a_yes_runs_what_was_proposed(self):
        """A yes in a terminal runs the proposed chain — that is the offer,
        and the chain is whatever the platform's hint said it was."""
        import subprocess
        ran = []
        real_run = subprocess.run
        subprocess.run = lambda hints, **k: (ran.append(hints),
                                             subprocess.CompletedProcess((), 0))[1]
        real_stdin = sys.stdin
        sys.stdin = _FakeTTY(["y\n"])
        try:
            self.assertTrue(native.prompt_and_fix_services(["redis"]))
        finally:
            sys.stdin = real_stdin
            subprocess.run = real_run
        self.assertEqual(ran, [native.get_service_hints(["redis"])])

    def test_a_decline_runs_nothing(self):
        """Anything but a yes is a no, and a no runs nothing."""
        import subprocess
        ran = []
        real_run = subprocess.run
        subprocess.run = lambda *a, **k: (ran.append((a, k)),
                                          subprocess.CompletedProcess((), 0))[1]
        real_stdin = sys.stdin
        sys.stdin = _FakeTTY(["\n"])
        try:
            self.assertFalse(native.prompt_and_fix_services(["redis"]))
        finally:
            sys.stdin = real_stdin
            subprocess.run = real_run
        self.assertFalse(ran)

    def test_the_arch_hint_scopes_the_postgres_commands_to_a_missing_postgres(self):
        """A machine that is missing only redis is not told to enable
        postgresql, and the initdb the postgres install needs is guarded —
        an initialized-but-stopped postgres keeps its cluster."""
        old = sys.platform
        sys.platform = "linux"
        try:
            hint = native.get_service_hints(["redis"])
        finally:
            sys.platform = old
        self.assertIn("sudo pacman -S --needed redis", hint)
        self.assertIn("sudo systemctl enable --now redis", hint)
        self.assertNotIn("postgresql", hint)

        sys.platform = "linux"
        try:
            hint = native.get_service_hints(["postgres"])
        finally:
            sys.platform = old
        self.assertIn("sudo pacman -S --needed postgresql", hint)
        self.assertIn("[ -d /var/lib/postgres/data ] ||", hint)
        self.assertLess(hint.index("pacman -S --needed postgresql"),
                        hint.index("initdb"))
        self.assertLess(hint.index("initdb"),
                        hint.index("systemctl enable --now postgresql"))


class _FakeTTY:
    """A stdin that says what it is told: a terminal, and lines of it."""

    def __init__(self, lines):
        self._lines = iter(lines)

    def isatty(self):
        return True

    def readline(self):
        return next(self._lines)

    @property
    def encoding(self):
        return "utf-8"


class TestTheRemoteRuntime(NativeCase):
    """The endpoint a machine was pointed at, and the wiring that follows."""

    KEYS = ("CODEXIFY_RUNTIME_ENDPOINT", "OPENAI_API_BASE", "CODEXIFY_MODEL")

    def setUp(self):
        super().setUp()
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def test_the_shell_wins_over_the_file(self):
        """An exported endpoint is the more recent choice, and it beats the
        one the installer wrote down."""
        os.environ["CODEXIFY_RUNTIME_ENDPOINT"] = "http://100.100.100.100:8000/v1"
        codexify.write_env(self.install,
                           {"CODEXIFY_RUNTIME_ENDPOINT":
                            "http://127.0.0.1:9000/v1"})
        base, model = native.runtime_endpoint_override(self.install)
        self.assertEqual(base, "http://100.100.100.100:8000/v1")
        self.assertIsNone(model)

    def test_the_file_answers_when_the_shell_is_silent(self):
        """The installer's Whoosh'd step writes the endpoint into the
        `.env`, and a wizard that probed only localhost would never find
        it. The `.env` is the record of how this machine was configured."""
        codexify.write_env(self.install,
                           {"CODEXIFY_RUNTIME_ENDPOINT":
                            "http://100.71.144.117:8000/v1",
                            "CODEXIFY_MODEL": "supergemma4-26b-uncensored"})
        base, model = native.runtime_endpoint_override(self.install)
        self.assertEqual(base, "http://100.71.144.117:8000/v1")
        self.assertEqual(model, "supergemma4-26b-uncensored")

    def test_a_machine_with_no_answer_gets_no_wiring(self):
        """No shell, no file: there is nothing to override with, and the
        preset probing decides as it always has."""
        base, model = native.runtime_endpoint_override(self.install)
        self.assertIsNone(base)
        self.assertIsNone(model)

    def test_the_wiring_points_every_address_at_the_broker(self):
        """`LOCAL_BASE_URL` is the address the backend's own clients dial,
        so it is the one that carries the tailnet address — not the
        localhost guess the preset made."""
        wiring = native.remote_runtime_overrides(
            "http://100.71.144.117:8000/v1", "supergemma4-26b-uncensored")
        self.assertEqual(wiring["LOCAL_BASE_URL"],
                         "http://100.71.144.117:8000/v1")
        self.assertEqual(wiring["LOCAL_DOCKER_FALLBACK_BASE_URL"],
                         "http://100.71.144.117:8000/v1")
        self.assertEqual(wiring["VAULTNODE_BASE_URL"],
                         "http://100.71.144.117:8000")
        self.assertEqual(wiring["CODEXIFY_RUNTIME_ENDPOINT"],
                         "http://100.71.144.117:8000/v1")
        self.assertEqual(wiring["CODEXIFY_MODEL"],
                         "supergemma4-26b-uncensored")

    def test_setup_wires_the_endpoint_the_file_names(self):
        """The wizard reads what the installer wrote: the plan points the
        stack at the Mac Studio's broker, not at the localhost fallback,
        and the answer is written down for the next run."""
        def runner(args, cwd=None, timeout=120.0, env=None):
            import subprocess
            return subprocess.CompletedProcess(args, 0, "ok", "")

        original = native.tcp_open
        native.tcp_open = lambda host, port, timeout=1.5: True
        codexify.write_env(self.install,
                           {"CODEXIFY_RUNTIME_ENDPOINT":
                            "http://100.71.144.117:8000/v1"})
        try:
            report = native.setup(self.install, runner=runner,
                                  http_getter=self.getter, dry_run=True)
        finally:
            native.tcp_open = original
        self.assertEqual(report.exit_code, native.EXIT_OK)
        diff = next(row.detail for row in report.rows
                    if row.label == "the diff")
        self.assertIn("+LOCAL_BASE_URL=http://100.71.144.117:8000/v1", diff)
        self.assertIn("+LLM_MODEL=supergemma4-26b-uncensored", diff)
        # The endpoint is already in the file — the plan keeps it, as an
        # unchanged line, which is the point: what was chosen persists.
        self.assertIn("CODEXIFY_RUNTIME_ENDPOINT="
                      "http://100.71.144.117:8000/v1", diff)
        # Nothing was written — a plan, and nothing else. The file keeps
        # the localhost address the template gave it, not the broker's.
        written = codexify.read_env(self.install / codexify.ENV_FILE)
        self.assertEqual(written["LOCAL_BASE_URL"],
                         "http://localhost:8000/v1")


class TestTheProcesses(NativeCase):

    def test_a_stopped_supervisor_is_not_running(self):
        self.assertIsNone(native.supervisor_running(self.install))

    def test_a_supervisor_that_is_not_there_is_reported_not_crashed(self):
        stopped, detail = native.stop(self.install)
        self.assertFalse(stopped)
        self.assertIn("not running", detail)

    def test_the_process_rows_are_empty_before_anything_runs(self):
        self.assertEqual(native.process_rows(self.install), [])

    def test_a_dead_process_is_reported_as_dead(self):
        """A pid file for something that is gone is a finding."""
        pid_dir = self.install / native.PID_DIR
        pid_dir.mkdir(parents=True, exist_ok=True)
        (pid_dir / "backend.pid").write_text("999999\n", encoding="utf-8")
        rows = native.process_rows(self.install)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].mark, native.BAD)
        self.assertIn("not running", rows[0].detail)

    def test_the_supervisor_knows_the_venv_is_required(self):
        """Starting without an environment is a refusal, not a crash."""
        import subprocess
        done = subprocess.run(
            [sys.executable, str(native.SUPERVISOR), "--install",
             str(self.install)],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 2)
        self.assertIn("genesis codexify setup --native", done.stderr)


if __name__ == "__main__":
    unittest.main()
