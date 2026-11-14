# WIZARD — one key, every system
<!-- visibility: sam-readable -->

**Status: the ladder is built, and the wizard is built on top of it.** This
document describes a tool that now exists: `setup.sh` and `tools/bootstrap.py`
implement the three rungs, and `--wizard` is a mode on them. The provider
detection in §5.6 is `tools/keydetect.py`; the node contract it hands the
tailnet to is `docs/NODE-CONTRACT.md`. What is *not* built is anything that
was never in this document: a commit, a push, or a machine launch.

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

### 1.1 One prompt per secret, three consumers

A run of the wizard contains **one credential prompt per distinct secret**:
one for the provider key, and — only when the node is joining a tailnet — one
for the tailnet auth key (§4.5). The two are different secrets belonging to
different systems; neither is asked for twice, and no consumer ever asks the
operator to paste a key into more than one place.

The provider key entered first reaches every onboarded system. No consumer
asks a second question, and no consumer requires the operator to paste the key
into three different files.

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
                 │  the wizard  (one prompt per secret)    │
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
   is replaced before it is printed (`docs/PORTABILITY.md` §9.3);
6. every credential the wizard **finds already on the machine** is reported
   — the shell rc files' exports especially — and one is retired only when the
   operator says so, with the rc file backed up and the change recorded in the
   manifest (§6.1). The wizard never writes a credential into an rc file, and
   never retires one it cannot show is superseded.

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

**The key's format is read the moment it is entered**, before anything is
planned: it says which provider to ask, and §5.6 is what that means. The
operator can override it (`--provider`), and the override wins.

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
| a live call through the **proved provider's** backend | **genesis chat** authenticates with the key |
| the gateway's own endpoint, with `OPENAI_API_KEY` from the Codexify `.env` | **Codexify** authenticates with the key |
| `POST` to the shim's `/v1/messages`, asserting `"type": "message"` | **Claude Code** reaches a gateway that speaks Anthropic Messages |
| the helper script's stdout length and fingerprint | the credential Claude Code would read is the one that was verified |

A consumer that fails is reported as failing. The wizard does not print
"ready" because it got most of the way — it prints the table, and exits 1 if
any consumer failed.

**A row that reports a write is a write report, not a readiness report.** The
wizard can honestly say "wrote `ANTHROPIC_BASE_URL=…` to the settings file";
it cannot honestly say "Claude Code is ready" from having written the file.
A readiness row either makes the request above or says exactly what it wrote
and stops there. A row that reports success for a check that was never run is
482's false-success one level up — and the wizard is the one tool on this
machine whose whole purpose is not to be that.

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

**The provisioning command is a different tool, and it owns different keys.**
`genesis codexify` (`docs/CODEXIFY.md`) brings the whole Codexify system up,
not just its credential. It therefore has to write what that system needs to
exist at all — including `GUARDIAN_API_KEY` and `NEO4J_PASS`, which compose
refuses to start without and which `graph-init` exits 1 without.

The split is deliberate, and it holds in both directions:

- the **credential wizard** never writes `GUARDIAN_API_KEY`. It is not the
  credential wizard's secret, and it will not rotate one;
- the **provisioning command** generates `GUARDIAN_API_KEY` and `NEO4J_PASS`
  and never asks the operator for either. They are Codexify's secrets, and
  the operator is not asked to type a secret they do not own.

§1.1's rule is unchanged: one prompt per distinct secret, and the only secret
a human enters is the provider key.

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

**`ANTHROPIC_BASE_URL` points at the Anthropic endpoint, which is sometimes
the shim.** When the gateway is Anthropic-shaped, that is the gateway. When
the gateway is **OpenAI-shaped**, it is the **shim** — the shim is the thing
that speaks Anthropic Messages, so the shim's own address is what Claude Code
must be given. Writing the gateway's URL there instead would point Claude Code
at a protocol it cannot speak, while a shim sat installed and idle beside it.
The wizard therefore derives that one value from **where the Anthropic
endpoint actually is**, and the dry-run diff says which of the two it chose.

**That file is machine-wide, and the wizard says so.** `~/.claude/settings.json`
is not this wizard's settings — it is the settings of *every* Claude Code
session on the machine, including other agents' and their operator's. Setting
`ANTHROPIC_BASE_URL` there re-routes all of them to the gateway this wizard
just chose. That is what the wizard is for, and it is also the widest blast
radius in the whole run, so the dry-run diff names that file explicitly, and
the apply reports what it changed there. On a machine where other agents
share it, that is a fact worth saying out loud before the write, not after.

### 4.4 The shim

`src/shim.py` is the Anthropic↔OpenAI translation layer, already written, and
it is **required** whenever the gateway is OpenAI-shaped. The wizard's job is
to put it in the path and keep it running:

- a **user** service, never a system one — a `systemd --user` unit on Linux, a
  `LaunchAgent` on macOS. No `sudo`, and it survives `omarchy-update`;
- the service sources `<repo>/.env` and execs the shim, so the credential
  reaches the process the way the harness reaches it;
- **the service carries three things, not one: the upstream, the port, and the
  credential.** The upstream is not a credential and does not live in `.env`,
  so it has to be set explicitly — `GENESIS_SHIM_UPSTREAM` and
  `GENESIS_SHIM_PORT` beside the key. And **both service formats must carry
  the same three**, because they share a launcher: a unit that gets the
  upstream from an `Environment=` line and a plist that gets it from nowhere
  is a service that starts and exits 2 on one platform while working on the
  other;
- it binds `127.0.0.1` only, on a fixed port (8790), and the wizard says so
  when it reports the port;
- the model ID it serves is the one the gateway **serves**, cross-checked
  against the gateway's own model list rather than its advertisement
  (`docs/PORTABILITY.md` §7.4).

If the gateway is Anthropic-shaped, the shim is not installed and the wizard
says why: *the gateway speaks Anthropic Messages natively; no translation
layer is needed.*

### 4.5 The tailnet — a second secret

The node contract makes the tailnet the connective layer: Tailscale, installed
by the system package manager, with the node joining by an auth key. That is a
**second distinct secret**, and the rules above have to account for it rather
than be quietly bent.

**The prompt.** One more prompt, for the tailnet key, and only when the node
is actually joining a tailnet. It is not interchangeable with the provider
key, it is never asked for twice, and a node that is not joining a tailnet is
never asked at all.

**Where it lives.** `~/.config/luna-node/auth.key`, mode 0600, outside every
repository — the same shape as the provider key's rule (§2.2): not in a
tracked path, never in a log, a receipt or a manifest entry.

**What kind of key.** A tailnet auth key is not one thing but a set of
properties, and the wizard asks for the narrowest set that will do the job:

- **single-use** — the key is consumed when this node registers, so it cannot
  be used to add a second machine afterwards;
- **pre-authorized** — the node joins without a separate admin approval;
- **tag-scoped** — the node is bound to a tag whose ACLs say what it may reach,
  rather than inheriting the whole tailnet;
- **expiring** — the key has a bounded lifetime, so a copy that leaks stops
  working on its own.

"Never enters the repo" does nothing for any of that. A reusable,
unexpiring, unscoped key left on a machine that later gets sold, lent or
repurposed is a standing invitation to the tailnet, and the file's mode is
not the control — the **key's properties** are.

One property is a decision rather than a default: **ephemeral**, which
removes the node from the tailnet when it goes offline. That is right for a
disposable guest node and wrong for one that must survive a reboot, so the
wizard takes it from the node's role rather than assuming it. (The CLI
documents how the key is *passed*; it does not document the key's
properties — those are the tailnet's policy, so check them where the key is
created.)

The wizard records the key's **properties** in the node profile, never the
key, so the blast radius is visible in the record rather than implied by it.

**How it is passed — and this is the part that has to be right.** The
`tailscale` CLI takes the key in `--auth-key`, and a key in an argument *is*
an argument: it lands in the process table and in the shell history. But the
flag accepts a **path**:

```
  --auth-key value
        node authorization key; if it begins with "file:", then it's a path
        to a file containing the authkey
```

So the wizard runs:

```
tailscale up --auth-key=file:<the key file>
```

and the argument carries a **path**, not the secret. §2.2's rule therefore
holds without an exception: the value never appears in a command line, an
argument, an environment dump, a log, a receipt, a manifest entry, or a diff.
The flag is spelled `--auth-key`, with the hyphen; `--authkey` is not it.

**What the wizard records afterwards** is not the key: the tailnet hostname
and the address the node was given, written to the node-local profile
(§9.6). Nothing else ever reads the key file.

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

**The credential header is the endpoint's, not the wizard's.** The two
Anthropic-shaped endpoints do not agree on it: OpenRouter's `/messages`
accepts `x-api-key`, `Bearer`, or both, while MiniMax's Anthropic route
documents **Bearer only**. A probe that hard-codes `x-api-key` therefore
answers 401 against a perfectly good key and reads as "the key is wrong" —
the wrong conclusion, from the right evidence. So the probe sends the
credential the way the endpoint documents it, and where an endpoint accepts
only one form, that form is what the wizard uses.

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
- **a shape the probe accepts and a shape the decision uses that disagree** —
  the probe's assertion must be at least as strong as the decision it feeds. A
  gateway that answers `/v1/messages` with an OpenAI body is an
  **OpenAI-shaped gateway on an Anthropic route**: the probe accepts the 200,
  and if the decision then follows the route's *declared* shape, the wizard
  installs no shim and points `ANTHROPIC_BASE_URL` at a protocol Claude Code
  cannot speak. **The shape the probe accepts is the shape the decision
  uses** — a route's name is not a shape — and a body that is neither
  `"type": "message"` nor `"choices"` is **rejected**, not accepted as
  whatever the route promised.
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
and this was measured on the machine this spec was written on: the key in
`<repo>/.env` was named `OPENROUTER_API_KEY`, carried the `sk-cp-` prefix,
and answered **401 — "this is not an OpenRouter API key"** against the
endpoint its own variable name pointed at. The same key answered **200** with
an Anthropic body against a different provider's Anthropic route. It was
never a bad key; it was a good key under the wrong name, and the name had
been the only evidence anyone had.

**The wizard does not resolve that by reading the prefix.** It resolves it by
asking the gateway. If the key authenticates against the configured gateway,
it belongs to it, whatever the variable is called. If it does not, the wizard
exits 8 and says which endpoint rejected it — and the variable name becomes a
question with an answer rather than a question with a prefix.

### 5.6 Which provider is this key from?

The detection is **format sniffing**, and it is the difference between a
wizard that asks three questions and one that asks one.

| the key begins | the wizard asks | the variable it is written under |
|---|---|---|
| `sk-ant-…` | Anthropic | `ANTHROPIC_API_KEY` |
| `sk-or-…` | OpenRouter | `OPENROUTER_API_KEY` |
| `sk-cp-…` | MiniMax, then OpenRouter | `MINIMAX_API_KEY` |
| `sk-proj-…`, `sk-…` | OpenAI | `OPENAI_API_KEY` |
| anything else | nothing — it asks the operator | the operator's answer |

Three things about that table, and the third is the one that matters:

1. **The variable name is the point.** A key under the wrong name
   authenticates nowhere, and the failure looks like a bad key rather than
   a bad filename. The wizard writes the key under the name its provider
   uses, and that name is the one the harness reads it from
   (`src/harness.py`, the `BACKENDS` table).
2. **`sk-cp-` is a family, not a single provider.** The wizard asks MiniMax
   first and OpenRouter second, and the gateway decides. The walk exists
   because the format genuinely does not identify the provider — §5.5 is
   the reason.
3. **The format is a shortlist, never an answer.** It narrows "every
   provider on earth" to two or three places worth asking. It never says
   the key works, and it never says the key is the provider's.

**What this does not change.** §5.5 still holds: the wizard does not
*resolve* a key by reading the prefix. It resolves it by asking the
gateway. The format decides where to ask; the answer decides what is true.
A key whose format matches nothing is not guessed at — the wizard asks
which provider it is, because guessing is how the wrong variable name gets
written in the first place.

**The override.** `--provider {anthropic,openai,openrouter,minimax}` says
*I know where this key came from*. It replaces the shortlist with that
provider's endpoints, and the probe still decides. An override is not a way
to skip the verification; it is a way to skip the guessing.

**Measured, not assumed.** The table was checked against live endpoints:

- an `sk-or-` key authenticates at OpenRouter's Anthropic route with
  either `x-api-key` or `Authorization: Bearer`;
- an `sk-cp-` key authenticates at MiniMax's Anthropic route with
  `Authorization: Bearer`, and OpenRouter **refuses the same key** with a
  401 that says, in its own words, that OpenRouter keys begin `sk-or-`;
- so the walk finds MiniMax for an `sk-cp-` key, and the refusal it meets
  at OpenRouter is a fact rather than a guess.

Those two measurements are also why the walk is a walk and not a table: the
same prefix has been seen on both sides of it.

**Where it lives.** `tools/keydetect.py` — `sniff()`, `detect()`,
`probe()`, `probe_candidates()`, `fingerprint()`, `mask()`. The probe
asserts on the **body** (§5.1), and it is the only thing that decides
which endpoint won.

**What the proof settles is not only the provider but the name.** A variable
name is a *claim* about which provider owns a key, and a claim is not a fact.
So the wizard writes the key under the name of the endpoint it **proved**,
and refuses to write a key under a name the selected backend does not read.
A file that looks configured and answers 401 is the same failure as a
placeholder — the string was written, and nothing works.

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
| `stale-credentials` | update/remove | the shell rc files' credential exports — only the ones the operator retires | standard |
| `readiness` | none | — (the probes) | standard |

Each action carries `desired` and `observed`, and the plan is the difference
between them (`docs/PORTABILITY.md` §6.1). Running the wizard twice on the
same machine produces an empty plan — *already converged, nothing to do* — and
that is the definition of idempotency this spec adopts.

**The credentials that were already there.** The wizard is not the only thing
that can hold a credential on a machine, and on a used one it usually is not
the only one. This machine carries **two live credentials in `~/.zshrc`** — an
`OPENROUTER_API_KEY` and a `MINIMAX_API_KEY` — and the resolution order
differs by consumer: the harness prefers `<repo>/.env` (`load_env()` first,
then the environment), but the local wrappers under `bin/` prefer the
**environment** and fall back to **sourcing the rc file** when the variable is
unset.

So a wizard that only writes `.env` leaves those wrappers authenticating with
a key nobody verified, and leaves the old key live on the machine
indefinitely. That is 482's false-success one layer up: the wizard says
*configured*, and the machine answers with a different credential.

The wizard therefore has an action for it. It **detects** credential-shaped
exports in the shell rc files the profile names, and for each one it offers
exactly two outcomes, both recorded in the manifest:

- **retire** — the export is removed, the rc file is backed up first, and the
  change is reversible through the manifest. The operator asks for this
  explicitly; the wizard never decides it, and it never retires a credential
  it cannot show is superseded;
- **keep** — the export stays, and the wizard says so in those words: *this
  machine still carries a credential the wizard did not verify, and the
  wrappers will prefer it.*

What the wizard does is make the machine's credential state **visible**. A
machine with two live keys and a wizard that says nothing about them is
exactly the case where the wrong key silently answers.

### 6.2 Ownership, and the one special case

The ownership rule is unchanged (`docs/PORTABILITY.md` §6.2): the wizard writes
only files it owns, recorded in `~/.config/genesis/manifest.json` with a
content hash; a file that changed underneath it is a **conflict** (exit 6),
not an overwrite.

**One hash function, named once.** The manifest and the ownership check must
compute the content hash the *same* way — one function, called from both
sides. Two implementations of "the content hash" is how a file the tool wrote
itself comes back as a conflict on the second run, and that is the one
failure this whole mechanism exists to prevent.

**A symlink is not a file.** The ownership check compares the **target's**
bytes, and a link whose target is gone is a **divergence to report**, not a
phantom to clean up: the wizard does not delete a link it did not create, and
it does not treat a dangling one as "already converged".

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
| `--provider NAME` | override the provider the key is routed to. Default: detected — the key's prefix narrows the candidate family and the probe decides (§5.5). The provider carries the key's **variable name** and its **auth style**, so naming it wrong is naming the wrong file and the wrong header |
| `--model ID` | the model ID the gateway **serves** |
| `--codexify DIR` | the Codexify installation to configure. Default: detected |
| `--no-codexify` | skip the Codexify target (it becomes a manual step) |
| `--no-claude` | skip the Claude Code target |
| `--key-file PATH` | read the key from a file instead of prompting. The **path** is a path; the value never appears in the process table |
| `--keep-key` | keep the key already configured: the wizard does not prompt, does not write a new credential, and carries the value the **manifest already records** (by fingerprint) into the plan unchanged. It may still re-verify that value against the gateway — a key that no longer works is not a key to keep |
| `--tailnet-auth-key-file PATH` | the tailnet auth key's file. Default `~/.config/luna-node/auth.key`, mode 0600. The **path** is what is passed to `tailscale` (`--auth-key=file:`), never the value (§4.5) |
| `--no-tailscale` | skip the tailnet step entirely; the node is not asked for an auth key. The name is `docs/PORTABILITY.md`'s, not a second name for the same thing |

The shim's port is **fixed at 8790**, not a flag: it is the port `src/shim.py`
binds by default, and a port the operator has to remember is a port the
operator will get wrong. The wizard reports the port it used, and the
`GENESIS_SHIM_PORT` environment in the service is what carries it.

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

- [ ] a run contains **one credential prompt per distinct secret** — the
      provider key, plus the tailnet auth key when the node joins a tailnet —
      each read without echo, and a test asserts the prompt count, not just
      the outcome;
- [ ] the value never appears in a command line, an argument, a log, a
      receipt, a manifest entry, or a tracked file — enforced by a test that
      scans the outputs and the working tree, as
      `test_bootstrap_never_writes_credentials` does for the bootstrap;
- [ ] a **wrong key** exits 8, writes nothing, and says so — and a test
      proves the "nothing" by hashing the filesystem before and after;
- [ ] a **right key** is verified against the gateway before the plan is
      computed, and the probe asserts the **body**, not the status;
- [ ] the shape the probe accepts and the shape the decision uses are **the
      same shape**: a gateway answering an Anthropic route with an OpenAI body
      is treated as OpenAI-shaped, and a body that cannot be classified is
      rejected rather than accepted as the route's declared shape (§5.3);
- [ ] the key is written under the variable name of the endpoint it was
      **proved** against, and the wizard refuses a name the selected backend
      does not read — a file that looks configured and answers 401 is a
      placeholder wearing a different hat (§5.5);
- [ ] the probe sends the credential the way the endpoint **documents** it —
      `x-api-key` or `Bearer`, as that endpoint accepts — so a good key is
      never reported as a bad one (§5.2);
- [ ] after `--apply`, all three consumers authenticate **with the configured
      key** — genesis chat, Codexify and Claude Code-through-the-shim — and
      the wizard prints one line per consumer with what actually happened,
      where "what happened" means **a request was made**: a row that reports a
      write says what it wrote and does not claim the consumer works (§3.7);
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
- [ ] the wizard reports the credentials **already on the machine** — the
      shell rc files' exports — and either retires them on the operator's
      word (reversible, manifest-recorded, rc file backed up) or says plainly
      that they remain and that the wrappers will prefer them (§6.1);
- [ ] the shim is a **user** service, binds `127.0.0.1` only, and is
      installed only when the gateway is OpenAI-shaped;
- [ ] **both gateway shapes are exercised.** A key of one shape never reaches
      the other shape's branch, so the branch this machine's key does not take
      is untested by construction — the shim path needs its own test against a
      synthetic OpenAI-shaped endpoint, and the service it starts needs its own
      test on **both** platforms, because a unit that carries the upstream and
      a plist that does not are the same script with different luck (§4.4,
      §4.3);
- [ ] the shim service carries the **upstream**, the port and the credential,
      and `ANTHROPIC_BASE_URL` points at the **shim** when the gateway is
      OpenAI-shaped — not at the gateway, which speaks a protocol Claude Code
      cannot speak (§4.3, §4.4);
- [ ] the verification runs from the target's network position, and a
      loopback-only gateway on a remote node is reported as unreachable
      rather than written as a working configuration;
- [ ] the tailnet auth key, when the node joins a tailnet, is asked for
      **once** and stored at mode 0600, and `tailscale` is invoked with
      `--auth-key=file:<path>` — a path in the argument, never the value
      (§4.5);
- [ ] the tailnet auth key is asked for as the narrowest key that will do the
      job — single-use, pre-authorized, tag-scoped, expiring — and its
      **properties** are recorded in the node profile, never the key (§4.5);
- [ ] the wizard's exit codes are covered by tests, including 8.

---

## 11. Amendments to `docs/PORTABILITY.md`

This document extends the portability spec. The delta, exactly:

1. **§5.4 (flag set)** — add the wizard's flags from §8 above. The wizard
   **composes with the ladder's existing names** rather than inventing
   parallel ones: the tailnet step is skipped with `--no-tailscale`, not with
   a second spelling of the same switch, and `--provider` names the provider
   family the key was proved against.
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
8. **§6.4 (what the bootstrap installs)** — add the tailnet action: install
   Tailscale by the system package manager, take the auth key from the
   wizard's second prompt, store it at mode 0600, and pass it as
   `--auth-key=file:<path>` (§4.5). §6.5's rule that the bootstrap *reads*
   credentials from the environment still stands; the wizard is what
   establishes this one, and it does so only when the node is joining a
   tailnet.
9. **§5.4 (flag set)** — add `genesis codexify`, a separate command from the
   ladder. It is the *provisioning* surface for Codexify
   (`docs/CODEXIFY.md`), and it owns `GUARDIAN_API_KEY` and `NEO4J_PASS`,
   which the credential wizard does not touch (§4.2). It is self-contained:
   it does not go through the ladder's receipt and manifest, and it does not
   disturb the ladder's own actions.

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
