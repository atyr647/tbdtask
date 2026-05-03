"""Routes for users between log-in and a bound organization.

* ``GET /no-orgs`` — landing page when the user has zero active
  memberships. Reachable only when authenticated. Offers three paths:
  pending-invite acceptance, self-serve org creation, and an invite-token
  redemption form.
* ``POST /orgs/create`` — any logged-in user can create a new org and is
  enrolled as the founding member. Phase 2 attaches "Org Owner" role.
* ``POST /invites/accept`` — redeems a signed invite token. The org admin
  UI that *issues* invites lands in Phase 2; the model + accept route
  ship now so admins can hand out tokens via out-of-band means.
* ``GET /orgs/select`` — picker shown when the user has 2+ active
  memberships. Issued straight after login.
* ``POST /orgs/select`` — binds the chosen membership to the session and
  rotates the session id. CSRF protected.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as M
from ..auth import invites as invites_mod
from ..auth import sessions as sess_mod
from ..auth.dependencies import (
    get_current_session,
    get_current_user,
    get_db,
    require_user,
)
from ..auth.permissions import ROLE_TEMPLATES
from ..auth.security import CSRF_COOKIE_NAME, issue_csrf_token
from ..middleware import _client_ip
from ..templating import templates


router = APIRouter()


def _slugify(name: str) -> str:
    """Convert a free-form org name to a URL-safe slug.

    Conservative: only lowercase a-z, 0-9, and hyphens. Empty result
    falls back to "org".
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "org"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _seed_role_templates(db: Session, org_id: int) -> None:
    """Seed built-in role templates and permissions for a new org."""
    for tmpl in ROLE_TEMPLATES:
        existing = db.execute(
            select(M.Role).where(
                M.Role.org_id == org_id,
                M.Role.template_slug == tmpl.slug,
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        role = M.Role(
            org_id=org_id,
            template_slug=tmpl.slug,
            name=tmpl.name,
            description=tmpl.description,
            builtin=True,
            workcenter_scopable=tmpl.workcenter_scopable,
        )
        db.add(role)
        db.flush()
        for permission_code in tmpl.permissions:
            db.add(M.RolePermission(role_id=role.id, permission_code=permission_code))


@router.get("/no-orgs")
def no_orgs(
    request: Request,
    user: M.UserAccount = Depends(require_user),
    db: Session = Depends(get_db),
):
    pending = db.execute(
        select(M.OrgMembership, M.Organization)
        .join(M.Organization, M.Organization.id == M.OrgMembership.org_id)
        .where(
            M.OrgMembership.user_id == user.id,
            M.OrgMembership.status == "pending",
        )
    ).all()
    return templates.TemplateResponse(
        request,
        "auth/no_orgs.html",
        {
            "user": user,
            "pending_invites": [
                {"membership_id": m.id, "org_name": o.name, "org_slug": o.slug}
                for m, o in pending
            ],
        },
    )


@router.get("/orgs/select")
def org_picker(
    request: Request,
    user: M.UserAccount = Depends(require_user),
    db: Session = Depends(get_db),
):
    memberships = db.execute(
        select(M.OrgMembership, M.Organization)
        .join(M.Organization, M.Organization.id == M.OrgMembership.org_id)
        .where(
            M.OrgMembership.user_id == user.id,
            M.OrgMembership.status == "active",
        )
    ).all()
    return templates.TemplateResponse(
        request,
        "auth/org_picker.html",
        {
            "user": user,
            "memberships": [
                {"membership_id": m.id, "org_name": o.name, "org_slug": o.slug}
                for m, o in memberships
            ],
        },
    )


@router.post("/orgs/create")
def org_create(
    request: Request,
    name: str = Form(...),
    user: M.UserAccount = Depends(require_user),
    session: M.UserSession = Depends(get_current_session),
    db: Session = Depends(get_db),
):
    """Any logged-in user can create an org and become its founding member.

    Phase 2: the founding member is granted the ``org_owner`` role so they
    can administer the org immediately.
    """
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no session")

    cleaned = name.strip()
    if not cleaned or len(cleaned) > 128:
        raise HTTPException(400, "name must be 1-128 characters")

    # Resolve a unique slug. Append a numeric suffix on collision rather
    # than failing — orgs share a global slug namespace and there's no
    # legitimate reason to refuse a duplicate name.
    base_slug = _slugify(cleaned)
    slug = base_slug
    suffix = 2
    while (
        db.execute(
            select(M.Organization).where(M.Organization.slug == slug)
        ).scalar_one_or_none()
        is not None
    ):
        slug = f"{base_slug}-{suffix}"
        suffix += 1

    org = M.Organization(slug=slug, name=cleaned)
    db.add(org)
    db.flush()
    _seed_role_templates(db, org.id)

    membership = M.OrgMembership(
        org_id=org.id,
        user_id=user.id,
        status="active",
    )
    db.add(membership)
    db.flush()

    # Phase 2: grant the founding member the Org Owner role so they can
    # administer the org immediately (manage members, roles, workcenters).
    owner_role = db.execute(
        select(M.Role).where(
            M.Role.org_id == org.id,
            M.Role.template_slug == "org_owner",
        )
    ).scalar_one_or_none()
    if owner_role is not None:
        db.add(
            M.MembershipRole(
                membership_id=membership.id,
                role_id=owner_role.id,
                workcenter_id=None,
            )
        )

    rotated = sess_mod.rotate(
        db,
        session,
        membership_id=membership.id,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        reason="org_created",
    )
    db.add(
        M.AuthEvent(
            kind="org_created",
            user_id=user.id,
            session_id=rotated.id,
            provider=None,
            detail={"org_id": org.id, "org_slug": org.slug},
            ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    )

    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        sess_mod.SESSION_COOKIE_NAME,
        rotated.id,
        max_age=int(sess_mod.ABSOLUTE_TIMEOUT.total_seconds()),
        httponly=True,
        secure=sess_mod.SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        issue_csrf_token(rotated.id),
        httponly=False,
        secure=sess_mod.SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/invites/accept")
def invite_accept_magic_link(
    request: Request,
    token: str = "",
    user: Optional[M.UserAccount] = Depends(get_current_user),
    session: Optional[M.UserSession] = Depends(get_current_session),
    db: Session = Depends(get_db),
):
    """Magic-link invite acceptance.

    The invitee clicks a link like ``/invites/accept?token=...``. If not
    logged in they are redirected to ``/login?next=/invites/accept?token=...``.

    The raw token is never logged, never stored in audit events, and never
    appears in the URL after acceptance (redirect to clean ``/``).
    """
    if user is None or session is None:
        # Preserve the full invite URL for post-login redirect.
        next_path = f"/invites/accept?token={token}" if token else "/invites/accept"
        return RedirectResponse(f"/login?next={next_path}", status_code=302)
    return _accept_invite(db, request, user, session, token)


@router.post("/invites/accept")
def invite_accept_post(
    request: Request,
    token: str = Form(...),
    user: M.UserAccount = Depends(require_user),
    session: M.UserSession = Depends(get_current_session),
    db: Session = Depends(get_db),
):
    """Legacy form-based invite acceptance. Kept for backwards compat."""
    return _accept_invite(db, request, user, session, token)


def _accept_invite(
    db: Session,
    request: Request,
    user: M.UserAccount,
    session: M.UserSession,
    token: str,
) -> Response:
    """Shared invite acceptance logic.

    On success: creates membership, auto-creates Person record from invite
    data, rotates session, redirects to clean ``/``.
    """
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no session")

    token_clean = (token or "").strip()
    if not token_clean:
        return _invite_failure(db, request, user, session, reason="empty_token")

    digest = invites_mod.hash_invite_token(token_clean)
    invite = db.execute(
        select(M.OrgInvite).where(M.OrgInvite.token_hash == digest)
    ).scalar_one_or_none()

    if invite is None:
        return _invite_failure(db, request, user, session, reason="not_found")
    if invite.revoked_at is not None:
        return _invite_failure(
            db, request, user, session, reason="revoked", invite_id=invite.id
        )
    if invite.accepted_at is not None:
        return _invite_failure(
            db, request, user, session, reason="already_used", invite_id=invite.id
        )
    if invite.expires_at < _now():
        return _invite_failure(
            db, request, user, session, reason="expired", invite_id=invite.id
        )
    if invite.intended_email.lower() != user.email.lower():
        return _invite_failure(
            db,
            request,
            user,
            session,
            reason="email_mismatch",
            invite_id=invite.id,
        )

    existing = db.execute(
        select(M.OrgMembership).where(
            M.OrgMembership.org_id == invite.org_id,
            M.OrgMembership.user_id == user.id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _invite_failure(
            db,
            request,
            user,
            session,
            reason="already_member",
            invite_id=invite.id,
        )

    membership = M.OrgMembership(
        org_id=invite.org_id,
        user_id=user.id,
        status="active",
    )
    db.add(membership)

    # Auto-create Person record from invite data so the invitee only
    # needs to fill in planned departure, arrival, etc.
    if invite.first_name or invite.last_name:
        fn = (invite.first_name or "").strip()
        ln = (invite.last_name or "").strip()
        full = f"{fn} {ln}".strip()
        person = M.Person(
            first_name=fn or None,
            last_name=ln,
            full_display=full,
            org_id=invite.org_id,
        )
        db.add(person)
        db.flush()

        # If a title was provided, create the initial PersonRate row.
        if invite.rate:
            db.add(
                M.PersonRate(
                    person_id=person.id,
                    rate=invite.rate,
                    paygrade=invite.paygrade,
                    valid_from=datetime.now().date(),
                    org_id=invite.org_id,
                )
            )
        # Initial roster status.
        db.add(
            M.PersonRosterStatus(
                person_id=person.id,
                status="active",
                valid_from=datetime.now().date(),
                org_id=invite.org_id,
            )
        )

    invite.accepted_at = _now()
    invite.accepted_by_user_id = user.id
    db.flush()

    rotated = sess_mod.rotate(
        db,
        session,
        membership_id=membership.id,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        reason="invite_accepted",
    )
    db.add(
        M.AuthEvent(
            kind="invite_accepted",
            user_id=user.id,
            session_id=rotated.id,
            provider=None,
            detail={
                "invite_id": invite.id,
                "org_id": invite.org_id,
                "membership_id": membership.id,
            },
            ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    )

    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        sess_mod.SESSION_COOKIE_NAME,
        rotated.id,
        max_age=int(sess_mod.ABSOLUTE_TIMEOUT.total_seconds()),
        httponly=True,
        secure=sess_mod.SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        issue_csrf_token(rotated.id),
        httponly=False,
        secure=sess_mod.SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    return response


def _invite_failure(
    db: Session,
    request: Request,
    user: M.UserAccount,
    session: M.UserSession,
    *,
    reason: str,
    invite_id: int = None,
) -> Response:
    """Audit-log the failure and return a generic error.

    The error message intentionally does not distinguish between "token
    doesn't exist" and "token exists but you can't use it" — that would
    let an attacker probe for valid tokens. The reason is recorded in
    the audit log for the admin who issued the invite, not in the
    response.
    """
    db.add(
        M.AuthEvent(
            kind="invite_rejected",
            user_id=user.id,
            session_id=session.id,
            provider=None,
            detail={"reason": reason, "invite_id": invite_id},
            ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    )
    return Response(
        "invite link is invalid, expired, or not for you",
        status_code=400,
        media_type="text/plain",
    )


@router.post("/orgs/select")
def org_select(
    request: Request,
    membership_id: int = Form(...),
    user: M.UserAccount = Depends(require_user),
    session: M.UserSession = Depends(get_current_session),
    db: Session = Depends(get_db),
):
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no session")

    target = db.get(M.OrgMembership, membership_id)
    if target is None or target.user_id != user.id or target.status != "active":
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "membership not found or inactive"
        )

    # Rotate the session: an org change is a privilege boundary change.
    rotated = sess_mod.rotate(
        db,
        session,
        membership_id=target.id,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        reason="org_switch",
    )
    db.add(
        M.AuthEvent(
            kind="org_switch",
            user_id=user.id,
            session_id=rotated.id,
            provider=None,
            detail={"membership_id": target.id, "org_id": target.org_id},
            ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    )

    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        sess_mod.SESSION_COOKIE_NAME,
        rotated.id,
        max_age=int(sess_mod.ABSOLUTE_TIMEOUT.total_seconds()),
        httponly=True,
        secure=sess_mod.SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        issue_csrf_token(rotated.id),
        httponly=False,
        secure=sess_mod.SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/orgs/leave")
def org_leave(
    request: Request,
    user: M.UserAccount = Depends(require_user),
    session: M.UserSession = Depends(get_current_session),
    db: Session = Depends(get_db),
):
    """Leave the currently bound organization.

    Server-side checks:
    * Cannot leave if this is the last org_owner for the org (org would be
      orphaned).
    * Session is rotated; if the user has other memberships the session
      binds to the next one, otherwise it becomes unbound.
    """
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no session")

    membership = db.get(M.OrgMembership, session.current_membership_id)
    if membership is None or membership.user_id != user.id:
        raise HTTPException(404, "no active membership to leave")

    org_id = membership.org_id

    # Check: cannot leave if this is the last org_owner.
    from ..auth.authorization import membership_has_role_template

    is_owner = membership_has_role_template(db, membership.id, "org_owner")
    if is_owner:
        other_owners = (
            db.execute(
                select(M.OrgMembership).where(
                    M.OrgMembership.org_id == org_id,
                    M.OrgMembership.id != membership.id,
                    M.OrgMembership.status == "active",
                )
            )
            .scalars()
            .all()
        )
        has_other_owner = any(
            membership_has_role_template(db, m.id, "org_owner") for m in other_owners
        )
        if not has_other_owner:
            raise HTTPException(
                400,
                "Cannot leave: you are the last org owner. "
                "Transfer ownership to another member first.",
            )

    # Soft-delete the membership (set status to suspended).
    membership.status = "suspended"
    db.flush()

    # Determine redirect: if user has other active memberships, bind to
    # the first one; otherwise leave session unbound.
    other_memberships = (
        db.execute(
            select(M.OrgMembership).where(
                M.OrgMembership.user_id == user.id,
                M.OrgMembership.status == "active",
            )
        )
        .scalars()
        .all()
    )

    next_membership_id = other_memberships[0].id if other_memberships else None

    rotated = sess_mod.rotate(
        db,
        session,
        membership_id=next_membership_id,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        reason="org_left",
    )
    db.add(
        M.AuthEvent(
            kind="org_left",
            user_id=user.id,
            session_id=rotated.id,
            provider=None,
            detail={"org_id": org_id, "membership_id": membership.id},
            ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    )

    redirect_to = "/orgs/select" if other_memberships else "/no-orgs"
    response = RedirectResponse(redirect_to, status_code=302)
    response.set_cookie(
        sess_mod.SESSION_COOKIE_NAME,
        rotated.id,
        max_age=int(sess_mod.ABSOLUTE_TIMEOUT.total_seconds()),
        httponly=True,
        secure=sess_mod.SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        issue_csrf_token(rotated.id),
        httponly=False,
        secure=sess_mod.SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/orgs/delete")
def org_delete(
    request: Request,
    user: M.UserAccount = Depends(require_user),
    session: M.UserSession = Depends(get_current_session),
    db: Session = Depends(get_db),
):
    """Delete the entire organization.

    Only available when the user is the sole active member and an org_owner.
    Hard-deletes all tenant-scoped data for the org, then the org itself.
    """
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no session")

    membership = db.get(M.OrgMembership, session.current_membership_id)
    if membership is None or membership.user_id != user.id:
        raise HTTPException(404, "no active membership")

    org_id = membership.org_id

    from ..auth.authorization import membership_has_role_template

    if not membership_has_role_template(db, membership.id, "org_owner"):
        raise HTTPException(403, "Only org owners can delete the organization.")

    # Check: must be the only active member.
    active_members = (
        db.execute(
            select(M.OrgMembership).where(
                M.OrgMembership.org_id == org_id,
                M.OrgMembership.status == "active",
            )
        )
        .scalars()
        .all()
    )
    if len(active_members) > 1:
        raise HTTPException(
            400,
            "Cannot delete: there are other active members. Remove them first.",
        )

    org = db.get(M.Organization, org_id)
    org_slug = org.slug if org else None

    # Rotate the current session before deleting the active membership it
    # points at. The old session will be removed below with the rest of the
    # org-bound sessions; the replacement session is unbound.
    rotated = sess_mod.rotate(
        db,
        session,
        membership_id=None,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        reason="org_deleted",
    )

    db.add(
        M.AuthEvent(
            kind="org_deleted",
            user_id=user.id,
            session_id=rotated.id,
            provider=None,
            detail={"org_id": org_id, "org_slug": org_slug},
            ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    )

    # Hard-delete all tenant-scoped rows for this org. Order matters due to
    # FK constraints; tables without org_id are handled with subqueries.
    from sqlalchemy import text as sa_text

    db.execute(
        sa_text(
            "DELETE FROM user_sessions WHERE current_membership_id IN "
            "(SELECT id FROM org_memberships WHERE org_id = :oid)"
        ),
        {"oid": org_id},
    )
    db.execute(
        sa_text(
            "DELETE FROM membership_roles WHERE membership_id IN "
            "(SELECT id FROM org_memberships WHERE org_id = :oid)"
        ),
        {"oid": org_id},
    )
    db.execute(
        sa_text(
            "DELETE FROM role_permissions WHERE role_id IN "
            "(SELECT id FROM roles WHERE org_id = :oid)"
        ),
        {"oid": org_id},
    )

    for table in (
        "task_assignments",
        "task_instances",
        "task_templates",
        "task_categories",
        "crew_memberships",
        "crews",
        "person_quals",
        "person_drivers_licenses",
        "person_prds",
        "person_roster_status",
        "person_duty_sections",
        "person_rates",
        "absences",
        "persons",
        "qualifications",
        "absence_codes",
        "alerts",
        "worklists",
        "import_batches",
        "data_audit_events",
        "workcenters",
        "roles",
        "org_invites",
        "org_memberships",
    ):
        db.execute(sa_text(f"DELETE FROM {table} WHERE org_id = :oid"), {"oid": org_id})

    db.flush()

    # Delete the org itself.
    if org:
        db.delete(org)

    response = RedirectResponse("/no-orgs", status_code=302)
    response.set_cookie(
        sess_mod.SESSION_COOKIE_NAME,
        rotated.id,
        max_age=int(sess_mod.ABSOLUTE_TIMEOUT.total_seconds()),
        httponly=True,
        secure=sess_mod.SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        issue_csrf_token(rotated.id),
        httponly=False,
        secure=sess_mod.SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    return response
