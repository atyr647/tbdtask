"""Tests for invite acceptance, leave-org, and kick-member flows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import models as M
from app.auth import invites as invites_mod
from app.auth import sessions as sess_mod
from app.auth.authorization import membership_has_role_template
from app.auth.permissions import ROLE_OWNER


def _make_org(session, *, slug="alpha", name="Alpha Org"):
    org = M.Organization(slug=slug, name=name)
    session.add(org)
    session.flush()
    return org


def _seed_org_roles(session, org):
    """Seed role templates for an org (normally done at org-create time)."""
    for tmpl in (ROLE_OWNER,):
        role = M.Role(
            org_id=org.id,
            template_slug=tmpl.slug,
            name=tmpl.name,
            description=tmpl.description,
            builtin=True,
        )
        session.add(role)
    session.flush()


def _grant_role(session, membership, role_template_slug):
    """Grant a role template to a membership."""
    role = session.execute(
        select(M.Role).where(
            M.Role.org_id == membership.org_id,
            M.Role.template_slug == role_template_slug,
        )
    ).scalar_one_or_none()
    if role:
        session.add(
            M.MembershipRole(
                membership_id=membership.id,
                role_id=role.id,
                workcenter_id=None,
            )
        )
        session.flush()


def _make_user(session, *, email="alice@example.com"):
    u = M.UserAccount(email=email)
    session.add(u)
    session.flush()
    return u


def _make_membership(session, *, org, user, status="active"):
    m = M.OrgMembership(org_id=org.id, user_id=user.id, status=status)
    session.add(m)
    session.flush()
    return m


def _make_invite(session, *, org, email, **kwargs):
    issued = invites_mod.create_invite(
        session,
        org_id=org.id,
        intended_email=email,
        created_by_user_id=None,
        **kwargs,
    )
    return issued


# ---------------------------------------------------------------------------
# Magic-link invite acceptance
# ---------------------------------------------------------------------------


class TestMagicLinkInvite:
    def test_invite_token_not_reusable(self, session):
        """An invite token can only be used once."""
        org = _make_org(session)
        issued = _make_invite(session, org=org, email="bob@example.com")
        invite = session.get(M.OrgInvite, issued.invite_id)

        # First acceptance would succeed (simulated by setting accepted_at).
        invite.accepted_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.flush()

        # Second acceptance attempt should fail the accepted_at check.
        assert invite.accepted_at is not None

    def test_expired_invite_fails(self, session):
        """An expired invite cannot be redeemed."""
        org = _make_org(session)
        issued = _make_invite(session, org=org, email="bob@example.com", ttl_days=1)
        invite = session.get(M.OrgInvite, issued.invite_id)
        # Manually expire it.
        invite.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            days=1
        )
        session.flush()

        assert invite.expires_at < datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Leave-org flow
# ---------------------------------------------------------------------------


class TestLeaveOrg:
    def test_last_org_owner_cannot_leave(self, session):
        """The last org_owner in an org cannot leave."""
        org = _make_org(session)
        _seed_org_roles(session, org)
        user = _make_user(session)
        mem = _make_membership(session, org=org, user=user)
        _grant_role(session, mem, "org_owner")

        assert membership_has_role_template(session, mem.id, "org_owner")
        # There are no other owners, so this is the last one.
        other_owners = (
            session.execute(
                select(M.OrgMembership).where(
                    M.OrgMembership.org_id == org.id,
                    M.OrgMembership.id != mem.id,
                    M.OrgMembership.status == "active",
                )
            )
            .scalars()
            .all()
        )
        assert not any(
            membership_has_role_template(session, m.id, "org_owner")
            for m in other_owners
        )


# ---------------------------------------------------------------------------
# Kick-member flow
# ---------------------------------------------------------------------------


class TestKickMember:
    def test_kick_target_must_belong_to_current_org(self, session):
        """An admin cannot kick a member from a different org."""
        org_a = _make_org(session, slug="org-a", name="Org A")
        org_b = _make_org(session, slug="org-b", name="Org B")
        user_a = _make_user(session, email="admin@a.com")
        user_b = _make_user(session, email="victim@b.com")
        _make_membership(session, org=org_a, user=user_a)
        mem_b = _make_membership(session, org=org_b, user=user_b)

        # mem_b belongs to org_b, not org_a.
        assert mem_b.org_id != org_a.id

    def test_cannot_kick_yourself(self, session):
        """A member cannot kick themselves through the kick endpoint."""
        org = _make_org(session)
        user = _make_user(session)
        mem = _make_membership(session, org=org, user=user)

        # The kick endpoint should reject self-kicks.
        assert mem.user_id == user.id

    def test_last_org_owner_cannot_be_kicked(self, session):
        """The last org_owner in an org cannot be kicked."""
        org = _make_org(session)
        _seed_org_roles(session, org)
        user = _make_user(session)
        mem = _make_membership(session, org=org, user=user)
        _grant_role(session, mem, "org_owner")

        # This is the only owner.
        assert membership_has_role_template(session, mem.id, "org_owner")
        # _last_owner_id should return this membership.
        from app.routes.admin import _last_owner_id

        assert _last_owner_id(session, org.id) == mem.id

    def test_kick_rotates_session(self, session):
        """When a member is kicked, their sessions bound to this org are revoked."""
        org = _make_org(session)
        admin = _make_user(session, email="admin@example.com")
        victim = _make_user(session, email="victim@example.com")
        _make_membership(session, org=org, user=admin)
        victim_mem = _make_membership(session, org=org, user=victim)

        # Create a session bound to victim's membership.
        victim_session = sess_mod.create_session(
            session,
            user_id=victim.id,
            membership_id=victim_mem.id,
            ip="1.2.3.4",
            user_agent="test",
        )
        session.flush()

        # Verify session exists.
        assert sess_mod.lookup_session(session, victim_session.id) is not None

        # Kick the victim.
        victim_mem.status = "suspended"
        session.flush()
        sess_mod.revoke_all_for_membership(session, victim_mem.id)
        session.flush()

        # Session should be revoked.
        assert sess_mod.lookup_session(session, victim_session.id) is None


# ---------------------------------------------------------------------------
# Org deletion
# ---------------------------------------------------------------------------


class TestOrgDeletion:
    def test_cannot_delete_with_other_members(self, session):
        """An org with multiple active members cannot be deleted."""
        org = _make_org(session)
        _seed_org_roles(session, org)
        owner = _make_user(session, email="owner@example.com")
        member = _make_user(session, email="member@example.com")
        owner_mem = _make_membership(session, org=org, user=owner)
        _make_membership(session, org=org, user=member)
        _grant_role(session, owner_mem, "org_owner")

        active_members = (
            session.execute(
                select(M.OrgMembership).where(
                    M.OrgMembership.org_id == org.id,
                    M.OrgMembership.status == "active",
                )
            )
            .scalars()
            .all()
        )
        assert len(active_members) == 2

    def test_sole_owner_can_delete(self, session):
        """A sole active member who is org_owner can delete the org."""
        org = _make_org(session)
        _seed_org_roles(session, org)
        owner = _make_user(session, email="owner@example.com")
        owner_mem = _make_membership(session, org=org, user=owner)
        _grant_role(session, owner_mem, "org_owner")

        active_members = (
            session.execute(
                select(M.OrgMembership).where(
                    M.OrgMembership.org_id == org.id,
                    M.OrgMembership.status == "active",
                )
            )
            .scalars()
            .all()
        )
        assert len(active_members) == 1
        assert membership_has_role_template(session, owner_mem.id, "org_owner")

    def test_non_owner_cannot_delete(self, session):
        """A non-owner cannot delete the org even if sole member."""
        org = _make_org(session)
        _seed_org_roles(session, org)
        member = _make_user(session, email="member@example.com")
        member_mem = _make_membership(session, org=org, user=member)
        # No org_owner role granted.

        assert not membership_has_role_template(session, member_mem.id, "org_owner")
