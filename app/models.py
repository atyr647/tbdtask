"""
Database models for tbdtask.

Conventions:
- Effective-dated tables carry valid_from / valid_to (NULL = current row).
- All user-visible entities support soft-delete: active, archived_at, archived_reason.
- Imported rows are tagged with import_batch_id for provenance.
- Display ordering is explicit via display_order where the UI is order-sensitive.
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


# ---------------------------------------------------------------------------
# Mixins
# ---------------------------------------------------------------------------

class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )


class SoftDeleteMixin:
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    archived_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ProvenanceMixin:
    import_batch_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("import_batches.id"), nullable=True, index=True
    )


# ---------------------------------------------------------------------------
# Provenance / import tracking
# ---------------------------------------------------------------------------

class ImportBatch(Base, TimestampMixin):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_file: Mapped[str] = mapped_column(String(512), nullable=False)
    source_workbook_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    row_counts: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


# ---------------------------------------------------------------------------
# Personnel
# ---------------------------------------------------------------------------

class Person(Base, TimestampMixin, SoftDeleteMixin, ProvenanceMixin):
    __tablename__ = "persons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_name: Mapped[str] = mapped_column(String(128), nullable=False)
    first_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    full_display: Mapped[str] = mapped_column(String(256), nullable=False)
    position: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Incoming / arrival tracking. Populated for personnel still en route;
    # cleared (or just ignored) once they're on board and active.
    arrival_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    sponsor_person_id: Mapped[Optional[int]] = mapped_column(ForeignKey("persons.id"), nullable=True)
    orders_received: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    itinerary_received: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    aob_scheduled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    barracks_assigned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    rates: Mapped[list["PersonRate"]] = relationship(back_populates="person")
    duty_sections: Mapped[list["PersonDutySection"]] = relationship(back_populates="person")
    prds: Mapped[list["PersonPrd"]] = relationship(back_populates="person")
    roster_statuses: Mapped[list["PersonRosterStatus"]] = relationship(back_populates="person")
    drivers_licenses: Mapped[list["PersonDriversLicense"]] = relationship(back_populates="person")
    quals: Mapped[list["PersonQual"]] = relationship(back_populates="person")
    absences: Mapped[list["Absence"]] = relationship(back_populates="person")
    sponsor: Mapped[Optional["Person"]] = relationship(remote_side=[id], foreign_keys=[sponsor_person_id])


def _effective_date_cols():
    return (
        mapped_column(Date, nullable=False),
        mapped_column(Date, nullable=True),
    )


class PersonRate(Base, TimestampMixin, ProvenanceMixin):
    __tablename__ = "person_rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id"), nullable=False, index=True)
    rate: Mapped[str] = mapped_column(String(32), nullable=False)
    paygrade: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    person: Mapped["Person"] = relationship(back_populates="rates")

    __table_args__ = (
        Index("ix_person_rates_current", "person_id", "valid_to"),
    )


class PersonDutySection(Base, TimestampMixin, ProvenanceMixin):
    __tablename__ = "person_duty_sections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id"), nullable=False, index=True)
    duty_section: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    person: Mapped["Person"] = relationship(back_populates="duty_sections")

    __table_args__ = (
        Index("ix_person_duty_sections_current", "person_id", "valid_to"),
    )


class PersonPrd(Base, TimestampMixin, ProvenanceMixin):
    """Projected Rotation Date. New row per change (initial / extension / correction)."""

    __tablename__ = "person_prds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id"), nullable=False, index=True)
    prd_date: Mapped[date] = mapped_column(Date, nullable=False)
    change_reason: Mapped[str] = mapped_column(String(32), default="initial", nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    person: Mapped["Person"] = relationship(back_populates="prds")


class PersonRosterStatus(Base, TimestampMixin, ProvenanceMixin):
    __tablename__ = "person_roster_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    person: Mapped["Person"] = relationship(back_populates="roster_statuses")

    __table_args__ = (
        CheckConstraint(
            "status IN ('active','incoming','prd_pending','departed','dropped')",
            name="ck_roster_status_value",
        ),
    )


class PersonDriversLicense(Base, TimestampMixin, ProvenanceMixin):
    __tablename__ = "person_drivers_licenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id"), nullable=False, index=True)
    has_license: Mapped[bool] = mapped_column(Boolean, nullable=False)
    expires_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    person: Mapped["Person"] = relationship(back_populates="drivers_licenses")


# ---------------------------------------------------------------------------
# Qualifications
# ---------------------------------------------------------------------------

class Qualification(Base, TimestampMixin, SoftDeleteMixin, ProvenanceMixin):
    __tablename__ = "qualifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    code: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    validity_period_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pinned_column: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    person_quals: Mapped[list["PersonQual"]] = relationship(back_populates="qualification")

    __table_args__ = (
        UniqueConstraint("name", name="uq_qualification_name"),
    )


PERSON_QUAL_STATUSES = (
    "not_assigned",
    "assigned",
    "in_progress",
    "qualified",
    "dinq",
    "expired",
    "waived",
)


class PersonQual(Base, TimestampMixin, SoftDeleteMixin, ProvenanceMixin):
    __tablename__ = "person_quals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id"), nullable=False, index=True)
    qual_id: Mapped[int] = mapped_column(ForeignKey("qualifications.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    achieved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    person: Mapped["Person"] = relationship(back_populates="quals")
    qualification: Mapped["Qualification"] = relationship(back_populates="person_quals")

    __table_args__ = (
        CheckConstraint(
            "status IN ('not_assigned','assigned','in_progress','qualified','dinq','expired','waived')",
            name="ck_person_qual_status",
        ),
        Index("ix_person_quals_current", "person_id", "qual_id", "valid_to"),
    )


# ---------------------------------------------------------------------------
# Absences
# ---------------------------------------------------------------------------

class AbsenceCode(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "absence_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Absence(Base, TimestampMixin, SoftDeleteMixin, ProvenanceMixin):
    __tablename__ = "absences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id"), nullable=False, index=True)
    code_id: Mapped[int] = mapped_column(ForeignKey("absence_codes.id"), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    end_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    person: Mapped["Person"] = relationship(back_populates="absences")
    code: Mapped["AbsenceCode"] = relationship()

    __table_args__ = (
        Index("ix_absences_dates", "start_date", "end_date"),
    )


# ---------------------------------------------------------------------------
# Crews (plumbing only for now)
# ---------------------------------------------------------------------------

class Crew(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "crews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class CrewMembership(Base, TimestampMixin):
    __tablename__ = "crew_memberships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    crew_id: Mapped[int] = mapped_column(ForeignKey("crews.id"), nullable=False, index=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id"), nullable=False, index=True)
    role: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

class TaskCategory(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "task_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


CARRY_OVER_POLICIES = (
    "auto_same_person",
    "auto_any_qualified",
    "never",
    "manual_prompt",
)


class TaskTemplate(Base, TimestampMixin, SoftDeleteMixin, ProvenanceMixin):
    __tablename__ = "task_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    category_id: Mapped[Optional[int]] = mapped_column(ForeignKey("task_categories.id"), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    estimated_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    splittable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reassignable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    carry_over_policy: Mapped[str] = mapped_column(
        String(32), default="auto_same_person", nullable=False
    )
    recurrence_rule: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    required_drivers_license: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    required_duty_section: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    required_crew_id: Mapped[Optional[int]] = mapped_column(ForeignKey("crews.id"), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    category: Mapped[Optional["TaskCategory"]] = relationship()

    __table_args__ = (
        CheckConstraint(
            "carry_over_policy IN ('auto_same_person','auto_any_qualified','never','manual_prompt')",
            name="ck_task_carry_over_policy",
        ),
    )


class TaskTemplateRequiredQual(Base):
    __tablename__ = "task_template_required_quals"

    task_template_id: Mapped[int] = mapped_column(
        ForeignKey("task_templates.id"), primary_key=True
    )
    qual_id: Mapped[int] = mapped_column(ForeignKey("qualifications.id"), primary_key=True)


TASK_INSTANCE_STATUSES = (
    "open",
    "in_progress",
    "done",
    "discarded",
    "carried",
)


class Worklist(Base, TimestampMixin, SoftDeleteMixin, ProvenanceMixin):
    __tablename__ = "worklists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    week_starting: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("worklists.id"), nullable=True)
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    locked_by_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    amended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    amendment_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    operator_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class TaskInstance(Base, TimestampMixin, SoftDeleteMixin, ProvenanceMixin):
    __tablename__ = "task_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[Optional[int]] = mapped_column(ForeignKey("task_templates.id"), nullable=True)
    worklist_id: Mapped[Optional[int]] = mapped_column(ForeignKey("worklists.id"), nullable=True, index=True)
    scheduled_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, index=True)
    category_id: Mapped[Optional[int]] = mapped_column(ForeignKey("task_categories.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    completion_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Single per-task hours figure. Every assignee on the task receives
    # full credit for these hours when rolling up personnel stats.
    hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    carried_from_instance_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("task_instances.id"), nullable=True
    )
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    assignments: Mapped[list["TaskAssignment"]] = relationship(back_populates="instance")
    category: Mapped[Optional["TaskCategory"]] = relationship()

    __table_args__ = (
        CheckConstraint(
            "status IN ('open','in_progress','done','discarded','carried')",
            name="ck_task_instance_status",
        ),
    )


class TaskAssignment(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "task_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[int] = mapped_column(
        ForeignKey("task_instances.id"), nullable=False, index=True
    )
    person_id: Mapped[Optional[int]] = mapped_column(ForeignKey("persons.id"), nullable=True)
    is_poic: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    external_poic_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    completion_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    hours_worked: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    instance: Mapped["TaskInstance"] = relationship(back_populates="assignments")
    person: Mapped[Optional["Person"]] = relationship()

    __table_args__ = (
        CheckConstraint(
            "person_id IS NOT NULL OR external_poic_name IS NOT NULL",
            name="ck_assignment_has_subject",
        ),
        Index(
            "ux_one_poic_per_instance",
            "instance_id",
            unique=True,
            sqlite_where=text("is_poic = 1"),
        ),
    )


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

class Alert(Base, TimestampMixin):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    person_id: Mapped[Optional[int]] = mapped_column(ForeignKey("persons.id"), nullable=True, index=True)
    task_instance_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("task_instances.id"), nullable=True
    )
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    dismissed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    snoozed_until: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class Setting(Base, TimestampMixin):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
