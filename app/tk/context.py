"""Runtime bootstrap and the session access helpers.

The FastAPI app entered a ``tenant_context`` per request via middleware.
A Tk app has no request pipeline, so we do the same thing here once: the
launcher forces single-tenant mode, ``init_db()`` brings the schema to
head and seeds the ``default`` org, and every data access runs inside
``tenant_context(<default org id>)`` so the tenancy loader-criteria and
``org_id`` autofill behave exactly as they do under the web server.
"""

from __future__ import annotations

import os
from typing import Callable, TypeVar

# Force single-tenant before anything auth-aware imports and reads the flag,
# mirroring ``app.main``'s launcher guard. Must happen prior to importing the
# db/models modules below.
os.environ.setdefault("TBDTASK_SINGLE_TENANT", "1")
os.environ.setdefault("TBDTASK_LAUNCHER", "1")

from sqlalchemy import select  # noqa: E402

from .. import models as M  # noqa: E402
from ..db import init_db, session_scope  # noqa: E402
from ..tenancy import tenant_context  # noqa: E402

T = TypeVar("T")

_org_id: int | None = None


def bootstrap() -> int:
    """Bring the DB to head and resolve the single-tenant org id.

    Idempotent. Returns the org id every session is bound to.
    """
    global _org_id
    if _org_id is not None:
        return _org_id
    init_db()
    with session_scope() as s:
        oid = s.scalar(select(M.Organization.id).order_by(M.Organization.id).limit(1))
    if oid is None:
        raise RuntimeError(
            "single-tenant mode requires a seeded organization; "
            "init_db() should have created one"
        )
    _org_id = oid
    return oid


def org_id() -> int:
    return _org_id if _org_id is not None else bootstrap()


def read(fn: Callable[..., T]) -> T:
    """Run ``fn(session)`` inside the tenant context, read-only.

    ``fn`` must return only detached values (DTOs / primitives) — never
    live ORM instances, which would raise ``DetachedInstanceError`` on
    the first lazy load after the session closes.
    """
    oid = org_id()
    with tenant_context(oid), session_scope() as s:
        return fn(s)


def write(fn: Callable[..., T]) -> T:
    """Run ``fn(session)`` inside the tenant context and commit.

    Identical to :func:`read` today (``session_scope`` commits on a clean
    exit); kept as a distinct name so call sites document intent and so a
    future audit-actor hook has one place to attach.
    """
    oid = org_id()
    with tenant_context(oid), session_scope() as s:
        return fn(s)
