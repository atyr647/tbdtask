"""
Worklist view service.

Composes a week into a (day -> person -> [tasks]) tree, with availability
annotations attached so the printed/rendered worklist tells the operator at a
glance who's out and why on each day. Tasks with multiple assignees appear
under each assignee's row with a shared marker; an unassigned bucket holds
tasks with no person attached.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import models as M
from .availability import get_day_report, PersonDay


@dataclass
class TaskRow:
    instance: M.TaskInstance
    assignment: Optional[M.TaskAssignment]
    other_assignees: list[str]
    is_poic: bool
    external_poic: Optional[str]
    category_name: Optional[str]


@dataclass
class PersonDayBlock:
    person: M.Person
    rate: Optional[str]
    duty_section: Optional[int]
    availability: Optional[PersonDay]  # full info
    tasks: list[TaskRow] = field(default_factory=list)


@dataclass
class DayBlock:
    on_date: date
    weekday: str
    person_blocks: list[PersonDayBlock] = field(default_factory=list)
    unassigned_tasks: list[TaskRow] = field(default_factory=list)
    out_today: list[PersonDay] = field(default_factory=list)
    percent_present: float = 0.0
    total: int = 0
    present_full: int = 0


@dataclass
class WeekView:
    worklist: M.Worklist
    days: list[DayBlock]
    week_total_tasks: int


WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _current_attr(rows, attr):
    for r in rows:
        if r.valid_to is None:
            return getattr(r, attr)
    return None


def _assignee_label(a: M.TaskAssignment) -> str:
    if a.person:
        return a.person.full_display
    return a.external_poic_name or "(unassigned)"


def build_week_view(session: Session, worklist: M.Worklist, days: int = 7) -> WeekView:
    monday = worklist.week_starting
    instances = list(
        session.scalars(
            select(M.TaskInstance)
            .where(
                M.TaskInstance.worklist_id == worklist.id,
                M.TaskInstance.active == True,  # noqa: E712
            )
            .options(
                selectinload(M.TaskInstance.assignments).selectinload(M.TaskAssignment.person),
                selectinload(M.TaskInstance.category),
            )
            .order_by(M.TaskInstance.scheduled_date, M.TaskInstance.display_order, M.TaskInstance.id)
        ).all()
    )
    instances_by_date: dict[Optional[date], list[M.TaskInstance]] = {}
    for inst in instances:
        instances_by_date.setdefault(inst.scheduled_date, []).append(inst)

    day_blocks: list[DayBlock] = []
    week_total = 0
    for offset in range(days):
        d = monday + timedelta(days=offset)
        report = get_day_report(session, d)
        block = DayBlock(
            on_date=d,
            weekday=WEEKDAY_NAMES[d.weekday()],
            out_today=[r for r in report.rows if r.absence is not None],
            percent_present=report.percent_present,
            total=report.total,
            present_full=report.present_full,
        )
        availability_by_person = {r.person.id: r for r in report.rows}
        person_block_by_id: dict[int, PersonDayBlock] = {}
        for inst in instances_by_date.get(d, []):
            week_total += 1
            cat_name = inst.category.name if inst.category else None
            assignments = [a for a in inst.assignments if a.active]
            other_labels = [_assignee_label(a) for a in assignments]
            if not assignments:
                block.unassigned_tasks.append(TaskRow(
                    instance=inst, assignment=None, other_assignees=[],
                    is_poic=False, external_poic=None, category_name=cat_name,
                ))
                continue
            for a in assignments:
                others = [lbl for lbl in other_labels if lbl != _assignee_label(a)]
                if a.person:
                    pb = person_block_by_id.get(a.person.id)
                    if pb is None:
                        rate = _current_attr(a.person.rates, "rate")
                        ds = _current_attr(a.person.duty_sections, "duty_section")
                        pb = PersonDayBlock(
                            person=a.person, rate=rate, duty_section=ds,
                            availability=availability_by_person.get(a.person.id),
                        )
                        person_block_by_id[a.person.id] = pb
                    pb.tasks.append(TaskRow(
                        instance=inst, assignment=a, other_assignees=others,
                        is_poic=a.is_poic, external_poic=a.external_poic_name,
                        category_name=cat_name,
                    ))
                else:
                    # External POIC with no Person row (e.g., visiting supervisor).
                    block.unassigned_tasks.append(TaskRow(
                        instance=inst, assignment=a, other_assignees=others,
                        is_poic=a.is_poic, external_poic=a.external_poic_name,
                        category_name=cat_name,
                    ))
        # Order person blocks by display_order on Person
        block.person_blocks = sorted(
            person_block_by_id.values(),
            key=lambda b: (b.person.display_order, b.person.id),
        )
        day_blocks.append(block)

    return WeekView(worklist=worklist, days=day_blocks, week_total_tasks=week_total)
