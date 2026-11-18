#!/data/data/com.termux/files/usr/bin/sh
# PHONE NODE BOOT — one entry point, started by ~/.termux/boot/ at power-on.
#
# Owns the wake lock (one holder, not two scripts each grabbing it) and
# starts the phone's daemons:
#   - heartbeat.sh   slow_state telemetry to the Studio (tailnet only)
#   - listen.sh      optional call/listener script, if present
#
# Idempotent: if the daemons are already running (boot script fired twice),
# the old ones are left alone and this exits.

NODE_DIR="$HOME/.luna-node"
LOG="$NODE_DIR/log/boot.log"

mkdir -p "$NODE_DIR/log"

# One wake lock for everything the phone node runs.
termux-wake-lock

already_running() { pgrep -f "$1" >/dev/null; }

if ! already_running "luna-node/heartbeat.sh"; then
    nohup sh "$NODE_DIR/heartbeat.sh" >> "$NODE_DIR/log/heartbeat-daemon.log" 2>&1 &
    echo "$(date -u +%FT%TZ) heartbeat started" >> "$LOG"
fi

# Optional listener (call playback, notifications — deployment-specific).
# Configure with LISTEN_CMD, e.g. "sh $NODE_DIR/listen.sh user@studio-host".
if ! already_running "luna-node/listen.sh" && [ -n "$LISTEN_CMD" ]; then
    nohup $LISTEN_CMD >> "$NODE_DIR/log/listen.log" 2>&1 &
    echo "$(date -u +%FT%TZ) listener started" >> "$LOG"
fi
