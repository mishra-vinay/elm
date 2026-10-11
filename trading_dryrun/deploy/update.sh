#!/usr/bin/env bash
# Update the installed copy from this checkout, restart, and show whether it came up.
#   cd ~/elm && git pull && bash trading_dryrun/deploy/update.sh
set -euo pipefail
APP=/opt/dryrun
SRC="$(cd "$(dirname "$0")/.." && pwd)"
rm -rf "$APP/dryrun" && cp -r "$SRC/dryrun" "$SRC/requirements.txt" "$SRC/scripts" "$APP/"
"$APP/venv/bin/pip" install --quiet -r "$APP/requirements.txt"
sudo systemctl restart dryrun
sleep 4
if curl -fsS localhost:8000/health >/dev/null; then
  echo "OK: dashboard is up on localhost:8000 (open it through the SSH tunnel)"
else
  echo "NOT UP. Last log lines:"; sudo journalctl -u dryrun -n 25 --no-pager; exit 1
fi
