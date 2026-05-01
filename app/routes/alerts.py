from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from ..db import SessionLocal
from .. import models as M
from ..services import alerts as alerts_service
from ..templating import render

router = APIRouter()


@router.get("/alerts")
def list_alerts(request: Request, show: str = "active"):
    with SessionLocal() as s:
        alerts_service.recompute(s)
        s.commit()
        if show == "history":
            rows = list(
                s.scalars(
                    select(M.Alert)
                    .order_by(M.Alert.created_at.desc())
                    .limit(200)
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
            resolved.append({
                "alert": a, "person_label": person_label, "worklist_label": wl_label,
            })
    return render(request, "alerts/list.html", items=resolved, show=show)


@router.post("/alerts/{alert_id}/dismiss")
def dismiss_alert(alert_id: int):
    with SessionLocal() as s:
        a = s.get(M.Alert, alert_id)
        if not a:
            raise HTTPException(404)
        a.dismissed_at = datetime.now()
        s.commit()
    return RedirectResponse("/alerts", status_code=303)


@router.post("/alerts/{alert_id}/resolve")
def resolve_alert(alert_id: int):
    with SessionLocal() as s:
        a = s.get(M.Alert, alert_id)
        if not a:
            raise HTTPException(404)
        a.resolved_at = datetime.now()
        s.commit()
    return RedirectResponse("/alerts", status_code=303)
