from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..auth.authorization import require
from ..auth.permissions import (
    P_PERSONNEL_ARCHIVE,
    P_PERSONNEL_VIEW,
    P_PERSONNEL_WRITE,
)
from ..auth.sensitive_info import scan_text
from ..db import SessionLocal
from .. import models as M
from ..data import ranks as rank_catalog
from ..services import effective as eff
from ..templating import render

router = APIRouter(prefix="/personnel")

def _sensitive_warnings(*texts: Optional[str]) -> list[str]:
    """Return human-readable warning labels for any detected PII/CUI
    patterns across the given text fields."""
    seen: set[str] = set()
    warnings = []
    for t in texts:
        if not t:
            continue
        report = scan_text(t)
        for m in report.matches:
            if m.label not in seen:
                seen.add(m.label)
                warnings.append(m.label)
    return warnings

ROSTER_STATUS_VALUES = ("active", "departed")
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


_group_for = rank_catalog.group_for


def _paygrade_for(rate: Optional[str]) -> Optional[str]:
    return rank_catalog.paygrade_for(rate or "") if rate else None


def _current_status(person: M.Person) -> Optional[str]:
    for r in person.roster_statuses:
        if r.valid_to is None:
            return r.status
    return None


@router.get("")
def list_personnel(request: Request, _: None = Depends(require(P_PERSONNEL_VIEW))):
    with SessionLocal() as s:
        people = s.scalars(
            select(M.Person)
            .where(M.Person.active == True)  # noqa: E712
            .options(
                selectinload(M.Person.rates),
                selectinload(M.Person.duty_sections),
                selectinload(M.Person.roster_statuses),
            )
            .order_by(M.Person.display_order)
        ).all()
        # Active list excludes incoming personnel — they live on /incoming.
        rows = []
        for p in people:
            if _current_status(p) == "incoming":
                continue
            cur = _current(p)
            rate = cur["rate"].rate if cur["rate"] else None
            paygrade = cur["rate"].paygrade if cur["rate"] else None
            rows.append({
                "id": p.id,
                "rate": rate,
                "paygrade": paygrade,
                "last_name": p.last_name,
                "first_name": p.first_name,
                "position": p.position,
                "duty_section": cur["duty_section"].duty_section if cur["duty_section"] else None,
                "group": _group_for(rate, paygrade),
                "notes": p.notes,
            })
    groups = ["Leadership", "Senior", "Professional", "Associate", "Other"]
    grouped = {g: [r for r in rows if r["group"] == g] for g in groups}
    return render(request, "personnel/list.html", grouped=grouped, total=len(rows))


@router.get("/incoming")
def list_incoming(request: Request, _: None = Depends(require(P_PERSONNEL_VIEW))):
    with SessionLocal() as s:
        people = list(s.scalars(
            select(M.Person)
            .where(M.Person.active == True)  # noqa: E712
            .options(
                selectinload(M.Person.rates),
                selectinload(M.Person.roster_statuses),
                selectinload(M.Person.sponsor),
            )
            .order_by(M.Person.arrival_date.is_(None), M.Person.arrival_date)
        ).all())
        rows = []
        for p in people:
            if _current_status(p) != "incoming":
                continue
            cur = _current(p)
            rate = cur["rate"].rate if cur["rate"] else None
            checks = [p.orders_received, p.itinerary_received, p.aob_scheduled, p.barracks_assigned]
            done = sum(1 for c in checks if c)
            rows.append({
                "person": p,
                "rate": rate,
                "arrival_date": p.arrival_date,
                "sponsor_label": p.sponsor.full_display if p.sponsor else None,
                "checklist_done": done,
                "checklist_total": len(checks),
                "orders_received": p.orders_received,
                "itinerary_received": p.itinerary_received,
                "aob_scheduled": p.aob_scheduled,
                "barracks_assigned": p.barracks_assigned,
            })
    return render(request, "personnel/incoming.html", rows=rows)


@router.get("/departed")
def list_departed(request: Request, _: None = Depends(require(P_PERSONNEL_VIEW))):
    with SessionLocal() as s:
        people = list(s.scalars(
            select(M.Person)
            .where(M.Person.active == False)  # noqa: E712
            .options(selectinload(M.Person.rates))
            .order_by(M.Person.archived_at.desc())
        ).all())
        rows = []
        for p in people:
            cur_rate = next((r for r in p.rates if r.valid_to is None), None)
            rows.append({
                "person": p,
                "rate": cur_rate.rate if cur_rate else None,
                "departed_at": p.archived_at,
                "reason": p.archived_reason,
            })
    return render(request, "personnel/departed.html", rows=rows)


@router.get("/incoming/new")
def new_incoming_form(request: Request, _: None = Depends(require(P_PERSONNEL_WRITE))):
    with SessionLocal() as s:
        sponsors = list(s.scalars(
            select(M.Person)
            .where(M.Person.active == True)  # noqa: E712
            .order_by(M.Person.display_order)
        ).all())
    return render(
        request, "personnel/incoming_new.html",
        sponsors=sponsors, rate_groups=rank_catalog.by_category(),
    )


@router.post("/incoming")
async def create_incoming(request: Request, _: None = Depends(require(P_PERSONNEL_WRITE))):
    form = await request.form()
    today = date.today()
    last_name = (form.get("last_name") or "").strip()
    if not last_name:
        raise HTTPException(400, "last name is required")
    rate = (form.get("rate") or "").strip() or None
    full_display = f"{rate} {last_name}".strip() if rate else last_name
    arrival_iso = form.get("arrival_date") or None
    sponsor_raw = form.get("sponsor_person_id") or None

    with SessionLocal() as s:
        last_pos = s.scalar(
            select(M.Person.display_order).order_by(M.Person.display_order.desc()).limit(1)
        ) or 0
        p = M.Person(
            last_name=last_name,
            first_name=(form.get("first_name") or None) and form.get("first_name").strip(),
            full_display=full_display,
            notes=(form.get("notes") or None),
            display_order=last_pos + 1,
            arrival_date=date.fromisoformat(arrival_iso) if arrival_iso else None,
            sponsor_person_id=int(sponsor_raw) if sponsor_raw else None,
            orders_received=bool(form.get("orders_received")),
            itinerary_received=bool(form.get("itinerary_received")),
            aob_scheduled=bool(form.get("aob_scheduled")),
            barracks_assigned=bool(form.get("barracks_assigned")),
        )
        s.add(p)
        s.flush()
        if rate:
            s.add(M.PersonRate(
                person_id=p.id, rate=rate,
                paygrade=rank_catalog.paygrade_for(rate),
                valid_from=today,
            ))
        s.add(M.PersonRosterStatus(person_id=p.id, status="incoming", valid_from=today))
        s.commit()
    return RedirectResponse("/personnel/incoming", status_code=303)


@router.post("/{person_id}/checklist")
async def update_checklist(
    person_id: int,
    request: Request,
    _: None = Depends(require(P_PERSONNEL_WRITE)),
):
    form = await request.form()
    with SessionLocal() as s:
        p = s.get(M.Person, person_id)
        if not p:
            raise HTTPException(404)
        p.orders_received = bool(form.get("orders_received"))
        p.itinerary_received = bool(form.get("itinerary_received"))
        p.aob_scheduled = bool(form.get("aob_scheduled"))
        p.barracks_assigned = bool(form.get("barracks_assigned"))
        if form.get("arrival_date"):
            p.arrival_date = date.fromisoformat(form.get("arrival_date"))
        sponsor_raw = form.get("sponsor_person_id")
        p.sponsor_person_id = int(sponsor_raw) if sponsor_raw else None
        s.commit()
    return RedirectResponse("/personnel/incoming", status_code=303)


@router.post("/{person_id}/arrived")
def mark_arrived(person_id: int, _: None = Depends(require(P_PERSONNEL_WRITE))):
    with SessionLocal() as s:
        p = s.get(M.Person, person_id)
        if not p:
            raise HTTPException(404)
        eff.set_new_value(
            s, M.PersonRosterStatus, person_id=p.id, effective_date=date.today(),
            fields={"status": "active"},
            no_op_if_unchanged=("status",),
        )
        s.commit()
    return RedirectResponse(f"/personnel/{person_id}", status_code=303)


@router.get("/new")
def new_person_form(request: Request, _: None = Depends(require(P_PERSONNEL_WRITE))):
    return render(
        request,
        "personnel/new.html",
        duty_sections=DUTY_SECTIONS,
        rate_groups=rank_catalog.by_category(),
    )


@router.post("")
async def create_person(
    request: Request,
    _: None = Depends(require(P_PERSONNEL_WRITE)),
):
    form = await request.form()
    last_name = (form.get("last_name") or "").strip()
    if not last_name:
        raise HTTPException(400, "last name is required")
    first_name = (form.get("first_name") or None) and (form.get("first_name") or "").strip()
    rate = (form.get("rate") or None) and (form.get("rate") or "").strip()
    position = (form.get("position") or None) and (form.get("position") or "").strip()
    notes = form.get("notes") or None
    duty_section = form.get("duty_section")
    prd_date = form.get("prd_date") or None
    has_drivers_license = form.get("has_drivers_license")
    drivers_license_expires = form.get("drivers_license_expires") or None
    roster_status = form.get("roster_status") or "active"

    # Phase 6: scan notes for PII/CUI patterns. If found, require
    # explicit acknowledgment via the ``sensitive_ack`` checkbox.
    notes_warnings = _sensitive_warnings(notes)
    if notes_warnings and not form.get("sensitive_ack"):
        return render(
            request, "personnel/new.html",
            duty_sections=DUTY_SECTIONS,
            rate_groups=rank_catalog.by_category(),
            sensitive_warnings=notes_warnings,
            form_data={
                "last_name": last_name,
                "first_name": first_name,
                "rate": rate,
                "position": position,
                "notes": notes,
                "duty_section": duty_section,
                "prd_date": prd_date,
                "roster_status": roster_status,
            },
        )

    today = date.today()
    with SessionLocal() as s:
        full_display = f"{rate} {last_name}".strip() if rate else last_name
        last_pos = s.scalar(
            select(M.Person.display_order).order_by(M.Person.display_order.desc()).limit(1)
        ) or 0
        p = M.Person(
            last_name=last_name,
            first_name=first_name,
            full_display=full_display.strip(),
            position=position,
            notes=notes,
            display_order=last_pos + 1,
        )
        s.add(p)
        s.flush()
        if rate:
            s.add(M.PersonRate(
                person_id=p.id, rate=rate,
                paygrade=rank_catalog.paygrade_for(rate),
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
def show_person(
    person_id: int,
    request: Request,
    _: None = Depends(require(P_PERSONNEL_VIEW)),
):
    from ..services.personnel_stats import stats_for
    with SessionLocal() as s:
        p = s.get(M.Person, person_id)
        if not p:
            raise HTTPException(404, "person not found")
        rates = list(p.rates)
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
        stats = stats_for(s, p.id, window_days=180)
        return render(
            request,
            "personnel/show.html",
            person=p,
            rates=rates,
            duty_sections=ds,
            prds=prds,
            drivers_licenses=dl,
            quals=quals,
            absences=absences,
            stats=stats,
        )


@router.get("/{person_id}/edit")
def edit_person_form(
    person_id: int,
    request: Request,
    _: None = Depends(require(P_PERSONNEL_WRITE)),
):
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
        duty_sections=DUTY_SECTIONS,
        rate_groups=rank_catalog.by_category(),
    )


@router.post("/{person_id}")
async def update_person(
    person_id: int,
    request: Request,
    _: None = Depends(require(P_PERSONNEL_WRITE)),
):
    form = await request.form()
    last_name = (form.get("last_name") or "").strip()
    first_name = (form.get("first_name") or None) and (form.get("first_name") or "").strip()
    position = (form.get("position") or None) and (form.get("position") or "").strip()
    notes = form.get("notes") or None
    rate = (form.get("rate") or None) and (form.get("rate") or "").strip()
    duty_section = form.get("duty_section")
    prd_date = form.get("prd_date") or None
    prd_reason = form.get("prd_reason") or "correction"
    has_drivers_license = form.get("has_drivers_license")
    drivers_license_expires = form.get("drivers_license_expires") or None
    roster_status = form.get("roster_status") or "active"
    effective_date = form.get("effective_date") or None

    eff_date = date.fromisoformat(effective_date) if effective_date else date.today()
    with SessionLocal() as s:
        p = s.get(M.Person, person_id)
        if not p:
            raise HTTPException(404, "person not found")

        # Phase 6: scan notes for PII/CUI patterns.
        notes_warnings = _sensitive_warnings(notes)
        if notes_warnings and not form.get("sensitive_ack"):
            cur = _current(p)
            return render(
                request, "personnel/edit.html",
                person=p, cur=cur,
                duty_sections=DUTY_SECTIONS,
                rate_groups=rank_catalog.by_category(),
                sensitive_warnings=notes_warnings,
            )

        p.last_name = last_name
        p.first_name = first_name
        p.position = position
        p.notes = notes
        # Recompute full_display from current rate (may have just changed below).
        p.full_display = (f"{rate} {p.last_name}".strip() if rate else p.last_name)

        if rate:
            eff.set_new_value(
                s, M.PersonRate, person_id=p.id, effective_date=eff_date,
                fields={"rate": rate, "paygrade": rank_catalog.paygrade_for(rate)},
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
def archive_person(
    person_id: int,
    reason: str = Form(""),
    _: None = Depends(require(P_PERSONNEL_ARCHIVE)),
):
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
