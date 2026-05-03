#!/usr/bin/env bash
# Backup/restore integration test for tbdtask.
#
# Verifies that:
# 1. A backup can be created from a live database
# 2. The backup contains the expected data
# 3. The backup can be restored to a fresh database
# 4. The restored database contains all original data
#
# Usage:
#   tools/test_backup_restore.sh
#
# Environment:
#   DATABASE_URL  — Postgres connection string (required)

set -euo pipefail

echo "[test] Starting backup/restore integration test..."

DB_URL="${DATABASE_URL:?DATABASE_URL is required}"
PG_URL="${DB_URL/postgresql+psycopg:\/\//postgresql://}"
PG_URL="${PG_URL/postgres:\/\//postgresql://}"
TIMESTAMP="$(date +%s)"
BACKUP_FILE="/tmp/tbdtask-backup-test-${TIMESTAMP}.bak"
RESTORE_DB="tbdtask_restore_test_${TIMESTAMP}"

cleanup() {
    rm -f "$BACKUP_FILE" 2>/dev/null || true
    # Drop the test restore database
    if [[ "$DB_URL" == postgresql* ]]; then
        psql "$PG_URL" -c "DROP DATABASE IF EXISTS $RESTORE_DB;" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# -----------------------------------------------------------------------
# Step 1: Seed test data
# -----------------------------------------------------------------------
echo "[test] Seeding test data..."

if [[ "$DB_URL" == postgresql* ]]; then
    # Create test data in the main database.
    psql "$PG_URL" -v ON_ERROR_STOP=1 <<'SQL'
    INSERT INTO organizations (slug, name) VALUES ('backup-test-org', 'Backup Test Org')
    ON CONFLICT DO NOTHING;

    INSERT INTO persons (last_name, full_display, display_order, orders_received, itinerary_received, aob_scheduled, barracks_assigned, active, org_id)
    SELECT 'BackupTest', 'Backup Test Person', 0, FALSE, FALSE, FALSE, FALSE, TRUE, o.id
    FROM organizations o WHERE o.slug = 'backup-test-org'
    ON CONFLICT DO NOTHING;

    INSERT INTO worklists (week_starting, name, version, locked, active, org_id)
    SELECT '2026-01-05', 'Backup Test Worklist', 1, FALSE, TRUE, o.id
    FROM organizations o WHERE o.slug = 'backup-test-org'
    ON CONFLICT DO NOTHING;
SQL

    # Count rows before backup.
    ORIGINAL_COUNT=$(psql "$PG_URL" -t -A -c "SELECT COUNT(*) FROM persons WHERE last_name = 'BackupTest';")
    echo "[test] Original row count: $ORIGINAL_COUNT"
fi

# -----------------------------------------------------------------------
# Step 2: Create backup
# -----------------------------------------------------------------------
echo "[test] Creating backup..."

export DATABASE_URL="$DB_URL"
bash tools/backup.sh "$BACKUP_FILE"

if [[ ! -f "$BACKUP_FILE" ]]; then
    echo "[test] FAIL: Backup file was not created"
    exit 1
fi

BACKUP_SIZE=$(stat -f%z "$BACKUP_FILE" 2>/dev/null || stat -c%s "$BACKUP_FILE" 2>/dev/null || echo "0")
echo "[test] Backup size: $BACKUP_SIZE bytes"

if [[ "$BACKUP_SIZE" -eq 0 ]]; then
    echo "[test] FAIL: Backup file is empty"
    exit 1
fi

# -----------------------------------------------------------------------
# Step 3: Restore to fresh database
# -----------------------------------------------------------------------
echo "[test] Restoring to fresh database..."

if [[ "$DB_URL" == postgresql* ]]; then
    # Create a fresh database for restore.
    psql "$PG_URL" -c "CREATE DATABASE $RESTORE_DB;"

    # Build the restore URL.
    RESTORE_URL=$(echo "$DB_URL" | sed "s|/[^/]*$|/$RESTORE_DB|")
    RESTORE_PG_URL=$(echo "$PG_URL" | sed "s|/[^/]*$|/$RESTORE_DB|")

    # Restore the full custom-format backup into the empty database. The dump
    # includes schema, data, indexes, constraints, policies, and alembic_version.
    pg_restore -d "$RESTORE_PG_URL" "$BACKUP_FILE"

    # Verify restored data.
    RESTORED_COUNT=$(psql "$RESTORE_PG_URL" -t -A -c "SELECT COUNT(*) FROM persons WHERE last_name = 'BackupTest';")
    echo "[test] Restored row count: $RESTORED_COUNT"

    if [[ "$RESTORED_COUNT" -ne "$ORIGINAL_COUNT" ]]; then
        echo "[test] FAIL: Restored row count ($RESTORED_COUNT) does not match original ($ORIGINAL_COUNT)"
        exit 1
    fi

    # Verify specific data integrity.
    WORKLIST_COUNT=$(psql "$RESTORE_PG_URL" -t -A -c "SELECT COUNT(*) FROM worklists WHERE name = 'Backup Test Worklist';")
    if [[ "$WORKLIST_COUNT" -lt 1 ]]; then
        echo "[test] FAIL: Worklist data not restored correctly"
        exit 1
    fi
fi

# -----------------------------------------------------------------------
# Step 4: Cleanup test data from original database
# -----------------------------------------------------------------------
echo "[test] Cleaning up test data..."

if [[ "$DB_URL" == postgresql* ]]; then
    psql "$PG_URL" -v ON_ERROR_STOP=1 <<'SQL'
    DELETE FROM worklists WHERE name = 'Backup Test Worklist';
    DELETE FROM persons WHERE last_name = 'BackupTest';
    DELETE FROM organizations WHERE slug = 'backup-test-org';
SQL
fi

echo "[test] PASS: Backup/restore integration test succeeded."
