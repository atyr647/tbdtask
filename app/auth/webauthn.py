"""WebAuthn registration + assertion primitives (Phase 8a).

This module is the *only* place that touches the `webauthn` library —
routes call ``begin_registration`` / ``finish_registration`` and
``begin_assertion`` / ``finish_assertion`` and never see raw library
types directly. That keeps the wire format stable if we ever swap the
underlying library.

Phase 8a scope:

* Detect WebAuthn PRF support at registration and persist the boolean
  fact via ``UserWebauthnCredential.prf_supported``. The PRF *output*
  is never stored, logged, or transmitted (see
  ``docs/security-architecture.md`` §1 and ``docs/phase-8a-implementation.md``).
* Provide step-up assertion that ``app/auth/step_up.py`` consumes.
* Emit ``AuthEvent`` rows for every state change.

Out of scope here (deferred to Phase 8b):

* PRF *output* derivation into ``KEK_credential``.
* Wrapping/unwrapping of any key material.
* Migration / new-device QR flow.

Feature gating: the module itself works regardless of
``TBDTASK_WEBAUTHN_ENABLED``; routes consult the flag before showing
any UI. This way unit tests can exercise the module without flipping
process-wide environment, and the flag only governs whether the new
code path is reachable from the outside.
"""

from __future__ import annotations

import json as _json
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .. import models as M


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    val = os.environ.get(name)
    return val if val not in (None, "") else default


def is_enabled() -> bool:
    """Whether routes/middleware should expose the WebAuthn flow.

    This gates UI surface and step-up enforcement. The module's pure
    primitives (``begin_registration`` etc.) work regardless, which keeps
    unit testing straightforward.
    """
    return _env("TBDTASK_WEBAUTHN_ENABLED", "0") == "1"


def rp_id() -> str:
    """The Relying Party ID (effectively the host).

    Local development with ``TBDTASK_INSECURE_LOCAL_COOKIES=1`` defaults
    to ``localhost``. Production must set ``TBDTASK_WEBAUTHN_RP_ID``
    explicitly so a misconfigured deploy fails loudly instead of
    accepting credentials issued for a different origin.
    """
    explicit = _env("TBDTASK_WEBAUTHN_RP_ID")
    if explicit:
        return explicit
    if _env("TBDTASK_INSECURE_LOCAL_COOKIES") == "1":
        return "localhost"
    raise RuntimeError(
        "TBDTASK_WEBAUTHN_RP_ID is not set. Configure it to the public "
        "host (e.g. 'tbdtask.example.com') before enabling WebAuthn."
    )


def rp_name() -> str:
    return _env("TBDTASK_WEBAUTHN_RP_NAME", "tbdtask")


def expected_origins() -> list[str]:
    """List of acceptable ``origin`` values in client responses.

    Multiple origins allow a hosted RP_ID with both apex and www
    aliases, or a dev environment that wants both ``http://localhost:8765``
    and ``http://127.0.0.1:8765``.
    """
    raw = _env("TBDTASK_WEBAUTHN_ORIGINS")
    if raw:
        return [s.strip() for s in raw.split(",") if s.strip()]
    rid = rp_id()
    if rid == "localhost":
        return [
            "http://localhost:8765",
            "http://127.0.0.1:8765",
            "https://localhost:8765",
        ]
    return [f"https://{rid}"]


REGISTRATION_TIMEOUT = timedelta(minutes=5)
ASSERTION_TIMEOUT = timedelta(minutes=2)
ENROLLMENT_GRACE_PERIOD = timedelta(days=14)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WebAuthnError(Exception):
    """Base class for all webauthn module errors."""


class WebAuthnVerificationError(WebAuthnError):
    """Raised when a client response fails verification.

    Routes turn this into a 400 with a stable error code; the message
    is never echoed to the client because library error messages can
    leak internal state.
    """


class ChallengeNotFound(WebAuthnError):
    """Raised when no pending challenge matches the user."""


class ChallengeExpired(WebAuthnError):
    """Raised when the matching challenge has timed out."""


# ---------------------------------------------------------------------------
# Data carriers
# ---------------------------------------------------------------------------


@dataclass
class RegistrationChallenge:
    """Server-issued challenge for a registration ceremony.

    Stored only in memory between begin and finish; the client
    round-trips it via the `state` cookie / signed payload (see the
    route layer). Each challenge is single-use.
    """

    challenge: bytes
    user_handle: bytes  # opaque to the authenticator; we use the user_account.id
    rp_id: str
    expires_at: datetime
    options_json: str


@dataclass
class RegistrationResult:
    credential_id: bytes
    public_key: bytes
    aaguid: Optional[str]
    transports: list[str]
    backup_eligible: bool
    backup_state: bool
    prf_supported: bool
    sign_count: int


@dataclass
class AssertionChallenge:
    challenge: bytes
    rp_id: str
    expires_at: datetime
    purpose: str
    allow_credential_ids: list[bytes]
    options_json: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _user_handle(user: M.UserAccount) -> bytes:
    """Stable per-user handle exposed to the authenticator.

    Uses the user_accounts.id encoded as 16 bytes. The handle is
    visible to the authenticator (some platforms store it for
    discoverable credentials), so do not include anything sensitive.
    """
    return user.id.to_bytes(16, "big", signed=False)


def _user_credentials(
    db: Session, *, user: M.UserAccount
) -> list[M.UserWebauthnCredential]:
    return (
        db.query(M.UserWebauthnCredential)
        .filter(
            M.UserWebauthnCredential.user_id == user.id,
            M.UserWebauthnCredential.revoked_at.is_(None),
        )
        .all()
    )


def has_active_credential(db: Session, *, user: M.UserAccount) -> bool:
    """Whether the user has at least one non-revoked credential."""
    return (
        db.query(M.UserWebauthnCredential.id)
        .filter(
            M.UserWebauthnCredential.user_id == user.id,
            M.UserWebauthnCredential.revoked_at.is_(None),
        )
        .first()
        is not None
    )


def _emit_auth_event(
    db: Session,
    *,
    kind: str,
    user_id: Optional[int] = None,
    session_id: Optional[str] = None,
    detail: Optional[dict] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    db.add(
        M.AuthEvent(
            kind=kind,
            user_id=user_id,
            session_id=session_id,
            detail=detail,
            ip=ip,
            user_agent=user_agent,
        )
    )
    # Flush so the row is queryable from the same session even when
    # autoflush is off (tests, batch operations).
    db.flush()


# ---------------------------------------------------------------------------
# Registration (begin / finish)
# ---------------------------------------------------------------------------


def begin_registration(
    db: Session,
    *,
    user: M.UserAccount,
) -> RegistrationChallenge:
    """Mint a registration challenge for ``user``.

    The challenge is returned plus a JSON-serialised options blob the
    client passes straight to ``navigator.credentials.create()``. The
    server caller is responsible for round-tripping ``challenge`` back
    into ``finish_registration`` (typically via a signed cookie / form).
    """
    challenge = secrets.token_bytes(32)
    rid = rp_id()

    # Exclude already-registered credentials so the authenticator can
    # surface a fresh-credential UI instead of accidentally overwriting.
    existing = _user_credentials(db, user=user)
    exclude = [PublicKeyCredentialDescriptor(id=row.credential_id) for row in existing]

    options = generate_registration_options(
        rp_id=rid,
        rp_name=rp_name(),
        user_id=_user_handle(user),
        user_name=user.email,
        user_display_name=user.display_name or user.email,
        challenge=challenge,
        timeout=int(REGISTRATION_TIMEOUT.total_seconds() * 1000),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=exclude or None,
    )
    # The webauthn library doesn't directly expose PRF in
    # generate_registration_options(); we add the extension to the
    # JSON blob the client uses. The client's PRF probe lives in
    # webauthn.js — see docs/phase-8a-implementation.md.
    from webauthn import options_to_json

    options_dict = _json_loads(options_to_json(options))
    options_dict["extensions"] = {
        "prf": {"eval": {"first": bytes_to_base64url(b"\x00" * 32)}}
    }
    options_json = _json_dumps(options_dict)

    return RegistrationChallenge(
        challenge=challenge,
        user_handle=_user_handle(user),
        rp_id=rid,
        expires_at=_utcnow() + REGISTRATION_TIMEOUT,
        options_json=options_json,
    )


def finish_registration(
    db: Session,
    *,
    user: M.UserAccount,
    client_response: dict,
    challenge: RegistrationChallenge,
    nickname: Optional[str] = None,
    request_ip: Optional[str] = None,
    request_user_agent: Optional[str] = None,
) -> RegistrationResult:
    """Verify the attestation, persist the credential, return summary.

    ``client_response`` is the JSON blob the browser returns from
    ``navigator.credentials.create()``. Caller is expected to also
    pass the ``prf`` extension result extracted from the client's
    ``getClientExtensionResults()`` if PRF was probed; the server
    only consumes the *boolean* fact of support (any non-empty PRF
    output → True), never the bytes.
    """
    if challenge.expires_at < _utcnow():
        _emit_auth_event(
            db,
            kind="webauthn_register_failed",
            user_id=user.id,
            detail={"reason": "challenge_expired"},
            ip=request_ip,
            user_agent=request_user_agent,
        )
        raise ChallengeExpired("Registration challenge expired")

    try:
        verified = verify_registration_response(
            credential=client_response,
            expected_challenge=challenge.challenge,
            expected_rp_id=challenge.rp_id,
            expected_origin=expected_origins(),
            require_user_verification=True,
        )
    except Exception as exc:  # webauthn lib raises a variety of subclasses
        _emit_auth_event(
            db,
            kind="webauthn_register_failed",
            user_id=user.id,
            detail={"reason": "verification_failed"},
            ip=request_ip,
            user_agent=request_user_agent,
        )
        raise WebAuthnVerificationError(str(exc)) from exc

    # PRF support detection. We never store the PRF *output*, only the
    # boolean fact that the authenticator returned one. The client
    # sends a stripped extension result with this bit and nothing else.
    prf_supported = bool(
        (client_response.get("clientExtensionResults") or {})
        .get("prf", {})
        .get("enabled", False)
    )

    transports = client_response.get("response", {}).get("transports") or []

    aaguid = (
        str(verified.aaguid)
        if verified.aaguid and str(verified.aaguid) != str(uuid.UUID(int=0))
        else None
    )

    row = M.UserWebauthnCredential(
        id=str(uuid.uuid4()),
        user_id=user.id,
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        aaguid=aaguid,
        transports=list(transports) if transports else None,
        backup_eligible=bool(verified.credential_backed_up)
        if hasattr(verified, "credential_backed_up")
        else False,
        backup_state=bool(
            getattr(verified, "credential_device_type", None) == "multi_device"
        ),
        prf_supported=prf_supported,
        nickname=nickname[:64] if nickname else None,
    )
    db.add(row)
    db.flush()  # surface unique-violations early

    _emit_auth_event(
        db,
        kind="webauthn_register",
        user_id=user.id,
        detail={
            "credential_id": row.id,
            "aaguid": aaguid,
            "prf_supported": prf_supported,
            "transports": transports,
        },
        ip=request_ip,
        user_agent=request_user_agent,
    )

    return RegistrationResult(
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        aaguid=aaguid,
        transports=list(transports),
        backup_eligible=row.backup_eligible,
        backup_state=row.backup_state,
        prf_supported=prf_supported,
        sign_count=verified.sign_count,
    )


# ---------------------------------------------------------------------------
# Assertion (begin / finish) — used for step-up
# ---------------------------------------------------------------------------


def begin_assertion(
    db: Session,
    *,
    user: M.UserAccount,
    purpose: str,
) -> AssertionChallenge:
    """Mint an assertion challenge for ``user`` for the given purpose.

    Purpose is echoed into the resulting ``StepUpGrant`` so a grant
    obtained for ``admin_grant`` cannot satisfy a gate looking for
    ``export``.
    """
    if not user_must_step_up_eligible(db, user=user):
        raise WebAuthnError(
            "User has no registered credentials and cannot be challenged"
        )

    challenge = secrets.token_bytes(32)
    rid = rp_id()
    creds = _user_credentials(db, user=user)
    allow = [
        PublicKeyCredentialDescriptor(
            id=c.credential_id,
            transports=[
                AuthenticatorTransport(t)
                for t in (c.transports or [])
                if _is_known_transport(t)
            ]
            or None,
        )
        for c in creds
    ]
    options = generate_authentication_options(
        rp_id=rid,
        challenge=challenge,
        timeout=int(ASSERTION_TIMEOUT.total_seconds() * 1000),
        allow_credentials=allow,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    from webauthn import options_to_json

    return AssertionChallenge(
        challenge=challenge,
        rp_id=rid,
        expires_at=_utcnow() + ASSERTION_TIMEOUT,
        purpose=purpose,
        allow_credential_ids=[c.credential_id for c in creds],
        options_json=options_to_json(options),
    )


def finish_assertion(
    db: Session,
    *,
    user: M.UserAccount,
    client_response: dict,
    challenge: AssertionChallenge,
    request_ip: Optional[str] = None,
    request_user_agent: Optional[str] = None,
) -> str:
    """Verify the assertion. Returns the ``UserWebauthnCredential.id``
    that satisfied the challenge.

    Updates ``sign_count`` and ``last_used_at`` on the credential.
    Raises ``WebAuthnVerificationError`` on any mismatch (unknown
    credential, replay of sign-count, missing UV flag, etc.).
    """
    if challenge.expires_at < _utcnow():
        _emit_auth_event(
            db,
            kind="webauthn_verify_failed",
            user_id=user.id,
            detail={"reason": "challenge_expired", "purpose": challenge.purpose},
            ip=request_ip,
            user_agent=request_user_agent,
        )
        raise ChallengeExpired("Assertion challenge expired")

    raw_id = _decode_credential_id(
        client_response.get("rawId") or client_response.get("id")
    )
    cred = (
        db.query(M.UserWebauthnCredential)
        .filter(
            M.UserWebauthnCredential.user_id == user.id,
            M.UserWebauthnCredential.credential_id == raw_id,
            M.UserWebauthnCredential.revoked_at.is_(None),
        )
        .first()
    )
    if cred is None:
        _emit_auth_event(
            db,
            kind="webauthn_verify_failed",
            user_id=user.id,
            detail={"reason": "unknown_credential", "purpose": challenge.purpose},
            ip=request_ip,
            user_agent=request_user_agent,
        )
        raise WebAuthnVerificationError("Unknown credential")

    try:
        verified = verify_authentication_response(
            credential=client_response,
            expected_challenge=challenge.challenge,
            expected_rp_id=challenge.rp_id,
            expected_origin=expected_origins(),
            credential_public_key=cred.public_key,
            credential_current_sign_count=cred.sign_count,
            require_user_verification=True,
        )
    except Exception as exc:
        _emit_auth_event(
            db,
            kind="webauthn_verify_failed",
            user_id=user.id,
            detail={"reason": "verification_failed", "purpose": challenge.purpose},
            ip=request_ip,
            user_agent=request_user_agent,
        )
        raise WebAuthnVerificationError(str(exc)) from exc

    cred.sign_count = verified.new_sign_count
    cred.last_used_at = _utcnow()
    db.flush()

    _emit_auth_event(
        db,
        kind="webauthn_verify",
        user_id=user.id,
        detail={"credential_id": cred.id, "purpose": challenge.purpose},
        ip=request_ip,
        user_agent=request_user_agent,
    )
    return cred.id


def revoke_credential(
    db: Session,
    *,
    user: M.UserAccount,
    credential_uuid: str,
    actor_user_id: Optional[int] = None,
) -> None:
    cred = (
        db.query(M.UserWebauthnCredential)
        .filter(
            M.UserWebauthnCredential.id == credential_uuid,
            M.UserWebauthnCredential.user_id == user.id,
            M.UserWebauthnCredential.revoked_at.is_(None),
        )
        .first()
    )
    if cred is None:
        return
    cred.revoked_at = _utcnow()
    db.flush()
    _emit_auth_event(
        db,
        kind="webauthn_revoke",
        user_id=user.id,
        detail={
            "credential_id": cred.id,
            "revoked_by_user_id": actor_user_id or user.id,
        },
    )


def user_must_step_up_eligible(db: Session, *, user: M.UserAccount) -> bool:
    """Whether step-up auth is even possible for this user.

    Returns False if the user has no active credentials. Routes that
    need to gate sensitive actions can use this to decide between
    challenging the user vs. refusing outright.
    """
    return has_active_credential(db, user=user)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# JSON helpers — webauthn returns a JSON string from options_to_json;
# we decode and re-encode to inject the PRF extension. Keep these
# isolated so a future swap to orjson stays mechanical.
def _json_loads(text: str) -> dict:
    return _json.loads(text)


def _json_dumps(data: dict) -> str:
    return _json.dumps(data, separators=(",", ":"))


_KNOWN_TRANSPORTS = {t.value for t in AuthenticatorTransport}


def _is_known_transport(t: str) -> bool:
    return t in _KNOWN_TRANSPORTS


def _decode_credential_id(raw) -> bytes:
    """Tolerant decode of the credential id from either a base64url
    string or pre-decoded bytes."""
    if raw is None:
        raise WebAuthnVerificationError("Missing credential id")
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    if isinstance(raw, str):
        from webauthn.helpers import base64url_to_bytes

        return base64url_to_bytes(raw)
    raise WebAuthnVerificationError("Unrecognised credential id encoding")
