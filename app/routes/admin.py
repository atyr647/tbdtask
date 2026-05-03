"""Org-administration UI: invites, members, roles, workcenters.

Phase 2 surfaces. Every route here is gated by an ``org.*`` permission;
the dashboard nav surfaces them only when the active membership has
``org.view``.

Layout (one ``/admin`` prefix, sub-pages by resource):

* ``GET  /admin``                           — landing page; links to the rest
* ``GET  /admin/members``                   — list active + suspended members,
*                                              with their role grants
* ``POST /admin/members/{id}/suspend``      — toggle suspended flag, revoke
*                                              that membership's sessions
* ``POST /admin/members/{id}/restore``      — un-suspend
* ``POST /admin/members/{id}/roles``        — grant a role (org-wide or
*                                              workcenter-scoped)
* ``POST /admin/members/{id}/roles/{grant_id}/revoke`` — revoke a grant
* ``GET  /admin/invites``                   — list outstanding + recent
* ``POST /admin/invites``                   — issue a new invite (returns
*                                              the raw token once)
* ``POST /admin/invites/{id}/revoke``       — mark an invite revoked
* ``GET  /admin/roles``                     — list roles for the org
* ``GET  /admin/roles/new``                 — custom-role form
* ``POST /admin/roles``                     — create custom role
* ``GET  /admin/roles/{id}/edit``           — edit role's name/perms
* ``POST /admin/roles/{id}``                — save edits
* ``POST /admin/roles/{id}/archive``        — archive non-builtin role
* ``GET  /admin/workcenters``               — tree view
* ``POST /admin/workcenters``               — create
* ``POST /admin/workcenters/{id}``          — edit
* ``POST /admin/workcenters/{id}/archive``  — archive

All POSTs are CSRF-protected by the global middleware. Mutating
helpers run inside the ambient tenant context entered by
``SessionMiddleware``, so writes can never cross orgs.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import models as M
from ..auth import invites as invites_mod
from ..auth import sessions as sess_mod
from ..auth.step_up import require_step_up
from ..auth.authorization import (
    membership_has_role_template,
    require,
)
from ..auth.dependencies import get_current_user, get_db, require_membership
from ..auth.permissions import (
    PERMISSIONS,
    P_ORG_ADMIN,
    P_ORG_INVITE,
    P_ORG_MANAGE_MEMBERS,
    P_ORG_MANAGE_WORKCENTERS,
    P_ORG_VIEW,
    is_known_permission,
)
from ..templating import render


router = APIRouter(prefix="/admin")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    """Conservative slug — same algorithm as onboarding's org slug."""
    s = _SLUG_RE.sub("-", name.lower()).strip("-")
    return s or "wc"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _resolve_org_id(membership: M.OrgMembership) -> int:
    """Pull the active org id off the membership.

    The session middleware enters ``tenant_context(membership.org_id)``
    for the duration of the request, so every query implicitly scopes
    to this org. We keep the raw id around for the few places that
    need it explicitly (creating a workcenter, validating that an
    invite belongs to the active org, etc.).
    """
    return membership.org_id


# ---------------------------------------------------------------------------
# Landing
# ---------------------------------------------------------------------------


@router.get("")
def admin_index(
    request: Request,
    membership: M.OrgMembership = Depends(require_membership),
    user: Optional[M.UserAccount] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _: None = Depends(require(P_ORG_VIEW)),
):
    org = db.get(M.Organization, _resolve_org_id(membership))
    org_id = _resolve_org_id(membership)
    is_owner = membership_has_role_template(db, membership.id, "org_owner")

    # Check if this is the sole active member (for org deletion option).
    active_count = (
        db.execute(
            select(M.OrgMembership).where(
                M.OrgMembership.org_id == org_id,
                M.OrgMembership.status == "active",
            )
        )
        .scalars()
        .all()
    )
    is_sole_owner = is_owner and len(active_count) == 1

    return render(
        request,
        "admin/index.html",
        org=org,
        is_owner=is_owner,
        is_sole_owner=is_sole_owner,
    )


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


@router.get("/members")
def list_members(
    request: Request,
    membership: M.OrgMembership = Depends(require_membership),
    db: Session = Depends(get_db),
    _: None = Depends(require(P_ORG_VIEW)),
):
    org_id = _resolve_org_id(membership)
    rows = db.execute(
        select(M.OrgMembership, M.UserAccount)
        .join(M.UserAccount, M.UserAccount.id == M.OrgMembership.user_id)
        .where(M.OrgMembership.org_id == org_id)
        .order_by(M.OrgMembership.id)
    ).all()
    members = []
    for m, u in rows:
        grants = db.execute(
            select(M.MembershipRole, M.Role, M.Workcenter)
            .join(M.Role, M.Role.id == M.MembershipRole.role_id)
            .outerjoin(M.Workcenter, M.Workcenter.id == M.MembershipRole.workcenter_id)
            .where(M.MembershipRole.membership_id == m.id)
            .order_by(M.Role.name)
        ).all()
        members.append(
            {
                "membership": m,
                "user": u,
                "grants": [
                    {
                        "grant": g,
                        "role": r,
                        "workcenter": wc,
                    }
                    for g, r, wc in grants
                ],
            }
        )

    roles = list(
        db.scalars(
            select(M.Role)
            .where(M.Role.org_id == org_id, M.Role.archived_at.is_(None))
            .order_by(M.Role.builtin.desc(), M.Role.name)
        ).all()
    )
    workcenters = list(
        db.scalars(
            select(M.Workcenter)
            .where(
                M.Workcenter.org_id == org_id,
                M.Workcenter.archived_at.is_(None),
            )
            .order_by(M.Workcenter.display_order, M.Workcenter.name)
        ).all()
    )
    return render(
        request,
        "admin/members.html",
        members=members,
        roles=roles,
        workcenters=workcenters,
        # Show the "Last Owner" guard inline so the UI can hide the
        # suspend button on the only owner. Phase 2 enforces it server-
        # side too; this is only a courtesy.
        last_owner_membership_id=_last_owner_id(db, org_id),
    )


def _last_owner_id(db: Session, org_id: int) -> Optional[int]:
    """Return the lone owner's membership id, or None if there are 2+."""
    owners = list(
        db.scalars(
            select(M.MembershipRole.membership_id)
            .join(M.Role, M.Role.id == M.MembershipRole.role_id)
            .join(
                M.OrgMembership,
                M.OrgMembership.id == M.MembershipRole.membership_id,
            )
            .where(
                M.OrgMembership.org_id == org_id,
                M.OrgMembership.status == "active",
                M.Role.template_slug == "org_owner",
                M.MembershipRole.workcenter_id.is_(None),
            )
        ).all()
    )
    if len(owners) == 1:
        return owners[0]
    return None


@router.post("/members/{member_id}/suspend")
def suspend_member(
    member_id: int,
    membership: M.OrgMembership = Depends(require_membership),
    user: Optional[M.UserAccount] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _: None = Depends(require(P_ORG_MANAGE_MEMBERS)),
    __: None = require_step_up("admin_grant"),
):
    org_id = _resolve_org_id(membership)
    target = db.get(M.OrgMembership, member_id)
    if target is None or target.org_id != org_id:
        raise HTTPException(404, "membership not found")
    if target.id == membership.id:
        # Bouncing yourself is a Phase-7 escape hatch via platform admin,
        # not an everyday operation. Refuse here so admins don't lock
        # themselves out by accident.
        raise HTTPException(400, "you cannot suspend your own membership")
    if target.id == _last_owner_id(db, org_id):
        raise HTTPException(400, "cannot suspend the last remaining org owner")
    target.status = "suspended"
    target.suspended_at = _now()
    sess_mod.revoke_all_for_membership(db, target.id)
    db.add(
        M.AuthEvent(
            kind="membership_suspended",
            user_id=(user.id if user else None),
            session_id=None,
            provider=None,
            detail={"target_membership_id": target.id, "org_id": org_id},
        )
    )
    return RedirectResponse("/admin/members", status_code=303)


@router.post("/members/{member_id}/restore")
def restore_member(
    member_id: int,
    membership: M.OrgMembership = Depends(require_membership),
    user: Optional[M.UserAccount] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _: None = Depends(require(P_ORG_MANAGE_MEMBERS)),
):
    org_id = _resolve_org_id(membership)
    target = db.get(M.OrgMembership, member_id)
    if target is None or target.org_id != org_id:
        raise HTTPException(404, "membership not found")
    target.status = "active"
    target.suspended_at = None
    db.add(
        M.AuthEvent(
            kind="membership_restored",
            user_id=(user.id if user else None),
            session_id=None,
            provider=None,
            detail={"target_membership_id": target.id, "org_id": org_id},
        )
    )
    return RedirectResponse("/admin/members", status_code=303)


@router.post("/members/{member_id}/roles")
def grant_role(
    member_id: int,
    role_id: int = Form(...),
    workcenter_id: Optional[int] = Form(None),
    membership: M.OrgMembership = Depends(require_membership),
    user: Optional[M.UserAccount] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _: None = Depends(require(P_ORG_MANAGE_MEMBERS)),
    __: None = require_step_up("admin_grant"),
):
    org_id = _resolve_org_id(membership)
    target = db.get(M.OrgMembership, member_id)
    if target is None or target.org_id != org_id:
        raise HTTPException(404, "membership not found")
    role = db.get(M.Role, role_id)
    if role is None or role.org_id != org_id or role.archived_at is not None:
        raise HTTPException(404, "role not found")

    wc_id: Optional[int] = workcenter_id if workcenter_id else None
    if wc_id is not None:
        wc = db.get(M.Workcenter, wc_id)
        if wc is None or wc.org_id != org_id or wc.archived_at is not None:
            raise HTTPException(404, "workcenter not found")
        if not role.workcenter_scopable:
            raise HTTPException(
                400,
                "this role cannot be scoped to a workcenter",
            )

    # Idempotent: same (membership, role, workcenter) means no-op.
    existing = db.execute(
        select(M.MembershipRole).where(
            M.MembershipRole.membership_id == target.id,
            M.MembershipRole.role_id == role.id,
            (
                M.MembershipRole.workcenter_id.is_(None)
                if wc_id is None
                else M.MembershipRole.workcenter_id == wc_id
            ),
        )
    ).scalar_one_or_none()
    if existing is None:
        grant = M.MembershipRole(
            membership_id=target.id,
            role_id=role.id,
            workcenter_id=wc_id,
            granted_by_user_id=(user.id if user else None),
        )
        db.add(grant)
        db.add(
            M.AuthEvent(
                kind="role_granted",
                user_id=(user.id if user else None),
                session_id=None,
                provider=None,
                detail={
                    "target_membership_id": target.id,
                    "role_id": role.id,
                    "role_template_slug": role.template_slug,
                    "workcenter_id": wc_id,
                    "org_id": org_id,
                },
            )
        )
    return RedirectResponse("/admin/members", status_code=303)


@router.post("/members/{member_id}/roles/{grant_id}/revoke")
def revoke_role(
    member_id: int,
    grant_id: int,
    membership: M.OrgMembership = Depends(require_membership),
    user: Optional[M.UserAccount] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _: None = Depends(require(P_ORG_MANAGE_MEMBERS)),
    __: None = require_step_up("admin_grant"),
):
    org_id = _resolve_org_id(membership)
    grant = db.get(M.MembershipRole, grant_id)
    if grant is None or grant.membership_id != member_id:
        raise HTTPException(404, "grant not found")
    target = db.get(M.OrgMembership, member_id)
    if target is None or target.org_id != org_id:
        raise HTTPException(404, "membership not found")
    role = db.get(M.Role, grant.role_id)

    # "Last owner" guard: refuse to remove the only org_owner grant
    # left in the org. Only enforced at the org-wide level (a
    # workcenter-scoped owner makes no sense; the role isn't
    # workcenter-scopable, but we still check defensively).
    if (
        role is not None
        and role.template_slug == "org_owner"
        and grant.workcenter_id is None
        and target.id == _last_owner_id(db, org_id)
    ):
        raise HTTPException(400, "cannot remove the last remaining org owner")

    db.delete(grant)
    db.add(
        M.AuthEvent(
            kind="role_revoked",
            user_id=(user.id if user else None),
            session_id=None,
            provider=None,
            detail={
                "target_membership_id": target.id,
                "role_id": grant.role_id,
                "role_template_slug": role.template_slug if role else None,
                "workcenter_id": grant.workcenter_id,
                "org_id": org_id,
            },
        )
    )
    return RedirectResponse("/admin/members", status_code=303)


@router.post("/members/{member_id}/kick")
def kick_member(
    member_id: int,
    request: Request,
    membership: M.OrgMembership = Depends(require_membership),
    user: Optional[M.UserAccount] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _: None = Depends(require(P_ORG_MANAGE_MEMBERS)),
):
    """Remove a member from the organization.

    Server-side checks:
    * Cannot kick yourself (use leave-org instead).
    * Cannot kick the last org_owner (org would be orphaned).
    * Cannot kick a higher/equal role unless the kicker is org_owner.
    * Target must belong to the current org.
    * Audit event is logged.
    * If the kicked user's current session is bound to this org, that
      session is revoked.
    """
    org_id = _resolve_org_id(membership)
    target = db.get(M.OrgMembership, member_id)
    if target is None or target.org_id != org_id:
        raise HTTPException(404, "membership not found")

    # Cannot kick yourself.
    if target.user_id == user.id:
        raise HTTPException(
            400, "Cannot kick yourself. Use the leave-org function instead."
        )

    # Cannot kick the last org_owner.
    if target.id == _last_owner_id(db, org_id):
        raise HTTPException(400, "Cannot kick the last remaining org owner.")

    # Role hierarchy check: non-owners cannot kick owners.
    from ..auth.authorization import membership_has_role_template

    kicker_is_owner = membership_has_role_template(db, membership.id, "org_owner")
    target_is_owner = membership_has_role_template(db, target.id, "org_owner")
    if target_is_owner and not kicker_is_owner:
        raise HTTPException(403, "Only org owners can kick other org owners.")

    # Soft-delete: set status to suspended.
    target.status = "suspended"
    db.flush()

    # Revoke any sessions bound to this membership so the kicked user
    # is immediately logged out of this org.
    sess_mod.revoke_all_for_membership(db, target.id)

    db.add(
        M.AuthEvent(
            kind="member_kicked",
            user_id=(user.id if user else None),
            session_id=None,
            provider=None,
            detail={
                "kicked_membership_id": target.id,
                "kicked_user_id": target.user_id,
                "org_id": org_id,
            },
        )
    )

    return RedirectResponse("/admin/members", status_code=303)


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------


@router.get("/invites")
def list_invites(
    request: Request,
    membership: M.OrgMembership = Depends(require_membership),
    db: Session = Depends(get_db),
    _: None = Depends(require(P_ORG_INVITE)),
):
    org_id = _resolve_org_id(membership)
    rows = list(
        db.scalars(
            select(M.OrgInvite)
            .where(M.OrgInvite.org_id == org_id)
            .order_by(M.OrgInvite.created_at.desc())
            .limit(100)
        ).all()
    )
    now = _now()
    invites = [
        {
            "invite": inv,
            "expired": inv.expires_at < now,
            "redeemable": (
                inv.accepted_at is None
                and inv.revoked_at is None
                and inv.expires_at >= now
            ),
        }
        for inv in rows
    ]
    return render(
        request,
        "admin/invites.html",
        invites=invites,
        max_ttl_days=invites_mod.MAX_INVITE_TTL_DAYS,
        default_ttl_days=invites_mod.DEFAULT_INVITE_TTL_DAYS,
    )


@router.post("/invites")
def create_invite(
    request: Request,
    first_name: str = Form(""),
    last_name: str = Form(""),
    rate: str = Form(""),
    paygrade: str = Form(""),
    intended_email: str = Form(...),
    ttl_days: int = Form(invites_mod.DEFAULT_INVITE_TTL_DAYS),
    membership: M.OrgMembership = Depends(require_membership),
    user: Optional[M.UserAccount] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _: None = Depends(require(P_ORG_INVITE)),
    __: None = require_step_up("admin_grant"),
):
    org_id = _resolve_org_id(membership)

    # Auto-capitalize first letter of names for lazy admins.
    fn = first_name.strip()
    if fn:
        fn = fn[0].upper() + fn[1:] if len(fn) > 1 else fn.upper()
    ln = last_name.strip()
    if ln:
        ln = ln[0].upper() + ln[1:] if len(ln) > 1 else ln.upper()
    rt = rate.strip() or None
    pg = paygrade.strip() or None

    try:
        issued = invites_mod.create_invite(
            db,
            org_id=org_id,
            intended_email=intended_email,
            created_by_user_id=(user.id if user else None),
            ttl_days=ttl_days,
            first_name=fn or None,
            last_name=ln or None,
            rate=rt,
            paygrade=pg,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    db.add(
        M.AuthEvent(
            kind="invite_issued",
            user_id=(user.id if user else None),
            session_id=None,
            provider=None,
            detail={
                "invite_id": issued.invite_id,
                "intended_email": intended_email.strip().lower(),
                "ttl_days": ttl_days,
                "org_id": org_id,
            },
        )
    )
    db.commit()

    # Render the invite list page with the raw token shown once inline.
    # No redirect — the token never appears in URLs, browser history, or
    # referrer headers.
    org = db.get(M.Organization, org_id)
    rows = list(
        db.scalars(
            select(M.OrgInvite)
            .where(M.OrgInvite.org_id == org_id)
            .order_by(M.OrgInvite.created_at.desc())
            .limit(100)
        ).all()
    )
    now = _now()
    invites = [
        {
            "invite": inv,
            "expired": inv.expires_at < now,
            "redeemable": (
                inv.accepted_at is None
                and inv.revoked_at is None
                and inv.expires_at >= now
            ),
        }
        for inv in rows
    ]
    base_url = str(request.base_url).rstrip("/")
    invite_link = f"{base_url}/invites/accept?token={issued.raw_token}"
    return render(
        request,
        "admin/invites.html",
        org=org,
        invites=invites,
        max_ttl_days=invites_mod.MAX_INVITE_TTL_DAYS,
        default_ttl_days=invites_mod.DEFAULT_INVITE_TTL_DAYS,
        new_invite_link=invite_link,
        new_invite_email=intended_email.strip().lower(),
        new_invite_expires=issued.expires_at,
    )


@router.post("/invites/{invite_id}/revoke")
def revoke_invite(
    invite_id: int,
    membership: M.OrgMembership = Depends(require_membership),
    user: Optional[M.UserAccount] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _: None = Depends(require(P_ORG_INVITE)),
    __: None = require_step_up("admin_grant"),
):
    org_id = _resolve_org_id(membership)
    inv = db.get(M.OrgInvite, invite_id)
    if inv is None or inv.org_id != org_id:
        raise HTTPException(404, "invite not found")
    if inv.accepted_at is not None:
        raise HTTPException(400, "already accepted; cannot revoke")
    if inv.revoked_at is None:
        inv.revoked_at = _now()
    db.add(
        M.AuthEvent(
            kind="invite_revoked",
            user_id=(user.id if user else None),
            session_id=None,
            provider=None,
            detail={"invite_id": inv.id, "org_id": org_id},
        )
    )
    return RedirectResponse("/admin/invites", status_code=303)


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


@router.get("/roles")
def list_roles(
    request: Request,
    membership: M.OrgMembership = Depends(require_membership),
    db: Session = Depends(get_db),
    _: None = Depends(require(P_ORG_VIEW)),
):
    org_id = _resolve_org_id(membership)
    rows = list(
        db.scalars(
            select(M.Role)
            .where(M.Role.org_id == org_id)
            .order_by(M.Role.builtin.desc(), M.Role.name)
            .options(selectinload(M.Role.permissions))
        ).all()
    )
    return render(
        request,
        "admin/roles.html",
        roles=rows,
    )


@router.get("/roles/new")
def new_role_form(
    request: Request,
    membership: M.OrgMembership = Depends(require_membership),
    _: None = Depends(require(P_ORG_ADMIN)),
):
    return render(
        request,
        "admin/role_new.html",
        permissions=PERMISSIONS,
    )


@router.post("/roles")
async def create_role(
    request: Request,
    membership: M.OrgMembership = Depends(require_membership),
    user: Optional[M.UserAccount] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _: None = Depends(require(P_ORG_ADMIN)),
):
    form = await request.form()
    name = (form.get("name") or "").strip()
    description = (form.get("description") or "").strip() or None
    if not name or len(name) > 128:
        raise HTTPException(400, "name must be 1-128 characters")
    workcenter_scopable = bool(form.get("workcenter_scopable"))
    perms = [c for c in form.getlist("permissions") if is_known_permission(c)]
    if not perms:
        raise HTTPException(400, "at least one permission is required")

    org_id = _resolve_org_id(membership)
    role = M.Role(
        org_id=org_id,
        template_slug=None,
        name=name,
        description=description,
        builtin=False,
        workcenter_scopable=workcenter_scopable,
    )
    db.add(role)
    db.flush()
    for code in perms:
        db.add(M.RolePermission(role_id=role.id, permission_code=code))
    db.add(
        M.AuthEvent(
            kind="role_created",
            user_id=(user.id if user else None),
            session_id=None,
            provider=None,
            detail={
                "role_id": role.id,
                "role_name": role.name,
                "permission_count": len(perms),
                "org_id": org_id,
            },
        )
    )
    return RedirectResponse("/admin/roles", status_code=303)


@router.get("/roles/{role_id}/edit")
def edit_role_form(
    role_id: int,
    request: Request,
    membership: M.OrgMembership = Depends(require_membership),
    db: Session = Depends(get_db),
    _: None = Depends(require(P_ORG_ADMIN)),
):
    org_id = _resolve_org_id(membership)
    role = db.get(M.Role, role_id)
    if role is None or role.org_id != org_id:
        raise HTTPException(404, "role not found")
    granted = {
        rp.permission_code
        for rp in db.execute(
            select(M.RolePermission).where(M.RolePermission.role_id == role.id)
        ).scalars()
    }
    return render(
        request,
        "admin/role_edit.html",
        role=role,
        permissions=PERMISSIONS,
        granted=granted,
    )


@router.post("/roles/{role_id}")
async def update_role(
    role_id: int,
    request: Request,
    membership: M.OrgMembership = Depends(require_membership),
    user: Optional[M.UserAccount] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _: None = Depends(require(P_ORG_ADMIN)),
):
    org_id = _resolve_org_id(membership)
    role = db.get(M.Role, role_id)
    if role is None or role.org_id != org_id:
        raise HTTPException(404, "role not found")
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name or len(name) > 128:
        raise HTTPException(400, "name must be 1-128 characters")
    perms = [c for c in form.getlist("permissions") if is_known_permission(c)]
    if not perms:
        raise HTTPException(400, "at least one permission is required")

    # Owner-role invariant: an admin can rename "Org Owner" but cannot
    # remove the org.admin permission (or any other) from it. The
    # founding owner relies on the role retaining its full grant; if
    # we let an admin strip it down, "owner" stops meaning anything.
    # Easier rule: builtin owner role's permissions are immutable
    # though name/description are editable.
    if role.template_slug == "org_owner":
        # Check the catalog set against the current grants. Anything
        # missing is a refused edit.
        current = {
            rp.permission_code
            for rp in db.execute(
                select(M.RolePermission).where(M.RolePermission.role_id == role.id)
            ).scalars()
        }
        if set(perms) != current:
            raise HTTPException(
                400,
                "cannot change the permissions of the org_owner role; "
                "create a custom role instead",
            )

    role.name = name
    role.description = (form.get("description") or "").strip() or None
    if not role.builtin:
        role.workcenter_scopable = bool(form.get("workcenter_scopable"))

    # Replace the grant set wholesale.
    db.execute(
        M.RolePermission.__table__.delete().where(
            M.RolePermission.__table__.c.role_id == role.id
        )
    )
    for code in perms:
        db.add(M.RolePermission(role_id=role.id, permission_code=code))
    db.add(
        M.AuthEvent(
            kind="role_updated",
            user_id=(user.id if user else None),
            session_id=None,
            provider=None,
            detail={
                "role_id": role.id,
                "permission_count": len(perms),
                "org_id": org_id,
            },
        )
    )
    return RedirectResponse("/admin/roles", status_code=303)


@router.post("/roles/{role_id}/archive")
def archive_role(
    role_id: int,
    membership: M.OrgMembership = Depends(require_membership),
    user: Optional[M.UserAccount] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _: None = Depends(require(P_ORG_ADMIN)),
    __: None = require_step_up("admin_grant"),
):
    org_id = _resolve_org_id(membership)
    role = db.get(M.Role, role_id)
    if role is None or role.org_id != org_id:
        raise HTTPException(404, "role not found")
    if role.builtin:
        raise HTTPException(
            400, "built-in roles cannot be archived; archive a custom role instead"
        )
    in_use = db.execute(
        select(M.MembershipRole.id).where(M.MembershipRole.role_id == role.id).limit(1)
    ).scalar_one_or_none()
    if in_use is not None:
        raise HTTPException(400, "role is in use; revoke its grants before archiving")
    role.archived_at = _now()
    db.add(
        M.AuthEvent(
            kind="role_archived",
            user_id=(user.id if user else None),
            session_id=None,
            provider=None,
            detail={"role_id": role.id, "org_id": org_id},
        )
    )
    return RedirectResponse("/admin/roles", status_code=303)


# ---------------------------------------------------------------------------
# Workcenters
# ---------------------------------------------------------------------------


@router.get("/workcenters")
def list_workcenters(
    request: Request,
    membership: M.OrgMembership = Depends(require_membership),
    db: Session = Depends(get_db),
    _: None = Depends(require(P_ORG_VIEW)),
):
    org_id = _resolve_org_id(membership)
    rows = list(
        db.scalars(
            select(M.Workcenter)
            .where(M.Workcenter.org_id == org_id, M.Workcenter.archived_at.is_(None))
            .order_by(M.Workcenter.display_order, M.Workcenter.name)
        ).all()
    )
    # Build a parent → children map for the template.
    by_parent: dict[Optional[int], list[M.Workcenter]] = {}
    for w in rows:
        by_parent.setdefault(w.parent_id, []).append(w)
    return render(
        request,
        "admin/workcenters.html",
        all_workcenters=rows,
        by_parent=by_parent,
    )


@router.post("/workcenters")
def create_workcenter(
    name: str = Form(...),
    parent_id: Optional[int] = Form(None),
    description: Optional[str] = Form(None),
    membership: M.OrgMembership = Depends(require_membership),
    user: Optional[M.UserAccount] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _: None = Depends(require(P_ORG_MANAGE_WORKCENTERS)),
):
    org_id = _resolve_org_id(membership)
    cleaned_name = name.strip()
    if not cleaned_name or len(cleaned_name) > 128:
        raise HTTPException(400, "name must be 1-128 characters")

    if parent_id:
        parent = db.get(M.Workcenter, parent_id)
        if parent is None or parent.org_id != org_id:
            raise HTTPException(404, "parent workcenter not found")
        if parent.archived_at is not None:
            raise HTTPException(400, "parent workcenter is archived")

    base = _slugify(cleaned_name)
    slug = base
    suffix = 2
    while (
        db.execute(
            select(M.Workcenter).where(
                M.Workcenter.org_id == org_id, M.Workcenter.slug == slug
            )
        ).scalar_one_or_none()
        is not None
    ):
        slug = f"{base}-{suffix}"
        suffix += 1

    last_order = (
        db.scalar(
            select(M.Workcenter.display_order)
            .where(M.Workcenter.org_id == org_id)
            .order_by(M.Workcenter.display_order.desc())
            .limit(1)
        )
        or 0
    )
    wc = M.Workcenter(
        org_id=org_id,
        parent_id=parent_id or None,
        name=cleaned_name,
        slug=slug,
        description=(description or "").strip() or None,
        display_order=last_order + 1,
    )
    db.add(wc)
    db.add(
        M.AuthEvent(
            kind="workcenter_created",
            user_id=(user.id if user else None),
            session_id=None,
            provider=None,
            detail={
                "workcenter_slug": slug,
                "parent_id": parent_id,
                "org_id": org_id,
            },
        )
    )
    return RedirectResponse("/admin/workcenters", status_code=303)


@router.post("/workcenters/{wc_id}")
def update_workcenter(
    wc_id: int,
    name: str = Form(...),
    parent_id: Optional[int] = Form(None),
    description: Optional[str] = Form(None),
    membership: M.OrgMembership = Depends(require_membership),
    user: Optional[M.UserAccount] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _: None = Depends(require(P_ORG_MANAGE_WORKCENTERS)),
):
    org_id = _resolve_org_id(membership)
    wc = db.get(M.Workcenter, wc_id)
    if wc is None or wc.org_id != org_id:
        raise HTTPException(404, "workcenter not found")
    cleaned = name.strip()
    if not cleaned or len(cleaned) > 128:
        raise HTTPException(400, "name must be 1-128 characters")

    new_parent: Optional[int] = parent_id or None
    if new_parent is not None:
        if new_parent == wc.id:
            raise HTTPException(400, "a workcenter cannot be its own parent")
        # Walk up the proposed parent's ancestry to refuse a cycle.
        cursor: Optional[int] = new_parent
        seen: set[int] = set()
        while cursor is not None:
            if cursor in seen:
                raise HTTPException(400, "cycle detected in ancestry")
            seen.add(cursor)
            if cursor == wc.id:
                raise HTTPException(
                    400, "cannot move under one of this workcenter's descendants"
                )
            row = db.get(M.Workcenter, cursor)
            cursor = row.parent_id if row is not None else None

    wc.name = cleaned
    wc.parent_id = new_parent
    wc.description = (description or "").strip() or None
    db.add(
        M.AuthEvent(
            kind="workcenter_updated",
            user_id=(user.id if user else None),
            session_id=None,
            provider=None,
            detail={"workcenter_id": wc.id, "org_id": org_id},
        )
    )
    return RedirectResponse("/admin/workcenters", status_code=303)


@router.post("/workcenters/{wc_id}/archive")
def archive_workcenter(
    wc_id: int,
    membership: M.OrgMembership = Depends(require_membership),
    user: Optional[M.UserAccount] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _: None = Depends(require(P_ORG_MANAGE_WORKCENTERS)),
):
    org_id = _resolve_org_id(membership)
    wc = db.get(M.Workcenter, wc_id)
    if wc is None or wc.org_id != org_id:
        raise HTTPException(404, "workcenter not found")
    children = db.execute(
        select(M.Workcenter.id)
        .where(
            M.Workcenter.parent_id == wc.id,
            M.Workcenter.archived_at.is_(None),
        )
        .limit(1)
    ).scalar_one_or_none()
    if children is not None:
        raise HTTPException(
            400,
            "workcenter has active children; archive or re-parent them first",
        )
    wc.archived_at = _now()
    db.add(
        M.AuthEvent(
            kind="workcenter_archived",
            user_id=(user.id if user else None),
            session_id=None,
            provider=None,
            detail={"workcenter_id": wc.id, "org_id": org_id},
        )
    )
    return RedirectResponse("/admin/workcenters", status_code=303)
