"""organizations and tenant scoping

Phase 0 of the multi-tenant pivot.

* Creates the ``organizations`` table.
* Adds a nullable ``org_id`` FK + index on every tenant-scoped table.
* Inserts a "Default Organization" and backfills every existing row to it
  so the column can be made NOT NULL safely in Phase 3.
* On Postgres only, creates row-level security policies that match
  ``org_id`` against a session-local GUC (``app.current_org_id``). RLS is
  NOT enabled on any table here — the policies exist as schema artifacts
  but have zero effect until Phase 3 issues
  ``ALTER TABLE ... ENABLE ROW LEVEL SECURITY``.

Revision ID: 4f1d2e9c8a31
Revises: ba3b919d6300
Create Date: 2026-05-02 11:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "4f1d2e9c8a31"
down_revision: Union[str, Sequence[str], None] = "ba3b919d6300"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Mirrored in app/tenancy.py::TENANT_SCOPED_TABLES. The Phase 0 test suite
# asserts the two tuples match, so a forgotten model surfaces immediately.
TENANT_SCOPED_TABLES: tuple[str, ...] = (
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
)


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("settings_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.UniqueConstraint("slug", name="uq_organizations_slug"),
    )

    bind = op.get_bind()

    # Seed a default org so the existing single-tenant DB has somewhere to
    # backfill into. Phase 3 makes ``org_id`` NOT NULL and that step would
    # fail loudly if any row were left orphaned.
    bind.execute(
        sa.text(
            "INSERT INTO organizations (slug, name) "
            "VALUES ('default', 'Default Organization')"
        )
    )
    default_org_id = bind.execute(
        sa.text("SELECT id FROM organizations WHERE slug = 'default'")
    ).scalar_one()

    for table in TENANT_SCOPED_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("org_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                f"fk_{table}_org_id",
                "organizations",
                ["org_id"],
                ["id"],
            )
            batch.create_index(f"ix_{table}_org_id", ["org_id"])

        bind.execute(
            sa.text(f"UPDATE {table} SET org_id = :oid WHERE org_id IS NULL").bindparams(
                oid=default_org_id
            )
        )

    # Postgres: define the RLS policies but leave RLS itself disabled.
    # Without ALTER TABLE ... ENABLE ROW LEVEL SECURITY these policies are
    # inert — Phase 3 turns them on. Doing the SQL now means the policy
    # text is reviewed and version-controlled alongside the substrate it
    # belongs to, instead of arriving in a much larger Phase 3 commit.
    if bind.dialect.name == "postgresql":
        for table in TENANT_SCOPED_TABLES:
            bind.execute(
                sa.text(
                    f"CREATE POLICY tenant_isolation_{table} ON {table} "
                    f"USING (org_id::text = current_setting('app.current_org_id', true))"
                )
            )


def downgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        for table in TENANT_SCOPED_TABLES:
            bind.execute(
                sa.text(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
            )

    for table in TENANT_SCOPED_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_index(f"ix_{table}_org_id")
            batch.drop_column("org_id")

    op.drop_table("organizations")
