"""Write operations + form choice helpers for the Tk front-end.

These mirror the FastAPI route handlers' logic (validation, effective-
dating via ``services.effective``, soft-delete, alert recompute) so the
native UI writes data identically to the web app. Each public function is
passed to ``context.write`` and runs inside ``tenant_context`` +
``session_scope`` (which commits on a clean return).

The PII/CUI gate the routes enforce on save lives here as
:func:`sensitive_warnings`; screens call it before committing and ask the
operator to acknowledge, matching the web flow's re-render-until-ack.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as M
from ..auth.sensitive_info import scan_text
from ..data import ranks as rank_catalog
from ..services import alerts as alerts_service
from ..services import effective as eff
from ..services.carry_over import apply_carry_over, find_pending_carry_overs
from ..services.task_generator import generate_for_worklist

DUTY_SECTIONS = (1, 2, 3, 4, 5, 6)
ROSTER_STATUSES = ("active", "incoming", "departed")
PRD_REASONS = ("initial", "extension", "correction")
TASK_STATUSES = ("open", "in_progress", "done", "discarded", "carried")
PERSON_QUAL_STATUSES = (
    "not_assigned", "assigned", "in_progress", "qualified", "dinq",
    "expired", "waived",
)
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
WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def sensitive_warnings(*texts: str | None) -> list[str]:
    """Distinct PII/CUI labels detected across the given free-text fields."""
    seen: set[str] = set()
    out: list[str] = []
    for t in texts:
        if not t:
            continue
        for m in scan_text(t).matches:
            if m.label not in seen:
                seen.add(m.label)
                out.append(m.label)
    return out


def _parse_date(value: str | None) -> date | None:
    value = (value or "").strip()
    return date.fromisoformat(value) if value else None


def _parse_time(value: str | None) -> time | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return time.fromisoformat(value)
    except ValueError:
        return None


def paygrade_choices() -> list[tuple[str, str]]:
    """(paygrade, label) for the picker — grade plus its rank where fixed."""
    out = []
    for pg, kind in rank_catalog.PAYGRADES:
        if kind == "officer":
            out.append((pg, f"{pg}  {rank_catalog.OFFICER_RANK[pg]}"))
        elif kind == "warrant":
            out.append((pg, f"{pg}  {rank_catalog.WARRANT_RANK[pg]}"))
        else:
            out.append((pg, pg))
    return out


def rating_choices() -> list[tuple[str, str]]:
    """(rating, rating) for enlisted; a leading '(non-rated)' option."""
    return ([(rank_catalog.NON_RATED_LABEL, rank_catalog.NON_RATED_LABEL)]
            + [(r, r) for r in rank_catalog.RATINGS])


def position_choices() -> list[tuple[str, str]]:
    return [(p, p) for p in rank_catalog.POSITIONS]


def is_enlisted_paygrade(paygrade: str | None) -> bool:
    return rank_catalog.is_enlisted(paygrade)


def absence_code_choices():
    """Return [(id, code)] for active absence codes."""
    def _q(s: Session):
        rows = s.scalars(
            select(M.AbsenceCode)
            .where(M.AbsenceCode.active == True)  # noqa: E712
            .order_by(M.AbsenceCode.display_order)
        ).all()
        return [(c.id, c.code) for c in rows]

    return _q


def active_people_choices():
    """Return [(id, name)] for active personnel (for sponsor/person pickers)."""
    def _q(s: Session):
        rows = s.scalars(
            select(M.Person)
            .where(M.Person.active == True)  # noqa: E712
            .order_by(M.Person.display_order)
        ).all()
        return [(p.id, p.full_display) for p in rows]

    return _q


# --------------------------------------------------------------------------
# Personnel
# --------------------------------------------------------------------------


def _resolve_rate(paygrade=None, rating=None, rate=None):
    """Return (rate_token, paygrade) from a paygrade(+rating) selection.

    Falls back to a raw ``rate`` token (deriving its paygrade) when no
    paygrade is supplied, so older call sites/tests keep working.
    """
    if paygrade:
        return rank_catalog.rate_token(paygrade, rating), paygrade
    if rate:
        return rate, rank_catalog.paygrade_for(rate)
    return None, None


def create_person(*, last_name, first_name=None, paygrade=None, rating=None,
                  rate=None, position=None, notes=None, duty_section=None,
                  prd_date=None, has_drivers_license=False,
                  drivers_license_expires=None, roster_status="active"):
    def _q(s: Session) -> int:
        today = date.today()
        rate_token, pg = _resolve_rate(paygrade, rating, rate)
        full_display = f"{rate_token} {last_name}".strip() if rate_token else last_name
        last_pos = s.scalar(
            select(M.Person.display_order)
            .order_by(M.Person.display_order.desc()).limit(1)
        ) or 0
        p = M.Person(
            last_name=last_name, first_name=first_name,
            full_display=full_display.strip(), position=position, notes=notes,
            display_order=last_pos + 1,
        )
        s.add(p)
        s.flush()
        if rate_token:
            s.add(M.PersonRate(person_id=p.id, rate=rate_token,
                               paygrade=pg, valid_from=today))
        if duty_section:
            s.add(M.PersonDutySection(person_id=p.id,
                                      duty_section=int(duty_section),
                                      valid_from=today))
        if prd_date:
            s.add(M.PersonPrd(person_id=p.id, prd_date=_parse_date(prd_date),
                              change_reason="initial", valid_from=today))
        s.add(M.PersonRosterStatus(person_id=p.id, status=roster_status,
                                   valid_from=today))
        s.add(M.PersonDriversLicense(
            person_id=p.id, has_license=bool(has_drivers_license),
            expires_on=_parse_date(drivers_license_expires), valid_from=today))
        s.flush()
        return p.id

    return _q


def create_incoming(*, last_name, first_name=None, paygrade=None, rating=None,
                    rate=None, notes=None, arrival_date=None,
                    sponsor_person_id=None, orders_received=False,
                    itinerary_received=False, aob_scheduled=False,
                    barracks_assigned=False, position=None):
    def _q(s: Session) -> int:
        today = date.today()
        rate_token, pg = _resolve_rate(paygrade, rating, rate)
        full_display = f"{rate_token} {last_name}".strip() if rate_token else last_name
        last_pos = s.scalar(
            select(M.Person.display_order)
            .order_by(M.Person.display_order.desc()).limit(1)
        ) or 0
        p = M.Person(
            last_name=last_name, first_name=first_name,
            full_display=full_display.strip(), notes=notes, position=position,
            display_order=last_pos + 1, arrival_date=_parse_date(arrival_date),
            sponsor_person_id=int(sponsor_person_id) if sponsor_person_id else None,
            orders_received=bool(orders_received),
            itinerary_received=bool(itinerary_received),
            aob_scheduled=bool(aob_scheduled),
            barracks_assigned=bool(barracks_assigned),
        )
        s.add(p)
        s.flush()
        if rate_token:
            s.add(M.PersonRate(person_id=p.id, rate=rate_token,
                               paygrade=pg, valid_from=today))
        s.add(M.PersonRosterStatus(person_id=p.id, status="incoming",
                                   valid_from=today))
        s.flush()
        return p.id

    return _q


def update_person(person_id, *, last_name, first_name=None, paygrade=None,
                  rating=None, rate=None, position=None, notes=None,
                  duty_section=None, prd_date=None, prd_reason="correction",
                  has_drivers_license=False, drivers_license_expires=None,
                  roster_status="active", effective_date=None):
    def _q(s: Session) -> int | None:
        p = s.get(M.Person, person_id)
        if not p:
            return None
        eff_date = _parse_date(effective_date) or date.today()
        rate_token, pg = _resolve_rate(paygrade, rating, rate)
        p.last_name = last_name
        p.first_name = first_name
        p.position = position
        p.notes = notes
        p.full_display = (f"{rate_token} {last_name}".strip()
                          if rate_token else last_name)
        if rate_token:
            eff.set_new_value(
                s, M.PersonRate, person_id=p.id, effective_date=eff_date,
                fields={"rate": rate_token, "paygrade": pg},
                no_op_if_unchanged=("rate", "paygrade"))
        if duty_section:
            eff.set_new_value(
                s, M.PersonDutySection, person_id=p.id, effective_date=eff_date,
                fields={"duty_section": int(duty_section)},
                no_op_if_unchanged=("duty_section",))
        if prd_date:
            eff.set_new_value(
                s, M.PersonPrd, person_id=p.id, effective_date=eff_date,
                fields={"prd_date": _parse_date(prd_date),
                        "change_reason": prd_reason},
                no_op_if_unchanged=("prd_date",))
        eff.set_new_value(
            s, M.PersonRosterStatus, person_id=p.id, effective_date=eff_date,
            fields={"status": roster_status}, no_op_if_unchanged=("status",))
        eff.set_new_value(
            s, M.PersonDriversLicense, person_id=p.id, effective_date=eff_date,
            fields={"has_license": bool(has_drivers_license),
                    "expires_on": _parse_date(drivers_license_expires)},
            no_op_if_unchanged=("has_license", "expires_on"))
        return p.id

    return _q


def update_checklist(person_id, *, orders_received, itinerary_received,
                     aob_scheduled, barracks_assigned, arrival_date=None,
                     sponsor_person_id=None):
    def _q(s: Session) -> int | None:
        p = s.get(M.Person, person_id)
        if not p:
            return None
        p.orders_received = bool(orders_received)
        p.itinerary_received = bool(itinerary_received)
        p.aob_scheduled = bool(aob_scheduled)
        p.barracks_assigned = bool(barracks_assigned)
        if arrival_date:
            p.arrival_date = _parse_date(arrival_date)
        p.sponsor_person_id = int(sponsor_person_id) if sponsor_person_id else None
        return p.id

    return _q


def mark_arrived(person_id):
    def _q(s: Session) -> int | None:
        p = s.get(M.Person, person_id)
        if not p:
            return None
        eff.set_new_value(
            s, M.PersonRosterStatus, person_id=p.id, effective_date=date.today(),
            fields={"status": "active"}, no_op_if_unchanged=("status",))
        return p.id

    return _q


def archive_person(person_id, reason=""):
    def _q(s: Session) -> int | None:
        p = s.get(M.Person, person_id)
        if not p:
            return None
        p.active = False
        p.archived_at = datetime.now()
        p.archived_reason = reason or None
        eff.set_new_value(
            s, M.PersonRosterStatus, person_id=p.id, effective_date=date.today(),
            fields={"status": "departed"}, no_op_if_unchanged=("status",))
        return p.id

    return _q


# --------------------------------------------------------------------------
# Absences
# --------------------------------------------------------------------------


class ValidationError(Exception):
    """Raised by a command when the caller's input is invalid; the screen
    surfaces the message rather than the generic error boundary."""


def create_absence(*, person_id, code_id, start_date, end_date,
                   start_time=None, end_time=None, reason=None, notes=None):
    def _q(s: Session) -> int:
        if not s.get(M.Person, int(person_id)) or not s.get(M.AbsenceCode, int(code_id)):
            raise ValidationError("person or absence code not found")
        sd, ed = _parse_date(start_date), _parse_date(end_date)
        if sd is None or ed is None:
            raise ValidationError("start and end dates are required (YYYY-MM-DD)")
        if ed < sd:
            raise ValidationError("end date precedes start date")
        a = M.Absence(person_id=int(person_id), code_id=int(code_id),
                      start_date=sd, end_date=ed, start_time=_parse_time(start_time),
                      end_time=_parse_time(end_time), reason=reason, notes=notes)
        s.add(a)
        s.flush()
        return a.id

    return _q


def update_absence(absence_id, *, code_id, start_date, end_date,
                   start_time=None, end_time=None, reason=None, notes=None):
    def _q(s: Session) -> int | None:
        a = s.get(M.Absence, absence_id)
        if not a:
            return None
        sd, ed = _parse_date(start_date), _parse_date(end_date)
        if sd is None or ed is None:
            raise ValidationError("start and end dates are required (YYYY-MM-DD)")
        if ed < sd:
            raise ValidationError("end date precedes start date")
        a.code_id = int(code_id)
        a.start_date = sd
        a.end_date = ed
        a.start_time = _parse_time(start_time)
        a.end_time = _parse_time(end_time)
        a.reason = reason
        a.notes = notes
        return a.person_id

    return _q


def archive_absence(absence_id, reason=""):
    def _q(s: Session) -> int | None:
        a = s.get(M.Absence, absence_id)
        if not a:
            return None
        a.active = False
        a.archived_at = datetime.now()
        a.archived_reason = reason or None
        return a.person_id

    return _q


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------


def dismiss_alert(alert_id):
    def _q(s: Session):
        a = s.get(M.Alert, alert_id)
        if a:
            a.dismissed_at = datetime.now()

    return _q


def resolve_alert(alert_id, note=None):
    def _q(s: Session):
        a = s.get(M.Alert, alert_id)
        if not a:
            return
        a.resolved_at = datetime.now()
        if note:
            existing = (a.notes or "").rstrip()
            a.notes = f"{existing}\n{note.strip()}" if existing else note.strip()

    return _q


def snooze_alert(alert_id, until):
    def _q(s: Session):
        until_date = _parse_date(until)
        if until_date is None:
            raise ValidationError("expected a date (YYYY-MM-DD)")
        a = s.get(M.Alert, alert_id)
        if a:
            a.snoozed_until = until_date

    return _q


def extend_prd(alert_id, *, new_date=None, days=None):
    def _q(s: Session):
        a = s.get(M.Alert, alert_id)
        if not a or not a.person_id:
            raise ValidationError("alert has no linked person")
        if new_date:
            new_prd = _parse_date(new_date)
            if new_prd is None:
                raise ValidationError("expected a date (YYYY-MM-DD)")
        else:
            d = max(1, min(int(days or 180), 365 * 3))
            cur = eff.current_row(s, M.PersonPrd, a.person_id)
            base = cur.prd_date if cur else date.today()
            new_prd = base + timedelta(days=d)
        eff.set_new_value(
            s, M.PersonPrd, person_id=a.person_id, effective_date=date.today(),
            fields={"prd_date": new_prd, "change_reason": "extension",
                    "note": f"Updated from alert #{alert_id}"},
            no_op_if_unchanged=("prd_date",))
        a.resolved_at = datetime.now()
        a.notes = (a.notes or "") + f"\nDeparture date updated to {new_prd.isoformat()}"
        s.flush()
        alerts_service.recompute(s)

    return _q


def archive_person_from_alert(alert_id, reason="Departure date passed"):
    def _q(s: Session):
        a = s.get(M.Alert, alert_id)
        if not a or not a.person_id:
            raise ValidationError("alert has no linked person")
        p = s.get(M.Person, a.person_id)
        if not p:
            raise ValidationError("person not found")
        p.active = False
        p.archived_at = datetime.now()
        p.archived_reason = reason
        eff.set_new_value(
            s, M.PersonRosterStatus, person_id=p.id, effective_date=date.today(),
            fields={"status": "departed"}, no_op_if_unchanged=("status",))
        a.resolved_at = datetime.now()
        a.notes = (a.notes or "") + f"\nPersonnel archived: {reason}"
        s.flush()
        alerts_service.recompute(s)

    return _q


# --------------------------------------------------------------------------
# Worklists
# --------------------------------------------------------------------------


def lock_worklist(worklist_id, locked_by_name=None):
    def _q(s: Session) -> int | None:
        wl = s.get(M.Worklist, worklist_id)
        if not wl:
            return None
        if wl.locked:
            return wl.id
        wl.locked = True
        wl.locked_at = datetime.now()
        wl.locked_by_name = (locked_by_name or None) and locked_by_name.strip()
        return wl.id

    return _q


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _name_for(week_starting: date) -> str:
    """"Week N Month YYYY" label, matching the route helper exactly."""
    first = week_starting.replace(day=1)
    first_monday = (
        _monday_of(first)
        if first.weekday() == 0
        else first + timedelta(days=(7 - first.weekday()) % 7)
    )
    week_index = ((week_starting - first_monday).days // 7) + 1
    return f"Week {week_index} {week_starting.strftime('%B %Y')}"


def create_worklist(week_starting):
    """Create (or find) the base worklist for a week and seed recurring tasks.

    Returns the worklist id. If a base worklist already exists for that
    Monday it's returned unchanged (matching the route's idempotent create
    -> setup behaviour). Snaps any in-week date to its Monday.
    """
    def _q(s: Session) -> int:
        monday = _parse_date(week_starting)
        if monday is None:
            raise ValidationError("a week start date is required (YYYY-MM-DD)")
        if monday.weekday() != 0:
            monday = _monday_of(monday)
        existing = s.scalar(
            select(M.Worklist).where(
                M.Worklist.week_starting == monday,
                M.Worklist.active == True,  # noqa: E712
                M.Worklist.parent_id.is_(None),
            )
        )
        if existing:
            return existing.id
        wl = M.Worklist(week_starting=monday, name=_name_for(monday), version=1)
        s.add(wl)
        s.flush()
        generate_for_worklist(s, wl)
        return wl.id

    return _q


def generate_worklist_tasks(worklist_id):
    """(Re)generate recurring-template tasks for an existing worklist."""
    def _q(s: Session) -> int | None:
        wl = s.get(M.Worklist, worklist_id)
        if not wl:
            return None
        if wl.locked:
            raise ValidationError("amend the worklist before generating tasks")
        generate_for_worklist(s, wl)
        return wl.id

    return _q


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------


def create_task(worklist_id, *, name, scheduled_date=None, category_id=None,
                description=None, person_ids=(), poic_person_id=None,
                external_poic_name=None):
    """Create a TaskInstance under a worklist and attach assignees.

    Mirrors the route's POIC defaulting: a single assignee, or the first of
    several, becomes the lead when none is chosen explicitly and there's no
    external lead.
    """
    def _q(s: Session) -> int:
        wl = s.get(M.Worklist, worklist_id)
        if wl is None:
            raise ValidationError("worklist not found")
        if wl.locked:
            raise ValidationError("worklist is locked; amend it before editing tasks")
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValidationError("task name is required")
        inst = M.TaskInstance(
            worklist_id=worklist_id,
            scheduled_date=_parse_date(scheduled_date),
            category_id=int(category_id) if category_id else None,
            name=clean_name[:240],
            description=description or None,
            status="open",
        )
        s.add(inst)
        s.flush()
        pids = [int(x) for x in person_ids if str(x).strip()]
        poic = int(poic_person_id) if poic_person_id else None
        ext = (external_poic_name or "").strip()
        if pids and poic is None and not ext:
            poic = pids[0]
        for pid in pids:
            s.add(M.TaskAssignment(instance_id=inst.id, person_id=pid,
                                   is_poic=(pid == poic)))
        if ext:
            s.add(M.TaskAssignment(instance_id=inst.id, external_poic_name=ext,
                                   is_poic=True))
        s.flush()
        return inst.id

    return _q


def update_task(task_id, *, name, scheduled_date=None, category_id=None,
                status="open", hours=None, description=None,
                completion_notes=None):
    def _q(s: Session) -> int | None:
        inst = s.get(M.TaskInstance, task_id)
        if not inst:
            return None
        wl = s.get(M.Worklist, inst.worklist_id) if inst.worklist_id else None
        if wl is not None and wl.locked:
            raise ValidationError("worklist is locked; amend it before editing tasks")
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValidationError("task name is required")
        inst.name = clean_name[:240]
        inst.scheduled_date = _parse_date(scheduled_date)
        inst.category_id = int(category_id) if category_id else None
        inst.status = status
        inst.hours = float(hours) if hours not in (None, "") else None
        inst.description = description or None
        inst.completion_notes = completion_notes or None
        if status == "done" and inst.completed_at is None:
            inst.completed_at = datetime.now()
        if status != "done":
            inst.completed_at = None
        return inst.worklist_id

    return _q


def archive_task(task_id, reason=""):
    def _q(s: Session) -> int | None:
        inst = s.get(M.TaskInstance, task_id)
        if not inst:
            return None
        wl = s.get(M.Worklist, inst.worklist_id) if inst.worklist_id else None
        if wl is not None and wl.locked:
            raise ValidationError("worklist is locked; amend it before editing tasks")
        inst.active = False
        inst.archived_at = datetime.now()
        inst.archived_reason = reason or None
        return inst.worklist_id

    return _q


def task_categories_choices():
    """Return [(id, name)] for active task categories."""
    def _q(s: Session):
        rows = s.scalars(
            select(M.TaskCategory)
            .where(M.TaskCategory.active == True)  # noqa: E712
            .order_by(M.TaskCategory.display_order)
        ).all()
        return [(c.id, c.name) for c in rows]

    return _q


# --------------------------------------------------------------------------
# Qualifications (per-person assignment / status changes)
# --------------------------------------------------------------------------


def _parse_dt(value: str | None):
    """Parse an ISO date or datetime into a datetime (the PersonQual
    timestamp columns are DateTime). Accepts bare YYYY-MM-DD."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        d = _parse_date(value)
        return datetime(d.year, d.month, d.day) if d else None


def qual_choices(exclude_person_id: int | None = None):
    """Return [(id, name)] for active qualifications.

    If ``exclude_person_id`` is given, quals the person already holds a
    current row for are dropped, so the assign form only offers new ones.
    """
    def _q(s: Session):
        held: set[int] = set()
        if exclude_person_id is not None:
            held = set(s.scalars(
                select(M.PersonQual.qual_id).where(
                    M.PersonQual.person_id == exclude_person_id,
                    M.PersonQual.valid_to.is_(None),
                )
            ).all())
        rows = s.scalars(
            select(M.Qualification)
            .where(M.Qualification.active == True)  # noqa: E712
            .order_by(M.Qualification.display_order, M.Qualification.name)
        ).all()
        return [(q.id, q.name) for q in rows if q.id not in held]

    return _q


def assign_quals(person_id, *, qual_ids, status="assigned", started_at=None,
                 achieved_at=None, notes=None):
    """Bulk-assign one or more qualifications to a person.

    ``status`` / ``started_at`` / ``achieved_at`` / ``notes`` apply to every
    selected qual. ``expires_at`` is derived from the qual's
    ``validity_period_days`` when an achieved date is given, matching the
    route. Returns the count created.
    """
    def _q(s: Session) -> int:
        p = s.get(M.Person, person_id)
        if not p:
            raise ValidationError("person not found")
        ids = [int(x) for x in (qual_ids or []) if str(x).strip()]
        if not ids:
            raise ValidationError("select at least one qualification")
        started_dt = _parse_dt(started_at)
        achieved_dt = _parse_dt(achieved_at)
        today = date.today()
        created = 0
        for qid in ids:
            q = s.get(M.Qualification, qid)
            if not q:
                continue
            expires_dt = None
            if achieved_dt and q.validity_period_days:
                expires_dt = achieved_dt + timedelta(days=q.validity_period_days)
            s.add(M.PersonQual(
                person_id=p.id, qual_id=q.id, status=status,
                started_at=started_dt, achieved_at=achieved_dt,
                expires_at=expires_dt, notes=notes or None, valid_from=today))
            created += 1
        # Flush inside the tenant context so the org_id autofill listener
        # populates org_id before the (possibly out-of-context) commit.
        s.flush()
        return created

    return _q


def update_person_qual(person_id, pq_id, *, status, started_at=None,
                       achieved_at=None, notes=None, effective_date=None):
    """Change a current person-qual: close the active row and append a new
    one capturing the change (the effective-dated audit pattern the route
    uses). Refuses to edit a historical row."""
    def _q(s: Session) -> int | None:
        pq = s.get(M.PersonQual, pq_id)
        if not pq or pq.person_id != person_id:
            raise ValidationError("qualification record not found")
        if pq.valid_to is not None:
            raise ValidationError("cannot edit a historical row directly")
        q = s.get(M.Qualification, pq.qual_id)
        eff_date = _parse_date(effective_date) or date.today()
        started_dt = _parse_dt(started_at)
        achieved_dt = _parse_dt(achieved_at)
        expires_dt = None
        if achieved_dt and q and q.validity_period_days:
            expires_dt = achieved_dt + timedelta(days=q.validity_period_days)
        pq.valid_to = eff_date
        s.add(M.PersonQual(
            person_id=person_id, qual_id=pq.qual_id, status=status,
            started_at=started_dt, achieved_at=achieved_dt,
            expires_at=expires_dt, notes=notes or None, valid_from=eff_date))
        s.flush()
        return person_id

    return _q


# --------------------------------------------------------------------------
# Qualification catalog (the qual definitions, not per-person rows)
# --------------------------------------------------------------------------


def create_qual(name):
    """Add a qualification to the catalog. The web UI only exposes the
    name; other fields (code/category/validity) stay null as there."""
    def _q(s: Session) -> int:
        clean = (name or "").strip()
        if not clean:
            raise ValidationError("a name is required")
        last_pos = s.scalar(
            select(M.Qualification.display_order)
            .order_by(M.Qualification.display_order.desc()).limit(1)
        ) or 0
        q = M.Qualification(name=clean, display_order=last_pos + 1)
        s.add(q)
        s.flush()
        return q.id

    return _q


def update_qual(qual_id, name):
    def _q(s: Session) -> int | None:
        q = s.get(M.Qualification, qual_id)
        if not q:
            return None
        clean = (name or "").strip()
        if not clean:
            raise ValidationError("a name is required")
        q.name = clean
        return q.id

    return _q


def archive_qual(qual_id, reason=""):
    def _q(s: Session) -> int | None:
        q = s.get(M.Qualification, qual_id)
        if not q:
            return None
        q.active = False
        q.archived_at = datetime.now()
        q.archived_reason = reason or None
        return q.id

    return _q


# --------------------------------------------------------------------------
# Task assignments (add / change lead / remove, after task creation)
# --------------------------------------------------------------------------


def _ensure_task_unlocked(s: Session, inst: M.TaskInstance) -> None:
    wl = s.get(M.Worklist, inst.worklist_id) if inst.worklist_id else None
    if wl is not None and wl.locked:
        raise ValidationError("worklist is locked; amend it before editing tasks")


def add_assignment(task_id, *, person_id=None, external_poic_name=None,
                   is_poic=False):
    """Attach a person (or an off-roster lead) to a task. Setting POIC
    demotes any current lead, matching the route."""
    def _q(s: Session) -> int | None:
        inst = s.get(M.TaskInstance, task_id)
        if not inst:
            return None
        _ensure_task_unlocked(s, inst)
        ext = (external_poic_name or "").strip() or None
        if not person_id and not ext:
            raise ValidationError("pick a person or enter an off-roster lead name")
        if is_poic:
            for a in s.scalars(select(M.TaskAssignment).where(
                M.TaskAssignment.instance_id == inst.id,
                M.TaskAssignment.is_poic == True,  # noqa: E712
                M.TaskAssignment.active == True,  # noqa: E712
            )).all():
                a.is_poic = False
        s.add(M.TaskAssignment(
            instance_id=inst.id, person_id=int(person_id) if person_id else None,
            external_poic_name=ext, is_poic=bool(is_poic)))
        s.flush()
        return inst.id

    return _q


def set_assignment_poic(task_id, assignment_id):
    """Make one assignment the lead, demoting the rest."""
    def _q(s: Session) -> int | None:
        a = s.get(M.TaskAssignment, assignment_id)
        if not a or a.instance_id != task_id:
            return None
        inst = s.get(M.TaskInstance, task_id)
        _ensure_task_unlocked(s, inst)
        for other in s.scalars(select(M.TaskAssignment).where(
            M.TaskAssignment.instance_id == task_id,
            M.TaskAssignment.is_poic == True,  # noqa: E712
            M.TaskAssignment.active == True,  # noqa: E712
        )).all():
            other.is_poic = False
        a.is_poic = True
        return task_id

    return _q


def remove_assignment(task_id, assignment_id):
    def _q(s: Session) -> int | None:
        a = s.get(M.TaskAssignment, assignment_id)
        if not a or a.instance_id != task_id:
            return None
        inst = s.get(M.TaskInstance, task_id)
        _ensure_task_unlocked(s, inst)
        a.active = False
        a.archived_at = datetime.now()
        return task_id

    return _q


# --------------------------------------------------------------------------
# Task templates (recurring task definitions)
# --------------------------------------------------------------------------


def _build_recurrence(kind, *, weekdays=None, n=None, weekday=None, day=None,
                      anchor=None) -> dict | None:
    kind = (kind or "none").strip()
    if kind in ("none", ""):
        return None
    if kind == "daily":
        return {"kind": "daily"}
    if kind == "weekdays":
        return {"kind": "weekdays", "weekdays": [int(x) for x in (weekdays or [])]}
    if kind == "every_n_weeks":
        return {"kind": "every_n_weeks", "n": int(n or 2),
                "weekday": int(weekday or 0), "anchor": anchor or None}
    if kind == "monthly_date":
        return {"kind": "monthly_date", "day": int(day or 1)}
    if kind == "monthly_nth_weekday":
        return {"kind": "monthly_nth_weekday", "n": int(n or 1),
                "weekday": int(weekday or 0)}
    return None


def create_template(*, name, category_id=None, description=None,
                    estimated_hours=None, carry_over_policy="auto_same_person",
                    recurrence=None, required_drivers_license=False,
                    required_duty_section=None, required_quals=(), notes=None,
                    splittable=False, reassignable=True):
    def _q(s: Session) -> int:
        clean = (name or "").strip()
        if not clean:
            raise ValidationError("a name is required")
        tmpl = M.TaskTemplate(
            name=clean[:240],
            category_id=int(category_id) if category_id else None,
            description=description or None,
            estimated_hours=float(estimated_hours) if estimated_hours not in
            (None, "") else None,
            splittable=bool(splittable),
            reassignable=bool(reassignable),
            carry_over_policy=carry_over_policy or "auto_same_person",
            recurrence_rule=recurrence,
            required_drivers_license=bool(required_drivers_license),
            required_duty_section=int(required_duty_section)
            if required_duty_section else None,
            notes=notes or None)
        s.add(tmpl)
        s.flush()
        for qid in required_quals or []:
            s.add(M.TaskTemplateRequiredQual(task_template_id=tmpl.id,
                                             qual_id=int(qid)))
        s.flush()
        return tmpl.id

    return _q


def update_template(template_id, *, name, category_id=None, description=None,
                    estimated_hours=None, carry_over_policy="auto_same_person",
                    recurrence=None, required_drivers_license=False,
                    required_duty_section=None, required_quals=(), notes=None,
                    splittable=False, reassignable=True):
    def _q(s: Session) -> int | None:
        tmpl = s.get(M.TaskTemplate, template_id)
        if not tmpl:
            return None
        clean = (name or "").strip()
        if not clean:
            raise ValidationError("a name is required")
        tmpl.name = clean[:240]
        tmpl.category_id = int(category_id) if category_id else None
        tmpl.description = description or None
        tmpl.estimated_hours = (float(estimated_hours)
                                if estimated_hours not in (None, "") else None)
        tmpl.splittable = bool(splittable)
        tmpl.reassignable = bool(reassignable)
        tmpl.carry_over_policy = carry_over_policy or "auto_same_person"
        tmpl.recurrence_rule = recurrence
        tmpl.required_drivers_license = bool(required_drivers_license)
        tmpl.required_duty_section = (int(required_duty_section)
                                      if required_duty_section else None)
        tmpl.notes = notes or None
        s.query(M.TaskTemplateRequiredQual).filter(
            M.TaskTemplateRequiredQual.task_template_id == template_id).delete()
        for qid in required_quals or []:
            s.add(M.TaskTemplateRequiredQual(task_template_id=tmpl.id,
                                             qual_id=int(qid)))
        s.flush()
        return tmpl.id

    return _q


def archive_template(template_id, reason=""):
    def _q(s: Session) -> int | None:
        tmpl = s.get(M.TaskTemplate, template_id)
        if not tmpl:
            return None
        tmpl.active = False
        tmpl.archived_at = datetime.now()
        tmpl.archived_reason = reason or None
        return tmpl.id

    return _q


# --------------------------------------------------------------------------
# Worklists: update name/notes, amend (clone a locked week), archive,
# carry-over apply
# --------------------------------------------------------------------------


def update_worklist(worklist_id, *, name, notes=None):
    def _q(s: Session) -> int | None:
        wl = s.get(M.Worklist, worklist_id)
        if not wl:
            return None
        if wl.locked:
            raise ValidationError("worklist is locked; create an amendment instead")
        clean = (name or "").strip()
        if not clean:
            raise ValidationError("a name is required")
        wl.name = clean
        wl.notes = notes or None
        return wl.id

    return _q


def amend_worklist(worklist_id, *, amendment_reason, operator_name=None):
    """Clone a locked worklist into a new editable version, duplicating its
    tasks + assignments. Returns the new worklist id."""
    def _q(s: Session) -> int:
        parent = s.get(M.Worklist, worklist_id)
        if not parent:
            raise ValidationError("worklist not found")
        if not parent.locked:
            raise ValidationError("only locked worklists are amended")
        reason = (amendment_reason or "").strip()
        if not reason:
            raise ValidationError("an amendment reason is required")
        sibling_version = s.scalar(
            select(M.Worklist.version)
            .where((M.Worklist.id == parent.id) |
                   (M.Worklist.parent_id == parent.id))
            .order_by(M.Worklist.version.desc()).limit(1)) or 1
        clone = M.Worklist(
            week_starting=parent.week_starting, name=parent.name,
            version=sibling_version + 1, parent_id=parent.id, notes=parent.notes,
            amended_at=datetime.now(), amendment_reason=reason,
            operator_name=(operator_name or None) and operator_name.strip())
        s.add(clone)
        s.flush()
        from sqlalchemy.orm import selectinload
        instances = s.scalars(
            select(M.TaskInstance).where(
                M.TaskInstance.worklist_id == parent.id,
                M.TaskInstance.active == True,  # noqa: E712
            ).options(selectinload(M.TaskInstance.assignments))).all()
        for inst in instances:
            new_inst = M.TaskInstance(
                template_id=inst.template_id, worklist_id=clone.id,
                scheduled_date=inst.scheduled_date, category_id=inst.category_id,
                name=inst.name, description=inst.description, status=inst.status,
                hours=inst.hours, notes=inst.notes,
                completion_notes=inst.completion_notes,
                completed_at=inst.completed_at, carried_from_instance_id=inst.id,
                display_order=inst.display_order)
            s.add(new_inst)
            s.flush()
            for a in inst.assignments:
                if not a.active:
                    continue
                s.add(M.TaskAssignment(
                    instance_id=new_inst.id, person_id=a.person_id,
                    is_poic=a.is_poic, external_poic_name=a.external_poic_name,
                    completed=a.completed, completion_notes=a.completion_notes,
                    hours_worked=a.hours_worked, display_order=a.display_order))
        s.flush()
        return clone.id

    return _q


def archive_worklist(worklist_id, reason=""):
    def _q(s: Session) -> int | None:
        wl = s.get(M.Worklist, worklist_id)
        if not wl:
            return None
        wl.active = False
        wl.archived_at = datetime.now()
        wl.archived_reason = reason or None
        return wl.id

    return _q


def apply_carry_overs(worklist_id, decisions):
    """Apply a batch of carry-over decisions.

    ``decisions`` maps instance_id -> dict(action=..., reassign_person_ids=[],
    poic_person_id=...). Actions: carry | reassign | complete | discard |
    leave. Returns the count of decisions acted on.
    """
    def _q(s: Session) -> int:
        wl = s.get(M.Worklist, worklist_id)
        if not wl:
            raise ValidationError("worklist not found")
        if wl.locked:
            raise ValidationError("amend the worklist before processing carry-overs")
        candidates = {c.instance.id: c for c in find_pending_carry_overs(s, wl)}
        acted = 0
        for iid, decision in (decisions or {}).items():
            c = candidates.get(iid)
            if c is None:
                continue
            action = decision.get("action", "leave")
            if action == "reassign":
                apply_carry_over(
                    s, wl, c, "reassign",
                    reassign_person_ids=decision.get("reassign_person_ids") or [],
                    new_poic_person_id=decision.get("poic_person_id"))
            else:
                apply_carry_over(s, wl, c, action)
            acted += 1
        s.flush()
        return acted

    return _q


def task_template_choices():
    """Return [(id, name)] for active task templates."""
    def _q(s: Session):
        rows = s.scalars(
            select(M.TaskTemplate)
            .where(M.TaskTemplate.active == True)  # noqa: E712
            .order_by(M.TaskTemplate.display_order, M.TaskTemplate.id)).all()
        return [(t.id, t.name) for t in rows]

    return _q
