from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..auth.authorization import require
from ..auth.permissions import P_TASKS_ARCHIVE, P_TASKS_WRITE
from ..db import SessionLocal
from .. import models as M
from ..services.assignment_helper import candidates_for
from ..templating import render

router = APIRouter()


def _ensure_unlocked(wl: Optional[M.Worklist]) -> None:
    if wl is None:
        raise HTTPException(404)
    if wl.locked:
        raise HTTPException(409, "worklist is locked; amend it before editing tasks")


@router.post("/worklists/{worklist_id}/tasks")
async def create_task(
    worklist_id: int,
    request: Request,
    _: None = Depends(require(P_TASKS_WRITE)),
):
    """Create a TaskInstance and (optionally) attach assignees in one shot.

    Form fields:
      name (required), scheduled_date, category_id, description, notes
      person_ids (multi)  - assignees from the roster
      poic_person_id      - which assignee to mark as task lead (if any)
      external_poic_name  - optional off-roster task lead
      next                - "setup" to redirect back to the wizard,
                            otherwise lands on the worklist show page
    """
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    with SessionLocal() as s:
        wl = s.get(M.Worklist, worklist_id)
        _ensure_unlocked(wl)
        scheduled_iso = form.get("scheduled_date") or None
        category_raw = form.get("category_id") or None
        inst = M.TaskInstance(
            worklist_id=worklist_id,
            scheduled_date=date.fromisoformat(scheduled_iso) if scheduled_iso else None,
            category_id=int(category_raw) if category_raw else None,
            name=name[:240],
            description=(form.get("description") or None),
            notes=(form.get("notes") or None),
            status="open",
            org_id=wl.org_id,
        )
        s.add(inst)
        s.flush()
        person_ids = [int(x) for x in form.getlist("person_ids") if str(x).strip()]
        poic_id = form.get("poic_person_id")
        poic_id_int = int(poic_id) if poic_id else None
        ext = (form.get("external_poic_name") or "").strip()
        # Multi-assignment guarantee: if more than one person is on the task
        # and the operator didn't pick a lead, default to the first assignee
        # so the task always has someone in charge.
        if len(person_ids) >= 2 and poic_id_int is None and not ext:
            poic_id_int = person_ids[0]
        # Single-assignment convenience: a one-person task with no explicit
        # Lead choice silently makes that person the task lead.
        if len(person_ids) == 1 and poic_id_int is None and not ext:
            poic_id_int = person_ids[0]
        for pid in person_ids:
            s.add(M.TaskAssignment(
                instance_id=inst.id,
                person_id=pid,
                is_poic=(pid == poic_id_int),
                org_id=wl.org_id,
            ))
        if ext:
            s.add(M.TaskAssignment(
                instance_id=inst.id,
                external_poic_name=ext,
                is_poic=True,
                org_id=wl.org_id,
            ))
        s.commit()
    redirect_to = f"/worklists/{worklist_id}/setup" if form.get("next") == "setup" else f"/worklists/{worklist_id}"
    return RedirectResponse(redirect_to, status_code=303)


@router.get("/tasks/{task_id}/edit")
def edit_task_form(
    task_id: int,
    request: Request,
    _: None = Depends(require(P_TASKS_WRITE)),
):
    with SessionLocal() as s:
        inst = s.get(M.TaskInstance, task_id)
        if not inst:
            raise HTTPException(404)
        wl = s.get(M.Worklist, inst.worklist_id) if inst.worklist_id else None
        categories = list(s.scalars(
            select(M.TaskCategory).where(M.TaskCategory.active == True).order_by(M.TaskCategory.display_order)  # noqa: E712
        ).all())
        people = list(s.scalars(
            select(M.Person).where(M.Person.active == True).order_by(M.Person.display_order)  # noqa: E712
        ).all())
        assignments = list(s.scalars(
            select(M.TaskAssignment)
            .where(M.TaskAssignment.instance_id == inst.id, M.TaskAssignment.active == True)  # noqa: E712
            .options(selectinload(M.TaskAssignment.person))
        ).all())
        suggestions = candidates_for(s, inst)[:6] if inst.template_id else []
    return render(
        request,
        "tasks/edit.html",
        instance=inst, worklist=wl, categories=categories, people=people,
        assignments=assignments, suggestions=suggestions,
    )


@router.post("/tasks/{task_id}")
def update_task(
    task_id: int,
    name: str = Form(...),
    scheduled_date: Optional[str] = Form(None),
    category_id: Optional[int] = Form(None),
    status: str = Form("open"),
    hours: Optional[float] = Form(None),
    description: Optional[str] = Form(None),
    completion_notes: Optional[str] = Form(None),
    _: None = Depends(require(P_TASKS_WRITE)),
):
    with SessionLocal() as s:
        inst = s.get(M.TaskInstance, task_id)
        if not inst:
            raise HTTPException(404)
        wl = s.get(M.Worklist, inst.worklist_id) if inst.worklist_id else None
        _ensure_unlocked(wl)
        inst.name = name.strip()[:240]
        inst.scheduled_date = date.fromisoformat(scheduled_date) if scheduled_date else None
        inst.category_id = category_id or None
        inst.status = status
        inst.hours = hours
        inst.description = (description or None)
        inst.notes = None  # consolidated into description
        inst.completion_notes = (completion_notes or None)
        if status == "done" and inst.completed_at is None:
            inst.completed_at = datetime.now()
        if status != "done":
            inst.completed_at = None
        s.commit()
        wl_id = inst.worklist_id
    return RedirectResponse(f"/worklists/{wl_id}" if wl_id else "/worklists", status_code=303)


@router.post("/tasks/{task_id}/archive")
def archive_task(
    task_id: int,
    reason: str = Form(""),
    _: None = Depends(require(P_TASKS_ARCHIVE)),
):
    with SessionLocal() as s:
        inst = s.get(M.TaskInstance, task_id)
        if not inst:
            raise HTTPException(404)
        wl = s.get(M.Worklist, inst.worklist_id) if inst.worklist_id else None
        _ensure_unlocked(wl)
        inst.active = False
        inst.archived_at = datetime.now()
        inst.archived_reason = reason or None
        s.commit()
        wl_id = inst.worklist_id
    return RedirectResponse(f"/worklists/{wl_id}" if wl_id else "/worklists", status_code=303)


@router.post("/tasks/{task_id}/assignments")
def add_assignment(
    task_id: int,
    person_id: Optional[int] = Form(None),
    external_poic_name: Optional[str] = Form(None),
    is_poic: Optional[str] = Form(None),
    _: None = Depends(require(P_TASKS_WRITE)),
):
    if not person_id and not external_poic_name:
            raise HTTPException(400, "must provide a person or an external lead name")
    with SessionLocal() as s:
        inst = s.get(M.TaskInstance, task_id)
        if not inst:
            raise HTTPException(404)
        wl = s.get(M.Worklist, inst.worklist_id) if inst.worklist_id else None
        _ensure_unlocked(wl)
        is_poic_bool = bool(is_poic)
        if is_poic_bool:
            # Demote any prior lead for this instance.
            existing = s.scalars(
                select(M.TaskAssignment).where(
                    M.TaskAssignment.instance_id == inst.id,
                    M.TaskAssignment.is_poic == True,  # noqa: E712
                    M.TaskAssignment.active == True,  # noqa: E712
                )
            ).all()
            for a in existing:
                a.is_poic = False
        s.add(M.TaskAssignment(
            instance_id=inst.id,
            person_id=person_id or None,
            external_poic_name=(external_poic_name or None) and external_poic_name.strip(),
            is_poic=is_poic_bool,
            org_id=inst.org_id,
        ))
        s.commit()
        wl_id = inst.worklist_id
    return RedirectResponse(f"/tasks/{task_id}/edit", status_code=303)


@router.post("/tasks/{task_id}/assignments/{assignment_id}")
def update_assignment(
    task_id: int,
    assignment_id: int,
    is_poic: Optional[str] = Form(None),
    completed: Optional[str] = Form(None),
    hours_worked: Optional[float] = Form(None),
    completion_notes: Optional[str] = Form(None),
    _: None = Depends(require(P_TASKS_WRITE)),
):
    with SessionLocal() as s:
        a = s.get(M.TaskAssignment, assignment_id)
        if not a or a.instance_id != task_id:
            raise HTTPException(404)
        inst = s.get(M.TaskInstance, task_id)
        wl = s.get(M.Worklist, inst.worklist_id) if inst.worklist_id else None
        _ensure_unlocked(wl)
        new_poic = bool(is_poic)
        if new_poic and not a.is_poic:
            existing = s.scalars(
                select(M.TaskAssignment).where(
                    M.TaskAssignment.instance_id == inst.id,
                    M.TaskAssignment.is_poic == True,  # noqa: E712
                    M.TaskAssignment.active == True,  # noqa: E712
                )
            ).all()
            for other in existing:
                other.is_poic = False
        a.is_poic = new_poic
        a.completed = bool(completed)
        a.hours_worked = hours_worked
        a.completion_notes = (completion_notes or None)
        s.commit()
    return RedirectResponse(f"/tasks/{task_id}/edit", status_code=303)


@router.post("/tasks/{task_id}/assignments/{assignment_id}/delete")
def delete_assignment(
    task_id: int,
    assignment_id: int,
    _: None = Depends(require(P_TASKS_WRITE)),
):
    with SessionLocal() as s:
        a = s.get(M.TaskAssignment, assignment_id)
        if not a or a.instance_id != task_id:
            raise HTTPException(404)
        inst = s.get(M.TaskInstance, task_id)
        wl = s.get(M.Worklist, inst.worklist_id) if inst.worklist_id else None
        _ensure_unlocked(wl)
        a.active = False
        a.archived_at = datetime.now()
        s.commit()
    return RedirectResponse(f"/tasks/{task_id}/edit", status_code=303)
