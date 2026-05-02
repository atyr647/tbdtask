"""End-to-end tests for lock/amend immutability via the HTTP layer."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from app import db as db_module
from app import models as M


def test_locked_worklist_rejects_task_create(client, session_factory):
    """POSTing a task to a locked worklist should return 409, no insert."""
    monday = date(2026, 5, 4)
    with session_factory() as s:
        wl = M.Worklist(week_starting=monday, name="Locked week", locked=True, org_id=1)
        s.add(wl); s.commit()
        wl_id = wl.id

    resp = client.post(f"/worklists/{wl_id}/tasks",
                       data={"name": "Should not be created"},
                       follow_redirects=False)
    assert resp.status_code == 409

    with session_factory() as s:
        rows = list(s.scalars(
            select(M.TaskInstance).where(M.TaskInstance.worklist_id == wl_id)
        ).all())
        assert rows == []


def test_locked_worklist_rejects_task_update(client, session_factory):
    monday = date(2026, 5, 4)
    with session_factory() as s:
        wl = M.Worklist(week_starting=monday, name="Locked week", locked=False, org_id=1)
        s.add(wl); s.flush()
        inst = M.TaskInstance(worklist_id=wl.id, name="Original", status="open", org_id=1)
        s.add(inst); s.commit()
        wl.locked = True
        s.commit()
        inst_id = inst.id

    resp = client.post(f"/tasks/{inst_id}",
                       data={"name": "tampered", "status": "done"},
                       follow_redirects=False)
    assert resp.status_code == 409

    with session_factory() as s:
        fresh = s.get(M.TaskInstance, inst_id)
        assert fresh.name == "Original"
        assert fresh.status == "open"


def test_locked_worklist_rejects_assignment_changes(client, session_factory):
    monday = date(2026, 5, 4)
    with session_factory() as s:
        p = M.Person(last_name="Doe", full_display="BM3 Doe", org_id=1)
        s.add(p); s.flush()
        s.add(M.PersonRosterStatus(person_id=p.id, status="active",
                                   valid_from=date.today(), org_id=1))
        wl = M.Worklist(week_starting=monday, name="W", locked=False, org_id=1)
        s.add(wl); s.flush()
        inst = M.TaskInstance(worklist_id=wl.id, name="Task", status="open", org_id=1)
        s.add(inst); s.flush()
        a = M.TaskAssignment(instance_id=inst.id, person_id=p.id, is_poic=True, org_id=1)
        s.add(a); s.commit()
        wl.locked = True
        s.commit()
        inst_id, a_id = inst.id, a.id

    resp = client.post(f"/tasks/{inst_id}/assignments/{a_id}/delete",
                       follow_redirects=False)
    assert resp.status_code == 409

    with session_factory() as s:
        fresh = s.get(M.TaskAssignment, a_id)
        assert fresh.active is True


def test_amend_clones_into_new_worklist_with_parent_link(client, session_factory):
    """Amending a locked worklist clones every task + assignment with hours."""
    monday = date(2026, 5, 4)
    with session_factory() as s:
        wl = M.Worklist(week_starting=monday, name="Original", locked=True, org_id=1)
        s.add(wl); s.flush()
        inst = M.TaskInstance(worklist_id=wl.id, name="Replace seatbelts",
                              status="open", hours=2.5,
                              scheduled_date=monday, org_id=1)
        s.add(inst); s.flush()
        p = M.Person(last_name="Tanner", full_display="CM2 Tanner", org_id=1)
        s.add(p); s.flush()
        s.add(M.PersonRosterStatus(person_id=p.id, status="active",
                                   valid_from=date.today(), org_id=1))
        s.add(M.TaskAssignment(instance_id=inst.id, person_id=p.id, is_poic=True, org_id=1))
        s.commit()
        wl_id = wl.id
        inst_id = inst.id
        person_id = p.id

    resp = client.post(f"/worklists/{wl_id}/amend",
                       data={"amendment_reason": "spelling fix",
                             "operator_name": "Op"},
                       follow_redirects=False)
    assert resp.status_code == 303
    new_url = resp.headers["location"]
    assert new_url.startswith("/worklists/")
    new_wl_id = int(new_url.rsplit("/", 1)[-1])
    assert new_wl_id != wl_id

    with session_factory() as s:
        new_wl = s.get(M.Worklist, new_wl_id)
        assert new_wl.parent_id == wl_id
        assert new_wl.version == 2
        assert new_wl.amendment_reason == "spelling fix"
        assert new_wl.operator_name == "Op"

        new_instances = list(s.scalars(
            select(M.TaskInstance).where(M.TaskInstance.worklist_id == new_wl_id)
        ).all())
        assert len(new_instances) == 1
        cloned = new_instances[0]
        assert cloned.name == "Replace seatbelts"
        assert cloned.hours == 2.5  # regression: hours preserved on amend
        assert cloned.carried_from_instance_id == inst_id
        assert cloned.scheduled_date == monday

        cloned_assignments = list(s.scalars(
            select(M.TaskAssignment)
            .where(M.TaskAssignment.instance_id == cloned.id)
        ).all())
        assert len(cloned_assignments) == 1
        assert cloned_assignments[0].person_id == person_id
        assert cloned_assignments[0].is_poic is True

        # Original worklist + its instance untouched.
        original = s.get(M.Worklist, wl_id)
        assert original.locked is True
        original_inst = s.get(M.TaskInstance, inst_id)
        assert original_inst.name == "Replace seatbelts"
        assert original_inst.status == "open"


def test_amend_rejects_unlocked_worklist(client, session_factory):
    monday = date(2026, 5, 4)
    with session_factory() as s:
        wl = M.Worklist(week_starting=monday, name="Open", locked=False, org_id=1)
        s.add(wl); s.commit()
        wl_id = wl.id

    resp = client.post(f"/worklists/{wl_id}/amend",
                       data={"amendment_reason": "early"},
                       follow_redirects=False)
    assert resp.status_code == 409


def test_lock_endpoint_makes_worklist_immutable(client, session_factory):
    """The lock endpoint flips the flag, after which task creates fail."""
    monday = date(2026, 5, 4)
    with session_factory() as s:
        wl = M.Worklist(week_starting=monday, name="W", locked=False, org_id=1)
        s.add(wl); s.commit()
        wl_id = wl.id

    # Before lock: task create succeeds.
    pre = client.post(f"/worklists/{wl_id}/tasks",
                      data={"name": "Pre-lock task"},
                      follow_redirects=False)
    assert pre.status_code == 303

    # Lock it.
    lock = client.post(f"/worklists/{wl_id}/lock",
                       data={"locked_by_name": "Op"},
                       follow_redirects=False)
    assert lock.status_code == 303

    # After lock: task create rejected.
    post = client.post(f"/worklists/{wl_id}/tasks",
                       data={"name": "Post-lock task"},
                       follow_redirects=False)
    assert post.status_code == 409
