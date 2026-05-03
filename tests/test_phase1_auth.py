"""Phase 1 + 1.5 auth tests.

Covers the make-or-break cases agreed in the rollout plan:

* Provider login can't accidentally link to the wrong account.
* Suspended user / membership invalidates the session immediately.
* Cross-provider email collision returns ``LinkRequired`` (no silent merge).
* Apple private-relay emails are unsafe-for-linking even when Apple says
  verified.
* Invite tokens are email-bound, time-limited, single-use, hash-at-rest.
* CSRF tokens survive their session and reject mismatches.
* Rate limiter blocks after the threshold.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import models as M
from app.auth import accounts as acct_mod
from app.auth import invites as invites_mod
from app.auth import sessions as sess_mod
from app.auth.providers import NormalizedIdentity, normalize_userinfo
from app.auth.rate_limit import InProcessLimiter
from app.auth.security import (
    issue_csrf_token,
    issue_oidc_state,
    random_token,
    validate_csrf_token,
    validate_oidc_state,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_org(session, *, slug="alpha", name="Alpha Org"):
    org = M.Organization(slug=slug, name=name)
    session.add(org)
    session.flush()
    return org


def _make_user(session, *, email="user@example.com", display_name=None):
    u = M.UserAccount(email=email, display_name=display_name)
    session.add(u)
    session.flush()
    return u


def _make_membership(session, *, org, user, status="active"):
    m = M.OrgMembership(org_id=org.id, user_id=user.id, status=status)
    session.add(m)
    session.flush()
    return m


# ---------------------------------------------------------------------------
# Userinfo normalization
# ---------------------------------------------------------------------------


class TestNormalizeUserinfo:
    def test_google_basic_pass_through(self):
        ident = normalize_userinfo(
            "google",
            {
                "sub": "g-12345",
                "email": "Alice@Example.com",
                "email_verified": True,
                "name": "Alice Example",
            },
        )
        assert ident.provider == "google"
        assert ident.subject == "g-12345"
        # Email is lowercased on the way in.
        assert ident.email == "alice@example.com"
        assert ident.email_verified is True
        assert ident.display_name == "Alice Example"

    def test_microsoft_email_verified_inferred_when_upn_matches(self):
        ident = normalize_userinfo(
            "microsoft",
            {
                "sub": "m-9",
                "email": "bob@corp.com",
                "preferred_username": "bob@corp.com",
            },
        )
        assert ident.email_verified is True

    def test_microsoft_email_verified_false_when_upn_mismatches(self):
        ident = normalize_userinfo(
            "microsoft",
            {
                "sub": "m-9",
                "email": "bob@corp.com",
                "preferred_username": "different@corp.com",
            },
        )
        assert ident.email_verified is False

    def test_apple_private_relay_blocks_account(self):
        """Make-or-break: Apple private-relay raises PrivateRelayBlocked.

        We refuse to create accounts with relay addresses because they block
        invite acceptance (email matching) and identity linking.
        """
        from app.auth.providers import PrivateRelayBlocked

        with pytest.raises(PrivateRelayBlocked):
            normalize_userinfo(
                "apple",
                {
                    "sub": "a-1",
                    "email": "abc123@privaterelay.appleid.com",
                    "email_verified": True,  # Apple says verified...
                },
            )

    def test_apple_real_email_passthrough(self):
        ident = normalize_userinfo(
            "apple",
            {"sub": "a-2", "email": "user@example.com", "email_verified": True},
        )
        assert ident.email_verified is True

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError):
            normalize_userinfo("facebook", {"sub": "f-1"})

    def test_missing_sub_raises(self):
        with pytest.raises(ValueError):
            normalize_userinfo("google", {"email": "x@y.com"})


# ---------------------------------------------------------------------------
# Account resolution (linking safety)
# ---------------------------------------------------------------------------


class TestAccountResolution:
    def _ident(self, **overrides) -> NormalizedIdentity:
        defaults = dict(
            provider="google",
            subject="g-1",
            email="alice@example.com",
            email_verified=True,
            display_name="Alice",
        )
        defaults.update(overrides)
        return NormalizedIdentity(**defaults)

    def test_existing_identity_logs_in(self, session):
        ident = self._ident()
        out1 = acct_mod.resolve_identity(session, ident)
        assert isinstance(out1, acct_mod.CreatedAccount)
        out2 = acct_mod.resolve_identity(session, ident)
        assert isinstance(out2, acct_mod.LoggedIn)
        assert out2.user_id == out1.user_id
        assert out2.identity_id == out1.identity_id

    def test_no_match_creates_account(self, session):
        out = acct_mod.resolve_identity(session, self._ident())
        assert isinstance(out, acct_mod.CreatedAccount)
        user = session.get(M.UserAccount, out.user_id)
        assert user.email == "alice@example.com"

    def test_email_match_with_different_provider_returns_link_required(self, session):
        """Make-or-break: provider login can't accidentally link to the
        wrong account. Same email, different provider → LinkRequired."""
        out_google = acct_mod.resolve_identity(
            session, self._ident(provider="google", subject="g-1")
        )
        assert isinstance(out_google, acct_mod.CreatedAccount)

        out_microsoft = acct_mod.resolve_identity(
            session,
            self._ident(provider="microsoft", subject="m-1"),
        )
        assert isinstance(out_microsoft, acct_mod.LinkRequired)
        assert out_microsoft.existing_user_id == out_google.user_id
        assert out_microsoft.candidate_email == "alice@example.com"

    def test_unverified_email_does_not_link(self, session):
        """An IdP claiming an unverified email gets a fresh account, not a merge."""
        out_a = acct_mod.resolve_identity(session, self._ident())
        assert isinstance(out_a, acct_mod.CreatedAccount)

        out_b = acct_mod.resolve_identity(
            session,
            self._ident(
                provider="microsoft",
                subject="m-9",
                email="alice@example.com",
                email_verified=False,
            ),
        )
        # No link prompt — just a new account because the email isn't verified.
        assert isinstance(out_b, acct_mod.CreatedAccount)
        assert out_b.user_id != out_a.user_id

    def test_unverified_email_creates_fresh_account(self, session):
        """An identity with email_verified=False never triggers email-based
        linking — resolve_identity creates a fresh account instead."""
        ident = self._ident(
            provider="apple",
            subject="a-1",
            email="user@example.com",
            email_verified=False,
        )
        # First sign-in: new account, no link prompt.
        out = acct_mod.resolve_identity(session, ident)
        assert isinstance(out, acct_mod.CreatedAccount)

    def test_link_to_other_user_blocked(self, session):
        acct_mod.resolve_identity(
            session, self._ident(provider="google", subject="g-1")
        )
        out_b = acct_mod.resolve_identity(
            session,
            self._ident(provider="google", subject="g-2", email="other@example.com"),
        )
        # Trying to attach the existing google identity to a different user
        # must raise — the (provider, subject) is unique to its owner.
        with pytest.raises(ValueError):
            acct_mod.link_identity_to_user(
                session,
                user_id=out_b.user_id,
                ident=NormalizedIdentity(
                    provider="google",
                    subject="g-1",
                    email="alice@example.com",
                    email_verified=True,
                    display_name="Alice",
                ),
            )

    def test_link_idempotent_for_same_user(self, session):
        out = acct_mod.resolve_identity(session, self._ident())
        first = acct_mod.link_identity_to_user(
            session,
            user_id=out.user_id,
            ident=NormalizedIdentity(
                provider="microsoft",
                subject="m-1",
                email="alice@example.com",
                email_verified=True,
                display_name="Alice",
            ),
        )
        second = acct_mod.link_identity_to_user(
            session,
            user_id=out.user_id,
            ident=NormalizedIdentity(
                provider="microsoft",
                subject="m-1",
                email="alice@example.com",
                email_verified=True,
                display_name="Alice",
            ),
        )
        assert first.id == second.id


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class TestSessions:
    def test_create_and_lookup(self, session):
        user = _make_user(session)
        s = sess_mod.create_session(
            session,
            user_id=user.id,
            membership_id=None,
            ip="1.2.3.4",
            user_agent="test",
        )
        assert sess_mod.lookup_session(session, s.id) is s

    def test_lookup_unknown_id_returns_none(self, session):
        assert sess_mod.lookup_session(session, "nope") is None

    def test_lookup_returns_none_after_revoke(self, session):
        user = _make_user(session)
        s = sess_mod.create_session(
            session, user_id=user.id, membership_id=None, ip=None, user_agent=None
        )
        sess_mod.revoke(session, s, reason="test")
        assert sess_mod.lookup_session(session, s.id) is None

    def test_rotate_revokes_old_and_issues_new(self, session):
        user = _make_user(session)
        old = sess_mod.create_session(
            session, user_id=user.id, membership_id=None, ip=None, user_agent=None
        )
        new = sess_mod.rotate(session, old, reason="test")
        assert old.revoked_at is not None
        assert new.id != old.id
        assert new.user_id == old.user_id
        # Old id no longer resolves.
        assert sess_mod.lookup_session(session, old.id) is None
        assert sess_mod.lookup_session(session, new.id) is new

    def test_lookup_returns_none_when_user_disabled(self, session):
        user = _make_user(session)
        s = sess_mod.create_session(
            session, user_id=user.id, membership_id=None, ip=None, user_agent=None
        )
        user.disabled_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.flush()
        assert sess_mod.lookup_session(session, s.id) is None

    def test_lookup_returns_none_when_membership_suspended(self, session):
        """Make-or-break: suspending a membership invalidates the session
        on the very next request, not retroactively."""
        org = _make_org(session)
        user = _make_user(session)
        mem = _make_membership(session, org=org, user=user, status="active")
        s = sess_mod.create_session(
            session, user_id=user.id, membership_id=mem.id, ip=None, user_agent=None
        )
        assert sess_mod.lookup_session(session, s.id) is s

        mem.status = "suspended"
        session.flush()
        # No retroactive purge needed — the lookup just refuses.
        assert sess_mod.lookup_session(session, s.id) is None

    def test_idle_timeout(self, session):
        user = _make_user(session)
        s = sess_mod.create_session(
            session, user_id=user.id, membership_id=None, ip=None, user_agent=None
        )
        # Push last_seen_at past the idle window without touching created_at.
        s.last_seen_at = datetime.now(timezone.utc).replace(tzinfo=None) - (
            sess_mod.IDLE_TIMEOUT + timedelta(seconds=1)
        )
        session.flush()
        assert sess_mod.lookup_session(session, s.id) is None

    def test_absolute_timeout(self, session):
        user = _make_user(session)
        s = sess_mod.create_session(
            session, user_id=user.id, membership_id=None, ip=None, user_agent=None
        )
        # Push created_at past the absolute window (last_seen_at also moves
        # back to keep the window math consistent with a stale session).
        backdate = datetime.now(timezone.utc).replace(tzinfo=None) - (
            sess_mod.ABSOLUTE_TIMEOUT + timedelta(seconds=1)
        )
        s.created_at = backdate
        s.last_seen_at = backdate
        session.flush()
        assert sess_mod.lookup_session(session, s.id) is None

    def test_revoke_all_for_user(self, session):
        user = _make_user(session)
        sessions = [
            sess_mod.create_session(
                session,
                user_id=user.id,
                membership_id=None,
                ip=None,
                user_agent=None,
            )
            for _ in range(3)
        ]
        revoked = sess_mod.revoke_all_for_user(session, user.id, reason="test")
        assert revoked == 3
        for s in sessions:
            assert sess_mod.lookup_session(session, s.id) is None

    def test_revoke_all_for_membership(self, session):
        org = _make_org(session)
        user = _make_user(session)
        mem = _make_membership(session, org=org, user=user)
        bound = sess_mod.create_session(
            session, user_id=user.id, membership_id=mem.id, ip=None, user_agent=None
        )
        unbound = sess_mod.create_session(
            session, user_id=user.id, membership_id=None, ip=None, user_agent=None
        )
        revoked = sess_mod.revoke_all_for_membership(session, mem.id)
        assert revoked == 1
        assert sess_mod.lookup_session(session, bound.id) is None
        # The unbound session lives — it doesn't belong to this membership.
        assert sess_mod.lookup_session(session, unbound.id) is unbound


# ---------------------------------------------------------------------------
# CSRF tokens
# ---------------------------------------------------------------------------


class TestCSRF:
    def test_roundtrip(self):
        token = issue_csrf_token("session-abc")
        assert validate_csrf_token(token, "session-abc") is True

    def test_token_for_other_session_rejected(self):
        token = issue_csrf_token("session-abc")
        assert validate_csrf_token(token, "session-xyz") is False

    def test_anonymous_token_works_for_login_page(self):
        token = issue_csrf_token(None)
        assert validate_csrf_token(token, None) is True

    def test_empty_token_rejected(self):
        assert validate_csrf_token("", "session-abc") is False

    def test_garbled_token_rejected(self):
        assert validate_csrf_token("not-a-real-token", "session-abc") is False


# ---------------------------------------------------------------------------
# OIDC state token
# ---------------------------------------------------------------------------


class TestOIDCState:
    def test_state_roundtrip(self):
        payload = {"provider": "google", "intent": "login", "next": "/"}
        token = issue_oidc_state(payload)
        assert validate_oidc_state(token) == payload

    def test_invalid_state_rejected(self):
        assert validate_oidc_state("not-a-state-token") is None

    def test_empty_state_rejected(self):
        assert validate_oidc_state("") is None


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_under_limit_allows(self):
        rl = InProcessLimiter(limit=3, window_seconds=60)
        for _ in range(3):
            assert rl.check("auth", "1.2.3.4") is True

    def test_over_limit_blocks(self):
        rl = InProcessLimiter(limit=3, window_seconds=60)
        for _ in range(3):
            rl.check("auth", "1.2.3.4")
        assert rl.check("auth", "1.2.3.4") is False

    def test_separate_keys_isolated(self):
        rl = InProcessLimiter(limit=2, window_seconds=60)
        rl.check("auth", "1.1.1.1")
        rl.check("auth", "1.1.1.1")
        # Different IP — fresh budget.
        assert rl.check("auth", "2.2.2.2") is True

    def test_separate_scopes_isolated(self):
        rl = InProcessLimiter(limit=2, window_seconds=60)
        rl.check("auth", "1.1.1.1")
        rl.check("auth", "1.1.1.1")
        assert rl.check("invite_accept", "1.1.1.1") is True


# ---------------------------------------------------------------------------
# Invite issuance + redemption
# ---------------------------------------------------------------------------


class TestInvites:
    def test_create_returns_raw_token_only_once(self, session):
        org = _make_org(session)
        issued = invites_mod.create_invite(
            session,
            org_id=org.id,
            intended_email="bob@example.com",
            created_by_user_id=None,
        )
        # Raw token returned to the caller; only the hash is in the DB.
        assert issued.raw_token
        invite = session.get(M.OrgInvite, issued.invite_id)
        assert invite.token_hash != issued.raw_token
        assert invite.token_hash == invites_mod.hash_invite_token(issued.raw_token)
        assert invite.intended_email == "bob@example.com"

    def test_create_lowercases_intended_email(self, session):
        org = _make_org(session)
        issued = invites_mod.create_invite(
            session,
            org_id=org.id,
            intended_email="Bob@Example.COM",
            created_by_user_id=None,
        )
        invite = session.get(M.OrgInvite, issued.invite_id)
        assert invite.intended_email == "bob@example.com"

    def test_create_rejects_ttl_below_one(self, session):
        org = _make_org(session)
        with pytest.raises(ValueError):
            invites_mod.create_invite(
                session,
                org_id=org.id,
                intended_email="bob@example.com",
                created_by_user_id=None,
                ttl_days=0,
            )

    def test_create_rejects_ttl_over_cap(self, session):
        org = _make_org(session)
        with pytest.raises(ValueError):
            invites_mod.create_invite(
                session,
                org_id=org.id,
                intended_email="bob@example.com",
                created_by_user_id=None,
                ttl_days=invites_mod.MAX_INVITE_TTL_DAYS + 1,
            )

    def test_create_rejects_invalid_email(self, session):
        org = _make_org(session)
        for bad in ("", "no-at-sign", "  "):
            with pytest.raises(ValueError):
                invites_mod.create_invite(
                    session,
                    org_id=org.id,
                    intended_email=bad,
                    created_by_user_id=None,
                )

    def test_token_hash_is_deterministic(self):
        a = invites_mod.hash_invite_token("abc")
        b = invites_mod.hash_invite_token("abc")
        c = invites_mod.hash_invite_token("abd")
        assert a == b
        assert a != c
        # SHA-256 hex digests are 64 characters.
        assert len(a) == 64

    def test_default_ttl_within_cap(self, session):
        org = _make_org(session)
        issued = invites_mod.create_invite(
            session,
            org_id=org.id,
            intended_email="x@y.com",
            created_by_user_id=None,
        )
        delta = issued.expires_at - datetime.now(timezone.utc).replace(tzinfo=None)
        assert delta <= timedelta(days=invites_mod.MAX_INVITE_TTL_DAYS)
        assert delta > timedelta(days=invites_mod.DEFAULT_INVITE_TTL_DAYS - 1)

    def test_create_stores_personnel_fields(self, session):
        org = _make_org(session)
        issued = invites_mod.create_invite(
            session,
            org_id=org.id,
            intended_email="bob@example.com",
            created_by_user_id=None,
            first_name="Bob",
            last_name="Smith",
            rate="BM3",
            paygrade="E-4",
        )
        invite = session.get(M.OrgInvite, issued.invite_id)
        assert invite.first_name == "Bob"
        assert invite.last_name == "Smith"
        assert invite.rate == "BM3"
        assert invite.paygrade == "E-4"

    def test_create_personnel_fields_optional(self, session):
        org = _make_org(session)
        issued = invites_mod.create_invite(
            session,
            org_id=org.id,
            intended_email="bob@example.com",
            created_by_user_id=None,
        )
        invite = session.get(M.OrgInvite, issued.invite_id)
        assert invite.first_name is None
        assert invite.last_name is None
        assert invite.rate is None
        assert invite.paygrade is None


# ---------------------------------------------------------------------------
# Random tokens
# ---------------------------------------------------------------------------


class TestRandomToken:
    def test_returns_url_safe_chars(self):
        for _ in range(20):
            t = random_token()
            assert all(c.isalnum() or c in ("-", "_") for c in t), (
                f"non-url-safe char in {t!r}"
            )

    def test_distinct_each_call(self):
        seen = {random_token() for _ in range(200)}
        assert len(seen) == 200
