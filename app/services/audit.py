"""Data-audit logging (Phase 4).

Provides a decorator ``@audit_write`` that wraps route handlers and records
every create/update/delete/archive operation on tenant-scoped tables. The
audit trail is immutable: rows are INSERT-only.

The audit system also powers the notification fan-out: certain event types
trigger in-app notifications to org admins and owners.
"""
from __future__ import annotations

import json
from datetime import datetime
from functools import wraps
from typing import Any, Callable, Optional

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as M

# Actions that trigger notifications to org admins.
_NOTIFY_ACTIONS = frozenset({
    "archive",
    "delete",
    "lock",
    "amend",
})


def _serialize(obj: Any) -> Any:
    """Make an ORM object or primitive JSON-serializable.

    For ORM instances, extracts column values into a dict. Dates become
    ISO strings; None stays None.
    """
    if obj is None:
        return None
    if hasattr(obj, "__table__"):
        result = {}
        for col in obj.__table__.columns:
            val = getattr(obj, col.name, None)
            if isinstance(val, datetime):
                val = val.isoformat()
            result[col.name] = val
        return result
    return obj


def record_audit_event(
    db: Session,
    *,
    action: str,
    table_name: str,
    row_id: int,
    org_id: int,
    membership_id: Optional[int] = None,
    before: Any = None,
    after: Any = None,
    detail: Optional[dict] = None,
) -> M.DataAuditEvent:
    """Insert an immutable audit event row.

    Returns the created event so callers can inspect the id if needed.
    """
    event = M.DataAuditEvent(
        org_id=org_id,
        action=action,
        table_name=table_name,
        row_id=row_id,
        actor_membership_id=membership_id,
        before_json=_serialize(before),
        after_json=_serialize(after),
        detail=detail,
    )
    db.add(event)
    return event


def audit_write(
    *,
    table_name: str,
    id_param: str = "id",
    action: str = "update",
    notify: bool = False,
):
    """Decorator that records an audit event after a successful route handler.

    Parameters
    ----------
    table_name : str
        The database table being modified (e.g. ``"persons"``).
    id_param : str
        The route parameter name that carries the row id (default: ``"id"``).
    action : str
        The audit action label (``"create"``, ``"update"``, ``"archive"``, etc.).
    notify : bool
        If True, the event also triggers a notification to org admins.

    Usage
    -----
    ```python
    @router.post("/{person_id}")
    @audit_write(table_name="persons", id_param="person_id", action="update")
    def update_person(person_id: int, ...):
        ...
    ```

    The decorator inspects the route's path parameters to find the row id,
    then records the event after the handler completes successfully. For
    create actions, the handler should return a RedirectResponse or the
    decorator will attempt to extract the new id from the response.
    """

    def decorator(fn: Callable):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            result = fn(*args, **kwargs)

            # Extract membership id from request state.
            request = kwargs.get("request")
            if request is None:
                for arg in args:
                    if isinstance(arg, Request):
                        request = arg
                        break

            membership_id = None
            if request and hasattr(request.state, "membership"):
                membership = getattr(request.state, "membership", None)
                if membership is not None:
                    membership_id = membership.id

            # Extract row id from path params or response.
            row_id = kwargs.get(id_param)
            if row_id is None and request:
                row_id = request.path_params.get(id_param)

            # For create actions, try to get the new id from the redirect.
            if row_id is None and action == "create":
                from fastapi.responses import RedirectResponse
                if isinstance(result, RedirectResponse):
                    # The redirect URL often contains the new id.
                    loc = result.headers.get("location", "")
                    # Try to extract the last path segment as an int.
                    parts = loc.rstrip("/").split("/")
                    if parts:
                        try:
                            row_id = int(parts[-1])
                        except ValueError:
                            pass

            if row_id is not None:
                # We don't have before/after snapshots here — the route
                # handler manages its own DB session. The audit event is
                # recorded with minimal info; richer snapshots require the
                # route to call ``record_audit_event`` directly.
                from ..db import SessionLocal
                with SessionLocal() as db:
                    # Resolve org_id from the membership.
                    resolved_org_id = None
                    if membership_id is not None:
                        memb = db.get(M.OrgMembership, membership_id)
                        if memb is not None:
                            resolved_org_id = memb.org_id

                    if resolved_org_id is None:
                        return  # Can't audit without an org context.

                    detail = {"route": request.url.path if request else None}
                    record_audit_event(
                        db,
                        action=action,
                        table_name=table_name,
                        row_id=int(row_id),
                        org_id=resolved_org_id,
                        membership_id=membership_id,
                        detail=detail,
                    )
                    db.commit()

                    # Notification fan-out.
                    if notify or action in _NOTIFY_ACTIONS:
                        _fan_out_notification(
                            db,
                            action=action,
                            table_name=table_name,
                            row_id=int(row_id),
                            org_id=resolved_org_id,
                            membership_id=membership_id,
                        )

            return result
        return wrapper
    return decorator


def _fan_out_notification(
    db: Session,
    *,
    action: str,
    table_name: str,
    row_id: int,
    org_id: int,
    membership_id: Optional[int],
) -> None:
    """Create in-app notifications for org admins/owners when a significant
    audit event occurs.

    The notification is a row in the ``alerts`` table with type ``audit_event``.
    Admins see these on the alerts page alongside operational alerts.
    """
    # Check if this action warrants a notification.
    if action not in _NOTIFY_ACTIONS:
        return

    # Create an audit alert visible to org admins.
    alert = M.Alert(
        org_id=org_id,
        alert_type="audit_event",
        severity="info",
        payload={
            "action": action,
            "table": table_name,
            "row_id": row_id,
            "actor_membership_id": membership_id,
        },
    )
    db.add(alert)
