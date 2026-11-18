#!/data/data/com.termux/files/usr/bin/sh
# PHONE NODE HEARTBEAT — the phone's report to the Studio.
#
# Runs whenever the phone is on: started at boot by boot.sh, held awake by
# the supervisor's wake lock, looping until Termux is stopped.
#
# The data path is the operator's tailnet: the Studio is addressed by its
# tailnet IP — no DNS, no public internet. Configure STUDIO below for your
# own deployment; defaults point at the operator's Studio.
#
# The payload is slow_state telemetry: lat/lon, battery, charging, wifi.
# No credentials, no file contents, no message bodies.

STUDIO="${LUNA_STUDIO:-http://100.71.144.117:8765/telemetry}"
INTERVAL="${LUNA_INTERVAL:-180}"          # seconds between beats
NODE_NAME="${LUNA_NODE_NAME:-PieratsPhone}"
LOG="$HOME/.luna-node/log/heartbeat.log"

mkdir -p "$HOME/.luna-node/log"
LAST_LAT=""; LAST_LON=""

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$LOG"; tail -n 500 "$LOG" > "$LOG.t" && mv "$LOG.t" "$LOG"; }

get_location() {
    # passive provider: cheap, no active GPS lock. On failure we keep the
    # last fix rather than reporting nothing — a stale fix is honest if
    # the node says so, and the receiver stamps received_at either way.
    FIX=$(termux-location -p passive 2>/dev/null | jq -r '
      if .latitude then "\(.latitude) \(.longitude)" else empty end')
    if [ -n "$FIX" ]; then
        LAST_LAT="${FIX%% *}"; LAST_LON="${FIX##* }"
        echo "$FIX"
    elif [ -n "$LAST_LAT" ]; then
        echo "$LAST_LAT $LAST_LON"
    fi
}

beat() {
    # location (with last-fix fallback)
    LOC=$(get_location)
    LAT=$(echo "$LOC" | awk '{print $1}')
    LON=$(echo "$LOC" | awk '{print $2}')

    # battery + charging
    BATTERY_JSON=$(termux-battery-status 2>/dev/null)
    BATTERY=$(echo "$BATTERY_JSON" | jq -r '.percentage // empty')
    CHARGING=$(echo "$BATTERY_JSON" | jq -r 'if .charging then true else false end')

    # wifi ssid
    WIFI=$(termux-wifi-connectioninfo 2>/dev/null | jq -r '.ssid // empty')

    # build the payload — the slow_state shape the Studio digest parses
    PAYLOAD=$(jq -n \
        --arg device "$NODE_NAME" \
        --arg wifi "$WIFI" \
        --argjson battery "${BATTERY:-0}" \
        --argjson charging "$CHARGING" \
        --argjson lat "${LAT:-null}" --argjson lon "${LON:-null}" \
        '{type:"slow_state", device:$device, wifi:$wifi,
          battery:$battery, charging:$charging,
          lat:$lat, lon:$lon} | with_entries(select(.value != null and .value != ""))')

    # post; failure is soft — next beat retries
    CODE=$(curl -s -m 15 -o /dev/null -w "%{http_code}" \
        -H "Content-Type: application/json" \
        -X POST --data "$PAYLOAD" "$STUDIO")
    log "beat battery=${BATTERY:-?}% charging=$CHARGING wifi=${WIFI:-?} lat=${LAT:-none} -> http $CODE"
}

# The wake lock is owned by boot.sh (the supervisor), not here: two scripts
# each holding a lock is two holders of the same claim.

log "phone node heartbeat starting (interval ${INTERVAL}s)"

while true; do
    beat
    [ "$1" = "--once" ] && break
    # jittered sleep so beats don't synchronize with anything
    JITTER=$((RANDOM % 20))
    sleep $((INTERVAL + JITTER))
done
