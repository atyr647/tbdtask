"""
Qual overview: per-qualification rollups (counts by status) and a
coverage-gap signal driven by a configurable threshold.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as M


DEFAULT_QUALIFIED_THRESHOLD = 2  # below this many qualified is flagged as a gap


@dataclass
class QualSummary:
    qual: M.Qualification
    counts: dict[str, int]
    qualified_names: list[str] = field(default_factory=list)
    in_progress_names: list[str] = field(default_factory=list)
    dinq_names: list[str] = field(default_factory=list)
    expiring_soon: list[tuple[str, str]] = field(default_factory=list)  # (person_name, expiry iso)
    gap: bool = False


def build_qual_overview(
    session: Session,
    qualified_threshold: int = DEFAULT_QUALIFIED_THRESHOLD,
    expiring_within_days: int = 30,
) -> list[QualSummary]:
    quals = list(
        session.scalars(
            select(M.Qualification)
            .where(M.Qualification.active == True)  # noqa: E712
            .order_by(M.Qualification.display_order)
        ).all()
    )
    rows = list(
        session.execute(
            select(M.PersonQual, M.Person)
            .join(M.Person, M.Person.id == M.PersonQual.person_id)
            .where(
                M.PersonQual.valid_to.is_(None),
                M.PersonQual.active == True,  # noqa: E712
                M.Person.active == True,  # noqa: E712
            )
        ).all()
    )

    by_qual: dict[int, list[tuple[M.PersonQual, M.Person]]] = defaultdict(list)
    for pq, p in rows:
        by_qual[pq.qual_id].append((pq, p))

    summaries: list[QualSummary] = []
    for q in quals:
        counts: dict[str, int] = defaultdict(int)
        s = QualSummary(qual=q, counts=counts)
        for pq, p in by_qual.get(q.id, []):
            counts[pq.status] += 1
            if pq.status == "qualified":
                s.qualified_names.append(p.full_display)
            elif pq.status == "in_progress":
                s.in_progress_names.append(p.full_display)
            elif pq.status == "dinq":
                s.dinq_names.append(p.full_display)
        s.gap = counts.get("qualified", 0) < qualified_threshold
        summaries.append(s)
    return summaries
