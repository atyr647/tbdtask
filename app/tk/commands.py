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

DUTY_SECTIONS = (1, 2, 3, 4, 5, 6)
ROSTER_STATUSES = ("active", "incoming", "departed")
PRD_REASONS = ("initial", "extension", "correction")


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


def rate_choices() -> list[str]:
    return [e["code"] for e in rank_catalog.all_entries()]


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


def create_person(*, last_name, first_name=None, rate=None, position=None,
                  notes=None, duty_section=None, prd_date=None,
                  has_drivers_license=False, drivers_license_expires=None,
                  roster_status="active"):
    def _q(s: Session) -> int:
        today = date.today()
        full_display = f"{rate} {last_name}".strip() if rate else last_name
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
        if rate:
            s.add(M.PersonRate(person_id=p.id, rate=rate,
                               paygrade=rank_catalog.paygrade_for(rate),
                               valid_from=today))
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


def create_incoming(*, last_name, first_name=None, rate=None, notes=None,
                    arrival_date=None, sponsor_person_id=None,
                    orders_received=False, itinerary_received=False,
                    aob_scheduled=False, barracks_assigned=False):
    def _q(s: Session) -> int:
        today = date.today()
        full_display = f"{rate} {last_name}".strip() if rate else last_name
        last_pos = s.scalar(
            select(M.Person.display_order)
            .order_by(M.Person.display_order.desc()).limit(1)
        ) or 0
        p = M.Person(
            last_name=last_name, first_name=first_name,
            full_display=full_display.strip(), notes=notes,
            display_order=last_pos + 1, arrival_date=_parse_date(arrival_date),
            sponsor_person_id=int(sponsor_person_id) if sponsor_person_id else None,
            orders_received=bool(orders_received),
            itinerary_received=bool(itinerary_received),
            aob_scheduled=bool(aob_scheduled),
            barracks_assigned=bool(barracks_assigned),
        )
        s.add(p)
        s.flush()
        if rate:
            s.add(M.PersonRate(person_id=p.id, rate=rate,
                               paygrade=rank_catalog.paygrade_for(rate),
                               valid_from=today))
        s.add(M.PersonRosterStatus(person_id=p.id, status="incoming",
                                   valid_from=today))
        s.flush()
        return p.id

    return _q


def update_person(person_id, *, last_name, first_name=None, rate=None,
                  position=None, notes=None, duty_section=None, prd_date=None,
                  prd_reason="correction", has_drivers_license=False,
                  drivers_license_expires=None, roster_status="active",
                  effective_date=None):
    def _q(s: Session) -> int | None:
        p = s.get(M.Person, person_id)
        if not p:
            return None
        eff_date = _parse_date(effective_date) or date.today()
        p.last_name = last_name
        p.first_name = first_name
        p.position = position
        p.notes = notes
        p.full_display = f"{rate} {last_name}".strip() if rate else last_name
        if rate:
            eff.set_new_value(
                s, M.PersonRate, person_id=p.id, effective_date=eff_date,
                fields={"rate": rate, "paygrade": rank_catalog.paygrade_for(rate)},
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
