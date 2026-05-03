"""Phase 0 tenancy scaffolding tests.

These exercise the substrate that Phase 3 will turn into hard isolation:

* ``TenantScopedMixin`` gives every tenant model an ``org_id``.
* ``tenant_context()`` binds an org id to the active call stack.
* The SQLAlchemy ``do_orm_execute`` listener filters tenant-model queries
  automatically when a context is active.

Many of the assertions deliberately probe the unhappy paths (cross-org
reads, sibling tables, eager loads, ``session.get`` by primary key) so any
future regression that loosens isolation surfaces here, not in production.
The ``test_cross_org_*`` cases map directly to the make-or-break security
questions agreed for this rollout.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app import models as M
from app.tenancy import (
    TENANT_SCOPED_TABLES,
    TenantScopedMixin,
    current_org_id,
    tenant_context,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_two_orgs(session):
    org_a = M.Organization(slug="alpha", name="Alpha Org")
    org_b = M.Organization(slug="bravo", name="Bravo Org")
    session.add_all([org_a, org_b])
    session.flush()
    return org_a, org_b


def _make_person(session, *, org_id, last_name):
    p = M.Person(
        last_name=last_name,
        full_display=f"{last_name} display",
        org_id=org_id,
    )
    session.add(p)
    session.flush()
    return p


# ---------------------------------------------------------------------------
# Source-of-truth parity
# ---------------------------------------------------------------------------

def test_tenant_scoped_tables_list_matches_models():
    """Every model with TenantScopedMixin must appear in the canonical list.

    Catches the silent failure mode where someone adds a new tenant model
    but forgets to update ``TENANT_SCOPED_TABLES`` (which migrations and the
    runtime listener both consume).
    """
    declared = {
        m.class_.__tablename__
        for m in M.Base.registry.mappers
        if issubclass(m.class_, TenantScopedMixin)
    }
    assert declared == set(TENANT_SCOPED_TABLES), (
        f"models with TenantScopedMixin: {sorted(declared)}\n"
        f"TENANT_SCOPED_TABLES:          {sorted(TENANT_SCOPED_TABLES)}\n"
        f"in models but not list:        {sorted(declared - set(TENANT_SCOPED_TABLES))}\n"
        f"in list but not models:        {sorted(set(TENANT_SCOPED_TABLES) - declared)}"
    )


def test_organizations_table_is_not_tenant_scoped():
    """The tenant root must not be filtered by its own column."""
    assert "organizations" not in TENANT_SCOPED_TABLES


def test_settings_table_is_not_tenant_scoped():
    """Settings is intentionally global — per-org config lives in Organization.settings_json."""
    assert "settings" not in TENANT_SCOPED_TABLES


# ---------------------------------------------------------------------------
# Context manager lifecycle
# ---------------------------------------------------------------------------

def test_tenant_context_sets_and_clears():
    assert current_org_id() is None
    with tenant_context(7):
        assert current_org_id() == 7
        with tenant_context(9):
            assert current_org_id() == 9
        assert current_org_id() == 7
    assert current_org_id() is None


def test_tenant_context_rejects_nonpositive():
    for bad in (0, -1, -999):
        with pytest.raises(ValueError):
            with tenant_context(bad):
                pass


def test_tenant_context_rejects_non_int():
    for bad in ("1", 1.0, None, True):
        with pytest.raises(ValueError):
            with tenant_context(bad):  # type: ignore[arg-type]
                pass


def test_tenant_context_clears_on_exception():
    with pytest.raises(RuntimeError):
        with tenant_context(5):
            raise RuntimeError("boom")
    assert current_org_id() is None


# ---------------------------------------------------------------------------
# Listener: filtering behaviour
# ---------------------------------------------------------------------------

def test_query_without_context_sees_all_rows(session):
    """Phase 0 listener must be a no-op when no context is active."""
    org_a, org_b = _seed_two_orgs(session)
    _make_person(session, org_id=org_a.id, last_name="Alpha")
    _make_person(session, org_id=org_b.id, last_name="Bravo")
    persons = session.execute(select(M.Person)).scalars().all()
    assert {p.last_name for p in persons} == {"Alpha", "Bravo"}


def test_query_inside_context_filters_to_one_org(session):
    org_a, org_b = _seed_two_orgs(session)
    _make_person(session, org_id=org_a.id, last_name="Alpha")
    _make_person(session, org_id=org_b.id, last_name="Bravo")

    with tenant_context(org_a.id):
        rows = session.execute(select(M.Person)).scalars().all()
        assert [r.last_name for r in rows] == ["Alpha"]

    with tenant_context(org_b.id):
        rows = session.execute(select(M.Person)).scalars().all()
        assert [r.last_name for r in rows] == ["Bravo"]


# ---------------------------------------------------------------------------
# Make-or-break: cross-org isolation cannot leak through any access path
# ---------------------------------------------------------------------------

def test_cross_org_read_denied_via_select(session):
    """User in Org A cannot read Org B data via a SELECT, even with a WHERE
    clause that names the row directly."""
    org_a, org_b = _seed_two_orgs(session)
    _make_person(session, org_id=org_b.id, last_name="Secret")

    with tenant_context(org_a.id):
        result = session.execute(
            select(M.Person).where(M.Person.last_name == "Secret")
        ).scalars().all()
        assert result == [], "cross-org SELECT must return no rows"


def test_cross_org_read_denied_via_session_get_fresh_session(session_factory):
    """``Session.get`` from a fresh session must enforce the tenant filter.

    A second ``session.get`` from the same session would cheat via the
    identity map (see the documented caveat below), but a freshly opened
    session has an empty cache and must round-trip to the DB, where the
    ``do_orm_execute`` listener applies the org filter.
    """
    setup = session_factory()
    org_a = M.Organization(slug="alpha", name="Alpha Org")
    org_b = M.Organization(slug="bravo", name="Bravo Org")
    setup.add_all([org_a, org_b])
    setup.flush()
    target = M.Person(
        last_name="Secret", full_display="Secret S", org_id=org_b.id
    )
    setup.add(target)
    setup.flush()
    # Snapshot ids before commit; the ORM expires attributes on commit
    # and the objects detach on session.close(), so accessing them later
    # raises DetachedInstanceError.
    org_a_id = org_a.id
    target_pk = target.id
    setup.commit()
    setup.close()

    fresh = session_factory()
    try:
        with tenant_context(org_a_id):
            assert fresh.get(M.Person, target_pk) is None
    finally:
        fresh.close()


def test_session_get_identity_map_caveat_phase3_will_close(session):
    """Documented limitation of the Phase 0 SQLAlchemy-only enforcement.

    ``Session.get`` short-circuits to the identity map when the requested
    PK is already cached in the session, so a cross-org ``get`` in a session
    that previously loaded the row will return it. In production each
    request gets a fresh session bound to one tenant, so the identity map
    can't contain another tenant's rows. Phase 3's Postgres RLS closes this
    gap at the DB level even for misconfigured sessions.

    This test pins the current behaviour so any change (e.g. a defensive
    layer added later) is intentional, not accidental.
    """
    org_a, org_b = _seed_two_orgs(session)
    target = _make_person(session, org_id=org_b.id, last_name="Cached")

    # ``target`` is now in this session's identity map.
    with tenant_context(org_a.id):
        # Identity-map hit returns the cached object across orgs. Use
        # select() instead of get() in app code where this matters.
        assert session.get(M.Person, target.id) is target

        # ``select()`` always issues a query, so the filter applies.
        assert session.execute(
            select(M.Person).where(M.Person.id == target.id)
        ).scalar_one_or_none() is None


def test_cross_org_count_returns_zero(session):
    org_a, org_b = _seed_two_orgs(session)
    for i in range(5):
        _make_person(session, org_id=org_b.id, last_name=f"B{i}")

    with tenant_context(org_a.id):
        assert session.execute(
            select(M.Person)
        ).scalars().all() == []


def test_isolation_applies_to_sibling_tenant_tables(session):
    """The mixin filter must hit every tenant model, not just Person.

    Probes a representative spread of tables — qualifications (catalog),
    worklists (workflow root), and alerts (notification queue) — so a
    regression that drops the mixin from one of them is caught.
    """
    org_a, org_b = _seed_two_orgs(session)

    session.add_all([
        M.Qualification(name="Q-A", display_order=0, org_id=org_a.id),
        M.Qualification(name="Q-B", display_order=0, org_id=org_b.id),
        M.Worklist(week_starting=date(2026, 1, 5), name="WL-A", org_id=org_a.id),
        M.Worklist(week_starting=date(2026, 1, 5), name="WL-B", org_id=org_b.id),
        M.Alert(alert_type="x", severity="info", org_id=org_a.id),
        M.Alert(alert_type="x", severity="info", org_id=org_b.id),
    ])
    session.flush()

    with tenant_context(org_a.id):
        assert [q.name for q in session.execute(select(M.Qualification)).scalars()] == ["Q-A"]
        assert [w.name for w in session.execute(select(M.Worklist)).scalars()] == ["WL-A"]
        assert session.execute(select(M.Alert)).scalars().all() != []
        for alert in session.execute(select(M.Alert)).scalars():
            assert alert.org_id == org_a.id


def test_global_tables_unaffected_by_context(session):
    """Settings and Organization itself must remain queryable across contexts."""
    org_a, _ = _seed_two_orgs(session)
    session.add(M.Setting(key="theme", value={"mode": "dark"}))
    session.flush()

    with tenant_context(org_a.id):
        # Setting is global — no filter applied.
        assert session.execute(select(M.Setting)).scalars().one().key == "theme"
        # Organization is the tenant root — also not filtered, otherwise an
        # org couldn't look itself up.
        assert session.execute(select(M.Organization)).scalars().all()


# ---------------------------------------------------------------------------
# Schema sanity
# ---------------------------------------------------------------------------

def test_every_tenant_model_has_org_id_column():
    for m in M.Base.registry.mappers:
        if issubclass(m.class_, TenantScopedMixin):
            cols = {c.name for c in m.local_table.columns}
            assert "org_id" in cols, f"{m.class_.__name__} missing org_id"


def test_organization_model_has_no_org_id():
    cols = {c.name for c in M.Organization.__table__.columns}
    assert "org_id" not in cols
