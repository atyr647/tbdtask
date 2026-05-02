"""OIDC sign-in, callback, logout, and identity-linking routes.

The flow:

* ``GET /login`` — renders the provider buttons. Public.
* ``GET /auth/{provider}/login`` — starts the OIDC dance. Stores a signed
  ``state`` payload (provider, return_to, intent=login|link). Redirects to
  the IdP. Rate-limited per IP.
* ``GET /auth/{provider}/callback`` — completes the dance. Validates state
  + nonce + token, normalizes userinfo, runs ``accounts.resolve_identity``,
  creates or rotates a session, and redirects to org-picker / dashboard.
* ``POST /auth/logout`` — revokes the session, clears the cookie. CSRF
  protected.
* ``GET /auth/{provider}/link`` — like ``/auth/{provider}/login`` but
  ``intent=link`` and requires an active session. The callback path is
  shared.

OIDC state validation is independent of CSRF — both are required defences
in depth. The state proves "this callback belongs to this user's redirect"
(prevents login CSRF / mix-up); CSRF tokens guard non-GET app routes.
"""
from __future__ import annotations

from typing import Optional

from authlib.integrations.starlette_client import OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as M
from ..auth import accounts as acct_mod
from ..auth import sessions as sess_mod
from ..auth.dependencies import get_current_user, get_db, require_user
from ..auth.providers import (
    configured_providers,
    get_client,
    is_configured,
    normalize_userinfo,
)
from ..auth.security import (
    CSRF_COOKIE_NAME,
    issue_csrf_token,
    issue_oidc_state,
    validate_oidc_state,
)
from ..middleware import SINGLE_TENANT_MODE, _client_ip
from ..templating import templates


router = APIRouter()


# ---------------------------------------------------------------------------
# Login page
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: Optional[str] = None) -> Response:
    if SINGLE_TENANT_MODE:
        # Single-tenant has no concept of login.
        return RedirectResponse("/", status_code=302)

    response = templates.TemplateResponse(
        request,
        "auth/login.html",
        {
            "providers": list(configured_providers().values()),
            "next": next or "/",
        },
    )
    # Issue an anonymous CSRF token so the (currently empty) login page
    # has one ready if it grows a form. Bound to the literal "anon"
    # string — gets rotated to a real session-bound token after sign-in.
    if request.cookies.get(CSRF_COOKIE_NAME) is None:
        token = issue_csrf_token(None)
        response.set_cookie(
            CSRF_COOKIE_NAME,
            token,
            httponly=False,  # form/JS read it, double-submit pattern
            secure=True,
            samesite="lax",
            path="/",
        )
    return response


# ---------------------------------------------------------------------------
# Login start
# ---------------------------------------------------------------------------

@router.get("/auth/{provider}/login")
async def login_start(
    provider: str,
    request: Request,
    next: Optional[str] = None,
):
    if not is_configured(provider):
        raise HTTPException(404, f"provider {provider!r} not configured")

    state = issue_oidc_state(
        {"provider": provider, "intent": "login", "next": next or "/"}
    )
    redirect_uri = str(request.url_for("oidc_callback", provider=provider))
    client = get_client(provider)
    return await client.authorize_redirect(request, redirect_uri, state=state)


# ---------------------------------------------------------------------------
# Link start (existing user attaching another provider)
# ---------------------------------------------------------------------------

@router.get("/auth/{provider}/link")
async def link_start(
    provider: str,
    request: Request,
    user: M.UserAccount = Depends(require_user),
):
    if not is_configured(provider):
        raise HTTPException(404, f"provider {provider!r} not configured")

    state = issue_oidc_state(
        {"provider": provider, "intent": "link", "user_id": user.id, "next": "/"}
    )
    redirect_uri = str(request.url_for("oidc_callback", provider=provider))
    client = get_client(provider)
    return await client.authorize_redirect(request, redirect_uri, state=state)


# ---------------------------------------------------------------------------
# Callback (shared between login + link)
# ---------------------------------------------------------------------------

@router.get("/auth/{provider}/callback", name="oidc_callback")
async def oidc_callback(
    provider: str,
    request: Request,
    db: Session = Depends(get_db),
):
    if not is_configured(provider):
        raise HTTPException(404, f"provider {provider!r} not configured")

    submitted_state = request.query_params.get("state")
    state = validate_oidc_state(submitted_state) if submitted_state else None
    if not state or state.get("provider") != provider:
        # Either signature failed, the token expired, or someone is
        # replaying a callback with a state for a different provider.
        _record_auth_event(
            db,
            kind="callback_state_invalid",
            user_id=None,
            session_id=None,
            provider=provider,
            request=request,
            detail={"reason": "invalid_or_mismatched_state"},
        )
        return _error("invalid or expired state", 400)

    client = get_client(provider)
    try:
        token = await client.authorize_access_token(request)
    except OAuthError as exc:
        _record_auth_event(
            db,
            kind="callback_oauth_error",
            user_id=None,
            session_id=None,
            provider=provider,
            request=request,
            detail={"error": str(exc)},
        )
        return _error("OIDC token exchange failed", 400)

    # Authlib parses the id_token and runs nonce verification when the
    # client was configured with ``nonce`` in the authorize_redirect call.
    # Userinfo claims live on ``token['userinfo']`` for OIDC clients.
    claims = token.get("userinfo") or {}
    if not claims:
        return _error("OIDC userinfo missing", 400)

    try:
        ident = normalize_userinfo(provider, claims)
    except ValueError as exc:
        return _error(f"invalid IdP claims: {exc}", 400)

    intent = state.get("intent", "login")
    if intent == "link":
        return _handle_link_callback(db, request, state, ident)
    return _handle_login_callback(db, request, state, ident)


def _handle_login_callback(
    db: Session, request: Request, state: dict, ident
) -> Response:
    outcome = acct_mod.resolve_identity(db, ident)

    if isinstance(outcome, acct_mod.LinkRequired):
        # Don't auto-merge. Send them through the explicit link flow,
        # which requires re-authenticating with the existing provider.
        _record_auth_event(
            db,
            kind="login_link_required",
            user_id=outcome.existing_user_id,
            session_id=None,
            provider=ident.provider,
            request=request,
            detail={"candidate_email_present": True},
        )
        return _error(
            "An account already exists with this verified email under a "
            "different provider. Sign in with that provider first, then "
            "link this one from your settings.",
            409,
        )

    user_id = outcome.user_id
    next_path = state.get("next") or "/"

    # Pick a membership to bind the session to.
    memberships = (
        db.execute(
            select(M.OrgMembership).where(
                M.OrgMembership.user_id == user_id,
                M.OrgMembership.status == "active",
            )
        )
        .scalars()
        .all()
    )
    membership_id = memberships[0].id if len(memberships) == 1 else None

    new_session = sess_mod.create_session(
        db,
        user_id=user_id,
        membership_id=membership_id,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    _record_auth_event(
        db,
        kind="login_success",
        user_id=user_id,
        session_id=new_session.id,
        provider=ident.provider,
        request=request,
        detail={
            "new_account": isinstance(outcome, acct_mod.CreatedAccount),
            "memberships": len(memberships),
        },
    )

    if not memberships:
        redirect = "/no-orgs"
    elif len(memberships) > 1:
        redirect = "/orgs/select"
    else:
        redirect = next_path

    response = RedirectResponse(redirect, status_code=302)
    _set_session_cookie(response, new_session.id)
    _rotate_csrf_cookie(response, new_session.id)
    return response


def _handle_link_callback(
    db: Session, request: Request, state: dict, ident
) -> Response:
    user_id = state.get("user_id")
    if not isinstance(user_id, int):
        return _error("link state missing user_id", 400)

    # The user must still be the same one who started the link flow. The
    # session middleware already verified they're logged in to make this
    # callback, but defence-in-depth: re-check.
    cookie_id = request.cookies.get(sess_mod.SESSION_COOKIE_NAME)
    sess = sess_mod.lookup_session(db, cookie_id)
    if sess is None or sess.user_id != user_id:
        return _error("link flow lost its session; start over", 401)

    try:
        acct_mod.link_identity_to_user(db, user_id=user_id, ident=ident)
    except ValueError as exc:
        _record_auth_event(
            db,
            kind="link_rejected",
            user_id=user_id,
            session_id=cookie_id,
            provider=ident.provider,
            request=request,
            detail={"reason": str(exc)},
        )
        return _error(str(exc), 409)

    _record_auth_event(
        db,
        kind="link_success",
        user_id=user_id,
        session_id=cookie_id,
        provider=ident.provider,
        request=request,
        detail=None,
    )

    # Rotate the session as a defensive measure on any privilege boundary
    # change, including new-identity attached.
    rotated = sess_mod.rotate(
        db,
        sess,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        reason="identity_linked",
    )
    response = RedirectResponse("/", status_code=302)
    _set_session_cookie(response, rotated.id)
    _rotate_csrf_cookie(response, rotated.id)
    return response


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

@router.post("/auth/logout")
def logout(
    request: Request,
    db: Session = Depends(get_db),
    user: Optional[M.UserAccount] = Depends(get_current_user),
):
    cookie_id = request.cookies.get(sess_mod.SESSION_COOKIE_NAME)
    if cookie_id:
        sess = db.get(M.UserSession, cookie_id)
        if sess is not None and sess.revoked_at is None:
            sess_mod.revoke(db, sess, reason="logout")
            _record_auth_event(
                db,
                kind="logout",
                user_id=user.id if user else None,
                session_id=cookie_id,
                provider=None,
                request=request,
                detail=None,
            )

    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(
        sess_mod.SESSION_COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax"
    )
    response.delete_cookie(CSRF_COOKIE_NAME, path="/", secure=True, samesite="lax")
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        sess_mod.SESSION_COOKIE_NAME,
        session_id,
        max_age=int(sess_mod.ABSOLUTE_TIMEOUT.total_seconds()),
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def _rotate_csrf_cookie(response: Response, session_id: str) -> None:
    token = issue_csrf_token(session_id)
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        httponly=False,
        secure=True,
        samesite="lax",
        path="/",
    )


def _error(message: str, code: int) -> Response:
    return Response(message, status_code=code, media_type="text/plain")


def _record_auth_event(
    db: Session,
    *,
    kind: str,
    user_id: Optional[int],
    session_id: Optional[str],
    provider: Optional[str],
    request: Request,
    detail: Optional[dict],
) -> None:
    db.add(
        M.AuthEvent(
            kind=kind,
            user_id=user_id,
            session_id=session_id,
            provider=provider,
            detail=detail,
            ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    )
    db.flush()
