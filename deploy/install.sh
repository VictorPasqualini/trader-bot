#!/usr/bin/env bash
# Provision a fresh Ubuntu 22.04/24.04 host to run Pouch unattended.
#
# Run it as root on the host, from a copy of this repository:
#
#     sudo bash deploy/install.sh /path/to/checkout
#
# It is safe to re-run: every step either creates something missing or updates
# something in place.

set -euo pipefail

SOURCE="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
TARGET=/opt/pouch
SERVICE=pouch

if [[ $EUID -ne 0 ]]; then
    echo "run this with sudo" >&2
    exit 1
fi

echo "==> packages"
apt-get update -qq
# python3-venv is separate from python3 on Debian-family images, and the build
# toolchain is what lets pip fall back to a source build when a wheel for this
# architecture is missing - which is the normal case on ARM hosts.
apt-get install -y -qq python3 python3-venv python3-dev build-essential rsync curl

echo "==> swap"
# The free tiers ship 1 GB of RAM. pandas plus a walk-forward across the whole
# book briefly needs more than that, and an out-of-memory kill in the middle of
# a poll is a missed candle close, which is the one thing the coverage gate
# counts against you. Swap turns that failure into a slow minute instead.
if [[ ! -f /swapfile ]]; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap -q /swapfile
    swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> user"
id -u pouch >/dev/null 2>&1 || useradd --system --home "$TARGET" --shell /usr/sbin/nologin pouch

echo "==> code"
mkdir -p "$TARGET"
# Everything but the local state: the database and the keys belong to the host,
# not to the checkout, so a redeploy must not overwrite them.
rsync -a --delete \
    --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
    --exclude 'data' --exclude '.env' \
    "$SOURCE/" "$TARGET/"
mkdir -p "$TARGET/data"

echo "==> virtualenv"
[[ -d "$TARGET/.venv" ]] || python3 -m venv "$TARGET/.venv"
"$TARGET/.venv/bin/pip" install -q --upgrade pip
"$TARGET/.venv/bin/pip" install -q -r "$TARGET/requirements.txt"

echo "==> permissions"
chown -R pouch:pouch "$TARGET"
# The keys are readable by their owner and nobody else.
[[ -f "$TARGET/.env" ]] && chmod 600 "$TARGET/.env"

echo "==> clock"
# Binance rejects a signed request whose timestamp is outside its recv window,
# so a drifting clock looks exactly like a broken API key.
timedatectl set-ntp true || true

echo "==> service"
install -m 644 "$TARGET/deploy/pouch.service" "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable "$SERVICE"

if [[ ! -f "$TARGET/.env" ]]; then
    install -o pouch -g pouch -m 600 "$TARGET/.env.example" "$TARGET/.env"
    echo
    echo "Put your keys in $TARGET/.env, then: systemctl start $SERVICE"
    exit 0
fi

systemctl restart "$SERVICE"
sleep 3
systemctl --no-pager --lines=10 status "$SERVICE" || true
echo
echo "Tunnel the dashboard from your own machine:"
echo "    ssh -L 8777:127.0.0.1:8777 <user>@<host>"
