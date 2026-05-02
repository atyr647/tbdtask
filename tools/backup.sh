#!/usr/bin/env bash
# Backup script for tbdtask — works for both SQLite (AppImage) and
# Postgres (hosted) deployments.
#
# Usage:
#   tools/backup.sh                     # backup to data/backups/
#   tools/backup.sh /path/to/dest.db    # backup to specific path
#
# Environment:
#   TBDTASK_DATA_DIR  — data directory (default: data/)
#   DATABASE_URL      — DB connection string (default: sqlite:///data/tbdtask.db)
#   BACKUP_DIR        — backup destination directory (default: data/backups/)

set -euo pipefail

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
DATA_DIR="${TBDTASK_DATA_DIR:-data}"
DATABASE_URL="${DATABASE_URL:-sqlite://${DATA_DIR}/tbdtask.db}"
BACKUP_DIR="${BACKUP_DIR:-${DATA_DIR}/backups}"

DEST="${1:-${BACKUP_DIR}/tbdtask-${TIMESTAMP}.bak}"

mkdir -p "$(dirname "$DEST")"

# Detect backend from DATABASE_URL.
if [[ "$DATABASE_URL" == postgresql* ]] || [[ "$DATABASE_URL" == postgres://* ]]; then
    # Postgres backup via pg_dump.
    echo "[backup] Postgres detected — using pg_dump..."
    # pg_dump accepts libpq URLs, not SQLAlchemy driver URLs.
    PG_URL="${DATABASE_URL/postgresql+psycopg:\/\//postgresql://}"
    PG_URL="${PG_URL/postgres:\/\//postgresql://}"
    pg_dump "$PG_URL" -Fc -f "$DEST"
    echo "[backup] Written to: $DEST"
elif [[ "$DATABASE_URL" == sqlite://* ]]; then
    # SQLite backup via file copy.
    DB_PATH="${DATABASE_URL#sqlite://}"
    if [[ ! -f "$DB_PATH" ]]; then
        echo "[backup] Database not found: $DB_PATH" >&2
        exit 1
    fi
    # WAL-aware copy: checkpoint first, then copy.
    if command -v sqlite3 >/dev/null 2>&1; then
        sqlite3 "$DB_PATH" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null 2>&1 || true
    fi
    cp "$DB_PATH" "$DEST"
    echo "[backup] Written to: $DEST"
else
    echo "[backup] Unknown DATABASE_URL scheme: $DATABASE_URL" >&2
    exit 1
fi
