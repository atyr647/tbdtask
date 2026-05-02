"""Add personnel fields to org_invites.

Admins now provide first_name, last_name, rate, and paygrade when
composing an invite. On acceptance a Person record is auto-created
so the invitee only needs to fill in their PRD and arrival details.

Revision ID: d7e9f2a3b48c
"""
from alembic import op
import sqlalchemy as sa


revision = "d7e9f2a3b48c"
down_revision = "c5d8e3f1a27b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("org_invites") as batch:
        batch.add_column(sa.Column("first_name", sa.String(128), nullable=True))
        batch.add_column(sa.Column("last_name", sa.String(128), nullable=True))
        batch.add_column(sa.Column("rate", sa.String(32), nullable=True))
        batch.add_column(sa.Column("paygrade", sa.String(8), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("org_invites") as batch:
        batch.drop_column("paygrade")
        batch.drop_column("rate")
        batch.drop_column("last_name")
        batch.drop_column("first_name")
