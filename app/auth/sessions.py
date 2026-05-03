"""Server-side session storage and rotation.

A session lives in the ``user_sessions`` table; the cookie carries only
the opaque id. Every authenticated request:

1. Looks up the session by id.
2. Verifies ``revoked_at IS NULL``, ``last_seen_at`` within idle window,
   and ``created_at`` within absolute window.
3. Verifies the user is not disabled and (if a membership is bound) the
   membership status is ``active``.
4. Updates ``last_seen_at`` (debounced).

Rotation = revoke the existing row and issue a brand-new id. Never reuse
ids, never update an id in place. Triggers: login, logout, identity
link/unlink, org switch, role/membership status change.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as M
from .security import random_token


# Cookie + session lifetime knobs. Phase 1 hard-codes them; Phase 7 makes
# them configurable per-org if anyone asks.
#
# For local-only tooling (screenshots/tests against http://127.0.0.1), use a
# non-__Host- cookie so browsers do not require the Secure flag. Keep this
# opt-in intentionally loud: production must never set it.
_INSECURE_LOCAL_COOKIES = os.environ.get("TBDTASK_INSECURE_LOCAL_COOKIES", "0") == "1"
if _INSECURE_LOCAL_COOKIES:
    if os.environ.get("DATABASE_URL", "").startswith("postgres"):
        raise RuntimeError("TBDTASK_INSECURE_LOCAL_COOKIES is not allowed with Postgres")
    SESSION_COOKIE_NAME = "tbdtask_session_local"
    SESSION_COOKIE_SECURE = False
else:
    SESSION_COOKIE_NAME = "__Host-tbdtask_session"
    SESSION_COOKIE_SECURE = True

IDLE_TIMEOUT = timedelta(hours=12)
ABSOLUTE_TIMEOUT = timedelta(days=30)
# How often we touch ``last_seen_at`` to avoid hammering the DB on every
# request. A request inside this window of the last update is a no-op.
LAST_SEEN_WRITE_DEBOUNCE = timedelta(minutes=1)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def create_session(
    db: Session,
    *,
    user_id: int,
    membership_id: Optional[int],
    ip: Optional[str],
    user_agent: Optional[str],
) -> M.UserSession:
    """Create a new session row. Returns the persisted object.

    The caller is responsible for setting the cookie on the response and
    for committing the surrounding transaction.
    """
    sess = M.UserSession(
        id=random_token(32),
        user_id=user_id,
        current_membership_id=membership_id,
        ip=ip,
        user_agent=user_agent,
    )
    db.add(sess)
    db.flush()
    return sess


def lookup_session(db: Session, session_id: Optional[str]) -> Optional[M.UserSession]:
    """Return the session row if valid, else None.

    "Valid" means: exists, not revoked, within idle + absolute windows,
    user not disabled, and — if a membership is bound — that membership
    is active. Any failure here returns None; the middleware decides what
    to do with that (usually: clear cookie + redirect to login).
    """
    if not session_id:
        return None

    sess = db.get(M.UserSession, session_id)
    if sess is None or sess.revoked_at is not None:
        return None

    now = _now()
    if now - sess.created_at > ABSOLUTE_TIMEOUT:
        return None
    if now - sess.last_seen_at > IDLE_TIMEOUT:
        return None

    user = db.get(M.UserAccount, sess.user_id)
    if user is None or user.disabled_at is not None:
        return None

    if sess.current_membership_id is not None:
        mem = db.get(M.OrgMembership, sess.current_membership_id)
        if mem is None or mem.status != "active":
            return None

    return sess


def touch(db: Session, session: M.UserSession) -> None:
    """Bump ``last_seen_at`` if the debounce window has passed."""
    now = _now()
    if now - session.last_seen_at >= LAST_SEEN_WRITE_DEBOUNCE:
        session.last_seen_at = now
        db.flush()


def revoke(db: Session, session: M.UserSession, *, reason: str) -> None:
    """Mark the session revoked. Idempotent."""
    if session.revoked_at is None:
        session.revoked_at = _now()
        db.flush()


def rotate(
    db: Session,
    old: M.UserSession,
    *,
    membership_id: Optional[int] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
    reason: str = "rotation",
) -> M.UserSession:
    """Revoke the existing session and issue a new one for the same user.

    ``membership_id`` defaults to the old session's binding; pass
    explicitly when rotating because of an org switch.
    """
    revoke(db, old, reason=reason)
    return create_session(
        db,
        user_id=old.user_id,
        membership_id=(
            membership_id if membership_id is not None else old.current_membership_id
        ),
        ip=ip if ip is not None else old.ip,
        user_agent=user_agent if user_agent is not None else old.user_agent,
    )


def revoke_all_for_user(db: Session, user_id: int, *, reason: str) -> int:
    """Revoke every active session for a user. Returns the count revoked.

    Called when a user is disabled or when a membership's status changes
    in a way that should kick out every device. The reason is recorded by
    the caller via an ``AuthEvent``; this helper is purely DB-level.
    """
    rows = (
        db.execute(
            select(M.UserSession).where(
                M.UserSession.user_id == user_id,
                M.UserSession.revoked_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    now = _now()
    for s in rows:
        s.revoked_at = now
    db.flush()
    return len(rows)


def revoke_all_for_membership(db: Session, membership_id: int) -> int:
    """Revoke every session bound to a specific membership.

    Used when a membership is suspended or deleted — the user can still
    log into other orgs, but any session anchored to this org is dead.
    """
    rows = (
        db.execute(
            select(M.UserSession).where(
                M.UserSession.current_membership_id == membership_id,
                M.UserSession.revoked_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    now = _now()
    for s in rows:
        s.revoked_at = now
    db.flush()
    return len(rows)
