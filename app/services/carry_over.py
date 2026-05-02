"""
Carry-forward of incomplete tasks at week rollover.

Finds task instances from prior worklists that are still ``open`` or
``in_progress`` whose scheduled date precedes the target week, and lets
the operator decide per-task: carry as-is, reassign, mark complete, or
discard. Auto-carry to the same person is the default policy, with
per-template overrides (auto_any_qualified / never / manual_prompt) read
from the originating ``TaskTemplate`` when present.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import models as M


@dataclass
class CarryCandidate:
    instance: M.TaskInstance
    assignments: list[M.TaskAssignment]
    source_worklist: Optional[M.Worklist]
    suggested_action: str  # carry | discard | manual
    template_policy: Optional[str] = None  # auto_same_person | auto_any_qualified | never | manual_prompt
    same_person_assignees: list[M.Person] = field(default_factory=list)


def _policy_for(inst: M.TaskInstance) -> Optional[str]:
    if inst.template_id is None:
        return None
    # template object isn't eagerly loaded by callers; we rely on whatever
    # SQLAlchemy gives us through the relationship if it's already attached
    return None


def find_pending_carry_overs(
    session: Session,
    target_worklist: M.Worklist,
) -> list[CarryCandidate]:
    instances = list(
        session.scalars(
            select(M.TaskInstance)
            .where(
                M.TaskInstance.active == True,  # noqa: E712
                M.TaskInstance.worklist_id != target_worklist.id,
                M.TaskInstance.status.in_(("open", "in_progress")),
            )
            .options(
                selectinload(M.TaskInstance.assignments).selectinload(M.TaskAssignment.person),
            )
            .order_by(M.TaskInstance.scheduled_date.is_(None), M.TaskInstance.scheduled_date, M.TaskInstance.id)
        ).all()
    )
    # Cap to prior weeks (or any week before target.week_starting) to avoid
    # offering tasks from a parallel worklist still in the future.
    pending: list[CarryCandidate] = []
    target_start = target_worklist.week_starting
    worklist_cache: dict[int, M.Worklist] = {}
    template_cache: dict[int, M.TaskTemplate] = {}
    for inst in instances:
        wl_id = inst.worklist_id
        if wl_id is None:
            continue
        wl = worklist_cache.get(wl_id) or session.get(M.Worklist, wl_id)
        worklist_cache[wl_id] = wl
        if wl is None or wl.id == target_worklist.id:
            continue
        if wl.week_starting >= target_start:
            continue
        # Look up template policy (cached).
        policy: Optional[str] = None
        if inst.template_id is not None:
            tmpl = template_cache.get(inst.template_id) or session.get(M.TaskTemplate, inst.template_id)
            if tmpl is not None:
                template_cache[inst.template_id] = tmpl
                policy = tmpl.carry_over_policy
        suggested = "carry"
        if policy == "never":
            suggested = "discard"
        elif policy == "manual_prompt":
            suggested = "manual"
        active_assignments = [a for a in inst.assignments if a.active]
        same_persons = [a.person for a in active_assignments if a.person is not None]
        pending.append(CarryCandidate(
            instance=inst, assignments=active_assignments,
            source_worklist=wl, suggested_action=suggested,
            template_policy=policy, same_person_assignees=same_persons,
        ))
    return pending


def apply_carry_over(
    session: Session,
    target_worklist: M.Worklist,
    candidate: CarryCandidate,
    action: str,
    *,
    new_scheduled_date: Optional[date] = None,
    reassign_person_ids: Optional[list[int]] = None,
    new_poic_person_id: Optional[int] = None,
    notes: Optional[str] = None,
) -> Optional[M.TaskInstance]:
    """Process a single carry-over decision.

    Actions:
      * ``carry`` - clone the task into ``target_worklist`` keeping the same
        assignees; original goes to ``carried``.
      * ``reassign`` - clone with a new assignee list (and optional new POIC).
      * ``complete`` - mark the original ``done``; nothing carried.
      * ``discard`` - mark the original ``discarded``; nothing carried.
      * ``leave`` - no-op; original keeps its open/in-progress status.
    """
    inst = candidate.instance
    if action not in {"carry", "reassign", "complete", "discard", "leave"}:
        raise ValueError(f"unknown carry action: {action}")

    if action == "leave":
        return None
    if action == "complete":
        inst.status = "done"
        inst.completed_at = datetime.now()
        return None
    if action == "discard":
        inst.status = "discarded"
        return None

    new_inst = M.TaskInstance(
        template_id=inst.template_id,
        worklist_id=target_worklist.id,
        scheduled_date=new_scheduled_date or target_worklist.week_starting,
        category_id=inst.category_id,
        name=inst.name,
        description=inst.description,
        status="open",
        hours=inst.hours,
        notes=notes if notes is not None else inst.notes,
        completion_notes=None,
        completed_at=None,
        carried_from_instance_id=inst.id,
        display_order=inst.display_order,
    )
    session.add(new_inst)
    session.flush()

    if action == "carry":
        for a in candidate.assignments:
            session.add(M.TaskAssignment(
                instance_id=new_inst.id,
                person_id=a.person_id,
                external_poic_name=a.external_poic_name,
                is_poic=a.is_poic,
                display_order=a.display_order,
            ))
    else:  # reassign
        person_ids = reassign_person_ids or []
        for pid in person_ids:
            session.add(M.TaskAssignment(
                instance_id=new_inst.id,
                person_id=pid,
                is_poic=(pid == new_poic_person_id),
            ))

    inst.status = "carried"
    return new_inst
