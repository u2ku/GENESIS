"""GENESIS — the supervision tests.

**What is this file about.** The wizard's job is to bring a stack up and
then tell the truth about it. Both halves can fail, and they fail in
ways that look like the stack is fine.

The truth is a port. A pid file is a claim: it can point at a process
that died, at a process that lost a race for the port, or at a process
belonging to an installation that was moved to the trash while its
supervisor kept the socket open. A wizard that trusts the claim reports
yesterday's stack as today's.

So the rules this file pins down are:

- **a zombie is not a running process.** `os.kill(pid, 0)` says yes to a
  process that has already died and is only waiting to be reaped. It
  holds nothing and it cannot be stopped.
- **the port decides who is serving.** A /ping that answers is not proof
  that *our* backend answered.
- **a booting server is not a broken server.** Vite resolves its whole
  dependency graph before it binds, and `--force` makes it do that from
  scratch every time. A single probe measures the boot.
- **a stop that leaves a port held has not stopped anything.**

Every test here is hermetic: no Docker, no network, no database, and no
process of the operator's is signalled.

    python3 -m unittest discover -s tests -v
    python3 tests/test_codexify_supervision.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import codexify  # noqa: E402
import codexify_native as native  # noqa: E402


def make_zombie():
    """A process that has died and not been reaped.

    The parent never waits for it, which is what makes it a zombie —
    the same state a child lands in when the supervisor that spawned it
    dies without reaping. A subprocess is used rather than a fork,
    because the suite runs in a threaded interpreter.
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    for _ in range(200):
        if codexify._is_zombie(proc.pid):
            return proc
        time.sleep(0.02)
    proc.wait()
    raise AssertionError("the child never became a zombie")


class SupervisionCase(unittest.TestCase):
    """A throwaway installation and a fake network."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.install = Path(self._tmp.name) / "codexify-core"
        self.install.mkdir()
        (self.install / ".env").write_text(
            "GUARDIAN_API_KEY=dev-local-only-change-me\n"
            "LOCAL_BASE_URL=http://127.0.0.1:8000/v1\n"
            "LOCAL_CHAT_MODEL=library2/ministral-3:8b\n"
            "LOCAL_EMBED_MODEL=/models/bge-large-en-v1-1\n",
            encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def getter(url, timeout=3.0):
        """A fake network. Only the model list answers."""
        if url.endswith("/v1/models"):
            return 200, json.dumps({"data": [{"id": "fake-model"}]})
        return 0, ""

    def _make_venv(self):
        """An environment, so a start gets as far as the port check."""
        python = self.install / "venv" / "bin" / "python"
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("#!/bin/sh\n", encoding="utf-8")
        python.chmod(0o755)


class TestAZombieIsNotAlive(SupervisionCase):

    def test_a_zombie_is_not_alive(self):
        """`os.kill(pid, 0)` says yes to a corpse."""
        proc = make_zombie()
        try:
            # The kernel still has the pid, so a signal *could* be sent.
            os.kill(proc.pid, 0)
            self.assertFalse(codexify.pid_alive(proc.pid))
        finally:
            proc.wait()                  # reap it, so the suite leaves none

    def test_a_live_process_is_alive(self):
        self.assertTrue(codexify.pid_alive(os.getpid()))

    def test_a_pid_that_is_not_there_is_not_alive(self):
        self.assertFalse(codexify.pid_alive(999999))


class TestTheSupervisorPidFile(SupervisionCase):

    def _write_pid(self, pid):
        path = self.install / native.SUPERVISOR_PID
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{pid}\n", encoding="utf-8")

    def test_a_zombie_supervisor_is_not_running(self):
        """A dead supervisor must not refuse the next start."""
        proc = make_zombie()
        try:
            self._write_pid(proc.pid)
            self.assertIsNone(native.supervisor_running(self.install))
        finally:
            proc.wait()

    def test_a_live_supervisor_is_running(self):
        self._write_pid(os.getpid())
        self.assertEqual(native.supervisor_running(self.install),
                         os.getpid())

    def test_a_dead_process_is_reported_as_dead(self):
        """A zombie in a pid file is a finding, not a green tick."""
        proc = make_zombie()
        try:
            pid_dir = self.install / native.PID_DIR
            pid_dir.mkdir(parents=True, exist_ok=True)
            (pid_dir / "backend.pid").write_text(f"{proc.pid}\n",
                                                 encoding="utf-8")
            rows = native.process_rows(self.install)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].mark, native.BAD)
            self.assertIn("not running", rows[0].detail)
        finally:
            proc.wait()


class TestThePortDecides(SupervisionCase):

    def test_readiness_names_the_process_that_holds_the_port(self):
        """A /ping that answers is not proof it was our backend."""
        def getter(url, timeout=3.0):
            if url.endswith("/v1/models"):
                return 200, json.dumps({"data": [{"id": "fake-model"}]})
            if url.endswith("/ping"):
                return 200, "{}"
            return 200, "{}"

        with mock.patch.object(codexify, "port_holder",
                               return_value=54498), \
             mock.patch.object(codexify, "process_cwd",
                               return_value="/Users/someone/.Trash/codexify-core"):
            rows = codexify.readiness(self.install, http_getter=getter,
                                      skip_docker=True,
                                      backend_pid=87114)
        backend = [row for row in rows if row.label == "the backend"]
        self.assertEqual(len(backend), 1)
        self.assertEqual(backend[0].mark, codexify.BAD)
        self.assertIn("54498", backend[0].detail)
        self.assertIn(".Trash", backend[0].detail)
        # And it stops there: the health checks below would have been the
        # other process's answers, wearing this install's name.
        self.assertNotIn("the provider", [row.label for row in rows])

    def test_readiness_is_happy_when_our_own_backend_holds_the_port(self):
        def getter(url, timeout=3.0):
            if url.endswith("/v1/models"):
                return 200, json.dumps({"data": [{"id": "fake-model"}]})
            return 200, "{}"

        with mock.patch.object(codexify, "port_holder", return_value=87114):
            rows = codexify.readiness(self.install, http_getter=getter,
                                      skip_docker=True,
                                      backend_pid=87114)
        backend = [row for row in rows if row.label == "the backend"]
        self.assertEqual(backend[0].mark, codexify.OK)

    def test_a_start_refuses_when_a_stranger_holds_the_port(self):
        """The race is lost before it starts, so it is refused first."""
        self._make_venv()
        elsewhere = str(Path(self._tmp.name))
        with mock.patch.object(codexify, "port_holder", return_value=54498), \
             mock.patch.object(codexify, "process_cwd",
                               return_value=elsewhere), \
             mock.patch.object(native, "_settle_ports"):
            started, detail = native.start(self.install)
        self.assertFalse(started)
        self.assertIn("54498", detail)
        self.assertIn(elsewhere, detail)
        self.assertIn("held by pid", detail)

    def test_a_start_is_not_refused_by_our_own_listener(self):
        """The check must not stop the stack it belongs to."""
        self._make_venv()
        pid_dir = self.install / native.PID_DIR
        pid_dir.mkdir(parents=True, exist_ok=True)
        (pid_dir / "backend.pid").write_text(f"{os.getpid()}\n",
                                             encoding="utf-8")
        with mock.patch.object(codexify, "port_holder",
                               return_value=os.getpid()), \
             mock.patch.object(codexify, "process_cwd",
                               return_value=str(self.install)), \
             mock.patch.object(native, "_stop_child_groups"), \
             mock.patch.object(native, "_settle_ports"), \
             mock.patch.object(native.subprocess, "Popen"):
            started, detail = native.start(self.install)
        # Our own leftover that would not let go is named as ours.
        self.assertIn("this install's own leftover", detail)

    def test_a_leftover_of_ours_is_cleared_by_the_stop(self):
        """A dead supervisor leaves children holding the ports."""
        pid_dir = self.install / native.PID_DIR
        pid_dir.mkdir(parents=True, exist_ok=True)
        (pid_dir / "backend.pid").write_text(f"{os.getpid()}\n",
                                             encoding="utf-8")
        cleared = []
        with mock.patch.object(native, "supervisor_running",
                               return_value=None), \
             mock.patch.object(native, "_stop_child_groups",
                               side_effect=lambda inst, pids: cleared.append(pids)), \
             mock.patch.object(codexify, "port_holder", return_value=None):
            stopped, detail = native.stop(self.install)
        self.assertTrue(stopped)
        self.assertEqual(cleared, [[os.getpid()]])

    def test_a_stop_with_nothing_to_stop_says_so(self):
        with mock.patch.object(native, "supervisor_running",
                               return_value=None):
            stopped, detail = native.stop(self.install)
        self.assertFalse(stopped)
        self.assertIn("not running", detail)

    def test_a_leftover_of_another_install_is_never_stopped(self):
        """A pid file is a claim; the working directory is the proof."""
        with mock.patch.object(codexify, "process_cwd",
                               return_value="/Users/someone/.Trash/codexify-core"):
            self.assertFalse(native._is_ours(self.install, os.getpid()))
        with mock.patch.object(codexify, "process_cwd",
                               return_value=str(self.install)):
            self.assertTrue(native._is_ours(self.install, os.getpid()))


class TestTheInterfaceIsGivenTime(SupervisionCase):

    def test_the_wait_gives_a_booting_server_its_chance(self):
        """One probe measures the boot, not the server."""
        attempts = []

        def getter(url, timeout=3.0):
            attempts.append(url)
            if url.endswith("/v1/models"):
                return 200, json.dumps({"data": [{"id": "fake-model"}]})
            if len(attempts) < 4:
                return 0, ""
            return 200, "<html></html>"

        status, _ = codexify.wait_for_url(codexify.HEALTH_FRONTEND, getter, 5.0)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(attempts), 4)

    def test_the_wait_gives_up_when_the_server_never_comes_up(self):
        def getter(url, timeout=3.0):
            return 0, ""

        status, _ = codexify.wait_for_url(codexify.HEALTH_FRONTEND, getter, 0.3)
        self.assertEqual(status, 0)

    def test_readiness_waits_for_the_interface(self):
        """The interface row is a verdict, not a snapshot of the boot."""
        seen = []

        def getter(url, timeout=3.0):
            seen.append(url)
            if url.endswith("/v1/models"):
                return 200, json.dumps({"data": [{"id": "fake-model"}]})
            if url.endswith(":5173/") and len(seen) < 3:
                return 0, ""
            return 200, "{}"

        with mock.patch.object(codexify, "port_holder",
                               return_value=os.getpid()):
            rows = codexify.readiness(self.install, http_getter=getter,
                                      skip_docker=True,
                                      backend_pid=os.getpid(),
                                      frontend_wait=3.0)
        interface = [row for row in rows if row.label == "the interface"]
        self.assertEqual(len(interface), 1)
        self.assertEqual(interface[0].mark, codexify.OK)


class TestAStopThatStopped(SupervisionCase):

    @staticmethod
    def _stopping(install):
        """A supervisor that is asked to stop and then stops.

        The first call finds it running; every call after that finds it
        gone. The signals are swallowed, because the pid is a fiction
        and the machine running the test is not the stack.
        """
        calls = {"n": 0}

        def running(_install):
            calls["n"] += 1
            return 1234 if calls["n"] == 1 else None

        return running

    def test_a_port_still_held_is_not_a_stop(self):
        """The ports are the truth."""
        with mock.patch.object(native, "supervisor_running",
                               side_effect=self._stopping(self.install)), \
             mock.patch.object(native.os, "kill"), \
             mock.patch.object(native, "_stop_child_groups"), \
             mock.patch.object(codexify, "port_holder", return_value=54498), \
             mock.patch.object(codexify, "process_cwd",
                               return_value="/Users/someone/.Trash/codexify-core"):
            stopped, detail = native.stop(self.install)
        self.assertFalse(stopped)
        self.assertIn("still held", detail)
        self.assertIn("54498", detail)

    def test_a_stack_with_no_port_left_is_down(self):
        with mock.patch.object(native, "supervisor_running",
                               side_effect=self._stopping(self.install)), \
             mock.patch.object(native.os, "kill"), \
             mock.patch.object(native, "_stop_child_groups"), \
             mock.patch.object(codexify, "port_holder", return_value=None):
            stopped, detail = native.stop(self.install)
        self.assertTrue(stopped)
        self.assertIn("down", detail)

    def test_a_strangers_port_does_not_make_our_stack_look_up(self):
        """Our stack is down; somebody else's port is their business.

        The port is still theirs, and it will stand in the way of the
        next start, so it is named — but our stack is down, and saying
        otherwise is a report about a stack that is not ours.
        """
        elsewhere = str(self._tmp.name)      # exists, and is not our install
        with mock.patch.object(native, "supervisor_running",
                               side_effect=self._stopping(self.install)), \
             mock.patch.object(native.os, "kill"), \
             mock.patch.object(native, "_stop_child_groups"), \
             mock.patch.object(codexify, "port_holder", return_value=54498), \
             mock.patch.object(codexify, "process_cwd", return_value=elsewhere):
            stopped, detail = native.stop(self.install)
        self.assertTrue(stopped)
        self.assertIn("down", detail)
        self.assertIn("54498", detail)
        self.assertIn("another installation", detail)


class TestTheOrphan(SupervisionCase):
    """A process whose installation has been deleted out from under it."""

    def test_a_process_whose_directory_is_gone_is_an_orphan(self):
        """The last survivor of an install that was removed.

        Its pid files went with the directory, so there is no record of
        it left anywhere — and the port it holds will be held until
        somebody stops it by hand.
        """
        import shutil
        import subprocess
        gone = Path(self._tmp.name) / "install"
        gone.mkdir()
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import time; time.sleep(30)"],
                                cwd=str(gone))
        try:
            shutil.rmtree(gone, ignore_errors=True)
            self.assertTrue(native._is_orphaned(proc.pid))
            # And it is not ours: its working directory is not this
            # install, because it is not anywhere at all.
            self.assertFalse(native._is_ours(self.install, proc.pid))
        finally:
            proc.kill()
            proc.wait()

    def test_a_live_process_is_not_an_orphan(self):
        self.assertFalse(native._is_orphaned(os.getpid()))

    def test_the_squatter_is_named_as_an_orphan(self):
        """The message says what it is and what to do about it."""
        with mock.patch.object(codexify, "port_holder", return_value=4054), \
             mock.patch.object(native, "_is_orphaned", return_value=True), \
             mock.patch.object(native, "_is_ours", return_value=False):
            message = native._port_squatter(self.install, True)
        self.assertIn("4054", message)
        self.assertIn("orphan", message)
        self.assertIn("by hand", message)


class TestASchemaRowThatIsNotAVerdict(SupervisionCase):
    """A diagnostic that failed is not a finding about the schema."""

    @staticmethod
    def _getter(url, timeout=3.0):
        if url.endswith("/v1/models"):
            return 200, json.dumps({"data": [{"id": "fake-model"}]})
        if url.endswith("/healthz"):
            return 200, json.dumps({
                "backend": "postgres",
                "projects_table_exists": False,
                "chat_threads_table_exists": False,
            })
        return 200, "{}"

    def test_a_migrated_schema_is_not_reported_as_missing(self):
        """The migration landed; the diagnostic is what is broken."""
        with mock.patch.object(codexify, "port_holder",
                               return_value=os.getpid()):
            rows = codexify.readiness(self.install,
                                      http_getter=self._getter,
                                      skip_docker=True,
                                      backend_pid=os.getpid(),
                                      schema_migrated=True)
        schema = [row for row in rows if row.label == "the schema"]
        self.assertEqual(schema[0].mark, codexify.OK)
        self.assertIn("there", schema[0].detail)

    def test_without_a_migration_the_row_still_believes_the_backend(self):
        """A snapshot has no migration of its own to stand on."""
        with mock.patch.object(codexify, "port_holder",
                               return_value=os.getpid()):
            rows = codexify.readiness(self.install,
                                      http_getter=self._getter,
                                      skip_docker=True,
                                      backend_pid=os.getpid())
        schema = [row for row in rows if row.label == "the schema"]
        self.assertEqual(schema[0].mark, codexify.BAD)
        self.assertIn("did not land", schema[0].detail)


class TestTheQuietStretches(SupervisionCase):
    """The long silences, and what is said during them."""

    def test_the_spinner_is_usable_where_there_is_no_terminal(self):
        """A redirected run must stay a clean transcript."""
        spinner = native._spinner("waiting for the backend")
        with spinner:
            pass

    def test_the_spinner_runs_the_slow_thing_and_returns_its_answer(self):
        seen = []

        def slow(a, b=0):
            seen.append((a, b))
            return "done"

        with mock.patch.object(native, "_spinner") as fake:
            fake.return_value = mock.MagicMock()
            answer = native._with_spinner("installing the environment",
                                          slow, 1, b=2)
        self.assertEqual(answer, "done")
        self.assertEqual(seen, [(1, 2)])

    def test_the_spinner_says_what_is_slow(self):
        """The label is the whole point: it names the wait."""
        spinner = native._spinner("waiting for the backend")
        self.assertIsNotNone(spinner)

    def test_the_spinner_is_off_when_stderr_is_not_a_terminal(self):
        """A piped run gets no animation, only the rows."""
        with mock.patch("sys.stderr") as fake:
            fake.isatty.return_value = False
            spinner = native._spinner("waiting for the backend")
        self.assertFalse(getattr(spinner, "enabled", False))


class TestTheSupervisorContract(SupervisionCase):
    """The contract the supervisor keeps in the file it is read from."""

    def test_the_interface_cannot_move_to_another_port(self):
        """A vite that quietly moves leaves the old one serving.

        Without `--strictPort`, vite takes the next free port when the
        one it was asked for is busy. The health check then passes
        against the old process — yesterday's interface, answering as
        today's — and nobody is told.
        """
        source = (ROOT / "tools" / "codexify_supervisor.py").read_text(
            encoding="utf-8")
        self.assertIn("--strictPort", source)


if __name__ == "__main__":
    unittest.main()
