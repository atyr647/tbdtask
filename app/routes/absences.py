from datetime import date, datetime, time, timedelta
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..db import SessionLocal
from .. import models as M
from ..services.absence_calendar import build_calendar
from ..templating import render

router = APIRouter()


def _codes(s):
    return list(s.scalars(
        select(M.AbsenceCode)
        .where(M.AbsenceCode.active == True)  # noqa: E712
        .order_by(M.AbsenceCode.display_order)
    ).all())


def _parse_time(value: Optional[str]) -> Optional[time]:
    if not value:
        return None
    try:
        return time.fromisoformat(value)
    except ValueError:
        return None


@router.get("/absences")
def list_absences(request: Request):
    today = date.today()
    with SessionLocal() as s:
        upcoming = list(
            s.scalars(
                select(M.Absence)
                .where(M.Absence.active == True, M.Absence.end_date >= today)  # noqa: E712
                .options(selectinload(M.Absence.person), selectinload(M.Absence.code))
                .order_by(M.Absence.start_date)
            ).all()
        )
        past = list(
            s.scalars(
                select(M.Absence)
                .where(M.Absence.active == True, M.Absence.end_date < today)  # noqa: E712
                .options(selectinload(M.Absence.person), selectinload(M.Absence.code))
                .order_by(M.Absence.start_date.desc())
                .limit(50)
            ).all()
        )
        return render(request, "absences/list.html", upcoming=upcoming, past=past)


@router.get("/absences/calendar")
def absence_calendar(
    request: Request,
    start: Optional[str] = None,
    days: int = 28,
):
    if days < 7 or days > 120:
        days = 28
    if start:
        try:
            start_date = date.fromisoformat(start)
        except ValueError:
            raise HTTPException(400, "expected ISO date for ?start=")
    else:
        # Anchor on the Monday of the current week so the grid aligns.
        today = date.today()
        start_date = today - timedelta(days=today.weekday())
    with SessionLocal() as s:
        view = build_calendar(s, start_date, days)
    prev_start = (start_date - timedelta(days=days)).isoformat()
    next_start = (start_date + timedelta(days=days)).isoformat()
    return render(
        request,
        "absences/calendar.html",
        view=view,
        days=days,
        prev_start=prev_start,
        next_start=next_start,
    )


@router.get("/personnel/{person_id}/absences/new")
def new_absence_form_for_person(person_id: int, request: Request):
    with SessionLocal() as s:
        p = s.get(M.Person, person_id)
        if not p:
            raise HTTPException(404)
        codes = _codes(s)
    return render(request, "absences/new.html", person=p, codes=codes, all_people=None)


@router.get("/absences/new")
def new_absence_form(request: Request):
    with SessionLocal() as s:
        codes = _codes(s)
        people = list(
            s.scalars(
                select(M.Person)
                .where(M.Person.active == True)  # noqa: E712
                .order_by(M.Person.display_order)
            ).all()
        )
    return render(request, "absences/new.html", person=None, codes=codes, all_people=people)


@router.post("/absences")
def create_absence(
    person_id: int = Form(...),
    code_id: int = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
    start_time: Optional[str] = Form(None),
    end_time: Optional[str] = Form(None),
    reason: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    with SessionLocal() as s:
        if not s.get(M.Person, person_id) or not s.get(M.AbsenceCode, code_id):
            raise HTTPException(404)
        sd = date.fromisoformat(start_date)
        ed = date.fromisoformat(end_date)
        if ed < sd:
            raise HTTPException(400, "end_date precedes start_date")
        a = M.Absence(
            person_id=person_id,
            code_id=code_id,
            start_date=sd,
            end_date=ed,
            start_time=_parse_time(start_time),
            end_time=_parse_time(end_time),
            reason=(reason or None),
            notes=(notes or None),
        )
        s.add(a)
        s.commit()
    return RedirectResponse(f"/personnel/{person_id}", status_code=303)


@router.get("/absences/{absence_id}/edit")
def edit_absence_form(absence_id: int, request: Request):
    with SessionLocal() as s:
        a = s.get(M.Absence, absence_id)
        if not a:
            raise HTTPException(404)
        p = s.get(M.Person, a.person_id)
        codes = _codes(s)
    return render(request, "absences/edit.html", absence=a, person=p, codes=codes)


@router.post("/absences/{absence_id}")
def update_absence(
    absence_id: int,
    code_id: int = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
    start_time: Optional[str] = Form(None),
    end_time: Optional[str] = Form(None),
    reason: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    with SessionLocal() as s:
        a = s.get(M.Absence, absence_id)
        if not a:
            raise HTTPException(404)
        sd = date.fromisoformat(start_date)
        ed = date.fromisoformat(end_date)
        if ed < sd:
            raise HTTPException(400, "end_date precedes start_date")
        a.code_id = code_id
        a.start_date = sd
        a.end_date = ed
        a.start_time = _parse_time(start_time)
        a.end_time = _parse_time(end_time)
        a.reason = (reason or None)
        a.notes = (notes or None)
        person_id = a.person_id
        s.commit()
    return RedirectResponse(f"/personnel/{person_id}", status_code=303)


@router.post("/absences/{absence_id}/archive")
def archive_absence(absence_id: int, reason: str = Form("")):
    with SessionLocal() as s:
        a = s.get(M.Absence, absence_id)
        if not a:
            raise HTTPException(404)
        a.active = False
        a.archived_at = datetime.now()
        a.archived_reason = reason or None
        person_id = a.person_id
        s.commit()
    return RedirectResponse(f"/personnel/{person_id}", status_code=303)
