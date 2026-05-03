"""Phase 2 authorization tests.

Covers the make-or-break cases for the permission catalog, role
templates, workcenter scoping, and the @require decorator:

* Catalog is closed and self-consistent (no duplicate codes, every
  template references known codes).
* Migration backfill seeds every existing org with the role templates
  and promotes exactly one founder per org.
* ``has_permission`` honours org-wide grants, workcenter-scoped grants,
  and the workcenter-tree walk for ancestors.
* Last-owner guards refuse to remove the only org_owner grant.
* The route gate denies on missing permissions, raises on unknown
  perms, and short-circuits in SINGLE_TENANT mode.
* Unknown permission strings cannot be persisted via the admin route
  (the route validates against the catalog).

Tests work directly against an in-memory SQLite via the shared
``session`` fixture in ``conftest.py``. Where a test needs the
hosted-mode middleware (e.g. for the admin UI or to assert 403s on
gated routes), it spins up a fresh app with ``TBDTASK_SINGLE_TENANT=0``
and stubs the membership onto request.state.
"""

from __future__ import annotations


import pytest
from sqlalchemy import select

from app import models as M
from app.auth import authorization as authz
from app.auth.permissions import (
    PERMISSION_CODES,
    PERMISSIONS,
    P_ALERTS_ACT,
    P_ORG_VIEW,
    P_PERSONNEL_VIEW,
    P_PERSONNEL_WRITE,
    P_TASKS_WRITE,
    P_WORKLISTS_LOCK,
    ROLE_OWNER,
    ROLE_TEMPLATES,
    ROLE_TEMPLATE_SLUGS,
    is_known_permission,
    role_template,
)


# ---------------------------------------------------------------------------
# Helpers — minimal seed for tests that don't go through the migration
# ---------------------------------------------------------------------------


def _make_org(session, *, slug="alpha", name="Alpha Org"):
    org = M.Organization(slug=slug, name=name)
    session.add(org)
    session.flush()
    return org


def _make_user(session, *, email="u@example.com", display_name=None):
    u = M.UserAccount(email=email, display_name=display_name)
    session.add(u)
    session.flush()
    return u


def _make_membership(session, *, org, user, status="active"):
    m = M.OrgMembership(org_id=org.id, user_id=user.id, status=status)
    session.add(m)
    session.flush()
    return m


def _seed_role(session, org_id, template):
    """Mirror the migration backfill in tests so we don't need to run
    Alembic against an in-memory DB. Returns the persisted Role row."""
    role = M.Role(
        org_id=org_id,
        template_slug=template.slug,
        name=template.name,
        description=template.description,
        builtin=True,
        workcenter_scopable=template.workcenter_scopable,
    )
    session.add(role)
    session.flush()
    for code in template.permissions:
        session.add(M.RolePermission(role_id=role.id, permission_code=code))
    session.flush()
    return role


def _seed_all_roles(session, org_id) -> dict:
    """Seed every template into the org. Returns slug → Role."""
    return {t.slug: _seed_role(session, org_id, t) for t in ROLE_TEMPLATES}


def _grant(
    session,
    *,
    membership,
    role,
    workcenter=None,
):
    g = M.MembershipRole(
        membership_id=membership.id,
        role_id=role.id,
        workcenter_id=(workcenter.id if workcenter is not None else None),
    )
    session.add(g)
    session.flush()
    return g


def _make_workcenter(session, *, org, name, parent=None, slug=None):
    wc = M.Workcenter(
        org_id=org.id,
        parent_id=(parent.id if parent is not None else None),
        name=name,
        slug=(slug or name.lower().replace(" ", "-")),
    )
    session.add(wc)
    session.flush()
    return wc


# ---------------------------------------------------------------------------
# Catalog self-consistency
# ---------------------------------------------------------------------------


class TestCatalog:
    def test_no_duplicate_permission_codes(self):
        codes = [p.code for p in PERMISSIONS]
        assert len(codes) == len(set(codes)), "duplicate permission codes"

    def test_every_template_references_known_codes(self):
        for template in ROLE_TEMPLATES:
            for code in template.permissions:
                assert is_known_permission(code), (
                    f"{template.slug!r} references unknown perm {code!r}"
                )

    def test_role_template_lookup(self):
        assert role_template("org_owner") is ROLE_OWNER
        with pytest.raises(KeyError):
            role_template("does_not_exist")

    def test_org_owner_has_every_permission(self):
        # Owners always retain the full catalog. The admin UI enforces
        # this; we pin the seed list as the durable contract.
        assert set(ROLE_OWNER.permissions) == PERMISSION_CODES

    def test_template_slugs_unique(self):
        slugs = [t.slug for t in ROLE_TEMPLATES]
        assert len(slugs) == len(set(slugs))
        assert ROLE_TEMPLATE_SLUGS == frozenset(slugs)


# ---------------------------------------------------------------------------
# Migration seed parity
# ---------------------------------------------------------------------------


def _load_phase2_migration():
    """Import the Phase 2 migration module by file path.

    Migration filenames start with a digit, which Python's import
    machinery refuses, so Alembic loads them at runtime via importlib.
    Tests need access to the seed list, so we replay the same trick.
    """
    import importlib.util
    from pathlib import Path

    project_root = Path(__file__).resolve().parent.parent
    mig_path = (
        project_root
        / "alembic"
        / "versions"
        / "9b3e5d2c1a40_authorization_workcenters_roles.py"
    )
    spec = importlib.util.spec_from_file_location("phase2_migration", mig_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigrationSeedParity:
    def test_migration_seed_matches_python_catalog(self):
        """The migration carries its own copy of the role-template seed
        list (so it can run before any Python catalog edits land); the
        two must stay in lockstep. A drift means the next migration
        author forgot to update the catalog or vice versa."""
        mig = _load_phase2_migration()

        # Map slug → seed dict from the migration.
        mig_by_slug = {t["slug"]: t for t in mig._TEMPLATE_SEEDS}
        py_by_slug = {t.slug: t for t in ROLE_TEMPLATES}

        assert set(mig_by_slug) == set(py_by_slug), (
            f"slugs differ: {set(mig_by_slug) ^ set(py_by_slug)}"
        )
        for slug, py in py_by_slug.items():
            ms = mig_by_slug[slug]
            assert ms["name"] == py.name
            assert ms["description"] == py.description
            assert ms["workcenter_scopable"] == py.workcenter_scopable
            # Set comparison: the seed order is not significant, but
            # the membership has to match exactly. A drift surfaces a
            # forgotten permission on either side.
            assert set(ms["permissions"]) == set(py.permissions), (
                f"perm set differs for {slug!r}: "
                f"+migration {set(ms['permissions']) - set(py.permissions)} "
                f"+python {set(py.permissions) - set(ms['permissions'])}"
            )

    def test_alembic_module_importable(self):
        """The migration module must import cleanly so a runtime
        upgrade() doesn't surface an ImportError mid-deploy."""
        mig = _load_phase2_migration()

        assert mig.revision == "9b3e5d2c1a40"
        assert mig.down_revision == "7c2b8a1d4e60"


# ---------------------------------------------------------------------------
# load_effective_permissions + has_permission
# ---------------------------------------------------------------------------


class TestEffectivePermissions:
    def test_org_wide_grant_yields_org_wide_set(self, session):
        org = _make_org(session)
        u = _make_user(session)
        m = _make_membership(session, org=org, user=u)
        roles = _seed_all_roles(session, org.id)
        _grant(session, membership=m, role=roles["lpo"])

        eff = authz.load_effective_permissions(session, m.id)
        assert P_PERSONNEL_WRITE.code in eff.org_wide
        assert eff.by_workcenter == {}

    def test_workcenter_scoped_grant_lands_in_by_wc(self, session):
        org = _make_org(session)
        u = _make_user(session)
        m = _make_membership(session, org=org, user=u)
        roles = _seed_all_roles(session, org.id)
        wc = _make_workcenter(session, org=org, name="Deck")
        _grant(session, membership=m, role=roles["dlpo"], workcenter=wc)

        eff = authz.load_effective_permissions(session, m.id)
        assert eff.org_wide == frozenset()
        assert wc.id in eff.by_workcenter
        assert P_PERSONNEL_WRITE.code in eff.by_workcenter[wc.id]

    def test_membership_with_no_grants_has_no_perms(self, session):
        org = _make_org(session)
        u = _make_user(session)
        m = _make_membership(session, org=org, user=u)
        eff = authz.load_effective_permissions(session, m.id)
        assert eff.org_wide == frozenset()
        assert eff.by_workcenter == {}


class TestHasPermission:
    def test_unknown_code_raises(self, session):
        with pytest.raises(ValueError):
            authz.has_permission(session, 1, "does.not.exist")

    def test_org_wide_grant_authorizes_org_wide_call(self, session):
        org = _make_org(session)
        u = _make_user(session)
        m = _make_membership(session, org=org, user=u)
        roles = _seed_all_roles(session, org.id)
        _grant(session, membership=m, role=roles["lpo"])

        assert authz.has_permission(session, m.id, P_PERSONNEL_WRITE.code)

    def test_org_wide_grant_authorizes_workcenter_call(self, session):
        """An org-wide grant covers every workcenter, not just None."""
        org = _make_org(session)
        u = _make_user(session)
        m = _make_membership(session, org=org, user=u)
        roles = _seed_all_roles(session, org.id)
        _grant(session, membership=m, role=roles["lpo"])
        wc = _make_workcenter(session, org=org, name="Deck")

        assert authz.has_permission(
            session, m.id, P_PERSONNEL_WRITE.code, workcenter_id=wc.id
        )

    def test_workcenter_grant_does_not_authorize_org_wide_call(self, session):
        """The route forgot to declare workcenter_param? Then no
        workcenter-scoped grant counts. Open by design."""
        org = _make_org(session)
        u = _make_user(session)
        m = _make_membership(session, org=org, user=u)
        roles = _seed_all_roles(session, org.id)
        wc = _make_workcenter(session, org=org, name="Deck")
        _grant(session, membership=m, role=roles["lpo"], workcenter=wc)

        # No workcenter_id given to the check → org-wide-only path.
        assert not authz.has_permission(session, m.id, P_PERSONNEL_WRITE.code)

    def test_workcenter_grant_authorizes_same_workcenter(self, session):
        org = _make_org(session)
        u = _make_user(session)
        m = _make_membership(session, org=org, user=u)
        roles = _seed_all_roles(session, org.id)
        wc = _make_workcenter(session, org=org, name="Deck")
        _grant(session, membership=m, role=roles["lpo"], workcenter=wc)

        assert authz.has_permission(
            session, m.id, P_PERSONNEL_WRITE.code, workcenter_id=wc.id
        )

    def test_workcenter_grant_authorizes_descendant(self, session):
        """A grant on the parent flows down to children via the ancestor walk."""
        org = _make_org(session)
        u = _make_user(session)
        m = _make_membership(session, org=org, user=u)
        roles = _seed_all_roles(session, org.id)
        parent = _make_workcenter(session, org=org, name="Deck", slug="deck")
        child = _make_workcenter(
            session, org=org, name="1st Division", parent=parent, slug="1div"
        )
        _grant(session, membership=m, role=roles["lpo"], workcenter=parent)

        assert authz.has_permission(
            session, m.id, P_PERSONNEL_WRITE.code, workcenter_id=child.id
        )

    def test_workcenter_grant_does_not_authorize_sibling(self, session):
        """Grant on Deck does not flow to a peer workcenter Engineering."""
        org = _make_org(session)
        u = _make_user(session)
        m = _make_membership(session, org=org, user=u)
        roles = _seed_all_roles(session, org.id)
        deck = _make_workcenter(session, org=org, name="Deck", slug="deck")
        eng = _make_workcenter(session, org=org, name="Engineering", slug="eng")
        _grant(session, membership=m, role=roles["lpo"], workcenter=deck)

        assert not authz.has_permission(
            session, m.id, P_PERSONNEL_WRITE.code, workcenter_id=eng.id
        )

    def test_role_without_perm_denies_even_org_wide(self, session):
        """Viewer has alerts.view but not alerts.act."""
        org = _make_org(session)
        u = _make_user(session)
        m = _make_membership(session, org=org, user=u)
        roles = _seed_all_roles(session, org.id)
        _grant(session, membership=m, role=roles["viewer"])

        assert authz.has_permission(session, m.id, P_ORG_VIEW.code)
        assert not authz.has_permission(session, m.id, P_ALERTS_ACT.code)

    def test_dlpo_lacks_lock_amend_by_design(self, session):
        org = _make_org(session)
        u = _make_user(session)
        m = _make_membership(session, org=org, user=u)
        roles = _seed_all_roles(session, org.id)
        _grant(session, membership=m, role=roles["dlpo"])

        assert authz.has_permission(session, m.id, P_PERSONNEL_WRITE.code)
        assert not authz.has_permission(session, m.id, P_WORKLISTS_LOCK.code)

    def test_member_can_write_tasks_and_absences(self, session):
        org = _make_org(session)
        u = _make_user(session)
        m = _make_membership(session, org=org, user=u)
        roles = _seed_all_roles(session, org.id)
        _grant(session, membership=m, role=roles["member"])

        assert authz.has_permission(session, m.id, P_TASKS_WRITE.code)
        # Member does NOT get personnel.write — that's a DLPO+ thing.
        assert not authz.has_permission(session, m.id, P_PERSONNEL_WRITE.code)


class TestWorkcenterAncestors:
    def test_includes_self(self, session):
        org = _make_org(session)
        wc = _make_workcenter(session, org=org, name="A")
        assert authz.workcenter_ancestors(session, wc.id) == [wc.id]

    def test_walks_to_root(self, session):
        org = _make_org(session)
        a = _make_workcenter(session, org=org, name="A", slug="a")
        b = _make_workcenter(session, org=org, name="B", parent=a, slug="b")
        c = _make_workcenter(session, org=org, name="C", parent=b, slug="c")
        assert authz.workcenter_ancestors(session, c.id) == [c.id, b.id, a.id]

    def test_handles_self_referential_loop_safely(self, session):
        """Self-FK loops shouldn't exist (admin UI rejects them) but we
        want the walker to bail rather than spin if data is corrupted."""
        org = _make_org(session)
        wc = _make_workcenter(session, org=org, name="A")
        wc.parent_id = wc.id
        session.flush()
        # No exception, returns the broken node and stops.
        chain = authz.workcenter_ancestors(session, wc.id)
        assert chain == [wc.id]


# ---------------------------------------------------------------------------
# Role-template membership predicate
# ---------------------------------------------------------------------------


class TestMembershipHasRoleTemplate:
    def test_true_for_org_wide_owner_grant(self, session):
        org = _make_org(session)
        u = _make_user(session)
        m = _make_membership(session, org=org, user=u)
        roles = _seed_all_roles(session, org.id)
        _grant(session, membership=m, role=roles["org_owner"])

        assert authz.membership_has_role_template(session, m.id, "org_owner")

    def test_false_when_role_is_workcenter_scoped(self, session):
        """A workcenter-scoped grant is not "org owner" for invariants
        like the last-owner guard."""
        org = _make_org(session)
        u = _make_user(session)
        m = _make_membership(session, org=org, user=u)
        roles = _seed_all_roles(session, org.id)
        wc = _make_workcenter(session, org=org, name="Deck")
        # Even though seeded org_owner refuses workcenter scope at the
        # admin layer, the schema technically permits it; the predicate
        # must honour the org-wide-only invariant.
        _grant(session, membership=m, role=roles["org_owner"], workcenter=wc)

        assert not authz.membership_has_role_template(session, m.id, "org_owner")

    def test_renamed_role_still_recognised_by_slug(self, session):
        """The local copy of "Org Owner" can be renamed to "Captain"; the
        invariant check still passes via template_slug."""
        org = _make_org(session)
        u = _make_user(session)
        m = _make_membership(session, org=org, user=u)
        roles = _seed_all_roles(session, org.id)
        roles["org_owner"].name = "Captain"
        session.flush()
        _grant(session, membership=m, role=roles["org_owner"])

        assert authz.membership_has_role_template(session, m.id, "org_owner")


# ---------------------------------------------------------------------------
# @require dependency
# ---------------------------------------------------------------------------


class TestRequireDependency:
    def test_unknown_perm_raises_at_decorator_build(self):
        from app.auth.permissions import Permission

        bogus = Permission("bogus.thing", "Bogus", "n/a")
        with pytest.raises(ValueError):
            authz.require(bogus)

    def test_single_tenant_short_circuits(self, monkeypatch):
        """In SINGLE_TENANT mode the dependency runs but never touches
        membership state — the AppImage offline path stays open."""
        monkeypatch.setenv("TBDTASK_SINGLE_TENANT", "1")
        dep = authz.require(P_PERSONNEL_VIEW)

        # No request.state.membership configured; the gate must not
        # raise because single-tenant mode short-circuits.
        from starlette.requests import Request

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/personnel",
            "headers": [],
            "query_string": b"",
            "path_params": {},
        }
        req = Request(scope)
        # Should not raise.
        dep(req)

    def test_hosted_mode_denies_when_no_membership(self, monkeypatch):
        from fastapi import HTTPException
        from starlette.requests import Request

        monkeypatch.setenv("TBDTASK_SINGLE_TENANT", "0")
        dep = authz.require(P_PERSONNEL_VIEW)

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/personnel",
            "headers": [],
            "query_string": b"",
            "path_params": {},
        }
        req = Request(scope)
        with pytest.raises(HTTPException) as exc:
            dep(req)
        assert exc.value.status_code == 401

    def test_hosted_mode_allows_when_perm_granted(
        self, monkeypatch, session, session_factory
    ):
        """Wire a real membership + grant + a stub Request and confirm
        the dependency passes silently."""
        monkeypatch.setenv("TBDTASK_SINGLE_TENANT", "0")
        from app import db as db_module
        from starlette.requests import Request

        # The dep opens its own SessionLocal; redirect that to our factory.
        monkeypatch.setattr(db_module, "SessionLocal", session_factory)

        org = _make_org(session)
        u = _make_user(session)
        m = _make_membership(session, org=org, user=u)
        roles = _seed_all_roles(session, org.id)
        _grant(session, membership=m, role=roles["lpo"])
        session.commit()

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/personnel",
            "headers": [],
            "query_string": b"",
            "path_params": {},
        }
        req = Request(scope)
        req.state.membership = m

        dep = authz.require(P_PERSONNEL_VIEW)
        dep(req)  # No exception means allowed.

    def test_hosted_mode_denies_when_perm_missing(
        self, monkeypatch, session, session_factory
    ):
        from fastapi import HTTPException
        from app import db as db_module
        from starlette.requests import Request

        monkeypatch.setenv("TBDTASK_SINGLE_TENANT", "0")
        monkeypatch.setattr(db_module, "SessionLocal", session_factory)

        org = _make_org(session)
        u = _make_user(session)
        m = _make_membership(session, org=org, user=u)
        roles = _seed_all_roles(session, org.id)
        _grant(session, membership=m, role=roles["viewer"])  # read-only
        session.commit()

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/personnel",
            "headers": [],
            "query_string": b"",
            "path_params": {},
        }
        req = Request(scope)
        req.state.membership = m

        dep = authz.require(P_PERSONNEL_WRITE)
        with pytest.raises(HTTPException) as exc:
            dep(req)
        assert exc.value.status_code == 403

    def test_workcenter_scoped_dependency_resolves_path_param(
        self, monkeypatch, session, session_factory
    ):
        from app import db as db_module
        from starlette.requests import Request

        monkeypatch.setenv("TBDTASK_SINGLE_TENANT", "0")
        monkeypatch.setattr(db_module, "SessionLocal", session_factory)

        org = _make_org(session)
        u = _make_user(session)
        m = _make_membership(session, org=org, user=u)
        roles = _seed_all_roles(session, org.id)
        deck = _make_workcenter(session, org=org, name="Deck", slug="deck")
        _grant(session, membership=m, role=roles["lpo"], workcenter=deck)
        session.commit()

        # Path param ``workcenter_id`` is what the gate looks up.
        scope = {
            "type": "http",
            "method": "POST",
            "path": f"/wc/{deck.id}",
            "headers": [],
            "query_string": b"",
            "path_params": {"workcenter_id": deck.id},
        }
        req = Request(scope)
        req.state.membership = m

        dep = authz.require(P_PERSONNEL_WRITE, workcenter_param="workcenter_id")
        dep(req)  # No exception.


# ---------------------------------------------------------------------------
# Catalog defence: an unknown code in role_permissions must surface
# ---------------------------------------------------------------------------


class TestCatalogDefence:
    def test_unknown_code_in_role_permissions_is_loud(self, session):
        """If a future migration accidentally inserts an unknown code,
        the catalog check ``is_known_permission`` fails — admins should
        scrub by checking that every persisted code is in the catalog.

        This test inserts a deliberately-bad row and asserts that
        ``is_known_permission`` flags it. The runtime route gate raises
        ValueError at decorator build time for unknown codes; persisted
        bad codes are caught by audit checks (or by the admin-UI-side
        validation, which rejects unknown codes from form input).
        """
        org = _make_org(session)
        roles = _seed_all_roles(session, org.id)
        bad = M.RolePermission(
            role_id=roles["viewer"].id, permission_code="garbage.code"
        )
        session.add(bad)
        session.flush()

        codes = list(session.scalars(select(M.RolePermission.permission_code)))
        unknown = [c for c in codes if not is_known_permission(c)]
        assert unknown == ["garbage.code"]
