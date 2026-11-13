# WIZARD — one key, every system
<!-- visibility: sam-readable -->

**Status: specification. Nothing here is built.** This document describes a
tool that does not exist yet. The repo it describes is real, the ladder it
extends is real (`docs/PORTABILITY.md`), and the shim it wires is real
(`src/shim.py`). The wizard is the missing front door.

The requirement is Zac's, and it is a hard one:

> One API key entered once in an onboarding wizard must configure every
> onboarded system — Codexify, genesis chat, and Claude Code behind the
> Anthropic→OpenAI shim.

This document is what that means, precisely enough to build and to test.

**Two things this document does not do.** It does not change
`docs/PORTABILITY.md` — it extends it, and §11 lists the amendment exactly, so
the two can be merged without a diff-by-memory. And it does not propose a
machine launch, a guest exec, a commit or a push. The VM stop stands; the
push gate stands.

---

## 1. The requirement, stated exactly

### 1.1 One prompt, three consumers

A run of the wizard contains **exactly one credential prompt**. The value
entered there reaches every onboarded system. No consumer asks a second
question, and no consumer requires the operator to paste the key into three
different files.

| consumer | what it is | how it gets the key | where the value sits |
|---|---|---|---|
| **genesis chat** | `src/harness.py`, the `openrouter` backend | `harness.load_env()` reads `<repo>/.env` at request time — already wired | `<repo>/.env` |
| **Codexify** | the `codexify-core` install | its own `.env`, `OPENAI_API_KEY` — its native mechanism | `<codexify-root>/.env` |
| **Claude Code** | the Anthropic CLI | `apiKeyHelper` → a script that reads `<repo>/.env` | nowhere else |

The **shim** (`src/shim.py`) is the Anthropic↔OpenAI translation layer. When
the gateway is OpenAI-shaped it sits between Claude Code and the gateway, and
its process reads the same `<repo>/.env`. The shim never sees a key that the
wizard has not already stored once.

The wizard is the only component that ever holds the value. Every other
component holds a *reference* — a path, a variable name, a helper path.

### 1.2 What "entered once" means — and what it does not

It means:

- one prompt, per machine, per key;
- the value is never typed again to configure a second system;
- re-running the wizard on a configured machine **does not prompt again** — it
  shows the key's fingerprint (§2.3) and asks *keep, replace, or remove*. That
  is a choice, not a re-entry. Only "replace" prompts, and then once.

It does not mean:

- the value is typed on a command line — it is not, because a command line is
  shell history;
- the value is echoed — the prompt reads without echo;
- the value lives in one file because the applications happen to agree. Each
  application reads its own credential file natively, so the value lands in
  one file per application that has a native place for it. The rule is
  **gitignored and mode 0600, never tracked, never logged, never in a
  receipt** — not "exactly one file on disk". §2.2 states the rule as it is.

### 1.3 The topology

```
                 ┌──────────────────────────────────────────┐
                 │  the wizard  (one prompt, one key)       │
                 └───────────────┬──────────────────────────┘
                                 │ writes references; the value once
        ┌────────────────────────┼──────────────────────────────┐
        │                        │                              │
        v                        v                              v
 <repo>/.env             <codexify>/.env              ~/.config/genesis/bin/
 (gitignored, 0600)      (gitignored, 0600)           gateway-token.sh
        │                        │                       (reads <repo>/.env)
        │                        │                              │
        v                        v                              v
  genesis chat            Codexify ──▶ gateway          Claude Code
   (direct)                (OpenAI shape)                  │
                                                             v
                                                   shim  (src/shim.py)
                                                   127.0.0.1:8790
                                                             │
                                                             v
                                                       the gateway
```

The gateway is one of two shapes, and the wizard **probes** rather than
assumes (`docs/PORTABILITY.md` §7.3, §13: *name the port, never the
product*):

- **Anthropic-shaped** — serves `/v1/messages`. Claude Code points straight at
  it. No shim in the path.
- **OpenAI-shaped** — serves `/v1/chat/completions` and answers 404 on
  `/v1/messages`. The shim is **required** and the wizard installs it as a
  user service. This is the case the requirement names.

Which one the onboarding target is, is an open question for Zac (§12.1). The
wizard does not need the answer in advance — it detects the shape at run time
and says which topology it found.

---

## 2. Where the key lives

### 2.1 One store per application, one writer

- `<repo>/.env` — the GENESIS repo's own credential file. Already gitignored
  (`.gitignore`), already read by `src/harness.py:load_env()`. The wizard
  creates it if absent, mode **0600**, and records it in the manifest.
- `<codexify-root>/.env` — Codexify's own credential file. Codexify's
  repository ignores `.env` itself, so this is outside the published tree by
  construction. The wizard writes `OPENAI_API_KEY` there — the variable
  Codexify's compose files and services already read.
- `~/.config/genesis/bin/gateway-token.sh` — the helper Claude Code calls.
  It contains a **path**, not a value: it reads the key out of `<repo>/.env`
  and prints it. Mode **0700**, owned by the wizard.

### 2.2 The rules

The wizard writes the value into a file only when all five hold:

1. the file is **gitignored** — verified with `git check-ignore`, not assumed;
2. the file is mode **0600** (0700 for the helper script);
3. the file is **not tracked** — the wizard refuses to write into a tracked
   path, whatever the variable is called;
4. the value never appears in a **command line, an argument, an environment
   dump, a log, a receipt, a manifest entry, or the diff**;
5. every output the wizard produces is **masked** — anything credential-shaped
   is replaced before it is printed (`docs/PORTABILITY.md` §9.3).

Rule 4 has one exception, and it is deliberate: the value is in the process
environment of the shim and of the Codexify services, because that is how
those programs read credentials. The environment is not a file, and it is not
persisted by the wizard.

### 2.3 The fingerprint

```
fingerprint = sha256(key).hexdigest()[:12]
```

A one-way digest of the value. It is not reversible, and it is not a secret.
The wizard uses it for three things:

- **detecting an already-configured machine** without prompting, and without
  displaying anything;
- **binding the plan to the key** — the fingerprint is part of the plan hash,
  so a key changed between the dry run and the apply expires the receipt
  (§6.3);
- **letting the operator confirm which key is installed** — the wizard can
  say "key `a1b2c3d4e5f6` is configured" without ever showing the key.

The fingerprint is recorded in the receipt and in the manifest. It is not
recorded in any published file.

---

## 3. The flow

The wizard is a **front end for the existing ladder** (`docs/PORTABILITY.md`
§5), not a second ladder. It adds one thing the ladder does not have: a
credential to collect, and a verification to perform.

| rung | command | mutates the host? | writes the repo? |
|---|---|---|---|
| **inspect** | `./setup.sh --wizard` | no | no |
| **dry-run** | `./setup.sh --wizard --dry-run` | no | yes — a receipt, and nothing else |
| **apply** | `./setup.sh --wizard --apply` | yes | yes — the receipt and the manifest |

`--wizard` is a **mode**, not a tier. It composes with `--aggressive` exactly
as the tier rules describe: `--wizard --aggressive` alone is a usage error
(exit 2); `--wizard --aggressive --apply` requires a receipt written with
`--aggressive`.

### 3.1 Step 0 — detect

Environment detection runs first and reads only the host
(`docs/PORTABILITY.md` §4). The profile is what decides the install route,
the daemon kind and the shell, and it is **never assumed**.

The two facts the wizard's own plan depends on:

- **`js_runtime`** — `null` on the measured Omarchy guest. The Claude Code
  CLI is then installed with the **native installer** (`docs/PORTABILITY.md`
  §7.1), which needs `curl` and a writable `~/.local/bin`. `npm` is used only
  if a JS runtime is already present *and* the operator asks for it. The
  wizard never assumes Node, and never fails because Node is missing.
- **`shell`** — `bash` on the guest, `zsh` on macOS. Every script the wizard
  writes is POSIX `sh`, and the rc file it touches is chosen by the profile.

### 3.2 Step 1 — the one prompt

```
genesis-wizard: the API key for this node.
  it is written once, to gitignored files only, and never displayed again.
  (press enter to keep the key already configured)
>
```

- read with **echo off** (`getpass`-equivalent; `stty -echo` on a TTY, never
  a pipe);
- the value is held **in memory only** for the duration of the run;
- on a machine that already has a key, the prompt offers to keep it and the
  operator never has to type it again;
- **no `--key` flag.** There is no way to pass a credential as an argument,
  because arguments are visible in the process table and in shell history.
  Non-interactive use reads the key from a file path (`--key-file PATH`), and
  the path is a path.

### 3.3 Step 2 — verify the key

**This is the step that does not exist anywhere else, and it is the one that
kills the failure the dry run found.** 482's finding: a script can write a
placeholder key, report success, and leave a machine that cannot talk to
anything. A wizard that only checks "the string was written" inherits that
bug exactly.

So the wizard **authenticates the key against the gateway before it plans
anything**. If the key does not authenticate, the wizard writes nothing and
exits **8**.

The verification is a real request, carrying the real credential, expecting a
real answer. §5 gives the exact probes.

### 3.4 Step 3 — the plan

Only after the key has authenticated does the wizard compute the plan. The
plan is a set of declarative actions (`docs/PORTABILITY.md` §6.1) — desired
state, not commands — and it covers, per consumer:

- the credential file(s) and their mode;
- the helper script and its path;
- the Claude Code `settings.json` `env` block;
- the shim service, when the gateway is OpenAI-shaped;
- the Codexify credential file.

The plan renders with **placeholders**, never values
(`docs/PORTABILITY.md` §9.7): the diff shows
`OPENROUTER_API_KEY=${OPENROUTER_API_KEY}`, not the key.

### 3.5 Step 4 — dry-run and the receipt

`--dry-run` renders the unified diff and writes the receipt
(`docs/PORTABILITY.md` §5.3). The receipt holds hashes and counts — never a
value, never a path that identifies a person.

The receipt additionally holds the **key fingerprint**, so the receipt is
bound to the key that was verified. Change the key, and the receipt expires
exactly as a changed machine does.

### 3.6 Step 5 — apply

`--apply` refuses without a matching receipt (exit 4), refuses a
tier-mismatched receipt, and refuses a receipt whose key fingerprint no longer
matches. Then it applies the actions through the **ownership rule**
(`docs/PORTABILITY.md` §6.2): it writes only files it owns, and a file it
does not own and did not create is a **conflict** (exit 6), not an overwrite.

Every file is created under `umask 077`.

### 3.7 Step 6 — readiness

The apply ends with the readiness probe (`docs/PORTABILITY.md` §6.6), and the
wizard's version of it is per consumer: **each of the three systems is
exercised with the configured key**, and each reports what actually happened.

| probe | what it proves |
|---|---|
| `genesis check` | the engine loads and the rail is green |
| a live call through the `openrouter` backend | **genesis chat** authenticates with the key |
| the gateway's own endpoint, with `OPENAI_API_KEY` from the Codexify `.env` | **Codexify** authenticates with the key |
| `POST` to the shim's `/v1/messages`, asserting `"type": "message"` | **Claude Code** reaches a gateway that speaks Anthropic Messages |
| the helper script's stdout length and fingerprint | the credential Claude Code would read is the one that was verified |

A consumer that fails is reported as failing. The wizard does not print
"ready" because it got most of the way — it prints the table, and exits 1 if
any consumer failed.

---

## 4. The three consumers, wired

### 4.1 genesis chat

`src/harness.py` already reads `<repo>/.env` at request time and already
declares the `openrouter` backend. The wizard's work here is small and
specific:

- ensure `<repo>/.env` exists, holds the key, and is mode 0600;
- ensure the backend the operator wants is the one that gets used — the
  default in the CLI is `mlx` (the local server), and a node configured
  against a remote provider needs `genesis chat --backend openrouter`. The
  wizard records the chosen backend in `~/.config/genesis/node.json` and
  prints the exact command. *(Amended behaviour, §11.4: the default should
  come from the node profile when one exists.)*
- **verify** — one live call through that backend.

Nothing else. `genesis chat` needs no new code path for the key.

### 4.2 Codexify

Codexify reads its credentials from its own `.env`, and its compose files and
services read `OPENAI_API_KEY` from it. The wizard:

- locates the Codexify installation (`--codexify DIR`, defaulting to a
  detected path, and the `codexify-core/` submodule in the repo if that is
  what is here);
- writes `OPENAI_API_KEY` into `<codexify-root>/.env`, creating the file from
  the template if it is absent, mode 0600;
- **does not touch `GUARDIAN_API_KEY`.** That is Codexify's own auth-boundary
  secret, not an LLM credential. The wizard does not own it and does not
  write it — a wizard that overwrites an application's auth secret is a
  different kind of dangerous.
- **does not touch anything else in that file.** The `.env` is a shared file
  with a dozen variables; the wizard writes its own line and leaves the rest
  byte-identical. It edits one line; it does not regenerate the file.
- if the Codexify installation cannot be found, the target is recorded as a
  **manual step** with the exact command (`docs/PORTABILITY.md` §6.5) — not
  skipped silently, and not a failure of the wizard.

### 4.3 Claude Code

The wiring is the documented one (`docs/PORTABILITY.md` §7.2), and it is
worth restating because the obvious alternative does nothing:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8790",
    "ANTHROPIC_MODEL": "the-model-id-the-gateway-serves"
  },
  "apiKeyHelper": "/home/you/.config/genesis/bin/gateway-token.sh"
}
```

- **there is no `api_base_url`, `api_key` or `baseUrl` key** — Claude Code
  ignores unknown top-level keys, so a file written with them looks right and
  does nothing at all;
- the `env` block carries the **base URL and the model ID** — references, not
  credentials;
- the `apiKeyHelper` is a **path**, and the script it points at reads the
  credential from `<repo>/.env` and prints it. The key is never in
  `settings.json`;
- the base URL is **everything before `/v1/messages`** — Claude Code appends
  that path itself. A trailing `/v1` is normalised away, and a URL still
  containing `<` is refused before it can reach disk.

The wizard writes `~/.claude/settings.json` **as a merge**, not as a
replacement: it reads the existing file, replaces only the keys it owns,
preserves everything else, and records the resulting hash in the manifest. A
wizard that overwrites a person's Claude Code settings wholesale is the same
defect the dry run found in `setup.sh` (`docs/PORTABILITY.md` §6.2), and it
would be worse here, because the wizard has just been given a credential.

### 4.4 The shim

`src/shim.py` is the Anthropic↔OpenAI translation layer, already written, and
it is **required** whenever the gateway is OpenAI-shaped. The wizard's job is
to put it in the path and keep it running:

- a **user** service, never a system one — a `systemd --user` unit on Linux, a
  `LaunchAgent` on macOS. No `sudo`, and it survives `omarchy-update`;
- the service sources `<repo>/.env` and execs the shim, so the credential
  reaches the process the way the harness reaches it;
- it binds `127.0.0.1` only, on a configurable port (default 8790), and the
  wizard says so when it reports the port;
- the model ID it serves is the one the gateway **serves**, cross-checked
  against the gateway's own model list rather than its advertisement
  (`docs/PORTABILITY.md` §7.4).

If the gateway is Anthropic-shaped, the shim is not installed and the wizard
says why: *the gateway speaks Anthropic Messages natively; no translation
layer is needed.*

---

## 5. The key verification, in detail

### 5.1 What counts as authenticated

A key is authenticated when **the gateway accepts it and returns a real
answer**. Three conditions, all of them:

1. the request carries the credential;
2. the response status is 200;
3. the response body carries the shape that proves a model answered — an
   Anthropic `"type": "message"`, or an OpenAI `"choices"` array.

### 5.2 The probes

**OpenAI-shaped gateway** — `POST {base}/chat/completions`:

```bash
curl -sS -m 20 -o /tmp/wizard-probe.json -w '%{http_code}' \
  -X POST "$BASE/chat/completions" \
  -H "authorization: Bearer $KEY" \
  -H 'content-type: application/json' \
  -d '{"model":"'"$MODEL"'","max_tokens":8,"messages":[{"role":"user","content":"ping"}]}'
```

Expect `200` **and** a body containing `"choices"`.

**Anthropic-shaped gateway** — `POST {base}/v1/messages`:

```bash
curl -sS -m 20 -o /tmp/wizard-probe.json -w '%{http_code}' \
  -X POST "$BASE/v1/messages" \
  -H "x-api-key: $KEY" \
  -H 'anthropic-version: 2023-06-01' \
  -H 'content-type: application/json' \
  -d '{"model":"'"$MODEL"'","max_tokens":8,"messages":[{"role":"user","content":"ping"}]}'
```

Expect `200` **and** a body containing `"type": "message"`.

**The shim** — the same Anthropic probe against the shim's port. This is the
probe that proves the whole path Claude Code will take.

The probe body is written to a temporary file that is **deleted immediately**,
and it never contains the credential. Only the status, the shape and the
latency are reported.

### 5.3 What is not proof

- **"the string was written"** — this is the false success the dry run found
  (482). A file containing a placeholder is not a working credential.
- **a 200 with the wrong body** — a gateway can answer 200 with an OpenAI
  body on an Anthropic route. The assertion is about the body, not the status.
- **a 401 treated as "reachable"** — 401 means *the key is wrong*, which is
  a different failure from *the gateway is down*, and it has a different fix.
- **a GET returning 405** — that is not a 404. The route exists and the verb
  is wrong; the probe must say which.
- **a probe run on the build machine** about a node on another machine
  (`docs/PORTABILITY.md` §6.8). Reachability is the failure that actually
  bites, and a host-local probe cannot see it.

### 5.4 From the target's position

The verification runs **where the node will run**. A gateway on `127.0.0.1`
that is loopback-only is reported as **unreachable from this node** — in those
words — and the wizard refuses to write a configuration that cannot work. It
does not write the config and hope.

### 5.5 The verification is what settles an unknown

A key's prefix does not identify its provider. A 125-character key beginning
`sk-cp-` may belong to a different family than the variable name suggests,
and the repo currently holds an open question about exactly that: the key in
`<repo>/.env` is named `OPENROUTER_API_KEY`, and the prefix does not read as
OpenRouter's.

**The wizard does not resolve that by reading the prefix.** It resolves it by
asking the gateway. If the key authenticates against the configured gateway,
it belongs to it, whatever the variable is called. If it does not, the wizard
exits 8 and says which endpoint rejected it — and the variable name becomes a
question with an answer rather than a question with a prefix.

---

## 6. The plan model

### 6.1 The actions

| id | effect | what it writes | tier |
|---|---|---|---|
| `wizard-detect` | none | — (the profile is in the plan) | standard |
| `wizard-verify-key` | none | — (a probe, not a write) | standard |
| `genesis-env` | create/update | `<repo>/.env`, mode 0600 | standard |
| `codexify-env` | create/update | `<codexify-root>/.env`, one line, mode 0600 | standard |
| `gateway-token-helper` | create/update | `~/.config/genesis/bin/gateway-token.sh`, mode 0700 | standard |
| `claude-settings` | create/update | `~/.claude/settings.json` — the `env` block and `apiKeyHelper`, merged | standard |
| `shim-service` | install/enable | the user unit or LaunchAgent | aggressive |
| `node-profile` | create/update | `~/.config/genesis/node.json` | standard |
| `readiness` | none | — (the probes) | standard |

Each action carries `desired` and `observed`, and the plan is the difference
between them (`docs/PORTABILITY.md` §6.1). Running the wizard twice on the
same machine produces an empty plan — *already converged, nothing to do* — and
that is the definition of idempotency this spec adopts.

### 6.2 Ownership, and the one special case

The ownership rule is unchanged (`docs/PORTABILITY.md` §6.2): the wizard writes
only files it owns, recorded in `~/.config/genesis/manifest.json` with a
content hash; a file that changed underneath it is a **conflict** (exit 6),
not an overwrite.

**The special case is the credential file.** The manifest must not become an
oracle for the credential, so for `<repo>/.env` and `<codexify-root>/.env` the
manifest records:

- the **key fingerprint** (so a changed key is detected), and
- a hash of the file **with the credential line replaced by a placeholder**
  (so an edit to any other line is detected).

The ownership check therefore still works for both kinds of change, and the
manifest never holds anything derived from the secret beyond the fingerprint.

### 6.3 The plan hash

```
plan_hash = sha256(canonical_json(profile, ordered_actions, key_fingerprint))
```

where each action contributes `(id, kind, target, desired, effect)`.

The fingerprint is in there on purpose. The receipt says *"this plan, on this
machine, with this key"*. If the operator replaces the key between the dry run
and the apply, the receipt expires and the diff is shown again — the same
mechanism that expires a receipt when the machine changes
(`docs/PORTABILITY.md` §5.3).

---

## 7. Failure modes

| exit | meaning | what the wizard says |
|---|---|---|
| 0 | applied, and every consumer verified | the table of what was verified |
| 1 | the apply failed partway, or a consumer failed its probe | what succeeded, what failed, what remains manual |
| 2 | usage error | the offending flag |
| 3 | unsupported host | the detected host and the supported set — nothing changed |
| 4 | no valid receipt, or the receipt no longer matches | the exact command to run |
| 5 | missing prerequisite (no `python3`, or too old) | nothing changed |
| 6 | conflict — the plan would overwrite a file it does not own | the file and the `--force-own` hint |
| 7 | unsupported substrate — the gateway speaks neither shape | the port, the URLs tried, and that nothing was written |
| **8** | **the key did not authenticate** | the endpoint, the status, and that nothing was written |
| **9** | **the wizard's own precondition failed** | no TTY for the prompt, or the credential file is not writable |

Exit 8 is the new one, and it is the honest answer to 482's finding. A key
that does not authenticate is a **refusal**, not a warning: the wizard writes
nothing, changes nothing, and says which endpoint rejected the credential.

Exit 7 remains the substrate refusal from `docs/PORTABILITY.md` §7.3 — a
gateway that answers 404 on `/v1/messages` while `/v1/chat/completions`
answers 200 is **not** a refusal when the shim is available, because the shim
*is* the translation. Exit 7 is for a gateway that speaks neither shape.

---

## 8. The flags

`--wizard` composes with the ladder's own flags. The wizard adds:

| flag | meaning |
|---|---|
| `--wizard` | run the wizard: detect, prompt once, verify, plan |
| `--gateway-url URL` | the gateway base. Default: detected. Never a placeholder — a URL containing `<` is refused |
| `--gateway-kind {auto,openai,anthropic}` | default `auto` — the shape is probed, never assumed |
| `--model ID` | the model ID the gateway **serves** |
| `--codexify DIR` | the Codexify installation to configure. Default: detected |
| `--no-codexify` | skip the Codexify target (it becomes a manual step) |
| `--no-claude` | skip the Claude Code target |
| `--shim-port N` | the shim's port. Default 8790. `127.0.0.1` only |
| `--key-file PATH` | read the key from a file instead of prompting. The **path** is a path; the value never appears in the process table |
| `--keep-key` | keep the key already configured; do not prompt, do not verify against a new key |

`--help` renders from the flag registry, as the rest of the contract requires
(`docs/PORTABILITY.md` §8.1) — the wizard's flags are added to the same table,
not written a second time.

---

## 9. What the wizard prints

Every output is masked. The rules:

- a credential-shaped string in any output becomes `***`;
- the diff shows placeholders, never values;
- the receipt carries hashes and counts;
- the key itself is displayed **never**, not even in a debug mode, and not
  even truncated. The fingerprint is displayed instead.

The wizard's own output is an artifact that gets pasted into tickets, logs
and chat. Treating it as public is the cheap assumption.

---

## 10. Definition of done

The wizard is done when:

- [ ] a run contains **exactly one** credential prompt, and it is read without
      echo — a test asserts the prompt count, not just the outcome;
- [ ] the value never appears in a command line, an argument, a log, a
      receipt, a manifest entry, or a tracked file — enforced by a test that
      scans the outputs and the working tree, as
      `test_bootstrap_never_writes_credentials` does for the bootstrap;
- [ ] a **wrong key** exits 8, writes nothing, and says so — and a test
      proves the "nothing" by hashing the filesystem before and after;
- [ ] a **right key** is verified against the gateway before the plan is
      computed, and the probe asserts the **body**, not the status;
- [ ] after `--apply`, all three consumers authenticate **with the configured
      key** — genesis chat, Codexify and Claude Code-through-the-shim — and
      the wizard prints one line per consumer with what actually happened;
- [ ] the key is not asked for twice: a second run on a configured machine
      prompts for a choice, not for the value;
- [ ] the install route branches on the detected `js_runtime`: the native
      installer on a machine with no JS runtime, and never a Node assumption;
- [ ] the plan is empty on a second run — *already converged, nothing to do*;
- [ ] `~/.claude/settings.json` is **merged**, not replaced, and every other
      key in it survives;
- [ ] `GUARDIAN_API_KEY` and every other Codexify variable are untouched;
- [ ] the credential files are mode 0600 and gitignored — verified with
      `git check-ignore`, not assumed;
- [ ] the shim is a **user** service, binds `127.0.0.1` only, and is
      installed only when the gateway is OpenAI-shaped;
- [ ] the verification runs from the target's network position, and a
      loopback-only gateway on a remote node is reported as unreachable
      rather than written as a working configuration;
- [ ] the wizard's exit codes are covered by tests, including 8.

---

## 11. Amendments to `docs/PORTABILITY.md`

This document extends the portability spec. The delta, exactly:

1. **§5.4 (flag set)** — add the wizard's flags from §8 above.
2. **§5.1 (the three rungs)** — add the `--wizard` mode column: the rungs are
   the same, the mode changes what the run collects and verifies.
3. **§5.5 (exit codes)** — add 8 (the key did not authenticate) and 9 (the
   wizard's own precondition failed).
4. **§6.4 (what the bootstrap installs)** — add the wizard's actions from
   §6.1 above.
5. **§6.3 (the plan hash)** — add `key_fingerprint` to the hashed inputs.
6. **§9.3 (credentials are never written)** — add the wizard's exception and
   its conditions (§2.2). The bootstrap's rule is unchanged: the bootstrap
   still never writes a credential. The wizard is the tool that establishes
   one, under a human's control, into gitignored files only.
7. **§4.1 (the signals)** — add `js_runtime` to the profile. It is already
   used by §7.1 but was not listed as a signal.

Nothing else in `docs/PORTABILITY.md` changes. Where the two documents could
be read as disagreeing, this one is about the wizard and that one is about
the bootstrap; the wizard is a mode of the bootstrap, and the ladder's rules
apply to it unchanged.

---

## 12. Open questions for Zac

1. **Which gateway does onboarding target?** The local OpenAI-shaped broker
   on `:8000` (the shim is required — the case this document is built
   around), the local Anthropic-shaped server on `:8082` (no shim), or a
   remote provider (no shim, and the key is the provider's)? The wizard
   detects the shape, but the *default* matters for a first-run experience.
2. **One value on disk, or one per application?** §1.2 takes the honest
   position: each application reads its own credential file natively, so the
   value lands in more than one gitignored file. If the requirement is
   literally one file, Codexify's compose has to be pointed at `<repo>/.env`
   instead, and that is a change to how Codexify is run, not to the wizard.
3. **The local broker's own credential.** The local broker may have its own
   key (`LOCAL_API_KEY`), which is not the provider key. The wizard treats
   that as a detected machine-local constant and never asks a human for it —
   but if the two are meant to be the same value, that should be said.
4. **The `openrouter` backend as the default.** The CLI defaults `genesis
   chat` to the local backend. Should the node profile decide it, or should
   the operator pass `--backend` every time? (§11.4 proposes the profile.)
5. **Does the wizard install the Claude Code CLI, or only configure it?** The
   native installer route is specced (`docs/PORTABILITY.md` §7.1); whether it
   belongs in the same run as the key entry is a UX call.
