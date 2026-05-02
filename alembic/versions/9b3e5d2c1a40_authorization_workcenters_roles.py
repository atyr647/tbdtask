"""authorization: workcenters, roles, role permissions, membership roles

Phase 2 of the multi-tenant pivot. Adds the four authorization tables
that back the permission catalog and seeds every existing org with the
default role templates.

* ``workcenters`` — nested org-scoped grouping used as a permission-grant
  scope. Self-FK ``parent_id``; soft-archived; per-org slug uniqueness.
* ``roles`` — org-scoped named bundles of permissions. Each org gets its
  own seeded copy of every entry in ``app.auth.permissions.ROLE_TEMPLATES``
  at migration time so admins can edit per-tenant without affecting
  other orgs.
* ``role_permissions`` — composite-PK (role_id, permission_code) join.
  ``permission_code`` is a free-form string at the schema layer; the
  catalog in ``app.auth.permissions`` is the authoritative validator.
* ``membership_roles`` — attach a role to a membership, optionally
  scoped to a single workcenter (NULL = org-wide). ``CASCADE`` on the
  three FKs so role/workcenter/membership deletion cleans up its
  attached grants automatically.

Backfill: every existing membership in the default org keeps its
unchanged "active member" effective behaviour, but they receive **no**
role by default. The Phase 1 commit message documented that "logged in"
was the only authorization gate before Phase 2; rather than auto-grant
``org_owner`` to every existing user, we promote exactly one founder
per org — the lowest-id active membership — to ``org_owner``. This keeps
the existing single-tenant DB usable by its operator while drawing a
clean line where authorization gates begin.

Revision ID: 9b3e5d2c1a40
Revises: 7c2b8a1d4e60
Create Date: 2026-05-03 12:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "9b3e5d2c1a40"
down_revision: Union[str, Sequence[str], None] = "7c2b8a1d4e60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Mirrored from app/auth/permissions.py. The model module imports this
# migration's behaviour indirectly (via the Python catalog), but we
# duplicate the seed list here so the migration is self-contained and
# can run before any app code edits land. A test in
# tests/test_phase2_authz.py asserts the two are in sync.
_TEMPLATE_SEEDS: tuple[dict, ...] = (
    {
        "slug": "org_owner",
        "name": "Org Owner",
        "description": (
            "Full control of the organization. Cannot be scoped to a "
            "workcenter. An org always has at least one owner; the last "
            "owner cannot remove themselves."
        ),
        "workcenter_scopable": False,
        "permissions": (
            "org.view",
            "org.admin",
            "org.invite",
            "org.manage_members",
            "org.manage_workcenters",
            "personnel.view",
            "personnel.write",
            "personnel.archive",
            "quals.view",
            "quals.write",
            "quals.archive",
            "absences.view",
            "absences.write",
            "absences.archive",
            "tasks.view",
            "tasks.write",
            "tasks.archive",
            "worklists.view",
            "worklists.write",
            "worklists.lock",
            "worklists.amend",
            "worklists.archive",
            "alerts.view",
            "alerts.triage",
            "alerts.act",
        ),
    },
    {
        "slug": "org_admin",
        "name": "Org Admin",
        "description": (
            "Manages members, invites, workcenters, and all operational "
            "data. Cannot transfer ownership; cannot destroy the org."
        ),
        "workcenter_scopable": False,
        "permissions": (
            "org.view",
            "org.invite",
            "org.manage_members",
            "org.manage_workcenters",
            "personnel.view",
            "personnel.write",
            "personnel.archive",
            "quals.view",
            "quals.write",
            "quals.archive",
            "absences.view",
            "absences.write",
            "absences.archive",
            "tasks.view",
            "tasks.write",
            "tasks.archive",
            "worklists.view",
            "worklists.write",
            "worklists.archive",
            "alerts.view",
            "alerts.triage",
            "alerts.act",
        ),
    },
    {
        "slug": "lpo",
        "name": "Team Lead",
        "description": (
            "Full operational control: edit personnel, qualifications, "
            "absences, worklists, lock + amend, take alert actions."
        ),
        "workcenter_scopable": True,
        "permissions": (
            "org.view",
            "personnel.view",
            "personnel.write",
            "personnel.archive",
            "quals.view",
            "quals.write",
            "quals.archive",
            "absences.view",
            "absences.write",
            "absences.archive",
            "tasks.view",
            "tasks.write",
            "tasks.archive",
            "worklists.view",
            "worklists.write",
            "worklists.lock",
            "worklists.amend",
            "worklists.archive",
            "alerts.view",
            "alerts.triage",
            "alerts.act",
        ),
    },
    {
        "slug": "dlpo",
        "name": "Assistant Team Lead",
        "description": (
            "Same as Team Lead minus lock/amend authority."
        ),
        "workcenter_scopable": True,
        "permissions": (
            "org.view",
            "personnel.view",
            "personnel.write",
            "personnel.archive",
            "quals.view",
            "quals.write",
            "quals.archive",
            "absences.view",
            "absences.write",
            "absences.archive",
            "tasks.view",
            "tasks.write",
            "tasks.archive",
            "worklists.view",
            "worklists.write",
            "worklists.archive",
            "alerts.view",
            "alerts.triage",
            "alerts.act",
        ),
    },
    {
        "slug": "member",
        "name": "Member",
        "description": (
            "Read-everything, write to operational data within scope. No "
            "archive, no lock/amend. Suitable for most rotated personnel."
        ),
        "workcenter_scopable": True,
        "permissions": (
            "org.view",
            "personnel.view",
            "quals.view",
            "absences.view",
            "absences.write",
            "tasks.view",
            "tasks.write",
            "worklists.view",
            "alerts.view",
            "alerts.triage",
        ),
    },
    {
        "slug": "viewer",
        "name": "Viewer",
        "description": (
            "Read-only access. Suitable for stakeholders, auditors, "
            "departing personnel during turnover."
        ),
        "workcenter_scopable": True,
        "permissions": (
            "org.view",
            "personnel.view",
            "quals.view",
            "absences.view",
            "tasks.view",
            "worklists.view",
            "alerts.view",
        ),
    },
)


def upgrade() -> None:
    op.create_table(
        "workcenters",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "display_order", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], name="fk_workcenters_org_id"
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["workcenters.id"], name="fk_workcenters_parent_id"
        ),
        sa.UniqueConstraint("org_id", "slug", name="uq_workcenter_org_slug"),
    )
    op.create_index("ix_workcenters_org_id", "workcenters", ["org_id"])
    op.create_index("ix_workcenters_parent_id", "workcenters", ["parent_id"])

    op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("template_slug", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "builtin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
        sa.Column(
            "workcenter_scopable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("TRUE"),
        ),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], name="fk_roles_org_id"
        ),
        sa.UniqueConstraint("org_id", "name", name="uq_role_org_name"),
        sa.UniqueConstraint(
            "org_id", "template_slug", name="uq_role_org_template_slug"
        ),
    )
    op.create_index("ix_roles_org_id", "roles", ["org_id"])

    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("permission_code", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("role_id", "permission_code"),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name="fk_role_permissions_role_id",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "membership_roles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("membership_id", sa.Integer(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("workcenter_id", sa.Integer(), nullable=True),
        sa.Column("granted_by_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "granted_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["membership_id"],
            ["org_memberships.id"],
            name="fk_membership_roles_membership_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name="fk_membership_roles_role_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workcenter_id"],
            ["workcenters.id"],
            name="fk_membership_roles_workcenter_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_user_id"],
            ["user_accounts.id"],
            name="fk_membership_roles_granted_by",
        ),
        sa.UniqueConstraint(
            "membership_id",
            "role_id",
            "workcenter_id",
            name="uq_membership_role_scope",
        ),
    )
    op.create_index(
        "ix_membership_roles_membership_id",
        "membership_roles",
        ["membership_id"],
    )
    op.create_index(
        "ix_membership_roles_role_id", "membership_roles", ["role_id"]
    )
    op.create_index(
        "ix_membership_roles_workcenter_id",
        "membership_roles",
        ["workcenter_id"],
    )

    # ------------------------------------------------------------------
    # Backfill: seed every existing org with the role templates and
    # promote one founding owner per org. Idempotent — re-runs on a
    # partly-seeded DB skip rows that already exist.
    # ------------------------------------------------------------------
    bind = op.get_bind()
    org_rows = bind.execute(sa.text("SELECT id FROM organizations")).fetchall()
    for (org_id,) in org_rows:
        _seed_org_roles(bind, org_id)
        _promote_founding_owner(bind, org_id)


def downgrade() -> None:
    op.drop_index("ix_membership_roles_workcenter_id", table_name="membership_roles")
    op.drop_index("ix_membership_roles_role_id", table_name="membership_roles")
    op.drop_index("ix_membership_roles_membership_id", table_name="membership_roles")
    op.drop_table("membership_roles")

    op.drop_table("role_permissions")

    op.drop_index("ix_roles_org_id", table_name="roles")
    op.drop_table("roles")

    op.drop_index("ix_workcenters_parent_id", table_name="workcenters")
    op.drop_index("ix_workcenters_org_id", table_name="workcenters")
    op.drop_table("workcenters")


# ---------------------------------------------------------------------------
# Backfill helpers — exposed at module scope so tests can drive them on a
# freshly migrated test DB without re-running the upgrade.
# ---------------------------------------------------------------------------

def _seed_org_roles(bind, org_id: int) -> None:
    """Insert the six built-in role templates for one org. Idempotent."""
    for tmpl in _TEMPLATE_SEEDS:
        existing = bind.execute(
            sa.text(
                "SELECT id FROM roles WHERE org_id = :oid AND template_slug = :slug"
            ).bindparams(oid=org_id, slug=tmpl["slug"])
        ).scalar_one_or_none()
        if existing is not None:
            role_id = existing
        else:
            res = bind.execute(
                sa.text(
                    "INSERT INTO roles "
                    "(org_id, template_slug, name, description, builtin, "
                    " workcenter_scopable) "
                    "VALUES (:oid, :slug, :name, :desc, TRUE, :wcs) "
                    "RETURNING id"
                ).bindparams(
                    oid=org_id,
                    slug=tmpl["slug"],
                    name=tmpl["name"],
                    desc=tmpl["description"],
                    wcs=True if tmpl["workcenter_scopable"] else False,
                )
            )
            role_id = res.scalar_one()

        for code in tmpl["permissions"]:
            already = bind.execute(
                sa.text(
                    "SELECT 1 FROM role_permissions "
                    "WHERE role_id = :rid AND permission_code = :code"
                ).bindparams(rid=role_id, code=code)
            ).scalar_one_or_none()
            if already is not None:
                continue
            bind.execute(
                sa.text(
                    "INSERT INTO role_permissions (role_id, permission_code) "
                    "VALUES (:rid, :code)"
                ).bindparams(rid=role_id, code=code)
            )


def _promote_founding_owner(bind, org_id: int) -> None:
    """Grant org_owner to the lowest-id active membership in this org.

    Rationale: the existing single-tenant DB has whatever members the
    operator set up before authorization gates existed. Without a Phase
    2 owner, *no one* can administer the org. We promote exactly one
    founder per org — the lowest membership id, which on the existing
    SQLite DB is the operator who first signed in. Other members get
    nothing and must be assigned roles by the founder via the admin UI.

    Idempotent: skips if any membership in the org already has org_owner.
    """
    already_owner = bind.execute(
        sa.text(
            "SELECT 1 FROM membership_roles mr "
            "JOIN roles r ON r.id = mr.role_id "
            "JOIN org_memberships m ON m.id = mr.membership_id "
            "WHERE m.org_id = :oid AND r.template_slug = 'org_owner' "
            "LIMIT 1"
        ).bindparams(oid=org_id)
    ).scalar_one_or_none()
    if already_owner is not None:
        return

    founder_membership_id = bind.execute(
        sa.text(
            "SELECT id FROM org_memberships "
            "WHERE org_id = :oid AND status = 'active' "
            "ORDER BY id ASC LIMIT 1"
        ).bindparams(oid=org_id)
    ).scalar_one_or_none()
    if founder_membership_id is None:
        # No active members — nothing to promote, and the next member
        # who signs in will need an external admin to bootstrap. The
        # SINGLE_TENANT-mode AppImage hits this path on a fresh install
        # because there are zero memberships. That's fine.
        return

    owner_role_id = bind.execute(
        sa.text(
            "SELECT id FROM roles "
            "WHERE org_id = :oid AND template_slug = 'org_owner'"
        ).bindparams(oid=org_id)
    ).scalar_one()

    bind.execute(
        sa.text(
            "INSERT INTO membership_roles "
            "(membership_id, role_id, workcenter_id, granted_by_user_id) "
            "VALUES (:mid, :rid, NULL, NULL)"
        ).bindparams(mid=founder_membership_id, rid=owner_role_id)
    )
