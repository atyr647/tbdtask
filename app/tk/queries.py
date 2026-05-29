"""Read/write functions that bridge ``app.services`` to the Tk DTOs.

Every public function here is meant to be passed to ``context.read`` /
``context.write``; it receives a live ``Session`` and must return only
detached DTOs from :mod:`app.tk.dto`. This is the single place that knows
both the ORM and the UI value objects — screens import from here, never
from ``app.models`` directly.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import models as M
from ..data import ranks as rank_catalog
from ..services import alerts as alerts_service
from ..services import personnel_stats
from ..services.absence_calendar import build_calendar
from ..services.availability import get_day_report
from ..services.carry_over import find_pending_carry_overs
from ..services.qual_overview import build_qual_overview
from ..services.recurrence import dates_in_window
from ..services.worklist_view import build_week_view, build_week_grid
from . import dto


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _current(rows, attr):
    for r in rows:
        if r.valid_to is None:
            return getattr(r, attr)
    return None


def _current_status(person: M.Person):
    for r in person.roster_statuses:
        if r.valid_to is None:
            return r.status
    return None


def _next_monday(today: date | None = None) -> date:
    today = today or date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


_TYPE_LABELS = {
    "prd_apply": "PRD — apply for orders",
    "prd_2month": "PRD — 2 months out",
    "prd_1month": "PRD — 1 month out",
    "prd_weekly": "PRD — within a month",
    "prd_passed": "PRD — passed",
    "qual_expiring": "Qualification expiring",
    "qual_expired": "Qualification expired",
    "worklist_carry_over_pending": "Pending carry-over",
}


def _alert_dto(s: Session, a: M.Alert) -> dto.AlertRowDTO:
    person_label = None
    if a.person_id:
        person_label = s.scalar(
            select(M.Person.full_display).where(M.Person.id == a.person_id)
        )
    payload = a.payload or {}
    detail_bits: list[str] = []
    if "prd" in payload:
        detail_bits.append(f"PRD {payload['prd']}")
    if "qual_name" in payload:
        detail_bits.append(str(payload["qual_name"]))
    for key in ("expires_on", "expired_on"):
        if key in payload:
            detail_bits.append(f"{key.replace('_', ' ')} {payload[key]}")
    if "days" in payload:
        detail_bits.append(f"{payload['days']} days")
    if "count" in payload:
        detail_bits.append(f"{payload['count']} task(s)")
    return dto.AlertRowDTO(
        id=a.id,
        type_label=_TYPE_LABELS.get(a.alert_type, a.alert_type),
        severity=a.severity,
        person_label=person_label,
        detail=" · ".join(detail_bits),
        created_at=a.created_at,
        snoozed_until=a.snoozed_until,
        dismissed_at=a.dismissed_at,
        resolved_at=a.resolved_at,
        is_prd=a.alert_type.startswith("prd_"),
        has_person=a.person_id is not None,
    )


# --------------------------------------------------------------------------
# Today / day overview
# --------------------------------------------------------------------------


def day_view(on_date: date):
    def _q(s: Session) -> dto.DayViewDTO:
        report = get_day_report(s, on_date)
        absent_rows = [
            dto.PersonDayDTO(
                person_id=r.person.id,
                name=r.person.full_display,
                rate=r.rate,
                duty_section=r.duty_section,
                code=r.code,
                partial=r.partial,
                start_time=r.start_time,
                end_time=r.end_time,
                reason=r.reason,
            )
            for r in report.rows
            if r.absence is not None
        ]

        scheduled = list(
            s.scalars(
                select(M.TaskInstance)
                .where(
                    M.TaskInstance.active == True,  # noqa: E712
                    M.TaskInstance.scheduled_date == on_date,
                )
                .options(
                    selectinload(M.TaskInstance.assignments).selectinload(
                        M.TaskAssignment.person
                    ),
                    selectinload(M.TaskInstance.category),
                )
                .order_by(M.TaskInstance.id)
            ).all()
        )
        scheduled_template_ids = {
            i.template_id for i in scheduled if i.template_id is not None
        }
        tasks: list[dto.DayTaskDTO] = []
        for inst in scheduled:
            assignments = [a for a in inst.assignments if a.active]
            assignees = [
                (a.person.full_display if a.person else (a.external_poic_name or "(ext)"))
                for a in assignments
            ]
            poic_label = None
            for a in assignments:
                if a.is_poic:
                    poic_label = a.person.full_display if a.person else a.external_poic_name
                    break
            tasks.append(
                dto.DayTaskDTO(
                    name=inst.name,
                    category=inst.category.name if inst.category else None,
                    assignees=assignees,
                    poic_label=poic_label,
                    status=inst.status,
                    source="scheduled",
                )
            )

        templates = list(
            s.scalars(
                select(M.TaskTemplate)
                .where(
                    M.TaskTemplate.active == True,  # noqa: E712
                    M.TaskTemplate.recurrence_rule.is_not(None),
                )
                .options(selectinload(M.TaskTemplate.category))
            ).all()
        )
        for t in templates:
            if t.id in scheduled_template_ids:
                continue
            if not dates_in_window(t.recurrence_rule or {}, on_date, days=1):
                continue
            tasks.append(
                dto.DayTaskDTO(
                    name=t.name,
                    category=t.category.name if t.category else None,
                    assignees=[],
                    poic_label=None,
                    status="recurring",
                    source="recurring",
                )
            )

        return dto.DayViewDTO(
            on_date=on_date,
            prev_day=(on_date - timedelta(days=1)).isoformat(),
            next_day=(on_date + timedelta(days=1)).isoformat(),
            today_iso=date.today().isoformat(),
            total=report.total,
            present_full=report.present_full,
            full_absent=report.full_absent,
            partial_absent=report.partial_absent,
            percent_present=report.percent_present,
            by_code=report.by_code,
            absent_rows=absent_rows,
            tasks=tasks,
        )

    return _q


def home():
    def _q(s: Session) -> dto.HomeDTO:
        alerts_service.recompute(s)
        day = day_view(date.today())(s)
        monday = date.today() - timedelta(days=date.today().weekday())
        wl = s.scalar(
            select(M.Worklist).where(
                M.Worklist.active == True,  # noqa: E712
                M.Worklist.week_starting == monday,
                M.Worklist.parent_id.is_(None),
            )
        )
        current_wl = None
        if wl:
            current_wl = dto.WorklistRowDTO(
                id=wl.id,
                name=wl.name,
                week_starting=wl.week_starting,
                version=wl.version,
                locked=wl.locked,
                amendment_count=0,
            )
        rows = [_alert_dto(s, a) for a in alerts_service.active_alerts(s)]
        urgent = sum(1 for r in rows if r.severity in ("urgent", "critical", "danger"))
        warn = sum(1 for r in rows if r.severity in ("warn", "warning"))
        info = sum(1 for r in rows if r.severity not in (
            "urgent", "critical", "danger", "warn", "warning"))
        return dto.HomeDTO(
            day=day,
            current_worklist=current_wl,
            alerts=rows,
            urgent_count=urgent,
            warn_count=warn,
            info_count=info,
        )

    return _q


# --------------------------------------------------------------------------
# Personnel
# --------------------------------------------------------------------------


def personnel_active():
    def _q(s: Session) -> dict[str, list[dto.PersonRowDTO]]:
        people = s.scalars(
            select(M.Person)
            .where(M.Person.active == True)  # noqa: E712
            .options(
                selectinload(M.Person.rates),
                selectinload(M.Person.duty_sections),
                selectinload(M.Person.roster_statuses),
            )
            .order_by(M.Person.display_order)
        ).all()
        groups = ["Leadership", "Senior", "Professional", "Associate", "Other"]
        grouped: dict[str, list[dto.PersonRowDTO]] = {g: [] for g in groups}
        for p in people:
            if _current_status(p) == "incoming":
                continue
            rate = _current(p.rates, "rate")
            paygrade = _current(p.rates, "paygrade")
            group = rank_catalog.group_for(rate, paygrade)
            grouped.setdefault(group, []).append(
                dto.PersonRowDTO(
                    id=p.id,
                    name=p.full_display,
                    rate=rate,
                    paygrade=paygrade,
                    position=p.position,
                    duty_section=_current(p.duty_sections, "duty_section"),
                    group=group,
                    notes=p.notes,
                )
            )
        return grouped

    return _q


def personnel_incoming():
    def _q(s: Session) -> list[dto.IncomingRowDTO]:
        people = s.scalars(
            select(M.Person)
            .where(M.Person.active == True)  # noqa: E712
            .options(
                selectinload(M.Person.rates),
                selectinload(M.Person.roster_statuses),
                selectinload(M.Person.sponsor),
            )
            .order_by(M.Person.arrival_date.is_(None), M.Person.arrival_date)
        ).all()
        rows: list[dto.IncomingRowDTO] = []
        for p in people:
            if _current_status(p) != "incoming":
                continue
            checks = [
                p.orders_received,
                p.itinerary_received,
                p.aob_scheduled,
                p.barracks_assigned,
            ]
            rows.append(
                dto.IncomingRowDTO(
                    id=p.id,
                    name=p.full_display,
                    rate=_current(p.rates, "rate"),
                    arrival_date=p.arrival_date,
                    sponsor_label=p.sponsor.full_display if p.sponsor else None,
                    checklist_done=sum(1 for c in checks if c),
                    checklist_total=len(checks),
                    orders_received=p.orders_received,
                    itinerary_received=p.itinerary_received,
                    aob_scheduled=p.aob_scheduled,
                    barracks_assigned=p.barracks_assigned,
                )
            )
        return rows

    return _q


def personnel_departed():
    def _q(s: Session) -> list[dto.DepartedRowDTO]:
        people = s.scalars(
            select(M.Person)
            .where(M.Person.active == False)  # noqa: E712
            .options(selectinload(M.Person.rates))
            .order_by(M.Person.archived_at.desc())
        ).all()
        return [
            dto.DepartedRowDTO(
                id=p.id,
                name=p.full_display,
                rate=_current(p.rates, "rate"),
                departed_at=p.archived_at,
                reason=p.archived_reason,
            )
            for p in people
        ]

    return _q


def person_profile(person_id: int):
    def _q(s: Session) -> dto.PersonProfileDTO | None:
        p = s.get(M.Person, person_id)
        if p is None:
            return None

        def eff_rows(rel, label_fn):
            return [
                dto.EffectiveRowDTO(
                    label=label_fn(r), valid_from=r.valid_from, valid_to=r.valid_to
                )
                for r in sorted(rel, key=lambda r: r.valid_from, reverse=True)
            ]

        rates = eff_rows(
            p.rates,
            lambda r: f"{r.rate}" + (f" ({r.paygrade})" if r.paygrade else ""),
        )
        duties = eff_rows(p.duty_sections, lambda r: f"Section {r.duty_section}")
        prds = eff_rows(
            p.prds, lambda r: f"{r.prd_date.isoformat()} · {r.change_reason}"
        )
        dls = eff_rows(
            p.drivers_licenses,
            lambda r: ("Licensed" if r.has_license else "No license")
            + (f" · exp {r.expires_on.isoformat()}" if r.expires_on else ""),
        )

        qual_rows = list(
            s.execute(
                select(M.PersonQual, M.Qualification)
                .join(M.Qualification, M.Qualification.id == M.PersonQual.qual_id)
                .where(
                    M.PersonQual.person_id == person_id,
                    M.PersonQual.valid_to.is_(None),
                )
                .order_by(M.Qualification.display_order)
            ).all()
        )
        quals = [
            dto.QualLineDTO(
                name=q.name,
                status=pq.status,
                achieved_at=pq.achieved_at,
                expires_at=pq.expires_at,
            )
            for pq, q in qual_rows
        ]

        stats = personnel_stats.stats_for(s, person_id, window_days=180)
        return dto.PersonProfileDTO(
            id=p.id,
            name=p.full_display,
            active=p.active,
            position=p.position,
            notes=p.notes,
            rates=rates,
            duty_sections=duties,
            prds=prds,
            drivers_licenses=dls,
            quals=quals,
            total_completed=stats.total_completed,
            total_hours=stats.total_hours,
            poic_count=stats.poic_count,
            by_category=[
                dto.CategoryStatDTO(c.name, c.count, c.hours, c.poic_count)
                for c in stats.by_category
            ],
        )

    return _q


# --------------------------------------------------------------------------
# Qualifications
# --------------------------------------------------------------------------


def quals_overview(threshold: int = 2):
    def _q(s: Session) -> list[dto.QualSummaryDTO]:
        summaries = build_qual_overview(s, qualified_threshold=threshold)
        return [
            dto.QualSummaryDTO(
                name=su.qual.name,
                counts=dict(su.counts),
                qualified_names=list(su.qualified_names),
                in_progress_names=list(su.in_progress_names),
                dinq_names=list(su.dinq_names),
                expiring_soon=list(su.expiring_soon),
                gap=su.gap,
            )
            for su in summaries
        ]

    return _q


def qual_matrix():
    def _q(s: Session) -> dto.MatrixDTO:
        quals = list(
            s.scalars(
                select(M.Qualification)
                .where(M.Qualification.active == True)  # noqa: E712
                .order_by(M.Qualification.display_order, M.Qualification.name)
            ).all()
        )
        people = list(
            s.scalars(
                select(M.Person)
                .where(M.Person.active == True)  # noqa: E712
                .options(selectinload(M.Person.rates))
                .order_by(M.Person.display_order)
            ).all()
        )
        pq_rows = s.execute(
            select(
                M.PersonQual.person_id, M.PersonQual.qual_id, M.PersonQual.status
            ).where(M.PersonQual.valid_to.is_(None))
        ).all()
        status_map = {(pid, qid): st for pid, qid, st in pq_rows}
        rows = []
        for p in people:
            if _current_status(p) == "incoming":
                continue
            rows.append(
                dto.MatrixRowDTO(
                    name=p.full_display,
                    title=_current(p.rates, "rate"),
                    cells=[status_map.get((p.id, q.id)) for q in quals],
                )
            )
        return dto.MatrixDTO(qual_names=[q.name for q in quals], rows=rows)

    return _q


# --------------------------------------------------------------------------
# Absences
# --------------------------------------------------------------------------


def absence_list():
    def _q(s: Session) -> list[dto.AbsenceRowDTO]:
        today = date.today()
        rows = s.scalars(
            select(M.Absence)
            .where(M.Absence.active == True)  # noqa: E712
            .options(selectinload(M.Absence.person), selectinload(M.Absence.code))
            .order_by(M.Absence.start_date.desc())
        ).all()
        out = []
        for a in rows:
            out.append(
                dto.AbsenceRowDTO(
                    id=a.id,
                    person_name=a.person.full_display,
                    code=a.code.code,
                    start_date=a.start_date,
                    end_date=a.end_date,
                    partial=bool(a.start_time or a.end_time),
                    reason=a.reason,
                )
            )
        # Most-recent-first, but surface anything still current/future first.
        out.sort(key=lambda r: (r.end_date < today, -r.start_date.toordinal()))
        return out

    return _q


def absence_calendar(start: date, days: int = 28):
    def _q(s: Session) -> dto.CalendarDTO:
        view = build_calendar(s, start, days)
        return dto.CalendarDTO(
            start_date=view.start_date,
            prev_start=(view.start_date - timedelta(days=days)).isoformat(),
            next_start=(view.start_date + timedelta(days=days)).isoformat(),
            days=list(view.days),
            month_groups=list(view.month_groups),
            rows=[
                dto.CalendarRowDTO(
                    name=r.person.full_display,
                    cells=[
                        dto.CalendarCellDTO(c.code, c.partial, c.reason) for c in r.cells
                    ],
                )
                for r in view.rows
            ],
            daily_full=list(view.daily_full),
            daily_partial=list(view.daily_partial),
            daily_percent_present=list(view.daily_percent_present),
            daily_total=view.daily_total,
        )

    return _q


# --------------------------------------------------------------------------
# Worklists
# --------------------------------------------------------------------------


def _wl_row(w: M.Worklist, amendment_count: int = 0) -> dto.WorklistRowDTO:
    return dto.WorklistRowDTO(
        id=w.id,
        name=w.name,
        week_starting=w.week_starting,
        version=w.version,
        locked=w.locked,
        amendment_count=amendment_count,
    )


def worklist_list():
    def _q(s: Session) -> dto.WorklistListDTO:
        today = date.today()
        all_lists = list(
            s.scalars(
                select(M.Worklist)
                .where(M.Worklist.active == True)  # noqa: E712
                .order_by(M.Worklist.week_starting.desc(), M.Worklist.version.desc())
            ).all()
        )
        children: dict[int, int] = {}
        bases: list[M.Worklist] = []
        for w in all_lists:
            if w.parent_id:
                children[w.parent_id] = children.get(w.parent_id, 0) + 1
            else:
                bases.append(w)
        current, upcoming, archived = [], [], []
        for w in bases:
            row = _wl_row(w, children.get(w.id, 0))
            end = w.week_starting + timedelta(days=6)
            if w.locked or end < today:
                archived.append(row)
            elif w.week_starting > today:
                upcoming.append(row)
            else:
                current.append(row)
        return dto.WorklistListDTO(
            current=current,
            upcoming=upcoming,
            archived=archived,
            suggested_monday=_next_monday().isoformat(),
        )

    return _q


def worklist_show(worklist_id: int):
    def _q(s: Session) -> dto.WeekViewDTO | None:
        wl = s.get(M.Worklist, worklist_id)
        if wl is None:
            return None
        view = build_week_view(s, wl)
        pending = 0 if wl.locked else len(find_pending_carry_overs(s, wl))

        days: list[dto.WeekDayDTO] = []
        for d in view.days:
            persons: list[dto.WeekPersonDTO] = []
            for pb in d.person_blocks:
                tasks = [
                    dto.WeekTaskDTO(
                        id=t.instance.id,
                        name=t.instance.name,
                        status=t.instance.status,
                        category=t.category_name,
                        is_poic=t.is_poic,
                        other_assignees=list(t.other_assignees),
                    )
                    for t in pb.tasks
                ]
                if not tasks:
                    continue
                out_code = (
                    pb.availability.code
                    if pb.availability and pb.availability.absence
                    else None
                )
                persons.append(
                    dto.WeekPersonDTO(
                        name=pb.person.full_display,
                        rate=pb.rate,
                        duty_section=pb.duty_section,
                        out_code=out_code,
                        tasks=tasks,
                    )
                )
            unassigned = [
                dto.WeekTaskDTO(
                    id=t.instance.id,
                    name=t.instance.name,
                    status=t.instance.status,
                    category=t.category_name,
                    is_poic=t.is_poic,
                    other_assignees=list(t.other_assignees),
                )
                for t in d.unassigned_tasks
            ]
            out_today = [
                dto.PersonDayDTO(
                    person_id=r.person.id,
                    name=r.person.full_display,
                    rate=r.rate,
                    duty_section=r.duty_section,
                    code=r.code,
                    partial=r.partial,
                    start_time=r.start_time,
                    end_time=r.end_time,
                    reason=r.reason,
                )
                for r in d.out_today
            ]
            days.append(
                dto.WeekDayDTO(
                    on_date=d.on_date,
                    weekday=d.weekday,
                    percent_present=d.percent_present,
                    total=d.total,
                    present_full=d.present_full,
                    persons=persons,
                    unassigned=unassigned,
                    out_today=out_today,
                )
            )

        return dto.WeekViewDTO(
            id=wl.id,
            name=wl.name,
            week_starting=wl.week_starting,
            locked=wl.locked,
            locked_by=wl.locked_by_name,
            version=wl.version,
            week_total_tasks=view.week_total_tasks,
            pending_count=pending,
            days=days,
        )

    return _q


def _grid_task(t) -> dto.GridTaskDTO:
    return dto.GridTaskDTO(
        name=t.instance.name,
        category=t.category_name,
        is_poic=t.is_poic,
        external_poic=t.external_poic,
        other_assignees=list(t.other_assignees),
    )


def week_grid(worklist_id: int, days: int = 5):
    """Print-ready person-row x day-column grid DTO for the landscape PDF."""
    def _q(s: Session) -> dto.WeekGridDTO | None:
        wl = s.get(M.Worklist, worklist_id)
        if wl is None:
            return None
        grid = build_week_grid(s, wl, days=days)

        headers = [
            dto.GridHeaderDTO(
                weekday=h.weekday,
                date_label=h.on_date.strftime("%b ") + str(h.on_date.day),
                percent_present=h.percent_present,
                present_count=h.present_count,
                out_count=h.out_count,
                out_summary=list(h.out_summary),
            )
            for h in grid.headers
        ]

        rows = []
        for r in grid.rows:
            cells = []
            for c in r.cells:
                # Pre-render the absence span the same way the print template does.
                span = None
                if c.absence_start_time and c.absence_end_time:
                    span = f"{c.absence_start_time}–{c.absence_end_time}"
                elif (c.absence_start_date and c.absence_end_date
                      and c.absence_start_date != c.absence_end_date):
                    span = (f"{c.absence_start_date.strftime('%m/%d').lstrip('0')}"
                            f"–{c.absence_end_date.strftime('%m/%d').lstrip('0')}")
                cells.append(dto.GridCellDTO(
                    absence_code=c.absence_code,
                    absence_partial=c.absence_partial,
                    absence_span=span,
                    absence_reason=c.absence_reason,
                    tasks=[_grid_task(t) for t in c.tasks],
                ))
            rows.append(dto.GridRowDTO(
                name=r.person.full_display,
                duty_section=r.duty_section,
                cells=cells,
            ))

        unassigned = []
        for d, task_rows in grid.unassigned_by_day.items():
            if not task_rows:
                continue
            unassigned.append(dto.GridUnassignedDayDTO(
                day_label=d.strftime("%a ") + d.strftime("%b ") + str(d.day),
                tasks=[_grid_task(t) for t in task_rows],
            ))

        locked_label = None
        if wl.locked:
            locked_label = "Locked"
            if wl.locked_at:
                locked_label += " " + wl.locked_at.strftime("%Y-%m-%d")
            if wl.locked_by_name:
                locked_label += f" by {wl.locked_by_name}"
        amendment_label = None
        if wl.parent_id:
            amendment_label = f"Amendment v{wl.version}"
            if wl.amendment_reason:
                amendment_label += f" ({wl.amendment_reason})"

        return dto.WeekGridDTO(
            worklist_name=wl.name,
            week_starting=wl.week_starting,
            locked=wl.locked,
            locked_label=locked_label,
            amendment_label=amendment_label,
            headers=headers,
            rows=rows,
            unassigned=unassigned,
        )

    return _q


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------


def alerts_list(show: str = "active"):
    def _q(s: Session) -> list[dto.AlertRowDTO]:
        alerts_service.recompute(s)
        if show == "history":
            rows = list(
                s.scalars(
                    select(M.Alert)
                    .where(M.Alert.resolved_at.is_not(None))
                    .order_by(M.Alert.resolved_at.desc())
                    .limit(100)
                ).all()
            )
        else:
            rows = alerts_service.active_alerts(s)
        return [_alert_dto(s, a) for a in rows]

    return _q


# --------------------------------------------------------------------------
# Raw current values for pre-filling edit forms
# --------------------------------------------------------------------------


def person_current(person_id: int):
    """Current editable values for a person (raw, not labels)."""
    def _q(s: Session) -> dict | None:
        p = s.get(M.Person, person_id)
        if p is None:
            return None
        rate = _current(p.rates, "rate")
        ds = _current(p.duty_sections, "duty_section")
        prd = _current(p.prds, "prd_date")
        dl_has = _current(p.drivers_licenses, "has_license")
        dl_exp = _current(p.drivers_licenses, "expires_on")
        return {
            "last_name": p.last_name,
            "first_name": p.first_name,
            "rate": rate,
            "position": p.position,
            "notes": p.notes,
            "duty_section": ds,
            "prd_date": prd.isoformat() if prd else None,
            "has_drivers_license": bool(dl_has),
            "drivers_license_expires": dl_exp.isoformat() if dl_exp else None,
            "roster_status": _current_status(p) or "active",
            "is_incoming": _current_status(p) == "incoming",
            "arrival_date": p.arrival_date.isoformat() if p.arrival_date else None,
            "sponsor_person_id": p.sponsor_person_id,
            "orders_received": p.orders_received,
            "itinerary_received": p.itinerary_received,
            "aob_scheduled": p.aob_scheduled,
            "barracks_assigned": p.barracks_assigned,
        }

    return _q


def absence_get(absence_id: int):
    """Current values for a single absence, for the edit form."""
    def _q(s: Session) -> dict | None:
        a = s.get(M.Absence, absence_id)
        if a is None:
            return None
        return {
            "id": a.id,
            "person_id": a.person_id,
            "person_name": a.person.full_display,
            "code_id": a.code_id,
            "start_date": a.start_date.isoformat(),
            "end_date": a.end_date.isoformat(),
            "start_time": a.start_time.strftime("%H:%M") if a.start_time else None,
            "end_time": a.end_time.strftime("%H:%M") if a.end_time else None,
            "reason": a.reason,
            "notes": a.notes,
        }

    return _q


def task_get(task_id: int):
    """Current values for a single task instance, for the edit form, plus a
    rendered list of its active assignees."""
    def _q(s: Session) -> dict | None:
        inst = s.get(M.TaskInstance, task_id)
        if inst is None:
            return None
        assignments = s.scalars(
            select(M.TaskAssignment)
            .where(
                M.TaskAssignment.instance_id == inst.id,
                M.TaskAssignment.active == True,  # noqa: E712
            )
            .options(selectinload(M.TaskAssignment.person))
        ).all()
        assignee_labels = []
        for a in assignments:
            label = a.person.full_display if a.person else (a.external_poic_name or "?")
            if a.is_poic:
                label += " (POIC)"
            assignee_labels.append(label)
        return {
            "id": inst.id,
            "worklist_id": inst.worklist_id,
            "name": inst.name,
            "scheduled_date": inst.scheduled_date.isoformat()
            if inst.scheduled_date else None,
            "category_id": inst.category_id,
            "status": inst.status,
            "hours": inst.hours,
            "description": inst.description,
            "completion_notes": inst.completion_notes,
            "assignees": assignee_labels,
        }

    return _q
