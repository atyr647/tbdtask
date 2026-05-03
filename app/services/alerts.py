"""
Alert engine.

Computes the current alert set on demand from data state. Alerts are
deduplicated by (alert_type, person_id, payload-key) so reruns just
update timestamps rather than spamming the queue. Operators dismiss or
resolve alerts; resolved ones disappear from the active queue but stay
in history.

Alert types:

* prd_2mo / prd_1mo / prd_weekly_in_month / prd_passed
* qual_expiring / qual_expired
* worklist_carry_over_pending
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import select, and_
from sqlalchemy.orm import Session

from .. import models as M


# How far ahead to surface qualification expirations.
QUAL_EXPIRING_WINDOW = 30


def _ensure_alert(
    session: Session,
    alert_type: str,
    *,
    severity: str = "info",
    person_id: Optional[int] = None,
    task_instance_id: Optional[int] = None,
    payload: Optional[dict] = None,
) -> M.Alert:
    payload_key = (payload or {}).get("key")
    stmt = select(M.Alert).where(
        M.Alert.alert_type == alert_type,
        M.Alert.resolved_at.is_(None),
        M.Alert.dismissed_at.is_(None),
    )
    if person_id is not None:
        stmt = stmt.where(M.Alert.person_id == person_id)
    if task_instance_id is not None:
        stmt = stmt.where(M.Alert.task_instance_id == task_instance_id)
    candidates = list(session.scalars(stmt).all())
    for a in candidates:
        if (a.payload or {}).get("key") == payload_key:
            a.payload = payload
            return a

    # Derive org_id from the person or task_instance.
    org_id = None
    if person_id is not None:
        person = session.get(M.Person, person_id)
        if person is not None:
            org_id = person.org_id
    if org_id is None and task_instance_id is not None:
        inst = session.get(M.TaskInstance, task_instance_id)
        if inst is not None:
            org_id = inst.org_id
    if org_id is None and payload and "worklist_id" in payload:
        wl = session.get(M.Worklist, payload["worklist_id"])
        if wl is not None:
            org_id = wl.org_id

    alert = M.Alert(
        alert_type=alert_type, severity=severity, person_id=person_id,
        task_instance_id=task_instance_id, payload=payload,
        org_id=org_id,
    )
    session.add(alert)
    return alert


def _resolve_stale(session: Session, alert_type: str, keep_keys: set):
    rows = session.scalars(
        select(M.Alert).where(
            M.Alert.alert_type == alert_type,
            M.Alert.resolved_at.is_(None),
            M.Alert.dismissed_at.is_(None),
        )
    ).all()
    now = datetime.now()
    for a in rows:
        key = (a.payload or {}).get("key")
        if key not in keep_keys:
            a.resolved_at = now


def _current_prd(p: M.Person) -> Optional[M.PersonPrd]:
    for r in p.prds:
        if r.valid_to is None:
            return r
    return None


def recompute(session: Session, *, today: Optional[date] = None) -> dict[str, int]:
    today = today or date.today()
    counts: dict[str, int] = {}

    # Planned departure windows --------------------------------------
    persons = list(
        session.scalars(
            select(M.Person).where(M.Person.active == True)  # noqa: E712
        ).all()
    )
    keep_orders: set = set()
    keep_2mo: set = set()
    keep_1mo: set = set()
    keep_weekly: set = set()
    keep_passed: set = set()
    for p in persons:
        prd = _current_prd(p)
        if not prd:
            continue
        delta = (prd.prd_date - today).days
        key = f"{p.id}:{prd.prd_date.isoformat()}"
        if delta < 0:
            keep_passed.add(key)
            _ensure_alert(
                session, "prd_passed", severity="urgent", person_id=p.id,
                payload={"key": key, "prd": prd.prd_date.isoformat(), "days": delta},
            )
        elif delta <= 7:
            keep_weekly.add(key)
            _ensure_alert(
                session, "prd_weekly_in_month", severity="urgent", person_id=p.id,
                payload={"key": key, "prd": prd.prd_date.isoformat(), "days": delta},
            )
        elif delta <= 30:
            keep_1mo.add(key)
            _ensure_alert(
                session, "prd_1mo", severity="warn", person_id=p.id,
                payload={"key": key, "prd": prd.prd_date.isoformat(), "days": delta},
            )
        elif delta <= 60:
            keep_2mo.add(key)
            _ensure_alert(
                session, "prd_2mo", severity="info", person_id=p.id,
                payload={"key": key, "prd": prd.prd_date.isoformat(), "days": delta},
            )
        elif delta <= 365:
            keep_orders.add(key)
            _ensure_alert(
                session, "prd_orders_window", severity="warn", person_id=p.id,
                payload={"key": key, "prd": prd.prd_date.isoformat(), "days": delta},
            )
    _resolve_stale(session, "prd_orders_window", keep_orders)
    _resolve_stale(session, "prd_2mo", keep_2mo)
    _resolve_stale(session, "prd_1mo", keep_1mo)
    _resolve_stale(session, "prd_weekly_in_month", keep_weekly)
    _resolve_stale(session, "prd_passed", keep_passed)
    counts["prd_orders_window"] = len(keep_orders)
    counts["prd_2mo"] = len(keep_2mo)
    counts["prd_1mo"] = len(keep_1mo)
    counts["prd_weekly_in_month"] = len(keep_weekly)
    counts["prd_passed"] = len(keep_passed)

    # Worklist carry-overs --------------------------------------------
    pending = session.execute(
        select(
            M.TaskInstance.worklist_id,
            M.TaskInstance.id,
        ).where(
            M.TaskInstance.active == True,  # noqa: E712
            M.TaskInstance.status.in_(("open", "in_progress")),
        )
    ).all()
    by_wl: dict[int, list[int]] = {}
    for wl_id, iid in pending:
        if wl_id is None:
            continue
        by_wl.setdefault(wl_id, []).append(iid)
    open_worklists = list(
        session.scalars(
            select(M.Worklist).where(
                M.Worklist.active == True,  # noqa: E712
                M.Worklist.locked == False,  # noqa: E712
                M.Worklist.week_starting > today - timedelta(days=14),
            )
        ).all()
    )
    keep_carry: set = set()
    for wl in open_worklists:
        # Count instances from earlier weeks still open.
        stale_ids = []
        for source_wl_id, ids in by_wl.items():
            if source_wl_id == wl.id:
                continue
            source = session.get(M.Worklist, source_wl_id)
            if not source or source.week_starting >= wl.week_starting:
                continue
            stale_ids.extend(ids)
        if not stale_ids:
            continue
        key = f"{wl.id}:{len(stale_ids)}"
        keep_carry.add(key)
        _ensure_alert(
            session, "worklist_carry_over_pending", severity="warn",
            payload={"key": key, "worklist_id": wl.id, "count": len(stale_ids)},
        )
    _resolve_stale(session, "worklist_carry_over_pending", keep_carry)
    counts["worklist_carry_over_pending"] = len(keep_carry)

    return counts


def active_alerts(session: Session) -> list[M.Alert]:
    today = date.today()
    return list(
        session.scalars(
            select(M.Alert)
            .where(
                M.Alert.dismissed_at.is_(None),
                M.Alert.resolved_at.is_(None),
                # Snoozed alerts disappear from the active queue until the
                # snooze date passes, then they reappear.
                (M.Alert.snoozed_until.is_(None) | (M.Alert.snoozed_until <= today)),
            )
            .order_by(
                _severity_order(M.Alert.severity).desc(),
                M.Alert.created_at.desc(),
            )
        ).all()
    )


def _severity_order(col):
    from sqlalchemy import case
    return case(
        {"urgent": 3, "warn": 2, "info": 1},
        value=col,
        else_=0,
    )
