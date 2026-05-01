from datetime import date

from fastapi import APIRouter, Request
from sqlalchemy import func, select

from ..db import SessionLocal
from .. import models as M
from ..templating import render

router = APIRouter()


@router.get("/")
def home(request: Request):
    with SessionLocal() as s:
        person_count = s.scalar(select(func.count()).select_from(M.Person).where(M.Person.active == True))  # noqa: E712
        qual_count = s.scalar(select(func.count()).select_from(M.Qualification).where(M.Qualification.active == True))  # noqa: E712
        absence_count = s.scalar(select(func.count()).select_from(M.Absence).where(M.Absence.active == True))  # noqa: E712
        worklist_count = s.scalar(select(func.count()).select_from(M.Worklist).where(M.Worklist.active == True))  # noqa: E712
        last_import = s.scalars(
            select(M.ImportBatch).order_by(M.ImportBatch.id.desc()).limit(1)
        ).first()
    return render(
        request,
        "home.html",
        person_count=person_count,
        qual_count=qual_count,
        absence_count=absence_count,
        worklist_count=worklist_count,
        last_import=last_import,
        today=date.today(),
    )
