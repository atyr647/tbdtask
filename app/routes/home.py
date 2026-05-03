from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from ..auth.authorization import require
from ..auth.permissions import P_ORG_VIEW
from ..db import SessionLocal
from .. import models as M
from ..services import alerts as alerts_service
from ..services.availability import get_day_report
from ..routes.alerts import _label as _alert_label
from ..templating import render

router = APIRouter()


@router.get("/")
def home(request: Request, _: None = Depends(require(P_ORG_VIEW))):
    today = date.today()
    with SessionLocal() as s:
        alerts_service.recompute(s)
        s.commit()
        # Availability snapshot for today.
        report = get_day_report(s, today)
        out_today = [r for r in report.rows if r.absence is not None]

        # Active worklist (current week if one exists).
        monday = today - timedelta(days=today.weekday())
        current_wl = s.scalar(
            select(M.Worklist).where(
                M.Worklist.active == True,  # noqa: E712
                M.Worklist.week_starting == monday,
                M.Worklist.parent_id.is_(None),
            )
        )

        active = alerts_service.active_alerts(s)
        urgent = [a for a in active if a.severity == "urgent"]
        warn = [a for a in active if a.severity == "warn"]
        info = [a for a in active if a.severity == "info"]

        def _subject(a: M.Alert) -> str:
            p = a.payload or {}
            type_label = _alert_label(a.alert_type)
            if a.person_id:
                person = s.get(M.Person, a.person_id)
                name = person.full_display if person else f"#{a.person_id}"
                if a.alert_type.startswith("prd"):
                    return f"{name} — {type_label} ({p.get('prd')}, {p.get('days')}d)"
                if a.alert_type.startswith("qual"):
                    qual = p.get("qual_name") or ""
                    return f"{name} — {qual} {type_label.lower()}"
                return f"{name} — {type_label}"
            if a.alert_type == "worklist_carry_over_pending":
                wl = (
                    s.get(M.Worklist, p.get("worklist_id"))
                    if p.get("worklist_id")
                    else None
                )
                return f"{wl.name if wl else 'worklist'} — {p.get('count')} carry-over pending"
            return type_label

        banner_items = [
            {"alert": a, "label": _subject(a), "type_label": _alert_label(a.alert_type)}
            for a in (urgent + warn)[:8]
        ]

        # Per-code rollup for "out today by reason"
        by_code = report.by_code

    return render(
        request,
        "home.html",
        today=today,
        report=report,
        out_today=out_today,
        by_code=by_code,
        current_wl=current_wl,
        banner_items=banner_items,
        urgent_count=len(urgent),
        warn_count=len(warn),
        info_count=len(info),
        total_active=len(active),
        tomorrow=(today + timedelta(days=1)).isoformat(),
        next_monday=(
            today + timedelta(days=(7 - today.weekday()) % 7 or 7)
        ).isoformat(),
        prev_day=(today - timedelta(days=1)).isoformat(),
    )
