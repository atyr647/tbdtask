"""
Shared pytest fixtures.

Each test gets its own in-memory SQLite database, fully isolated from other
tests and from the dev DB. The app's global engine/SessionLocal are
monkey-patched at fixture setup so route handlers (which import
``SessionLocal`` directly) hit the test DB without any code changes.
"""

from __future__ import annotations

import os

# Stop ``app.main.create_app()`` from running Alembic against the on-disk dev
# DB during test collection. Tests use in-memory SQLite per the fixtures
# below; the app-import side-effect would otherwise leak migrations into
# ``data/tbdtask.db``. Must be set before any ``app.*`` import.
os.environ.setdefault("TBDTASK_SKIP_AUTOMIGRATE", "1")
# Default tests to single-tenant mode so the auth + tenancy middleware
# pass through. Auth-flow tests opt out by spinning their own app
# instance with the env var unset.
os.environ.setdefault("TBDTASK_SINGLE_TENANT", "1")

from datetime import date, datetime  # noqa: E402
from typing import Iterator  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event, text  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import db as db_module  # noqa: E402
from app import models as M  # noqa: E402


@pytest.fixture
def engine():
    # An in-memory SQLite per test, fully isolated from other
    # tests and from the dev DB.
    eng = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_connection, _record):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    db_module.Base.metadata.create_all(eng)

    # Phase 3: seed a default org so the NOT NULL org_id constraint
    # is satisfiable. Tests that care about multi-tenancy create their
    # own orgs; everyone else uses this one.
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO organizations (slug, name) "
                "VALUES ('default', 'Default Organization')"
            )
        )

    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@pytest.fixture
def session(session_factory) -> Iterator[Session]:
    s = session_factory()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def client(engine, session_factory, monkeypatch):
    """A FastAPI TestClient bound to a fresh in-memory DB for the test."""
    # Each route module does `from ..db import SessionLocal` at import time,
    # so the local binding is captured. Patching ``db_module.SessionLocal``
    # alone does not affect those captured references — every route module
    # gets its own override here.
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", session_factory)
    import app.routes.absences as r_absences
    import app.routes.alerts as r_alerts
    import app.routes.home as r_home
    import app.routes.personnel as r_personnel
    import app.routes.quals as r_quals
    import app.routes.tasks as r_tasks
    import app.routes.templates as r_templates
    import app.routes.today as r_today
    import app.routes.worklists as r_worklists

    for mod in (
        r_absences,
        r_alerts,
        r_home,
        r_personnel,
        r_quals,
        r_tasks,
        r_templates,
        r_today,
        r_worklists,
    ):
        monkeypatch.setattr(mod, "SessionLocal", session_factory)

    from app.main import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# Domain helpers — small builders so each test reads top-down.
# ---------------------------------------------------------------------------


def make_person(
    session,
    last_name="Doe",
    rate="BM3",
    duty_section=2,
    paygrade="E-4",
    display_order=0,
    org_id=1,
):
    today = date.today()
    p = M.Person(
        last_name=last_name,
        full_display=f"{rate} {last_name}",
        display_order=display_order,
        org_id=org_id,
    )
    session.add(p)
    session.flush()
    session.add(
        M.PersonRate(
            person_id=p.id,
            rate=rate,
            paygrade=paygrade,
            valid_from=today,
            org_id=org_id,
        )
    )
    session.add(
        M.PersonDutySection(
            person_id=p.id, duty_section=duty_section, valid_from=today, org_id=org_id
        )
    )
    session.add(
        M.PersonRosterStatus(
            person_id=p.id, status="active", valid_from=today, org_id=org_id
        )
    )
    session.flush()
    return p


def make_absence_codes(session, org_id=1):
    codes = {}
    for i, code in enumerate(["Leave", "TAD", "School", "Medical", "Appt", "Other"]):
        c = M.AbsenceCode(code=code, display_order=i, org_id=org_id)
        session.add(c)
        codes[code] = c
    session.flush()
    return codes


def make_task_categories(session, org_id=1):
    cats = {}
    for i, name in enumerate(["Maintenance", "Corrective", "General"]):
        c = M.TaskCategory(name=name, display_order=i, org_id=org_id)
        session.add(c)
        cats[name] = c
    session.flush()
    return cats


def make_worklist(
    session,
    monday: date,
    *,
    name=None,
    locked=False,
    parent_id=None,
    version=1,
    org_id=1,
):
    wl = M.Worklist(
        week_starting=monday,
        name=name or f"Week of {monday.isoformat()}",
        version=version,
        parent_id=parent_id,
        locked=locked,
        locked_at=datetime.now() if locked else None,
        org_id=org_id,
    )
    session.add(wl)
    session.flush()
    return wl


def make_task(
    session,
    *,
    worklist_id,
    name="Test task",
    scheduled_date=None,
    status="open",
    hours=None,
    category_id=None,
    template_id=None,
    org_id=1,
):
    inst = M.TaskInstance(
        worklist_id=worklist_id,
        scheduled_date=scheduled_date,
        category_id=category_id,
        template_id=template_id,
        name=name,
        status=status,
        hours=hours,
        org_id=org_id,
    )
    session.add(inst)
    session.flush()
    return inst


def make_assignment(session, *, instance_id, person_id, is_poic=False, org_id=1):
    a = M.TaskAssignment(
        instance_id=instance_id, person_id=person_id, is_poic=is_poic, org_id=org_id
    )
    session.add(a)
    session.flush()
    return a


def make_absence(
    session,
    *,
    person_id,
    code_id,
    start_date,
    end_date,
    start_time=None,
    end_time=None,
    reason=None,
    org_id=1,
):
    a = M.Absence(
        person_id=person_id,
        code_id=code_id,
        start_date=start_date,
        end_date=end_date,
        start_time=start_time,
        end_time=end_time,
        reason=reason,
        org_id=org_id,
    )
    session.add(a)
    session.flush()
    return a


def make_qual(session, name, *, validity_period_days=None, display_order=0, org_id=1):
    q = M.Qualification(
        name=name,
        validity_period_days=validity_period_days,
        display_order=display_order,
        org_id=org_id,
    )
    session.add(q)
    session.flush()
    return q


def set_prd(session, person_id: int, prd: date, *, change_reason="initial", org_id=1):
    today = date.today()
    session.add(
        M.PersonPrd(
            person_id=person_id,
            prd_date=prd,
            change_reason=change_reason,
            valid_from=today,
            org_id=org_id,
        )
    )
    session.flush()
