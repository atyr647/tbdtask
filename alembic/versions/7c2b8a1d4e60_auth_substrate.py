"""auth substrate

Phase 1 of the multi-tenant pivot. Adds the four auth-layer tables that
support OIDC-only sign-in plus a minimal auth audit trail:

* ``user_accounts`` — login identity (no password column).
* ``identities`` — provider/subject pairs linked to a user.
* ``org_memberships`` — user-to-org links with active/pending/suspended
  status (role_template_id added in Phase 2).
* ``user_sessions`` — server-side session store; cookie carries only the
  opaque id.
* ``auth_events`` — append-only auth-flow audit trail (login, logout,
  link, suspended-session-revoke).

No ``org_id`` columns: these tables sit alongside the tenant root, not
below it. ``org_memberships`` references ``organizations`` directly.

Revision ID: 7c2b8a1d4e60
Revises: 4f1d2e9c8a31
Create Date: 2026-05-02 12:30:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "7c2b8a1d4e60"
down_revision: Union[str, Sequence[str], None] = "4f1d2e9c8a31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=True),
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
        sa.Column("disabled_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("email", name="uq_user_accounts_email"),
    )

    op.create_table(
        "identities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("subject", sa.String(length=256), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column(
            "linked_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user_accounts.id"], name="fk_identities_user_id"
        ),
        sa.UniqueConstraint("provider", "subject", name="uq_identity_provider_subject"),
        sa.CheckConstraint(
            "provider IN ('apple','google','microsoft')",
            name="ck_identity_provider",
        ),
    )
    op.create_index("ix_identities_user_id", "identities", ["user_id"])

    op.create_table(
        "org_memberships",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "joined_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("suspended_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], name="fk_org_memberships_org_id"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user_accounts.id"], name="fk_org_memberships_user_id"
        ),
        sa.UniqueConstraint("org_id", "user_id", name="uq_org_membership_org_user"),
        sa.CheckConstraint(
            "status IN ('active','pending','suspended')",
            name="ck_org_membership_status",
        ),
    )
    op.create_index("ix_org_memberships_org_id", "org_memberships", ["org_id"])
    op.create_index("ix_org_memberships_user_id", "org_memberships", ["user_id"])

    op.create_table(
        "user_sessions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("current_membership_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user_accounts.id"], name="fk_user_sessions_user_id"
        ),
        sa.ForeignKeyConstraint(
            ["current_membership_id"],
            ["org_memberships.id"],
            name="fk_user_sessions_current_membership_id",
        ),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    op.create_index(
        "ix_user_sessions_current_membership_id",
        "user_sessions",
        ["current_membership_id"],
    )

    op.create_table(
        "org_invites",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("intended_email", sa.String(length=320), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.Column("accepted_by_user_id", sa.Integer(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], name="fk_org_invites_org_id"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user_accounts.id"],
            name="fk_org_invites_created_by",
        ),
        sa.ForeignKeyConstraint(
            ["accepted_by_user_id"],
            ["user_accounts.id"],
            name="fk_org_invites_accepted_by",
        ),
        sa.UniqueConstraint("token_hash", name="uq_org_invites_token_hash"),
    )
    op.create_index("ix_org_invites_org_id", "org_invites", ["org_id"])

    op.create_table(
        "auth_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user_accounts.id"], name="fk_auth_events_user_id"
        ),
    )
    op.create_index("ix_auth_events_kind", "auth_events", ["kind"])
    op.create_index("ix_auth_events_user_id", "auth_events", ["user_id"])
    op.create_index("ix_auth_events_created_at", "auth_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_auth_events_created_at", table_name="auth_events")
    op.drop_index("ix_auth_events_user_id", table_name="auth_events")
    op.drop_index("ix_auth_events_kind", table_name="auth_events")
    op.drop_table("auth_events")

    op.drop_index(
        "ix_user_sessions_current_membership_id", table_name="user_sessions"
    )
    op.drop_index("ix_user_sessions_user_id", table_name="user_sessions")
    op.drop_table("user_sessions")

    op.drop_index("ix_org_invites_org_id", table_name="org_invites")
    op.drop_table("org_invites")

    op.drop_index("ix_org_memberships_user_id", table_name="org_memberships")
    op.drop_index("ix_org_memberships_org_id", table_name="org_memberships")
    op.drop_table("org_memberships")

    op.drop_index("ix_identities_user_id", table_name="identities")
    op.drop_table("identities")

    op.drop_table("user_accounts")
