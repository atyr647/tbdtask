"""Unit tests for Phase 8a — WebAuthn registration, assertion, and step-up.

The webauthn library's signature/attestation verification is exercised
upstream; here we test our own bookkeeping: challenge issuance and
expiry, audit event emission, the PRF-support boolean flow, the
single-use grant lifecycle, and the FastAPI gate behavior.

Where verification calls are needed for the *happy path*, we
monkey-patch the library's verify functions to return a fake
``VerifiedRegistration`` / ``VerifiedAuthentication`` so the rest of
the flow can be exercised without a real authenticator.
"""

from __future__ import annotations

import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

# Local-cookie mode so webauthn.rp_id() doesn't raise during module
# import in tests that don't otherwise set TBDTASK_WEBAUTHN_RP_ID.
os.environ.setdefault("TBDTASK_INSECURE_LOCAL_COOKIES", "1")

from app import models as M
from app.auth import step_up, webauthn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(session, email="alice@example.com", display_name="Alice"):
    u = M.UserAccount(email=email, display_name=display_name)
    session.add(u)
    session.flush()
    return u


def _make_session(session, *, user, current_membership_id=None):
    s = M.UserSession(
        id=secrets.token_urlsafe(16),
        user_id=user.id,
        current_membership_id=current_membership_id,
    )
    session.add(s)
    session.flush()
    return s


def _make_credential(
    session, *, user, prf_supported=True, revoked=False, nickname=None
):
    cred = M.UserWebauthnCredential(
        id=str(uuid.uuid4()),
        user_id=user.id,
        credential_id=secrets.token_bytes(32),
        public_key=secrets.token_bytes(64),
        sign_count=0,
        aaguid=str(uuid.uuid4()),
        transports=["internal"],
        backup_eligible=False,
        backup_state=False,
        prf_supported=prf_supported,
        nickname=nickname,
        revoked_at=datetime.utcnow() if revoked else None,
    )
    session.add(cred)
    session.flush()
    return cred


@dataclass
class FakeVerifiedRegistration:
    credential_id: bytes
    credential_public_key: bytes
    sign_count: int
    aaguid: object  # the lib uses uuid.UUID; mimic the str cast
    credential_backed_up: bool = False
    credential_device_type: str = "single_device"


@dataclass
class FakeVerifiedAuthentication:
    new_sign_count: int


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestConfiguration:
    def test_rp_id_falls_back_to_localhost_in_dev(self):
        with patch.dict(
            os.environ, {"TBDTASK_INSECURE_LOCAL_COOKIES": "1"}, clear=False
        ):
            os.environ.pop("TBDTASK_WEBAUTHN_RP_ID", None)
            assert webauthn.rp_id() == "localhost"

    def test_rp_id_uses_explicit_env(self):
        with patch.dict(
            os.environ, {"TBDTASK_WEBAUTHN_RP_ID": "tbdtask.example.com"}, clear=False
        ):
            assert webauthn.rp_id() == "tbdtask.example.com"

    def test_rp_id_required_in_production(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TBDTASK_WEBAUTHN_RP_ID", None)
            os.environ.pop("TBDTASK_INSECURE_LOCAL_COOKIES", None)
            with pytest.raises(RuntimeError):
                webauthn.rp_id()
            # Restore
            os.environ["TBDTASK_INSECURE_LOCAL_COOKIES"] = "1"

    def test_is_enabled_default_false(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TBDTASK_WEBAUTHN_ENABLED", None)
            assert webauthn.is_enabled() is False

    def test_is_enabled_when_set(self):
        with patch.dict(os.environ, {"TBDTASK_WEBAUTHN_ENABLED": "1"}, clear=False):
            assert webauthn.is_enabled() is True

    def test_origins_includes_dev_aliases_for_localhost(self):
        with patch.dict(
            os.environ, {"TBDTASK_INSECURE_LOCAL_COOKIES": "1"}, clear=False
        ):
            os.environ.pop("TBDTASK_WEBAUTHN_RP_ID", None)
            os.environ.pop("TBDTASK_WEBAUTHN_ORIGINS", None)
            origs = webauthn.expected_origins()
            assert "http://localhost:8765" in origs
            assert "http://127.0.0.1:8765" in origs


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_begin_registration_returns_challenge(self, session):
        user = _make_user(session)
        ch = webauthn.begin_registration(session, user=user)
        assert len(ch.challenge) == 32
        assert ch.user_handle == user.id.to_bytes(16, "big")
        assert ch.expires_at > datetime.utcnow()
        # PRF extension is requested in the options blob
        assert "prf" in ch.options_json

    def test_begin_registration_excludes_existing_credentials(self, session):
        user = _make_user(session)
        existing = _make_credential(session, user=user)
        ch = webauthn.begin_registration(session, user=user)
        # The library serialises credential ids as base64url; just assert
        # the options blob references our credential's bytes in some form
        from webauthn.helpers import bytes_to_base64url

        assert bytes_to_base64url(existing.credential_id) in ch.options_json

    def test_finish_registration_persists_credential(self, session, monkeypatch):
        user = _make_user(session)
        ch = webauthn.begin_registration(session, user=user)
        cred_bytes = secrets.token_bytes(32)
        pubkey = secrets.token_bytes(80)
        verified = FakeVerifiedRegistration(
            credential_id=cred_bytes,
            credential_public_key=pubkey,
            sign_count=0,
            aaguid=uuid.uuid4(),
        )
        monkeypatch.setattr(
            webauthn, "verify_registration_response", lambda **kw: verified
        )

        result = webauthn.finish_registration(
            session,
            user=user,
            client_response={"clientExtensionResults": {"prf": {"enabled": True}}},
            challenge=ch,
            nickname="iPhone 15",
        )
        assert result.prf_supported is True
        assert result.credential_id == cred_bytes
        rows = session.query(M.UserWebauthnCredential).filter_by(user_id=user.id).all()
        assert len(rows) == 1
        assert rows[0].credential_id == cred_bytes
        assert rows[0].nickname == "iPhone 15"
        assert rows[0].prf_supported is True

    def test_finish_registration_records_no_prf_when_authenticator_lacks_extension(
        self, session, monkeypatch
    ):
        user = _make_user(session)
        ch = webauthn.begin_registration(session, user=user)
        verified = FakeVerifiedRegistration(
            credential_id=secrets.token_bytes(32),
            credential_public_key=secrets.token_bytes(80),
            sign_count=0,
            aaguid=uuid.uuid4(),
        )
        monkeypatch.setattr(
            webauthn, "verify_registration_response", lambda **kw: verified
        )
        webauthn.finish_registration(
            session,
            user=user,
            client_response={"clientExtensionResults": {}},
            challenge=ch,
        )
        cred = (
            session.query(M.UserWebauthnCredential).filter_by(user_id=user.id).first()
        )
        assert cred.prf_supported is False

    def test_finish_registration_rejects_expired_challenge(self, session):
        user = _make_user(session)
        ch = webauthn.RegistrationChallenge(
            challenge=secrets.token_bytes(32),
            user_handle=user.id.to_bytes(16, "big"),
            rp_id="localhost",
            expires_at=datetime.utcnow() - timedelta(seconds=1),
            options_json="{}",
        )
        with pytest.raises(webauthn.ChallengeExpired):
            webauthn.finish_registration(
                session, user=user, client_response={}, challenge=ch
            )
        # Audit event recorded the failure
        events = (
            session.query(M.AuthEvent)
            .filter_by(user_id=user.id, kind="webauthn_register_failed")
            .all()
        )
        assert len(events) == 1
        assert events[0].detail["reason"] == "challenge_expired"

    def test_finish_registration_rejects_verification_failure(
        self, session, monkeypatch
    ):
        user = _make_user(session)
        ch = webauthn.begin_registration(session, user=user)

        def boom(**_kw):
            raise ValueError("attestation invalid")

        monkeypatch.setattr(webauthn, "verify_registration_response", boom)
        with pytest.raises(webauthn.WebAuthnVerificationError):
            webauthn.finish_registration(
                session, user=user, client_response={}, challenge=ch
            )
        events = (
            session.query(M.AuthEvent)
            .filter_by(user_id=user.id, kind="webauthn_register_failed")
            .all()
        )
        assert events
        assert events[0].detail["reason"] == "verification_failed"

    def test_finish_registration_emits_audit_event(self, session, monkeypatch):
        user = _make_user(session)
        ch = webauthn.begin_registration(session, user=user)
        verified = FakeVerifiedRegistration(
            credential_id=secrets.token_bytes(32),
            credential_public_key=secrets.token_bytes(80),
            sign_count=0,
            aaguid=uuid.uuid4(),
        )
        monkeypatch.setattr(
            webauthn, "verify_registration_response", lambda **kw: verified
        )
        webauthn.finish_registration(
            session,
            user=user,
            client_response={"clientExtensionResults": {"prf": {"enabled": True}}},
            challenge=ch,
        )
        events = (
            session.query(M.AuthEvent)
            .filter_by(user_id=user.id, kind="webauthn_register")
            .all()
        )
        assert len(events) == 1
        assert events[0].detail["prf_supported"] is True


# ---------------------------------------------------------------------------
# Assertion
# ---------------------------------------------------------------------------


class TestAssertion:
    def test_begin_assertion_returns_challenge(self, session):
        user = _make_user(session)
        _make_credential(session, user=user)
        ch = webauthn.begin_assertion(session, user=user, purpose="admin_grant")
        assert len(ch.challenge) == 32
        assert ch.purpose == "admin_grant"

    def test_begin_assertion_rejects_user_with_no_credentials(self, session):
        user = _make_user(session)
        with pytest.raises(webauthn.WebAuthnError):
            webauthn.begin_assertion(session, user=user, purpose="admin_grant")

    def test_begin_assertion_skips_revoked_credentials(self, session):
        user = _make_user(session)
        _make_credential(session, user=user, revoked=True)
        with pytest.raises(webauthn.WebAuthnError):
            webauthn.begin_assertion(session, user=user, purpose="admin_grant")

    def test_finish_assertion_returns_credential_uuid(self, session, monkeypatch):
        user = _make_user(session)
        cred = _make_credential(session, user=user)
        ch = webauthn.begin_assertion(session, user=user, purpose="admin_grant")
        monkeypatch.setattr(
            webauthn,
            "verify_authentication_response",
            lambda **kw: FakeVerifiedAuthentication(new_sign_count=42),
        )

        from webauthn.helpers import bytes_to_base64url

        client_response = {"rawId": bytes_to_base64url(cred.credential_id)}
        result = webauthn.finish_assertion(
            session, user=user, client_response=client_response, challenge=ch
        )
        assert result == cred.id
        session.refresh(cred)
        assert cred.sign_count == 42
        assert cred.last_used_at is not None

    def test_finish_assertion_unknown_credential_fails(self, session, monkeypatch):
        user = _make_user(session)
        _make_credential(session, user=user)
        ch = webauthn.begin_assertion(session, user=user, purpose="admin_grant")

        from webauthn.helpers import bytes_to_base64url

        client_response = {"rawId": bytes_to_base64url(secrets.token_bytes(32))}
        with pytest.raises(webauthn.WebAuthnVerificationError):
            webauthn.finish_assertion(
                session, user=user, client_response=client_response, challenge=ch
            )
        events = (
            session.query(M.AuthEvent)
            .filter_by(user_id=user.id, kind="webauthn_verify_failed")
            .all()
        )
        assert events
        assert events[0].detail["reason"] == "unknown_credential"

    def test_finish_assertion_rejects_expired_challenge(self, session):
        user = _make_user(session)
        cred = _make_credential(session, user=user)
        ch = webauthn.AssertionChallenge(
            challenge=secrets.token_bytes(32),
            rp_id="localhost",
            expires_at=datetime.utcnow() - timedelta(seconds=1),
            purpose="admin_grant",
            allow_credential_ids=[cred.credential_id],
            options_json="{}",
        )
        from webauthn.helpers import bytes_to_base64url

        with pytest.raises(webauthn.ChallengeExpired):
            webauthn.finish_assertion(
                session,
                user=user,
                client_response={"rawId": bytes_to_base64url(cred.credential_id)},
                challenge=ch,
            )

    def test_finish_assertion_emits_verify_audit_event(self, session, monkeypatch):
        user = _make_user(session)
        cred = _make_credential(session, user=user)
        ch = webauthn.begin_assertion(session, user=user, purpose="admin_grant")
        monkeypatch.setattr(
            webauthn,
            "verify_authentication_response",
            lambda **kw: FakeVerifiedAuthentication(new_sign_count=1),
        )
        from webauthn.helpers import bytes_to_base64url

        webauthn.finish_assertion(
            session,
            user=user,
            client_response={"rawId": bytes_to_base64url(cred.credential_id)},
            challenge=ch,
        )
        events = (
            session.query(M.AuthEvent)
            .filter_by(user_id=user.id, kind="webauthn_verify")
            .all()
        )
        assert len(events) == 1
        assert events[0].detail["purpose"] == "admin_grant"
        assert events[0].detail["credential_id"] == cred.id


# ---------------------------------------------------------------------------
# Credential management
# ---------------------------------------------------------------------------


class TestCredentialManagement:
    def test_revoke_credential_marks_row_revoked(self, session):
        user = _make_user(session)
        cred = _make_credential(session, user=user)
        webauthn.revoke_credential(session, user=user, credential_uuid=cred.id)
        session.refresh(cred)
        assert cred.revoked_at is not None

    def test_revoke_credential_emits_audit_event(self, session):
        user = _make_user(session)
        cred = _make_credential(session, user=user)
        webauthn.revoke_credential(session, user=user, credential_uuid=cred.id)
        events = (
            session.query(M.AuthEvent)
            .filter_by(user_id=user.id, kind="webauthn_revoke")
            .all()
        )
        assert len(events) == 1
        assert events[0].detail["credential_id"] == cred.id

    def test_revoke_credential_no_op_for_unknown_id(self, session):
        user = _make_user(session)
        # No credentials at all — shouldn't raise.
        webauthn.revoke_credential(
            session, user=user, credential_uuid=str(uuid.uuid4())
        )

    def test_has_active_credential(self, session):
        user = _make_user(session)
        assert webauthn.has_active_credential(session, user=user) is False
        _make_credential(session, user=user)
        assert webauthn.has_active_credential(session, user=user) is True

    def test_revoked_credential_does_not_count_as_active(self, session):
        user = _make_user(session)
        _make_credential(session, user=user, revoked=True)
        assert webauthn.has_active_credential(session, user=user) is False


# ---------------------------------------------------------------------------
# Step-up grants
# ---------------------------------------------------------------------------


class TestStepUpGrant:
    def test_grant_records_purpose_and_ttl(self, session):
        user = _make_user(session)
        sess = _make_session(session, user=user)
        cred = _make_credential(session, user=user)
        grant = step_up.grant_step_up(
            session,
            session_id=sess.id,
            credential_id=cred.id,
            purpose="admin_grant",
            ttl=timedelta(minutes=10),
        )
        assert grant.purpose == "admin_grant"
        assert grant.expires_at - grant.granted_at >= timedelta(minutes=9, seconds=55)

    def test_has_step_up_finds_fresh_grant(self, session):
        user = _make_user(session)
        sess = _make_session(session, user=user)
        cred = _make_credential(session, user=user)
        step_up.grant_step_up(
            session, session_id=sess.id, credential_id=cred.id, purpose="admin_grant"
        )
        assert step_up.has_step_up(session, session_id=sess.id, purpose="admin_grant")

    def test_has_step_up_rejects_expired_grant(self, session):
        user = _make_user(session)
        sess = _make_session(session, user=user)
        cred = _make_credential(session, user=user)
        step_up.grant_step_up(
            session,
            session_id=sess.id,
            credential_id=cred.id,
            purpose="admin_grant",
            ttl=timedelta(seconds=-1),
        )
        assert (
            step_up.has_step_up(session, session_id=sess.id, purpose="admin_grant")
            is False
        )

    def test_has_step_up_rejects_other_purpose(self, session):
        user = _make_user(session)
        sess = _make_session(session, user=user)
        cred = _make_credential(session, user=user)
        step_up.grant_step_up(
            session, session_id=sess.id, credential_id=cred.id, purpose="admin_grant"
        )
        assert (
            step_up.has_step_up(session, session_id=sess.id, purpose="export") is False
        )

    def test_consume_marks_grant_used(self, session):
        user = _make_user(session)
        sess = _make_session(session, user=user)
        cred = _make_credential(session, user=user)
        step_up.grant_step_up(
            session, session_id=sess.id, credential_id=cred.id, purpose="admin_grant"
        )
        assert step_up.has_step_up(
            session, session_id=sess.id, purpose="admin_grant", consume=True
        )
        # Second call returns False because the grant is consumed.
        assert (
            step_up.has_step_up(session, session_id=sess.id, purpose="admin_grant")
            is False
        )

    def test_revoke_grants_for_session_invalidates_all_purposes(self, session):
        user = _make_user(session)
        sess = _make_session(session, user=user)
        cred = _make_credential(session, user=user)
        step_up.grant_step_up(
            session, session_id=sess.id, credential_id=cred.id, purpose="admin_grant"
        )
        step_up.grant_step_up(
            session, session_id=sess.id, credential_id=cred.id, purpose="export"
        )
        n = step_up.revoke_grants_for_session(session, session_id=sess.id)
        assert n == 2
        assert (
            step_up.has_step_up(session, session_id=sess.id, purpose="admin_grant")
            is False
        )
        assert (
            step_up.has_step_up(session, session_id=sess.id, purpose="export") is False
        )

    def test_grant_does_not_satisfy_other_session(self, session):
        user_a = _make_user(session, email="a@example.com")
        user_b = _make_user(session, email="b@example.com")
        sess_a = _make_session(session, user=user_a)
        sess_b = _make_session(session, user=user_b)
        cred_a = _make_credential(session, user=user_a)
        step_up.grant_step_up(
            session,
            session_id=sess_a.id,
            credential_id=cred_a.id,
            purpose="admin_grant",
        )
        assert (
            step_up.has_step_up(session, session_id=sess_b.id, purpose="admin_grant")
            is False
        )
