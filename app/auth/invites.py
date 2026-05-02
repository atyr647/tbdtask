"""Invite issuance + redemption helpers.

The Phase 1 accept flow lives in ``app.routes.onboarding``. Phase 2's
admin UI will call ``create_invite`` from here. Centralising the TTL caps
and hashing logic keeps the security profile in one place.

Security profile (final, agreed):

* Tokens are 256 bits of URL-safe random. The raw token is shown to the
  admin exactly once (return value of ``create_invite``); only the
  SHA-256 hash lands in the DB.
* ``intended_email`` is required. Redemption refuses if the redeeming
  user's canonical email differs.
* Invites have a mandatory expiry. The admin UI may not exceed
  ``MAX_INVITE_TTL_DAYS``.
* Single-use: ``accepted_at`` set on success.
* Revocable: ``revoked_at`` set by admin action; redemption checks first.
* Generic redeem-failure responses; specific reason recorded only in
  the audit log so attackers can't probe for valid tokens.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .. import models as M
from .security import random_token


# Defaults the admin UI can override within the cap. A 7-day window is
# long enough to cover "I'll send this Friday and you redeem Monday"
# while still short enough that a leaked token rots before it's useful.
DEFAULT_INVITE_TTL_DAYS = 7
# Hard cap. The admin UI must reject any TTL above this.
MAX_INVITE_TTL_DAYS = 30


def hash_invite_token(raw: str) -> str:
    """SHA-256 hex digest. Same algo on issue + accept; never log raw tokens."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IssuedInvite:
    invite_id: int
    raw_token: str  # show once to the admin; never persisted in plaintext
    expires_at: datetime


def create_invite(
    db: Session,
    *,
    org_id: int,
    intended_email: str,
    created_by_user_id: Optional[int],
    ttl_days: int = DEFAULT_INVITE_TTL_DAYS,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    rate: Optional[str] = None,
    paygrade: Optional[str] = None,
) -> IssuedInvite:
    """Issue a fresh invite. Returns the row id and the raw token.

    The caller is responsible for delivering the raw token to the
    intended recipient out-of-band (email, Teams DM, etc.). The token
    is never re-displayable: a lost token requires issuing a new invite.

    Personnel fields (first_name, last_name, title, level) are stored
    so that on acceptance a ``Person`` record is auto-created.
    """
    if ttl_days < 1 or ttl_days > MAX_INVITE_TTL_DAYS:
        raise ValueError(
            f"ttl_days must be between 1 and {MAX_INVITE_TTL_DAYS}"
        )
    cleaned_email = intended_email.strip().lower()
    if not cleaned_email or "@" not in cleaned_email:
        raise ValueError("intended_email must be a valid address")

    raw = random_token(32)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    invite = M.OrgInvite(
        org_id=org_id,
        token_hash=hash_invite_token(raw),
        intended_email=cleaned_email,
        created_by_user_id=created_by_user_id,
        expires_at=now + timedelta(days=ttl_days),
        first_name=first_name,
        last_name=last_name,
        rate=rate,
        paygrade=paygrade,
    )
    db.add(invite)
    db.flush()
    return IssuedInvite(
        invite_id=invite.id,
        raw_token=raw,
        expires_at=invite.expires_at,
    )
