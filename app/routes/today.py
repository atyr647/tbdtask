from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Request

from ..db import SessionLocal
from ..services.availability import get_day_report
from ..templating import render

router = APIRouter()


@router.get("/today")
def today(request: Request):
    return _day_view(request, date.today())


@router.get("/day/{day_iso}")
def day(day_iso: str, request: Request):
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
    return render(
        request, "today/day.html",
        report=report,
        absent_rows=absent_rows,
        present_rows=present_rows,
        prev_day=(on_date - timedelta(days=1)).isoformat(),
        next_day=(on_date + timedelta(days=1)).isoformat(),
        today_iso=date.today().isoformat(),
    )
