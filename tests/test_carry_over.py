"""Tests for the carry-forward flow."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from app import models as M
from app.services.carry_over import apply_carry_over, find_pending_carry_overs
from tests.conftest import (
    make_assignment, make_person, make_task, make_task_categories,
    make_worklist,
)


def _setup(session):
    cats = make_task_categories(session)
    p1 = make_person(session, last_name="Tanner", display_order=0)
    p2 = make_person(session, last_name="Mason", display_order=1)
    return cats, p1, p2


def test_carry_preserves_task_hours(session):
    """Regression: cloned tasks must keep their estimated hours."""
    cats, p1, _ = _setup(session)
    monday_a = date(2026, 5, 4)
    monday_b = date(2026, 5, 11)
    wl_a = make_worklist(session, monday_a, locked=True)
    wl_b = make_worklist(session, monday_b)
    inst = make_task(session, worklist_id=wl_a.id, scheduled_date=monday_a,
                     name="Replace seatbelts on 947",
                     status="open", hours=2.5, category_id=cats["Maintenance"].id)
    make_assignment(session, instance_id=inst.id, person_id=p1.id, is_poic=True)
    session.commit()

    candidates = find_pending_carry_overs(session, wl_b)
    assert len(candidates) == 1
    new_inst = apply_carry_over(session, wl_b, candidates[0], "carry")
    session.commit()

    assert new_inst is not None
    assert new_inst.hours == 2.5
    assert new_inst.carried_from_instance_id == inst.id
    # Original transitions to 'carried'
    session.refresh(inst)
    assert inst.status == "carried"


def test_reassign_preserves_hours_and_replaces_assignees(session):
    cats, p1, p2 = _setup(session)
    wl_a = make_worklist(session, date(2026, 5, 4), locked=True)
    wl_b = make_worklist(session, date(2026, 5, 11))
    inst = make_task(session, worklist_id=wl_a.id,
                     scheduled_date=date(2026, 5, 4),
                     name="Order parts", status="in_progress", hours=1.5)
    make_assignment(session, instance_id=inst.id, person_id=p1.id, is_poic=True)
    session.commit()

    candidates = find_pending_carry_overs(session, wl_b)
    new_inst = apply_carry_over(session, wl_b, candidates[0], "reassign",
                                reassign_person_ids=[p2.id],
                                new_poic_person_id=p2.id)
    session.commit()

    assert new_inst.hours == 1.5
    new_assignments = list(session.scalars(
        select(M.TaskAssignment).where(M.TaskAssignment.instance_id == new_inst.id)
    ).all())
    assert len(new_assignments) == 1
    assert new_assignments[0].person_id == p2.id
    assert new_assignments[0].is_poic is True


def test_complete_marks_original_done_and_does_not_clone(session):
    _, p1, _ = _setup(session)
    wl_a = make_worklist(session, date(2026, 5, 4), locked=True)
    wl_b = make_worklist(session, date(2026, 5, 11))
    inst = make_task(session, worklist_id=wl_a.id, name="Stale task")
    make_assignment(session, instance_id=inst.id, person_id=p1.id)
    session.commit()

    candidates = find_pending_carry_overs(session, wl_b)
    result = apply_carry_over(session, wl_b, candidates[0], "complete")
    session.commit()

    assert result is None  # nothing carried
    session.refresh(inst)
    assert inst.status == "done"
    assert inst.completed_at is not None
    # No instance was created on the target worklist
    targets = list(session.scalars(
        select(M.TaskInstance).where(M.TaskInstance.worklist_id == wl_b.id)
    ).all())
    assert targets == []


def test_discard_marks_original_discarded(session):
    _, p1, _ = _setup(session)
    wl_a = make_worklist(session, date(2026, 5, 4), locked=True)
    wl_b = make_worklist(session, date(2026, 5, 11))
    inst = make_task(session, worklist_id=wl_a.id, name="No longer needed")
    make_assignment(session, instance_id=inst.id, person_id=p1.id)
    session.commit()

    candidates = find_pending_carry_overs(session, wl_b)
    apply_carry_over(session, wl_b, candidates[0], "discard")
    session.commit()

    session.refresh(inst)
    assert inst.status == "discarded"


def test_leave_does_not_change_anything(session):
    _, p1, _ = _setup(session)
    wl_a = make_worklist(session, date(2026, 5, 4), locked=True)
    wl_b = make_worklist(session, date(2026, 5, 11))
    inst = make_task(session, worklist_id=wl_a.id, name="Still pending",
                     status="in_progress")
    make_assignment(session, instance_id=inst.id, person_id=p1.id)
    session.commit()

    candidates = find_pending_carry_overs(session, wl_b)
    apply_carry_over(session, wl_b, candidates[0], "leave")
    session.commit()

    session.refresh(inst)
    assert inst.status == "in_progress"
    # Still surfaces as a candidate next time too
    again = find_pending_carry_overs(session, wl_b)
    assert len(again) == 1


def test_pending_excludes_done_and_discarded(session):
    _, p1, _ = _setup(session)
    wl_a = make_worklist(session, date(2026, 5, 4), locked=True)
    wl_b = make_worklist(session, date(2026, 5, 11))
    make_task(session, worklist_id=wl_a.id, name="Done", status="done")
    make_task(session, worklist_id=wl_a.id, name="Discarded", status="discarded")
    make_task(session, worklist_id=wl_a.id, name="Open", status="open")
    session.commit()

    pending = find_pending_carry_overs(session, wl_b)
    assert [c.instance.name for c in pending] == ["Open"]


def test_pending_only_pulls_from_earlier_weeks(session):
    """A task in a future-dated worklist must not surface as carry-over."""
    _, _, _ = _setup(session)
    target = make_worklist(session, date(2026, 5, 11))
    future = make_worklist(session, date(2026, 5, 18))
    make_task(session, worklist_id=future.id, name="Later-week task", status="open")
    session.commit()

    assert find_pending_carry_overs(session, target) == []
