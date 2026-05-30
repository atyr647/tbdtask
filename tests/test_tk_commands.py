"""Tests for the Tk front-end's write layer (``app.tk.commands``).

These run the command closures the way ``context.write`` does — inside a
tenant context against a session — and assert the same effects the web
route handlers produce (effective-dated rows, soft-delete, alert state).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app import models as M
from app.tenancy import tenant_context
from app.tk import commands as C

from tests.conftest import (
    make_absence_codes,
    make_person,
    make_qual,
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


def test_create_worklist_snaps_to_monday_and_is_idempotent(session):
    # A Wednesday snaps back to its Monday.
    wid = run(session, C.create_worklist("2026-06-03"))
    session.commit()
    wl = session.get(M.Worklist, wid)
    assert wl.week_starting == date(2026, 6, 1)
    assert wl.name.startswith("Week ")
    # Creating again for the same week returns the same row.
    again = run(session, C.create_worklist("2026-06-01"))
    assert again == wid


def test_create_task_defaults_first_assignee_to_poic(session):
    p1 = make_person(session, last_name="Mason", display_order=0)
    p2 = make_person(session, last_name="Vega", display_order=1)
    monday = date.today() - timedelta(days=date.today().weekday())
    wl = make_worklist(session, monday)
    session.commit()
    tid = run(session, C.create_task(
        wl.id, name="Sweep the deck",
        scheduled_date=monday.isoformat(),
        person_ids=[p1.id, p2.id]))
    session.commit()
    inst = session.get(M.TaskInstance, tid)
    assert inst.name == "Sweep the deck"
    poics = [a for a in inst.assignments if a.is_poic and a.active]
    assert len(poics) == 1 and poics[0].person_id == p1.id


def test_create_task_requires_name(session):
    monday = date.today() - timedelta(days=date.today().weekday())
    wl = make_worklist(session, monday)
    session.commit()
    with pytest.raises(C.ValidationError):
        run(session, C.create_task(wl.id, name="   "))


def test_create_task_rejected_when_locked(session):
    monday = date.today() - timedelta(days=date.today().weekday())
    wl = make_worklist(session, monday, locked=True)
    session.commit()
    with pytest.raises(C.ValidationError):
        run(session, C.create_task(wl.id, name="late task"))


def test_update_task_status_done_sets_completed_at(session):
    monday = date.today() - timedelta(days=date.today().weekday())
    wl = make_worklist(session, monday)
    session.commit()
    tid = run(session, C.create_task(wl.id, name="Inspect 402"))
    session.commit()
    run(session, C.update_task(tid, name="Inspect 402", status="done", hours=2.5))
    session.commit()
    inst = session.get(M.TaskInstance, tid)
    assert inst.status == "done"
    assert inst.completed_at is not None
    assert inst.hours == 2.5
    # Flipping back to open clears the completion timestamp.
    run(session, C.update_task(tid, name="Inspect 402", status="open"))
    session.commit()
    assert session.get(M.TaskInstance, tid).completed_at is None


def test_archive_task_soft_deletes(session):
    monday = date.today() - timedelta(days=date.today().weekday())
    wl = make_worklist(session, monday)
    session.commit()
    tid = run(session, C.create_task(wl.id, name="Scrap me"))
    session.commit()
    run(session, C.archive_task(tid, "duplicate"))
    session.commit()
    inst = session.get(M.TaskInstance, tid)
    assert inst.active is False and inst.archived_reason == "duplicate"


# -- Qualifications ---------------------------------------------------------


def test_assign_quals_derives_expiry_from_validity(session):
    p = make_person(session, last_name="Foster")
    q1 = make_qual(session, "Forklift", validity_period_days=365)
    q2 = make_qual(session, "Crane")  # no validity window
    session.commit()
    n = run(session, C.assign_quals(
        p.id, qual_ids=[q1.id, q2.id], status="qualified",
        achieved_at="2026-01-01"))
    session.commit()
    assert n == 2
    rows = {pq.qual_id: pq for pq in session.get(M.Person, p.id).quals}
    assert rows[q1.id].expires_at == datetime(2027, 1, 1)  # +365d
    assert rows[q2.id].expires_at is None                  # no window -> none


def test_assign_quals_requires_a_selection(session):
    p = make_person(session, last_name="Mason")
    session.commit()
    with pytest.raises(C.ValidationError):
        run(session, C.assign_quals(p.id, qual_ids=[]))


def test_qual_choices_excludes_already_held(session):
    p = make_person(session, last_name="Vega")
    q1 = make_qual(session, "Forklift")
    make_qual(session, "Crane")  # stays in catalog; should still be offered
    session.commit()
    run(session, C.assign_quals(p.id, qual_ids=[q1.id], status="assigned"))
    session.commit()
    offered = run(session, C.qual_choices(exclude_person_id=p.id))
    names = [n for _, n in offered]
    assert "Crane" in names and "Forklift" not in names


def test_update_person_qual_closes_and_appends(session):
    p = make_person(session, last_name="Tanner")
    q = make_qual(session, "Boat Crew", validity_period_days=730)
    session.commit()
    run(session, C.assign_quals(p.id, qual_ids=[q.id], status="in_progress"))
    session.commit()
    pq = next(r for r in session.get(M.Person, p.id).quals if r.valid_to is None)
    run(session, C.update_person_qual(
        p.id, pq.id, status="qualified", achieved_at="2026-03-01",
        effective_date="2026-03-01"))
    session.commit()
    rows = session.get(M.Person, p.id).quals
    current = [r for r in rows if r.valid_to is None]
    closed = [r for r in rows if r.valid_to is not None]
    assert len(current) == 1 and current[0].status == "qualified"
    assert current[0].expires_at == datetime(2028, 2, 29)  # +730d
    assert closed and closed[0].status == "in_progress"


def test_update_person_qual_rejects_historical_row(session):
    p = make_person(session, last_name="Banks")
    q = make_qual(session, "Diver")
    session.commit()
    run(session, C.assign_quals(p.id, qual_ids=[q.id], status="assigned"))
    session.commit()
    pq = next(r for r in session.get(M.Person, p.id).quals if r.valid_to is None)
    # Close it by updating once...
    run(session, C.update_person_qual(p.id, pq.id, status="qualified"))
    session.commit()
    # ...then editing the now-historical row must fail.
    with pytest.raises(C.ValidationError):
        run(session, C.update_person_qual(p.id, pq.id, status="dinq"))


# -- Qualification catalog --------------------------------------------------


def test_create_update_archive_qual(session):
    qid = run(session, C.create_qual("  Forklift  "))
    session.commit()
    q = session.get(M.Qualification, qid)
    assert q.name == "Forklift" and q.active is True
    run(session, C.update_qual(qid, "Forklift L2"))
    session.commit()
    assert session.get(M.Qualification, qid).name == "Forklift L2"
    run(session, C.archive_qual(qid, "obsolete"))
    session.commit()
    q = session.get(M.Qualification, qid)
    assert q.active is False and q.archived_reason == "obsolete"


def test_create_qual_requires_name(session):
    with pytest.raises(C.ValidationError):
        run(session, C.create_qual("   "))


# -- Task assignments -------------------------------------------------------


def test_add_assignment_demotes_prior_lead(session):
    p1 = make_person(session, last_name="A", display_order=0)
    p2 = make_person(session, last_name="B", display_order=1)
    monday = date.today() - timedelta(days=date.today().weekday())
    wl = make_worklist(session, monday)
    session.commit()
    tid = run(session, C.create_task(wl.id, name="Job", person_ids=[p1.id]))
    session.commit()
    # p1 is currently lead; adding p2 as lead must demote p1.
    run(session, C.add_assignment(tid, person_id=p2.id, is_poic=True))
    session.commit()
    inst = session.get(M.TaskInstance, tid)
    leads = [a for a in inst.assignments if a.is_poic and a.active]
    assert len(leads) == 1 and leads[0].person_id == p2.id


def test_add_external_lead_and_remove(session):
    p1 = make_person(session, last_name="A")
    monday = date.today() - timedelta(days=date.today().weekday())
    wl = make_worklist(session, monday)
    session.commit()
    tid = run(session, C.create_task(wl.id, name="Job", person_ids=[p1.id]))
    session.commit()
    run(session, C.add_assignment(tid, external_poic_name="  Outside Lead  "))
    session.commit()
    inst = session.get(M.TaskInstance, tid)
    ext = [a for a in inst.assignments if a.external_poic_name]
    assert ext and ext[0].external_poic_name == "Outside Lead"
    # Remove an assignment (soft delete).
    aid = inst.assignments[0].id
    run(session, C.remove_assignment(tid, aid))
    session.commit()
    assert session.get(M.TaskAssignment, aid).active is False


def test_add_assignment_requires_subject(session):
    p1 = make_person(session, last_name="A")
    monday = date.today() - timedelta(days=date.today().weekday())
    wl = make_worklist(session, monday)
    session.commit()
    tid = run(session, C.create_task(wl.id, name="Job", person_ids=[p1.id]))
    session.commit()
    with pytest.raises(C.ValidationError):
        run(session, C.add_assignment(tid))


def test_set_assignment_poic(session):
    p1 = make_person(session, last_name="A", display_order=0)
    p2 = make_person(session, last_name="B", display_order=1)
    monday = date.today() - timedelta(days=date.today().weekday())
    wl = make_worklist(session, monday)
    session.commit()
    tid = run(session, C.create_task(wl.id, name="Job", person_ids=[p1.id, p2.id]))
    session.commit()
    inst = session.get(M.TaskInstance, tid)
    p2_assign = next(a for a in inst.assignments if a.person_id == p2.id)
    run(session, C.set_assignment_poic(tid, p2_assign.id))
    session.commit()
    inst = session.get(M.TaskInstance, tid)
    leads = [a for a in inst.assignments if a.is_poic and a.active]
    assert len(leads) == 1 and leads[0].person_id == p2.id


# -- Task templates ---------------------------------------------------------


def test_create_template_with_recurrence_and_quals(session):
    q = make_qual(session, "Crane")
    session.commit()
    rec = C._build_recurrence("weekdays", weekdays=[0, 2, 4])
    tid = run(session, C.create_template(
        name="Field Day", recurrence=rec, required_quals=[q.id],
        carry_over_policy="never", estimated_hours=2.5))
    session.commit()
    t = session.get(M.TaskTemplate, tid)
    assert t.recurrence_rule == {"kind": "weekdays", "weekdays": [0, 2, 4]}
    assert t.carry_over_policy == "never" and t.estimated_hours == 2.5
    req = session.scalars(
        select(M.TaskTemplateRequiredQual.qual_id).where(
            M.TaskTemplateRequiredQual.task_template_id == tid)).all()
    assert list(req) == [q.id]


def test_update_template_resets_required_quals(session):
    q1 = make_qual(session, "A")
    q2 = make_qual(session, "B")
    session.commit()
    tid = run(session, C.create_template(name="T", required_quals=[q1.id]))
    session.commit()
    run(session, C.update_template(tid, name="T2", required_quals=[q2.id],
                                   recurrence=C._build_recurrence("daily")))
    session.commit()
    t = session.get(M.TaskTemplate, tid)
    assert t.name == "T2" and t.recurrence_rule == {"kind": "daily"}
    req = session.scalars(
        select(M.TaskTemplateRequiredQual.qual_id).where(
            M.TaskTemplateRequiredQual.task_template_id == tid)).all()
    assert list(req) == [q2.id]


def test_archive_template(session):
    tid = run(session, C.create_template(name="T"))
    session.commit()
    run(session, C.archive_template(tid, "done"))
    session.commit()
    assert session.get(M.TaskTemplate, tid).active is False


def test_build_recurrence_variants():
    assert C._build_recurrence("none") is None
    assert C._build_recurrence("daily") == {"kind": "daily"}
    assert C._build_recurrence("monthly_date", day=15) == {
        "kind": "monthly_date", "day": 15}
    assert C._build_recurrence("monthly_nth_weekday", n=2, weekday=3) == {
        "kind": "monthly_nth_weekday", "n": 2, "weekday": 3}


# -- Worklist update / amend / archive / carry-over -------------------------


def test_update_worklist_blocks_when_locked(session):
    monday = date.today() - timedelta(days=date.today().weekday())
    wl = make_worklist(session, monday, locked=True)
    session.commit()
    with pytest.raises(C.ValidationError):
        run(session, C.update_worklist(wl.id, name="x"))


def test_amend_clones_tasks_and_assignments(session):
    p1 = make_person(session, last_name="A")
    monday = date.today() - timedelta(days=date.today().weekday())
    wl = make_worklist(session, monday, locked=True)
    session.commit()
    # add a task + assignment directly to the locked parent for cloning
    tid = make_task(session, worklist_id=wl.id, name="Carry me")
    make_assignment(session, instance_id=tid.id, person_id=p1.id, is_poic=True)
    session.commit()
    new_id = run(session, C.amend_worklist(
        wl.id, amendment_reason="typo", operator_name="Chief"))
    session.commit()
    clone = session.get(M.Worklist, new_id)
    assert clone.parent_id == wl.id and clone.version == 2 and not clone.locked
    clone_tasks = session.scalars(
        select(M.TaskInstance).where(
            M.TaskInstance.worklist_id == new_id)).all()
    assert len(list(clone_tasks)) == 1
    ct = list(clone_tasks)[0]
    assert ct.name == "Carry me" and ct.carried_from_instance_id == tid.id
    assert any(a.is_poic and a.person_id == p1.id for a in ct.assignments)


def test_amend_requires_locked_and_reason(session):
    monday = date.today() - timedelta(days=date.today().weekday())
    wl = make_worklist(session, monday, locked=False)
    session.commit()
    with pytest.raises(C.ValidationError):
        run(session, C.amend_worklist(wl.id, amendment_reason="x"))
    wl.locked = True
    session.commit()
    with pytest.raises(C.ValidationError):
        run(session, C.amend_worklist(wl.id, amendment_reason="  "))


def test_archive_worklist(session):
    monday = date.today() - timedelta(days=date.today().weekday())
    wl = make_worklist(session, monday)
    session.commit()
    run(session, C.archive_worklist(wl.id, "old"))
    session.commit()
    assert session.get(M.Worklist, wl.id).active is False


def test_apply_carry_overs_carry_and_discard(session):
    p1 = make_person(session, last_name="A")
    last_monday = date.today() - timedelta(days=date.today().weekday() + 7)
    this_monday = date.today() - timedelta(days=date.today().weekday())
    prev = make_worklist(session, last_monday)
    cur = make_worklist(session, this_monday)
    session.commit()
    # two open tasks in the previous week
    t1 = make_task(session, worklist_id=prev.id, name="Carry task", status="open")
    make_assignment(session, instance_id=t1.id, person_id=p1.id, is_poic=True)
    t2 = make_task(session, worklist_id=prev.id, name="Drop task", status="open")
    session.commit()
    acted = run(session, C.apply_carry_overs(cur.id, {
        t1.id: {"action": "carry"},
        t2.id: {"action": "discard"},
    }))
    session.commit()
    assert acted == 2
    assert session.get(M.TaskInstance, t1.id).status == "carried"
    assert session.get(M.TaskInstance, t2.id).status == "discarded"
    carried = session.scalars(
        select(M.TaskInstance).where(
            M.TaskInstance.worklist_id == cur.id,
            M.TaskInstance.carried_from_instance_id == t1.id)).all()
    assert len(list(carried)) == 1
