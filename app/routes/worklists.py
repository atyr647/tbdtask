from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..auth.authorization import require
from ..auth.permissions import (
    P_WORKLISTS_AMEND,
    P_WORKLISTS_ARCHIVE,
    P_WORKLISTS_LOCK,
    P_WORKLISTS_VIEW,
    P_WORKLISTS_WRITE,
)
from ..db import SessionLocal
from .. import models as M
from ..services.carry_over import apply_carry_over, find_pending_carry_overs
from ..services.task_generator import generate_for_worklist
from ..services.worklist_view import build_week_view, build_week_grid
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
def list_worklists(request: Request, _: None = Depends(require(P_WORKLISTS_VIEW))):
    today = date.today()
    with SessionLocal() as s:
        all_lists = list(s.scalars(
            select(M.Worklist)
            .where(M.Worklist.active == True)  # noqa: E712
            .order_by(M.Worklist.week_starting.desc(), M.Worklist.version.desc())
        ).all())
    # Group amendments under their base worklist.
    children: dict[int, list[M.Worklist]] = {}
    bases: list[M.Worklist] = []
    for w in all_lists:
        if w.parent_id:
            children.setdefault(w.parent_id, []).append(w)
        else:
            bases.append(w)
    current: list[tuple[M.Worklist, list[M.Worklist]]] = []
    upcoming: list[tuple[M.Worklist, list[M.Worklist]]] = []
    archived_by_month: dict[str, list[tuple[M.Worklist, list[M.Worklist]]]] = {}
    for w in bases:
        kids = sorted(children.get(w.id, []), key=lambda c: c.version, reverse=True)
        end = w.week_starting + timedelta(days=6)
        is_archived = w.locked or end < today
        bucket = (w, kids)
        if is_archived:
            ym = w.week_starting.strftime("%Y-%m %B")
            archived_by_month.setdefault(ym, []).append(bucket)
        elif w.week_starting > today:
            upcoming.append(bucket)
        else:
            current.append(bucket)
    archived_groups = sorted(archived_by_month.items(), reverse=True)
    return render(
        request,
        "worklists/list.html",
        current=current, upcoming=upcoming,
        archived_groups=archived_groups,
        suggested_monday=_next_monday().isoformat(),
    )


@router.get("/worklists/new")
def new_worklist_form(request: Request, _: None = Depends(require(P_WORKLISTS_WRITE))):
    """Week picker — creates a worklist and lands on the setup page."""
    return render(
        request,
        "worklists/new.html",
        suggested_monday=_next_monday().isoformat(),
    )


@router.post("/worklists")
def create_worklist(
    week_starting: str = Form(...),
    _: None = Depends(require(P_WORKLISTS_WRITE)),
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
            return RedirectResponse(f"/worklists/{existing.id}/setup", status_code=303)
        wl = M.Worklist(
            week_starting=monday,
            name=_name_for(monday),
            version=1,
        )
        s.add(wl)
        s.flush()
        # Auto-generate recurring tasks for the new week.
        generate_for_worklist(s, wl)
        s.commit()
        new_id = wl.id
    # Land in setup mode so the operator can fill in tasks for the week
    # without first having to read the show page.
    return RedirectResponse(f"/worklists/{new_id}/setup", status_code=303)


@router.post("/worklists/{worklist_id}/generate")
def generate_worklist(
    worklist_id: int,
    _: None = Depends(require(P_WORKLISTS_WRITE)),
):
    with SessionLocal() as s:
        wl = s.get(M.Worklist, worklist_id)
        if not wl:
            raise HTTPException(404)
        if wl.locked:
            raise HTTPException(409, "amend the worklist before generating tasks")
        generate_for_worklist(s, wl)
        s.commit()
    return RedirectResponse(f"/worklists/{worklist_id}", status_code=303)


@router.get("/worklists/{worklist_id}")
def show_worklist(
    worklist_id: int,
    request: Request,
    _: None = Depends(require(P_WORKLISTS_VIEW)),
):
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
        pending = find_pending_carry_overs(s, wl) if not wl.locked else []
    return render(
        request,
        "worklists/show.html",
        view=view, worklist=wl, categories=categories, people=people,
        pending_count=len(pending),
    )


@router.get("/worklists/{worklist_id}/carry-over")
def carry_over_form(
    worklist_id: int,
    request: Request,
    _: None = Depends(require(P_WORKLISTS_WRITE)),
):
    with SessionLocal() as s:
        wl = s.get(M.Worklist, worklist_id)
        if not wl:
            raise HTTPException(404)
        if wl.locked:
            raise HTTPException(409, "amend the worklist before processing carry-overs")
        candidates = find_pending_carry_overs(s, wl)
        people = list(s.scalars(
            select(M.Person).where(M.Person.active == True).order_by(M.Person.display_order)  # noqa: E712
        ).all())
        # Pre-resolve display info for candidates while in session.
        resolved = []
        for c in candidates:
            assignee_labels = []
            for a in c.assignments:
                if a.person:
                    assignee_labels.append(a.person.full_display)
                elif a.external_poic_name:
                    assignee_labels.append(f"(ext) {a.external_poic_name}")
            resolved.append({
                "candidate": c,
                "assignee_labels": assignee_labels,
                "source_label": c.source_worklist.name if c.source_worklist else "—",
                "scheduled": c.instance.scheduled_date,
            })
    return render(
        request,
        "worklists/carry_over.html",
        worklist=wl, items=resolved, people=people,
    )


@router.post("/worklists/{worklist_id}/carry-over")
async def carry_over_apply(
    worklist_id: int,
    request: Request,
    _: None = Depends(require(P_WORKLISTS_WRITE)),
):
    form = await request.form()
    with SessionLocal() as s:
        wl = s.get(M.Worklist, worklist_id)
        if not wl:
            raise HTTPException(404)
        if wl.locked:
            raise HTTPException(409, "amend the worklist before processing carry-overs")
        candidates = find_pending_carry_overs(s, wl)
        for c in candidates:
            iid = c.instance.id
            action = form.get(f"action_{iid}", "leave")
            if action == "reassign":
                pids_raw = form.getlist(f"reassign_to_{iid}")
                pids = [int(x) for x in pids_raw if str(x).strip()]
                poic = form.get(f"poic_{iid}")
                poic_id = int(poic) if poic else None
                apply_carry_over(
                    s, wl, c, "reassign",
                    reassign_person_ids=pids,
                    new_poic_person_id=poic_id,
                )
            else:
                apply_carry_over(s, wl, c, action)
        s.commit()
    return RedirectResponse(f"/worklists/{worklist_id}", status_code=303)


@router.get("/worklists/{worklist_id}/setup")
def setup_worklist(
    worklist_id: int,
    request: Request,
    _: None = Depends(require(P_WORKLISTS_WRITE)),
):
    """Wizard view: pick what tasks happen on each day and who's on them."""
    with SessionLocal() as s:
        wl = s.get(M.Worklist, worklist_id)
        if not wl:
            raise HTTPException(404)
        if wl.locked:
            return RedirectResponse(f"/worklists/{worklist_id}", status_code=303)
        view = build_week_view(s, wl)
        categories = list(s.scalars(
            select(M.TaskCategory).where(M.TaskCategory.active == True).order_by(M.TaskCategory.display_order)  # noqa: E712
        ).all())
        people = list(s.scalars(
            select(M.Person).where(M.Person.active == True).order_by(M.Person.display_order)  # noqa: E712
        ).all())
    return render(
        request,
        "worklists/setup.html",
        view=view, worklist=wl, categories=categories, people=people,
    )


@router.get("/worklists/{worklist_id}/print")
def print_worklist(
    worklist_id: int,
    request: Request,
    days: int = 5,
    _: None = Depends(require(P_WORKLISTS_VIEW)),
):
    if days not in (5, 7):
        days = 5
    with SessionLocal() as s:
        wl = s.get(M.Worklist, worklist_id)
        if not wl:
            raise HTTPException(404)
        grid = build_week_grid(s, wl, days=days)
    return render(request, "worklists/print.html", grid=grid, worklist=wl, days=days)


@router.post("/worklists/{worklist_id}")
def update_worklist(
    worklist_id: int,
    name: str = Form(...),
    notes: Optional[str] = Form(None),
    _: None = Depends(require(P_WORKLISTS_WRITE)),
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
    _: None = Depends(require(P_WORKLISTS_LOCK)),
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
    _: None = Depends(require(P_WORKLISTS_AMEND)),
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
            org_id=parent.org_id,
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
                hours=inst.hours,
                notes=inst.notes,
                completion_notes=inst.completion_notes,
                completed_at=inst.completed_at,
                carried_from_instance_id=inst.id,
                display_order=inst.display_order,
                org_id=clone.org_id,
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
                    org_id=clone.org_id,
                ))
        s.commit()
        new_id = clone.id
    return RedirectResponse(f"/worklists/{new_id}", status_code=303)


@router.post("/worklists/{worklist_id}/archive")
def archive_worklist(
    worklist_id: int,
    reason: str = Form(""),
    _: None = Depends(require(P_WORKLISTS_ARCHIVE)),
):
    with SessionLocal() as s:
        wl = s.get(M.Worklist, worklist_id)
        if not wl:
            raise HTTPException(404)
        wl.active = False
        wl.archived_at = datetime.now()
        wl.archived_reason = reason or None
        s.commit()
    return RedirectResponse("/worklists", status_code=303)
