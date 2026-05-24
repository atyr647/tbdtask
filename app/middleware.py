"""Request-pipeline middleware.

Four layered concerns, kept in separate functions so each can be tested
in isolation:

1. **TrustedProxyMiddleware** — validates ``x-forwarded-*`` headers against
   a configured list of trusted proxy CIDRs. Strips untrusted forwarded
   headers to prevent client-side header injection.
2. **SecureHeadersMiddleware** — applies CSP, HSTS, X-Frame-Options, etc.
   to every response. Always runs.
3. **AuthRateLimitMiddleware** — per-IP fixed-window on /auth/* paths.
4. **SessionMiddleware** — looks up the cookie-borne session id, validates
   it, attaches ``request.state.session/user/membership``, and enters
   ``tenant_context`` for the active org. Skipped on a small allowlist of
   public routes (login, callback, static, healthz).
5. **CSRFMiddleware** — for unsafe methods, validates the form/header
   token against the session-bound cookie. OIDC callbacks are exempt
   because they have their own ``state`` parameter validation.

In ``SINGLE_TENANT`` mode, SessionMiddleware bypasses authentication
entirely and binds every request to the default org. This keeps the
AppImage offline path identical to its current behaviour.
"""

from __future__ import annotations

import ipaddress
import os
from contextvars import ContextVar
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from . import models as M
from .auth import sessions as sess_mod
from .auth.rate_limit import get_limiter
from .auth.security import (
    CSRF_COOKIE_NAME,
    CSRF_FORM_FIELD,
    CSRF_HEADER_NAME,
    secure_response_headers,
    validate_csrf_token,
)
from .db import SessionLocal
from .tenancy import tenant_context


SINGLE_TENANT_MODE = os.environ.get("TBDTASK_SINGLE_TENANT", "0") == "1"

# Global rate limiter instances — created once at import time. In hosted
# mode with REDIS_URL set these are RedisLimiters; otherwise in-process.
# Three separate buckets so invite acceptance can be rate-limited
# independently from login and org creation.
_AUTH_RATE_LIMITER = get_limiter(limit=10, window_seconds=300)
_ONBOARDING_RATE_LIMITER = get_limiter(limit=10, window_seconds=300)
_INVITE_ACCEPT_RATE_LIMITER = get_limiter(limit=5, window_seconds=300)

# ---------------------------------------------------------------------------
# Trusted proxy configuration
# ---------------------------------------------------------------------------

# Default trusted range: loopback only. Hosted deployments behind a reverse
# proxy should explicitly set TBDTASK_TRUSTED_PROXIES to that proxy's CIDR.
_DEFAULT_TRUSTED = (
    "127.0.0.0/8",
    "::1/128",
)


def _parse_trusted_proxies() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    raw = os.environ.get("TBDTASK_TRUSTED_PROXIES")
    if raw is not None:
        raw = raw.strip()
        if not raw:
            return []
        networks = []
        for token in raw.split(","):
            token = token.strip()
            if token:
                networks.append(ipaddress.ip_network(token, strict=False))
        return networks
    return [ipaddress.ip_network(n, strict=False) for n in _DEFAULT_TRUSTED]


TRUSTED_PROXY_NETWORKS = _parse_trusted_proxies()


def _is_trusted_proxy(ip_str: str) -> bool:
    """Return True if *ip_str* falls within a trusted proxy CIDR."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(addr in net for net in TRUSTED_PROXY_NETWORKS)


# Routes that bypass session enforcement. Keep this list short: every
# entry is a place where the auth invariant doesn't hold by design.
_PUBLIC_PATH_PREFIXES = (
    "/static/",
    "/auth/",
    "/healthz",
    "/login",
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
_ONBOARDING_RATE_LIMITED_PATHS = (
    "/orgs/create",
    "/orgs/select",
    "/orgs/delete",
)
_INVITE_ACCEPT_RATE_LIMITED_PATHS = ("/invites/accept",)


def _is_public(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in _PUBLIC_PATH_PREFIXES)


# ---------------------------------------------------------------------------
# Trusted proxy validation (runs first in the pipeline)
# ---------------------------------------------------------------------------

_FORWARDED_HEADERS = (
    "x-forwarded-for",
    "x-forwarded-proto",
    "x-forwarded-host",
    "x-forwarded-port",
    "x-real-ip",
)


class TrustedProxyMiddleware(BaseHTTPMiddleware):
    """Strip ``x-forwarded-*`` headers when the direct connection is not
    from a trusted proxy.

    Without this guard, any client can forge ``x-forwarded-for`` to bypass
    rate limits or spoof their IP, and can forge ``x-forwarded-proto`` to
    trick the app into emitting HSTS on plain HTTP.

    Configuration: ``TBDTASK_TRUSTED_PROXIES`` — comma-separated CIDRs.
    Defaults to loopback only. Set to empty string to trust nothing.
    """

    async def dispatch(self, request: Request, call_next):
        if not TRUSTED_PROXY_NETWORKS:
            # Trust-nothing mode: strip all forwarded headers so downstream
            # code (rate limiter, HSTS, _client_ip) falls back to the
            # direct connection IP.
            self._strip_forwarded(request)
            return await call_next(request)

        direct = request.client.host if request.client else None
        if direct and _is_trusted_proxy(direct):
            return await call_next(request)

        # Untrusted direct connection — strip forwarded headers.
        self._strip_forwarded(request)
        return await call_next(request)

    @staticmethod
    def _strip_forwarded(request: Request):
        """Remove forwarded headers from the ASGI scope so downstream
        code (Starlette's Request.headers) doesn't see them."""
        scope = request.scope
        if "headers" not in scope:
            return
        # scope["headers"] is a list of (name_bytes, value_bytes) tuples.
        # Rebuild it without the forwarded headers.
        scope["headers"] = [
            (name, value)
            for name, value in scope["headers"]
            if name.decode("latin-1").lower() not in _FORWARDED_HEADERS
        ]
        # Invalidate Starlette's cached Headers object so it rebuilds
        # from the modified scope on next access.
        if hasattr(request, "_headers"):
            delattr(request, "_headers")


def _client_ip(request: Request) -> str:
    """Return the real client IP.

    When the direct connection comes from a trusted proxy, the first entry
    in ``x-forwarded-for`` is used. Otherwise the forwarded header is
    ignored and the direct connection IP is returned. This prevents a
    malicious client from spoofing their IP by injecting their own
    ``x-forwarded-for`` header.
    """
    direct = request.client.host if request.client else "unknown"
    if not TRUSTED_PROXY_NETWORKS:
        return direct
    if not _is_trusted_proxy(direct):
        return direct
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return direct


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
        path = request.url.path
        ip = _client_ip(request)

        if any(path.startswith(p) for p in _AUTH_RATE_LIMITED_PATHS):
            if not _AUTH_RATE_LIMITER.check("auth", ip):
                return Response(
                    "too many auth attempts; try again later",
                    status_code=429,
                    headers={"Retry-After": "60"},
                )
        elif any(path.startswith(p) for p in _ONBOARDING_RATE_LIMITED_PATHS):
            if not _ONBOARDING_RATE_LIMITER.check("onboarding", ip):
                return Response(
                    "too many requests; try again later",
                    status_code=429,
                    headers={"Retry-After": "60"},
                )
        elif any(path.startswith(p) for p in _INVITE_ACCEPT_RATE_LIMITED_PATHS):
            if not _INVITE_ACCEPT_RATE_LIMITER.check("invite_accept", ip):
                return Response(
                    "too many invite attempts; try again later",
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


_SINGLE_TENANT_ORG_ID: Optional[int] = None


def _resolve_single_tenant_org_id() -> int:
    """Look up (and cache) the default org id used for single-tenant mode.

    The Phase 0 migration always seeds an org with slug='default', and the
    AppImage never creates more. Cached after the first resolution so we
    don't query on every request.
    """
    global _SINGLE_TENANT_ORG_ID
    if _SINGLE_TENANT_ORG_ID is not None:
        return _SINGLE_TENANT_ORG_ID
    from sqlalchemy import select

    with SessionLocal() as s:
        oid = s.scalar(select(M.Organization.id).order_by(M.Organization.id).limit(1))
    if oid is None:
        raise RuntimeError(
            "single-tenant mode requires a seeded organization; run init_db() first"
        )
    _SINGLE_TENANT_ORG_ID = oid
    return oid


class SessionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if _is_public(request.url.path):
            return await call_next(request)

        if SINGLE_TENANT_MODE:
            # Bind every request to the single seeded org so the loader
            # criteria filter and the auto-fill listener both have a value.
            oid = _resolve_single_tenant_org_id()
            with tenant_context(oid):
                return await call_next(request)

        # Single session per request: create here, attach to request state,
        # and let route handlers reuse it via ``get_db``. The middleware
        # owns the lifecycle (commit/rollback/close).
        cookie_id = request.cookies.get(sess_mod.SESSION_COOKIE_NAME)
        db = SessionLocal()
        request.state.db = db

        try:
            session = sess_mod.lookup_session(db, cookie_id)
            if session is None:
                if cookie_id:
                    response = _redirect_to_login(request)
                    response.delete_cookie(
                        sess_mod.SESSION_COOKIE_NAME,
                        path="/",
                        secure=sess_mod.SESSION_COOKIE_SECURE,
                        httponly=True,
                        samesite="lax",
                    )
                    return response
                return _redirect_to_login(request)

            # Touch is committed together with route-handler changes
            # (single transaction per request).
            sess_mod.touch(db, session)

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
                    response = Response(
                        status_code=302, headers={"Location": "/no-orgs"}
                    )
                else:
                    response = await call_next(request)
            else:
                with tenant_context(membership.org_id):
                    response = await call_next(request)

            db.commit()
            return response
        except Exception:
            db.rollback()
            raise
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
