"""
Helpers for effective-dated attribute tables.

Each effective-dated row carries (valid_from, valid_to). The currently active
row has valid_to = NULL. Setting a new value closes the prior current row at
the new effective date and inserts a new current row. We never silently
mutate prior values; that preserves the audit trail the data model promises.
"""
from __future__ import annotations

from datetime import date
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session


def current_row(session: Session, model, person_id: int):
    return session.scalars(
        select(model)
        .where(model.person_id == person_id, model.valid_to.is_(None))
        .order_by(model.valid_from.desc())
    ).first()


def history(session: Session, model, person_id: int) -> list:
    return list(
        session.scalars(
            select(model)
            .where(model.person_id == person_id)
            .order_by(model.valid_from.desc())
        ).all()
    )


def set_new_value(
    session: Session,
    model,
    *,
    person_id: int,
    effective_date: Optional[date] = None,
    fields: dict,
    no_op_if_unchanged: Iterable[str] = (),
):
    """Close the currently-active row and insert a new one carrying ``fields``.

    If the current row already matches ``fields`` on every key listed in
    ``no_op_if_unchanged``, returns the existing row without writing. That
    avoids littering history with null updates when an edit form is submitted
    unchanged.
    """
    eff = effective_date or date.today()
    cur = current_row(session, model, person_id)
    if cur is not None and no_op_if_unchanged:
        if all(getattr(cur, k) == fields.get(k) for k in no_op_if_unchanged):
            return cur
    if cur is not None:
        cur.valid_to = eff
    new_row = model(person_id=person_id, valid_from=eff, valid_to=None, **fields)
    session.add(new_row)
    session.flush()
    return new_row
