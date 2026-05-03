"""Permission catalog and role templates.

Phase 2 of the multi-tenant pivot. The catalog is the *single source of
truth* for what the app can authorize on. Every route gets gated by one
of these codes via the ``@require`` decorator in
``app.auth.authorization``.

Design:

* **Codes are strings of the form ``<resource>.<action>``.** Verb second
  reads naturally in code (``"personnel.write"``) and groups by resource
  in audit logs.
* **The catalog is closed.** Tests assert that every grant in the DB
  references a known code; an unknown code anywhere is a bug, not a
  feature. New permissions ship in code first, then a migration that
  attaches them to the relevant role templates.
* **Role templates are an authoring convenience, not a runtime concept.**
  Each org gets its own seeded copy of every template so admins can edit
  membership-attached roles per-tenant without touching the catalog.
* **Workcenter scoping is per-grant, not per-permission.** Some
  permissions only make sense org-wide (``org.admin``), but the schema
  doesn't enforce that — the role template author decides which roles
  are eligible for workcenter-scoped grants. Phase 3's RLS policies see
  the same shape and don't need a special case for "this perm can't be
  workcenter-scoped".

The runtime check (``has_permission``) is in
``app.auth.authorization``; this module is data-only so it can be
imported anywhere (migrations, tests, routes) without dragging session
state along.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet


# ---------------------------------------------------------------------------
# Permission catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Permission:
    """One permission code, with a human-readable label.

    The label is what the admin UI shows when picking permissions for a
    role; it never appears in tokens, URLs, or logs.
    """

    code: str
    label: str
    description: str


# Org-wide / administrative — never workcenter-scoped in any sensible
# role template, but the schema doesn't enforce that.
P_ORG_VIEW = Permission(
    "org.view",
    "View organization",
    "See the organization name, member list, and role assignments.",
)
P_ORG_ADMIN = Permission(
    "org.admin",
    "Administer organization",
    "Edit org name/settings, manage roles, manage workcenters, manage members. "
    "Phase 4 splits billing/destroy off into its own permission.",
)
P_ORG_INVITE = Permission(
    "org.invite",
    "Issue invitations",
    "Issue, list, and revoke invite tokens.",
)
P_ORG_MANAGE_MEMBERS = Permission(
    "org.manage_members",
    "Manage members",
    "Suspend, restore, and remove org members; assign roles to memberships.",
)
P_ORG_MANAGE_WORKCENTERS = Permission(
    "org.manage_workcenters",
    "Manage workcenters",
    "Create, edit, archive, and re-parent workcenters within the org.",
)

# Personnel
P_PERSONNEL_VIEW = Permission(
    "personnel.view",
    "View personnel",
    "Read the personnel roster and per-person history.",
)
P_PERSONNEL_WRITE = Permission(
    "personnel.write",
    "Edit personnel",
    "Create or edit personnel records, in-process incoming personnel, "
    "update planned departure dates, drivers licenses, team/group, position.",
)
P_PERSONNEL_ARCHIVE = Permission(
    "personnel.archive",
    "Archive personnel",
    "Soft-delete (depart) and restore personnel records.",
)

# Qualifications
P_QUALS_VIEW = Permission(
    "quals.view",
    "View qualifications",
    "Read the qualification catalog and per-person matrix.",
)
P_QUALS_WRITE = Permission(
    "quals.write",
    "Edit qualifications",
    "Add or edit qualifications in the catalog, assign quals to personnel, "
    "update qualification status and dates.",
)
P_QUALS_ARCHIVE = Permission(
    "quals.archive",
    "Archive qualifications",
    "Soft-delete qualifications from the catalog.",
)

# Absences
P_ABSENCES_VIEW = Permission(
    "absences.view", "View absences", "Read absence records and the calendar grid."
)
P_ABSENCES_WRITE = Permission(
    "absences.write", "Edit absences", "Create and edit absence records."
)
P_ABSENCES_ARCHIVE = Permission(
    "absences.archive", "Archive absences", "Soft-delete absence records."
)

# Tasks (instances + templates)
P_TASKS_VIEW = Permission(
    "tasks.view", "View tasks", "Read task templates and instances."
)
P_TASKS_WRITE = Permission(
    "tasks.write",
    "Edit tasks",
    "Create and edit task templates, ad-hoc task instances, and assignments.",
)
P_TASKS_ARCHIVE = Permission(
    "tasks.archive", "Archive tasks", "Soft-delete task templates and instances."
)

# Worklists
P_WORKLISTS_VIEW = Permission(
    "worklists.view",
    "View worklists",
    "Read worklists, day overviews, and printable views.",
)
P_WORKLISTS_WRITE = Permission(
    "worklists.write",
    "Edit worklists",
    "Create worklists, edit unlocked worklists, generate from templates, "
    "carry over open tasks.",
)
P_WORKLISTS_LOCK = Permission(
    "worklists.lock",
    "Lock worklists",
    "Lock a worklist, freezing it from further edits.",
)
P_WORKLISTS_AMEND = Permission(
    "worklists.amend",
    "Amend locked worklists",
    "Open an amendment on a locked worklist, cloning it into a new versioned snapshot.",
)
P_WORKLISTS_ARCHIVE = Permission(
    "worklists.archive", "Archive worklists", "Soft-delete worklists."
)

# Alerts
P_ALERTS_VIEW = Permission("alerts.view", "View alerts", "Read the alerts queue.")
P_ALERTS_TRIAGE = Permission(
    "alerts.triage",
    "Triage alerts",
    "Dismiss, snooze, and resolve-with-note on alerts.",
)
P_ALERTS_ACT = Permission(
    "alerts.act",
    "Act on alerts",
    "Take admin actions surfaced via alerts (extend a departure date, archive a "
    "person from an alert row).",
)


# Authoritative ordered tuple. New permissions are appended to keep the
# admin-UI ordering stable; tests assert no duplicates.
PERMISSIONS: tuple[Permission, ...] = (
    P_ORG_VIEW,
    P_ORG_ADMIN,
    P_ORG_INVITE,
    P_ORG_MANAGE_MEMBERS,
    P_ORG_MANAGE_WORKCENTERS,
    P_PERSONNEL_VIEW,
    P_PERSONNEL_WRITE,
    P_PERSONNEL_ARCHIVE,
    P_QUALS_VIEW,
    P_QUALS_WRITE,
    P_QUALS_ARCHIVE,
    P_ABSENCES_VIEW,
    P_ABSENCES_WRITE,
    P_ABSENCES_ARCHIVE,
    P_TASKS_VIEW,
    P_TASKS_WRITE,
    P_TASKS_ARCHIVE,
    P_WORKLISTS_VIEW,
    P_WORKLISTS_WRITE,
    P_WORKLISTS_LOCK,
    P_WORKLISTS_AMEND,
    P_WORKLISTS_ARCHIVE,
    P_ALERTS_VIEW,
    P_ALERTS_TRIAGE,
    P_ALERTS_ACT,
)


PERMISSION_CODES: FrozenSet[str] = frozenset(p.code for p in PERMISSIONS)


def is_known_permission(code: str) -> bool:
    """Return True iff ``code`` appears in the catalog.

    Used in tests to assert every DB-side grant references a real code,
    and in the admin UI to reject unknown codes from form submissions.
    """
    return code in PERMISSION_CODES


# ---------------------------------------------------------------------------
# Role templates
# ---------------------------------------------------------------------------

# Bundles for readability. The composition of each template is below.
_VIEW_ALL = (
    P_ORG_VIEW.code,
    P_PERSONNEL_VIEW.code,
    P_QUALS_VIEW.code,
    P_ABSENCES_VIEW.code,
    P_TASKS_VIEW.code,
    P_WORKLISTS_VIEW.code,
    P_ALERTS_VIEW.code,
)

_OPS_WRITE = (
    P_PERSONNEL_WRITE.code,
    P_QUALS_WRITE.code,
    P_ABSENCES_WRITE.code,
    P_TASKS_WRITE.code,
    P_WORKLISTS_WRITE.code,
    P_ALERTS_TRIAGE.code,
    P_ALERTS_ACT.code,
)

_OPS_ARCHIVE = (
    P_PERSONNEL_ARCHIVE.code,
    P_QUALS_ARCHIVE.code,
    P_ABSENCES_ARCHIVE.code,
    P_TASKS_ARCHIVE.code,
    P_WORKLISTS_ARCHIVE.code,
)


@dataclass(frozen=True)
class RoleTemplate:
    """Seed role definition.

    Seeded once per org at org-create time. Admins can edit per-org
    copies (rename, add/remove permissions) without affecting other
    tenants. The ``slug`` is stable across renames so migrations and
    tests can refer to a role without depending on its label.
    """

    slug: str
    name: str
    description: str
    permissions: tuple[str, ...]
    # Org-wide roles (owner/admin) cannot be scoped to a workcenter; the
    # admin UI hides the workcenter selector when this is False. Kept on
    # the template, not the role row, because admins can still detach the
    # default and create a custom workcenter-scoped variant.
    workcenter_scopable: bool = True


ROLE_OWNER = RoleTemplate(
    slug="org_owner",
    name="Org Owner",
    description=(
        "Full control of the organization. Cannot be scoped to a workcenter. "
        "An org always has at least one owner; the last owner cannot remove "
        "themselves."
    ),
    permissions=tuple(p.code for p in PERMISSIONS),
    workcenter_scopable=False,
)

ROLE_ADMIN = RoleTemplate(
    slug="org_admin",
    name="Org Admin",
    description=(
        "Manages members, invites, workcenters, and all operational data. "
        "Cannot transfer ownership; cannot destroy the org."
    ),
    permissions=(
        P_ORG_VIEW.code,
        P_ORG_INVITE.code,
        P_ORG_MANAGE_MEMBERS.code,
        P_ORG_MANAGE_WORKCENTERS.code,
        *_VIEW_ALL[1:],  # skip duplicate org.view
        *_OPS_WRITE,
        *_OPS_ARCHIVE,
    ),
    workcenter_scopable=False,
)

ROLE_LPO = RoleTemplate(
    slug="lpo",
    name="Team Lead",
    description=(
        "Full operational control: edit personnel, qualifications, absences, "
        "worklists, lock + amend, take alert actions."
    ),
    permissions=(
        *_VIEW_ALL,
        *_OPS_WRITE,
        *_OPS_ARCHIVE,
        P_WORKLISTS_LOCK.code,
        P_WORKLISTS_AMEND.code,
    ),
    workcenter_scopable=True,
)

ROLE_DLPO = RoleTemplate(
    slug="dlpo",
    name="Assistant Team Lead",
    description=("Same as Team Lead minus lock/amend authority."),
    permissions=(
        *_VIEW_ALL,
        *_OPS_WRITE,
        *_OPS_ARCHIVE,
    ),
    workcenter_scopable=True,
)

ROLE_MEMBER = RoleTemplate(
    slug="member",
    name="Member",
    description=(
        "Read-everything, write to operational data within scope. No "
        "archive, no lock/amend. Suitable for most rotated personnel."
    ),
    permissions=(
        *_VIEW_ALL,
        P_TASKS_WRITE.code,
        P_ABSENCES_WRITE.code,
        P_ALERTS_TRIAGE.code,
    ),
    workcenter_scopable=True,
)

ROLE_VIEWER = RoleTemplate(
    slug="viewer",
    name="Viewer",
    description=(
        "Read-only access. Suitable for stakeholders, auditors, "
        "departing personnel during turnover."
    ),
    permissions=_VIEW_ALL,
    workcenter_scopable=True,
)


ROLE_TEMPLATES: tuple[RoleTemplate, ...] = (
    ROLE_OWNER,
    ROLE_ADMIN,
    ROLE_LPO,
    ROLE_DLPO,
    ROLE_MEMBER,
    ROLE_VIEWER,
)


ROLE_TEMPLATE_SLUGS: FrozenSet[str] = frozenset(t.slug for t in ROLE_TEMPLATES)


def role_template(slug: str) -> RoleTemplate:
    """Look up a role template by slug. Raises if unknown."""
    for t in ROLE_TEMPLATES:
        if t.slug == slug:
            return t
    raise KeyError(f"unknown role template slug: {slug!r}")
