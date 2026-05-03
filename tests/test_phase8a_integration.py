"""Integration tests for Phase 8a — gate behaviour, enrollment redirect.

Tests the wiring between the step-up gate and FastAPI dependencies,
plus the SessionMiddleware enrollment-redirect helper. We exercise
the dependency function directly with a hand-built ``Request`` object
to avoid spinning up the full FastAPI test client and a real session
cookie just to verify a one-line gate.
"""

from __future__ import annotations

import os
import secrets
import uuid
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

os.environ.setdefault("TBDTASK_INSECURE_LOCAL_COOKIES", "1")

from app import models as M
from app.auth import step_up as step_up_mod
from app.auth.sessions import SESSION_COOKIE_NAME
from app.middleware import _passkey_enrollment_redirect


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(session, email="alice@example.com"):
    u = M.UserAccount(email=email, display_name="Alice")
    session.add(u)
    session.flush()
    return u


def _make_session(session, *, user):
    s = M.UserSession(id=secrets.token_urlsafe(16), user_id=user.id)
    session.add(s)
    session.flush()
    return s


def _make_credential(session, *, user, prf_supported=True, revoked=False):
    cred = M.UserWebauthnCredential(
        id=str(uuid.uuid4()),
        user_id=user.id,
        credential_id=secrets.token_bytes(32),
        public_key=secrets.token_bytes(64),
        sign_count=0,
        prf_supported=prf_supported,
        revoked_at=datetime.utcnow() if revoked else None,
    )
    session.add(cred)
    session.flush()
    return cred


def _fake_request(*, session_cookie):
    """Minimal stand-in for fastapi.Request used by ``require_step_up``.

    The dependency only reads ``request.cookies``, ``request.url``, and
    ``request.headers``, so a SimpleNamespace gets us there.
    """
    return SimpleNamespace(
        cookies={SESSION_COOKIE_NAME: session_cookie} if session_cookie else {},
        url=SimpleNamespace(__str__=lambda self: "/admin/members"),
        headers={"user-agent": "tests"},
        client=SimpleNamespace(host="127.0.0.1"),
    )


# ---------------------------------------------------------------------------
# Step-up gate
# ---------------------------------------------------------------------------


class TestRequireStepUpDependency:
    def test_no_op_when_feature_disabled(self, session, monkeypatch):
        """With TBDTASK_WEBAUTHN_ENABLED unset, the gate is a pass-through."""
        monkeypatch.delenv("TBDTASK_WEBAUTHN_ENABLED", raising=False)
        user = _make_user(session)
        sess = _make_session(session, user=user)
        # Keep the SessionLocal pointed at our test session
        from app.db import SessionLocal as RealSessionLocal  # noqa: F401
        from app.auth import step_up as su

        monkeypatch.setattr(
            su,
            "SessionLocal",
            lambda: _SessionWrapper(session),
        )
        dep = step_up_mod.require_step_up("admin_grant").dependency
        # Should not raise
        assert dep(_fake_request(session_cookie=sess.id)) is None

    def test_rejects_when_enabled_and_no_grant(self, session, monkeypatch):
        monkeypatch.setenv("TBDTASK_WEBAUTHN_ENABLED", "1")
        user = _make_user(session)
        sess = _make_session(session, user=user)
        from app.auth import step_up as su

        monkeypatch.setattr(su, "SessionLocal", lambda: _SessionWrapper(session))
        dep = step_up_mod.require_step_up("admin_grant").dependency
        with pytest.raises(step_up_mod.StepUpRequired) as exc_info:
            dep(_fake_request(session_cookie=sess.id))
        assert exc_info.value.purpose == "admin_grant"
        assert exc_info.value.status_code == 403

    def test_accepts_when_grant_present(self, session, monkeypatch):
        monkeypatch.setenv("TBDTASK_WEBAUTHN_ENABLED", "1")
        user = _make_user(session)
        sess = _make_session(session, user=user)
        cred = _make_credential(session, user=user)
        step_up_mod.grant_step_up(
            session,
            session_id=sess.id,
            credential_id=cred.id,
            purpose="admin_grant",
        )
        from app.auth import step_up as su

        monkeypatch.setattr(su, "SessionLocal", lambda: _SessionWrapper(session))
        dep = step_up_mod.require_step_up("admin_grant").dependency
        # Should not raise
        assert dep(_fake_request(session_cookie=sess.id)) is None

    def test_single_use_grant_consumed_on_first_pass(self, session, monkeypatch):
        monkeypatch.setenv("TBDTASK_WEBAUTHN_ENABLED", "1")
        user = _make_user(session)
        sess = _make_session(session, user=user)
        cred = _make_credential(session, user=user)
        step_up_mod.grant_step_up(
            session,
            session_id=sess.id,
            credential_id=cred.id,
            purpose="admin_grant",
        )
        from app.auth import step_up as su

        monkeypatch.setattr(su, "SessionLocal", lambda: _SessionWrapper(session))
        dep = step_up_mod.require_step_up("admin_grant").dependency
        # First pass succeeds and consumes
        dep(_fake_request(session_cookie=sess.id))
        # Second pass: grant is consumed, gate rejects
        with pytest.raises(step_up_mod.StepUpRequired):
            dep(_fake_request(session_cookie=sess.id))

    def test_purpose_isolation(self, session, monkeypatch):
        monkeypatch.setenv("TBDTASK_WEBAUTHN_ENABLED", "1")
        user = _make_user(session)
        sess = _make_session(session, user=user)
        cred = _make_credential(session, user=user)
        step_up_mod.grant_step_up(
            session, session_id=sess.id, credential_id=cred.id, purpose="export"
        )
        from app.auth import step_up as su

        monkeypatch.setattr(su, "SessionLocal", lambda: _SessionWrapper(session))
        dep = step_up_mod.require_step_up("admin_grant").dependency
        with pytest.raises(step_up_mod.StepUpRequired):
            dep(_fake_request(session_cookie=sess.id))

    def test_no_session_cookie_redirects(self, session, monkeypatch):
        monkeypatch.setenv("TBDTASK_WEBAUTHN_ENABLED", "1")
        from app.auth import step_up as su

        monkeypatch.setattr(su, "SessionLocal", lambda: _SessionWrapper(session))
        dep = step_up_mod.require_step_up("admin_grant").dependency
        with pytest.raises(step_up_mod.StepUpRequired):
            dep(_fake_request(session_cookie=None))

    def test_failed_gate_emits_audit_event(self, session, monkeypatch):
        monkeypatch.setenv("TBDTASK_WEBAUTHN_ENABLED", "1")
        user = _make_user(session)
        sess = _make_session(session, user=user)
        from app.auth import step_up as su

        monkeypatch.setattr(su, "SessionLocal", lambda: _SessionWrapper(session))
        dep = step_up_mod.require_step_up("admin_grant").dependency
        with pytest.raises(step_up_mod.StepUpRequired):
            dep(_fake_request(session_cookie=sess.id))
        # Re-fetch from the underlying session
        events = (
            session.query(M.AuthEvent)
            .filter_by(kind="step_up_failed", session_id=sess.id)
            .all()
        )
        assert events
        assert events[0].detail["purpose"] == "admin_grant"


# ---------------------------------------------------------------------------
# Enrollment redirect middleware helper
# ---------------------------------------------------------------------------


class TestEnrollmentRedirect:
    def test_no_op_when_feature_disabled(self, session, monkeypatch):
        monkeypatch.delenv("TBDTASK_WEBAUTHN_ENABLED", raising=False)
        user = _make_user(session)
        req = _fake_redirect_request("/admin/members")
        assert _passkey_enrollment_redirect(session, req, user) is None

    def test_no_op_when_enforced_after_unset(self, session, monkeypatch):
        monkeypatch.setenv("TBDTASK_WEBAUTHN_ENABLED", "1")
        monkeypatch.delenv("TBDTASK_WEBAUTHN_ENFORCED_AFTER", raising=False)
        user = _make_user(session)
        req = _fake_redirect_request("/admin/members")
        assert _passkey_enrollment_redirect(session, req, user) is None

    def test_no_op_before_cutoff(self, session, monkeypatch):
        monkeypatch.setenv("TBDTASK_WEBAUTHN_ENABLED", "1")
        future = (date.today() + timedelta(days=30)).isoformat()
        monkeypatch.setenv("TBDTASK_WEBAUTHN_ENFORCED_AFTER", future)
        user = _make_user(session)
        req = _fake_redirect_request("/admin/members")
        assert _passkey_enrollment_redirect(session, req, user) is None

    def test_redirects_after_cutoff_when_no_credential(self, session, monkeypatch):
        monkeypatch.setenv("TBDTASK_WEBAUTHN_ENABLED", "1")
        past = (date.today() - timedelta(days=1)).isoformat()
        monkeypatch.setenv("TBDTASK_WEBAUTHN_ENFORCED_AFTER", past)
        user = _make_user(session)
        req = _fake_redirect_request("/admin/members")
        resp = _passkey_enrollment_redirect(session, req, user)
        assert resp is not None
        assert resp.status_code == 303
        assert "/passkey/register" in resp.headers["Location"]
        assert "next=" in resp.headers["Location"]

    def test_no_redirect_after_cutoff_when_credential_exists(
        self, session, monkeypatch
    ):
        monkeypatch.setenv("TBDTASK_WEBAUTHN_ENABLED", "1")
        past = (date.today() - timedelta(days=1)).isoformat()
        monkeypatch.setenv("TBDTASK_WEBAUTHN_ENFORCED_AFTER", past)
        user = _make_user(session)
        _make_credential(session, user=user)
        req = _fake_redirect_request("/admin/members")
        assert _passkey_enrollment_redirect(session, req, user) is None

    @pytest.mark.parametrize(
        "exempt_path",
        [
            "/passkey/register",
            "/passkey/manage",
            "/step-up",
            "/auth/google/login",
            "/login",
            "/orgs/select",
            "/no-orgs",
            "/healthz",
            "/static/app.css",
        ],
    )
    def test_exempt_paths_not_redirected(self, session, monkeypatch, exempt_path):
        monkeypatch.setenv("TBDTASK_WEBAUTHN_ENABLED", "1")
        past = (date.today() - timedelta(days=1)).isoformat()
        monkeypatch.setenv("TBDTASK_WEBAUTHN_ENFORCED_AFTER", past)
        user = _make_user(session)
        req = _fake_redirect_request(exempt_path)
        assert _passkey_enrollment_redirect(session, req, user) is None

    def test_invalid_cutoff_format_is_no_op(self, session, monkeypatch):
        monkeypatch.setenv("TBDTASK_WEBAUTHN_ENABLED", "1")
        monkeypatch.setenv("TBDTASK_WEBAUTHN_ENFORCED_AFTER", "garbage")
        user = _make_user(session)
        req = _fake_redirect_request("/admin/members")
        # Bad format should fail closed (no-op), not raise.
        assert _passkey_enrollment_redirect(session, req, user) is None

    def test_no_redirect_for_anonymous_user(self, session, monkeypatch):
        monkeypatch.setenv("TBDTASK_WEBAUTHN_ENABLED", "1")
        past = (date.today() - timedelta(days=1)).isoformat()
        monkeypatch.setenv("TBDTASK_WEBAUTHN_ENFORCED_AFTER", past)
        req = _fake_redirect_request("/admin/members")
        assert _passkey_enrollment_redirect(session, req, None) is None


# ---------------------------------------------------------------------------
# Session wrapper for require_step_up tests
# ---------------------------------------------------------------------------


class _SessionWrapper:
    """Yield the wrapped session as a context-manager target.

    ``require_step_up`` does ``with SessionLocal() as db:`` — the
    ``SessionLocal`` callable normally returns a fresh session, but in
    these tests we need it to reuse the test fixture's session so the
    grants we just inserted are visible.
    """

    def __init__(self, real_session):
        self._real = real_session

    def __enter__(self):
        return self._real

    def __exit__(self, exc_type, exc, tb):
        # Don't close — the test fixture owns the session.
        if exc is None:
            try:
                self._real.commit()
            except Exception:
                self._real.rollback()
                raise
        else:
            self._real.rollback()
        return False


def _fake_redirect_request(path: str):
    return SimpleNamespace(
        url=SimpleNamespace(path=path, query=""),
    )
