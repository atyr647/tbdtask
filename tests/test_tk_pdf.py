"""Tests for the Tk front-end's worklist PDF export.

Covers the print-grid query (DTO shape) and the ReportLab renderer
(produces a valid PDF). The renderer test skips cleanly when ReportLab
isn't installed, since it's an optional dependency.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.tenancy import tenant_context
from app.tk import dto, pdf
from app.tk import commands as C
from app.tk import queries as Q

from tests.conftest import (
    make_absence,
    make_absence_codes,
    make_person,
    make_worklist,
)


def run(session, fn):
    with tenant_context(1):
        return fn(session)


@pytest.fixture
def week(session):
    codes = make_absence_codes(session)
    p1 = make_person(session, last_name="Reyes", rate="GS13", display_order=0)
    p2 = make_person(session, last_name="Holland", rate="GS12", display_order=1)
    monday = date.today() - timedelta(days=date.today().weekday())
    wl = make_worklist(session, monday)
    session.commit()
    # A task assigned to p1, and an absence for p2 on Monday.
    run(session, C.create_task(
        wl.id, name="Sweep the deck", scheduled_date=monday.isoformat(),
        person_ids=[p1.id]))
    make_absence(session, person_id=p2.id, code_id=codes["Leave"].id,
                 start_date=monday, end_date=monday, reason="family")
    session.commit()
    return wl.id


def test_week_grid_dto_shape(session, week):
    grid = run(session, Q.week_grid(week))
    assert isinstance(grid, dto.WeekGridDTO)
    assert len(grid.headers) == 5  # Mon–Fri default
    # The assigned person shows up as a row with a task.
    reyes = next((r for r in grid.rows if "Reyes" in r.name), None)
    assert reyes is not None
    assert any(c.tasks for c in reyes.cells)
    # The absent person shows up with an absence code in Monday's cell.
    holland = next((r for r in grid.rows if "Holland" in r.name), None)
    assert holland is not None
    assert holland.cells[0].absence_code == "Leave"
    # Monday's header carries an out summary line for Holland.
    assert any("Holland" in s for s in grid.headers[0].out_summary)


def test_week_grid_missing_returns_none(session):
    assert run(session, Q.week_grid(999_999)) is None


@pytest.mark.skipif(not pdf.available(), reason="ReportLab not installed")
def test_render_worklist_pdf(session, week, tmp_path):
    grid = run(session, Q.week_grid(week))
    out = tmp_path / "wl.pdf"
    path = pdf.render_worklist_pdf(grid, str(out))
    data = out.read_bytes()
    assert path == str(out)
    assert data[:5] == b"%PDF-"
    assert b"%%EOF" in data[-2048:]
    assert out.stat().st_size > 1000
