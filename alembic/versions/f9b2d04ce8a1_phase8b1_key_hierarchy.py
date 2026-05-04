"""Phase 8b.1 — key hierarchy plumbing (org_master_keys, credential_keys).

Adds the two tables that hold the wrapped key material:

* ``org_master_keys`` — per-org ``KEK_org_master`` wrapped under the
  operator key. One active row per org; rotated rows stay with
  ``retired_at`` populated.
* ``credential_keys`` — per-(credential, org) wrap of the same
  ``KEK_org_master`` under the credential's PRF-derived KEK. Empty
  in 8b.1; populated by the bootstrap / wrap-on-enrollment flow in
  8b.2.

Both tables are additive. No existing table is modified, so this
migration reverses cleanly.

Phase 8b runtime is gated by ``TBDTASK_WEBAUTHN_ENABLED``; the
tables can sit empty until both that flag and the operator-key
configuration are in place.

Revision ID: f9b2d04ce8a1
"""

from alembic import op
import sqlalchemy as sa


revision = "f9b2d04ce8a1"
down_revision = "e8a1c0d34f5b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_master_keys",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("wrapped_org_kek", sa.LargeBinary(), nullable=False),
        sa.Column("operator_key_id", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column("retired_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_org_master_keys_org_id",
        "org_master_keys",
        ["org_id"],
    )

    op.create_table(
        "credential_keys",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "credential_id",
            sa.String(length=36),
            sa.ForeignKey("user_webauthn_credentials.id"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("wrapped_org_kek", sa.LargeBinary(), nullable=False),
        sa.Column("prf_salt", sa.LargeBinary(), nullable=False),
        sa.Column(
            "wrapped_by_credential_id",
            sa.String(length=36),
            sa.ForeignKey("user_webauthn_credentials.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column("retired_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_credential_keys_credential_id",
        "credential_keys",
        ["credential_id"],
    )
    op.create_index(
        "ix_credential_keys_org_id",
        "credential_keys",
        ["org_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_credential_keys_org_id", table_name="credential_keys")
    op.drop_index(
        "ix_credential_keys_credential_id", table_name="credential_keys"
    )
    op.drop_table("credential_keys")
    op.drop_index("ix_org_master_keys_org_id", table_name="org_master_keys")
    op.drop_table("org_master_keys")
