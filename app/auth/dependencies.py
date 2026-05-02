"""FastAPI dependencies for the auth + tenancy layer.

Routes consume these instead of poking at request state directly. The
middleware (``app.middleware``) is the only place that *populates* the
attached request state; the dependencies just read it.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .. import models as M
from ..db import SessionLocal


def get_db() -> Session:
    """Per-request DB session.

    FastAPI calls this via ``Depends`` and closes the generator afterwards,
    so a single transaction scopes the whole request.
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_current_session(request: Request) -> Optional[M.UserSession]:
    """Return the session row attached by the middleware, or None."""
    return getattr(request.state, "session", None)


def get_current_user(request: Request) -> Optional[M.UserAccount]:
    return getattr(request.state, "user", None)


def get_current_membership(request: Request) -> Optional[M.OrgMembership]:
    return getattr(request.state, "membership", None)


def require_user(
    user: Optional[M.UserAccount] = Depends(get_current_user),
) -> M.UserAccount:
    """Force authentication. Raises 401 if no user in scope."""
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
        )
    return user


def require_membership(
    membership: Optional[M.OrgMembership] = Depends(get_current_membership),
) -> M.OrgMembership:
    """Force a bound active membership. Raises 401 if none.

    Routes that should work for users who haven't picked an org yet
    (``/no-orgs``, the org picker, ``/auth/logout``) should use
    ``require_user`` instead.
    """
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="no active organization membership",
        )
    return membership
