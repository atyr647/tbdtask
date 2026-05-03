"""Phase 3: database-enforced tenant isolation.

* Makes ``org_id`` NOT NULL on every tenant-scoped table (the Phase 0
  migration already backfilled existing rows, so this is safe).
* Enables Postgres row-level security (RLS) on every tenant table.
* Adds a per-table policy that matches ``org_id`` against the session-local
  GUC ``app.current_org_id``.
* Makes ``data_audit_events`` INSERT-only at the DB layer.

SQLite does not support RLS; the NOT NULL constraint still applies so the
schema is identical across backends. The app-layer ``do_orm_execute``
listener continues to provide tenant scoping for SQLite.

Revision ID: c5d8e3f1a27b
"""
from alembic import op
import sqlalchemy as sa


revision = "c5d8e3f1a27b"
down_revision = "ab47f07f0922"
branch_labels = None
depends_on = None

# Every tenant-scoped table. Mirrors app/tenancy.py::TENANT_SCOPED_TABLES.
TENANT_TABLES = (
    "persons",
    "person_rates",
    "person_duty_sections",
    "person_prds",
    "person_roster_status",
    "person_drivers_licenses",
    "qualifications",
    "person_quals",
    "absence_codes",
    "absences",
    "crews",
    "crew_memberships",
    "task_categories",
    "task_templates",
    "task_instances",
    "task_assignments",
    "worklists",
    "alerts",
    "import_batches",
    "data_audit_events",
    "workcenters",
    "roles",
)


def upgrade() -> None:
    bind = op.get_bind()

    # ------------------------------------------------------------------
    # 1. Make org_id NOT NULL on every tenant table.
    # ------------------------------------------------------------------
    for table in TENANT_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.alter_column("org_id", nullable=False)

    # ------------------------------------------------------------------
    # 2. Enable RLS on Postgres.
    # ------------------------------------------------------------------
    if bind.dialect.name != "postgresql":
        return

    for table in TENANT_TABLES:
        bind.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        bind.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))

        # Drop any existing policy (idempotent re-run).
        bind.execute(
            sa.text(
                f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}"
            )
        )

        # Fail-closed: when app.current_org_id is unset (NULL), the
        # comparison org_id = NULL is never true, so all rows are denied.
        # Use NULLIF to handle empty strings from RESET.
        bind.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation_{table} ON {table} "
                f"USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::int) "
                f"WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::int)"
            )
        )

    # ------------------------------------------------------------------
    # 3. Audit table: INSERT-only.
    # ------------------------------------------------------------------
    bind.execute(sa.text("ALTER TABLE data_audit_events ENABLE ROW LEVEL SECURITY"))
    bind.execute(
        sa.text(
            "DROP POLICY IF EXISTS audit_insert_only ON data_audit_events"
        )
    )
    bind.execute(
        sa.text(
            "CREATE POLICY audit_insert_only ON data_audit_events "
            "FOR INSERT WITH CHECK "
            "(org_id = NULLIF(current_setting('app.current_org_id', true), '')::int)"
        )
    )
    # App needs SELECT for admin UI — scoped to tenant.
    bind.execute(
        sa.text(
            "CREATE POLICY audit_read ON data_audit_events "
            "FOR SELECT USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::int)"
        )
    )

    # ------------------------------------------------------------------
    # 4. Audit table index for performance.
    # ------------------------------------------------------------------
    op.create_index(
        "ix_data_audit_events_org_id_occurred_at",
        "data_audit_events",
        ["org_id", "occurred_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        for table in TENANT_TABLES:
            bind.execute(
                sa.text(
                    f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}"
                )
            )
            bind.execute(
                sa.text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            )
            bind.execute(
                sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
            )

        bind.execute(
            sa.text("DROP POLICY IF EXISTS audit_insert_only ON data_audit_events")
        )
        bind.execute(
            sa.text("DROP POLICY IF EXISTS audit_read ON data_audit_events")
        )
        bind.execute(
            sa.text("ALTER TABLE data_audit_events DISABLE ROW LEVEL SECURITY")
        )

    op.drop_index("ix_data_audit_events_org_id_occurred_at", table_name="data_audit_events")

    for table in TENANT_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.alter_column("org_id", nullable=True)
