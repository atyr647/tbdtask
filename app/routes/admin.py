from fastapi import APIRouter, Request
from sqlalchemy import func, select

from ..db import SessionLocal
from .. import models as M
from ..templating import render

router = APIRouter(prefix="/admin")


@router.get("/verify")
def verify(request: Request):
    """Show import provenance and the current row counts so the operator can
    sanity-check what landed in the DB versus what they expected from the
    legacy workbook."""
    with SessionLocal() as s:
        batches = s.scalars(
            select(M.ImportBatch).order_by(M.ImportBatch.id.desc())
        ).all()
        live_counts = {
            "persons": s.scalar(select(func.count()).select_from(M.Person).where(M.Person.active == True)),  # noqa: E712
            "qualifications": s.scalar(
                select(func.count()).select_from(M.Qualification).where(M.Qualification.active == True)  # noqa: E712
            ),
            "person_quals": s.scalar(
                select(func.count()).select_from(M.PersonQual).where(M.PersonQual.valid_to.is_(None), M.PersonQual.active == True)  # noqa: E712
            ),
            "absences": s.scalar(select(func.count()).select_from(M.Absence).where(M.Absence.active == True)),  # noqa: E712
            "task_categories": s.scalar(select(func.count()).select_from(M.TaskCategory)),
            "absence_codes": s.scalar(select(func.count()).select_from(M.AbsenceCode)),
        }
        # Personnel imported only because they appeared on a non-roster sheet.
        ad_hoc = s.scalars(
            select(M.Person)
            .where(M.Person.notes.like("%not on Personnel Roster%"))
            .order_by(M.Person.id)
        ).all()
    return render(
        request,
        "admin/verify.html",
        batches=batches,
        live_counts=live_counts,
        ad_hoc_persons=ad_hoc,
    )
