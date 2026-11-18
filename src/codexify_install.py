"""CODEXIFY INSTALL — getting the code onto the machine.

The vendored install is a git submodule, and a submodule is a promise the
parent repo cannot keep by itself: a plain clone leaves `codexify-core/`
empty, and the pinned commit is whatever the parent last vendored — not
whatever upstream has shipped since. `install` and `update` are the two
verbs that close that gap:

- **install** makes the directory exist, at the latest upstream `main`.
  It runs against a machine that has nothing, so an empty or missing
  directory is its normal case, not an error.
- **update** moves an existing installation to the latest upstream
  `main`. It runs against a machine that has something, so a missing
  directory is an error, and a dirty working tree is a refusal — an
  update that discards local edits is a destructive action wearing a
  harmless name.

What neither verb does: provision. Building the environment, making the
database, starting the stack — that is `setup`'s job, and one command does
one job. `install` ends with the code on the disk and a pointer to setup.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import codexify

# The vendored sources are read from this repository, not from the
# checkout — a checkout rewrite is exactly what they have to survive.
_ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------- the contract --

EXIT_OK = codexify.EXIT_OK
EXIT_MISSING_INSTALL = codexify.EXIT_MISSING_INSTALL
EXIT_PREREQUISITE = codexify.EXIT_PREREQUISITE
EXIT_CONFLICT = codexify.EXIT_CONFLICT

OK = codexify.OK
BAD = codexify.BAD
MANUAL = codexify.MANUAL

Row = codexify.Row
Report = codexify.Report

CommandRunner = codexify_native_runner = Callable[
    ..., subprocess.CompletedProcess]

SUBMODULE = "codexify-core"
REMOTE_BRANCH = "origin/main"

# How long a git step that touches the network may take. The clone is
# hundreds of megabytes and a slow link moves it at a few hundred KiB/s,
# so the honest time is minutes, not seconds. The old five minutes timed
# the wizard out of a clone that was still progressing — and a killed
# `git` leaves the `git clone` it spawned running, so the wizard reported
# a failure while the download went on without it. The operator watches
# the progress stream, so this is only the ceiling that lets a transfer
# that has truly stalled end — an hour is far past any real one here.
NETWORK_TIMEOUT = 3600.0


def _run_command(args: list[str], cwd: Optional[Path] = None,
                 timeout: float = 300.0,
                 stream: bool = True,
                 label: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run a command and keep its output. Never raises on a non-zero exit.

    With `stream`, the child's stdout and stderr are printed as they
    arrive — a fetch, a clone and a checkout are minutes of silence
    otherwise, and the wizard's contract is that the operator sees
    everything the machine is doing.

    `label` is what the spinner says while it runs: the step in plain
    words — "fetching the codexify-core submodule" — rather than the
    command's own name, because a name tells the operator what was
    typed, not what is happening.

    The child leads a process group of its own, and a timeout or a Ctrl+C
    takes the whole group down. `git submodule update` runs `git clone`,
    which runs `git-remote-https`; killing only the first leaves the rest
    downloading with nothing waiting for them, so the wizard would report
    a failure while the clone finished behind its back.
    """
    spin = (stream and timeout > 300 and sys.stderr.isatty())
    stop = threading.Event()
    thread = None
    if spin:
        thread = threading.Thread(
            target=_spin_stderr, args=(stop, label or "working"), daemon=True)
        thread.start()
    try:
        proc = subprocess.Popen(
            args, cwd=str(cwd) if cwd else None, text=True,
            stdout=None if stream else subprocess.PIPE,
            stderr=None if stream else subprocess.PIPE,
            start_new_session=True)
    except FileNotFoundError:
        stop.set()
        if thread is not None:
            thread.join(timeout=1.0)
        return subprocess.CompletedProcess(args, 127, "", "not found")
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        return subprocess.CompletedProcess(
            args, 124, "", f"timed out after {int(timeout)}s")
    except BaseException:
        # Ctrl+C reaches the foreground group, which is no longer the
        # child's: without this the operator stops the wizard and the
        # clone carries on.
        _kill_group(proc)
        raise
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=1.0)
    return subprocess.CompletedProcess(args, proc.returncode,
                                       out or "", err or "")


def _spin_stderr(stop: threading.Event, label: str) -> None:
    """A one-line working animation, on the operator's stderr only.

    A pac-man bar: the mouth eats its way along a row of dots, one per
    second, wrapping like a level — motion that never stalls even on a
    fetch ten minutes long, because the elapsed counter beside it is the
    thing that tells the truth about how long it has been.

    Redrawn in place with `\\r`, so the child's streamed lines stay whole
    above it; cleared on the way out so the row is not left hanging.
    """
    DOTS = 10
    started = time.time()
    try:
        while not stop.wait(0.1):
            elapsed = time.time() - started
            eaten = int(elapsed) % DOTS
            row = ("·" * eaten) + "ᗢ" + ("·" * (DOTS - eaten - 1))
            sys.stderr.write(
                f"\r\033[K  ᗢ {label or 'working'}  {row}  "
                f"{int(elapsed)}s")
            sys.stderr.flush()
    finally:
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()


def _kill_group(proc: subprocess.Popen) -> None:
    """Stop a child and everything it started: politely, then for certain.

    The group is swept with SIGKILL after the leader has had its moment,
    because a leader that exits on SIGTERM can leave members behind that
    were not listening.
    """
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()


def _git(runner: CommandRunner, repo: Path, *args: str,
         timeout: float = 300.0,
         stream: bool = False,
         label: Optional[str] = None) -> subprocess.CompletedProcess:
    return runner(["git", "-C", str(repo), *args], timeout=timeout,
                  stream=stream, label=label)


def _resolve_target(repo: Path, explicit: Optional[str]) -> Optional[Path]:
    """Where the install belongs.

    The same precedence as `find_install`: the explicit path wins, then
    the vendored submodule slot. The explicit path has to be the
    submodule's own name for the submodule machinery to work in it, which
    is a constraint the operator is told about rather than silently
    second-guessed.
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    return (repo / SUBMODULE).resolve()


def _is_empty_dir(path: Path) -> bool:
    """A directory with nothing in it — what a plain clone leaves."""
    return path.is_dir() and not any(path.iterdir())


def _status_text(done: subprocess.CompletedProcess) -> str:
    return ((done.stderr or "").strip() or (done.stdout or "").strip()
            or "git said nothing")


def _sync_lfs(runner: CommandRunner, target: Path, report: Report,
              timeout: float) -> tuple[bool, Report]:
    """Fetch the LFS objects the checkout is missing, if it is missing any.

    A checkout made without git-lfs looks complete and is not: every
    LFS-tracked file is its pointer text instead of its content, and the
    first tool that parses one — pnpm on package.json — dies inside a
    parse error that names neither file nor cause. This step is where
    the wizard says it instead.

    A machine with git-lfs and a pending pointer needs one `git lfs
    pull` in the submodule; one without it needs the package first, and
    gets the command named as a prerequisite. Convergence is not proof:
    a checkout that converged its way past a git-lfs-less past takes the
    same probe every run until the objects arrive.
    """
    pending = codexify.lfs_pointers_pending(target)
    if not pending:
        report.add("the LFS objects", OK, "already in the checkout")
        return True, report
    probe = runner(["git", "lfs", "version"])
    if probe.returncode != 0:
        report.add("the LFS objects", BAD,
                   f"{', '.join(pending)} are pointer files — git-lfs is "
                   f"not installed, and the checkout cannot have their "
                   f"content without it. Run: {codexify.lfs_install_hint()}")
        report.exit_code = EXIT_PREREQUISITE
        report.verdict = ("the checkout's LFS objects are missing — "
                          "nothing was installed")
        return False, report
    done = _git(runner, target, "lfs", "pull", "--quiet",
                timeout=timeout, stream=True,
                label="fetching the submodule's LFS objects")
    if done.returncode != 0:
        report.add("the LFS objects", BAD, _status_text(done)[-300:])
        report.exit_code = EXIT_PREREQUISITE
        report.verdict = ("the LFS objects could not be fetched — "
                          "nothing was installed")
        return False, report
    still = codexify.lfs_pointers_pending(target)
    if still:
        report.add("the LFS objects", BAD,
                   f"pulled, but {', '.join(still)} are still pointer "
                   "files — upstream may be missing the objects")
        report.exit_code = EXIT_PREREQUISITE
        report.verdict = ("the checkout's LFS objects are missing — "
                          "nothing was installed")
        return False, report
    report.add("the LFS objects", OK,
               f"fetched the content for {len(pending)} pointer file(s)")
    return True, report


def install(repo: Path, explicit: Optional[str] = None,
            runner: CommandRunner = _run_command,
            timeout: float = NETWORK_TIMEOUT, stream: bool = True) -> Report:
    """Make the installation exist, at the latest upstream main.

    The rows report the steps in the order they can be wrong: is the
    parent repo a git checkout at all, is the submodule registered, did
    the fetch reach upstream, did the checkout land, is the result a real
    installation. A machine that already has the latest commits gets
    green rows and no writes, because converging is the job — applying
    is not.
    """
    report = Report(heading="CODEXIFY (install)")
    target = _resolve_target(repo, explicit)

    # -- the parent repo ----------------------------------------------------
    if not (repo / ".git").exists():
        report.add("the repository", BAD,
                   f"{repo} is not a git checkout — the vendored install "
                   "lives in its submodule, and there is no submodule "
                   "machinery without git")
        report.exit_code = EXIT_PREREQUISITE
        report.verdict = "nothing was installed"
        return report
    report.add("the repository", OK, str(repo))

    # -- the submodule registration ----------------------------------------
    done = _git(runner, repo, "config", "--file", ".gitmodules",
                f"submodule.{SUBMODULE}.url")
    if done.returncode != 0 or not (done.stdout or "").strip():
        report.add("the submodule", BAD,
                   "the .gitmodules entry is not there. The vendored "
                   "install is a submodule, and this checkout does not "
                   "know about it — a re-clone with submodules, or a "
                   "manual entry, is needed first")
        report.exit_code = EXIT_PREREQUISITE
        report.verdict = "nothing was installed"
        return report
    report.add("the submodule", OK,
               (done.stdout or "").strip() + " (" + SUBMODULE + ")")

    # -- the checkout -------------------------------------------------------
    # `update --init` is the documented recovery for an empty directory and
    # the installer of a missing one. It is idempotent: an already-checked-
    # out submodule costs a few stat calls. It does *not* move an existing
    # checkout to the latest — that is the fetch below, which runs always,
    # so an install is never quietly the stale pinned commit.
    done = _git(runner, repo, "submodule", "update", "--init",
                "--recursive", "--quiet", SUBMODULE,
                timeout=timeout, stream=stream,
                label="installing the codex-core submodule")
    if done.returncode != 0:
        report.add("the checkout", BAD, _status_text(done)[-300:])
        report.exit_code = EXIT_PREREQUISITE
        report.verdict = "nothing was installed"
        return report
    if not target.is_dir():
        report.add("the checkout", BAD,
                   f"{target} is still not a directory")
        report.exit_code = EXIT_PREREQUISITE
        report.verdict = "nothing was installed"
        return report

    # -- the latest ---------------------------------------------------------
    fetched, report = _latest(runner, repo, target, report, timeout,
                               stream=stream)
    if not fetched:
        return report

    # -- the result ---------------------------------------------------------
    if not codexify.is_install(target):
        report.add("the installation", BAD,
                   f"{target} is not a Codexify installation — it is "
                   f"missing {codexify.COMPOSE_FILE} or "
                   f"{codexify.ENV_TEMPLATE}")
        report.exit_code = EXIT_CONFLICT
        report.verdict = "the code is on the disk, but it is not Codexify"
        return report
    report.add("the installation", OK,
               f"{target} — 'genesis codexify setup' provisions and "
               "starts it")

    report.verdict = "the latest code is on the disk"
    return report


def update(repo: Path, explicit: Optional[str] = None,
           runner: CommandRunner = _run_command,
           timeout: float = NETWORK_TIMEOUT, stream: bool = False) -> Report:
    """Move an existing installation to the latest upstream main.

    A missing installation is an error here — `install` is the verb for a
    machine that has nothing — and a dirty working tree is a refusal, for
    the same reason `setup` refuses to overwrite a hand-edited `.env`: an
    update that discards local edits is destructive, and destructive
    actions are the operator's call, taken with `--force` or by hand.
    """
    report = Report(heading="CODEXIFY (update)")
    target = _resolve_target(repo, explicit)

    if not codexify.is_install(target):
        report.add("the installation", BAD, codexify.install_note(explicit))
        report.exit_code = EXIT_MISSING_INSTALL
        report.verdict = "nothing was updated"
        return report

    # -- the working tree ---------------------------------------------------
    dirty = _git(runner, target, "status", "--porcelain")
    if dirty.returncode != 0:
        report.add("the working tree", BAD, _status_text(dirty)[-300:])
        report.exit_code = EXIT_PREREQUISITE
        report.verdict = "nothing was updated"
        return report
    if (dirty.stdout or "").strip():
        report.add("the working tree", BAD,
                   "there are local changes:\n"
                   + (dirty.stdout or "").strip()[:300])
        report.exit_code = EXIT_CONFLICT
        report.verdict = ("nothing was updated — local changes are the "
                          "operator's to keep or drop")
        return report
    report.add("the working tree", OK, "clean")

    # -- the latest ---------------------------------------------------------
    fetched, report = _latest(runner, repo, target, report, timeout,
                               stream=stream)
    if not fetched:
        return report

    report.verdict = "the installation is at the latest upstream main"
    return report


def _latest(runner: CommandRunner, repo: Path, target: Path,
            report: Report, timeout: float,
            stream: bool = False) -> tuple[bool, Report]:
    """Fetch upstream and check out its main, reporting the movement.

    Shared by install and update, because "the latest" is the same steps
    in both: where are we, can upstream be reached, what is there now,
    move to it. The one difference is what a refusal means — for install
    it is a machine that ends with no code, for update it is an install
    that stays where it is. Either way nothing is written on failure.
    """
    here = _git(runner, target, "rev-parse", "--short", "HEAD")
    if here.returncode != 0:
        report.add("the code", BAD, _status_text(here)[-300:])
        report.exit_code = EXIT_PREREQUISITE
        return False, report
    here_sha = (here.stdout or "").strip()
    report.add("the code", OK, f"at {here_sha}")

    done = _git(runner, repo, "submodule", "foreach", "--quiet",
                "git fetch --quiet origin main",
                timeout=timeout, stream=stream,
                label="fetching the codexify-core submodule upstream")
    # `foreach` walks every submodule; this repo has one. If the walk
    # fails, fall back to fetching inside the submodule directly.
    if done.returncode != 0:
        done = _git(runner, target, "fetch", "--quiet", "origin", "main",
                    timeout=timeout, stream=True,
                    label="fetching the codexify-core submodule upstream")
    if done.returncode != 0:
        detail = _status_text(done)
        report.add("the fetch", BAD,
                   ("upstream could not be reached: " + detail)[-300:]
                   if detail else "upstream could not be reached")
        report.exit_code = EXIT_PREREQUISITE
        report.verdict = ("the network is a prerequisite — nothing was "
                          "written")
        return False, report
    report.add("the fetch", OK, "upstream main is local")

    there = _git(runner, target, "rev-parse", "--short", REMOTE_BRANCH)
    if there.returncode != 0:
        report.add("upstream main", BAD, _status_text(there)[-300:])
        report.exit_code = EXIT_PREREQUISITE
        return False, report
    there_sha = (there.stdout or "").strip()

    if here_sha == there_sha:
        # A checkout that did not move still needs its fixes. The fixes
        # are applied after a move, so an install whose latest came in
        # before a fix existed — upstream did not change, the fix did —
        # is a checkout that has converged its way past ever applying it.
        # The pnpm-workspace placeholders are the proof this matters: a
        # machine that fetched its submodule before that fix was written
        # takes this branch on every run forever, and never gets it.
        applied, note = apply_vendored_fixes(target)
        report.add("the vendored fixes", OK if applied else MANUAL,
                   note)
        report.add("upstream main", OK,
                   f"also at {there_sha} — the latest already")
        # Convergence is not proof the LFS objects arrived: a checkout
        # made before git-lfs was installed has every file and none of
        # the contents, and it takes this branch on every run.
        ok, report = _sync_lfs(runner, target, report, timeout)
        return ok, report

    done = _git(runner, target, "checkout", "--quiet", there_sha,
                timeout=timeout, stream=stream,
                label="checking out the codexify-core submodule upstream")
    if done.returncode != 0:
        report.add("the move", BAD, _status_text(done)[-300:])
        report.exit_code = EXIT_PREREQUISITE
        report.verdict = "nothing was written"
        return False, report
    report.add("the move", OK, f"{here_sha} → {there_sha}")
    applied, note = apply_vendored_fixes(target)
    report.add("the vendored fixes", OK if applied else MANUAL, note)
    # The move just rewrote the working tree: whatever LFS objects the
    # last checkout had are gone with it, so a fresh checkout needs a
    # fresh fetch of them too.
    ok, report = _sync_lfs(runner, target, report, timeout)
    return ok, report


# ------------------------------------------------------------- the fixes --

# Fixes for bugs in the vendored code that the native path depends on.
# They live here — not in the submodule — because the submodule is
# upstream's: an install or an update overwrites whatever is inside it,
# and a fix that lives there is a fix that silently vanishes on the next
# fetch. Each entry is an exact find/replace on a file; if the text is
# not found, the fix is already applied or upstream changed the code,
# and both are reported rather than guessed at.
VENDORED_FIXES = (
    {
        "file": "backend/tts_service/config.py",
        "why": ("the TTS service ships only HuggingFace backends; the "
                "British male voice needs a provider entry so the console "
                "can list, preview and save profiles against it"),
        "find": "TTS_PROVIDERS = {\n",
        "replace": ("TTS_PROVIDERS = {\n"
                    "    # -- Local kokoro (GENESIS-provisioned, British "
                    "male voices) --\n"
                    "    \"kokoro\": {\n"
                    "        \"backend\": \"kokoro\",\n"
                    "        \"voice\": \"bm_george\",\n"
                    "    },\n"),
    },
    {
        "file": "backend/tts_service/app.py",
        "why": ("_resolve_backend knows one backend type; kokoro resolves "
                "to the vendored backend class, lazily imported so the "
                "service still boots without kokoro installed"),
        "find": ("    else:\n"
                 "        raise ValueError(f\"Unsupported backend type: "
                 "{backend_type}\")\n"),
        "replace": ("    elif backend_type == \"kokoro\":\n"
                    "        from .backends.kokoro_tts import "
                    "KokoroTTSBackend\n"
                    "        return KokoroTTSBackend(\n"
                    "            voice=config.get(\"voice\", \"bm_george\"))\n"
                    "    else:\n"
                    "        raise ValueError(f\"Unsupported backend type: "
                    "{backend_type}\")\n"),
    },
    {
        "file": "guardian/core/pgdb.py",
        "why": ("table_exists reads row[0] from a dict_row connection, "
                "which is a KeyError swallowed into 'migrations did not "
                "land' — a false failure on a healthy database"),
        "find": '''                cur.execute(
                    "SELECT to_regclass(%s)",
                    (f"public.{table_name}",),
                )
                row = cur.fetchone()
                return row[0] is not None if row else False''',
        "replace": '''                cur.execute(
                    "SELECT to_regclass(%s) AS reg",
                    (f"public.{table_name}",),
                )
                row = cur.fetchone()
                # dict_row rows need the alias, not an index
                return row["reg"] is not None if row else False''',
    },
    {
        "file": "frontend/src/hooks/useThemeController.ts",
        "why": ("the theme hook defaults to system when localStorage is "
                "empty, so a deployment cannot ship a house theme — the "
                "VITE_THEME_DEFAULT env var is honored as the fallback "
                "default, and localStorage still wins once set"),
        "find": '''  const [mode, setMode] = useState<ThemeMode>(() => {
    if (typeof window === "undefined") return "system";
    const raw = window.localStorage.getItem("cfy.themeMode");''',
        "replace": '''  const [mode, setMode] = useState<ThemeMode>(() => {
    if (typeof window === "undefined") return "system";
    const raw = window.localStorage.getItem("cfy.themeMode");
    if (raw == null) {
      // The deployment's theme — set by the host that serves this build.
      // localStorage still wins once the user has chosen.
      const fallback = (import.meta.env.VITE_THEME_DEFAULT as string) || "";
      return coerceMode(fallback);
    }''',
    },
    {
        "file": "pnpm-workspace.yaml",
        "why": ("upstream left literal placeholders in the build-approval "
                "map — pnpm 10's package-manager-policy reader parses the "
                "values as booleans and dies with 'expected value at line "
                "1 column 1' before installing anything; the container "
                "never saw it because its build only copies frontend/. "
                "Both tools need their postinstall builds for the dev "
                "server to run, so the values become true"),
        "find": """allowBuilds:
  '@tailwindcss/oxide': set this to true or false
  esbuild: set this to true or false""",
        "replace": """allowBuilds:
  '@tailwindcss/oxide': true
  esbuild: true""",
    },
)


# New files the native path needs — a fix can only patch what upstream
# ships; a backend upstream has never heard of has to arrive whole.
# The source lives in this repository and is copied into the checkout,
# so it survives an install/update that rewrites the tree.
VENDORED_FILES = (
    {
        "file": "backend/tts_service/backends/kokoro_tts.py",
        "why": ("a kokoro backend for the TTS service: the British male "
                "voice, run from the kokoro home the bootstrap builds on "
                "each machine — upstream ships no kokoro support"),
        "source": "tools/kokoro/codexify_kokoro_tts.py",
    },
)


def apply_vendored_fixes(target: Path) -> tuple[bool, str]:
    """Apply the fixes and files the native path needs to the fresh checkout.

    Idempotent: every fix is checked before it is applied, and every file
    is copied only when it differs from the vendored source, so a second
    run is a no-op. Returns (all_applied, note).
    """
    applied, already, notes = 0, 0, []
    for fix in VENDORED_FIXES:
        path = target / fix["file"]
        if not path.is_file():
            notes.append(f"{fix['file']} is not there to fix")
            continue
        text = path.read_text(encoding="utf-8")
        if fix["replace"] in text:
            already += 1
            continue
        if fix["find"] not in text:
            notes.append(f"{fix['file']}: the code to fix is not there — "
                         "upstream may have changed it")
            continue
        path.write_text(text.replace(fix["find"], fix["replace"], 1),
                        encoding="utf-8")
        applied += 1
    for entry in VENDORED_FILES:
        source = _ROOT / entry["source"]
        path = target / entry["file"]
        if not source.is_file():
            notes.append(f"{entry['source']}: the vendored source is "
                         "missing from this checkout of GENESIS")
            continue
        content = source.read_text(encoding="utf-8")
        if path.is_file() and path.read_text(encoding="utf-8") == content:
            already += 1
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        applied += 1
    total = len(VENDORED_FIXES) + len(VENDORED_FILES)
    if notes:
        # Something upstream moved or went missing: say so, and say so
        # as a row a human reads (MANUAL), not as success with the
        # missing pieces swallowed. What did apply, applied.
        prefix = (f"{applied} applied, {already} already there; "
                  if applied or already else "")
        return False, prefix + "; ".join(notes)
    if applied:
        return True, (f"{applied} fix(es)/file(s) applied, "
                      f"{already} already there")
    if already == total:
        return True, "all already applied"
    return False, "nothing was applied"


def prompt_whooshd_manual_config() -> dict[str, str]:
    """The Whoosh'd broker this machine should talk to, asked for once.

    The defaults are the Mac Studio's — the `space.codexify.whooshd`
    LaunchAgent serving the tailnet — and hitting enter takes them. A
    piped run takes them without asking: a script cannot answer a
    question, and the defaults are what it wanted anyway.
    """
    endpoint = codexify.DEFAULT_REMOTE_RUNTIME_ENDPOINT
    model = codexify.DEFAULT_REMOTE_RUNTIME_MODEL
    if (sys.stdin.isatty()
            and not getattr(prompt_whooshd_manual_config, "no_prompts", False)):
        print("\n[Whoosh'd Remote Inference Broker Setup]")
        try:
            endpoint = (input(f"  > Runtime endpoint URL [{endpoint}]: ").strip()
                        or endpoint)
            model = (input(f"  > Model identifier [{model}]: ").strip()
                     or model)
        except (EOFError, KeyboardInterrupt):
            pass
    return {
        "OPENAI_API_BASE": endpoint,
        "CODEXIFY_RUNTIME_ENDPOINT": endpoint,
        "CODEXIFY_MODEL": model,
        "CODEXIFY_API_KEY": "sk-local-macstudio",
    }

def main() -> None:
    """Run the interactive installation wizard with Whoosh'd remote broker configuration."""
    print("=== Codexify Genesis Installer ===")
    config = prompt_whooshd_manual_config()
    env_path = Path(".env")
    env_lines = [f"{k}={v}" for k, v in config.items()]
    env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    print(f"✅ Wrote remote inference config to {env_path.resolve()}")
    report = install(Path("."))
    print(report)
    raise SystemExit(report.exit_code)

if __name__ == "__main__":
    main()
