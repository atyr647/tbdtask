"""Detached value objects handed to the Tk widgets.

Everything the UI renders is one of these. They hold only primitives
(and other DTOs), so they survive past the SQLAlchemy session that built
them. Keeping the boundary explicit is what lets the screens stay dumb:
a widget never touches an ORM relationship and therefore never triggers a
lazy load against a closed session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time


# --------------------------------------------------------------------------
# Today / day overview
# --------------------------------------------------------------------------


@dataclass
class PersonDayDTO:
    person_id: int
    name: str
    rate: str | None
    duty_section: int | None
    code: str | None
    partial: bool
    start_time: time | None
    end_time: time | None
    reason: str | None

    @property
    def window_label(self) -> str:
        if not self.partial:
            return ""
        s = self.start_time.strftime("%H%M") if self.start_time else ""
        e = self.end_time.strftime("%H%M") if self.end_time else ""
        if s and e:
            return f"{s}–{e}"
        return s or e


@dataclass
class DayTaskDTO:
    name: str
    category: str | None
    assignees: list[str]
    poic_label: str | None
    status: str
    source: str  # "scheduled" | "recurring"


@dataclass
class DayViewDTO:
    on_date: date
    prev_day: str
    next_day: str
    today_iso: str
    total: int
    present_full: int
    full_absent: int
    partial_absent: int
    percent_present: float
    by_code: dict[str, int]
    absent_rows: list[PersonDayDTO]
    tasks: list[DayTaskDTO]


# --------------------------------------------------------------------------
# Personnel
# --------------------------------------------------------------------------


@dataclass
class PersonRowDTO:
    id: int
    name: str
    rate: str | None
    paygrade: str | None
    position: str | None
    duty_section: int | None
    group: str
    notes: str | None


@dataclass
class IncomingRowDTO:
    id: int
    name: str
    rate: str | None
    arrival_date: date | None
    sponsor_label: str | None
    checklist_done: int
    checklist_total: int
    orders_received: bool
    itinerary_received: bool
    aob_scheduled: bool
    barracks_assigned: bool


@dataclass
class DepartedRowDTO:
    id: int
    name: str
    rate: str | None
    departed_at: datetime | None
    reason: str | None


@dataclass
class EffectiveRowDTO:
    label: str
    valid_from: date
    valid_to: date | None


@dataclass
class QualLineDTO:
    name: str
    status: str
    achieved_at: datetime | None
    expires_at: datetime | None
    pq_id: int | None = None
    started_at: datetime | None = None
    due_at: date | None = None
    notes: str | None = None


@dataclass
class CategoryStatDTO:
    name: str
    count: int
    hours: float
    poic_count: int


@dataclass
class PersonProfileDTO:
    id: int
    name: str
    active: bool
    position: str | None
    notes: str | None
    rates: list[EffectiveRowDTO]
    duty_sections: list[EffectiveRowDTO]
    prds: list[EffectiveRowDTO]
    drivers_licenses: list[EffectiveRowDTO]
    quals: list[QualLineDTO]
    total_completed: int
    total_hours: float
    poic_count: int
    by_category: list[CategoryStatDTO]


# --------------------------------------------------------------------------
# Qualifications
# --------------------------------------------------------------------------


@dataclass
class QualSummaryDTO:
    name: str
    counts: dict[str, int]
    qualified_names: list[str]
    in_progress_names: list[str]
    dinq_names: list[str]
    expiring_soon: list[tuple[str, str]]
    gap: bool


@dataclass
class MatrixRowDTO:
    name: str
    title: str | None
    cells: list[str | None]  # status per qual column, aligned to qual_names


@dataclass
class MatrixDTO:
    qual_names: list[str]
    rows: list[MatrixRowDTO]


# --------------------------------------------------------------------------
# Absences
# --------------------------------------------------------------------------


@dataclass
class AbsenceRowDTO:
    id: int
    person_name: str
    code: str
    start_date: date
    end_date: date
    partial: bool
    reason: str | None


@dataclass
class CalendarCellDTO:
    code: str | None
    partial: bool
    reason: str | None


@dataclass
class CalendarRowDTO:
    name: str
    cells: list[CalendarCellDTO]


@dataclass
class CalendarDTO:
    start_date: date
    prev_start: str
    next_start: str
    days: list[date]
    month_groups: list[tuple[str, int]]
    rows: list[CalendarRowDTO]
    daily_full: list[int]
    daily_partial: list[int]
    daily_percent_present: list[float]
    daily_total: int


# --------------------------------------------------------------------------
# Worklists
# --------------------------------------------------------------------------


@dataclass
class WorklistRowDTO:
    id: int
    name: str
    week_starting: date
    version: int
    locked: bool
    amendment_count: int


@dataclass
class WorklistListDTO:
    current: list[WorklistRowDTO]
    upcoming: list[WorklistRowDTO]
    archived: list[WorklistRowDTO]
    suggested_monday: str


@dataclass
class WeekTaskDTO:
    id: int
    name: str
    status: str
    category: str | None
    is_poic: bool
    other_assignees: list[str]


@dataclass
class WeekPersonDTO:
    name: str
    rate: str | None
    duty_section: int | None
    out_code: str | None
    tasks: list[WeekTaskDTO]


@dataclass
class WeekDayDTO:
    on_date: date
    weekday: str
    percent_present: float
    total: int
    present_full: int
    persons: list[WeekPersonDTO]
    unassigned: list[WeekTaskDTO]
    out_today: list[PersonDayDTO]


@dataclass
class WeekViewDTO:
    id: int
    name: str
    week_starting: date
    locked: bool
    locked_by: str | None
    version: int
    week_total_tasks: int
    pending_count: int
    days: list[WeekDayDTO]


# --- print grid (person rows x day columns, for the landscape PDF) ---------


@dataclass
class GridTaskDTO:
    name: str
    category: str | None
    is_poic: bool
    external_poic: str | None
    other_assignees: list[str]


@dataclass
class GridCellDTO:
    absence_code: str | None
    absence_partial: bool
    absence_span: str | None  # pre-rendered "0800-1200" or "6/1-6/3"
    absence_reason: str | None
    tasks: list[GridTaskDTO]


@dataclass
class GridHeaderDTO:
    weekday: str
    date_label: str  # "Jun 1"
    percent_present: float
    present_count: int
    out_count: int
    out_summary: list[str]


@dataclass
class GridRowDTO:
    name: str
    duty_section: int | None
    cells: list[GridCellDTO]


@dataclass
class GridUnassignedDayDTO:
    day_label: str  # "Mon Jun 1"
    tasks: list[GridTaskDTO]


@dataclass
class WeekGridDTO:
    worklist_name: str
    week_starting: date
    locked: bool
    locked_label: str | None  # "Locked 2026-06-01 by Chief"
    amendment_label: str | None  # "Amendment v2 (reason)"
    headers: list[GridHeaderDTO]
    rows: list[GridRowDTO]
    unassigned: list[GridUnassignedDayDTO]


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------


@dataclass
class AlertRowDTO:
    id: int
    type_label: str
    severity: str
    person_label: str | None
    detail: str
    created_at: datetime | None
    snoozed_until: date | None
    dismissed_at: datetime | None
    resolved_at: datetime | None
    is_prd: bool = False
    has_person: bool = False


@dataclass
class HomeDTO:
    day: DayViewDTO
    current_worklist: WorklistRowDTO | None
    alerts: list[AlertRowDTO]
    urgent_count: int
    warn_count: int
    info_count: int
