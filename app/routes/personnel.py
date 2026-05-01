from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..db import SessionLocal
from .. import models as M
from ..templating import render

router = APIRouter(prefix="/personnel")


def _current_rate(person: M.Person) -> str | None:
    for r in person.rates:
        if r.valid_to is None:
            return r.rate
    return None


def _current_status(person: M.Person) -> str | None:
    for s in person.roster_statuses:
        if s.valid_to is None:
            return s.status
    return None


def _group_for(rate: str | None, paygrade: str | None) -> str:
    if not rate:
        return "Other"
    if rate.startswith(("CWO", "ENS", "LT", "LCDR")):
        return "Khakis"
    if rate.endswith("(Sel)"):
        return "Khakis"
    if paygrade in ("E-7", "E-8", "E-9"):
        return "Khakis"
    if paygrade == "E-6":
        return "E6"
    if paygrade == "E-5":
        return "E5"
    return "Junior"


@router.get("")
def list_personnel(request: Request):
    with SessionLocal() as s:
        people = s.scalars(
            select(M.Person)
            .where(M.Person.active == True)  # noqa: E712
            .options(
                selectinload(M.Person.rates),
                selectinload(M.Person.roster_statuses),
                selectinload(M.Person.duty_sections),
            )
            .order_by(M.Person.display_order)
        ).all()
        rows = []
        for p in people:
            current_rate = _current_rate(p)
            paygrade = next((r.paygrade for r in p.rates if r.valid_to is None), None)
            current_ds = next(
                (d.duty_section for d in p.duty_sections if d.valid_to is None), None
            )
            rows.append({
                "id": p.id,
                "display": p.full_display,
                "rate": current_rate,
                "paygrade": paygrade,
                "duty_section": current_ds,
                "status": _current_status(p),
                "group": _group_for(current_rate, paygrade),
                "notes": p.notes,
            })
    groups = ["Khakis", "E6", "E5", "Junior", "Other"]
    grouped = {g: [r for r in rows if r["group"] == g] for g in groups}
    return render(request, "personnel/list.html", grouped=grouped, total=len(rows))


@router.get("/{person_id}")
def show_person(person_id: int, request: Request):
    with SessionLocal() as s:
        p = s.get(M.Person, person_id)
        if not p:
            raise HTTPException(404, "person not found")
        # Touch relationships within the session.
        rates = list(p.rates)
        roster = list(p.roster_statuses)
        ds = list(p.duty_sections)
        prds = list(p.prds)
        dl = list(p.drivers_licenses)
        quals = (
            s.execute(
                select(M.PersonQual, M.Qualification)
                .join(M.Qualification, M.Qualification.id == M.PersonQual.qual_id)
                .where(M.PersonQual.person_id == p.id, M.PersonQual.valid_to.is_(None))
                .order_by(M.Qualification.display_order)
            )
            .all()
        )
        absences = list(
            s.scalars(
                select(M.Absence)
                .where(M.Absence.person_id == p.id, M.Absence.active == True)  # noqa: E712
                .order_by(M.Absence.start_date.desc())
            ).all()
        )
        return render(
            request,
            "personnel/show.html",
            person=p,
            rates=rates,
            roster=roster,
            duty_sections=ds,
            prds=prds,
            drivers_licenses=dl,
            quals=quals,
            absences=absences,
        )
