"""Phase 8a — WebAuthn credentials + step-up grants.

Adds two new tables:

* ``user_webauthn_credentials`` — one row per registered passkey or
  security key. Stores the COSE public key, credential id, sign-count,
  authenticator metadata, and a ``prf_supported`` boolean flag. The PRF
  *output* is never persisted; only the boolean fact of support is
  captured at registration.

* ``step_up_grants`` — short-lived proofs that the user re-verified
  with a passkey. Scoped to (session, credential, purpose). Single-use
  grants are consumed by ``require_step_up``.

Both tables are additive. No existing table is modified, so this
migration is reversible by ``downgrade()`` without data loss.

The Phase 8a behavior is gated at runtime by ``TBDTASK_WEBAUTHN_ENABLED``
(see ``app/auth/webauthn.py``); the tables can sit empty in production
until the flag is flipped on.

Revision ID: e8a1c0d34f5b
"""

from alembic import op
import sqlalchemy as sa


revision = "e8a1c0d34f5b"
down_revision = "d7e9f2a3b48c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_webauthn_credentials",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("credential_id", sa.LargeBinary(), nullable=False, unique=True),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column(
            "sign_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("aaguid", sa.String(length=36), nullable=True),
        sa.Column("transports", sa.JSON(), nullable=True),
        sa.Column(
            "backup_eligible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "backup_state",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "prf_supported",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("nickname", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_user_webauthn_credentials_user_id",
        "user_webauthn_credentials",
        ["user_id"],
    )

    op.create_table(
        "step_up_grants",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=64),
            sa.ForeignKey("user_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "credential_id",
            sa.String(length=36),
            sa.ForeignKey("user_webauthn_credentials.id"),
            nullable=False,
        ),
        sa.Column(
            "granted_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("purpose", sa.String(length=64), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_step_up_grants_session_id",
        "step_up_grants",
        ["session_id"],
    )
    op.create_index(
        "ix_step_up_grants_purpose",
        "step_up_grants",
        ["purpose"],
    )


def downgrade() -> None:
    op.drop_index("ix_step_up_grants_purpose", table_name="step_up_grants")
    op.drop_index("ix_step_up_grants_session_id", table_name="step_up_grants")
    op.drop_table("step_up_grants")
    op.drop_index(
        "ix_user_webauthn_credentials_user_id",
        table_name="user_webauthn_credentials",
    )
    op.drop_table("user_webauthn_credentials")
