"""Auth-layer security primitives: CSRF, secure headers.

Phase 1.5 hard-gate components. The CSRF token + headers middleware all
land here so they can be reviewed and tested as a single unit before any
state-changing route ships.

Design notes:

* CSRF uses a signed double-submit token tied to the session id, not the
  request body. Both the form and a same-name cookie carry the token; the
  middleware checks they match AND the signature is valid for the current
  session. This survives subdomain isolation and works with HTMX.
* Secure headers are applied by middleware so no individual route handler
  can forget. CSP starts strict; per-route relaxations (e.g. for inline
  styles) are explicit.
"""
from __future__ import annotations

import os
import secrets
from typing import Optional

from itsdangerous import BadSignature, URLSafeTimedSerializer


# Random URL-safe identifier suitable for session ids, OIDC state, and
# nonces. 32 bytes = 256 bits, base64url-encoded ≈ 43 chars.
def random_token(num_bytes: int = 32) -> str:
    return secrets.token_urlsafe(num_bytes)


# ---------------------------------------------------------------------------
# Signing — single source for app-internal HMAC signatures
# ---------------------------------------------------------------------------

# Read once at import. Set ``TBDTASK_SECRET_KEY`` in any environment that
# isn't a transient test process. The dev fallback is intentionally
# derived from a stable per-installation file so single-tenant AppImage
# users get a stable signer without manual setup.
def _resolve_secret_key() -> str:
    env = os.environ.get("TBDTASK_SECRET_KEY")
    if env:
        return env
    # Single-tenant / dev fallback: persist a random key to the data dir
    # the first time we run and reuse it thereafter. Never used in hosted
    # mode because hosted deployments must set TBDTASK_SECRET_KEY.
    from ..db import DATA_DIR

    key_path = DATA_DIR / "secret_key"
    if key_path.exists():
        return key_path.read_text().strip()
    fresh = random_token(48)
    key_path.write_text(fresh)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return fresh


SECRET_KEY = _resolve_secret_key()


def signer(salt: str) -> URLSafeTimedSerializer:
    """Return an itsdangerous serializer bound to a per-purpose salt.

    Distinct salts per purpose ("csrf", "oidc-state", "oidc-nonce") prevent
    a token issued for one purpose from validating against another.
    """
    return URLSafeTimedSerializer(SECRET_KEY, salt=salt)


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

# Tokens last as long as a session would; the cookie is rotated on session
# rotation, but the token signature has its own expiration as a defence in
# depth.
if os.environ.get("TBDTASK_INSECURE_LOCAL_COOKIES", "0") == "1":
    CSRF_COOKIE_NAME = "tbdtask_csrf_local"
else:
    CSRF_COOKIE_NAME = "__Host-tbdtask_csrf"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"
CSRF_TOKEN_TTL_SECONDS = 60 * 60 * 24  # 24h


def issue_csrf_token(session_id: Optional[str]) -> str:
    """Return a signed token bound to the session id.

    A null session id (anonymous user fetching the login page) gets a
    token bound to the literal string ``"anon"`` so the form can still
    validate. The login POST that creates the session rotates the token
    along with the session.
    """
    bound_to = session_id or "anon"
    return signer("csrf").dumps(bound_to)


def validate_csrf_token(token: str, session_id: Optional[str]) -> bool:
    if not token:
        return False
    try:
        bound_to = signer("csrf").loads(token, max_age=CSRF_TOKEN_TTL_SECONDS)
    except BadSignature:
        return False
    expected = session_id or "anon"
    return secrets.compare_digest(bound_to, expected)


# ---------------------------------------------------------------------------
# OIDC state + nonce
# ---------------------------------------------------------------------------

OIDC_STATE_TTL_SECONDS = 60 * 10  # 10 min — must complete redirect dance promptly


def issue_oidc_state(payload: dict) -> str:
    """Sign the OIDC state payload (provider, return_url, csrf-binding)."""
    return signer("oidc-state").dumps(payload)


def validate_oidc_state(token: str) -> Optional[dict]:
    try:
        return signer("oidc-state").loads(token, max_age=OIDC_STATE_TTL_SECONDS)
    except BadSignature:
        return None


# ---------------------------------------------------------------------------
# Secure response headers
# ---------------------------------------------------------------------------

# Strict-by-default CSP. ``'self'`` only for scripts/styles; no inline,
# no eval. HTMX is loaded as a static asset so it lives under ``'self'``.
# Per-route relaxations (e.g. nonced inline scripts for a single page)
# would be added explicitly, never globally.
DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "base-uri 'self'"
)


def secure_response_headers(*, https: bool) -> dict[str, str]:
    """Return the static headers applied to every response.

    HSTS is only emitted on HTTPS responses so local-dev HTTP traffic
    isn't pinned to HTTPS by browser caches.
    """
    headers = {
        "Content-Security-Policy": DEFAULT_CSP,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "geolocation=(), camera=(), microphone=(), payment=()",
    }
    if https:
        # 6 months, includeSubDomains, preload-eligible. Hosts that can't
        # serve HTTPS for every subdomain should override via env var.
        headers["Strict-Transport-Security"] = (
            "max-age=15552000; includeSubDomains; preload"
        )
    return headers
