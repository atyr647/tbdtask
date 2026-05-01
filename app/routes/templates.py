"""TaskTemplate CRUD: the originating definition for recurring tasks."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from ..db import SessionLocal
from .. import models as M
from ..services.recurrence import describe
from ..templating import render

router = APIRouter()


CARRY_OVER_POLICIES = (
    ("auto_same_person", "Auto carry to same person (default)"),
    ("auto_any_qualified", "Auto carry; reassign to any qualified personnel"),
    ("never", "Never carry — drop if not done"),
    ("manual_prompt", "Always prompt at carry-over"),
)
RECURRENCE_KINDS = (
    ("none", "(no recurrence)"),
    ("daily", "Every day"),
    ("weekdays", "Specific weekday(s)"),
    ("every_n_weeks", "Every N weeks on a chosen weekday"),
    ("monthly_date", "Day-of-month"),
    ("monthly_nth_weekday", "Nth weekday of the month"),
)
WEEKDAY_OPTIONS = list(enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]))


def _parse_recurrence(form) -> Optional[dict]:
    kind = (form.get("rec_kind") or "none").strip()
    if kind in {"none", ""}:
        return None
    if kind == "daily":
        return {"kind": "daily"}
    if kind == "weekdays":
        weekdays = [int(x) for x in form.getlist("rec_weekdays")]
        return {"kind": "weekdays", "weekdays": weekdays}
    if kind == "every_n_weeks":
        return {
            "kind": "every_n_weeks",
            "n": int(form.get("rec_n") or 2),
            "weekday": int(form.get("rec_weekday") or 0),
            "anchor": form.get("rec_anchor") or None,
        }
    if kind == "monthly_date":
        return {"kind": "monthly_date", "day": int(form.get("rec_day") or 1)}
    if kind == "monthly_nth_weekday":
        return {
            "kind": "monthly_nth_weekday",
            "n": int(form.get("rec_n") or 1),
            "weekday": int(form.get("rec_weekday") or 0),
        }
    return None


@router.get("/task-templates")
def list_templates(request: Request):
    with SessionLocal() as s:
        templates = list(
            s.scalars(
                select(M.TaskTemplate)
                .where(M.TaskTemplate.active == True)  # noqa: E712
                .order_by(M.TaskTemplate.display_order, M.TaskTemplate.id)
            ).all()
        )
        cats = {c.id: c.name for c in s.scalars(select(M.TaskCategory)).all()}
    descriptions = {t.id: describe(t.recurrence_rule or {}) for t in templates}
    return render(
        request, "templates/list.html",
        templates=templates, cats=cats, descriptions=descriptions,
    )


@router.get("/task-templates/new")
def new_template_form(request: Request):
    with SessionLocal() as s:
        cats = list(s.scalars(
            select(M.TaskCategory).where(M.TaskCategory.active == True).order_by(M.TaskCategory.display_order)  # noqa: E712
        ).all())
        quals = list(s.scalars(
            select(M.Qualification).where(M.Qualification.active == True).order_by(M.Qualification.display_order)  # noqa: E712
        ).all())
    return render(
        request, "templates/new.html",
        cats=cats, quals=quals,
        carry_over_policies=CARRY_OVER_POLICIES,
        recurrence_kinds=RECURRENCE_KINDS,
        weekday_options=WEEKDAY_OPTIONS,
    )


@router.post("/task-templates")
async def create_template(request: Request):
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    with SessionLocal() as s:
        tmpl = M.TaskTemplate(
            name=name[:240],
            category_id=int(form.get("category_id")) if form.get("category_id") else None,
            description=(form.get("description") or None),
            estimated_hours=float(form.get("estimated_hours")) if form.get("estimated_hours") else None,
            splittable=bool(form.get("splittable")),
            reassignable=bool(form.get("reassignable", "1")),
            carry_over_policy=form.get("carry_over_policy") or "auto_same_person",
            recurrence_rule=_parse_recurrence(form),
            required_drivers_license=bool(form.get("required_drivers_license")),
            required_duty_section=int(form.get("required_duty_section")) if form.get("required_duty_section") else None,
            notes=(form.get("notes") or None),
        )
        s.add(tmpl)
        s.flush()
        for qid in form.getlist("required_quals"):
            try:
                s.add(M.TaskTemplateRequiredQual(task_template_id=tmpl.id, qual_id=int(qid)))
            except ValueError:
                continue
        s.commit()
    return RedirectResponse("/task-templates", status_code=303)


@router.get("/task-templates/{template_id}/edit")
def edit_template_form(template_id: int, request: Request):
    with SessionLocal() as s:
        tmpl = s.get(M.TaskTemplate, template_id)
        if not tmpl:
            raise HTTPException(404)
        cats = list(s.scalars(
            select(M.TaskCategory).where(M.TaskCategory.active == True).order_by(M.TaskCategory.display_order)  # noqa: E712
        ).all())
        quals = list(s.scalars(
            select(M.Qualification).where(M.Qualification.active == True).order_by(M.Qualification.display_order)  # noqa: E712
        ).all())
        required_qual_ids = set(s.scalars(
            select(M.TaskTemplateRequiredQual.qual_id).where(M.TaskTemplateRequiredQual.task_template_id == template_id)
        ).all())
    return render(
        request, "templates/edit.html",
        template=tmpl, cats=cats, quals=quals,
        required_qual_ids=required_qual_ids,
        carry_over_policies=CARRY_OVER_POLICIES,
        recurrence_kinds=RECURRENCE_KINDS,
        weekday_options=WEEKDAY_OPTIONS,
    )


@router.post("/task-templates/{template_id}")
async def update_template(template_id: int, request: Request):
    form = await request.form()
    with SessionLocal() as s:
        tmpl = s.get(M.TaskTemplate, template_id)
        if not tmpl:
            raise HTTPException(404)
        tmpl.name = (form.get("name") or "").strip()[:240]
        tmpl.category_id = int(form.get("category_id")) if form.get("category_id") else None
        tmpl.description = form.get("description") or None
        tmpl.estimated_hours = float(form.get("estimated_hours")) if form.get("estimated_hours") else None
        tmpl.splittable = bool(form.get("splittable"))
        tmpl.reassignable = bool(form.get("reassignable"))
        tmpl.carry_over_policy = form.get("carry_over_policy") or "auto_same_person"
        tmpl.recurrence_rule = _parse_recurrence(form)
        tmpl.required_drivers_license = bool(form.get("required_drivers_license"))
        tmpl.required_duty_section = int(form.get("required_duty_section")) if form.get("required_duty_section") else None
        tmpl.notes = form.get("notes") or None
        # Reset required quals.
        s.query(M.TaskTemplateRequiredQual).filter(
            M.TaskTemplateRequiredQual.task_template_id == template_id
        ).delete()
        for qid in form.getlist("required_quals"):
            try:
                s.add(M.TaskTemplateRequiredQual(task_template_id=tmpl.id, qual_id=int(qid)))
            except ValueError:
                continue
        s.commit()
    return RedirectResponse("/task-templates", status_code=303)


@router.post("/task-templates/{template_id}/archive")
def archive_template(template_id: int, reason: str = Form("")):
    with SessionLocal() as s:
        tmpl = s.get(M.TaskTemplate, template_id)
        if not tmpl:
            raise HTTPException(404)
        tmpl.active = False
        tmpl.archived_at = datetime.now()
        tmpl.archived_reason = reason or None
        s.commit()
    return RedirectResponse("/task-templates", status_code=303)
