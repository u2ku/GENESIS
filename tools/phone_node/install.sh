#!/data/data/com.termux/files/usr/bin/sh
# ONE-LINE INSTALL — the phone fetches and runs this; it does the rest.
#   curl $STUDIO/node/install.sh | sh
# STUDIO defaults to the operator's Studio; override with LUNA_STUDIO.
set -e

BASE="${LUNA_STUDIO:-http://100.71.144.117:8765}/node"

echo "== packages"
pkg install -y termux-api curl jq || true

echo "== node dir + scripts"
mkdir -p ~/.luna-node/log ~/.termux/boot
for f in heartbeat.sh boot.sh; do
  curl -s "$BASE/$f" -o ~/.luna-node/$f
done
chmod +x ~/.luna-node/*.sh

echo "== boot hook"
printf '#!/data/data/com.termux/files/usr/bin/sh\nexec sh $HOME/.luna-node/boot.sh\n' \
  > ~/.termux/boot/luna-node
chmod +x ~/.termux/boot/luna-node

echo "== start"
sh ~/.luna-node/boot.sh
echo "done — phone node running"
