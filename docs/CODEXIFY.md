# CODEXIFY — what a full setup is, and what it needs
<!-- visibility: sam-readable -->

**This document is the answer to one question: what does a working Codexify
actually require?** It is also the specification behind `genesis codexify`,
which does the whole thing in one command.

Codexify is the third of GENESIS's three consumers. The other two — `genesis
chat` and Claude Code — need a key and a base URL. Codexify needs a **system**:
a database, a graph, a queue, four one-shot jobs, a backend, a frontend, four
workers, and a local inference runtime for the workers to call. A key in a
file is not a setup.

**What is not built is anything that was never in this document.** The wizard
itself is built (`src/codexify.py`); the docs are the contract it keeps.

---

## 1. The vendored install

Codexify lives in the `codexify-core/` git submodule, pinned by the parent
repository.

> **The submodule is the live upstream.** It pins Resonant-Jones/Codexify
> at its current `main` (0fd3d62), so a fresh `git clone --recurse-submodules`
> gives consumers the same tree the developers build against. The earlier
> repository (Resonant-Jones/Codexify-Core) is now a read-only archive; this
> commit switches the submodule to its successor so consumers do not clone
> an increasingly stale tree.

**A plain clone leaves `codexify-core/` empty.** It is a submodule, and
`git clone` without `--recurse-submodules` gives you an empty directory. An
empty directory is **not an installation**: the wizard checks for
`docker-compose.yml` **and** `.env.template`, and refuses with exit **3**
rather than writing a `.env` into a directory nobody will ever run.

---

## 2. The canonical stack

`docker-compose.yml` is the canonical file — the only one that builds from
source, and the one the README, the `Makefile`, the CI smoke workflow and the
smoke launcher all use. The other three (`docker-compose.runtime.yml`,
`.compiled.yml`, `.webui-runtime.yml`) are the prebuilt-image variants and
their extensions.

| service | image | host port | role |
|---|---|---|---|
| `db` | postgres:15 | 5433 | persistence |
| `neo4j` | neo4j:5 | 7474 / 7687 | graph store |
| `redis` | redis:7-alpine | internal | task queue and event streams |
| `graph-init` | neo4j:5 | — | one-shot: constraints, indexes, seed nodes |
| `migrator` | built | — | one-shot: `alembic upgrade heads`, then `seed_defaults.py` |
| `model-prep` | built | — | one-shot: fetch the embedding model into `/models` |
| `backend` | built | **8888** | the FastAPI Guardian app; healthcheck `/ping` |
| `worker-warmup` | built | — | warms the local inference endpoint |
| `worker-chat` | built | — | consumes the Redis chat queue; heartbeats every 45 s |
| `worker-chat-embed` | built | — | embeds chat turns |
| `worker-document-embed` | built | — | embeds uploaded documents |
| `frontend` | node:20-alpine | **5173** | the Vite dev server (corepack pnpm) |

Optional profiles: `--profile voice` (`tts`, port 8000), `--profile cli`
(`obsidian-ingest`, `chatgpt-migrate`, `hf-prefetch`, `embed-prefetch`),
`--profile backfill` (`embedding-backfill`, `graph-backfill`).

### The order is not advisory

Compose's `depends_on` graph is the authority, and it runs in this order:

1. `db` healthy → the **`migrator`** runs and must exit 0;
2. `neo4j` healthy → the **`graph-init`** runs and must exit 0;
3. **`model-prep`** runs and must exit 0;
4. only then does the **`backend`** start. It waits for Postgres (120 s),
   verifies eight tables, seeds, and execs uvicorn;
5. only then do the **`frontend`** and the **workers** start — the frontend
   waits for the backend to be *healthy*, and the workers wait for Redis.

So `docker compose up` is not one thing happening. It is six, in order, and
three of them are one-shot jobs whose exit code decides whether the fourth
happens at all.

---

## 3. The environment file

Compose reads `<codexify>/.env` twice over: once as `env_file`, and once for
`${…}` interpolation. These variables have **no default**, so compose itself
refuses to start without them:

| variable | what happens without it |
|---|---|
| `GUARDIAN_API_KEY` | `${GUARDIAN_API_KEY:?…}` — **compose exits** |
| `NEO4J_PASS` | `graph-init` exits 1: `ERROR: NEO4J_PASS is not set` |
| `LOCAL_CHAT_MODEL` | injected into seven services as an empty string |

And these are needed for the thing to work rather than to start:
`NEO4J_USER`, `LLM_PROVIDER` (must be `local`), `LOCAL_BASE_URL`,
`LOCAL_EMBED_MODEL` (a path **inside the container**:
`/models/bge-large-en-v1.5`), `VITE_GUARDIAN_API_KEY` (the browser has to be
able to reach the backend), `DATABASE_URL`.

### Four traps

These are the reasons a wizard cannot simply copy `.env.template`, and the
reasons it cannot simply shell out to Codexify's own setup.

**1. `.env.template` does not carry `LOCAL_CHAT_MODEL` at all.** It is the one
variable compose has no default for, and it is in `.env.example` but not in
the template. A template copied verbatim hands every worker an empty model.

**2. `.env.template` points the LLM at `localhost`, which is wrong inside a
container.** It sets `LOCAL_BASE_URL=http://localhost:8000/v1`, and compose
injects that value straight into the containers. From inside a container,
`localhost` is the container itself, so the LLM path is dead.

The working posture is Codexify's own canonical docker shape — the address
the containers can reach, not the address the host can reach:

```
LOCAL_BASE_URL=http://host.docker.internal:8000/v1
LOCAL_DOCKER_FALLBACK_BASE_URL=http://host.docker.internal:8000/v1
VAULTNODE_BASE_URL=http://host.docker.internal:8000
VAULTNODE_HEALTH_ENDPOINTS=/health,/health/runtime,/ready,/v1/models,/api/tags
```

The **host** address is where the runtime is *probed* — the wizard asks
`127.0.0.1:8000` which model is being served, and writes the *container*
address into the file. Two addresses for one runtime, and the difference is
the whole reason a container cannot use `localhost`.

`VAULTNODE_HEALTH_ENDPOINTS` is not cosmetic: `worker-warmup` reads it to
decide where to probe, and a service the wizard starts with no endpoints to
probe cannot verify anything.

There is a second mechanism in Codexify — a docker fallback bridge — but it
is **opt-in** (`CODEXIFY_LOCAL_DOCKER_FALLBACK_ENABLED` defaults to `false`)
and it only engages when the primary host is loopback. With the primary
already being the docker address, it has nothing to add.

**3. Codexify's own wizard writes the file at mode 0644.** There is no
`chmod` anywhere in it. The file holds `GUARDIAN_API_KEY` and `NEO4J_PASS`,
and 0644 is world-readable. `docs/WIZARD.md` §2.2 requires **0600**.

**4. Re-running Codexify's wizard drops the operator's own keys.** It
re-seeds the file from `.env.template` and carries over exactly one value,
`GUARDIAN_API_KEY`. Anything an operator added by hand survives only in
`.env.bak`. A wizard that loses a key and then reports success is the false
success this repository keeps refusing.

---

## 4. The local inference runtime

The workers do not talk to a cloud provider. They talk to a **local
OpenAI-compatible endpoint**, and they must be told two things about it: where
it is, and which model it serves.

Codexify's presets — `whooshd-mlx`, `ollama`, `lmstudio`,
`custom-openai-compatible` — are the values `LOCAL_RUNTIME_PRESET` may take.
A value outside that set does not resolve.

The model id is not a guess. The endpoint advertises what it serves, on
`/v1/models`, and that is what belongs in `LOCAL_CHAT_MODEL`. Codexify has a
`default_local_runtime_preset_id()` that would know the platform default — it
is imported into its setup wizard and **never called**, so the
platform-aware default it promises never happens. Asking is better than
guessing.

A local runtime that is not running is not a fatal error for the
configuration — it means `LOCAL_CHAT_MODEL` is empty, and the report says so.

---

## 5. How Codexify's own setup falls short

`python -m guardian.cli.memoryos_cli setup` is the upstream wizard. It is a
**Textual TUI** — it needs `pip install '.[tui]'` — and it **stops after
writing `.env`**. It does not start a service, does not run a migration, does
not verify what it wrote, and does not set the file's mode.

Its `classify_setup_readiness()` is a good piece of work — a fifteen-state
ladder that classifies a machine's readiness — and it has **no production
caller**. It is a library with a test suite and nothing else.

`genesis codexify` does the whole thing, and does the four things above
differently:

| Codexify's wizard | `genesis codexify` |
|---|---|
| a Textual TUI | a plain terminal command, stdlib only |
| writes 0644 | writes **0600** |
| re-seeds from the template, dropping the operator's keys | **merges** the operator's file over the template |
| leaves `LOCAL_CHAT_MODEL` unset | writes it, from what the endpoint advertises |
| leaves `LOCAL_BASE_URL` pointing at the container's own localhost | writes the docker address, in Codexify's canonical shape |
| never starts anything | drives `docker compose up`, then waits for it |
| never verifies | one row per health endpoint |

---

## 6. The verification

These are the endpoints that answer, and what each one proves.

| endpoint | auth | proves |
|---|---|---|
| `GET :8888/ping` | none | the application process is up — this is compose's own healthcheck |
| `GET :8888/healthz` | none | **the migrations landed** — it reports `projects_table_exists` and `chat_threads_table_exists` |
| `GET :8888/health` | `X-API-Key` | the core health envelope |
| `GET :8888/health/chat` | none | Redis is reachable and the chat worker's heartbeat is fresh (≤10 s) |
| `GET :8888/health/llm` | none | the provider answers, and the model is advertised |
| `GET :8888/health/vector` | none | the vector store does a live add and search |
| `GET :5173` | none | the interface is serving |

`/healthz` is the one that matters most. It is what separates "the container
started" from "the schema exists" — and a container that started with no
migration is a backend that answers every request with an error.

**The rule the rows obey:** a row that reports a write is a write report, not
a readiness report. "Wrote `LOCAL_CHAT_MODEL`" is a fact. "Codexify is ready"
is a claim, and only a request that came back with the right answer earns it.

---

## 7. The command

```
genesis codexify install     get the latest upstream code — works on an empty machine,
                              then chains straight into setup
genesis codexify setup       detect, configure, bring up, wait, verify
genesis codexify update      move an existing install to the latest upstream main
genesis codexify status      what is true about it right now; writes nothing
genesis codexify down        stop the stack
```

`install` is the one verb that runs without an installation — its whole job
is a machine that has nothing. It initializes the submodule, fetches
upstream `main`, checks out the latest (not the parent's pinned commit),
verifies the result is a real install, and then runs `setup` with the same
arguments rather than stopping at the code. One command, and a fresh
machine ends at a running stack or an honest failure — never at
installed-but-not-started.

`update` is its conservative sibling: it refuses without an install (exit
**3**) and refuses a dirty working tree (exit **6**) rather than discard
local edits, and reports the commit movement, old → new. Both treat the
network as a prerequisite: an unreachable upstream is exit **5**, and
nothing is written on a failure.

`setup` takes `--codexify DIR`, `--preset`, `--model`, `--allow-cloud`,
`--profile`, `--services`, `--no-up`, `--no-build`, `--dry-run`, `--timeout`,
`--with-provider-key`, `--replace-key`, `--key-file`, and `--provider`;
`down` takes `--volumes`. Run `genesis codexify --help` for the full list.

`setup --native` also takes `--launcher desktop` (or `--launcher
applications`): once every check has passed, it writes a double-clickable
`Codexify.app` bundle — a shell script that runs `genesis codexify setup`
if the backend is not answering, then opens the interface in a browser.
It is made only when the stack is up, because a launcher that opens a
browser at a dead stack is a false success on the Desktop.

### Which way it runs

**Native is the default**, and no flag is needed to get it.

```
genesis codexify setup                # native
genesis codexify setup --docker       # the containers upstream ships
genesis codexify setup --native       # the same as the default, said out loud
```

Native is the default because it needs no container runtime, no per-platform
licence and no privileged daemon install — and it is the only one of the two
that works on a machine without Docker at all. §9 is what it takes.

**But an installation keeps the mode it has.** A compose installation has
`host.docker.internal` in its `.env` and the containers behind it; rewriting
that to a host address under a running stack breaks the thing it was
serving. So a run without a flag *follows the installation*, and a flag is
how the mode is changed deliberately.

That is the idempotency rule this repository holds to — running the wizard
twice must produce an empty plan, and a wizard that flips modes between
runs does not. The mode is a property of the installation, and the
installation is where it is recorded.

### The dry run

**`--dry-run` writes nothing** and prints the diff with every secret's value
replaced by a placeholder, so the operator can see what would change without
the diff carrying a credential into a ticket.

### The timeout

**`--timeout` matters.** `docker compose up -d` returns when the containers
are **created**, not when they are **healthy**. The backend then waits for
Postgres, verifies eight tables, seeds, and execs uvicorn — and the frontend
only starts after the backend's healthcheck passes. The wizard polls until
the backend answers, up to the timeout, and reports the last thing it saw.
Probing once, immediately, would report a perfectly good cold stack as
broken.

The exit codes are the bootstrap ladder's, so a caller that already reads a
GENESIS exit code reads this one without learning a second table:

| exit | meaning |
|---|---|
| 0 | set up, and every check passed |
| 1 | set up, and something did not — what succeeded, what failed, what is still manual |
| 2 | usage error |
| 3 | no Codexify installation found |
| 5 | a prerequisite is missing (no docker; or no postgres or redis) |
| 6 | the configuration would overwrite something the wizard does not own — a path that is not gitignored |
| 8 | the provider key did not authenticate. Nothing was written |
| 9 | the wizard's own precondition failed (no TTY, and no `--key-file`) |

### The secrets

`GUARDIAN_API_KEY`, `NEO4J_PASS` and `GUARDIAN_SESSION_SECRET` belong to
Codexify, not to a provider. **The operator is never asked for any of
them** — the wizard generates them, because compose refuses to start without
the first, `graph-init` exits 1 without the second, and the template ships
the third as `replace-with-long-random-secret`. An existing, non-placeholder
value is kept: a second run must not rotate the key under a running stack.

Only a provider key is a human's to enter, and only once. `setup` never asks
for one by default — the stack runs local-first, and the credential wizard
(`setup.sh --wizard`) already owns that key. `--with-provider-key` asks once,
verifies it against the endpoint, and refuses with exit 8 if it does not
authenticate. The key is reported by **fingerprint**, never by value.

---

## 8. What is still manual

Even after `genesis codexify setup`:

- **Docker itself is not installed by the wizard.** It reports that it is
  missing and says where to get it. Installing Docker is an operator's call,
  not a wizard's.
- **The first boot downloads the embedding model**, which takes a few
  minutes.
- **A local runtime is not started by the wizard.** It reports which one it
  found and which model it serves.

---

## 9. The native path — no Docker

Docker is the reference setup for Codexify, and it is a good one. But it is
not the only one, and a machine without Docker is still a machine. **This is
the default mode** (§7).

```
genesis codexify setup          # native, on a machine that has never been set up
genesis codexify status
genesis codexify down
```

The same backend, the same database schema and the same chat worker — as
processes on the host instead of containers.

### What it needs

| | compose | native |
|---|---|---|
| Postgres | `postgres:15` container | **mandatory** — `brew install postgresql@15`, then start it |
| Redis | `redis:7` container | **mandatory** — `brew install redis`, then start it |
| Neo4j | compose *requires* it: the backend waits on `graph-init` | **not needed** — graph writes default off, and the driver is not imported |
| Python | none on the host | a 3.11 venv inside the installation |
| packages | pinned in the image | the pinned set **minus the heavy ones**, plus the four `requirements/base.in` leaves out |
| embedding model | `model-prep` fetches it into the container | **not needed** — the mock backend is used |
| processes | compose supervises 22 | one supervisor, six processes |

### The three traps Docker hides

These are why the native path is not "the compose file with the word docker
removed". Each one is a real thing that fails silently or fatally.

**1. The addresses change.** Inside a container the local runtime is at
`host.docker.internal`. On the host it is at `127.0.0.1`. **One `.env`
cannot serve both** — the native one carries the host addresses.

**2. Redis has to be named.** The default `REDIS_URL` is
`redis://redis:6379/0` — the compose *service name*. Natively that name
does not resolve, and the chat queue is unavailable with no obvious reason.

**3. The config source has to be said.** `CODEXIFY_CONFIG_SOURCE` defaults
to `strict`, and strict compares the core settings against the legacy ones.
Their defaults disagree — core says `LLM_PROVIDER=local`, legacy says
`AI_BACKEND=groq` — and strict turns that disagreement into a startup
abort. Compose sets `core`; the native path has to as well.

### The fourth trap, which is not Docker's fault

The application's lifespan constructs the vector store, and the vector store
constructs an embedder, and the **default** embedder wants
`sentence-transformers` — which wants torch, which is a gigabyte. So the
native path starts with the **mock** embedding backend:

```
CODEXIFY_EMBEDDINGS_BACKEND=mock
CODEXIFY_VECTOR_STORE=faiss
```

`faiss` is the lighter of the two stores the app supports (there is no
pgvector option in this path), and the mock backend needs only numpy. The
cost is honest and worth stating: **vector search over documents returns
meaningless vectors** until the real embedding backend is installed. Chat,
threads, memory and the schema are unaffected.

### The environment

It lives inside the installation, in `codexify-core/venv` — a directory that
is already gitignored, and removing it removes the whole thing.

The supervisor is `tools/codexify_supervisor.py`. It starts the backend and
the workers, keeps their pids and their logs under `codexify-core/.native/`,
and stops everything when it is signalled. It is a **user** process: no
`sudo`, and nothing that has to be re-rooted to keep itself alive.

### What the wizard will not do

It will not install system packages. When Postgres or Redis is missing it
says exactly what to run and stops:

```
brew install postgresql@15 redis
```

Installing packages on someone's machine is the operator's call. The wizard
reports the gap and names the fix; it does not take it.
