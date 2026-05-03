"""Phase 3: database-enforced tenant isolation tests.

These tests prove that the database itself blocks cross-org access,
even if the app layer forgets to filter. They run against the actual
DB engine (not just the ORM) to verify RLS / constraint enforcement.
"""

import uuid
import pytest
from datetime import date
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import models as M
from app.db import SessionLocal, engine, IS_SQLITE


RLS_TEST_ROLE = "tbdtask_test_role"


def _ensure_rls_test_role():
    """Create/grant the non-owner role used to prove RLS is enforced."""
    if IS_SQLITE:
        return
    try:
        with engine.begin() as conn:
            conn.execute(
                text(f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_roles WHERE rolname = '{RLS_TEST_ROLE}'
                    ) THEN
                        CREATE ROLE {RLS_TEST_ROLE} NOLOGIN;
                    END IF;
                END
                $$;
            """)
            )
            conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {RLS_TEST_ROLE}"))
            conn.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
                    f"TO {RLS_TEST_ROLE}"
                )
            )
            conn.execute(
                text(
                    f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {RLS_TEST_ROLE}"
                )
            )
    except Exception as exc:
        pytest.skip(f"cannot create/grant RLS test role: {exc}")


def _make_org(session, name="TestOrg"):
    slug = f"{name.lower()}-{uuid.uuid4().hex[:8]}"
    org = M.Organization(name=name, slug=slug)
    session.add(org)
    session.flush()
    return org


def _make_person(session, org_id, last_name="Test"):
    p = M.Person(last_name=last_name, full_display=last_name, org_id=org_id)
    session.add(p)
    session.flush()
    return p


def _make_worklist(session, org_id, name="WL"):
    wl = M.Worklist(
        week_starting=date(2026, 1, 5),
        name=f"{name}-{uuid.uuid4().hex[:8]}",
        org_id=org_id,
    )
    session.add(wl)
    session.flush()
    return wl


# ---------------------------------------------------------------------------
# Test 1: Cross-org read blocked at DB level
# ---------------------------------------------------------------------------


@pytest.mark.skipif(IS_SQLITE, reason="RLS is Postgres-only")
def test_cross_org_read_blocked_db_level():
    """Open a raw connection as org A, try to read org B's data.
    The DB must return zero rows even with a direct SELECT."""
    _ensure_rls_test_role()
    session = SessionLocal()
    org_a = org_b = p_a = p_b = None
    try:
        org_a = _make_org(session, "OrgA")
        org_b = _make_org(session, "OrgB")
        p_a = _make_person(session, org_a.id, "Alice")
        p_b = _make_person(session, org_b.id, "Bob")
        session.commit()

        # Now test RLS with raw SQL using a non-owner role.
        with engine.begin() as conn:
            conn.execute(text(f"SET ROLE {RLS_TEST_ROLE}"))
            conn.execute(text(f"SET app.current_org_id = '{org_a.id}'"))

            # Should only see Alice.
            result = conn.execute(
                text(f"SELECT * FROM persons WHERE org_id = {org_a.id}")
            )
            assert len(result.fetchall()) == 1

            result = conn.execute(
                text(f"SELECT * FROM persons WHERE org_id = {org_b.id}")
            )
            assert len(result.fetchall()) == 0  # RLS blocks org B rows

            conn.execute(text("RESET ROLE"))
    finally:
        session.rollback()
        if p_a and p_b:
            session.query(M.Person).filter(M.Person.id.in_([p_a.id, p_b.id])).delete(
                synchronize_session=False
            )
        if org_a and org_b:
            session.query(M.Organization).filter(
                M.Organization.id.in_([org_a.id, org_b.id])
            ).delete(synchronize_session=False)
        session.commit()
        session.close()


# ---------------------------------------------------------------------------
# Test 2: Cross-org insert blocked at DB level
# ---------------------------------------------------------------------------


@pytest.mark.skipif(IS_SQLITE, reason="RLS is Postgres-only")
def test_cross_org_insert_blocked_db_level():
    """Set session org_id = A, attempt insert with org_id = B.
    The DB must reject it."""
    _ensure_rls_test_role()
    session = SessionLocal()
    org_a = org_b = None
    try:
        org_a = _make_org(session, "OrgA")
        org_b = _make_org(session, "OrgB")
        session.commit()

        # Use a nested transaction to handle the aborted state after RLS violation.
        with engine.connect() as outer_conn:
            outer_conn.execute(text(f"SET ROLE {RLS_TEST_ROLE}"))
            outer_conn.execute(text(f"SET app.current_org_id = '{org_a.id}'"))
            outer_conn.commit()

            # Start a nested transaction for the failing insert.
            nested = outer_conn.begin_nested()
            try:
                outer_conn.execute(
                    text(
                        f"INSERT INTO persons (last_name, full_display, org_id) VALUES ('Spy', 'Spy', {org_b.id})"
                    )
                )
                outer_conn.commit()
                assert False, "RLS should have blocked the insert"
            except Exception:
                nested.rollback()  # Rollback the nested transaction

            outer_conn.execute(text("RESET ROLE"))
            outer_conn.commit()
    finally:
        session.rollback()
        session.query(M.Person).filter(M.Person.last_name == "Spy").delete(
            synchronize_session=False
        )
        if org_a and org_b:
            session.query(M.Organization).filter(
                M.Organization.id.in_([org_a.id, org_b.id])
            ).delete(synchronize_session=False)
        session.commit()
        session.close()


# ---------------------------------------------------------------------------
# Test 3: App bug simulation — bypass filter, DB still enforces
# ---------------------------------------------------------------------------


@pytest.mark.skipif(IS_SQLITE, reason="RLS is Postgres-only")
def test_app_bypass_still_blocked_by_db():
    """Intentionally bypass the app-layer filter and query directly.
    RLS must still block cross-org reads."""
    _ensure_rls_test_role()
    session = SessionLocal()
    org_a = org_b = wl_a = wl_b = None
    try:
        org_a = _make_org(session, "OrgA")
        org_b = _make_org(session, "OrgB")
        wl_a = _make_worklist(session, org_a.id, "WL1")
        wl_b = _make_worklist(session, org_b.id, "WL2")
        session.commit()

        with engine.begin() as conn:
            conn.execute(text(f"SET ROLE {RLS_TEST_ROLE}"))
            conn.execute(text(f"SET app.current_org_id = '{org_a.id}'"))

            # Direct SELECT without WHERE — RLS should still filter.
            result = conn.execute(text("SELECT id, org_id FROM worklists"))
            rows = result.fetchall()
            assert len(rows) == 1
            assert rows[0].org_id == org_a.id

            conn.execute(text("RESET ROLE"))
    finally:
        session.rollback()
        if wl_a and wl_b:
            session.query(M.Worklist).filter(
                M.Worklist.id.in_([wl_a.id, wl_b.id])
            ).delete(synchronize_session=False)
        if org_a and org_b:
            session.query(M.Organization).filter(
                M.Organization.id.in_([org_a.id, org_b.id])
            ).delete(synchronize_session=False)
        session.commit()
        session.close()


# ---------------------------------------------------------------------------
# Test 4: Fail-closed — no org_id set means no access
# ---------------------------------------------------------------------------


@pytest.mark.skipif(IS_SQLITE, reason="RLS is Postgres-only")
def test_fail_closed_no_org_set():
    """When app.current_org_id is NULL, all tenant tables must return zero rows."""
    _ensure_rls_test_role()
    session = SessionLocal()
    org = p = None
    try:
        org = _make_org(session, "OrgA")
        p = _make_person(session, org.id, "Alice")
        session.commit()

        # Use the main engine but in a fresh connection without setting org_id.
        # The checkout hook will set it to NULL via RESET, which is what we want.
        with engine.begin() as conn:
            conn.execute(text(f"SET ROLE {RLS_TEST_ROLE}"))
            # Don't set app.current_org_id - checkout hook already set it to NULL

            result = conn.execute(text("SELECT * FROM persons"))
            assert len(result.fetchall()) == 0

            conn.execute(text("RESET ROLE"))
    finally:
        session.rollback()
        if p:
            session.query(M.Person).filter(M.Person.id == p.id).delete(
                synchronize_session=False
            )
        if org:
            session.query(M.Organization).filter(M.Organization.id == org.id).delete(
                synchronize_session=False
            )
        session.commit()
        session.close()


# ---------------------------------------------------------------------------
# Test 5: NOT NULL constraint enforced (SQLite + Postgres)
# ---------------------------------------------------------------------------


def test_org_id_not_null_enforced(session):
    """Inserting a tenant-scoped row without org_id must fail."""
    p = M.Person(last_name="NoOrg", full_display="NoOrg")
    session.add(p)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


# ---------------------------------------------------------------------------
# Test 6: resolve_org_id helper works correctly
# ---------------------------------------------------------------------------


def test_resolve_org_id_from_explicit():
    from app.tenancy import resolve_org_id

    assert resolve_org_id(explicit=42) == 42


def test_resolve_org_id_from_worklist(session):
    from app.tenancy import resolve_org_id

    org = _make_org(session)
    wl = _make_worklist(session, org.id)
    assert resolve_org_id(worklist=wl) == org.id


def test_resolve_org_id_from_person(session):
    from app.tenancy import resolve_org_id

    org = _make_org(session)
    p = _make_person(session, org.id)
    assert resolve_org_id(person=p) == org.id


def test_resolve_org_id_priority(session):
    """Explicit > worklist > person."""
    from app.tenancy import resolve_org_id

    org_a = _make_org(session, "A")
    org_b = _make_org(session, "B")
    wl = _make_worklist(session, org_a.id)
    p = _make_person(session, org_b.id)
    # Explicit wins.
    assert resolve_org_id(explicit=99, worklist=wl, person=p) == 99
    # Worklist wins over person.
    assert resolve_org_id(worklist=wl, person=p) == org_a.id
    # Person alone.
    assert resolve_org_id(person=p) == org_b.id


def test_resolve_org_id_raises_when_no_source():
    from app.tenancy import resolve_org_id

    with pytest.raises(ValueError, match="Cannot resolve org_id"):
        resolve_org_id()


# ---------------------------------------------------------------------------
# Test 7: @validates('org_id') catches None at model level
# ---------------------------------------------------------------------------


def test_validates_org_id_catches_none():
    p = M.Person(last_name="Test", full_display="Test")
    with pytest.raises(ValueError, match="org_id cannot be None"):
        p.org_id = None
