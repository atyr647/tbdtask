#!/usr/bin/env bash
# Restore script for tbdtask — works for both SQLite (AppImage) and
# Postgres (hosted) deployments.
#
# Usage:
#   tools/restore.sh /path/to/backup.bak    # restore from backup file
#
# Environment:
#   TBDTASK_DATA_DIR  — data directory (default: data/)
#   DATABASE_URL      — DB connection string (default: sqlite:///data/tbdtask.db)
#   BACKUP_FILE       — backup file path (alternative to positional arg)

set -euo pipefail

BACKUP_FILE="${1:-${BACKUP_FILE:-}}"
if [[ -z "$BACKUP_FILE" ]]; then
    echo "[restore] Usage: tools/restore.sh <backup-file>" >&2
    echo "[restore] Or set BACKUP_FILE environment variable" >&2
    exit 1
fi

if [[ ! -f "$BACKUP_FILE" ]]; then
    echo "[restore] Backup file not found: $BACKUP_FILE" >&2
    exit 1
fi

DATA_DIR="${TBDTASK_DATA_DIR:-data}"
DATABASE_URL="${DATABASE_URL:-sqlite://${DATA_DIR}/tbdtask.db}"

# Detect backend from DATABASE_URL.
if [[ "$DATABASE_URL" == postgresql* ]] || [[ "$DATABASE_URL" == postgres://* ]]; then
    echo "[restore] Postgres detected — restoring via pg_restore..."
    # pg_restore accepts libpq URLs, not SQLAlchemy driver URLs.
    PG_URL="${DATABASE_URL/postgresql+psycopg:\/\//postgresql://}"
    PG_URL="${PG_URL/postgres:\/\//postgresql://}"

    # Verify the backup is a valid Postgres dump.
    if ! pg_restore --list "$BACKUP_FILE" >/dev/null 2>&1; then
        echo "[restore] Backup file is not a valid Postgres custom-format dump" >&2
        echo "[restore] Expected format: pg_dump -Fc output" >&2
        exit 1
    fi

    # Warn about data loss.
    echo "[restore] WARNING: This will overwrite the current database."
    echo "[restore] Press Ctrl+C to cancel, or wait 5 seconds..."
    sleep 5

    # Restore.
    pg_restore --clean --if-exists -d "$PG_URL" "$BACKUP_FILE"
    echo "[restore] Restored from: $BACKUP_FILE"

elif [[ "$DATABASE_URL" == sqlite://* ]]; then
    DB_PATH="${DATABASE_URL#sqlite://}"

    if [[ ! -f "$DB_PATH" ]]; then
        echo "[restore] Database not found: $DB_PATH" >&2
        exit 1
    fi

    # Warn about data loss.
    echo "[restore] WARNING: This will overwrite the current database at $DB_PATH"
    echo "[restore] Press Ctrl+C to cancel, or wait 5 seconds..."
    sleep 5

    # WAL-aware restore: checkpoint existing DB first, then overwrite.
    if command -v sqlite3 >/dev/null 2>&1; then
        sqlite3 "$DB_PATH" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null 2>&1 || true
    fi

    cp "$BACKUP_FILE" "$DB_PATH"
    echo "[restore] Restored from: $BACKUP_FILE"
else
    echo "[restore] Unknown DATABASE_URL scheme: $DATABASE_URL" >&2
    exit 1
fi
