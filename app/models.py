"""
Database models for tbdtask.

Conventions:
- Effective-dated tables carry valid_from / valid_to (NULL = current row).
- All user-visible entities support soft-delete: active, archived_at, archived_reason.
- Imported rows are tagged with import_batch_id for provenance.
- Display ordering is explicit via display_order where the UI is order-sensitive.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .tenancy import TenantScopedMixin


# ---------------------------------------------------------------------------
# Organization (tenant root)
# ---------------------------------------------------------------------------


class Organization(Base):
    """A tenant. Every row in a tenant-scoped table FKs back to one of these.

    ``slug`` is the URL-stable identifier. ``settings_json`` holds per-org
    configuration (retention windows, allowed providers, sensitive-info
    re-acknowledgment interval) — fully populated in later phases.
    """

    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    settings_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )


# ---------------------------------------------------------------------------
# Identity & sessions (Phase 1)
# ---------------------------------------------------------------------------

# Allowed values for ``identity.provider``. Any new provider must land here
# AND in the OIDC client registry — the auth code asserts the two are in sync.
IDENTITY_PROVIDERS: tuple[str, ...] = ("apple", "google", "microsoft")

# Allowed values for ``org_membership.status``. ``pending`` covers an
# invite that hasn't been accepted; ``suspended`` keeps the row around (for
# audit + restoration) without granting access.
MEMBERSHIP_STATUSES: tuple[str, ...] = ("active", "pending", "suspended")


class UserAccount(Base):
    """A login identity. Not tied to any one organization.

    Auth is OIDC-only: there is no password column, no recovery token, no
    email-link fallback. Recovery routes through the upstream IdP. ``email``
    is the canonical contact address (lowercased, unique) but is *not* used
    on its own to identify a user during sign-in — that's always
    ``(provider, subject)`` from the IdP.
    """

    __tablename__ = "user_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )
    # Set when the account is administratively disabled. Sessions belonging
    # to a disabled user are revoked on next request, not retroactively
    # purged.
    disabled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    identities: Mapped[list["Identity"]] = relationship(back_populates="user")
    memberships: Mapped[list["OrgMembership"]] = relationship(back_populates="user")


class Identity(Base):
    """An OIDC identity attached to a ``UserAccount``.

    Lookup at sign-in is always ``(provider, subject)`` since ``subject`` is
    the only stable identifier across email changes upstream. Email-based
    matching is allowed only as a candidate for *explicit* linking — never
    for silent merge. Apple private-relay addresses are flagged via
    ``email_verified=False`` on the linking path even when Apple says
    verified, since they prove relay control, not mailbox control.
    """

    __tablename__ = "identities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user_accounts.id"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    subject: Mapped[str] = mapped_column(String(256), nullable=False)
    # Snapshot at link time; used only for display, never for lookup.
    email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )

    user: Mapped["UserAccount"] = relationship(back_populates="identities")

    __table_args__ = (
        UniqueConstraint("provider", "subject", name="uq_identity_provider_subject"),
        CheckConstraint(
            "provider IN ('apple','google','microsoft')",
            name="ck_identity_provider",
        ),
    )


class OrgMembership(Base):
    """Links a ``UserAccount`` to an ``Organization`` with a status.

    Roles attach via the ``membership_roles`` join table (Phase 2). A
    membership can hold multiple roles, and each role-grant can be
    org-wide or scoped to a specific workcenter (and its descendants).
    Until Phase 2's permission gates ship, every active member has the
    same effective permissions ("logged in"); the catalog and the
    ``@require`` decorator land together so tests can pin behaviour
    end-to-end.
    """

    __tablename__ = "org_memberships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user_accounts.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    suspended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    user: Mapped["UserAccount"] = relationship(back_populates="memberships")
    organization: Mapped["Organization"] = relationship()
    role_grants: Mapped[list["MembershipRole"]] = relationship(
        back_populates="membership", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # A user can have at most one membership per org; multi-org users
        # have separate rows. Distinct memberships per status (e.g. archived
        # + active) are not modelled — we soft-suspend instead.
        UniqueConstraint("org_id", "user_id", name="uq_org_membership_org_user"),
        CheckConstraint(
            "status IN ('active','pending','suspended')",
            name="ck_org_membership_status",
        ),
    )


class UserSession(Base):
    """Server-side session record. The cookie carries only the opaque ``id``.

    Every authenticated request looks the row up, verifies ``revoked_at IS
    NULL``, ``last_seen_at`` within the idle window, and ``created_at``
    within the absolute window. Suspending a membership or disabling a user
    revokes their sessions on the next request, not retroactively (sessions
    cleaned up by a periodic sweep — Phase 7).

    Not tenant-scoped: a user can hold sessions across multiple orgs in
    different tabs/devices, each session bound to a specific membership.
    """

    __tablename__ = "user_sessions"

    # Random URL-safe token (256 bits). Stored as the primary key so cookie
    # validation is a single PK lookup. Never logged in plaintext.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user_accounts.id"), nullable=False, index=True
    )
    # Nullable until the user picks an org (zero-org users land on
    # ``/no-orgs`` with a session that has no membership bound).
    current_membership_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("org_memberships.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)


class OrgInvite(Base):
    """A signed invite token an org admin issues so an outsider can join.

    Invite tokens are stored hashed so a DB leak doesn't expose live
    invitations. The signed token (sent to the invitee out-of-band) is
    never persisted in plaintext. ``intended_email`` is required — the
    accept flow refuses to bind the invite to any other user.

    The admin also provides the person's name and rate/title so that on
    acceptance a ``Person`` record is auto-created. The invitee then only
    needs to fill in their PRD and other details.
    """

    __tablename__ = "org_invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    # SHA-256 hex digest of the raw invite token. Lookup uses this; the
    # raw token never touches the DB.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # Required: an invite is bound to one specific email and is rejected
    # if the redeeming user's canonical email differs. Prevents a leaked
    # invite from being claimed by anyone but the intended recipient.
    intended_email: Mapped[str] = mapped_column(String(320), nullable=False)
    # Personnel details the admin provides so the Person record is
    # auto-created on acceptance. The invitee fills in PRD, arrival, etc.
    first_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    rate: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    paygrade: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("user_accounts.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    accepted_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("user_accounts.id"), nullable=True
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class AuthEvent(Base):
    """Minimal audit trail for auth-flow events.

    Phase 4 introduces the full ``audit_event`` table with structured
    before/after diffs for tenant data; this table covers only the
    auth-layer events that need to be auditable from day one (login,
    logout, link, unlink, suspended-session-revoke). Kept separate so the
    auth audit trail survives even if the broader audit subsystem is
    misconfigured.
    """

    __tablename__ = "auth_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("user_accounts.id"), nullable=True, index=True
    )
    session_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Free-form, but never contains tokens, secrets, or full IdP responses
    # — only metadata like ``{"reason": "membership_suspended"}``.
    detail: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False, index=True
    )


# ---------------------------------------------------------------------------
# Authorization (Phase 2): workcenters, roles, permission grants
# ---------------------------------------------------------------------------


class Workcenter(Base):
    """A nested grouping inside an org used for permission scoping.

    Workcenters live alongside the personnel/qual data that already
    carries ``org_id``; a workcenter is itself tenant-scoped via the
    explicit ``org_id`` column (Phase 2 introduces the table after the
    Phase 0 backfill, so it doesn't go through ``TenantScopedMixin``;
    Phase 3 brings RLS uniformly).

    Nesting is a self-referencing FK; depth is unbounded but the
    permission walker caps lookups at a small depth in practice (LCPO
    ► LPO ► DLPO ► Member is typical). A grant on a parent workcenter
    is honoured for descendants: see
    ``app.auth.authorization.has_permission``.

    Soft-archive only — historical workcenter associations stay valid
    for audit even after the workcenter is decommissioned.
    """

    __tablename__ = "workcenters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("workcenters.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )

    parent: Mapped[Optional["Workcenter"]] = relationship(
        remote_side=[id], foreign_keys=[parent_id]
    )

    __table_args__ = (
        # Slug is unique within an org. Different orgs can both have
        # "deck" without clashing.
        UniqueConstraint("org_id", "slug", name="uq_workcenter_org_slug"),
    )


class Role(Base):
    """A named bundle of permissions, scoped to one organization.

    Each org gets its own seeded copy of every entry in
    ``app.auth.permissions.ROLE_TEMPLATES`` at org-creation time so
    admins can edit, rename, or extend role definitions per-tenant
    without affecting other orgs.

    ``template_slug`` records the seed template the row was minted from
    (or ``NULL`` for custom roles authored in the admin UI). Used only
    for migrations + audit; it is *not* the primary key, and renaming a
    role doesn't change it. The slug is what the runtime authorization
    layer uses to look up "is this the org_owner role?" for invariants
    like "an org always has at least one owner".

    ``builtin`` rows can be edited but not deleted — admins might rename
    "LPO" to fit local culture but should never be able to remove the
    seeded baseline. Custom roles (``builtin=False``) are fully
    deletable when no membership references them.
    """

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    template_slug: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    workcenter_scopable: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )

    permissions: Mapped[list["RolePermission"]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # Role names unique per-org. Rename collisions are an admin-UI
        # validation, but the constraint is the durable defence.
        UniqueConstraint("org_id", "name", name="uq_role_org_name"),
        # Built-in roles: at most one row per (org, template_slug) so
        # the seed step is naturally idempotent. Custom roles have
        # template_slug NULL and aren't subject to this constraint;
        # SQLite doesn't enforce uniqueness on NULL columns by default,
        # which is what we want here.
        UniqueConstraint("org_id", "template_slug", name="uq_role_org_template_slug"),
    )


class RolePermission(Base):
    """One permission grant on a role.

    The ``permission_code`` is one of the strings from
    ``app.auth.permissions.PERMISSIONS``. Tests assert every row matches
    a known code; the route handler that edits roles validates against
    the catalog before insertion.

    No ``org_id`` column: the row inherits scope from its role.
    """

    __tablename__ = "role_permissions"

    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_code: Mapped[str] = mapped_column(String(64), primary_key=True)

    role: Mapped["Role"] = relationship(back_populates="permissions")


class MembershipRole(Base):
    """Attach a role to an org membership, optionally workcenter-scoped.

    ``workcenter_id`` is NULL for org-wide grants. When set, the grant
    applies to that workcenter and all of its descendants — see the
    walker in ``app.auth.authorization``.

    Composite uniqueness: a membership can have the same role granted
    org-wide AND scoped to a specific workcenter (sometimes useful for
    e.g. "DLPO over the org but specifically also a Member of Deck"),
    but the same (membership, role, workcenter) tuple can't repeat.
    """

    __tablename__ = "membership_roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    membership_id: Mapped[int] = mapped_column(
        ForeignKey("org_memberships.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workcenter_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("workcenters.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    granted_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("user_accounts.id"), nullable=True
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )

    membership: Mapped["OrgMembership"] = relationship(back_populates="role_grants")
    role: Mapped["Role"] = relationship()
    workcenter: Mapped[Optional["Workcenter"]] = relationship()

    __table_args__ = (
        # SQLite doesn't enforce uniqueness on rows whose NULLable column
        # is NULL, which is exactly the behaviour we want here:
        # multiple workcenter-scoped grants of the same role to the same
        # membership are allowed (one per workcenter), and at most one
        # org-wide grant of that role.
        UniqueConstraint(
            "membership_id",
            "role_id",
            "workcenter_id",
            name="uq_membership_role_scope",
        ),
    )


# ---------------------------------------------------------------------------
# Mixins
# ---------------------------------------------------------------------------


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )


class SoftDeleteMixin:
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    archived_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ProvenanceMixin:
    import_batch_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("import_batches.id"), nullable=True, index=True
    )


# ---------------------------------------------------------------------------
# Provenance / import tracking
# ---------------------------------------------------------------------------


class ImportBatch(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_file: Mapped[str] = mapped_column(String(512), nullable=False)
    source_workbook_version: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    row_counts: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


# ---------------------------------------------------------------------------
# Personnel
# ---------------------------------------------------------------------------


class Person(Base, TimestampMixin, SoftDeleteMixin, ProvenanceMixin, TenantScopedMixin):
    __tablename__ = "persons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_name: Mapped[str] = mapped_column(String(128), nullable=False)
    first_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    full_display: Mapped[str] = mapped_column(String(256), nullable=False)
    position: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Incoming / arrival tracking. Populated for personnel still en route;
    # cleared (or just ignored) once they're on board and active.
    arrival_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    sponsor_person_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("persons.id"), nullable=True
    )
    orders_received: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    itinerary_received: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    aob_scheduled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    barracks_assigned: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    rates: Mapped[list["PersonRate"]] = relationship(back_populates="person")
    duty_sections: Mapped[list["PersonDutySection"]] = relationship(
        back_populates="person"
    )
    prds: Mapped[list["PersonPrd"]] = relationship(back_populates="person")
    roster_statuses: Mapped[list["PersonRosterStatus"]] = relationship(
        back_populates="person"
    )
    drivers_licenses: Mapped[list["PersonDriversLicense"]] = relationship(
        back_populates="person"
    )
    quals: Mapped[list["PersonQual"]] = relationship(back_populates="person")
    absences: Mapped[list["Absence"]] = relationship(back_populates="person")
    sponsor: Mapped[Optional["Person"]] = relationship(
        remote_side=[id], foreign_keys=[sponsor_person_id]
    )


def _effective_date_cols():
    return (
        mapped_column(Date, nullable=False),
        mapped_column(Date, nullable=True),
    )


class PersonRate(Base, TimestampMixin, ProvenanceMixin, TenantScopedMixin):
    __tablename__ = "person_rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id"), nullable=False, index=True
    )
    rate: Mapped[str] = mapped_column(String(32), nullable=False)
    paygrade: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    person: Mapped["Person"] = relationship(back_populates="rates")

    __table_args__ = (Index("ix_person_rates_current", "person_id", "valid_to"),)


class PersonDutySection(Base, TimestampMixin, ProvenanceMixin, TenantScopedMixin):
    __tablename__ = "person_duty_sections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id"), nullable=False, index=True
    )
    duty_section: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    person: Mapped["Person"] = relationship(back_populates="duty_sections")

    __table_args__ = (
        Index("ix_person_duty_sections_current", "person_id", "valid_to"),
    )


class PersonPrd(Base, TimestampMixin, ProvenanceMixin, TenantScopedMixin):
    """Projected Rotation Date. New row per change (initial / extension / correction)."""

    __tablename__ = "person_prds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id"), nullable=False, index=True
    )
    prd_date: Mapped[date] = mapped_column(Date, nullable=False)
    change_reason: Mapped[str] = mapped_column(
        String(32), default="initial", nullable=False
    )
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    person: Mapped["Person"] = relationship(back_populates="prds")


class PersonRosterStatus(Base, TimestampMixin, ProvenanceMixin, TenantScopedMixin):
    __tablename__ = "person_roster_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    person: Mapped["Person"] = relationship(back_populates="roster_statuses")

    __table_args__ = (
        CheckConstraint(
            "status IN ('active','incoming','prd_pending','departed','dropped')",
            name="ck_roster_status_value",
        ),
    )


class PersonDriversLicense(Base, TimestampMixin, ProvenanceMixin, TenantScopedMixin):
    __tablename__ = "person_drivers_licenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id"), nullable=False, index=True
    )
    has_license: Mapped[bool] = mapped_column(Boolean, nullable=False)
    expires_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    person: Mapped["Person"] = relationship(back_populates="drivers_licenses")


# ---------------------------------------------------------------------------
# Qualifications
# ---------------------------------------------------------------------------


class Qualification(
    Base, TimestampMixin, SoftDeleteMixin, ProvenanceMixin, TenantScopedMixin
):
    __tablename__ = "qualifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    code: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    validity_period_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pinned_column: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    person_quals: Mapped[list["PersonQual"]] = relationship(
        back_populates="qualification"
    )

    __table_args__ = (UniqueConstraint("name", name="uq_qualification_name"),)


PERSON_QUAL_STATUSES = (
    "not_assigned",
    "assigned",
    "in_progress",
    "qualified",
    "dinq",
    "expired",
    "waived",
)


class PersonQual(
    Base, TimestampMixin, SoftDeleteMixin, ProvenanceMixin, TenantScopedMixin
):
    __tablename__ = "person_quals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id"), nullable=False, index=True
    )
    qual_id: Mapped[int] = mapped_column(
        ForeignKey("qualifications.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    achieved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    person: Mapped["Person"] = relationship(back_populates="quals")
    qualification: Mapped["Qualification"] = relationship(back_populates="person_quals")

    __table_args__ = (
        CheckConstraint(
            "status IN ('not_assigned','assigned','in_progress','qualified','dinq','expired','waived')",
            name="ck_person_qual_status",
        ),
        Index("ix_person_quals_current", "person_id", "qual_id", "valid_to"),
    )


# ---------------------------------------------------------------------------
# Absences
# ---------------------------------------------------------------------------


class AbsenceCode(Base, TimestampMixin, SoftDeleteMixin, TenantScopedMixin):
    __tablename__ = "absence_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Code is unique per-org (Phase 3 swaps the global UNIQUE for a composite
    # one over (org_id, code)). Kept globally unique for now since the
    # backfilled DB has only one org.
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Absence(
    Base, TimestampMixin, SoftDeleteMixin, ProvenanceMixin, TenantScopedMixin
):
    __tablename__ = "absences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id"), nullable=False, index=True
    )
    code_id: Mapped[int] = mapped_column(ForeignKey("absence_codes.id"), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    end_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    person: Mapped["Person"] = relationship(back_populates="absences")
    code: Mapped["AbsenceCode"] = relationship()

    __table_args__ = (Index("ix_absences_dates", "start_date", "end_date"),)


# ---------------------------------------------------------------------------
# Crews (plumbing only for now)
# ---------------------------------------------------------------------------


class Crew(Base, TimestampMixin, SoftDeleteMixin, TenantScopedMixin):
    __tablename__ = "crews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Same per-org-uniqueness story as AbsenceCode.code — global UNIQUE for
    # now, becomes (org_id, name) composite in Phase 3.
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class CrewMembership(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "crew_memberships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    crew_id: Mapped[int] = mapped_column(
        ForeignKey("crews.id"), nullable=False, index=True
    )
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id"), nullable=False, index=True
    )
    role: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


class TaskCategory(Base, TimestampMixin, SoftDeleteMixin, TenantScopedMixin):
    __tablename__ = "task_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Same per-org-uniqueness story as AbsenceCode/Crew. Phase 3 swaps the
    # global UNIQUE for (org_id, name).
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


CARRY_OVER_POLICIES = (
    "auto_same_person",
    "auto_any_qualified",
    "never",
    "manual_prompt",
)


class TaskTemplate(
    Base, TimestampMixin, SoftDeleteMixin, ProvenanceMixin, TenantScopedMixin
):
    __tablename__ = "task_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    category_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("task_categories.id"), nullable=True
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    estimated_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    splittable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reassignable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    carry_over_policy: Mapped[str] = mapped_column(
        String(32), default="auto_same_person", nullable=False
    )
    recurrence_rule: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    required_drivers_license: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    required_duty_section: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    required_crew_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("crews.id"), nullable=True
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    category: Mapped[Optional["TaskCategory"]] = relationship()

    __table_args__ = (
        CheckConstraint(
            "carry_over_policy IN ('auto_same_person','auto_any_qualified','never','manual_prompt')",
            name="ck_task_carry_over_policy",
        ),
    )


class TaskTemplateRequiredQual(Base):
    __tablename__ = "task_template_required_quals"

    task_template_id: Mapped[int] = mapped_column(
        ForeignKey("task_templates.id"), primary_key=True
    )
    qual_id: Mapped[int] = mapped_column(
        ForeignKey("qualifications.id"), primary_key=True
    )


TASK_INSTANCE_STATUSES = (
    "open",
    "in_progress",
    "done",
    "discarded",
    "carried",
)


class Worklist(
    Base, TimestampMixin, SoftDeleteMixin, ProvenanceMixin, TenantScopedMixin
):
    __tablename__ = "worklists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    week_starting: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("worklists.id"), nullable=True
    )
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    locked_by_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    amended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    amendment_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    operator_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class TaskInstance(
    Base, TimestampMixin, SoftDeleteMixin, ProvenanceMixin, TenantScopedMixin
):
    __tablename__ = "task_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("task_templates.id"), nullable=True
    )
    worklist_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("worklists.id"), nullable=True, index=True
    )
    scheduled_date: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True, index=True
    )
    category_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("task_categories.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    completion_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Single per-task hours figure. Every assignee on the task receives
    # full credit for these hours when rolling up personnel stats.
    hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    carried_from_instance_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("task_instances.id"), nullable=True
    )
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    assignments: Mapped[list["TaskAssignment"]] = relationship(
        back_populates="instance"
    )
    category: Mapped[Optional["TaskCategory"]] = relationship()

    __table_args__ = (
        CheckConstraint(
            "status IN ('open','in_progress','done','discarded','carried')",
            name="ck_task_instance_status",
        ),
    )


class TaskAssignment(Base, TimestampMixin, SoftDeleteMixin, TenantScopedMixin):
    __tablename__ = "task_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[int] = mapped_column(
        ForeignKey("task_instances.id"), nullable=False, index=True
    )
    person_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("persons.id"), nullable=True
    )
    is_poic: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    external_poic_name: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    completion_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    hours_worked: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    instance: Mapped["TaskInstance"] = relationship(back_populates="assignments")
    person: Mapped[Optional["Person"]] = relationship()

    __table_args__ = (
        CheckConstraint(
            "person_id IS NOT NULL OR external_poic_name IS NOT NULL",
            name="ck_assignment_has_subject",
        ),
        Index(
            "ux_one_poic_per_instance",
            "instance_id",
            unique=True,
            sqlite_where=text("is_poic = 1"),
        ),
    )


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


class Alert(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    person_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("persons.id"), nullable=True, index=True
    )
    task_instance_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("task_instances.id"), nullable=True
    )
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    dismissed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    snoozed_until: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class Setting(Base, TimestampMixin):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Data audit log (Phase 4)
# ---------------------------------------------------------------------------


class DataAuditEvent(Base, TenantScopedMixin):
    """Immutable record of who changed what tenant data and when.

    Each row captures a single write operation (create/update/delete/archive)
    on a tenant-scoped table. The ``action`` field identifies the operation
    type; ``table_name`` and ``row_id`` identify the target; ``before_json``
    and ``after_json`` carry the old/new values (null for create/delete).

    Rows are INSERT-only at the application layer; Phase 3 RLS enforces
    INSERT-only at the DB level.
    """

    __tablename__ = "data_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    """One of: create, update, delete, archive, unarchive, lock, amend."""

    table_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    """The database table that was modified (e.g. 'persons', 'absences')."""

    row_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    """Primary key of the modified row."""

    actor_membership_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("org_memberships.id"), nullable=True
    )
    """The org membership that performed the action. Null for system actions."""

    before_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    """Snapshot of the row before the change (null for create)."""

    after_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    """Snapshot of the row after the change (null for delete)."""

    detail: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    """Additional context: field-level diffs, route path, user agent, etc."""

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False, index=True
    )

    __table_args__ = (
        Index("ix_data_audit_table_row", "table_name", "row_id"),
        Index("ix_data_audit_actor", "actor_membership_id"),
    )
