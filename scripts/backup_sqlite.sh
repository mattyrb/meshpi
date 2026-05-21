#!/usr/bin/env bash
#
# meshpi: daily SQLite backup using the online .backup command.
#
# Safe to run while meshpi is writing to the live database because SQLite's
# .backup uses its own page-by-page lock instead of copying the file.
#
# Backups land in a `backups/` directory next to the live DB by default and
# include the UTC date in the filename. Files older than RETENTION_DAYS
# (default 30) are pruned.
#
# Usage:
#   scripts/backup_sqlite.sh                       # uses defaults
#   DB_PATH=/path/to/db.sqlite scripts/backup_sqlite.sh
#   BACKUP_DIR=/elsewhere/backups RETENTION_DAYS=60 scripts/backup_sqlite.sh

set -euo pipefail

DB_PATH="${DB_PATH:-/mnt/meshpi-data/meshpi.db}"
BACKUP_DIR="${BACKUP_DIR:-$(dirname "$DB_PATH")/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"

if [[ ! -f "$DB_PATH" ]]; then
    echo "ERROR: db not found at $DB_PATH" >&2
    exit 2
fi
if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "ERROR: sqlite3 not installed. sudo apt install -y sqlite3" >&2
    exit 2
fi

mkdir -p "$BACKUP_DIR"

stamp="$(date -u +%Y-%m-%d)"
out="$BACKUP_DIR/meshpi-${stamp}.db"

# .backup is the safe way to copy a live DB; cp can produce a corrupt copy
# when the writer is mid-transaction.
sqlite3 "$DB_PATH" ".backup '$out'"
echo "wrote $out ($(du -h "$out" | cut -f1))"

# Prune older copies.
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'meshpi-*.db' \
    -mtime "+${RETENTION_DAYS}" -print -delete | \
    sed 's/^/pruned /' || true
