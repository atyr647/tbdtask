"""Tests for the Tk front-end's write layer (``app.tk.commands``).

These run the command closures the way ``context.write`` does — inside a
tenant context against a session — and assert the same effects the web
route handlers produce (effective-dated rows, soft-delete, alert state).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app import models as M
from app.tenancy import tenant_context
from app.tk import commands as C

from tests.conftest import (
    make_absence_codes,
    make_person,
    make_worklist,
    set_prd,
)


def run(session, cmd):
    with tenant_context(1):
        return cmd(session)


# -- Personnel --------------------------------------------------------------


def test_create_person_writes_effective_rows(session):
    pid = run(session, C.create_person(
        last_name="Vega", rate="Technician", duty_section=3,
        prd_date="2027-01-01", notes="hello"))
    session.commit()
    p = session.get(M.Person, pid)
    assert p.full_display == "Technician Vega"
    assert any(r.valid_to is None and r.rate == "Technician" for r in p.rates)
    assert any(d.duty_section == 3 for d in p.duty_sections)
    assert any(pr.prd_date == date(2027, 1, 1) for pr in p.prds)
    # roster status defaults to active
    assert any(s.status == "active" and s.valid_to is None
               for s in p.roster_statuses)


def test_create_incoming_sets_incoming_status(session):
    pid = run(session, C.create_incoming(
        last_name="Olson", rate="Intern", arrival_date="2026-07-01",
        orders_received=True))
    session.commit()
    p = session.get(M.Person, pid)
    assert p.arrival_date == date(2026, 7, 1)
    assert p.orders_received is True
    assert any(s.status == "incoming" and s.valid_to is None
               for s in p.roster_statuses)


def test_update_person_closes_prior_rate_row(session):
    p = make_person(session, last_name="Banks", rate="Coordinator")
    session.commit()
    run(session, C.update_person(
        p.id, last_name="Banks", rate="Supervisor", roster_status="active",
        effective_date=date.today().isoformat()))
    session.commit()
    session.refresh(p)
    current = [r for r in p.rates if r.valid_to is None]
    closed = [r for r in p.rates if r.valid_to is not None]
    assert len(current) == 1 and current[0].rate == "Supervisor"
    assert closed and closed[0].rate == "Coordinator"
    assert p.full_display == "Supervisor Banks"


def test_mark_arrived_flips_status(session):
    pid = run(session, C.create_incoming(last_name="Marsh", rate="Intern"))
    session.commit()
    run(session, C.mark_arrived(pid))
    session.commit()
    p = session.get(M.Person, pid)
    assert any(s.status == "active" and s.valid_to is None
               for s in p.roster_statuses)


def test_archive_person_soft_deletes(session):
    p = make_person(session, last_name="Tate")
    session.commit()
    run(session, C.archive_person(p.id, "transferred"))
    session.commit()
    session.refresh(p)
    assert p.active is False
    assert p.archived_reason == "transferred"


# -- Absences ---------------------------------------------------------------


def test_create_absence_and_validation(session):
    codes = make_absence_codes(session)
    p = make_person(session, last_name="Juarez")
    session.commit()
    aid = run(session, C.create_absence(
        person_id=p.id, code_id=codes["Leave"].id,
        start_date=date.today().isoformat(),
        end_date=(date.today() + timedelta(days=2)).isoformat(),
        reason="family"))
    session.commit()
    assert session.get(M.Absence, aid).reason == "family"

    with pytest.raises(C.ValidationError):
        run(session, C.create_absence(
            person_id=p.id, code_id=codes["Leave"].id,
            start_date=date.today().isoformat(),
            end_date=(date.today() - timedelta(days=2)).isoformat()))


def test_archive_absence(session):
    codes = make_absence_codes(session)
    p = make_person(session, last_name="Spencer")
    session.commit()
    aid = run(session, C.create_absence(
        person_id=p.id, code_id=codes["School"].id,
        start_date=date.today().isoformat(),
        end_date=date.today().isoformat()))
    session.commit()
    run(session, C.archive_absence(aid, "cancelled"))
    session.commit()
    a = session.get(M.Absence, aid)
    assert a.active is False and a.archived_reason == "cancelled"


# -- Alerts -----------------------------------------------------------------


def test_alert_snooze_and_resolve(session):
    a = M.Alert(alert_type="prd_2month", severity="warn", org_id=1)
    session.add(a)
    session.commit()
    run(session, C.snooze_alert(a.id, (date.today() + timedelta(days=5)).isoformat()))
    session.commit()
    assert session.get(M.Alert, a.id).snoozed_until == date.today() + timedelta(days=5)

    run(session, C.resolve_alert(a.id, "handled"))
    session.commit()
    refreshed = session.get(M.Alert, a.id)
    assert refreshed.resolved_at is not None
    assert "handled" in (refreshed.notes or "")


def test_extend_prd_updates_person_and_resolves(session):
    p = make_person(session, last_name="Foster")
    set_prd(session, p.id, date.today() + timedelta(days=30))
    a = M.Alert(alert_type="prd_1month", severity="urgent",
                person_id=p.id, org_id=1)
    session.add(a)
    session.commit()
    run(session, C.extend_prd(a.id, days=180))
    session.commit()
    current = [pr for pr in session.get(M.Person, p.id).prds if pr.valid_to is None]
    assert current and current[0].change_reason == "extension"
    assert session.get(M.Alert, a.id).resolved_at is not None


# -- Worklists --------------------------------------------------------------


def test_lock_worklist(session):
    monday = date.today() - timedelta(days=date.today().weekday())
    wl = make_worklist(session, monday)
    session.commit()
    run(session, C.lock_worklist(wl.id, "  Chief  "))
    session.commit()
    session.refresh(wl)
    assert wl.locked is True
    assert wl.locked_by_name == "Chief"
    assert wl.locked_at is not None
