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


# ---------------------------------------------------------------------------
# Compact print grid: person rows x day columns (legacy Excel layout).
# ---------------------------------------------------------------------------

@dataclass
class GridDayCell:
    on_date: date
    absence_code: Optional[str]
    absence_partial: bool
    absence_reason: Optional[str]
    tasks: list[TaskRow] = field(default_factory=list)


@dataclass
class GridPersonRow:
    person: M.Person
    rate: Optional[str]
    duty_section: Optional[int]
    cells: list[GridDayCell]
    has_any_content: bool


@dataclass
class GridDayHeader:
    on_date: date
    weekday: str
    out_count: int
    present_count: int
    out_summary: list[str]


@dataclass
class WeekGrid:
    worklist: M.Worklist
    headers: list[GridDayHeader]
    rows: list[GridPersonRow]
    unassigned_by_day: dict[date, list[TaskRow]]


WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def build_week_grid(session: Session, worklist: M.Worklist, days: int = 5) -> WeekGrid:
    """Lay out the week as a person-row x day-column grid for compact print.

    Days defaults to 5 (Mon–Fri); pass 7 for the full week. Only personnel
    with at least one task or absence in the window appear as rows. The
    optional unassigned bucket holds tasks with no person attached.
    """
    monday = worklist.week_starting
    day_dates = [monday + timedelta(days=i) for i in range(days)]
    end = day_dates[-1]

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

    # Per-day: who's out (with full DayReport), and which TaskInstances live there.
    headers: list[GridDayHeader] = []
    instances_by_date: dict[date, list[M.TaskInstance]] = {d: [] for d in day_dates}
    for inst in instances:
        if inst.scheduled_date in instances_by_date:
            instances_by_date[inst.scheduled_date].append(inst)
        elif inst.scheduled_date is None:
            instances_by_date[day_dates[0]].append(inst)  # park unscheduled on day 1
    absence_lookup: dict[date, dict[int, PersonDay]] = {}
    for d in day_dates:
        report = get_day_report(session, d)
        absence_lookup[d] = {r.person.id: r for r in report.rows}
        out = [r for r in report.rows if r.absence is not None]
        out_summary = [
            f"{r.person.full_display} ({r.code}{f' {r.start_time}-{r.end_time}' if r.partial else ''})"
            for r in out
        ]
        headers.append(GridDayHeader(
            on_date=d, weekday=WEEKDAY_NAMES[d.weekday()],
            out_count=len(out), present_count=report.total - len(out),
            out_summary=out_summary,
        ))

    # Pre-collect labels for shared assignees.
    def _other_labels(a: M.TaskAssignment, all_assignments: list[M.TaskAssignment]) -> list[str]:
        return [_assignee_label(o) for o in all_assignments if o is not a and o.active]

    # Build per-person grid rows.
    person_rows: dict[int, GridPersonRow] = {}
    unassigned_by_day: dict[date, list[TaskRow]] = {d: [] for d in day_dates}

    def _ensure_row(person: M.Person) -> GridPersonRow:
        if person.id in person_rows:
            return person_rows[person.id]
        rate = _current_attr(person.rates, "rate")
        ds = _current_attr(person.duty_sections, "duty_section")
        cells = [
            GridDayCell(
                on_date=d, absence_code=None, absence_partial=False, absence_reason=None,
            )
            for d in day_dates
        ]
        # Annotate absences across the whole week so the row reflects the
        # personnel's status even on days without tasks.
        for i, d in enumerate(day_dates):
            avail = absence_lookup[d].get(person.id)
            if avail and avail.absence:
                cells[i].absence_code = avail.code
                cells[i].absence_partial = avail.partial
                cells[i].absence_reason = avail.reason
        row = GridPersonRow(
            person=person, rate=rate, duty_section=ds, cells=cells, has_any_content=False,
        )
        person_rows[person.id] = row
        return row

    for d_idx, d in enumerate(day_dates):
        for inst in instances_by_date[d]:
            cat_name = inst.category.name if inst.category else None
            assignments = [a for a in inst.assignments if a.active]
            if not assignments:
                unassigned_by_day[d].append(TaskRow(
                    instance=inst, assignment=None, other_assignees=[],
                    is_poic=False, external_poic=None, category_name=cat_name,
                ))
                continue
            for a in assignments:
                others = _other_labels(a, assignments)
                if a.person:
                    row = _ensure_row(a.person)
                    row.cells[d_idx].tasks.append(TaskRow(
                        instance=inst, assignment=a, other_assignees=others,
                        is_poic=a.is_poic, external_poic=a.external_poic_name,
                        category_name=cat_name,
                    ))
                    row.has_any_content = True
                else:
                    unassigned_by_day[d].append(TaskRow(
                        instance=inst, assignment=a, other_assignees=others,
                        is_poic=a.is_poic, external_poic=a.external_poic_name,
                        category_name=cat_name,
                    ))

    # Promote rows for personnel who are merely absent (no tasks) so the
    # printed page still shows their leave status.
    for d_idx, d in enumerate(day_dates):
        for person_id, avail in absence_lookup[d].items():
            if avail.absence is None:
                continue
            if person_id in person_rows:
                continue
            _ensure_row(avail.person).has_any_content = True

    rows = sorted(
        (r for r in person_rows.values() if r.has_any_content),
        key=lambda r: (r.person.display_order, r.person.id),
    )
    return WeekGrid(
        worklist=worklist, headers=headers, rows=rows,
        unassigned_by_day=unassigned_by_day,
    )


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
