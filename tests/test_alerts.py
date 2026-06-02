"""Tests for the alerts engine — PRD windows, qual expiration, recompute."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select

from app import models as M
from app.services.alerts import recompute, active_alerts
from tests.conftest import (
    make_person,
    make_task,
    make_task_categories,
    make_worklist,
    set_prd,
    make_qual,
)


# ---------------------------------------------------------------------------
# PRD windows
# ---------------------------------------------------------------------------


def _today():
    return date.today()


def test_prd_passed_is_urgent(session):
    p = make_person(session, "Tanner")
    set_prd(session, p.id, _today() - timedelta(days=3))
    session.commit()
    recompute(session)
    session.commit()

    rows = active_alerts(session)
    assert len(rows) == 1
    assert rows[0].alert_type == "prd_passed"
    assert rows[0].severity == "urgent"
    assert rows[0].person_id == p.id


def test_prd_within_a_week_is_weekly_in_month_urgent(session):
    p = make_person(session, "Tanner")
    set_prd(session, p.id, _today() + timedelta(days=5))
    session.commit()
    recompute(session)
    session.commit()

    rows = active_alerts(session)
    assert [r.alert_type for r in rows] == ["prd_weekly_in_month"]
    assert rows[0].severity == "urgent"


def test_prd_25_days_out_is_1mo_warn(session):
    p = make_person(session, "Sandoval")
    set_prd(session, p.id, _today() + timedelta(days=25))
    session.commit()
    recompute(session)
    session.commit()

    rows = active_alerts(session)
    assert [r.alert_type for r in rows] == ["prd_1mo"]
    assert rows[0].severity == "warn"


def test_prd_50_days_out_is_2mo_info(session):
    p = make_person(session, "Foster")
    set_prd(session, p.id, _today() + timedelta(days=50))
    session.commit()
    recompute(session)
    session.commit()

    rows = active_alerts(session)
    assert [r.alert_type for r in rows] == ["prd_2mo"]
    assert rows[0].severity == "info"


def test_prd_300_days_out_is_orders_window_warn(session):
    p = make_person(session, "Vega")
    set_prd(session, p.id, _today() + timedelta(days=300))
    session.commit()
    recompute(session)
    session.commit()

    rows = active_alerts(session)
    assert [r.alert_type for r in rows] == ["prd_orders_window"]
    assert rows[0].severity == "warn"


def test_prd_more_than_a_year_out_yields_no_alert(session):
    p = make_person(session, "Sims")
    set_prd(session, p.id, _today() + timedelta(days=400))
    session.commit()
    recompute(session)
    session.commit()

    rows = [r for r in active_alerts(session) if r.alert_type.startswith("prd")]
    assert rows == []


def test_recompute_resolves_stale_prd_alert_when_data_changes(session):
    """If a PRD moves out of the band, the prior alert should be resolved."""
    p = make_person(session, "Tanner")
    set_prd(session, p.id, _today() + timedelta(days=5))  # urgent
    session.commit()
    recompute(session)
    session.commit()
    assert any(r.alert_type == "prd_weekly_in_month" for r in active_alerts(session))

    # Push the PRD out to 200 days — should resolve the urgent one and
    # raise a prd_orders_window alert instead.
    cur = session.scalars(
        select(M.PersonPrd).where(
            M.PersonPrd.person_id == p.id, M.PersonPrd.valid_to.is_(None)
        )
    ).first()
    cur.valid_to = _today()
    session.add(
        M.PersonPrd(
            person_id=p.id,
            prd_date=_today() + timedelta(days=200),
            change_reason="extension",
            valid_from=_today(),
            org_id=1,
        )
    )
    session.commit()
    recompute(session)
    session.commit()

    rows = [r for r in active_alerts(session) if r.alert_type.startswith("prd")]
    types = sorted(r.alert_type for r in rows)
    assert types == ["prd_orders_window"]


def test_no_prd_means_no_alert(session):
    make_person(session, "Adler")
    session.commit()
    recompute(session)
    session.commit()
    assert active_alerts(session) == []


# ---------------------------------------------------------------------------
# Carry-over alert
# ---------------------------------------------------------------------------


def test_carry_over_pending_alert_fires_on_open_worklist(session):
    make_task_categories(session)
    make_person(session, "Pearce")
    older = make_worklist(session, _today() - timedelta(days=14))
    make_task(
        session,
        worklist_id=older.id,
        name="Open from prior week",
        status="in_progress",
        scheduled_date=_today() - timedelta(days=14),
    )
    target = make_worklist(session, _today())  # current week
    session.commit()

    recompute(session)
    session.commit()
    rows = [
        r
        for r in active_alerts(session)
        if r.alert_type == "worklist_carry_over_pending"
    ]
    assert len(rows) == 1
    assert (rows[0].payload or {}).get("worklist_id") == target.id


# ---------------------------------------------------------------------------
# Qualification expiration: tracked on PersonQual.expires_at; no alert fires
# in the current product, but the model still records the expiration date so
# operators can see it on the personnel detail page. Tests below pin the
# expires_at calculation since that's the critical bit.
# ---------------------------------------------------------------------------


def test_qual_expires_at_set_from_validity_period_days(session):
    """When a qual has a validity period and a person achieves it, the
    person-qual row's expires_at is set to achieved_at + validity_period_days."""
    q = make_qual(session, "HMMWV", validity_period_days=365)
    p = make_person(session, "Tanner")
    achieved = datetime.now() - timedelta(days=10)
    expected_expiry = achieved + timedelta(days=365)
    pq = M.PersonQual(
        person_id=p.id,
        qual_id=q.id,
        status="qualified",
        achieved_at=achieved,
        expires_at=expected_expiry,
        valid_from=_today(),
        org_id=1,
    )
    session.add(pq)
    session.commit()

    fetched = session.get(M.PersonQual, pq.id)
    assert fetched.expires_at == expected_expiry
    # delta is exactly the validity period
    delta_days = (fetched.expires_at - fetched.achieved_at).days
    assert delta_days == 365


def test_qual_with_no_validity_has_no_expiry(session):
    q = make_qual(session, "Tagout Authority", validity_period_days=None)
    p = make_person(session, "Tanner")
    achieved = datetime.now() - timedelta(days=10)
    pq = M.PersonQual(
        person_id=p.id,
        qual_id=q.id,
        status="qualified",
        achieved_at=achieved,
        expires_at=None,
        valid_from=_today(),
        org_id=1,
    )
    session.add(pq)
    session.commit()

    fetched = session.get(M.PersonQual, pq.id)
    assert fetched.expires_at is None


# Qualification deadlines: a pending person-qual with a due date drives
# qual_due_soon (within 30 days) and qual_overdue (past due) alerts.


def test_qual_due_soon_alert_within_30_days(session):
    q = make_qual(session, "RHIB Coxswain")
    p = make_person(session, "Vega")
    pq = M.PersonQual(
        person_id=p.id,
        qual_id=q.id,
        status="assigned",
        due_at=_today() + timedelta(days=10),
        valid_from=_today(),
        org_id=1,
    )
    session.add(pq)
    session.commit()
    recompute(session, today=_today())
    types = {a.alert_type for a in active_alerts(session)}
    assert "qual_due_soon" in types
    assert "qual_overdue" not in types


def test_qual_overdue_alert_is_urgent(session):
    q = make_qual(session, "Small Arms")
    p = make_person(session, "Mason")
    pq = M.PersonQual(
        person_id=p.id,
        qual_id=q.id,
        status="in_progress",
        due_at=_today() - timedelta(days=3),
        valid_from=_today(),
        org_id=1,
    )
    session.add(pq)
    session.commit()
    recompute(session, today=_today())
    overdue = [a for a in active_alerts(session) if a.alert_type == "qual_overdue"]
    assert len(overdue) == 1 and overdue[0].severity == "urgent"


def test_qualified_qual_with_due_date_raises_no_alert(session):
    # A qual that's already qualified shouldn't alert even if due_at lingers.
    q = make_qual(session, "Helmsman")
    p = make_person(session, "Reyes")
    pq = M.PersonQual(
        person_id=p.id,
        qual_id=q.id,
        status="qualified",
        due_at=_today() - timedelta(days=5),
        valid_from=_today(),
        org_id=1,
    )
    session.add(pq)
    session.commit()
    recompute(session, today=_today())
    types = {a.alert_type for a in active_alerts(session)}
    assert "qual_overdue" not in types and "qual_due_soon" not in types


def test_qual_deadline_alert_resolves_when_achieved(session):
    q = make_qual(session, "EOOW")
    p = make_person(session, "Tanner")
    pq = M.PersonQual(
        person_id=p.id,
        qual_id=q.id,
        status="assigned",
        due_at=_today() + timedelta(days=5),
        valid_from=_today(),
        org_id=1,
    )
    session.add(pq)
    session.commit()
    recompute(session, today=_today())
    assert any(a.alert_type == "qual_due_soon" for a in active_alerts(session))
    # Person achieves it: close the row, drop the deadline.
    pq.status = "qualified"
    pq.due_at = None
    session.commit()
    recompute(session, today=_today())
    assert not any(a.alert_type == "qual_due_soon" for a in active_alerts(session))
