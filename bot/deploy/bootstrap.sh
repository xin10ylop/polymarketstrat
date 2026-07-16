#!/usr/bin/env bash
# One-shot droplet setup. Run as root from anywhere:
#   bash bootstrap.sh <git-clone-url-or-skip-if-repo-already-at-/opt/polymarketstrat>
set -euo pipefail

REPO_URL="${1:-}"
DIR=/opt/polymarketstrat

apt-get update -qq
apt-get install -y -qq python3-venv git chrony
systemctl enable --now chrony    # accurate clock: window boundaries depend on it

if [ ! -d "$DIR" ]; then
  if [ -z "$REPO_URL" ]; then
    echo "repo not found at $DIR and no clone URL given" >&2
    exit 1
  fi
  git clone "$REPO_URL" "$DIR"
fi

cd "$DIR"
python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q aiohttp

echo "--- preflight ---"
venv/bin/python -m bot.preflight

# NOTE: do not touch polybot.service — that name belongs to a different project on this host
cp bot/deploy/polybot-toll.service bot/deploy/polybot-snipe.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now polybot-toll polybot-snipe
sleep 5
systemctl --no-pager status polybot-toll polybot-snipe | grep -E "polybot|Active"
echo
echo "OK. Watch:      journalctl -u polybot-toll -f   (or polybot-snipe)"
echo "Daily report:   cd $DIR && BOT_DATA_DIR=bot/data/toll venv/bin/python -m bot.report 7"
