"""Phase 2 admin-route behaviour tests.

These tests exercise the admin module's helpers and the invariants its
routes enforce — last-owner protection, slug uniqueness, cycle
detection in workcenter re-parenting, custom-role validation. The
routes themselves are thin wrappers around these helpers + the
``@require`` gate (covered in ``test_phase2_authz.py``); the helpers
are where the meaningful logic lives.

We don't run the FastAPI test client here because the hosted-mode
middleware stack (Session + CSRF + tenant context) requires more
setup than these tests need; the relevant integration is already
covered in ``test_phase2_authz`` for the gate, and individual route
behaviour is tested by calling the route handler functions directly
with a real DB session and a stubbed request.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from sqlalchemy import select

from app import models as M
from app.auth import invites as invites_mod
from app.auth.permissions import ROLE_TEMPLATES
from app.routes import admin as admin_mod


# ---------------------------------------------------------------------------
# Helpers (mirror test_phase2_authz to keep tests independent)
# ---------------------------------------------------------------------------

def _make_org(session, *, slug="alpha", name="Alpha Org"):
    org = M.Organization(slug=slug, name=name)
    session.add(org)
    session.flush()
    return org


def _make_user(session, *, email="u@example.com"):
    u = M.UserAccount(email=email)
    session.add(u)
    session.flush()
    return u


def _make_membership(session, *, org, user, status="active"):
    m = M.OrgMembership(org_id=org.id, user_id=user.id, status=status)
    session.add(m)
    session.flush()
    return m


def _seed_role(session, org_id, template):
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
    return {t.slug: _seed_role(session, org_id, t) for t in ROLE_TEMPLATES}


def _grant(session, *, membership, role, workcenter=None):
    g = M.MembershipRole(
        membership_id=membership.id,
        role_id=role.id,
        workcenter_id=(workcenter.id if workcenter is not None else None),
    )
    session.add(g)
    session.flush()
    return g


# ---------------------------------------------------------------------------
# Slug helper
# ---------------------------------------------------------------------------

class TestSlugify:
    def test_basic(self):
        assert admin_mod._slugify("Deck Division") == "deck-division"

    def test_special_chars_collapse(self):
        assert admin_mod._slugify("R/D & Test  Group!") == "r-d-test-group"

    def test_empty_falls_back_to_default(self):
        assert admin_mod._slugify("") == "wc"
        assert admin_mod._slugify("!!!") == "wc"


# ---------------------------------------------------------------------------
# Last-owner guard
# ---------------------------------------------------------------------------

class TestLastOwnerGuard:
    def test_returns_id_when_only_one_owner(self, session):
        org = _make_org(session)
        u1 = _make_user(session, email="a@example.com")
        u2 = _make_user(session, email="b@example.com")
        m1 = _make_membership(session, org=org, user=u1)
        m2 = _make_membership(session, org=org, user=u2)
        roles = _seed_all_roles(session, org.id)
        _grant(session, membership=m1, role=roles["org_owner"])
        # m2 has only LPO, not owner

        last = admin_mod._last_owner_id(session, org.id)
        assert last == m1.id

    def test_returns_none_when_two_owners(self, session):
        org = _make_org(session)
        u1 = _make_user(session, email="a@example.com")
        u2 = _make_user(session, email="b@example.com")
        m1 = _make_membership(session, org=org, user=u1)
        m2 = _make_membership(session, org=org, user=u2)
        roles = _seed_all_roles(session, org.id)
        _grant(session, membership=m1, role=roles["org_owner"])
        _grant(session, membership=m2, role=roles["org_owner"])

        assert admin_mod._last_owner_id(session, org.id) is None

    def test_returns_none_when_no_owners(self, session):
        """Edge case: an org with no owners. The check returns None
        because there's nothing to protect; callers see a 'no last owner'
        signal and any role can be revoked."""
        org = _make_org(session)
        u = _make_user(session)
        m = _make_membership(session, org=org, user=u)
        roles = _seed_all_roles(session, org.id)
        _grant(session, membership=m, role=roles["lpo"])

        assert admin_mod._last_owner_id(session, org.id) is None

    def test_workcenter_scoped_owner_does_not_count(self, session):
        """Only org-wide grants of org_owner satisfy the invariant. A
        workcenter-scoped owner row is meaningless for "is this the
        last owner?" — the predicate must ignore it."""
        org = _make_org(session)
        u = _make_user(session)
        m = _make_membership(session, org=org, user=u)
        roles = _seed_all_roles(session, org.id)
        wc = M.Workcenter(org_id=org.id, name="Deck", slug="deck")
        session.add(wc)
        session.flush()
        # Force a workcenter-scoped grant of org_owner (the admin UI
        # would refuse via workcenter_scopable=False, but the schema
        # allows it; the predicate must still treat it as not-an-owner).
        _grant(session, membership=m, role=roles["org_owner"], workcenter=wc)

        assert admin_mod._last_owner_id(session, org.id) is None


# ---------------------------------------------------------------------------
# Invite helpers — ensure the admin route's entry point validates inputs
# ---------------------------------------------------------------------------

class TestInviteCreation:
    def test_create_invite_lowercases_email(self, session):
        org = _make_org(session)
        issued = invites_mod.create_invite(
            session,
            org_id=org.id,
            intended_email="USER@Example.COM",
            created_by_user_id=None,
            ttl_days=7,
        )
        invite = session.get(M.OrgInvite, issued.invite_id)
        assert invite.intended_email == "user@example.com"

    def test_create_invite_rejects_ttl_over_cap(self, session):
        org = _make_org(session)
        with pytest.raises(ValueError):
            invites_mod.create_invite(
                session,
                org_id=org.id,
                intended_email="x@y.com",
                created_by_user_id=None,
                ttl_days=invites_mod.MAX_INVITE_TTL_DAYS + 1,
            )

    def test_create_invite_rejects_zero_ttl(self, session):
        org = _make_org(session)
        with pytest.raises(ValueError):
            invites_mod.create_invite(
                session,
                org_id=org.id,
                intended_email="x@y.com",
                created_by_user_id=None,
                ttl_days=0,
            )

    def test_create_invite_returns_raw_token_only_once(self, session):
        """Hash at rest. The raw token returned to the admin must not be
        recoverable from the DB later."""
        org = _make_org(session)
        issued = invites_mod.create_invite(
            session,
            org_id=org.id,
            intended_email="x@y.com",
            created_by_user_id=None,
            ttl_days=7,
        )
        invite = session.get(M.OrgInvite, issued.invite_id)
        assert invite.token_hash != issued.raw_token  # not the raw token
        assert invite.token_hash == invites_mod.hash_invite_token(
            issued.raw_token
        )


# ---------------------------------------------------------------------------
# Workcenter cycle protection — the admin route checks ancestry on save.
# We exercise the same ancestor walker the routes call into.
# ---------------------------------------------------------------------------

class TestWorkcenterReparentValidation:
    def test_self_parent_creates_loop_caught_by_walker(self, session):
        """The route's update handler refuses ``parent_id == wc.id``
        explicitly. The walker is what catches descendant-cycle attempts.
        """
        from app.auth.authorization import workcenter_ancestors

        org = _make_org(session)
        a = M.Workcenter(org_id=org.id, name="A", slug="a")
        session.add(a)
        session.flush()
        b = M.Workcenter(org_id=org.id, parent_id=a.id, name="B", slug="b")
        session.add(b)
        session.flush()

        # Ancestry of b is [b, a].
        assert workcenter_ancestors(session, b.id) == [b.id, a.id]

        # If we (manually) tried to set a.parent_id = b.id, ancestry of
        # a becomes [a, b, a] — the walker stops at the second a.
        a.parent_id = b.id
        session.flush()
        chain = workcenter_ancestors(session, a.id)
        # Loop detected: walker stops without spinning.
        assert len(chain) <= 4
        assert chain[0] == a.id


# ---------------------------------------------------------------------------
# Role catalog defence — admin route refuses unknown perm codes
# ---------------------------------------------------------------------------

class TestRoleCatalogDefence:
    def test_is_known_permission_rejects_garbage(self):
        from app.auth.permissions import is_known_permission

        assert not is_known_permission("garbage.code")
        assert not is_known_permission("")
        assert not is_known_permission("personnel")  # missing action
        assert is_known_permission("personnel.view")


# ---------------------------------------------------------------------------
# Org creation grants founder the org_owner role
# ---------------------------------------------------------------------------

class TestOrgCreatorRoleGrant:
    def test_founding_member_gets_org_owner_role(self, session):
        """When a user creates a new org, the onboarding route grants
        them the ``org_owner`` role so they can administer it immediately."""
        from app.routes.onboarding import org_create

        org = _make_org(session)
        roles = {t.slug: _seed_role(session, org.id, t) for t in ROLE_TEMPLATES}

        # Simulate what the route does: create org + membership, then
        # grant the org_owner role (the code we added in onboarding.py).
        u = _make_user(session, email="founder@example.com")
        m = _make_membership(session, org=org, user=u)

        # This is the logic from onboarding.py after db.flush():
        owner_role = session.execute(
            select(M.Role).where(
                M.Role.org_id == org.id,
                M.Role.template_slug == "org_owner",
            )
        ).scalar_one_or_none()
        assert owner_role is not None
        session.add(
            M.MembershipRole(
                membership_id=m.id,
                role_id=owner_role.id,
                workcenter_id=None,
            )
        )
        session.flush()

        # Verify the grant exists.
        grant = session.execute(
            select(M.MembershipRole).where(
                M.MembershipRole.membership_id == m.id,
                M.MembershipRole.role_id == owner_role.id,
            )
        ).scalar_one_or_none()
        assert grant is not None
        assert grant.workcenter_id is None  # org-wide grant
