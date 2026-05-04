"""Dev-only login shortcut for local smoke testing.

**Disabled by default.** Routes here only register when
``TBDTASK_DEV_LOGIN=1`` is set. The intent is to give a developer on
their laptop a one-click "log me in" button so they can exercise
flows that need an authenticated session — passkey enrollment,
step-up gates, the dashboard, the admin panel — without configuring
OAuth credentials for Google/Apple/Microsoft.

This is **not** a production capability. The route refuses to register
unless ``TBDTASK_INSECURE_LOCAL_COOKIES=1`` *also* — which the secure
cookie-prefix logic in ``app.auth.sessions`` only honours on
localhost. Two env-var locks against accidentally exposing it.

What it does:

1. Ensures an organization called "Dev Org" exists.
2. Ensures a UserAccount with email
   ``TBDTASK_DEV_LOGIN_EMAIL`` (default ``dev@example.com``) exists.
3. Ensures the user has an active membership in the org.
4. Ensures the membership has the built-in ``Org Owner`` role with
   every permission, so they can poke at every page.
5. Creates a fresh session and sets the session cookie.
6. Redirects to ``/`` (or ``?next=...``).

Tearing it down: unset ``TBDTASK_DEV_LOGIN`` and restart. The dev
data stays in the DB but the route is gone.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from .. import models as M
from ..auth import sessions as sess_mod
from ..auth.permissions import PERMISSION_CODES
from ..auth.security import CSRF_COOKIE_NAME, issue_csrf_token
from ..db import SessionLocal


router = APIRouter()


def _enabled() -> bool:
    return (
        os.environ.get("TBDTASK_DEV_LOGIN") == "1"
        and os.environ.get("TBDTASK_INSECURE_LOCAL_COOKIES") == "1"
    )


def _dev_email() -> str:
    return os.environ.get("TBDTASK_DEV_LOGIN_EMAIL", "dev@example.com")


@router.get("/dev-login")
def dev_login(request: Request, next: Optional[str] = None) -> Response:
    if not _enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    db = SessionLocal()
    try:
        org = db.execute(
            select(M.Organization).where(M.Organization.slug == "dev")
        ).scalar_one_or_none()
        if org is None:
            org = M.Organization(slug="dev", name="Dev Org")
            db.add(org)
            db.flush()

        user = db.execute(
            select(M.UserAccount).where(M.UserAccount.email == _dev_email())
        ).scalar_one_or_none()
        if user is None:
            user = M.UserAccount(email=_dev_email(), display_name="Dev User")
            db.add(user)
            db.flush()

        mem = db.execute(
            select(M.OrgMembership).where(
                M.OrgMembership.org_id == org.id,
                M.OrgMembership.user_id == user.id,
            )
        ).scalar_one_or_none()
        if mem is None:
            mem = M.OrgMembership(org_id=org.id, user_id=user.id, status="active")
            db.add(mem)
            db.flush()
        elif mem.status != "active":
            mem.status = "active"

        # Org Owner role with every permission, so the dev user can
        # exercise every page including admin.
        role = db.execute(
            select(M.Role).where(
                M.Role.org_id == org.id,
                M.Role.template_slug == "org_owner",
            )
        ).scalar_one_or_none()
        if role is None:
            role = M.Role(
                org_id=org.id,
                name="Org Owner",
                template_slug="org_owner",
                description="Dev: full access",
                builtin=True,
                workcenter_scopable=False,
            )
            db.add(role)
            db.flush()
            for code in PERMISSION_CODES:
                db.add(M.RolePermission(role_id=role.id, permission_code=code))
            db.flush()

        grant = db.execute(
            select(M.MembershipRole).where(
                M.MembershipRole.membership_id == mem.id,
                M.MembershipRole.role_id == role.id,
                M.MembershipRole.workcenter_id.is_(None),
            )
        ).scalar_one_or_none()
        if grant is None:
            db.add(
                M.MembershipRole(
                    membership_id=mem.id,
                    role_id=role.id,
                    workcenter_id=None,
                )
            )

        # Phase 8b.1: bootstrap the org's KEK_org_master if WebAuthn is
        # enabled. Idempotent — no-op on repeat dev-logins.
        from ..auth import key_hierarchy as kh
        from ..auth import key_vault as kv
        from ..auth import webauthn as wa

        if wa.is_enabled():
            try:
                vault = kv.get_vault()
                kh.bootstrap_org_keys(
                    db,
                    org_id=org.id,
                    vault=vault,
                    actor_user_id=user.id,
                )
            except Exception:
                # Don't block dev login if vault setup fails; surface
                # the error in a banner via the existing audit pipeline.
                pass

        sess = sess_mod.create_session(
            db,
            user_id=user.id,
            membership_id=mem.id,
            ip=(request.client.host if request.client else None),
            user_agent=request.headers.get("user-agent"),
        )
        db.commit()

        # Safe-redirect: only relative paths, never an external URL.
        target = next or "/"
        if not target.startswith("/") or target.startswith("//"):
            target = "/"
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            sess_mod.SESSION_COOKIE_NAME,
            sess.id,
            max_age=int(sess_mod.ABSOLUTE_TIMEOUT.total_seconds()),
            httponly=True,
            secure=sess_mod.SESSION_COOKIE_SECURE,
            samesite="lax",
            path="/",
        )
        response.set_cookie(
            CSRF_COOKIE_NAME,
            issue_csrf_token(sess.id),
            max_age=int(sess_mod.ABSOLUTE_TIMEOUT.total_seconds()),
            httponly=False,
            secure=sess_mod.SESSION_COOKIE_SECURE,
            samesite="lax",
            path="/",
        )
        return response
    finally:
        db.close()
