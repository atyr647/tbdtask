"""Tenant scoping primitives.

Phase 0 of the multi-tenant pivot. Defines:

* ``TenantScopedMixin`` — the marker + ``org_id`` column applied to every
  model that belongs to a single organization.
* ``tenant_context()`` — a context manager that binds an org id to the
  active call stack via a ``ContextVar`` so it survives async hops.
* ``resolve_org_id()`` — centralized org resolution that eliminates manual
  ``org_id`` assignment throughout routes and services.
* A ``do_orm_execute`` listener registered on the global ``Session`` class
  that injects an ``org_id == current`` filter on every query for
  tenant-scoped models when a context is active.

Nothing in the request pipeline enters a tenant context yet — that lands in
Phase 3. The listener is a no-op until then, so existing single-tenant
code paths are unaffected. Tests opt in by entering ``tenant_context(...)``
explicitly to verify isolation actually works end-to-end before Phase 3
turns enforcement on for real traffic.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Iterator, Optional

from sqlalchemy import ForeignKey, Integer, event
from sqlalchemy.orm import (
    Mapped,
    Session,
    declared_attr,
    mapped_column,
    validates,
    with_loader_criteria,
)

if TYPE_CHECKING:
    from . import models as M


# Authoritative list of tenant-scoped table names. The Alembic migration that
# adds ``org_id`` columns iterates over this same tuple, so adding a new
# tenant model is a one-line change here plus a model declaration.
TENANT_SCOPED_TABLES: tuple[str, ...] = (
    "persons",
    "person_rates",
    "person_duty_sections",
    "person_prds",
    "person_roster_status",
    "person_drivers_licenses",
    "qualifications",
    "person_quals",
    "absence_codes",
    "absences",
    "crews",
    "crew_memberships",
    "task_categories",
    "task_templates",
    "task_instances",
    "task_assignments",
    "worklists",
    "alerts",
    "import_batches",
    "data_audit_events",
    "workcenters",
    "roles",
)


class TenantScopedMixin:
    """Marker + ``org_id`` column for models that belong to one organization.

    The mixin itself is the target of ``with_loader_criteria`` so isolation
    applies to every subclass automatically — no per-model registration.

    ``org_id`` is NOT NULL — every tenant-scoped row must belong to an org.
    Phase 0 created the column as nullable for backfill; Phase 3 makes it
    NOT NULL and turns on Postgres row-level security.
    """

    @declared_attr
    def org_id(cls) -> Mapped[int]:
        return mapped_column(
            Integer,
            ForeignKey("organizations.id"),
            nullable=False,
            index=True,
        )

    @validates("org_id")
    def _validate_org_id(self, key, value):
        if value is None:
            raise ValueError("org_id cannot be None on tenant-scoped model")
        return value


def resolve_org_id(
    *,
    worklist: Optional["M.Worklist"] = None,
    person: Optional["M.Person"] = None,
    explicit: Optional[int] = None,
) -> int:
    """Centralized org_id resolution.

    Call this instead of manually assigning ``org_id`` in routes/services.
    Eliminates the class of bugs where org_id is forgotten or set wrong.

    Priority: explicit > worklist.org_id > person.org_id > ValueError.
    """
    if explicit is not None:
        return explicit
    if worklist is not None:
        return worklist.org_id
    if person is not None:
        return person.org_id
    raise ValueError("Cannot resolve org_id: no parent entity or explicit value provided")


_current_org_id: ContextVar[Optional[int]] = ContextVar(
    "tbdtask_current_org_id", default=None
)


def current_org_id() -> Optional[int]:
    """Return the org id bound to the current call stack, or ``None``."""
    return _current_org_id.get()


@contextmanager
def tenant_context(org_id: int) -> Iterator[int]:
    """Bind an org id to the active call stack.

    All ORM queries against tenant-scoped models inside the block get an
    automatic ``org_id == <id>`` filter. Nesting is allowed; the inner
    block's id wins until it exits.
    """
    if not isinstance(org_id, int) or isinstance(org_id, bool) or org_id <= 0:
        raise ValueError("tenant_context requires a positive int org_id")
    token = _current_org_id.set(org_id)
    try:
        yield org_id
    finally:
        _current_org_id.reset(token)


def _inject_tenant_filter(execute_state) -> None:
    oid = _current_org_id.get()
    if oid is None:
        return
    if execute_state.is_relationship_load:
        # Eager/lazy relationship loads inherit options from the parent
        # query, so the criteria is already in scope. Re-applying here
        # would double-filter and break joinedload paths.
        return
    # ``oid`` is captured as a closure variable (not invoked inside the
    # lambda) so SQLAlchemy's lambda compiler can extract it as a bind
    # parameter. Calling ``ContextVar.get()`` inside the lambda body raises
    # InvalidRequestError because the lambda compiler refuses to invoke
    # arbitrary callables when probing for bound values.
    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(
            TenantScopedMixin,
            lambda cls: cls.org_id == oid,
            include_aliases=True,
        )
    )


def register_tenancy_listeners() -> None:
    """Install the ``do_orm_execute`` listener on the global Session class.

    Idempotent: re-calling is a no-op. Called once from ``app.db`` at import
    time so any session created anywhere in the app participates without
    per-call setup.
    """
    if not event.contains(Session, "do_orm_execute", _inject_tenant_filter):
        event.listen(Session, "do_orm_execute", _inject_tenant_filter)
