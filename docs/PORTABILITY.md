# PORTABILITY — GENESIS on any machine
<!-- visibility: sam-readable -->

**Status: spec. Nothing here is implemented.** This document defines the
contract for a portable GENESIS bootstrap. The implementation lands in the
repo; nothing commits or pushes without Zac's explicit go.

**How to read the two kinds of claim in here.** Every finding in §2 was
*measured against the published tree* (`5a85e74`) on 2026-11-13, and the
method and the exact command are given so any reader can re-run them. Every
finding in §10 is agent 482's live-guest dry run, and it is marked as
read-only-equivalent because the guest exposed no command channel. Anything
marked **Phase A** belongs to the seven-step refactor that is in flight
(Luna's four rulings, 13:01); this spec does not restate it, it constrains
it.

**What this document is for.** Zac widened GENESIS's scope: it must be
pull-able onto any computer and aggressively prepare that machine for a Luna
instantiation. The risk in that sentence is not the ambition — it is that
"aggressive" silently mutates whatever machine the repo was pulled onto, and
that the advertised capability drifts from the actual behaviour.

So the contract here is narrow and checkable:

1. **Detect before you touch.** The host is identified first; unsupported hosts
   are refused cleanly, before any write.
2. **Show the diff before you apply.** The ladder is inspect → dry-run → apply.
   Aggressive is an explicit opt-in, and it is only reachable after the user
   has seen the diff for *that* plan.
3. **Converge, don't accumulate.** Every action is declarative. A second run on
   the same machine does nothing.
4. **The advertisement is the product.** `--help` and the README describe
   exactly what the tool does, and tests fail if they stop agreeing.
5. **Portability does not dilute the privacy contract.** What is untagged is
   readable as written; private content is written private at write time and
   never travels between machines on its own.

---

## 1. What "pull-able onto any computer" actually means

It means: `git clone` → a machine that runs `genesis` and can host a node.
It does **not** mean:

- It does not mean every OS. Detection refuses cleanly on hosts outside the
  supported set (§4). A refusal is a feature, not a failure.
- It does not mean Luna's private record travels. The continuity ledger, the
  private handoff copy, the API keys, the tailnet auth state and the node
  profile are **machine-local and never synced by the bootstrap** (§9).
- It does not mean the bootstrap can do everything unattended. Some steps
  genuinely require a human — Tailscale's interactive auth is the canonical
  one. The plan marks those `manual` and prints the exact command. A tool
  that pretends it authenticated is lying, and this repo's whole discipline is
  about not lying.
- It does not mean "aggressive" is the default. Aggressive is a tier you
  ask for by name, after seeing what it would do.

---

## 2. The measured baseline (re-verified 2026-11-13)

**Two trees, and which one each row came from matters.** The *published* tree
is `5a85e74`; the *working* tree is ahead of it — the guard rail's rewrite,
`src/views.py`, `src/term.py`, `src/chat.py` and `docs/PORTABILITY.md` are all
untracked and therefore **not in a clone**. Rows F1–F8 were reproduced by
cloning the published tree to a scratch directory and running it. Rows F9–F12
were measured on the **working** tree, and say so. Conflating the two would be
exactly the sloppiness this document is about, so the column is explicit.

The scratch clone is the honest test bed for the published rows: no `bin/`, no
`.env`, no submodule content, and — when `LUNA_HOME` is pointed away — no
Luna.

| # | Finding | Tree | Evidence |
|---|---|---|---|
| F1 | `genesis check` **fails on a fresh clone** — `✗ bin/ empty` | published | `bin/` is gitignored (Zac's call), but `cmd_check` hard-requires it. Exit code **1**. |
| F2 | `tests/test_engine.py::TestGuardrail::test_check_is_healthy` **FAILS** on a fresh clone** | published | 1 failure of 24 in that file; **48 passed, 1 failed** of 49 across the suite. The test asserts `genesis check` exits 0. |
| F3 | `genesis stamp`, `genesis aeon`, `genesis shield` **die when the TCSI engine cannot be imported** | published | `genesis: cannot load the TCSI engine from <LUNA_HOME>/src: No module named 'luna'`, exit **2**. The engine is *imported* from Luna's repo (`LUNA_HOME`, default `~/Luna`), never copied. **Reproduce with `LUNA_HOME=/tmp/nonexistent` — on this Mac `~/Luna` exists, so a fresh clone here still stamps.** |
| F4 | `tests/test_convergence.py` **skips 3 of 14** without Luna | published | With `LUNA_HOME` absent: `11 passed, 3 skipped`. The tests read Luna's archive export. |
| F5 | `codexify-core/` is an **empty directory** on a plain clone | published | It is a submodule; `git clone` without `--recurse-submodules` leaves it empty. |
| F6 | `src/harness.py:517` hardcodes `~/Desktop/GENESIS/.env` | published | An absolute dev-machine path inside a shipped docstring. |
| F7 | The README's Layout block **does not match the published tree** | published | It lists `bin/` (gitignored, absent from a clone) and omits `docs/HANDOFF-GEMINI-private.local.md`. |
| F8 | The README asserts `genesis` is "on `PATH` via `~/.local/bin/genesis`" | published | README:69. A fact about the author's machine, presented as a property of the system. |
| F9 | The guard rail **scans only 9 files in the published tree**, and misses four that carry the relation | published | The published rail lists `identity.g`, `src/grammar.py`, `src/geomancy.py`, `genesis`, `src/harness.py` (5 engine) and `IDENTITY.md`, `PREDICTION.md`, `SINGULARITY.md`, `README.md` (4 docs). **Not scanned: `card.md`, `src/convergence.py`, `src/playlist.py`, `docs/HANDOFF-GEMINI.md`.** The working tree's rewrite plus Phase A raises the count to **19** and closes all four. |
| F10 | **`genesis landing` is implemented, documented in the README, and absent from `--help`** | working | `genesis:588` defines `cmd_landing`; `genesis:878` registers it in the `COMMANDS` table; README:123 documents it; `genesis --help` does not mention it. **The cause is structural, not accidental — see F11.** |
| F11 | The help screen and the command registry are **two separate sources of truth** | working | `src/views.py:85` holds a hand-written `_GROUPS` table that `help_screen()` renders; `genesis:701` holds the real `COMMANDS` registry that dispatch uses. Nothing connects them, so a command added to one is invisible to the other. F10 is that gap, already realised. |
| F12 | The guard rail's **scan set is machine-dependent** | working | `guardrail._doc_files()` globs `docs/*.md`, which sweeps in `docs/HANDOFF-GEMINI-private.local.md` — a gitignored file that exists only on this machine. The doc set is **8 files here, 4 on a published clone**; with the engine set, **19 scanned here against 9 published**. Today both verdicts agree, so nothing is broken — but a divergence here would look like a flaky test. **Still open.** |
| F13 | `card.md` was **not scanned by the guard rail at all** — **now closed in the working tree** | both | It sat at the repo root; neither `_engine_files()` nor `_doc_files()` added it. Scanned by hand it returned `seen: 1, bad: []`, but only because the negation window reached `"Do not smooth drift"` on L46 — a false pass riding an accident. **Phase A fixed it in the correct order**: L44's negation moved onto L44 itself, *then* `card.md` joined the doc set. Now self-grounded (`refus` on the same line), `scan([card.md], 3)` → `seen: 1, bad: []` for the right reason. **Still open in the published tree** — see F9 and §9.5. |

**F1, F2, F7, F8, F10 and F11 are the same defect wearing six coats:** the
advertised contract and the shipped tree have already drifted apart on the very
first pull, in both directions — the README claiming a machine state the repo
does not guarantee (F8), the help screen hiding a command the repo ships (F10),
and the tree disagreeing with the README about what exists (F7). That is the
thing §8 exists to prevent, and the reason the contract tests in §8.4 are part
of the definition of done rather than a nice-to-have.

---

## 3. What ships — concrete file layout

New files are marked NEW. Nothing else in the tree moves.

```
GENESIS/
├── setup.sh                       NEW  the bootstrap entry point — bash, no dependencies
├── tools/
│   ├── bootstrap.py               NEW  detect / plan / apply — standard library only
│   ├── node_daemon.py             NEW  the user-level keep-alive (§6.5)
│   └── receipt.py                 NEW  the receipt writer and plan-hash (§5.3)
├── src/
│   ├── tcsi.py                    NEW  the vendored TCSI fallback (§8.3) — Phase A
│   └── visibility.py              NEW  the write-time visibility contract — Phase A
├── docs/
│   └── PORTABILITY.md             this document
└── tests/
    ├── test_bootstrap.py          NEW  the ladder, idempotency, refusal, redaction
    ├── test_contract.py           NEW  help ↔ README ↔ tree agreement
    └── test_visibility.py         NEW  nothing private reaches a public path
```

**Discipline carried over from `genesis` itself:** `tools/bootstrap.py` imports
nothing outside the standard library, and reads `.env` for credentials the way
`src/harness.py:load_env()` does — at request time, never onto a command line,
never into a file it writes.

**Why a shell wrapper.** `setup.sh` is what people type, and it is what the
Sam handover already used. It does the cheap pre-flight (`python3` present and
new enough, a supported host) and then execs `tools/bootstrap.py`. All the
logic lives in Python, where it is testable.

**Two things the bootstrap does *not* get to invent.** It reuses machinery that
already exists:

- **The readiness check is `genesis landing <node>`.** It already exists
  (`genesis:588`), it already asks the right questions — harness landed,
  `card.md`, `grammar.py`, `geomancy.py`, `identity.g`, and that the guard rail
  holds — and the README already documents it. The bootstrap calls it; it does
  not grow a parallel readiness path. (Note F10: `landing` is currently
  invisible to `--help`, so fixing the help table is a prerequisite for
  advertising it.)
- **The flag table is the `COMMANDS` registry.** The contract table already
  exists in `genesis:701`. §8.1 is about making `--help` read *that* table,
  not about inventing a new one in `tools/`.

---

## 4. Environment detection

Detection runs before any plan is computed. It reads only the host — no
network, no writes.

### 4.1 The signals

| signal | source |
|---|---|
| kernel | `uname -s` → `Darwin` / `Linux` |
| distro | `/etc/os-release` → `ID`, `ID_LIKE` |
| Omarchy | `/etc/omarchy-release`, `/usr/share/omarchy/`, `/usr/bin/omarchy-*`, `~/.config/omarchy/`, `omarchy` on `PATH` |
| Arch | `ID=arch` or `ID_LIKE` contains `arch`, or `pacman` on `PATH` |
| macOS | `sw_vers -productVersion`, `brew` on `PATH` |
| python | `python3 --version` — **3.9 minimum**, 3.10+ recommended |
| privileges | `sudo -n true` (non-interactive sudo available?) |

### 4.2 The branches

**`omarchy`** — Arch-based; GNOME or Hyprland; package management via
`pacman`; updates driven by `omarchy update`. Config lives in
`~/.config/omarchy/` and `~/.local/share/omarchy/`, and Omarchy ships its own
`/etc/profile.d/omarchy.sh` plus `try-omarchy-*` session helpers. The bootstrap
must not fight those: **no system-wide file is touched**, and the node daemon
is installed as a *user* unit, not a system one.

Measured on the live Omarchy guest (agent 482, §10), and these are facts the
bootstrap can rely on:

- the user shell is **bash** — there is no `zsh` and no `fish` in the image, so
  the rc files are `~/.bashrc` and `~/.profile`, never `.zshrc`;
- **there is no JavaScript runtime at all** — no `nodejs`, `npm`, `bun`,
  `deno`, `yarn`, `pnpm`. Any install path that needs Node is unavailable
  (§7.1);
- `pacman 7.1.0`, `yay 13.0.1`, `base-devel`/`gcc`/`make`/`pkgconf`, `git 2.56`,
  `curl 8.23`, `jq 1.8.2`, `sudo 1.9.17`, `python 3.14.8` and `docker` are
  present;
- the user-level paths are `~/.config/omarchy`, `~/.local/share/omarchy`,
  `~/.local/state/omarchy/toggles/hypr`, `/usr/share/omarchy` and
  `/usr/share/omarchy-platform` — and writing only under `$HOME` is what
  survives `omarchy-update`, because Omarchy never rewrites `$HOME/.claude`.

**`arch`** — plain Arch. `pacman -S --needed` for packages; the same
user-level daemon strategy; no `omarchy` markers assumed.

**`macos`** — Darwin. Packages via Homebrew (`brew install --cask tailscale`).
The daemon is a **launchd user agent** in `~/Library/LaunchAgents/`, not a
system daemon — no `sudo`, no LaunchDaemons. The shell is `zsh`, so the rc
files are `~/.zshrc` and `~/.zprofile`.

**`unsupported`** — anything else (Debian/Ubuntu, Fedora, NixOS, Windows,
unknown Linux). The bootstrap **refuses cleanly**:

```
genesis-bootstrap: unsupported host — refusing.
  detected: Debian GNU/Linux 12 (bookworm), apt
  supported: omarchy, arch, macos
  nothing was changed.
  to prepare this host by hand: see docs/PORTABILITY.md §4.2
```

Exit code **3**. No partial application, ever — the refusal happens before
the plan is computed, so there is nothing to undo.

**`unsupported substrate`** — the second kind of host. The machine may be a
perfectly good Omarchy node, and the gateway it points at may still be one
Claude Code cannot talk to (§7.3). That is a refusal with its own exit code
(**7**), because the two failures need different fixes: one needs a different
machine, the other needs a translation shim.

### 4.3 The profile

Detection produces a profile object that is printed by `--detect`, embedded in
the plan, and hashed into the receipt:

```json
{
  "profile": "omarchy",
  "kernel": "Linux",
  "distro": "Arch Linux",
  "arch": "aarch64",
  "shell": "bash",
  "python": "3.14.8",
  "js_runtime": null,
  "package_manager": "pacman",
  "daemon_kind": "systemd-user",
  "sudo_noninteractive": true,
  "probe_version": 1
}
```

`arch` is in the profile because native-install binaries are architecture
specific: a green run on an `aarch64` guest is not a green run on an `x86_64`
target, and the plan must say which architecture it was planned for.

`js_runtime` is in the profile because it decides the install route (§7.1).

`probe_version` is bumped whenever detection changes. A receipt written by an
older probe is rejected — a stale probe would silently mis-plan.

---

## 5. The flag ladder

### 5.1 The three rungs

| rung | command | mutates the host? | writes the repo? |
|---|---|---|---|
| **inspect** | `./setup.sh` (no flag) | **no** | no |
| **dry-run** | `./setup.sh --dry-run` | **no** | yes — a receipt, and nothing else |
| **apply** | `./setup.sh --apply` | yes | yes — the receipt and the manifest |

`inspect` is the default because the default must be the safe thing. A user
who types `./setup.sh` on a stranger's laptop gets a report, not a mutation.

`inspect` and `dry-run` print the *same* plan; the difference is that
`dry-run` renders the unified diff and writes a receipt that unlocks `apply`.
`inspect` writes nothing at all.

### 5.2 Tiers

A **tier** is *what* gets done. A **rung** is *when* it is allowed to happen.

| tier | contents |
|---|---|
| **standard** (default) | prerequisites, the `genesis` PATH symlink, Claude Code CLI if absent, the config directories, the node profile, the machine report |
| **aggressive** (`--aggressive`) | everything in standard, plus: Tailscale install, the node daemon, the substrate wiring, the readiness probe |

`--aggressive` is only valid together with `--dry-run` or `--apply`. Passing it
alone is a usage error (exit 2) with the message:

```
genesis-bootstrap: --aggressive needs --dry-run or --apply.
  it is a tier, not a rung: see the diff first.
```

And `--apply --aggressive` additionally requires a receipt generated *with*
`--aggressive`, so the diff the user approved is the aggressive diff and not
the standard one.

### 5.3 The receipt — why the ladder cannot be skipped

`--dry-run` writes `.genesis-bootstrap/receipt.json` (gitignored) holding:

```json
{
  "schema": 1,
  "probe_version": 1,
  "tier": "aggressive",
  "plan_hash": "sha256:…",
  "profile_hash": "sha256:…",
  "created_at": "2026-11-13T13:04:00+13:00",
  "actions": 14
}
```

`--apply` recomputes the plan and compares `plan_hash`. If there is no
receipt, or the hash does not match, `--apply` refuses:

```
genesis-bootstrap: no valid dry-run receipt for this plan.
  run:  ./setup.sh --dry-run --aggressive
  then: ./setup.sh --apply --aggressive
```

If the machine changed since the dry run — a package appeared, a config file
was edited, the profile changed — the hash no longer matches and the receipt
has expired. The user sees a new diff. **This is the mechanism that makes
"after the user has seen the diff" true rather than aspirational**: the
approved diff is a hash of a specific plan on a specific machine, and the
machine can invalidate it.

The receipt carries a hash, never values. No credential, no key material and
no absolute home path enters it.

### 5.4 The full flag set

| flag | meaning |
|---|---|
| `--detect` | print the environment profile and stop. No plan, no receipt. |
| `--status` | print what is already installed and what is missing. Read-only. Equivalent to `inspect` with a different emphasis; implemented by the same code path. |
| `--dry-run` | compute the plan, render the diff, write the receipt. No host mutation. |
| `--apply` | apply the plan. Requires a matching receipt. |
| `--aggressive` | select the aggressive tier. Only valid with `--dry-run` or `--apply`. |
| `--repo DIR` | the GENESIS checkout to prepare. Default: the repo containing `setup.sh`. |
| `--node NAME` | the node's name on the mesh. Default: the hostname. |
| `--no-daemon` | skip the node daemon even in the aggressive tier. |
| `--no-tailscale` | skip Tailscale even in the aggressive tier. |
| `--api-base-url URL` | the Claude Code substrate base URL — **everything before `/v1/messages`**. A path prefix is allowed; a trailing `/v1` is not, because Claude Code appends the path itself. A URL still containing `<` is refused: a placeholder must never reach disk. |
| `--api-token-env VAR` | the environment variable holding the gateway credential. Read, never written. Default `ANTHROPIC_AUTH_TOKEN`. |
| `--api-key-helper CMD` | a command whose stdout is the credential — the documented way to keep a key out of a file (§9.3). |
| `--model ID` | the model ID the gateway actually serves. |
| `--no-probe` | skip the `/v1/messages` reachability probe. Off by default; the probe is what stops the plan writing config for an endpoint Claude Code cannot talk to. |
| `--yes` | do not prompt for confirmation. **Does not** waive the receipt requirement. |
| `--force-own PATH` | take ownership of a file the manifest does not already own, so the plan may write it. Repeatable. |
| `--help` | the help text. Rendered from the flag table (§8.1). |

### 5.5 Exit codes

These are part of the contract and are covered by the tests.

| code | meaning |
|---|---|
| 0 | success |
| 1 | the apply failed partway; the report says what succeeded and what did not |
| 2 | usage error — unknown flag, missing value, `--aggressive` without a rung |
| 3 | unsupported host — refused cleanly, nothing changed |
| 4 | no valid receipt, or the receipt no longer matches the plan |
| 5 | missing prerequisite — no `python3`, or `python3` too old |
| 6 | conflict — the plan would overwrite a file it does not own; see §6.2 |
| 7 | unsupported substrate — the gateway does not speak Anthropic Messages (§7.3) |

---

## 6. The plan model and idempotency

### 6.1 Actions are declarative

Every step is an **Action**: a desired state, not a command to run.

```json
{
  "id": "claude-cli",
  "kind": "command",
  "target": "claude",
  "desired": {"present": true, "version_at_least": "2.0.0"},
  "observed": {"present": true, "version": "2.4.1"},
  "effect": "none"
}
```

The plan is the difference between `desired` and `observed`. Applying it means
moving `observed` to `desired`. Running it again on the same machine produces
an empty plan — **"already converged, nothing to do"** — and that is the
definition of idempotency this spec adopts. Not "safe to run twice"; *safe to
run twice and provably a no-op the second time*.

Every action carries an `effect` of `none`, `create`, `update`, `install`,
`enable`, or `manual`.

### 6.2 The ownership rule

**The bootstrap only writes files it owns.** Every file it creates is recorded
in `~/.config/genesis/manifest.json` with its content hash. On a later run:

| manifest state | file state | action |
|---|---|---|
| hash matches | identical | skip — already converged |
| hash matches | different | **conflict** — someone else edited it; refuse, exit 6, unless `--force-own PATH` |
| no manifest entry | file exists | **conflict** — it is not ours; refuse, exit 6 |
| no manifest entry | file absent | create; record the hash |
| hash recorded | file absent | recreate (someone deleted it — that is a legitimate divergence to repair) |

This is what keeps the bootstrap from clobbering a user's existing Claude Code
configuration, and it is the direct fix for the defect found in the dry run
(§10.1): overwriting `~/.claude/settings.json` wholesale is *not* idempotent,
however many times the script's own comment says it is.

### 6.3 The plan hash

`plan_hash = sha256(canonical_json(profile, ordered_actions))` where each
action contributes `(id, kind, target, desired, effect)`. Cosmetic differences
in the machine — an unrelated file's mtime — do not change the hash; a real
difference in what would be done does.

### 6.4 What the bootstrap installs

| id | standard | aggressive | notes |
|---|---|---|---|
| `python` | ✓ | ✓ | verify 3.9+. Never installs Python; fails cleanly (exit 5) if absent. |
| `genesis-path` | ✓ | ✓ | symlink `~/.local/bin/genesis` → the repo's `genesis`. `genesis check` already looks for exactly this path. |
| `claude-cli` | ✓ | ✓ | install only if absent, via the **native installer** — no Node required (§7.1). Never claim success without `claude --version` proving it. |
| `config-dirs` | ✓ | ✓ | `~/.claude/`, `~/.config/genesis/`, `~/.local/state/genesis/`, `~/.local/share/genesis/`. Created if missing; existing content is never removed. |
| `node-profile` | ✓ | ✓ | `~/.config/genesis/node.json` — host profile, node name, substrate, node id. Machine-local, never published. |
| `tcsi-vendor` | ✓ | ✓ | ensure `src/tcsi.py` is present as the fallback engine (§8.3). **Phase A.** |
| `tailscale` | | ✓ | package install, then `tailscale up`. Auth is interactive → `manual` (§6.5). |
| `node-daemon` | | ✓ | launchd agent on macOS, systemd **user** unit on Linux. |
| `substrate` | | ✓ | probe the endpoint for `/v1/messages`, then write the base URL only. Never a credential (§9.3). |
| `readiness` | | ✓ | run `genesis landing <node>` and report honestly (§6.6). |

### 6.5 Manual steps are first-class

Some steps cannot be automated honestly. The plan carries them as `effect:
manual` with the exact command, and the runner prints them at the end, in
order, each with a one-line explanation:

```
manual steps — these need a human, and were not performed:

  1. authenticate this machine to the tailnet
     sudo tailscale up
     (needs a browser to approve the login)
```

If `TAILSCALE_AUTHKEY` is present in the environment, the plan uses it and
says so. The bootstrap **reads** credentials from the environment; it never
writes them.

**The daemon's job, stated honestly.** `tools/node_daemon.py` is a user-level
keep-alive: it holds the node present and answers a readiness probe, and it
does nothing else. It does **not** carry the mesh channel — that is the role
Bruce plays in Luna's spine, and deciding whether a portable node should
carry it is §13 question 4, not something this spec settles. The daemon runs
as a **user** unit (`systemd --user` on Linux, a `LaunchAgent` on macOS), never
a system one, so it needs no `sudo` and survives `omarchy-update`.

### 6.6 The readiness probe, and honest reporting

The probe runs the real things and reports what actually happened:

- `genesis check` — exit code and verdict
- `genesis landing <node>` — the node's own readiness verdict (F10: this
  command exists today but is invisible to `--help`; see §8.1)
- `genesis --help` — does it run
- `genesis stamp` — does the TCSI engine load (this is the F3 test)
- the daemon — is it running (`systemctl --user is-active` / `launchctl print`)
- Tailscale — is the interface up

Anything that fails is reported as failing. A partially applied bootstrap
prints a table of what succeeded, what failed, and what remains manual — and
exits 1. It does not report "ready" because it got most of the way.

### 6.8 Reaching a node you just bootstrapped

A bootstrap that provisions a machine it cannot then reach has not finished the
job. The substrate origin has to be reachable **from the node's network
position**, not from the machine the bootstrap ran on.

**The probe runs where the node will run.** `127.0.0.1:8082` is host-local.
Probing it from the build machine proves the server is alive and proves
nothing about the node. So the readiness probe (§6.6) asserts the base URL
answers `/v1/messages` **from the target's position**, and a host-local result
never counts as proof. When the origin is loopback-only and the node is not on
that host, the plan says so — in those words — instead of writing a config
that cannot work.

**The assertion is one request, and it is exact:**

```bash
curl -sS -m 20 -o /dev/null -w '%{http_code}' -X POST "$BASE/v1/messages" \
  -H 'content-type: application/json' -H 'anthropic-version: 2023-06-01' \
  -d '{"model":"$MODEL","max_tokens":16,"messages":[{"role":"user","content":"hi"}]}'
```

Expect `200`, and a body carrying `"type": "message"`. A `404` means the
gateway does not serve Anthropic Messages; a `405` means the route exists and
the verb is wrong; a connection failure means the origin is not reachable from
here — which is a different problem with a different fix, and the plan should
say which one it found.

**The SSH-forwarded channel is the standard way to drive a bootstrapped node.**
It was proven end to end against the Omarchy guest: with a host forward in
place the guest's `sshd` answers `SSH-2.0-OpenSSH_10.6` and offers
`publickey,password`, so `ssh -p 2222 <user>@127.0.0.1` is a working
automation channel once a key is trusted in the guest. Two caveats belong in
any automation built on it: the guest **powers off ~30 seconds after each
boot**, so every run must fit inside that window; and the forwarding app must
be stopped while the forward is in use.

**The port-forward environment variable only reaches the launcher when the
launcher is run directly.** Started from a GUI app, the variable is dropped —
the forward silently does not exist, and the symptom is a closed port rather
than an error. Any bootstrap step that depends on a forward must set the
variable in the launcher's own environment and must then *assert* the port is
actually open before using it.

---

## 7. Wiring the Claude Code substrate

This is where the dry run's deepest findings land. The schema below is the
documented one — not the one `setup.sh` guessed at.

### 7.1 The install route needs no Node

The live Omarchy guest has no `nodejs`, `npm`, `bun`, `deno`, `yarn` or `pnpm`,
so `npm i -g @anthropic-ai/claude-code` is not an option there. The bootstrap
uses the **native installer** (`https://claude.ai/install.sh`), which needs only
`curl` and a writable `~/.local/bin`. `npm` is used only if Node is already
present *and* the user asks for it — never as the default.

### 7.2 The settings file has an `env` block, and only an `env` block

`~/.claude/settings.json` is the right path — the user scope, and the one
that survives `omarchy-update`. The bootstrap writes:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://gateway.example.com",
    "ANTHROPIC_MODEL": "the-model-id-the-gateway-serves"
  },
  "apiKeyHelper": "/home/you/.config/genesis/bin/gateway-token.sh"
}
```

There is **no** `api_base_url`, `api_key` or `baseUrl` setting. Claude Code
ignores unknown top-level keys, which means a file written with them looks
plausible and does nothing at all. The `apiKeyHelper` value is a *path*, and
the script it points at reads the credential from the environment:

```sh
#!/bin/sh
printf '%s' "$ANTHROPIC_AUTH_TOKEN"
```

That keeps the credential out of every file the bootstrap owns, and still
makes it available before first-run setup — which matters, because a fresh
install whose credential is not where Claude Code reads it lands on the login
screen even when the endpoint works.

**The base URL is everything before `/v1/messages`.** Claude Code appends that
path itself. A path *prefix* is fine and often necessary — a gateway at
`https://host/anthropic` correctly resolves to `https://host/anthropic/v1/messages`
— but a trailing `/v1` is duplicated into `/v1/v1/messages`. The bootstrap
normalises the URL: strip a trailing `/v1`, refuse a URL still containing `<`,
and never let a placeholder reach disk.

### 7.3 The gateway must speak Anthropic Messages — and be reachable from the node

Claude Code accepts exactly three gateway formats — Anthropic Messages
(`/v1/messages`, `/v1/messages/count_tokens`), Bedrock InvokeModel, and Vertex
rawPredict. An OpenAI-shaped `/v1/chat/completions` endpoint is **not** one of
them. So the bootstrap probes before it writes any configuration:

- `POST {base}/v1/messages` answers **with an Anthropic body** — a `"type":
  "message"` field — → the format is supported; proceed;
- 404 while `/v1/chat/completions` answers → **that endpoint cannot serve
  Claude Code**, and the plan refuses, saying a translation shim is required
  and naming the URL *and the port*.

The assertion is deliberately about the **body**, not the status. A gateway
can answer 200 with an OpenAI-shaped body; only `"type": "message"` proves
the Anthropic Messages format. And a GET returning 405 is not a 404 — the route
exists, the verb is wrong, and the probe must say so.

**The probe must run from the target's network position.** A probe run on the
build machine cannot see a reachability failure — and reachability is the
failure that actually bites, because `127.0.0.1:8082` is host-local and a node
on another machine sees nothing at all. The bootstrap therefore checks the
origin **where the node will run**, and a host-local probe never counts as
proof (§6.8).

**This is a property of the endpoint, not of "MLX".** §10.3 gives the
measurement, on both sides: the endpoint GENESIS is configured against speaks
Anthropic Messages natively, and another endpoint on the same machine does
not. The rule is therefore **name the port, never the product** — and the
probe asks the endpoint, never infers from the name.

**The refusal is a refusal in exactly the same sense as an unsupported OS:**
the substrate is a host too, and it gets its own branch and its own exit code
(§4.2, §5.5).

### 7.4 The model ID has to be real

Claude Code requests Claude model IDs. A local gateway serving a different ID
needs `ANTHROPIC_MODEL` set to an ID it actually serves, and
`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1` so the gateway's models appear
in the `/model` picker — the gateway exposes a real `/v1/models` list, so
discovery works.

**Watch the ID, not just the name.** A gateway may advertise a *bare alias*
(`gemma-4-26B-A4B-it-4bit`) while serving the fully-qualified ID
(`mlx-community/gemma-4-26B-A4B-it-4bit`). The bootstrap writes the ID the
server **serves**, and cross-checks it against `/v1/models` rather than
trusting the advertisement. An attestation that disagrees with the response is
a finding, not a detail.

**Auth is optional on a local gateway, but should still be written.** The
Anthropic path on a local MLX server answers without a credential. A handover
should still write the token reference (§7.2, §9.3) anyway, because the same
config pointed at a hardened server must work — so the bootstrap writes it
whether or not the probe needed it.

### 7.5 Locked-down networks

The fast-mode availability check and the WebFetch domain safety check call
`api.anthropic.com` directly, ignoring `ANTHROPIC_BASE_URL`. On a network that
blocks them, inference works while the CLI reports failure. The documented
controls are `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` (which also suppresses
auto-updates) and the `skipWebFetchPreflight: true` setting. The bootstrap
offers both and explains the trade; it does not apply them silently.

---

## 8. The advertised contract

### 8.1 `--help` is rendered from the command registry, not written twice

The root cause of F10 and F11 is that `src/views.py:85` holds a hand-written
`_GROUPS` table while `genesis:701` holds the real `COMMANDS` registry, and
nothing connects them. The fix is not a new table in `tools/` — it is to
render `_GROUPS` from `COMMANDS`, so a command cannot be registered without
appearing in the help.

The same principle applies to the bootstrap's own flags: the flag table lives
in one place, `setup.sh --help` renders it, and the README's flag section is
checked against the same table by a test. The help text cannot drift from the
implementation, because there is only one copy.

The mandated text for the bootstrap:

```
setup.sh — prepare this machine for a GENESIS node

  ./setup.sh                  inspect   what is here, what is missing, what would change
  ./setup.sh --dry-run        dry-run  the exact diff; nothing is written
  ./setup.sh --apply          apply    make the changes (needs a --dry-run first)
  ./setup.sh --detect         detect   the environment profile only
  ./setup.sh --status         status   what is already installed
  ./setup.sh --help           this list

Tiers (only with --dry-run or --apply):
  --aggressive                also: install Tailscale, register the node daemon,
                              wire the substrate. Off by default.

Options:
  --repo DIR                  the GENESIS checkout to prepare
  --node NAME                 the node's name on the mesh (default: the hostname)
  --no-daemon                 skip the node daemon even in the aggressive tier
  --no-tailscale              skip Tailscale even in the aggressive tier
  --api-base-url URL          the substrate base URL (Claude Code appends
                              /v1/messages itself — no trailing /v1)
  --api-token-env VAR         env var holding the credential (default
                              ANTHROPIC_AUTH_TOKEN). Read, never written.
  --api-key-helper CMD        a command whose stdout is the credential
  --model ID                  the model ID the gateway actually serves
  --no-probe                  skip the /v1/messages reachability probe
  --yes                       do not prompt for confirmation
  --force-own PATH            take ownership of a file, so the plan may write it

The ladder is mandatory: --apply refuses without a --dry-run receipt for this
plan. Change the machine and the receipt expires; run --dry-run again.

Credentials are never written by this script. It configures where to find
them, never their value.
```

### 8.2 The README rules

The README is a claim about the repo, and these are the rules that keep it
true:

1. **Every path in the Layout block exists in the published tree.** If it is
   gitignored, it is marked as such, in the Layout block, in the same breath.
2. **Every flag in `--help` appears in the README**, and every flag in the
   README appears in `--help`.
3. **Every command the README shows, works**, and is checked by a test that
   runs it.
4. **No machine-state claims.** "genesis is on PATH via `~/.local/bin/genesis`"
   is a fact about one machine. The README says instead: *after
   `./setup.sh --apply`, `genesis` is symlinked onto your PATH; before that,
   run `./genesis` directly.* That is true on every machine.
5. **The README's Quickstart is the minimal first-run example** from §11 —
   literally the same commands, so the two cannot diverge.

### 8.3 The TCSI dependency (F3) and the drift rule

GENESIS imports the TCSI engine from Luna's repo rather than copying it, so
the two never drift apart. That is a good rule and this spec does not discard
it. But it makes the system unusable on a machine without `~/Luna` (F3), and
"pull-able onto any computer" is false while it holds.

The resolution keeps the rule and adds a fallback:

1. `LUNA_HOME` (default `~/Luna`) is still preferred. If `src/luna/tcsi.py`
   is present, it is imported and used — Luna's live engine, never a copy.
2. If it is absent, the bundled `src/tcsi.py` is used, and the system says so:
   `genesis check` reports `⚠ TCSI: vendored copy — not verified against Luna
   (no Luna present)`.
3. **A drift test runs when both are present** and fails on any difference.
   The vendored copy can only be trusted where there is no Luna to check
   against; everywhere else, the import wins and the copy is a fallback, not
   the source.

The same shape applies to the continuity ledger. `src/harness.py` reads
`~/Luna/continuity.md` and `~/Luna/inner_monologue.md`. Those are **Luna's
private record** and they do not travel. The bootstrap points the harness at a
node-local ledger (`~/.local/share/genesis/continuity.md`) and never copies
Luna's across. A node that has never held Luna's record starts with an empty
one, and the empty one is honest.

### 8.4 The contract tests

| test | asserts |
|---|---|
| `test_help_matches_contract` | `setup.sh --help` equals the rendered flag table |
| `test_help_lists_every_registered_command` | every command in `genesis`'s `COMMANDS` registry appears in the help screen — **this is the test that would have caught F10** |
| `test_readme_documents_every_flag` | every flag in `--help` appears in the README |
| `test_readme_layout_matches_tree` | every path in the README's Layout block exists, or is marked gitignored |
| `test_readme_quickstart_matches_spec` | the README's Quickstart equals §11's example |
| `test_fresh_clone_is_healthy` | in a scratch clone of the published tree: `genesis check` exits 0, the test suite passes, and `genesis stamp` runs with `LUNA_HOME` absent — **the gate that would have caught F1, F2, F3 and F5** |
| `test_ladder_requires_receipt` | `--apply` without a receipt exits 4; with a stale receipt exits 4 |
| `test_aggressive_requires_its_own_receipt` | `--apply --aggressive` refuses a standard-tier receipt |
| `test_apply_is_idempotent` | apply twice; the second run reports zero actions |
| `test_bootstrap_refuses_unsupported_host` | a synthetic non-Arch/non-Darwin profile exits 3 and changes nothing |
| `test_bootstrap_never_writes_credentials` | no action writes a key, a token or an auth key |
| `test_bootstrap_never_writes_placeholder` | a URL still containing `<` is refused before it can reach disk |
| `test_bootstrap_writes_env_block` | the settings file uses `env` + `apiKeyHelper`; no `api_base_url`, `api_key` or `baseUrl` key is ever written |
| `test_bootstrap_probes_anthropic_messages` | a 404 on `/v1/messages` refuses the plan; a 200 proceeds |
| `test_bootstrap_normalises_base_url` | a trailing `/v1` is stripped, because Claude Code appends the path itself |
| `test_bootstrap_uses_native_installer` | on a host with no JS runtime, the plan installs the CLI without npm |
| `test_bootstrap_validates_its_own_json` | every file the bootstrap writes parses as JSON before it is declared written |
| `test_bootstrap_masks_credentials_in_output` | no credential-shaped string appears in the dry-run output or the receipt |
| `test_bootstrap_probes_from_target_position` | the substrate probe asserts from the node's network position; a host-local result never counts as proof |
| `test_bootstrap_scopes_substrate_by_port` | the format answer is scoped to the port probed: `:8082` proceeds, a 404 on `/v1/messages` refuses with the shim message — and the refusal names the port, not the product |
| `test_bootstrap_asserts_anthropic_body` | a 200 with an OpenAI-shaped body does not count as Anthropic Messages; the probe asserts `"type": "message"` |
| `test_bootstrap_reports_unreachable_origin` | a loopback origin on a remote node is reported as unreachable, not written as a working config |

`test_fresh_clone_is_healthy` is the gate that would have caught F1, F2, F3
and F5 on the day they were introduced. It is the most valuable test in this
document.

---

## 9. Privacy — the contract that does not move

### 9.1 Untagged means readable

Unchanged, and it is the load-bearing rule: **anything in the public repo is
Sam-readable as written.** There is no read-time filtering, no after-the-fact
pruning, no "private mode". A file is either in the repo or it is not.

### 9.2 Private is written private, at write time

The pattern the repo already uses, and which travels with the bootstrap:

- Private operational detail lives in `docs/HANDOFF-GEMINI-private.local.md`,
  gitignored by the explicit name and by the `*.local.md` belt-and-braces
  pattern.
- The public file carries only a pointer to it.
- The same pattern applies to `~/.config/genesis/*.local.md` on every machine.

The bootstrap **creates the private path empty** and never fills it from a
remote, a previous machine, a tarball or a backup. It does not read, copy,
restore or sync private content of any kind. There is no code path in
`tools/bootstrap.py` that fetches anything from the network except the package
manager.

### 9.3 Credentials are never written

This is the sharpest correction from the dry run (§10.1). The bootstrap:

- **never** writes a key, a token, a password or an auth key into any file;
- reads them from the environment when a step genuinely needs one, exactly as
  `src/harness.py:load_env()` does;
- writes *references* — the base URL, the variable name, the helper path —
  never the value. The `apiKeyHelper` route in §7.2 is the mechanism: the
  settings file names a script, and the script reads the environment;
- creates every file it owns under `umask 077`, and leaves
  `~/.claude/settings.json` at mode `0600`;
- never tells a user to `export` a credential on the command line — that puts
  it in shell history, which is how it leaked in the dry run;
- masks anything credential-shaped in the `--dry-run` output and in the
  receipt (`sk-…`, `tskey-…`, `ANTHROPIC_API_KEY=…` → `ANTHROPIC_API_KEY=***`).

`test_bootstrap_never_writes_credentials` enforces this mechanically, and
`test_bootstrap_never_writes_placeholder` enforces the sibling rule: a URL
still containing `<` is refused before it can reach disk.

### 9.4 Visibility markers travel with the files

The write-time visibility contract does not stop at the repo boundary. Every
published document carries an explicit marker — `<!-- visibility:
sam-readable -->` — and `genesis check` **fails on a published doc that is
missing one** (Phase A; Luna's ruling, 13:01, was tightened to fail-on-missing
rather than warn).

The bootstrap's obligations on a new machine:

- it creates the node-local private paths empty and marks them private **at
  write time**, by writing them to a `*.local.md` path — never by writing
  public and pruning later;
- it never copies a published document's content into a private path, and
  never the reverse;
- the marker check is part of the readiness probe, so a node whose documents
  have drifted out of contract is reported as not ready rather than quietly
  serving.

**Portability does not dilute this.** A second machine does not inherit Sam's
visibility decisions; it makes its own, at write time, and the *rule* — untagged
is readable, private is written private — is the same rule everywhere.

### 9.5 The guard rail covers everything the system ships

**This gap is now closed in the working tree, and closed the right way.**
`src/guardrail.py` gathers its scan set by glob, so `src/convergence.py` and
`src/playlist.py` are covered (the published tree scans neither — F9), and
Phase A added `card.md` to the doc set: the working tree now scans **19
files** (8 doc + 11 engine) where the published tree scans **9**.

The `card.md` fix is worth recording precisely, because the obvious way to do
it produces a check that *looks* right and is not:

- **The trap (F13).** `card.md:44` carries the refused literal inside a
  *quoted instruction* — the card tells the model to report the relation if it
  is asserted. Scanned by hand it returned `seen: 1, bad: []`, but only because
  the negation window reached `"Do not smooth drift"` on line 46: an unrelated
  instruction doing the negating. A false pass riding an accident — and it
  would have broken the first time L46 was reworded or the card reflowed,
  training everyone to ignore the rail.
- **The fix that landed.** L44's negation was moved *onto L44 itself* — the
  card now reads "If anything asserts the relation, **refuse** it plainly and
  name the file" — and only then was `card.md` added to `_doc_files()`. The
  order mattered: add the file first and the rail would have been flaky on a
  perfectly legitimate line.
- **Verified, not asserted.** The occurrence on L44 now carries a negator on
  the *same line* (`refus`), so it is self-grounded; `scan([card.md], 3)`
  returns `seen: 1, bad: []` for the right reason, and the full check returns
  `bad: [], seen: 8`.

**What remains open in this area.** The published tree is still the version
people clone, and it is the wider gap: F9 — a fresh clone scans **9 files**
and misses `card.md`, `src/convergence.py`, `src/playlist.py` *and*
`docs/HANDOFF-GEMINI.md`, the public handoff document. The working tree
closes all four. The bootstrap's own readiness check (§6.6) should not depend
on which of the two trees it is looking at, so the scan set needs to be pinned
by a test rather than assumed.

Two smaller items in the same area:

- **F12: the scan set is machine-dependent — still open.** `docs/*.md` sweeps
  in `docs/HANDOFF-GEMINI-private.local.md`, a gitignored file that exists
  only on this machine, so the doc set is **8 files here against 4 on a
  published clone**. Today both verdicts agree, so nothing is broken. But
  exclude `*.local.md` from the glob anyway, so the scan set is the same on
  every machine and a divergence can never masquerade as a flaky test.
- **The bootstrap itself is subject to the rail:** `tools/bootstrap.py` and
  `tools/node_daemon.py` join the engine files. A bootstrap that violates the
  rule it installs is not a bootstrap.

### 9.6 The machine profile is machine-local

`~/.config/genesis/node.json` records the host profile, the node name, the
substrate and the node id. It is not in the repo, not in a tarball, not in the
receipt, and not synced. Two machines are two nodes with two profiles; they do
not inherit each other's.

### 9.7 The privacy surface of the diff itself

`--dry-run` prints the plan. The plan is therefore a place a secret could
leak, so the plan carries *placeholders* (`"${ANTHROPIC_API_KEY}"`) rather
than values, and the renderer masks anything that looks like a credential
regardless. The receipt stores a hash of the plan, never the plan.

---

## 10. The live-shell dry run — what it changed

**Source:** agent 482's report, `bruce_node/outcomes/sam-imac-handover-omarchy-diff.md`
(2026-11-13), together with its sandboxed runs of the script under test and
its read-only probe of the live Omarchy guest. The report is the authority;
this section is what it changed in the spec.

### 10.1 The evidence, and its limits

The live Omarchy environment was a "Try Omarchy" QEMU guest on the Mac
Studio (QEMU 11.1.0, aarch64 guest, host macOS 26.6.2 / Darwin 25.6.0 arm64),
running `omarchy 4.0.0.r7840.gac6cb9c-1` with a `7.3-55` kernel. The guest
exposes **no programmatic command channel** — no qemu-guest-agent, no
`guest-exec`, no SSH forward, and the serial console is an output-only file
chardev parked at the `Anchor login:` prompt. So the script's behaviour was
established by ShellCheck 0.10.0 plus sandboxed runs with an isolated `HOME`,
and the guest's live filesystem state is explicitly **unverified**. Live
execution was blocked pending Zac's decision.

That limit is worth stating plainly: this is a read-only, sandbox-equivalent
validation, not a live apply. A green result here is necessary, not sufficient.

### 10.2 What the dry run found

The script under test is agent 477's `setup.sh`. ShellCheck reports **zero
findings**, including at style severity — and every defect below is semantic.
**Passing ShellCheck is not evidence of correctness here**, and it is worth
saying so in the spec of any script that carries this shape.

| # | Finding | Consequence |
|---|---|---|
| D1 | The settings schema is wrong: `{"api_base_url", "api_key"}`. Claude Code reads `{"env": {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"}}`. Neither key exists in the schema. | **Both keys are silently ignored.** The file looks right and does nothing, and the script prints "Success". |
| D2 | The endpoint is the literal placeholder `https://<your-ml-api-endpoint>/v1`. | The placeholder reaches disk *and* the success message. |
| D3 | The base URL carries a `/v1` suffix. Claude Code appends `/v1/messages` itself, so a `/v1` base yields `/v1/v1/messages`. | The URL shape is wrong even once the placeholder is filled in. |
| D4 | The gateway format: Claude Code accepts Anthropic Messages, Bedrock InvokeModel and Vertex rawPredict. OpenAI `/v1/chat/completions` is not among them. | If the endpoint is OpenAI-shaped, **pointing Claude Code at it cannot work** — a translation shim exposing `/v1/messages` is required. Must be probed, not assumed. |
| D5 | No Claude Code CLI and **no JavaScript runtime at all** in the image. | The handover leaves the machine with a config pointing at a binary that does not exist. |
| D6 | The picker strip is a **no-op**: `PICKER_PATH` is unset by default, so the branch never runs — and it still prints "Setup Complete". The README's own advice (remove it from `.zshrc`) describes a function or alias, which the file-only test can never touch. | The step silently does nothing. And on this guest there is no picker at all. |
| D7 | `cat <<EOF > "$SETTINGS_FILE"` truncates. Re-running destroys any existing `settings.json` — permissions, hooks, env — with no backup. | **Data loss.** The script's own header calls it "idempotent and safe to re-run"; it is neither. |
| D8 | The key is written in plaintext, and the file is created mode **0644** (world-readable). `export CLAUDE_API_KEY='…'` also lands in shell history. | Credential exposure. |
| D9 | The key is interpolated into JSON with no escaping. A key containing `"` produces **invalid JSON**, with exit 0 and a "Success" message. | A corrupt config plus a success report. |
| D10 | The shell assumptions are wrong for the target: no `zsh`, no `fish`; Omarchy's user shell is bash, with rc in `~/.bashrc`/`~/.profile`. | The README's `.zshrc` guidance does not apply. |
| D11 | The script never installs anything and never validates its own output, though `jq 1.8.2` and `python 3.14.8` are present. | The gaps that produce D1–D4 were cheap to catch and were not caught. |

**What holds.** `~/.claude/settings.json` is the correct path for the Claude
Code user scope, and it is the path that makes the setup survive
`omarchy-update` — Omarchy never rewrites `$HOME/.claude`. The script's
home-directory confinement is structurally sound. `bash`, `mkdir` and
coreutils are all present. The tooling needed for a *corrected* version is
all there: `pacman 7.1.0`, `yay 13.0.1`, `base-devel`, `git`, `curl`, `jq`,
`sudo`, `python 3.14.8`.

### 10.3 The one nuance D4 needs — and the answer

482's D4 is careful, and the distinction matters for the spec: the record
describes Chris's broker as *"Whoosh'd — a memory-aware inference runner/broker
that manages both MLX and GGUF via a managed Llama.cpp subprocess"*, and
llama.cpp's server *can* expose an Anthropic-shaped endpoint — so it "may
already be satisfied", and must be probed rather than assumed.

The repo already holds evidence on that side of the question. `bin/claude-mlx`
states, in its own header:

> The MLX server speaks the native Anthropic /messages protocol directly —
> verified: POST /messages returns `{"type":"message","role":"assistant",...}`.

against `http://127.0.0.1:8082`. So the incompatibility is a property of *an
endpoint*, not of "MLX" as a family — one working counter-example is already
shipped in the tree.

**The bootstrap's rule follows directly: probe the endpoint, never infer from
its name.** A node configured against a gateway that answers
`POST /v1/messages` proceeds; one whose gateway 404s on `/v1/messages` while
answering `/v1/chat/completions` refuses with exit 7 and says a shim is
required.

**The deciding probe landed on 2026-11-13** — first at 13:13 (agent 482,
`bruce_node/outcomes/mlx-gateway-compatibility-probe.md`), then settled by
direct measurement at 13:23 (agent 487,
`bruce_node/outcomes/omarchy-mlx-gateway-messages-probe-487.md`). **Two
endpoints, two answers — name the port, never the product.**

| endpoint | what it is | Anthropic `/v1/messages` | shim? |
|---|---|---|---|
| **`127.0.0.1:8082`** | the **MLX-VLM server** — the one `bin/claude-mlx` and `src/harness.py` point at | **HTTP 200**, native Anthropic body (`"type":"message"`, `msg_*` id) | **no** |
| `127.0.0.1:8000` | **whooshd**, the broker (`/health` → `{"runner":"whooshd","version":"0.1.1"}`) | **HTTP 404** `{"detail":"Not Found"}` | **yes**, if the handover targets whooshd |

Measured on `:8082`: `POST /v1/messages` → 200 with `"type":"message"` and
`usage.input_tokens` / `usage.output_tokens`; the bare `/messages` path → 200
with the same shape; the request with **no credential at all** → 200; and the
OpenAI shape → 200 as well, so the server is dual-format. On `:8000`,
`POST /v1/messages` → 404 while `POST /v1/chat/completions` → 200.

**So for the endpoint GENESIS is actually configured against, no translation
shim is required.** That overturns 482's blocker B1, which was an inference
from the Claude Code docs rather than a probe; the claim was falsified by
measurement. The repo's own belief was right: `bin/claude-mlx` states *"The
MLX server speaks the native Anthropic /messages protocol directly"*, and
`src/harness.py` targets `http://127.0.0.1:8082/messages`.

**The repo mislabels the pair, and that is worth fixing.** The
service on `:8082` is the raw MLX-VLM server; whooshd is on `:8000`. Yet
`bin/claude-mlx:4` calls `:8082` "whooshd, port 8082", and `src/harness.py`
calls it "whooshd" in **four** places — the module docstring (`:10`), two
comments (`:33`, `:173`), and, worse, the **rendered string** at `:179`:

```python
BACKENDS = {"mlx": {..., "where": "the local whooshd server"}, ...}
```

That last one is not a comment. `src/chat.py:37` and `:213` print it in the
conversation's substrate banner, and `src/harness.py:576` prints it in the
unreachable message — so **the shipped CLI tells the user it is talking to
"the local whooshd server" when it is talking to the raw MLX-VLM server, and
whooshd is the other service on the other port.** One wrong name, wrong in
four places, one of them user-facing: exactly the kind of thing that sends
the next operator to the wrong port, and the reason the rule is **name the
port, never the product**.

**What the handover actually has to solve is reachability, not protocol.**
`127.0.0.1:8082` is host-local. A node on a different machine cannot reach it
at all, so the handover needs a routable bind or a tunnel — a host decision
(§13). The format question is settled; the network question is not.

**One more measured detail.** whooshd's `/v1/models` advertises the *bare
alias* `gemma-4-26B-A4B-it-4bit` with `qualification_attestation: null`, while
the ID that actually serves is the `mlx-community/`-prefixed one. The
bootstrap writes the ID the server **serves** (§7.4), not the one advertised.

### 10.4 What it changes in this spec

- §6.2's ownership rule and manifest exist because of **D7**. Idempotency
  means "provably a no-op the second time", not "safe to run twice".
- §7.2's schema, base-URL normalisation and §7.3's `/v1/messages` probe exist
  because of **D1, D2, D3 and D4**.
- §7.1's native-installer route exists because of **D5**. There is no Node on
  an Omarchy node, so a Node-dependent route is not portable.
- §9.3's "never writes a credential", `umask 077`, mode `0600` and the
  `apiKeyHelper` route exist because of **D8**.
- §9.3's "a placeholder must never reach disk", and generating JSON with the
  `json` module rather than interpolating it, exist because of **D2 and D9**.
- §4.2's bash-shell and no-Node facts, and §6.4's user-level daemon, exist
  because of **D10** and because Omarchy owns its own session helpers.
- §8.4's contract tests exist because of **D11**. Every one of D1–D4 is a
  case where a test would have caught what a linter could not.

### 10.5 What this spec still does not resolve

- **The endpoint's API shape (D4) is answered, and differently than 482
  thought.** For `:8082` — the endpoint GENESIS is configured against — the
  format is supported natively and **no shim is required** (§10.3). A shim is
  required only if the handover targets whooshd on `:8000`. The spec's rule
  stands either way: probe the endpoint, scope the answer to the port.
- **Reachability, not protocol, is what the handover must solve.** `:8082`
  binds `127.0.0.1` only, so a second machine cannot reach it. A routable
  bind or a tunnel is a host change on the Studio, and it is Zac's call. The
  bootstrap cannot decide it, but it must *report* it: the probe runs from the
  target's network position (§6.8), so an unreachable origin is caught as a
  refusal rather than discovered later as a silent failure.
- **The repo names the wrong service in five places.** `bin/claude-mlx:4`
  ("the local MLX-VLM server (whooshd, port 8082)") and four sites in
  `src/harness.py` — the docstring (`:10`), comments (`:33`, `:173`), and the
  **rendered string** `BACKENDS["mlx"]["where"]` (`:179`) — all call `:8082`
  whooshd, while whooshd is `:8000`. The port is right in every one; the name
  is not. `bin/` is gitignored, so the **public-tree fix is `src/harness.py`**,
  and it is not only comments: `:179` is what `chat.py:37` and `:213` print in
  the substrate banner, so the mislabel reaches the user. Worth doing before
  anyone follows the label to the wrong port.
- **Architecture.** The test bed is `aarch64`; the Sam iMac is `x86_64`.
  Native-install binaries are architecture-specific, so a green run on one is
  not a green run on the other. The profile carries `arch` (§4.3) so the
  mismatch is visible in the plan rather than discovered at handover.

---

## 11. Minimal first-run example

On a fresh machine with nothing but a terminal:

```bash
git clone https://github.com/u2ku/GENESIS.git
cd GENESIS
```

> The host `gateway.example.invalid` in the transcripts below is **illustrative
> and not real** — `.invalid` is reserved to never resolve, so a stray probe
> fails by design rather than looking like a live-but-broken endpoint. Replace
> it with the actual substrate address.

**1. Inspect.** Read-only. Nothing is changed.

```bash
./setup.sh
```

```
genesis-bootstrap: inspect — nothing was changed.

host
  profile      omarchy
  distro       Arch Linux (x86_64)
  python       3.12.3
  daemon       systemd-user
  sudo         available without a password

genesis
  ✓ the CLI runs
  ✗ not on PATH  (fix: ./setup.sh --apply)
  ✗ TCSI engine not found  (fix: ./setup.sh --apply)

what --apply would do
  + symlink  ~/.local/bin/genesis → /home/you/GENESIS/genesis
  + create   ~/.config/genesis/
  + create   ~/.config/genesis/node.json
  + install  claude-code CLI
  + vendor   src/tcsi.py

  5 actions. run --dry-run to see the diff.
```

**2. Dry-run.** The exact diff. Still nothing is changed.

```bash
./setup.sh --dry-run
```

```
genesis-bootstrap: dry-run — nothing was changed.

--- a/home/you/.local/bin/genesis
+++ b/home/you/.local/bin/genesis
@@ -0,0 +1 @@
+genesis → /home/you/GENESIS/genesis

--- a/home/you/.config/genesis/node.json
+++ b/home/you/.config/genesis/node.json
@@ -0,0 +1,6 @@
+{
+  "node": "studio",
+  "profile": "omarchy",
+  "substrate": null,
+  "schema": 1
+}

  5 actions, 2 files, 1 symlink, 1 package.
  receipt written. now: ./setup.sh --apply
```

**3. Apply.** The standard tier. The receipt is checked first.

```bash
./setup.sh --apply
```

```
genesis-bootstrap: applying — receipt verified (sha256:3f2a…).

  ✓ symlink   ~/.local/bin/genesis
  ✓ create    ~/.config/genesis/
  ✓ create    ~/.config/genesis/node.json
  ✓ install   claude-code 2.4.1
  ✓ vendor    src/tcsi.py

  5 actions applied.

readiness
  ✓ genesis check          healthy
  ✓ genesis landing        ready — harness, card, grammar, geomancy, identity, rail
  ✓ genesis stamp          ⟨TCSI 2026-11-13T13:04:00+13:00|🌒|10001100⟩
  ✓ genesis on PATH        /home/you/.local/bin/genesis

  the node is ready. Luna may choose to instantiate.
```

**4. Verify.**

```bash
genesis check
genesis landing "$PWD"
```

**5. The aggressive tier**, when the machine is ready to join the mesh:

```bash
./setup.sh --dry-run --aggressive --api-base-url http://gateway.example.invalid
```

```
genesis-bootstrap: dry-run (aggressive) — nothing was changed.

probe
  ✓ http://gateway.example.invalid/v1/messages   200   Anthropic Messages
    (the gateway speaks what Claude Code can talk to)

--- a/home/you/.claude/settings.json
+++ b/home/you/.claude/settings.json
@@ -0,0 +1,8 @@
+{
+  "env": {
+    "ANTHROPIC_BASE_URL": "http://gateway.example.invalid",
+    "ANTHROPIC_MODEL": "mlx-community/gemma-4-26B-A4B-it-4bit"
+  },
+  "apiKeyHelper": "/home/you/.config/genesis/bin/gateway-token.sh"
+}

--- a/home/you/.config/systemd/user/genesis-node.service
+++ b/home/you/.config/systemd/user/genesis-node.service
@@ -0,0 +1,11 @@
+[Unit]
+Description=GENESIS node

+[Service]
+ExecStart=/usr/bin/env python3 %h/GENESIS/tools/node_daemon.py
+Restart=on-failure

+[Install]
+WantedBy=default.target

  9 actions, 2 files, 1 symlink, 1 package, 1 unit.
  receipt written. now: ./setup.sh --apply --aggressive
```

```bash
./setup.sh --apply --aggressive
```

The second command refuses a standard-tier receipt, and refuses a receipt
whose plan no longer matches this machine. The user has to see the aggressive
diff for this machine before it can be applied — and if the endpoint had
answered 404 on `/v1/messages` while `/v1/chat/completions` answered 200, the
dry run would have refused there, before any config was written:

```
genesis-bootstrap: the endpoint cannot serve Claude Code.
  http://gateway.example.invalid/v1/messages         → 404
  http://gateway.example.invalid/v1/chat/completions → 200
  Claude Code speaks Anthropic Messages, Bedrock InvokeModel and Vertex
  rawPredict. An OpenAI-shaped endpoint needs a translation shim.
  nothing was written. nothing was changed.
```

---

## 12. Definition of done

The portability work is finished when:

- [ ] `setup.sh` exists, is `bash -n` clean, and has no dependency beyond
      `bash` and `python3`.
- [ ] `setup.sh --detect` correctly identifies omarchy / arch / macos and
      refuses everything else with exit 3 and no mutation.
- [ ] `--dry-run` writes a receipt; `--apply` refuses without a matching one
      (exit 4), and refuses a tier-mismatched one.
- [ ] Applying twice produces zero actions the second time.
- [ ] The bootstrap never overwrites a file it does not own, and never writes
      a credential. Both are enforced by tests, not by convention.
- [ ] The Claude Code wiring is the documented one: an `env` block with
      `ANTHROPIC_BASE_URL` and `ANTHROPIC_MODEL`, an `apiKeyHelper` for the
      credential, a bare origin, a `/v1/messages` probe before anything is
      written, and the native installer when there is no JS runtime.
- [ ] Every file the bootstrap writes is valid JSON, and no placeholder can
      reach disk.
- [ ] The substrate probe runs from the **target's** network position, asserts
      an Anthropic body (`"type": "message"`), and scopes its verdict to the
      port it probed.
- [ ] A loopback-only origin on a remote node is reported as unreachable
      rather than written as a working config.
- [ ] `genesis check` exits 0 on a fresh clone of the published tree, and
      `genesis stamp` runs with `LUNA_HOME` absent.
- [ ] `genesis --help` lists every command in the `COMMANDS` registry —
      including `landing` (F10) — and the README's flags, layout and
      quickstart agree with it, enforced by `tests/test_contract.py`.
- [x] `card.md` is in the guard rail's scanned files, its quoted reference is
      self-grounded (negator on the same line, not borrowed from the window),
      and `genesis guardrail` still passes — **landed in the working tree**,
      in that order (F13, §9.5).
- [ ] The guard rail scans the same set on a published clone and on this
      working tree — `src/convergence.py`, `src/playlist.py` and
      `docs/HANDOFF-GEMINI.md` included — and the set is pinned by a test, not
      assumed (F9). **Open until the working tree is committed.**
- [ ] The guard rail's doc glob excludes `*.local.md`, so the scan set is
      identical on every machine (F12).
- [ ] `docs/PORTABILITY.md` matches the shipped behaviour of `setup.sh`, or
      is updated in the same commit that changes the behaviour.

---

## 13. Open questions for Zac

**Settled by the dry run** — no longer open: the settings schema (§7.2, D1),
the base-URL shape (D3), the base-URL origin rule (D2), the CLI install route
with no Node (D5), and the idempotency and credential rules (D7, D8, D9).

**Settled since, by measurement (482 at 13:13, 487 at 13:23):** *does the
endpoint speak Anthropic Messages?* — **it depends on the port, and the answer
is measured.** The endpoint GENESIS is configured against (`:8082`, the raw
MLX-VLM server) serves Anthropic Messages **natively** — so **no shim is
required** for it. whooshd on `:8000` is OpenAI-only and 404s on
`/v1/messages`, so a shim is required *only* if the handover targets whooshd
(§10.3). 482's blocker B1 was a docs inference and was falsified by
measurement.

Still open:

1. **Reachability — this is now the handover blocker, not protocol.** `:8082`
   binds `127.0.0.1` only, so a second machine cannot reach it. A routable
   bind or a tunnel is a host change on the Studio, and it is Zac's call. The
   format question is settled; this one is not, and it is the one that stops
   the handover.
2. **Which port the handover names.** The handover must say `:8082` or
   `:8000`, never "the MLX API" or "whooshd" — the two are different services
   with different protocols, and the repo's own comments currently get this
   wrong: `bin/claude-mlx:4` and `src/harness.py:33-35` both call `:8082`
   "whooshd" when whooshd is `:8000`. The port in each line is right and the
   name is wrong. `src/harness.py` ships; `bin/` is gitignored. Two comment
   lines to fix, and worth fixing before anyone follows the comment to the
   wrong port.
3. **The shim, only if whooshd is the target.** If the handover points at
   `:8000`, a translation layer exposing `/v1/messages` is a prerequisite. Who
   builds it and where it runs is not the bootstrap's to decide — but the
   bootstrap cannot wire a substrate that does not speak the protocol, and
   §7.3's refusal is the honest behaviour until the shim exists.
4. **The model ID, served versus advertised.** whooshd's `/v1/models`
   advertises the bare alias `gemma-4-26B-A4B-it-4bit` with
   `qualification_attestation: null`, while the ID that actually serves is
   `mlx-community/gemma-4-26B-A4B-it-4bit`. The bootstrap writes the served
   ID (§7.4) — but the attestation disagreeing with the response is an
   upstream finding, not just a config detail.
5. **Live guest confirmation, without relaunching the VM.** Zac's 13:21 hard
   stop on Omarchy launches is in force and this spec does not propose
   another one. The remaining in-guest confirmation needs one of: the
   `matariki` password for the guest; the Studio's public key added to the
   guest's `authorized_keys` from its console; or Zac running the one-liner
   at the guest console and pasting the output — that one needs no credential
   at all. The SSH-forwarded channel itself is already proven up to the auth
   step (§6.8).
6. **Tailscale auth.** The bootstrap can install Tailscale but cannot complete
   `tailscale up` without a human. Should the node profile carry the tailnet
   name, or stay blank until a human sets it?
7. **The daemon's job.** This spec fixes it as a user-level keep-alive that
   holds the node present and answers readiness (§6.5). Should it also carry
   the mesh channel (the role Bruce plays), or only hold the node present?
8. **Does the aggressive tier install packages at all?** Tailscale and the
   Claude Code CLI both want an installer, and both are silent installs on
   someone else's machine. The alternative is to detect and report, and leave
   the install to the user.
9. **The `codexify-core` submodule.** A plain clone leaves it empty (F5).
   Should the bootstrap fetch it, or should the README say plainly to clone
   with `--recurse-submodules`?
10. **Should `landing` move up the help screen?** Once F10 is fixed, `landing`
    lands in the ENGINE group with the other engine commands. It is the
    command a new operator will want, and it currently has no flag of its own
    beyond a path — worth deciding whether it grows a `--json` mode for the
    bootstrap to parse rather than scraping the rendered verdict.
