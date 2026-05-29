"""Tests for the Tk front-end's data bridge (``app.tk.queries``).

These exercise the DTO-building layer directly against a session — no
display required — so the screens can stay thin and untested. They assert
that each query reuses the services correctly and returns detached DTOs
of the expected shape.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app import models as M
from app.tenancy import tenant_context
from app.tk import dto
from app.tk import queries as Q

from tests.conftest import (
    make_absence,
    make_absence_codes,
    make_person,
    make_qual,
    make_task,
    make_task_categories,
    make_worklist,
)


@pytest.fixture
def populated(session):
    """A small but representative org: people, a qual, an absence, a worklist."""
    codes = make_absence_codes(session)
    make_task_categories(session)
    p1 = make_person(session, last_name="Reyes", rate="GS13", display_order=0)
    p2 = make_person(session, last_name="Holland", rate="GS12", display_order=1)
    q = make_qual(session, "Forklift")
    session.add(
        M.PersonQual(
            person_id=p1.id, qual_id=q.id, status="qualified",
            valid_from=date.today(), org_id=1,
        )
    )
    make_absence(
        session,
        person_id=p2.id,
        code_id=codes["Leave"].id,
        start_date=date.today(),
        end_date=date.today(),
        reason="Annual leave",
    )
    monday = date.today() - timedelta(days=date.today().weekday())
    wl = make_worklist(session, monday)
    make_task(
        session, worklist_id=wl.id, name="Sweep the deck",
        scheduled_date=date.today(),
    )
    session.commit()
    return {"p1": p1.id, "p2": p2.id, "qual": q.id, "worklist": wl.id}


def run(session, q):
    """Invoke a query closure inside the tenant context, as ``context.read`` does."""
    with tenant_context(1):
        return q(session)


def test_day_view_counts_present_and_absent(session, populated):
    view = run(session, Q.day_view(date.today()))
    assert isinstance(view, dto.DayViewDTO)
    assert view.total == 2
    assert view.full_absent == 1
    assert [r.name for r in view.absent_rows] == ["GS12 Holland"]
    assert any(t.source == "scheduled" for t in view.tasks)


def test_personnel_active_groups_and_excludes_incoming(session, populated):
    grouped = run(session, Q.personnel_active())
    names = [p.name for rows in grouped.values() for p in rows]
    assert "GS13 Reyes" in names
    assert "GS12 Holland" in names
    # Every returned row is a detached DTO, not an ORM object.
    for rows in grouped.values():
        for p in rows:
            assert isinstance(p, dto.PersonRowDTO)


def test_person_profile_includes_quals_and_effective_rows(session, populated):
    prof = run(session, Q.person_profile(populated["p1"]))
    assert isinstance(prof, dto.PersonProfileDTO)
    assert prof.name == "GS13 Reyes"
    assert [q.name for q in prof.quals] == ["Forklift"]
    assert prof.quals[0].status == "qualified"
    # current rate row carries valid_to is None
    assert any(r.valid_to is None for r in prof.rates)


def test_person_profile_missing_returns_none(session, populated):
    assert run(session, Q.person_profile(999_999)) is None


def test_qual_matrix_aligns_cells_to_columns(session, populated):
    mx = run(session, Q.qual_matrix())
    assert mx.qual_names == ["Forklift"]
    assert all(len(row.cells) == len(mx.qual_names) for row in mx.rows)
    reyes = next(r for r in mx.rows if "Reyes" in r.name)
    assert reyes.cells[0] == "qualified"


def test_absence_list_and_calendar(session, populated):
    rows = run(session, Q.absence_list())
    assert any(r.person_name == "GS12 Holland" for r in rows)

    cal = run(session, Q.absence_calendar(date.today(), 7))
    assert len(cal.days) == 7
    assert cal.daily_total == 2
    assert isinstance(cal.rows[0], dto.CalendarRowDTO)


def test_worklist_list_and_week_view(session, populated):
    listing = run(session, Q.worklist_list())
    all_rows = listing.current + listing.upcoming + listing.archived
    assert any(r.id == populated["worklist"] for r in all_rows)

    view = run(session, Q.worklist_show(populated["worklist"]))
    assert isinstance(view, dto.WeekViewDTO)
    assert len(view.days) == 7
    assert view.week_total_tasks >= 1


def test_alerts_list_returns_dtos(session, populated):
    rows = run(session, Q.alerts_list("active"))
    assert all(isinstance(r, dto.AlertRowDTO) for r in rows)
