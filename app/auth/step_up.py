"""Step-up auth grants and gate dependency (Phase 8a).

A *step-up grant* is a short-lived row in ``step_up_grants`` that
records "this session re-verified with this credential, for this
purpose, at this time." Routes that perform sensitive actions add a
``Depends(require_step_up("admin_grant"))`` and the gate raises
``StepUpRequired`` if no fresh grant exists.

Single-use grants are consumed (``consumed_at`` set) by the gate on a
successful match, so each sensitive action prompts once. Non-single-use
grants are reusable for a TTL window — useful in 8c+ for keeping a
field-decrypt session unlocked across multiple reads.

Gating: this module is enabled in two layers.

* ``require_step_up`` honors the ``TBDTASK_WEBAUTHN_ENABLED`` flag —
  while the flag is off, gates are short-circuited so the existing
  app keeps working unchanged. This is the primary roll-back lever.
* Tests can pass ``enforce_when_disabled=True`` (or call the
  primitives directly) to exercise the gate without flipping the
  process-wide environment.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import models as M
from ..db import SessionLocal
from . import webauthn as _webauthn
from .sessions import SESSION_COOKIE_NAME


STEP_UP_TTL = timedelta(minutes=10)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StepUpRequired(HTTPException):
    """Raised by ``require_step_up`` when no fresh grant satisfies the gate.

    The route layer catches this and either renders the step-up
    challenge page (HTML) or returns a 403 with a JSON body pointing
    at the challenge endpoint (API). The status_code is 403 rather
    than 401 because the user is *authenticated* — they just need to
    re-verify for this specific action.
    """

    def __init__(self, purpose: str, *, next_path: Optional[str] = None) -> None:
        super().__init__(
            status_code=403, detail={"reason": "step_up_required", "purpose": purpose}
        )
        self.purpose = purpose
        self.next_path = next_path


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def grant_step_up(
    db: Session,
    *,
    session_id: str,
    credential_id: str,
    purpose: str,
    ttl: timedelta = STEP_UP_TTL,
) -> M.StepUpGrant:
    """Persist a step-up grant after a successful assertion.

    Caller is responsible for having verified the assertion with
    ``webauthn.finish_assertion`` and for confirming the session row
    exists. The grant is *not* automatically single-use — that's a
    consumer-side decision via ``has_step_up(consume=True)``.
    """
    grant = M.StepUpGrant(
        id=str(uuid.uuid4()),
        session_id=session_id,
        credential_id=credential_id,
        expires_at=_utcnow() + ttl,
        purpose=purpose,
    )
    db.add(grant)
    db.flush()
    return grant


def has_step_up(
    db: Session,
    *,
    session_id: str,
    purpose: str,
    consume: bool = False,
) -> bool:
    """Whether a fresh, matching, unconsumed grant exists.

    If ``consume`` is True and a match is found, mark it consumed
    before returning. Returns False if the session has no grant for
    this purpose, the grant has expired, or the grant has already
    been consumed.
    """
    now = _utcnow()
    grant = (
        db.query(M.StepUpGrant)
        .filter(
            M.StepUpGrant.session_id == session_id,
            M.StepUpGrant.purpose == purpose,
            M.StepUpGrant.consumed_at.is_(None),
            M.StepUpGrant.expires_at > now,
        )
        .order_by(M.StepUpGrant.granted_at.desc())
        .first()
    )
    if grant is None:
        return False
    if consume:
        grant.consumed_at = now
        db.flush()
    return True


def revoke_grants_for_session(db: Session, *, session_id: str) -> int:
    """Mark all live grants for ``session_id`` consumed.

    Useful on logout and on credential revocation. Returns the number
    of grants invalidated.
    """
    now = _utcnow()
    rows = (
        db.query(M.StepUpGrant)
        .filter(
            M.StepUpGrant.session_id == session_id,
            M.StepUpGrant.consumed_at.is_(None),
        )
        .all()
    )
    for r in rows:
        r.consumed_at = now
    if rows:
        db.flush()
    return len(rows)


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


def require_step_up(
    purpose: str,
    *,
    single_use: bool = True,
    enforce_when_disabled: bool = False,
):
    """Return a FastAPI dependency that gates a route on a fresh grant.

    Usage:

        @router.post("/admin/something")
        def something(_: None = Depends(require_step_up("admin_grant"))):
            ...

    While ``TBDTASK_WEBAUTHN_ENABLED`` is unset/false, the gate is a
    no-op so existing behavior is preserved. Tests can pass
    ``enforce_when_disabled=True`` to exercise the gate without flipping
    the env var.
    """

    def dep(request: Request) -> None:
        if not enforce_when_disabled and not _webauthn.is_enabled():
            return
        session_id = request.cookies.get(SESSION_COOKIE_NAME)
        if not session_id:
            # No session => the auth middleware will redirect to login
            # before we get here. Be defensive anyway.
            raise StepUpRequired(purpose, next_path=str(request.url))
        with SessionLocal() as db:
            ok = has_step_up(
                db,
                session_id=session_id,
                purpose=purpose,
                consume=single_use,
            )
            if ok and single_use:
                db.commit()
            if not ok:
                # Emit an audit event so a brute-force scan against the
                # gate is visible. Don't include any user identifier
                # because a no-session caller might not have one.
                db.add(
                    M.AuthEvent(
                        kind="step_up_failed",
                        session_id=session_id,
                        detail={"purpose": purpose, "reason": "no_grant"},
                        ip=_client_ip(request),
                        user_agent=request.headers.get("user-agent"),
                    )
                )
                db.commit()
                raise StepUpRequired(purpose, next_path=str(request.url))

    return Depends(dep)


def _client_ip(request: Request) -> Optional[str]:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        # Trust only the first hop; the proxy middleware validates
        # provenance upstream.
        return fwd.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None
