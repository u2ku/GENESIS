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

import subprocess
from pathlib import Path
from typing import Callable, Optional

import codexify

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


def _run_command(args: list[str], cwd: Optional[Path] = None,
                 timeout: float = 300.0,
                 stream: bool = False) -> subprocess.CompletedProcess:
    """Run a command and keep its output. Never raises on a non-zero exit.

    With `stream`, the child's stdout and stderr are printed as they
    arrive — a fetch, a clone and a checkout are minutes of silence
    otherwise, and the wizard's contract is that the operator sees
    everything the machine is doing.
    """
    try:
        if stream:
            done = subprocess.run(args, cwd=str(cwd) if cwd else None,
                                   text=True, timeout=timeout)
            return subprocess.CompletedProcess(args, done.returncode, "", "")
        return subprocess.run(args, cwd=str(cwd) if cwd else None,
                               capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, 127, "", "not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", "timed out")


def _git(runner: CommandRunner, repo: Path, *args: str,
         timeout: float = 300.0,
         stream: bool = False) -> subprocess.CompletedProcess:
    return runner(["git", "-C", str(repo), *args], timeout=timeout,
                  stream=stream)


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


def install(repo: Path, explicit: Optional[str] = None,
            runner: CommandRunner = _run_command,
            timeout: float = 300.0, stream: bool = False) -> Report:
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
    done = _git(runner, repo, "submodule", "update", "--init", "--recursive",
                SUBMODULE, timeout=timeout, stream=stream)
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
           timeout: float = 300.0, stream: bool = False) -> Report:
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

    done = _git(runner, repo, "submodule", "foreach",
                "git fetch origin main", stream=stream)
    # `foreach` walks every submodule; this repo has one. If the walk
    # fails, fall back to fetching inside the submodule directly.
    if done.returncode != 0:
        done = _git(runner, target, "fetch", "origin", "main",
                    timeout=timeout)
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
        report.add("upstream main", OK,
                   f"also at {there_sha} — the latest already")
        return True, report

    done = _git(runner, target, "checkout", there_sha, timeout=timeout,
                stream=stream)
    if done.returncode != 0:
        report.add("the move", BAD, _status_text(done)[-300:])
        report.exit_code = EXIT_PREREQUISITE
        report.verdict = "nothing was written"
        return False, report
    report.add("the move", OK, f"{here_sha} → {there_sha}")
    applied, note = apply_vendored_fixes(target)
    report.add("the vendored fixes", OK if applied else MANUAL, note)
    return True, report


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
)


def apply_vendored_fixes(target: Path) -> tuple[bool, str]:
    """Apply the fixes the native path needs to the fresh checkout.

    Idempotent: every fix is checked before it is applied, so a second
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
    if applied:
        return True, f"{applied} fix(es) applied, {already} already there"
    if already == len(VENDORED_FIXES):
        return True, "all already applied"
    return False, "; ".join(notes) or "nothing was applied"


def prompt_whooshd_manual_config() -> dict:
    """Interactive wizard step to manually configure remote Whoosh'd inference broker."""
    print("\n[Whoosh'd Remote Inference Broker Setup]")
    endpoint = input("  > Enter runtime endpoint URL [http://100.71.144.117:8000/v1]: ").strip() or "http://100.71.144.117:8000/v1"
    model = input("  > Enter model identifier [supergemma4-26b-uncensored]: ").strip() or "supergemma4-26b-uncensored"
    return {
        "OPENAI_API_BASE": endpoint,
        "CODEXIFY_RUNTIME_ENDPOINT": endpoint,
        "CODEXIFY_MODEL": model
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
