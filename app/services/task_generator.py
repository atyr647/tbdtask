"""
Materialize TaskInstances for a worklist from active TaskTemplates.

Generation is idempotent: an instance is created only when no instance
already exists for the same (template_id, scheduled_date, worklist_id).
This way an operator can re-run "Generate recurring tasks" safely after
adding new templates without producing duplicates.
"""

from __future__ import annotations


from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as M
from .recurrence import dates_in_window


def generate_for_worklist(session: Session, worklist: M.Worklist, days: int = 7) -> int:
    """Returns the number of instances created."""
    if worklist.locked:
        return 0
    week_start = worklist.week_starting
    templates = list(
        session.scalars(
            select(M.TaskTemplate).where(
                M.TaskTemplate.active == True,  # noqa: E712
                M.TaskTemplate.recurrence_rule.is_not(None),
            )
        ).all()
    )
    created = 0
    for tmpl in templates:
        rule = tmpl.recurrence_rule or {}
        dates = dates_in_window(rule, week_start, days)
        for d in dates:
            existing = session.scalar(
                select(M.TaskInstance.id).where(
                    M.TaskInstance.worklist_id == worklist.id,
                    M.TaskInstance.template_id == tmpl.id,
                    M.TaskInstance.scheduled_date == d,
                    M.TaskInstance.active == True,  # noqa: E712
                )
            )
            if existing is not None:
                continue
            inst = M.TaskInstance(
                template_id=tmpl.id,
                worklist_id=worklist.id,
                scheduled_date=d,
                category_id=tmpl.category_id,
                name=tmpl.name,
                description=tmpl.description,
                status="open",
                hours=tmpl.estimated_hours,
                notes=tmpl.notes,
                org_id=worklist.org_id,
            )
            session.add(inst)
            created += 1
    if created:
        session.flush()
    return created
