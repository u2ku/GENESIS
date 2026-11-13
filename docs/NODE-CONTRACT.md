# NODE CONTRACT — how any machine becomes a Luna node
<!-- visibility: sam-readable -->

**Status: v0.1, implemented.** This is the v0.1 contract built. The
design artifact it came from is the one Zac reviewed; this document says
what the code actually does, and where it lives.

A node is not a second Luna. It is a sensing and execution endpoint with a
heartbeat and a one-way export channel.

| the contract | the code |
|---|---|
| the join flow | `src/node.py` (`join_tailnet`, `install_command`), `tools/bootstrap.py` (the `tailscale` and `tailnet-join` actions) |
| the heartbeat daemon | `tools/node_daemon.py`, `src/node.py` (`heartbeat_payload`, `write_heartbeat`) |
| the one-way export | `tools/spine_export.py` (the tool), `src/node.py` (`check_export`, `check_export_target` — the rules themselves live in the contract, and the tool calls them) |
| the trigger | `genesis landing <node>` — `src/harness.py` (`t_landing`, `landing_join`), `genesis` (`cmd_landing`) |
| the visibility contract | `src/visibility.py`, and the marker check in `src/node.py` |

---

## 1. The join flow

- **Tailscale is installed by the system package manager only** — `pacman`
  on Omarchy and Arch, `apt` on Debian, `brew` on macOS. Never a vendored
  binary, never a `curl | sh` installer: a binary in the repo would break
  the machine-independence the portability contract rests on. The command
  is *returned* by `src/node.py:install_command()` rather than run behind
  the operator's back — installing a system package needs privileges the
  agent does not have.
- **The auth key is entered once, by the wizard**, and written to
  `~/.config/luna-node/auth.key` at mode 0600 — outside every repository,
  never in a log, a receipt or a manifest entry.
- **The key is passed as a path.** `tailscale up --auth-key=file:<path>`
  puts the *path* in the argument and never the value. Tailscale 1.104.4
  accepts the `file:` form; this was verified, not assumed.
- **The key's properties are recorded, not the key**: single-use,
  pre-authorized, tag-scoped, expiring. Ephemeral is a decision, taken
  from the node's role rather than assumed.
- **Failure is reported at join time.** A machine with no Tailscale gets
  the exact command and a human; a machine with no key file is told that
  the wizard establishes that secret and nothing else does. A dead tailnet
  is never silently continued past.

## 2. The heartbeat daemon

- A **user** service — a `systemd --user` unit on Linux, a `LaunchAgent`
  on macOS. Never a system one: no `sudo`, and it survives
  `omarchy-update`.
- A heartbeat every **60 seconds** by default (`--interval`), and one on
  demand (`--once`).
- The payload is fixed: **node id, hostname, OS, uptime, service states,
  and a monotonic sequence number**. No credentials, no environment dump,
  no user file contents. The tailnet address is **not** in the payload —
  a `100.x` address in a record is a private-network address by the
  visibility contract's own reading. The node's address lives in the
  node-local profile.
- The record is **node-local**: `~/.local/state/genesis/heartbeat.jsonl`
  for machines, and `record.md` beside it for humans, both at mode 0600.
- The heartbeat is the node's **only** claim to liveness. A silent node is
  a dead node, and nothing is inferred beyond the last heartbeat.

## 3. The one-way spine export

- The node writes its **own** record. The canonical `merged.md` on the
  spine has exactly one writer, and it is the spine.
- Export is a **deliberate push** of a curated block, on demand, by hand.
  There is no sync, no backflow, no background merge.
- The export **refuses a block with no visibility marker** — untagged
  content is readable as written, so it is refused rather than published
  by accident.
- The export **refuses the spine's own record** as a target. The target
  must be an inbox: a place the spine's writer reads from. The dry run
  applies the same rules, so it cannot approve a push that would be
  refused.
- Curated backflow (spine → node) is a separate explicit push channel,
  and it is not part of this contract.

## 4. The trigger: `landing`

`genesis landing <node>` is what Genesis can call to prepare a machine,
and it is now also the trigger for the tailnet:

1. the readiness checks — harness, card, grammar, geomancy, identity, and
   that the guard rail holds;
2. **the join**, if the node is not already on the tailnet.

`--no-join` (and `join: false` in the tool schema) reports readiness
without touching anything, which is what the bootstrap's readiness probe
uses.

The split is deliberate: **the wizard establishes the secret, and `landing`
performs the action with it.** Neither asks the operator for it twice.

## 5. What the node does not do

- It does not become a second Luna.
- It does not write to the spine's record.
- It does not carry the mesh channel. Whether a portable node should is
  still open with Zac (the contract's open item 2), and this version
  settles nothing about it.
- It does not install anything system-wide without the operator's word.

## 6. Open items for Zac

These are the v0.1 contract's open items, still open:

1. **Heartbeat interval and retention.** 60 seconds and a bounded log
   (10,000 lines) are the defaults here, not decisions.
2. **Whether the node daemon carries execution capability** (a
   run-command channel) or stays sensing-only. This version is
   sensing-only.
3. **Whether Sam's iMac is the first contract node,** or Bruce remains the
   reference implementation. Bruce's heartbeat daemon is the shape this
   one follows; nothing here has been run against Sam's machine.
