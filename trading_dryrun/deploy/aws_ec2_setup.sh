#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu 24.04 EC2 instance (region ap-south-1, Mumbai). Run as the default `ubuntu` user:
#     scp -r trading_dryrun ubuntu@<ip>:~/ && ssh ubuntu@<ip> 'bash ~/trading_dryrun/deploy/aws_ec2_setup.sh'
set -euo pipefail
APP=/opt/dryrun
SRC="$(cd "$(dirname "$0")/.." && pwd)"

sudo timedatectl set-timezone Asia/Kolkata
sudo apt-get update -y
sudo apt-get install -y python3-venv sqlite3

# a 1 GB instance (t2/t3.micro) is enough for this app, but swap prevents out-of-memory kills while installing
if ! swapon --show | grep -q swapfile; then
  sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

sudo mkdir -p "$APP/data" && sudo chown -R ubuntu:ubuntu "$APP"
cp -r "$SRC/dryrun" "$SRC/requirements.txt" "$SRC/scripts" "$APP/"
[ -f "$APP/.env" ] || { cp "$SRC/.env.example" "$APP/.env"; chmod 600 "$APP/.env"; echo ">> edit $APP/.env (Dhan credentials, FEED_MODE=dhan, password)"; }
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --quiet -r "$APP/requirements.txt"

sudo cp "$SRC/deploy/dryrun.service" /etc/systemd/system/dryrun.service
sudo cp "$SRC/deploy/backup.sh" /usr/local/bin/dryrun-backup && sudo chmod +x /usr/local/bin/dryrun-backup
( crontab -l 2>/dev/null | grep -v dryrun-backup; echo "30 16 * * 1-5 /usr/local/bin/dryrun-backup" ) | crontab -
sudo systemctl daemon-reload
sudo systemctl enable dryrun
echo
echo "Done. Next: nano $APP/.env ; sudo systemctl start dryrun ; journalctl -u dryrun -f"
echo "Open the dashboard from your computer through an SSH tunnel:  ssh -L 8000:localhost:8000 ubuntu@<ip>  then http://localhost:8000"
