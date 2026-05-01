from datetime import date

from fastapi import APIRouter, Request
from sqlalchemy import func, select

from ..db import SessionLocal
from .. import models as M
from ..services import alerts as alerts_service
from ..templating import render

router = APIRouter()


@router.get("/")
def home(request: Request):
    with SessionLocal() as s:
        alerts_service.recompute(s)
        s.commit()
        person_count = s.scalar(select(func.count()).select_from(M.Person).where(M.Person.active == True))  # noqa: E712
        qual_count = s.scalar(select(func.count()).select_from(M.Qualification).where(M.Qualification.active == True))  # noqa: E712
        absence_count = s.scalar(select(func.count()).select_from(M.Absence).where(M.Absence.active == True))  # noqa: E712
        worklist_count = s.scalar(select(func.count()).select_from(M.Worklist).where(M.Worklist.active == True))  # noqa: E712
        last_import = s.scalars(
            select(M.ImportBatch).order_by(M.ImportBatch.id.desc()).limit(1)
        ).first()
        active = alerts_service.active_alerts(s)
        urgent = [a for a in active if a.severity == "urgent"]
        warn = [a for a in active if a.severity == "warn"]
        info = [a for a in active if a.severity == "info"]
        # Resolve subject labels for the banner.
        def _label(a: M.Alert) -> str:
            p = a.payload or {}
            if a.person_id:
                person = s.get(M.Person, a.person_id)
                name = person.full_display if person else f"#{a.person_id}"
                if a.alert_type.startswith("prd"):
                    return f"{name} — PRD {p.get('prd')} ({p.get('days')}d)"
                if a.alert_type.startswith("qual"):
                    return f"{name} — {p.get('qual_name')} ({a.alert_type.replace('qual_', '')})"
                return name
            if a.alert_type == "worklist_carry_over_pending":
                wl = s.get(M.Worklist, p.get("worklist_id")) if p.get("worklist_id") else None
                return f"{wl.name if wl else 'worklist'} — {p.get('count')} carry-over pending"
            return a.alert_type
        banner_items = [{"alert": a, "label": _label(a)} for a in (urgent + warn)[:6]]
    return render(
        request,
        "home.html",
        person_count=person_count,
        qual_count=qual_count,
        absence_count=absence_count,
        worklist_count=worklist_count,
        last_import=last_import,
        today=date.today(),
        banner_items=banner_items,
        urgent_count=len(urgent),
        warn_count=len(warn),
        info_count=len(info),
        total_active=len(active),
    )
