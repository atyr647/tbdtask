"""Tests for the recurring task generator."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from app import models as M
from app.services.task_generator import generate_for_worklist
from tests.conftest import make_task_categories, make_worklist


def _make_template(
    session, *, name, rule, estimated_hours=None, category_id=None, org_id=1
):
    t = M.TaskTemplate(
        name=name,
        recurrence_rule=rule,
        estimated_hours=estimated_hours,
        category_id=category_id,
        org_id=org_id,
    )
    session.add(t)
    session.flush()
    return t


def test_daily_template_generates_seven_instances(session):
    cats = make_task_categories(session)
    _make_template(
        session, name="Quarters", rule={"kind": "daily"}, category_id=cats["General"].id
    )
    wl = make_worklist(session, date(2026, 5, 4))
    session.commit()

    created = generate_for_worklist(session, wl)
    session.commit()
    assert created == 7
    instances = list(
        session.scalars(
            select(M.TaskInstance)
            .where(M.TaskInstance.worklist_id == wl.id)
            .order_by(M.TaskInstance.scheduled_date)
        ).all()
    )
    assert [i.scheduled_date.weekday() for i in instances] == [0, 1, 2, 3, 4, 5, 6]


def test_weekday_template_generates_only_chosen_days(session):
    _make_template(
        session, name="MWF", rule={"kind": "weekdays", "weekdays": [0, 2, 4]}
    )
    wl = make_worklist(session, date(2026, 5, 4))
    session.commit()

    generate_for_worklist(session, wl)
    session.commit()
    instances = list(
        session.scalars(
            select(M.TaskInstance).where(M.TaskInstance.worklist_id == wl.id)
        ).all()
    )
    weekdays = sorted(i.scheduled_date.weekday() for i in instances)
    assert weekdays == [0, 2, 4]


def test_generated_instance_inherits_estimated_hours(session):
    """Regression: the generator must copy estimated_hours -> instance.hours."""
    _make_template(
        session,
        name="Long task",
        rule={"kind": "weekdays", "weekdays": [0]},
        estimated_hours=4.0,
    )
    wl = make_worklist(session, date(2026, 5, 4))
    session.commit()

    generate_for_worklist(session, wl)
    session.commit()
    inst = session.scalars(
        select(M.TaskInstance).where(M.TaskInstance.worklist_id == wl.id)
    ).first()
    assert inst is not None
    assert inst.hours == 4.0


def test_generation_is_idempotent(session):
    _make_template(session, name="Quarters", rule={"kind": "daily"})
    wl = make_worklist(session, date(2026, 5, 4))
    session.commit()

    first = generate_for_worklist(session, wl)
    session.commit()
    second = generate_for_worklist(session, wl)
    session.commit()
    assert first == 7
    assert second == 0  # no duplicates on second run
    total = session.scalar(
        select(__import__("sqlalchemy").func.count())
        .select_from(M.TaskInstance)
        .where(M.TaskInstance.worklist_id == wl.id)
    )
    assert total == 7


def test_generator_skips_locked_worklists(session):
    _make_template(session, name="Quarters", rule={"kind": "daily"})
    wl = make_worklist(session, date(2026, 5, 4), locked=True)
    session.commit()

    created = generate_for_worklist(session, wl)
    session.commit()
    assert created == 0


def test_inactive_template_not_generated(session):
    t = _make_template(session, name="Quarters", rule={"kind": "daily"})
    t.active = False
    wl = make_worklist(session, date(2026, 5, 4))
    session.commit()

    created = generate_for_worklist(session, wl)
    session.commit()
    assert created == 0
