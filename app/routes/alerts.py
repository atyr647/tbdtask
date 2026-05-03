from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from ..auth.authorization import require
from ..auth.permissions import (
    P_ALERTS_ACT,
    P_ALERTS_TRIAGE,
    P_ALERTS_VIEW,
    P_PERSONNEL_ARCHIVE,
)
from ..db import SessionLocal
from .. import models as M
from ..services import alerts as alerts_service
from ..services import effective as eff
from ..templating import render

router = APIRouter()


# Internal type slug -> human-readable label shown on the alerts list.
TYPE_LABELS = {
    "prd_orders_window": "Departure planning window",
    "prd_2mo": "Departure in ~2 months",
    "prd_1mo": "Departure in ~1 month",
    "prd_weekly_in_month": "Departure this week",
    "prd_passed": "Departure date passed",
    "qual_expiring": "Qualification expiring soon",
    "qual_expired": "Qualification expired",
    "worklist_carry_over_pending": "Carry-over pending",
}


def _label(slug: str) -> str:
    return TYPE_LABELS.get(slug, slug.replace("_", " ").capitalize())


@router.get("/alerts")
def list_alerts(
    request: Request,
    show: str = "active",
    _: None = Depends(require(P_ALERTS_VIEW)),
):
    with SessionLocal() as s:
        alerts_service.recompute(s)
        s.commit()
        if show == "history":
            rows = list(
                s.scalars(
                    select(M.Alert).order_by(M.Alert.created_at.desc()).limit(200)
                ).all()
            )
        else:
            rows = alerts_service.active_alerts(s)
        # Resolve person names and worklist names while in session.
        resolved = []
        for a in rows:
            person_label = None
            if a.person_id:
                p = s.get(M.Person, a.person_id)
                person_label = p.full_display if p else None
            wl_id = (a.payload or {}).get("worklist_id")
            wl_label = None
            if wl_id:
                wl = s.get(M.Worklist, wl_id)
                wl_label = wl.name if wl else None
            resolved.append(
                {
                    "alert": a,
                    "person_label": person_label,
                    "worklist_label": wl_label,
                    "type_label": _label(a.alert_type),
                }
            )
    return render(request, "alerts/list.html", items=resolved, show=show)


@router.post("/alerts/{alert_id}/dismiss")
def dismiss_alert(alert_id: int, _: None = Depends(require(P_ALERTS_TRIAGE))):
    with SessionLocal() as s:
        a = s.get(M.Alert, alert_id)
        if not a:
            raise HTTPException(404)
        a.dismissed_at = datetime.now()
        s.commit()
    return RedirectResponse("/alerts", status_code=303)


@router.post("/alerts/{alert_id}/resolve")
def resolve_alert(
    alert_id: int,
    note: Optional[str] = Form(None),
    _: None = Depends(require(P_ALERTS_TRIAGE)),
):
    """Mark an alert as resolved. Optional note is appended to the alert's
    notes field so the audit trail explains why."""
    with SessionLocal() as s:
        a = s.get(M.Alert, alert_id)
        if not a:
            raise HTTPException(404)
        a.resolved_at = datetime.now()
        if note:
            existing = (a.notes or "").rstrip()
            a.notes = f"{existing}\n{note.strip()}" if existing else note.strip()
        s.commit()
    return RedirectResponse("/alerts", status_code=303)


@router.post("/alerts/{alert_id}/snooze")
def snooze_alert(
    alert_id: int,
    until: str = Form(...),
    _: None = Depends(require(P_ALERTS_TRIAGE)),
):
    """Push an alert out until a chosen date. The alert disappears from the
    active queue and reappears on/after that date."""
    try:
        until_date = date.fromisoformat(until)
    except ValueError:
        raise HTTPException(400, "expected YYYY-MM-DD for until")
    with SessionLocal() as s:
        a = s.get(M.Alert, alert_id)
        if not a:
            raise HTTPException(404)
        a.snoozed_until = until_date
        s.commit()
    return RedirectResponse("/alerts", status_code=303)


@router.post("/alerts/{alert_id}/extend-prd")
def extend_prd(
    alert_id: int,
    new_date: Optional[str] = Form(None),
    days: Optional[int] = Form(None),
    _: None = Depends(require(P_ALERTS_ACT)),
):
    """Update the linked person's planned departure date. Caller supplies
    either ``new_date`` (an absolute YYYY-MM-DD) or ``days`` (relative offset
    from the current date). Closes the prior row and inserts a new
    effective-dated row tagged with change_reason='extension'."""
    with SessionLocal() as s:
        a = s.get(M.Alert, alert_id)
        if not a or not a.person_id:
            raise HTTPException(404)
        if new_date:
            try:
                new_prd = date.fromisoformat(new_date)
            except ValueError:
                raise HTTPException(400, "expected YYYY-MM-DD for new_date")
        else:
            d = max(1, min(int(days or 180), 365 * 3))
            cur = eff.current_row(s, M.PersonPrd, a.person_id)
            base = cur.prd_date if cur else date.today()
            new_prd = base + timedelta(days=d)
        eff.set_new_value(
            s,
            M.PersonPrd,
            person_id=a.person_id,
            effective_date=date.today(),
            fields={
                "prd_date": new_prd,
                "change_reason": "extension",
                "note": f"Updated from alert #{alert_id}",
            },
            no_op_if_unchanged=("prd_date",),
        )
        a.resolved_at = datetime.now()
        a.notes = (a.notes or "") + f"\nDeparture date updated to {new_prd.isoformat()}"
        s.flush()
        alerts_service.recompute(s)
        s.commit()
    return RedirectResponse("/alerts", status_code=303)


@router.post("/alerts/{alert_id}/archive-person")
def archive_person_from_alert(
    alert_id: int,
    reason: str = Form("Departure date passed"),
    # archiving a person via this action is more sensitive than mere
    # alert triage; require both alerts.act AND personnel.archive.
    _act: None = Depends(require(P_ALERTS_ACT)),
    _archive: None = Depends(require(P_PERSONNEL_ARCHIVE)),
):
    with SessionLocal() as s:
        a = s.get(M.Alert, alert_id)
        if not a or not a.person_id:
            raise HTTPException(404)
        p = s.get(M.Person, a.person_id)
        if not p:
            raise HTTPException(404)
        p.active = False
        p.archived_at = datetime.now()
        p.archived_reason = reason
        eff.set_new_value(
            s,
            M.PersonRosterStatus,
            person_id=p.id,
            effective_date=date.today(),
            fields={"status": "departed"},
            no_op_if_unchanged=("status",),
        )
        a.resolved_at = datetime.now()
        a.notes = (a.notes or "") + f"\nPersonnel archived: {reason}"
        s.flush()
        alerts_service.recompute(s)
        s.commit()
    return RedirectResponse("/alerts", status_code=303)
