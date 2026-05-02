"""
Long-term task history rollups attached to a personnel profile.

Aggregates completed assignments by category to surface "what has this
person been working on lately" without forcing the operator to scroll
through every prior worklist. Elapsed time uses ``hours_worked`` from
the assignment when present.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as M


@dataclass
class CategoryStat:
    name: str
    count: int = 0
    hours: float = 0.0
    poic_count: int = 0


@dataclass
class RecentTask:
    instance: M.TaskInstance
    category_name: Optional[str]
    completed_at: Optional[datetime]
    hours_worked: Optional[float]
    is_poic: bool


@dataclass
class PersonStats:
    person_id: int
    window_days: Optional[int]
    total_completed: int
    total_hours: float
    poic_count: int
    by_category: list[CategoryStat]
    recent: list[RecentTask]


def stats_for(
    session: Session,
    person_id: int,
    window_days: Optional[int] = 180,
    recent_limit: int = 15,
) -> PersonStats:
    cutoff: Optional[date] = None
    if window_days is not None:
        cutoff = date.today() - timedelta(days=window_days)

    completed_q = (
        select(M.TaskAssignment, M.TaskInstance, M.TaskCategory)
        .join(M.TaskInstance, M.TaskInstance.id == M.TaskAssignment.instance_id)
        .outerjoin(M.TaskCategory, M.TaskCategory.id == M.TaskInstance.category_id)
        .where(
            M.TaskAssignment.person_id == person_id,
            M.TaskAssignment.completed == True,  # noqa: E712
            M.TaskAssignment.active == True,  # noqa: E712
        )
        .order_by(M.TaskInstance.completed_at.desc().nulls_last(), M.TaskInstance.id.desc())
    )
    if cutoff is not None:
        completed_q = completed_q.where(
            (M.TaskInstance.completed_at.is_not(None) &
             (M.TaskInstance.completed_at >= datetime.combine(cutoff, datetime.min.time())))
            | (M.TaskInstance.scheduled_date.is_not(None) &
               (M.TaskInstance.scheduled_date >= cutoff))
        )

    rows = list(session.execute(completed_q).all())

    by_cat: dict[str, CategoryStat] = defaultdict(lambda: CategoryStat(name="Uncategorized"))
    by_cat_seen_names: set[str] = set()
    total_count = 0
    total_hours = 0.0
    poic_count = 0
    recent: list[RecentTask] = []

    for assignment, instance, category in rows:
        cat_name = category.name if category else "Uncategorized"
        stat = by_cat.setdefault(cat_name, CategoryStat(name=cat_name))
        by_cat_seen_names.add(cat_name)
        stat.count += 1
        if assignment.hours_worked:
            stat.hours += float(assignment.hours_worked)
        if assignment.is_poic:
            stat.poic_count += 1
            poic_count += 1
        total_count += 1
        if assignment.hours_worked:
            total_hours += float(assignment.hours_worked)
        if len(recent) < recent_limit:
            recent.append(RecentTask(
                instance=instance,
                category_name=cat_name,
                completed_at=instance.completed_at,
                hours_worked=assignment.hours_worked,
                is_poic=assignment.is_poic,
            ))

    by_cat_list = sorted(by_cat.values(), key=lambda c: -c.count)

    return PersonStats(
        person_id=person_id,
        window_days=window_days,
        total_completed=total_count,
        total_hours=round(total_hours, 2),
        poic_count=poic_count,
        by_category=by_cat_list,
        recent=recent,
    )
