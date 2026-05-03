from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..auth.authorization import require
from ..auth.permissions import P_PERSONNEL_VIEW
from ..db import SessionLocal
from .. import models as M
from ..services.availability import get_day_report
from ..services.recurrence import dates_in_window
from ..templating import render

router = APIRouter()


@dataclass
class DayTask:
    name: str
    category: Optional[str]
    assignees: list[str]
    poic_label: Optional[str]
    status: str
    source: str  # "scheduled" | "recurring"
    instance_id: Optional[int]
    template_id: Optional[int]


@router.get("/today")
def today(request: Request, _: None = Depends(require(P_PERSONNEL_VIEW))):
    return _day_view(request, date.today())


@router.get("/day/{day_iso}")
def day(day_iso: str, request: Request, _: None = Depends(require(P_PERSONNEL_VIEW))):
    try:
        d = date.fromisoformat(day_iso)
    except ValueError:
        raise HTTPException(400, "expected ISO date YYYY-MM-DD")
    return _day_view(request, d)


def _day_view(request: Request, on_date: date):
    with SessionLocal() as s:
        report = get_day_report(s, on_date)
        absent_rows = [r for r in report.rows if r.absence is not None]
        present_rows = [r for r in report.rows if r.absence is None]
        # Tasks scheduled for this date on any active worklist.
        scheduled = list(s.scalars(
            select(M.TaskInstance)
            .where(
                M.TaskInstance.active == True,  # noqa: E712
                M.TaskInstance.scheduled_date == on_date,
            )
            .options(
                selectinload(M.TaskInstance.assignments).selectinload(M.TaskAssignment.person),
                selectinload(M.TaskInstance.category),
            )
            .order_by(M.TaskInstance.id)
        ).all())
        scheduled_template_ids = {i.template_id for i in scheduled if i.template_id is not None}
        tasks: list[DayTask] = []
        for inst in scheduled:
            assignments = [a for a in inst.assignments if a.active]
            assignees = [
                (a.person.full_display if a.person else (a.external_poic_name or "(ext)"))
                for a in assignments
            ]
            poic_label = None
            for a in assignments:
                if a.is_poic:
                    poic_label = a.person.full_display if a.person else a.external_poic_name
                    break
            tasks.append(DayTask(
                name=inst.name,
                category=(inst.category.name if inst.category else None),
                assignees=assignees,
                poic_label=poic_label,
                status=inst.status,
                source="scheduled",
                instance_id=inst.id,
                template_id=inst.template_id,
            ))

        # Recurring templates that fire today but haven't been materialized
        # to a worklist yet — surface them so the day overview reflects
        # daily routines like morning quarters even on weeks with no
        # current worklist.
        templates = list(s.scalars(
            select(M.TaskTemplate)
            .where(
                M.TaskTemplate.active == True,  # noqa: E712
                M.TaskTemplate.recurrence_rule.is_not(None),
            )
            .options(selectinload(M.TaskTemplate.category))
        ).all())
        for t in templates:
            if t.id in scheduled_template_ids:
                continue
            if not dates_in_window(t.recurrence_rule or {}, on_date, days=1):
                continue
            tasks.append(DayTask(
                name=t.name,
                category=(t.category.name if t.category else None),
                assignees=[],
                poic_label=None,
                status="recurring",
                source="recurring",
                instance_id=None,
                template_id=t.id,
            ))
    return render(
        request, "today/day.html",
        report=report,
        absent_rows=absent_rows,
        present_rows=present_rows,
        tasks=tasks,
        prev_day=(on_date - timedelta(days=1)).isoformat(),
        next_day=(on_date + timedelta(days=1)).isoformat(),
        today_iso=date.today().isoformat(),
    )
