#!/usr/bin/env python3
"""NODE DAEMON — the heartbeat, on an interval.

A node claims liveness by writing a heartbeat and nothing else. This daemon
does that: a fixed payload, a monotonic sequence number, an interval, and a
write to the node-local record. It carries no credential and reads no user
file.

It writes **two** node-local records, and the second is the node's report to
the spine. The heartbeat log is the node's own machine record. The event log
(`spine_events.jsonl`) holds one **native event** per beat — the shape the
Studio's reducer reads when it reaches for this node, the way it reaches for
Bruce's ledger. The node computes neither an event id nor a chain: the spine
has exactly one writer, and it is the spine. This daemon never writes into
the spine itself, and it never pushes.

It runs as a **user** service — a `systemd --user` unit on Linux, a
`LaunchAgent` on macOS — never as a system one. No `sudo`, and it survives
`omarchy-update`, because a machine that has to be re-rooted to keep its own
node alive is not a node.

    python3 tools/node_daemon.py --interval 60
    python3 tools/node_daemon.py --once        # one heartbeat, then out

Standard library only.
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import node  # noqa: E402


def run_once(sequence: int) -> dict:
    """One heartbeat, and the event that reports it.

    Two node-local writes: the heartbeat the node keeps for itself, and
    the native event the Studio pulls when it reaches for this node. The
    heartbeat is written first, so the node's own record is never older
    than the event that says the node is alive.
    """
    payload = node.heartbeat_payload(sequence)
    node.write_heartbeat(payload)
    event = node.write_spine_event(payload)
    payload["event_id"] = event["provenance"]["local_event_id"]
    return payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="node_daemon.py",
        description="The GENESIS node heartbeat daemon.")
    parser.add_argument("--interval", type=int, default=node.DEFAULT_INTERVAL,
                        help="seconds between heartbeats (default 60)")
    parser.add_argument("--once", action="store_true",
                        help="write one heartbeat and exit")
    parser.add_argument("--max", type=int, default=0,
                        help="stop after this many heartbeats (0: never)")
    args = parser.parse_args(argv)

    if args.interval < 5:
        print("node_daemon: the interval must be at least 5 seconds.",
              file=sys.stderr)
        return 2

    stopping = {"now": False}

    def stop(signum, frame):
        stopping["now"] = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, stop)
        except (ValueError, OSError):
            pass

    sequence = node.last_sequence()
    count = 0
    while not stopping["now"]:
        sequence += 1
        count += 1
        payload = run_once(sequence)
        print(f"heartbeat {payload['seq']} {payload['ts']} "
              f"node={payload['node']} joined={payload['tailnet_joined']} "
              f"event={payload.get('event_id')}",
              flush=True)
        if args.once:
            return 0
        if args.max and count >= args.max:
            return 0
        # Sleep in short slices so a signal is answered promptly.
        deadline = time.time() + args.interval
        while not stopping["now"] and time.time() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.time())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
