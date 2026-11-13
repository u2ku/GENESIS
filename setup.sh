#!/usr/bin/env bash
# GENESIS — the bootstrap entry point.
#
# Three rungs, and the default is the safe one:
#
#   ./setup.sh              inspect — read the machine, change nothing
#   ./setup.sh --dry-run    the diff, and a receipt that unlocks the apply
#   ./setup.sh --apply      change the machine, against a matching receipt
#
# The wizard is a mode, not a second ladder:
#
#   ./setup.sh --wizard --dry-run    prompt once, verify the key, show the diff
#   ./setup.sh --wizard --apply      the same, applied
#
# This wrapper does the cheap pre-flight — a python3 that is new enough, and
# a repository that is actually a GENESIS checkout — and then hands over to
# tools/bootstrap.py, where all the logic lives and where it can be tested.
#
# Dependencies: bash and python3. Nothing else, and no network.

set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

die() {
  echo "genesis-bootstrap: $*" >&2
  exit "${2:-2}"
}

# -- python3, and new enough.
if ! command -v python3 >/dev/null 2>&1; then
  die "python3 is not on PATH.
  nothing was changed.
  install python3 (3.9 or newer) and run this again." 5
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  die "python3 is too old — 3.9 is the minimum.
  nothing was changed." 5
fi

# -- this really is a GENESIS checkout.
if [ ! -f "$HERE/genesis" ] || [ ! -d "$HERE/src" ] || [ ! -f "$HERE/tools/bootstrap.py" ]; then
  die "this is not a GENESIS checkout — genesis, src/ and tools/bootstrap.py are expected.
  nothing was changed." 3
fi

exec python3 "$HERE/tools/bootstrap.py" "$@"
