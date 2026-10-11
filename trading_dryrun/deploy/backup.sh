#!/usr/bin/env bash
# Daily consistent copy of the SQLite database, keeping 14 days. For off-machine safety also copy it to S3 or another host.
set -euo pipefail
DB=/opt/dryrun/data/dryrun.db
OUT=/opt/dryrun/data/backups
mkdir -p "$OUT"
sqlite3 "$DB" ".backup '$OUT/dryrun-$(date +%F).db'"
gzip -f "$OUT/dryrun-$(date +%F).db"
find "$OUT" -name 'dryrun-*.db.gz' -mtime +14 -delete
