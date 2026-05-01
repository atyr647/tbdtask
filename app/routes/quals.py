from collections import defaultdict

from fastapi import APIRouter, Request
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..db import SessionLocal
from .. import models as M
from ..templating import render

router = APIRouter(prefix="/quals")


@router.get("")
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


@router.get("/matrix")
def qual_matrix(request: Request):
    """Person x qualification grid, mirroring the legacy spreadsheet layout."""
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
        # Map (person_id, qual_id) -> status (current row only).
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
