"""Request-pipeline middleware.

Three layered concerns, kept in separate functions so each can be tested
in isolation:

1. **SecureHeadersMiddleware** — applies CSP, HSTS, X-Frame-Options, etc.
   to every response. Always runs.
2. **SessionMiddleware** — looks up the cookie-borne session id, validates
   it, attaches ``request.state.session/user/membership``, and enters
   ``tenant_context`` for the active org. Skipped on a small allowlist of
   public routes (login, callback, static, healthz).
3. **CSRFMiddleware** — for unsafe methods, validates the form/header
   token against the session-bound cookie. OIDC callbacks are exempt
   because they have their own ``state`` parameter validation.

In ``SINGLE_TENANT`` mode, SessionMiddleware bypasses authentication
entirely and binds every request to the default org. This keeps the
AppImage offline path identical to its current behaviour.
"""
from __future__ import annotations

import os
from contextvars import ContextVar
from typing import Optional

from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from . import models as M
from .auth import sessions as sess_mod
from .auth.security import (
    AUTH_RATE_LIMITER,
    CSRF_COOKIE_NAME,
    CSRF_FORM_FIELD,
    CSRF_HEADER_NAME,
    secure_response_headers,
    validate_csrf_token,
)
from .db import SessionLocal
from .tenancy import tenant_context


SINGLE_TENANT_MODE = os.environ.get("TBDTASK_SINGLE_TENANT", "0") == "1"

# Routes that bypass session enforcement. Keep this list short: every
# entry is a place where the auth invariant doesn't hold by design.
_PUBLIC_PATH_PREFIXES = (
    "/static/",
    "/auth/",
    "/healthz",
    "/login",
    "/no-orgs",
    "/favicon",
)

# Routes exempt from CSRF validation. OIDC callback uses ``state`` instead;
# /healthz is GET-only but listed for completeness.
_CSRF_EXEMPT_PATHS = (
    "/auth/google/callback",
    "/auth/microsoft/callback",
    "/auth/apple/callback",
    "/healthz",
)

_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
_AUTH_RATE_LIMITED_PATHS = (
    "/auth/google/login",
    "/auth/google/callback",
    "/auth/microsoft/login",
    "/auth/microsoft/callback",
    "/auth/apple/login",
    "/auth/apple/callback",
)


def _is_public(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in _PUBLIC_PATH_PREFIXES)


def _client_ip(request: Request) -> str:
    """Best-effort client IP. Phase 7 hardens this with a trusted-proxy list."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Secure headers (always on)
# ---------------------------------------------------------------------------

class SecureHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        https = (
            request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto") == "https"
        )
        for k, v in secure_response_headers(https=https).items():
            response.headers.setdefault(k, v)
        return response


# ---------------------------------------------------------------------------
# Auth rate limiting (per-IP, narrow scope to auth endpoints)
# ---------------------------------------------------------------------------

class AuthRateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if any(request.url.path.startswith(p) for p in _AUTH_RATE_LIMITED_PATHS):
            ip = _client_ip(request)
            if not AUTH_RATE_LIMITER.check("auth", ip):
                return Response(
                    "too many auth attempts; try again later",
                    status_code=429,
                    headers={"Retry-After": "60"},
                )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Session + tenancy
# ---------------------------------------------------------------------------

# Holds the org id resolved for the current request so the listener in
# ``app.tenancy`` picks it up. Exposed via the ``tenant_context`` manager
# entered in dispatch().
_request_org_id: ContextVar[Optional[int]] = ContextVar(
    "tbdtask_request_org_id", default=None
)


class SessionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if _is_public(request.url.path):
            return await call_next(request)

        if SINGLE_TENANT_MODE:
            # Offline AppImage path. There's literally one tenant, so no
            # auth boundary, no scoping to enforce — just pass through.
            # The tenancy listener stays a no-op because no
            # ``tenant_context`` is entered; queries run unfiltered across
            # the single org's data, which is what the AppImage UX expects.
            return await call_next(request)

        # Hosted mode — full session lookup.
        cookie_id = request.cookies.get(sess_mod.SESSION_COOKIE_NAME)
        db = SessionLocal()
        try:
            session = sess_mod.lookup_session(db, cookie_id)
            if session is None:
                if cookie_id:
                    # Stale or revoked cookie. Clear it and redirect to /login
                    # so the user doesn't bounce on a 401.
                    response = _redirect_to_login(request)
                    response.delete_cookie(
                        sess_mod.SESSION_COOKIE_NAME,
                        path="/",
                        secure=True,
                        httponly=True,
                        samesite="lax",
                    )
                    return response
                return _redirect_to_login(request)

            sess_mod.touch(db, session)
            db.commit()

            user = db.get(M.UserAccount, session.user_id)
            membership = (
                db.get(M.OrgMembership, session.current_membership_id)
                if session.current_membership_id is not None
                else None
            )
            request.state.session = session
            request.state.user = user
            request.state.membership = membership

            if membership is None:
                # Logged in but no org bound — only ``/no-orgs`` and the org
                # picker are reachable. Anything else redirects there.
                if request.url.path not in ("/no-orgs", "/orgs/select"):
                    return Response(status_code=302, headers={"Location": "/no-orgs"})
                return await call_next(request)

            with tenant_context(membership.org_id):
                return await call_next(request)
        finally:
            db.close()


def _redirect_to_login(request: Request) -> Response:
    # Preserve the originally requested path for post-login redirect.
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    location = f"/login?next={target}" if target != "/" else "/login"
    return Response(status_code=302, headers={"Location": location})


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method in _SAFE_METHODS:
            return await call_next(request)
        if any(request.url.path.startswith(p) for p in _CSRF_EXEMPT_PATHS):
            return await call_next(request)
        if SINGLE_TENANT_MODE:
            # AppImage offline path has no auth boundary; CSRF on localhost-only
            # is theatre. Leave headers/CSP doing the real work.
            return await call_next(request)

        cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
        header_token = request.headers.get(CSRF_HEADER_NAME)
        form_token: Optional[str] = None

        ctype = request.headers.get("content-type", "")
        if (
            "application/x-www-form-urlencoded" in ctype
            or "multipart/form-data" in ctype
        ):
            # Read form once, then re-inject into the request scope so the
            # downstream handler still sees it.
            form = await request.form()
            form_token = form.get(CSRF_FORM_FIELD)
            request._form = form  # type: ignore[attr-defined]

        submitted = header_token or form_token
        session_id = request.cookies.get(sess_mod.SESSION_COOKIE_NAME)

        if (
            not submitted
            or not cookie_token
            or submitted != cookie_token
            or not validate_csrf_token(submitted, session_id)
        ):
            return Response("CSRF check failed", status_code=403)

        return await call_next(request)
