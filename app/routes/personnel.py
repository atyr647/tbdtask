from datetime import date
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..db import SessionLocal
from .. import models as M
from ..services import effective as eff
from ..templating import render

router = APIRouter(prefix="/personnel")

ROSTER_STATUS_VALUES = ("active", "prd_pending", "departed", "dropped")
DUTY_SECTIONS = (1, 2, 3, 4, 5, 6)


def _current(person: M.Person):
    cur_rate = next((r for r in person.rates if r.valid_to is None), None)
    cur_ds = next((d for d in person.duty_sections if d.valid_to is None), None)
    cur_prd = next((p for p in person.prds if p.valid_to is None), None)
    cur_status = next((s for s in person.roster_statuses if s.valid_to is None), None)
    cur_dl = next((d for d in person.drivers_licenses if d.valid_to is None), None)
    return {
        "rate": cur_rate, "duty_section": cur_ds, "prd": cur_prd,
        "status": cur_status, "drivers_license": cur_dl,
    }


def _group_for(rate: Optional[str], paygrade: Optional[str]) -> str:
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


PAYGRADE_BY_RATE = {
    "CWO2": "W-2", "CWO3": "W-3", "CWO4": "W-4", "CWO5": "W-5",
    "ENS": "O-1", "LTJG": "O-2", "LT": "O-3", "LCDR": "O-4",
    "BMC": "E-7", "BMC(Sel)": "E-6", "BMCS": "E-8", "BMCM": "E-9",
    "ENC": "E-7", "CMC": "E-7", "QMC": "E-7", "ETC": "E-7", "GMC": "E-7",
    "BM1": "E-6", "EN1": "E-6", "CM1": "E-6", "QM1": "E-6",
    "ET1": "E-6", "GM1": "E-6", "MM1": "E-6", "IT1": "E-6",
    "BM2": "E-5", "EN2": "E-5", "CM2": "E-5", "GM2": "E-5",
    "ET2": "E-5", "MM2": "E-5", "QM2": "E-5", "IT2": "E-5",
    "BM3": "E-4", "EN3": "E-4", "CM3": "E-4", "GM3": "E-4",
    "ET3": "E-4", "MM3": "E-4", "QM3": "E-4", "IT3": "E-4",
    "SN": "E-3", "BMSN": "E-3", "ENFN": "E-3", "CMCN": "E-3",
    "ITSN": "E-3", "GMSN": "E-3", "MMFN": "E-3", "QMSN": "E-3",
    "SA": "E-2", "FA": "E-2", "CN": "E-2",
    "SR": "E-1", "FR": "E-1", "BMSR": "E-1", "CMCR": "E-1",
    "ENFR": "E-1", "ITSR": "E-1", "GMSR": "E-1",
}


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
            cur = _current(p)
            rate = cur["rate"].rate if cur["rate"] else None
            paygrade = cur["rate"].paygrade if cur["rate"] else None
            rows.append({
                "id": p.id,
                "display": p.full_display,
                "rate": rate,
                "paygrade": paygrade,
                "duty_section": cur["duty_section"].duty_section if cur["duty_section"] else None,
                "status": cur["status"].status if cur["status"] else None,
                "group": _group_for(rate, paygrade),
                "notes": p.notes,
            })
    groups = ["Khakis", "E6", "E5", "Junior", "Other"]
    grouped = {g: [r for r in rows if r["group"] == g] for g in groups}
    return render(request, "personnel/list.html", grouped=grouped, total=len(rows))


@router.get("/new")
def new_person_form(request: Request):
    return render(
        request,
        "personnel/new.html",
        roster_statuses=ROSTER_STATUS_VALUES,
        duty_sections=DUTY_SECTIONS,
    )


@router.post("")
def create_person(
    last_name: str = Form(...),
    first_name: Optional[str] = Form(None),
    rate: Optional[str] = Form(None),
    duty_section: Optional[int] = Form(None),
    prd_date: Optional[str] = Form(None),
    has_drivers_license: Optional[str] = Form(None),
    drivers_license_expires: Optional[str] = Form(None),
    roster_status: str = Form("active"),
    notes: Optional[str] = Form(None),
):
    today = date.today()
    with SessionLocal() as s:
        full_display = f"{rate} {last_name}".strip() if rate else last_name
        last_pos = s.scalar(
            select(M.Person.display_order).order_by(M.Person.display_order.desc()).limit(1)
        ) or 0
        p = M.Person(
            last_name=last_name.strip(),
            first_name=(first_name or None) and first_name.strip(),
            full_display=full_display.strip(),
            notes=(notes or None),
            display_order=last_pos + 1,
        )
        s.add(p)
        s.flush()
        if rate:
            s.add(M.PersonRate(
                person_id=p.id, rate=rate.strip(),
                paygrade=PAYGRADE_BY_RATE.get(rate.strip()),
                valid_from=today,
            ))
        if duty_section:
            s.add(M.PersonDutySection(
                person_id=p.id, duty_section=int(duty_section), valid_from=today,
            ))
        if prd_date:
            s.add(M.PersonPrd(
                person_id=p.id,
                prd_date=date.fromisoformat(prd_date),
                change_reason="initial",
                valid_from=today,
            ))
        s.add(M.PersonRosterStatus(
            person_id=p.id, status=roster_status, valid_from=today,
        ))
        s.add(M.PersonDriversLicense(
            person_id=p.id,
            has_license=bool(has_drivers_license),
            expires_on=date.fromisoformat(drivers_license_expires) if drivers_license_expires else None,
            valid_from=today,
        ))
        s.commit()
        new_id = p.id
    return RedirectResponse(f"/personnel/{new_id}", status_code=303)


@router.get("/{person_id}")
def show_person(person_id: int, request: Request):
    with SessionLocal() as s:
        p = s.get(M.Person, person_id)
        if not p:
            raise HTTPException(404, "person not found")
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
            ).all()
        )
        absences = list(
            s.scalars(
                select(M.Absence)
                .where(M.Absence.person_id == p.id, M.Absence.active == True)  # noqa: E712
                .order_by(M.Absence.start_date.desc())
            ).all()
        )
        # Refresh detached relationships explicitly, then close.
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


@router.get("/{person_id}/edit")
def edit_person_form(person_id: int, request: Request):
    with SessionLocal() as s:
        p = s.get(M.Person, person_id)
        if not p:
            raise HTTPException(404, "person not found")
        cur = _current(p)
    return render(
        request,
        "personnel/edit.html",
        person=p,
        cur=cur,
        roster_statuses=ROSTER_STATUS_VALUES,
        duty_sections=DUTY_SECTIONS,
    )


@router.post("/{person_id}")
def update_person(
    person_id: int,
    last_name: str = Form(...),
    first_name: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    rate: Optional[str] = Form(None),
    duty_section: Optional[int] = Form(None),
    prd_date: Optional[str] = Form(None),
    prd_reason: str = Form("correction"),
    has_drivers_license: Optional[str] = Form(None),
    drivers_license_expires: Optional[str] = Form(None),
    roster_status: str = Form("active"),
    effective_date: Optional[str] = Form(None),
):
    eff_date = date.fromisoformat(effective_date) if effective_date else date.today()
    with SessionLocal() as s:
        p = s.get(M.Person, person_id)
        if not p:
            raise HTTPException(404, "person not found")
        p.last_name = last_name.strip()
        p.first_name = (first_name or None) and first_name.strip()
        p.notes = (notes or None)
        # Recompute full_display from current rate (may have just changed below).
        new_rate = rate.strip() if rate else None
        p.full_display = (f"{new_rate} {p.last_name}".strip() if new_rate else p.last_name)

        if new_rate:
            eff.set_new_value(
                s, M.PersonRate, person_id=p.id, effective_date=eff_date,
                fields={"rate": new_rate, "paygrade": PAYGRADE_BY_RATE.get(new_rate)},
                no_op_if_unchanged=("rate", "paygrade"),
            )
        if duty_section:
            eff.set_new_value(
                s, M.PersonDutySection, person_id=p.id, effective_date=eff_date,
                fields={"duty_section": int(duty_section)},
                no_op_if_unchanged=("duty_section",),
            )
        if prd_date:
            eff.set_new_value(
                s, M.PersonPrd, person_id=p.id, effective_date=eff_date,
                fields={
                    "prd_date": date.fromisoformat(prd_date),
                    "change_reason": prd_reason,
                },
                no_op_if_unchanged=("prd_date",),
            )
        eff.set_new_value(
            s, M.PersonRosterStatus, person_id=p.id, effective_date=eff_date,
            fields={"status": roster_status},
            no_op_if_unchanged=("status",),
        )
        eff.set_new_value(
            s, M.PersonDriversLicense, person_id=p.id, effective_date=eff_date,
            fields={
                "has_license": bool(has_drivers_license),
                "expires_on": date.fromisoformat(drivers_license_expires) if drivers_license_expires else None,
            },
            no_op_if_unchanged=("has_license", "expires_on"),
        )
        s.commit()
    return RedirectResponse(f"/personnel/{person_id}", status_code=303)


@router.post("/{person_id}/archive")
def archive_person(person_id: int, reason: str = Form("")):
    with SessionLocal() as s:
        p = s.get(M.Person, person_id)
        if not p:
            raise HTTPException(404, "person not found")
        p.active = False
        from datetime import datetime as _dt
        p.archived_at = _dt.now()
        p.archived_reason = reason or None
        eff.set_new_value(
            s, M.PersonRosterStatus, person_id=p.id, effective_date=date.today(),
            fields={"status": "departed"},
            no_op_if_unchanged=("status",),
        )
        s.commit()
    return RedirectResponse("/personnel", status_code=303)
