from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..db import SessionLocal
from .. import models as M
from ..services.qual_overview import build_qual_overview
from ..templating import render

router = APIRouter()


PERSON_QUAL_STATUSES = (
    "not_assigned", "assigned", "in_progress", "qualified",
    "dinq", "expired", "waived",
)


# ---------------------------------------------------------------------------
# Catalog (read + CRUD)
# ---------------------------------------------------------------------------

@router.get("/quals")
def list_quals(request: Request):
    with SessionLocal() as s:
        quals = s.scalars(
            select(M.Qualification)
            .where(M.Qualification.active == True)  # noqa: E712
            .order_by(M.Qualification.display_order)
        ).all()
        by_category: dict[str, list] = defaultdict(list)
        for q in quals:
            by_category[q.category or "Uncategorized"].append(q)
    return render(request, "quals/list.html", by_category=dict(by_category))


@router.get("/quals/new")
def new_qual_form(request: Request):
    return render(request, "quals/new.html")


@router.post("/quals")
def create_qual(
    name: str = Form(...),
    code: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
    pinned_column: Optional[str] = Form(None),
    validity_period_days: Optional[int] = Form(None),
    notes: Optional[str] = Form(None),
):
    with SessionLocal() as s:
        last_pos = s.scalar(
            select(M.Qualification.display_order).order_by(M.Qualification.display_order.desc()).limit(1)
        ) or 0
        q = M.Qualification(
            name=name.strip(),
            code=(code or None) and code.strip(),
            category=(category or None) and category.strip(),
            pinned_column=bool(pinned_column),
            validity_period_days=validity_period_days,
            notes=(notes or None),
            display_order=last_pos + 1,
        )
        s.add(q)
        s.commit()
    return RedirectResponse("/quals", status_code=303)


@router.get("/quals/overview")
def quals_overview(request: Request, threshold: int = 2):
    threshold = max(1, min(threshold, 10))
    with SessionLocal() as s:
        summaries = build_qual_overview(s, qualified_threshold=threshold)
    return render(request, "quals/overview.html", summaries=summaries, threshold=threshold)


@router.get("/quals/matrix")
def qual_matrix(request: Request):
    with SessionLocal() as s:
        quals = s.scalars(
            select(M.Qualification)
            .where(M.Qualification.active == True)  # noqa: E712
            .order_by(M.Qualification.display_order)
        ).all()
        people = s.scalars(
            select(M.Person)
            .where(M.Person.active == True)  # noqa: E712
            .options(selectinload(M.Person.rates))
            .order_by(M.Person.display_order)
        ).all()
        rows = s.execute(
            select(M.PersonQual.person_id, M.PersonQual.qual_id, M.PersonQual.status)
            .where(M.PersonQual.valid_to.is_(None), M.PersonQual.active == True)  # noqa: E712
        ).all()
        status_map: dict[tuple[int, int], str] = {(p, q): st for p, q, st in rows}
    grid = []
    for p in people:
        grid.append({
            "person": p,
            "cells": [status_map.get((p.id, q.id)) for q in quals],
        })
    return render(request, "quals/matrix.html", quals=quals, grid=grid)


@router.get("/quals/{qual_id}/edit")
def edit_qual_form(qual_id: int, request: Request):
    with SessionLocal() as s:
        q = s.get(M.Qualification, qual_id)
        if not q:
            raise HTTPException(404, "qual not found")
    return render(request, "quals/edit.html", qual=q)


@router.post("/quals/{qual_id}")
def update_qual(
    qual_id: int,
    name: str = Form(...),
    code: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
    pinned_column: Optional[str] = Form(None),
    validity_period_days: Optional[int] = Form(None),
    notes: Optional[str] = Form(None),
):
    with SessionLocal() as s:
        q = s.get(M.Qualification, qual_id)
        if not q:
            raise HTTPException(404, "qual not found")
        q.name = name.strip()
        q.code = (code or None) and code.strip()
        q.category = (category or None) and category.strip()
        q.pinned_column = bool(pinned_column)
        q.validity_period_days = validity_period_days
        q.notes = (notes or None)
        s.commit()
    return RedirectResponse("/quals", status_code=303)


@router.post("/quals/{qual_id}/archive")
def archive_qual(qual_id: int, reason: str = Form("")):
    with SessionLocal() as s:
        q = s.get(M.Qualification, qual_id)
        if not q:
            raise HTTPException(404, "qual not found")
        q.active = False
        q.archived_at = datetime.now()
        q.archived_reason = reason or None
        s.commit()
    return RedirectResponse("/quals", status_code=303)


# ---------------------------------------------------------------------------
# Person-qual assignment / status change
# ---------------------------------------------------------------------------

@router.get("/personnel/{person_id}/quals/new")
def new_person_qual_form(person_id: int, request: Request):
    with SessionLocal() as s:
        p = s.get(M.Person, person_id)
        if not p:
            raise HTTPException(404, "person not found")
        # Quals not currently assigned to this person.
        already = {
            row.qual_id for row in s.scalars(
                select(M.PersonQual).where(
                    M.PersonQual.person_id == person_id,
                    M.PersonQual.valid_to.is_(None),
                    M.PersonQual.active == True,  # noqa: E712
                )
            )
        }
        avail = [
            q for q in s.scalars(
                select(M.Qualification)
                .where(M.Qualification.active == True)  # noqa: E712
                .order_by(M.Qualification.display_order)
            ).all()
            if q.id not in already
        ]
    return render(
        request, "quals/person_new.html",
        person=p, available=avail, statuses=PERSON_QUAL_STATUSES,
    )


@router.post("/personnel/{person_id}/quals")
def create_person_qual(
    person_id: int,
    qual_id: int = Form(...),
    status: str = Form("assigned"),
    started_at: Optional[str] = Form(None),
    achieved_at: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    today = date.today()
    with SessionLocal() as s:
        p = s.get(M.Person, person_id)
        q = s.get(M.Qualification, qual_id)
        if not p or not q:
            raise HTTPException(404)
        started_dt = datetime.fromisoformat(started_at) if started_at else None
        achieved_dt = datetime.fromisoformat(achieved_at) if achieved_at else None
        expires_dt = None
        if achieved_dt and q.validity_period_days:
            expires_dt = achieved_dt + timedelta(days=q.validity_period_days)
        s.add(M.PersonQual(
            person_id=p.id, qual_id=q.id, status=status,
            started_at=started_dt, achieved_at=achieved_dt, expires_at=expires_dt,
            notes=notes or None,
            valid_from=today,
        ))
        s.commit()
    return RedirectResponse(f"/personnel/{person_id}", status_code=303)


@router.get("/personnel/{person_id}/quals/{pq_id}/edit")
def edit_person_qual_form(person_id: int, pq_id: int, request: Request):
    with SessionLocal() as s:
        pq = s.get(M.PersonQual, pq_id)
        if not pq or pq.person_id != person_id:
            raise HTTPException(404)
        p = s.get(M.Person, person_id)
        q = s.get(M.Qualification, pq.qual_id)
        history = s.scalars(
            select(M.PersonQual)
            .where(
                M.PersonQual.person_id == person_id,
                M.PersonQual.qual_id == pq.qual_id,
            )
            .order_by(M.PersonQual.valid_from.desc())
        ).all()
    return render(
        request, "quals/person_edit.html",
        person=p, qual=q, pq=pq, history=list(history),
        statuses=PERSON_QUAL_STATUSES,
    )


@router.post("/personnel/{person_id}/quals/{pq_id}")
def update_person_qual(
    person_id: int,
    pq_id: int,
    status: str = Form(...),
    started_at: Optional[str] = Form(None),
    achieved_at: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    effective_date: Optional[str] = Form(None),
):
    eff_date = date.fromisoformat(effective_date) if effective_date else date.today()
    with SessionLocal() as s:
        pq = s.get(M.PersonQual, pq_id)
        if not pq or pq.person_id != person_id:
            raise HTTPException(404)
        if pq.valid_to is not None:
            raise HTTPException(400, "cannot edit a historical row directly")
        q = s.get(M.Qualification, pq.qual_id)
        started_dt = datetime.fromisoformat(started_at) if started_at else None
        achieved_dt = datetime.fromisoformat(achieved_at) if achieved_at else None
        expires_dt = None
        if achieved_dt and q.validity_period_days:
            expires_dt = achieved_dt + timedelta(days=q.validity_period_days)
        # Close current and append a new row capturing the change.
        pq.valid_to = eff_date
        s.add(M.PersonQual(
            person_id=person_id, qual_id=pq.qual_id, status=status,
            started_at=started_dt, achieved_at=achieved_dt, expires_at=expires_dt,
            notes=notes or None,
            valid_from=eff_date,
        ))
        s.commit()
    return RedirectResponse(f"/personnel/{person_id}", status_code=303)
