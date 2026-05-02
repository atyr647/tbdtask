"""
Builds the people x days grid for the leave/absence overview, mirroring the
legacy 'BPT Master Leave Tracker' sheet but as a live computed view.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as M


@dataclass
class CalendarCell:
    code: Optional[str]
    partial: bool
    reason: Optional[str]


@dataclass
class CalendarRow:
    person: M.Person
    cells: list[CalendarCell]


@dataclass
class CalendarView:
    start_date: date
    days: list[date]
    rows: list[CalendarRow]
    daily_full: list[int]
    daily_partial: list[int]
    daily_total: int

    @property
    def daily_absent(self) -> list[int]:
        return [f + p for f, p in zip(self.daily_full, self.daily_partial)]

    @property
    def daily_percent_present(self) -> list[float]:
        if self.daily_total == 0:
            return [0.0] * len(self.daily_full)
        out = []
        for f, p in zip(self.daily_full, self.daily_partial):
            value = (self.daily_total - f - p / 2) / self.daily_total * 100
            out.append(round(value, 1))
        return out

    @property
    def month_groups(self) -> list[tuple[str, int]]:
        """Tuples of (label, span) for the month-header row. The label
        format includes the year so transitions (e.g. Dec 2026 -> Jan 2027)
        are unambiguous, and a year change always closes the prior block."""
        groups: list[tuple[str, int]] = []
        if not self.days:
            return groups
        cur_label = self.days[0].strftime("%B %Y")
        cur_span = 0
        for d in self.days:
            label = d.strftime("%B %Y")
            if label != cur_label:
                groups.append((cur_label, cur_span))
                cur_label = label
                cur_span = 0
            cur_span += 1
        groups.append((cur_label, cur_span))
        return groups


def _current_attr(rows, attr):
    for r in rows:
        if r.valid_to is None:
            return getattr(r, attr)
    return None


def build_calendar(session: Session, start: date, days: int = 28) -> CalendarView:
    day_dates = [start + timedelta(days=i) for i in range(days)]
    end = day_dates[-1]
    people = list(
        session.scalars(
            select(M.Person)
            .where(M.Person.active == True)  # noqa: E712
            .order_by(M.Person.display_order)
        ).all()
    )
    absences = list(
        session.scalars(
            select(M.Absence)
            .where(
                M.Absence.active == True,  # noqa: E712
                M.Absence.start_date <= end,
                M.Absence.end_date >= start,
            )
        ).all()
    )
    by_person: dict[int, list[M.Absence]] = {}
    for a in absences:
        by_person.setdefault(a.person_id, []).append(a)

    rows: list[CalendarRow] = []
    daily_full = [0] * days
    daily_partial = [0] * days
    for p in people:
        cells: list[CalendarCell] = []
        person_absences = by_person.get(p.id, [])
        for i, d in enumerate(day_dates):
            hit: Optional[M.Absence] = None
            for a in person_absences:
                if a.start_date <= d <= a.end_date:
                    hit = a
                    break
            if hit is None:
                cells.append(CalendarCell(code=None, partial=False, reason=None))
            else:
                partial = bool(hit.start_time or hit.end_time)
                cells.append(CalendarCell(code=hit.code.code, partial=partial, reason=hit.reason))
                if partial:
                    daily_partial[i] += 1
                else:
                    daily_full[i] += 1
        rows.append(CalendarRow(person=p, cells=cells))

    return CalendarView(
        start_date=start, days=day_dates, rows=rows,
        daily_full=daily_full, daily_partial=daily_partial,
        daily_total=len(people),
    )
