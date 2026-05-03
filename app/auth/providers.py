"""OIDC client registry for Apple / Google / Microsoft.

Each provider is configured from environment variables so credentials
never live in the repo. A provider whose ``client_id`` env var is missing
is silently absent from the login page — the rest still work. This keeps
local-dev installs functional with only one provider configured.

Account linking rules (locked decisions):

* Lookup is always ``(provider, subject)``. The subject is stable across
  email changes upstream.
* Email-based candidate lookup is allowed only when the IdP returns
  ``email_verified=true``. Even then, the resolution is to send the user
  through an *explicit* re-auth-the-other-provider link flow — never a
  silent merge.
* Apple private-relay addresses (``@privaterelay.appleid.com``) are
  treated as ``email_verified=False`` for linking purposes regardless of
  what Apple says, since the relay address proves Apple control, not
  mailbox control. Implemented in ``normalize_userinfo``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from authlib.integrations.starlette_client import OAuth


# Discovery URLs. Apple's is published; Google's is the standard well-known;
# Microsoft uses the multi-tenant ``common`` issuer so any work/school/MSA
# account can sign in. Org admins who want to restrict to a specific tenant
# point ``OIDC_MICROSOFT_ISSUER`` at their tenant-specific URL.
GOOGLE_DISCOVERY = "https://accounts.google.com/.well-known/openid-configuration"
MICROSOFT_DISCOVERY_DEFAULT = (
    "https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration"
)
APPLE_DISCOVERY = "https://appleid.apple.com/.well-known/openid-configuration"


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    display_name: str
    client_id: str
    client_secret: str
    discovery_url: str
    scopes: str = "openid email profile"


def _env(name: str) -> Optional[str]:
    val = os.environ.get(name)
    return val if val else None


def _build_provider_configs() -> dict[str, ProviderConfig]:
    """Read provider config from env. Providers without a client_id are skipped."""
    configs: dict[str, ProviderConfig] = {}

    google_id = _env("OIDC_GOOGLE_CLIENT_ID")
    google_secret = _env("OIDC_GOOGLE_CLIENT_SECRET")
    if google_id and google_secret:
        configs["google"] = ProviderConfig(
            name="google",
            display_name="Google",
            client_id=google_id,
            client_secret=google_secret,
            discovery_url=GOOGLE_DISCOVERY,
        )

    ms_id = _env("OIDC_MICROSOFT_CLIENT_ID")
    ms_secret = _env("OIDC_MICROSOFT_CLIENT_SECRET")
    if ms_id and ms_secret:
        configs["microsoft"] = ProviderConfig(
            name="microsoft",
            display_name="Microsoft",
            client_id=ms_id,
            client_secret=ms_secret,
            discovery_url=_env("OIDC_MICROSOFT_DISCOVERY") or MICROSOFT_DISCOVERY_DEFAULT,
        )

    apple_id = _env("OIDC_APPLE_CLIENT_ID")
    # Apple uses a JWT-based client_secret. Phase 1 accepts a pre-minted
    # JWT via env; a refresh helper that mints from the EC private key
    # lands in Phase 7 ops automation. Without a configured secret the
    # provider is hidden from the login page.
    apple_secret = _env("OIDC_APPLE_CLIENT_SECRET")
    if apple_id and apple_secret:
        configs["apple"] = ProviderConfig(
            name="apple",
            display_name="Apple",
            client_id=apple_id,
            client_secret=apple_secret,
            discovery_url=APPLE_DISCOVERY,
            # Apple is restrictive: only ``name`` and ``email`` are available
            # and only on first sign-in. We capture the subject and treat
            # ``name`` as best-effort.
            scopes="openid email name",
        )

    return configs


_provider_configs: dict[str, ProviderConfig] = _build_provider_configs()


def configured_providers() -> dict[str, ProviderConfig]:
    """Return the providers with usable credentials. Read-only snapshot."""
    return dict(_provider_configs)


def is_configured(provider: str) -> bool:
    return provider in _provider_configs


# ---------------------------------------------------------------------------
# Authlib OAuth registry
# ---------------------------------------------------------------------------

oauth = OAuth()


def _register_clients() -> None:
    for cfg in _provider_configs.values():
        oauth.register(
            name=cfg.name,
            client_id=cfg.client_id,
            client_secret=cfg.client_secret,
            server_metadata_url=cfg.discovery_url,
            client_kwargs={"scope": cfg.scopes},
        )


_register_clients()


def get_client(provider: str):
    """Return the Authlib client for ``provider`` or raise KeyError."""
    if provider not in _provider_configs:
        raise KeyError(f"OIDC provider {provider!r} is not configured")
    return getattr(oauth, provider)


# ---------------------------------------------------------------------------
# Userinfo normalization (linking-safety enforced here)
# ---------------------------------------------------------------------------

APPLE_PRIVATE_RELAY_DOMAIN = "@privaterelay.appleid.com"


@dataclass(frozen=True)
class NormalizedIdentity:
    provider: str
    subject: str
    email: Optional[str]
    # ``email_verified`` is the *linking-safe* flag, not the raw IdP claim.
    # See Apple-relay handling below.
    email_verified: bool
    display_name: Optional[str]


def normalize_userinfo(provider: str, claims: dict) -> NormalizedIdentity:
    """Convert raw IdP claims into the dataclass the auth flow consumes.

    Critically: an Apple private-relay address is ALWAYS treated as
    ``email_verified=False`` here, even if Apple says verified. The relay
    address proves the user controls Apple's relay forwarding, not the
    underlying mailbox, so it's unsafe as a linking signal.
    """
    if provider not in ("apple", "google", "microsoft"):
        raise ValueError(f"unknown provider {provider!r}")

    subject = claims.get("sub")
    if not subject:
        raise ValueError("OIDC userinfo is missing 'sub' claim")

    email = claims.get("email")
    raw_verified = bool(claims.get("email_verified", False))

    # Microsoft's v2 endpoint sometimes omits email_verified; treat the
    # presence of a ``upn``/``preferred_username`` matching the email as
    # verified. Conservative default: not verified unless we're sure.
    if provider == "microsoft" and "email_verified" not in claims:
        upn = claims.get("preferred_username") or claims.get("upn")
        raw_verified = bool(email) and bool(upn) and upn.lower() == email.lower()

    email_verified = raw_verified
    if (
        provider == "apple"
        and email
        and email.lower().endswith(APPLE_PRIVATE_RELAY_DOMAIN)
    ):
        email_verified = False

    display_name = (
        claims.get("name")
        or claims.get("given_name")
        or (email.split("@")[0] if email else None)
    )

    return NormalizedIdentity(
        provider=provider,
        subject=str(subject),
        email=email.lower() if email else None,
        email_verified=email_verified,
        display_name=display_name,
    )
