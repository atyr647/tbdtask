"""Permission checking and route gating.

Phase 2 of the multi-tenant pivot. Hangs together the permission catalog
(``app.auth.permissions``), the membership-role schema (``models.py``),
and the FastAPI request pipeline.

Usage:

    from .auth.authorization import require
    from .auth.permissions import P_PERSONNEL_WRITE

    @router.post("/personnel")
    def create_person(..., _ = Depends(require(P_PERSONNEL_WRITE))):
        ...

The decorator returns a FastAPI dependency that:

* Reads the membership attached by ``app.middleware.SessionMiddleware``.
* Loads the membership's role grants and walks the workcenter tree if
  the route is workcenter-scoped (``workcenter_param=`` argument).
* Raises ``HTTPException(403)`` on failure with a generic detail; the
  reason (membership-id, missing perm) is captured in the audit log
  via ``M.AuthEvent`` only.

In ``SINGLE_TENANT`` mode every check returns True — the AppImage has
no auth boundary by design, so the decorator is a no-op there.

Workcenter-scoped checks:

* ``require(perm, workcenter_param="workcenter_id")`` looks up the
  param from ``request.path_params`` first, then ``request.query_params``,
  then the (already-parsed) form. The grant is honoured when the
  membership has the permission either org-wide *or* on the requested
  workcenter or any of its ancestors. Phase 3's RLS pushes this into
  the DB, but for now the walk happens in Python.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, FrozenSet, Optional, Sequence

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as M
from .. import db as _db
from .permissions import Permission, is_known_permission


# Read once at import. Tests can monkeypatch ``app.middleware.SINGLE_TENANT_MODE``
# to flip behaviour, and we honour that by re-reading at check time so the
# import-order doesn't lock the decorator to a stale value.
def _single_tenant_mode() -> bool:
    return os.environ.get("TBDTASK_SINGLE_TENANT", "0") == "1"


# ---------------------------------------------------------------------------
# Effective-permission resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EffectivePermissions:
    """Snapshot of a membership's permission grants.

    ``org_wide`` is the set of permission codes granted org-wide (i.e.
    via a role with ``workcenter_id IS NULL``).

    ``by_workcenter`` maps a workcenter id → permission codes granted on
    that specific workcenter. The workcenter-tree walk happens later
    (``has_permission``) so the snapshot is cheap to compute and cache.

    Phase 3 RLS will reproduce the same logic at the DB level; this
    dataclass stays the source of truth for app-layer checks plus
    admin-UI rendering.
    """

    org_wide: FrozenSet[str]
    by_workcenter: dict[int, FrozenSet[str]]


def load_effective_permissions(db: Session, membership_id: int) -> EffectivePermissions:
    """Read every permission code granted to ``membership_id``.

    A single SQL hit (joined across membership_roles → roles →
    role_permissions) returns one row per (workcenter_id, code) tuple.
    Caller is responsible for tenant scoping; in practice this runs
    inside a ``tenant_context`` so the listener already restricts the
    join to the active org.
    """
    rows = db.execute(
        select(
            M.MembershipRole.workcenter_id,
            M.RolePermission.permission_code,
        )
        .join(M.Role, M.Role.id == M.MembershipRole.role_id)
        .join(M.RolePermission, M.RolePermission.role_id == M.Role.id)
        .where(M.MembershipRole.membership_id == membership_id)
    ).all()

    org_wide: set[str] = set()
    by_wc: dict[int, set[str]] = {}
    for wc_id, code in rows:
        if wc_id is None:
            org_wide.add(code)
        else:
            by_wc.setdefault(wc_id, set()).add(code)

    return EffectivePermissions(
        org_wide=frozenset(org_wide),
        by_workcenter={k: frozenset(v) for k, v in by_wc.items()},
    )


def workcenter_ancestors(db: Session, workcenter_id: int) -> list[int]:
    """Return ``[workcenter_id, parent_id, grandparent_id, ...]``.

    Includes the start node. The iteration is Python-side because the
    typical depth is single digits and SQLite doesn't have recursive
    CTEs everywhere we want to support. Loops are detected and bail —
    self-FK loops shouldn't exist (the admin UI rejects re-parenting
    that would create one) but we don't trust the schema to enforce
    invariants the app should be enforcing anyway.
    """
    chain: list[int] = []
    seen: set[int] = set()
    current: Optional[int] = workcenter_id
    while current is not None:
        if current in seen:
            break
        seen.add(current)
        chain.append(current)
        wc = db.get(M.Workcenter, current)
        current = wc.parent_id if wc is not None else None
    return chain


def has_permission(
    db: Session,
    membership_id: int,
    code: str,
    *,
    workcenter_id: Optional[int] = None,
) -> bool:
    """Return True iff ``membership_id`` has ``code`` for the given scope.

    * Org-wide grants always apply.
    * Workcenter-scoped grants apply when the requested workcenter is
      the granted workcenter or one of its descendants. Implemented by
      walking *up* from the requested workcenter to its ancestors and
      checking each grant point.
    * If ``workcenter_id`` is None, only org-wide grants count. Routes
      that don't take a workcenter param work this way — only an
      org-wide grant of the perm authorizes them.

    Unknown ``code`` raises immediately; the catalog is closed and a
    typo in route gating must surface loudly.
    """
    if not is_known_permission(code):
        raise ValueError(f"unknown permission code: {code!r}")

    eff = load_effective_permissions(db, membership_id)
    if code in eff.org_wide:
        return True
    if workcenter_id is None:
        return False

    for ancestor in workcenter_ancestors(db, workcenter_id):
        if code in eff.by_workcenter.get(ancestor, frozenset()):
            return True
    return False


def membership_has_role_template(
    db: Session, membership_id: int, template_slug: str
) -> bool:
    """Check whether a membership holds a role minted from a given seed.

    Used for invariants like "only an org_owner can transfer ownership".
    Renaming the local copy of the role doesn't affect the check —
    ``template_slug`` is set at seed time and never edited.
    """
    return (
        db.execute(
            select(M.MembershipRole.id)
            .join(M.Role, M.Role.id == M.MembershipRole.role_id)
            .where(
                M.MembershipRole.membership_id == membership_id,
                M.Role.template_slug == template_slug,
                M.MembershipRole.workcenter_id.is_(None),
            )
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


# ---------------------------------------------------------------------------
# FastAPI dependency factory
# ---------------------------------------------------------------------------


def _resolve_workcenter_id(request: Request, param: str) -> Optional[int]:
    """Pull the workcenter id from the request, in priority order.

    Order: path params → query params → already-parsed form. We don't
    re-read the request body; the CSRF middleware has already parsed
    the form for state-changing methods, leaving it on
    ``request._form``. GET routes shouldn't be workcenter-scoped via a
    body parameter, so falling back to the form is just a convenience
    for POSTs that opt into it.
    """
    if param in request.path_params:
        raw = request.path_params[param]
    elif param in request.query_params:
        raw = request.query_params[param]
    else:
        form = getattr(request, "_form", None)
        raw = form.get(param) if form is not None else None
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        # An unparseable workcenter id from the URL is the user's fault;
        # fall through to the permission check, which will deny because
        # there's no scope to match.
        return None


def require(
    permission: Permission,
    *,
    workcenter_param: Optional[str] = None,
) -> Callable:
    """Build a FastAPI dependency that gates a route on ``permission``.

    The returned callable is suitable for ``Depends(require(P_FOO))``.
    On failure it raises ``HTTPException(403)`` with a generic detail.

    ``workcenter_param`` opts the route into workcenter-scoped checking.
    Pass the name of the request parameter that carries the workcenter
    id (path, query, or form). If the route doesn't have one available,
    omit ``workcenter_param`` and the check uses org-wide grants only.
    """
    if not is_known_permission(permission.code):
        raise ValueError(
            f"require() received an unknown permission: {permission.code!r}"
        )

    code = permission.code

    def dependency(request: Request) -> None:
        # Single-tenant AppImage path: every gate is open. The
        # tenancy/auth middleware also short-circuits in this mode, so
        # there's nothing for us to check against anyway.
        if _single_tenant_mode():
            return

        membership: Optional[M.OrgMembership] = getattr(
            request.state, "membership", None
        )
        if membership is None:
            # Should not happen in practice — the SessionMiddleware
            # redirects unauthenticated traffic before routes get hit
            # — but the contract is that protected routes have a
            # membership in scope or 401 out before reaching the gate.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="not authenticated",
            )

        # Ad-hoc DB session for the permission check. Cheap enough not
        # to bother threading the request session through; the listener
        # filters everything by org via the active tenant_context.
        # Resolve SessionLocal at call time, not import time, so test
        # monkeypatching of ``app.db.SessionLocal`` actually takes
        # effect. The factory itself is cheap to look up.
        db = _db.SessionLocal()
        try:
            wc_id = (
                _resolve_workcenter_id(request, workcenter_param)
                if workcenter_param is not None
                else None
            )
            allowed = has_permission(db, membership.id, code, workcenter_id=wc_id)
        finally:
            db.close()

        if not allowed:
            # 403 not 404 here — the user is authenticated but doesn't
            # have the perm. Phase 6's sensitive-info posture might
            # change this for resources where existence itself is
            # sensitive; for ops data 403 is the right signal so the UI
            # can show a "you don't have permission" message instead of
            # a generic dead end.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="permission denied",
            )

    # Give the dependency a meaningful name for FastAPI's debug introspection.
    dependency.__name__ = f"require_{code.replace('.', '_')}"
    return dependency


def require_any(
    permissions: Sequence[Permission],
    *,
    workcenter_param: Optional[str] = None,
) -> Callable:
    """Variant that passes if *any* of the listed permissions check out.

    Use sparingly — most routes should declare the single permission
    that authorises them. This helper exists for handful of pages that
    legitimately serve more than one role's perms (e.g. the dashboard
    is fine for both viewers and admins).
    """
    if not permissions:
        raise ValueError("require_any() needs at least one permission")
    for p in permissions:
        if not is_known_permission(p.code):
            raise ValueError(f"require_any: unknown perm {p.code!r}")

    codes = tuple(p.code for p in permissions)

    def dependency(request: Request) -> None:
        if _single_tenant_mode():
            return
        membership: Optional[M.OrgMembership] = getattr(
            request.state, "membership", None
        )
        if membership is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="not authenticated",
            )

        db = _db.SessionLocal()
        try:
            wc_id = (
                _resolve_workcenter_id(request, workcenter_param)
                if workcenter_param is not None
                else None
            )
            for code in codes:
                if has_permission(db, membership.id, code, workcenter_id=wc_id):
                    return
        finally:
            db.close()

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="permission denied",
        )

    dependency.__name__ = "require_any_" + "_".join(c.replace(".", "_") for c in codes)
    return dependency
