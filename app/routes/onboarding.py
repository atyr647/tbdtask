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

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as M
from ..auth import invites as invites_mod
from ..auth import sessions as sess_mod
from ..auth.dependencies import get_current_session, get_db, require_user
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


@router.get("/no-orgs")
def no_orgs(
    request: Request,
    user: M.UserAccount = Depends(require_user),
    db: Session = Depends(get_db),
):
    pending = (
        db.execute(
            select(M.OrgMembership, M.Organization)
            .join(M.Organization, M.Organization.id == M.OrgMembership.org_id)
            .where(
                M.OrgMembership.user_id == user.id,
                M.OrgMembership.status == "pending",
            )
        )
        .all()
    )
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
    memberships = (
        db.execute(
            select(M.OrgMembership, M.Organization)
            .join(M.Organization, M.Organization.id == M.OrgMembership.org_id)
            .where(
                M.OrgMembership.user_id == user.id,
                M.OrgMembership.status == "active",
            )
        )
        .all()
    )
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

    Phase 2 attaches an "Org Owner" role template; for now the membership
    just exists with status=active.
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
    membership = M.OrgMembership(
        org_id=org.id,
        user_id=user.id,
        status="active",
    )
    db.add(membership)
    db.flush()

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
        secure=True,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        issue_csrf_token(rotated.id),
        httponly=False,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/invites/accept")
def invite_accept(
    request: Request,
    token: str = Form(...),
    user: M.UserAccount = Depends(require_user),
    session: M.UserSession = Depends(get_current_session),
    db: Session = Depends(get_db),
):
    """Redeem an invite token.

    Security profile (see docs/security/threat-model.md):

    * Tokens are stored hashed; the raw token reaches us only via the
      form field and is hashed before any DB lookup.
    * One-shot: ``accepted_at`` set on success; subsequent submits 404.
    * Email-bound invites refuse to redeem if the authenticated user's
      canonical email differs.
    * All outcomes audited; failure responses are intentionally generic
      to avoid leaking whether a token *exists* but is unredeemable.
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
    # intended_email is mandatory on every invite, so this is always a hard
    # check. A user whose canonical email differs cannot redeem, even if
    # the raw token has somehow reached them.
    if invite.intended_email.lower() != user.email.lower():
        return _invite_failure(
            db,
            request,
            user,
            session,
            reason="email_mismatch",
            invite_id=invite.id,
        )

    # Make sure we don't double-add a membership; if one already exists
    # under any status, surface that and don't quietly upgrade.
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
        secure=True,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        issue_csrf_token(rotated.id),
        httponly=False,
        secure=True,
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
    if (
        target is None
        or target.user_id != user.id
        or target.status != "active"
    ):
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
        secure=True,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        issue_csrf_token(rotated.id),
        httponly=False,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response
