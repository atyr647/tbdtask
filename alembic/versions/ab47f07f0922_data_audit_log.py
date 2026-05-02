"""data audit log for tenant writes

Phase 4: immutable audit trail for all create/update/delete/archive
operations on tenant-scoped tables. Rows are INSERT-only at the
application layer; Postgres RLS (Phase 3) enforces this at the DB level.

Revision ID: ab47f07f0922
"""
from alembic import op
import sqlalchemy as sa


revision = "ab47f07f0922"
down_revision = "9b3e5d2c1a40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "data_audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), nullable=False, index=True),
        sa.Column("action", sa.String(32), nullable=False, index=True),
        sa.Column("table_name", sa.String(64), nullable=False, index=True),
        sa.Column("row_id", sa.Integer(), nullable=False, index=True),
        sa.Column("actor_membership_id", sa.Integer(), nullable=True),
        sa.Column("before_json", sa.JSON(), nullable=True),
        sa.Column("after_json", sa.JSON(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
            index=True,
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["actor_membership_id"], ["org_memberships.id"], ondelete="SET NULL"
        ),
    )
    # Composite index for table+row lookups (e.g. "show me all changes to person 42").
    op.create_index(
        "ix_data_audit_table_row",
        "data_audit_events",
        ["table_name", "row_id"],
    )
    op.create_index(
        "ix_data_audit_actor",
        "data_audit_events",
        ["actor_membership_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_data_audit_actor", table_name="data_audit_events")
    op.drop_index("ix_data_audit_table_row", table_name="data_audit_events")
    op.drop_table("data_audit_events")
