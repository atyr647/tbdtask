from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..db import SessionLocal
from .. import models as M
from ..services.worklist_view import build_week_view
from ..templating import render

router = APIRouter()


def _next_monday(today: Optional[date] = None) -> date:
    today = today or date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _name_for(week_starting: date) -> str:
    """Approximate "Week N Month YYYY" label, matching the legacy convention."""
    first = week_starting.replace(day=1)
    first_monday = _monday_of(first) if first.weekday() == 0 else first + timedelta(days=(7 - first.weekday()) % 7)
    week_index = ((week_starting - first_monday).days // 7) + 1
    return f"Week {week_index} {week_starting.strftime('%B %Y')}"


@router.get("/worklists")
def list_worklists(request: Request):
    today = date.today()
    with SessionLocal() as s:
        all_lists = s.scalars(
            select(M.Worklist)
            .where(M.Worklist.active == True)  # noqa: E712
            .order_by(M.Worklist.week_starting.desc(), M.Worklist.version.desc())
        ).all()
    current = []
    upcoming = []
    archived = []
    for w in all_lists:
        end = w.week_starting + timedelta(days=6)
        if w.locked or end < today:
            archived.append(w)
        elif w.week_starting > today:
            upcoming.append(w)
        else:
            current.append(w)
    return render(
        request,
        "worklists/list.html",
        current=current, upcoming=upcoming, archived=archived,
        suggested_monday=_next_monday().isoformat(),
    )


@router.get("/worklists/new")
def new_worklist_form(request: Request):
    return render(
        request,
        "worklists/new.html",
        suggested_monday=_next_monday().isoformat(),
    )


@router.post("/worklists")
def create_worklist(
    week_starting: str = Form(...),
    name: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    monday = date.fromisoformat(week_starting)
    if monday.weekday() != 0:
        monday = _monday_of(monday)
    with SessionLocal() as s:
        existing = s.scalar(
            select(M.Worklist).where(
                M.Worklist.week_starting == monday,
                M.Worklist.active == True,  # noqa: E712
                M.Worklist.parent_id.is_(None),
            )
        )
        if existing:
            return RedirectResponse(f"/worklists/{existing.id}", status_code=303)
        wl = M.Worklist(
            week_starting=monday,
            name=(name or _name_for(monday)).strip(),
            notes=(notes or None),
            version=1,
        )
        s.add(wl)
        s.commit()
        new_id = wl.id
    return RedirectResponse(f"/worklists/{new_id}", status_code=303)


@router.get("/worklists/{worklist_id}")
def show_worklist(worklist_id: int, request: Request):
    with SessionLocal() as s:
        wl = s.get(M.Worklist, worklist_id)
        if not wl:
            raise HTTPException(404)
        view = build_week_view(s, wl)
        categories = list(s.scalars(
            select(M.TaskCategory).where(M.TaskCategory.active == True).order_by(M.TaskCategory.display_order)  # noqa: E712
        ).all())
        people = list(s.scalars(
            select(M.Person).where(M.Person.active == True).order_by(M.Person.display_order)  # noqa: E712
        ).all())
    return render(
        request,
        "worklists/show.html",
        view=view, worklist=wl, categories=categories, people=people,
    )


@router.get("/worklists/{worklist_id}/print")
def print_worklist(worklist_id: int, request: Request):
    with SessionLocal() as s:
        wl = s.get(M.Worklist, worklist_id)
        if not wl:
            raise HTTPException(404)
        view = build_week_view(s, wl)
    return render(request, "worklists/print.html", view=view, worklist=wl)


@router.post("/worklists/{worklist_id}")
def update_worklist(
    worklist_id: int,
    name: str = Form(...),
    notes: Optional[str] = Form(None),
):
    with SessionLocal() as s:
        wl = s.get(M.Worklist, worklist_id)
        if not wl:
            raise HTTPException(404)
        if wl.locked:
            raise HTTPException(409, "worklist is locked; create an amendment instead")
        wl.name = name.strip()
        wl.notes = notes or None
        s.commit()
    return RedirectResponse(f"/worklists/{worklist_id}", status_code=303)


@router.post("/worklists/{worklist_id}/lock")
def lock_worklist(
    worklist_id: int,
    locked_by_name: Optional[str] = Form(None),
):
    with SessionLocal() as s:
        wl = s.get(M.Worklist, worklist_id)
        if not wl:
            raise HTTPException(404)
        if wl.locked:
            return RedirectResponse(f"/worklists/{worklist_id}", status_code=303)
        wl.locked = True
        wl.locked_at = datetime.now()
        wl.locked_by_name = (locked_by_name or None) and locked_by_name.strip()
        s.commit()
    return RedirectResponse(f"/worklists/{worklist_id}", status_code=303)


@router.post("/worklists/{worklist_id}/amend")
def amend_worklist(
    worklist_id: int,
    amendment_reason: str = Form(...),
    operator_name: Optional[str] = Form(None),
):
    """Clone a locked worklist into a new version that may be edited. The
    original snapshot remains locked. Tasks and assignments are duplicated
    so edits don't affect history."""
    with SessionLocal() as s:
        parent = s.get(M.Worklist, worklist_id)
        if not parent:
            raise HTTPException(404)
        if not parent.locked:
            raise HTTPException(409, "only locked worklists are amended")
        sibling_count = s.scalar(
            select(M.Worklist.version)
            .where(
                (M.Worklist.id == parent.id) |
                (M.Worklist.parent_id == parent.id)
            )
            .order_by(M.Worklist.version.desc())
            .limit(1)
        ) or 1
        clone = M.Worklist(
            week_starting=parent.week_starting,
            name=parent.name,
            version=sibling_count + 1,
            parent_id=parent.id,
            notes=parent.notes,
            amended_at=datetime.now(),
            amendment_reason=amendment_reason.strip(),
            operator_name=(operator_name or None) and operator_name.strip(),
        )
        s.add(clone)
        s.flush()
        # Duplicate active TaskInstances + their TaskAssignments.
        instances = s.scalars(
            select(M.TaskInstance)
            .where(M.TaskInstance.worklist_id == parent.id, M.TaskInstance.active == True)  # noqa: E712
            .options(selectinload(M.TaskInstance.assignments))
        ).all()
        for inst in instances:
            new_inst = M.TaskInstance(
                template_id=inst.template_id,
                worklist_id=clone.id,
                scheduled_date=inst.scheduled_date,
                category_id=inst.category_id,
                name=inst.name,
                description=inst.description,
                status=inst.status,
                notes=inst.notes,
                completion_notes=inst.completion_notes,
                completed_at=inst.completed_at,
                carried_from_instance_id=inst.id,
                display_order=inst.display_order,
            )
            s.add(new_inst)
            s.flush()
            for a in inst.assignments:
                if not a.active:
                    continue
                s.add(M.TaskAssignment(
                    instance_id=new_inst.id,
                    person_id=a.person_id,
                    is_poic=a.is_poic,
                    external_poic_name=a.external_poic_name,
                    completed=a.completed,
                    completion_notes=a.completion_notes,
                    hours_worked=a.hours_worked,
                    display_order=a.display_order,
                ))
        s.commit()
        new_id = clone.id
    return RedirectResponse(f"/worklists/{new_id}", status_code=303)


@router.post("/worklists/{worklist_id}/archive")
def archive_worklist(worklist_id: int, reason: str = Form("")):
    with SessionLocal() as s:
        wl = s.get(M.Worklist, worklist_id)
        if not wl:
            raise HTTPException(404)
        wl.active = False
        wl.archived_at = datetime.now()
        wl.archived_reason = reason or None
        s.commit()
    return RedirectResponse("/worklists", status_code=303)
