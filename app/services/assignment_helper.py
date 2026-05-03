"""
Suggest assignment candidates for a TaskInstance.

Filters by the task's constraints (required quals, drivers license,
duty section) and the person's availability on the scheduled date.
Already-assigned people are excluded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as M
from .availability import get_day_report


@dataclass
class Candidate:
    person: M.Person
    rate: Optional[str]
    duty_section: Optional[int]
    qualified_count: int
    has_drivers_license: bool
    availability_status: str  # "present" | "partial" | "out"
    reasons: list[str]  # why they qualified or got filtered (debug)


def _current(rows, attr):
    for r in rows:
        if r.valid_to is None:
            return getattr(r, attr)
    return None


def candidates_for(
    session: Session,
    instance: M.TaskInstance,
    include_unavailable: bool = False,
) -> list[Candidate]:
    template = (
        session.get(M.TaskTemplate, instance.template_id)
        if instance.template_id
        else None
    )
    required_qual_ids: set[int] = set()
    required_dl = False
    required_ds: Optional[int] = None
    if template is not None:
        required_qual_ids = set(
            session.scalars(
                select(M.TaskTemplateRequiredQual.qual_id).where(
                    M.TaskTemplateRequiredQual.task_template_id == template.id
                )
            ).all()
        )
        required_dl = bool(template.required_drivers_license)
        required_ds = template.required_duty_section

    # Already assigned to this instance.
    already_ids = set(
        session.scalars(
            select(M.TaskAssignment.person_id).where(
                M.TaskAssignment.instance_id == instance.id,
                M.TaskAssignment.active == True,  # noqa: E712
                M.TaskAssignment.person_id.is_not(None),
            )
        ).all()
    )

    persons = list(
        session.scalars(
            select(M.Person)
            .where(M.Person.active == True)  # noqa: E712
            .order_by(M.Person.display_order)
        ).all()
    )

    # Build qual lookup once: (person_id, qual_id) -> status
    qual_rows = session.execute(
        select(M.PersonQual.person_id, M.PersonQual.qual_id, M.PersonQual.status).where(
            M.PersonQual.valid_to.is_(None),
            M.PersonQual.active == True,  # noqa: E712
        )
    ).all()
    person_quals: dict[int, dict[int, str]] = {}
    for pid, qid, st in qual_rows:
        person_quals.setdefault(pid, {})[qid] = st

    # Availability for the scheduled date.
    avail_by_person: dict[int, str] = {}
    if instance.scheduled_date is not None:
        report = get_day_report(session, instance.scheduled_date)
        for r in report.rows:
            if r.absence is None:
                avail_by_person[r.person.id] = "present"
            elif r.partial:
                avail_by_person[r.person.id] = "partial"
            else:
                avail_by_person[r.person.id] = "out"

    out: list[Candidate] = []
    for p in persons:
        if p.id in already_ids:
            continue
        rate = _current(p.rates, "rate")
        ds = _current(p.duty_sections, "duty_section")
        dl_row = next((d for d in p.drivers_licenses if d.valid_to is None), None)
        has_dl = bool(dl_row.has_license) if dl_row else False
        person_q = person_quals.get(p.id, {})
        qualified_for = sum(
            1 for qid in required_qual_ids if person_q.get(qid) == "qualified"
        )
        reasons: list[str] = []
        if required_qual_ids and qualified_for < len(required_qual_ids):
            missing = [
                qid for qid in required_qual_ids if person_q.get(qid) != "qualified"
            ]
            reasons.append(f"missing_quals:{len(missing)}")
        if required_dl and not has_dl:
            reasons.append("no_drivers_license")
        if required_ds and ds != required_ds:
            reasons.append(f"wrong_duty_section:{ds}")
        availability_status = avail_by_person.get(p.id, "present")
        if availability_status == "out":
            reasons.append("out")
        if reasons and not include_unavailable:
            continue
        out.append(
            Candidate(
                person=p,
                rate=rate,
                duty_section=ds,
                qualified_count=qualified_for,
                has_drivers_license=has_dl,
                availability_status=availability_status,
                reasons=reasons,
            )
        )
    # Rank: most quals, then has license, then present-status, then display_order.
    out.sort(
        key=lambda c: (
            -c.qualified_count,
            0 if c.has_drivers_license else 1,
            {"present": 0, "partial": 1, "out": 2}[c.availability_status],
            c.person.display_order,
        )
    )
    return out
