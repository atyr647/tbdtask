"""
Availability service.

Answers "who's at work, who isn't, and why" for a given date. The output is
derived on demand from absence records and effective-dated roster status, so
there's no duplicated state to keep in sync.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as M


@dataclass
class PersonDay:
    person: M.Person
    rate: Optional[str]
    duty_section: Optional[int]
    absence: Optional[M.Absence]
    code: Optional[str]
    partial: bool
    start_time: Optional[time]
    end_time: Optional[time]
    reason: Optional[str]

    @property
    def is_present(self) -> bool:
        return self.absence is None

    @property
    def is_partial(self) -> bool:
        return self.absence is not None and self.partial


@dataclass
class DayReport:
    on_date: date
    rows: list[PersonDay]
    total: int
    full_absent: int
    partial_absent: int
    present_full: int
    by_code: dict[str, int]

    @property
    def percent_present(self) -> float:
        if self.total == 0:
            return 0.0
        return round(
            (self.present_full + self.partial_absent / 2) / self.total * 100, 1
        )


def _current_attr(rows, attr):
    for r in rows:
        if r.valid_to is None:
            return getattr(r, attr)
    return None


def get_day_report(session: Session, on_date: date) -> DayReport:
    people = session.scalars(
        select(M.Person)
        .where(M.Person.active == True)  # noqa: E712
        .order_by(M.Person.display_order)
    ).all()
    # Active absence rows that overlap on_date.
    absences = session.scalars(
        select(M.Absence).where(
            M.Absence.active == True,  # noqa: E712
            M.Absence.start_date <= on_date,
            M.Absence.end_date >= on_date,
        )
    ).all()
    by_person: dict[int, M.Absence] = {a.person_id: a for a in absences}

    rows: list[PersonDay] = []
    by_code: dict[str, int] = {}
    full_absent = 0
    partial_absent = 0
    for p in people:
        # Touch effective-dated relationships within session.
        rate = _current_attr(p.rates, "rate")
        ds = _current_attr(p.duty_sections, "duty_section")
        absence = by_person.get(p.id)
        partial = bool(absence and (absence.start_time or absence.end_time))
        code = absence.code.code if absence else None
        if absence:
            if partial:
                partial_absent += 1
            else:
                full_absent += 1
            by_code[code] = by_code.get(code, 0) + 1
        rows.append(
            PersonDay(
                person=p,
                rate=rate,
                duty_section=ds,
                absence=absence,
                code=code,
                partial=partial,
                start_time=absence.start_time if absence else None,
                end_time=absence.end_time if absence else None,
                reason=absence.reason if absence else None,
            )
        )

    total = len(rows)
    present_full = total - full_absent - partial_absent
    return DayReport(
        on_date=on_date,
        rows=rows,
        total=total,
        full_absent=full_absent,
        partial_absent=partial_absent,
        present_full=present_full,
        by_code=by_code,
    )
