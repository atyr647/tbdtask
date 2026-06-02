"""Add due_at deadline to person_quals.

When a qualification is assigned to someone who does not already hold or
waive it, the assigner sets a deadline (``due_at``) by which it must be
achieved. The alerts engine raises qual_due_soon / qual_overdue from it.

Revision ID: e1f3a7c2b9d0
"""

from alembic import op
import sqlalchemy as sa


revision = "e1f3a7c2b9d0"
down_revision = "d7e9f2a3b48c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("person_quals") as batch:
        batch.add_column(sa.Column("due_at", sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("person_quals") as batch:
        batch.drop_column("due_at")
