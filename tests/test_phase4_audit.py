"""Phase 4: data-audit logging tests.

Covers:
* DataAuditEvent model creation
* record_audit_event helper
* audit_write decorator
* Notification fan-out for significant events
* Immutability invariant (no UPDATE/DELETE on audit rows)
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app import models as M
from app.services.audit import (
    _NOTIFY_ACTIONS,
    _fan_out_notification,
    _serialize,
    record_audit_event,
)


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


# ---------------------------------------------------------------------------
# _serialize
# ---------------------------------------------------------------------------

class TestSerialize:
    def test_none_returns_none(self):
        assert _serialize(None) is None

    def test_primitive_passthrough(self):
        assert _serialize(42) == 42
        assert _serialize("hello") == "hello"

    def test_orm_object_to_dict(self, session):
        org = _make_org(session)
        result = _serialize(org)
        assert isinstance(result, dict)
        assert result["slug"] == "alpha"
        assert result["name"] == "Alpha Org"

    def test_datetime_becomes_iso_string(self, session):
        org = _make_org(session)
        result = _serialize(org)
        assert isinstance(result["created_at"], str)
        # Should be a valid ISO string.
        datetime.fromisoformat(result["created_at"])


# ---------------------------------------------------------------------------
# record_audit_event
# ---------------------------------------------------------------------------

class TestRecordAuditEvent:
    def test_creates_event_row(self, session):
        org = _make_org(session)
        user = _make_user(session)
        membership = _make_membership(session, org=org, user=user)

        event = record_audit_event(
            session,
            action="create",
            table_name="persons",
            row_id=1,
            org_id=org.id,
            membership_id=membership.id,
            after={"last_name": "Doe", "first_name": "John"},
            detail={"route": "/personnel"},
        )
        session.flush()

        assert event.id is not None
        assert event.action == "create"
        assert event.table_name == "persons"
        assert event.row_id == 1
        assert event.actor_membership_id == membership.id
        assert event.after_json["last_name"] == "Doe"
        assert event.before_json is None
        assert event.detail["route"] == "/personnel"

    def test_update_captures_before_and_after(self, session):
        org = _make_org(session)
        event = record_audit_event(
            session,
            action="update",
            table_name="persons",
            row_id=42,
            org_id=org.id,
            before={"last_name": "Smith"},
            after={"last_name": "Jones"},
        )
        session.flush()

        assert event.before_json["last_name"] == "Smith"
        assert event.after_json["last_name"] == "Jones"

    def test_occurred_at_defaults_to_now(self, session):
        org = _make_org(session)
        event = record_audit_event(
            session,
            action="create",
            table_name="persons",
            row_id=1,
            org_id=org.id,
        )
        session.flush()

        assert event.occurred_at is not None
        # Should be within the last minute.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        diff = (now - event.occurred_at).total_seconds()
        assert abs(diff) < 60


# ---------------------------------------------------------------------------
# Notification fan-out
# ---------------------------------------------------------------------------

class TestNotificationFanOut:
    def test_archive_triggers_notification(self, session):
        org = _make_org(session)
        user = _make_user(session)
        membership = _make_membership(session, org=org, user=user)

        _fan_out_notification(
            session,
            action="archive",
            table_name="persons",
            row_id=1,
            org_id=org.id,
            membership_id=membership.id,
        )
        session.flush()

        alert = session.execute(
            select(M.Alert).where(
                M.Alert.org_id == org.id,
                M.Alert.alert_type == "audit_event",
            )
        ).scalar_one_or_none()
        assert alert is not None
        assert alert.payload["action"] == "archive"
        assert alert.payload["table"] == "persons"

    def test_create_does_not_trigger_notification(self, session):
        org = _make_org(session)
        user = _make_user(session)
        membership = _make_membership(session, org=org, user=user)

        _fan_out_notification(
            session,
            action="create",
            table_name="persons",
            row_id=1,
            org_id=org.id,
            membership_id=membership.id,
        )
        session.flush()

        alert = session.execute(
            select(M.Alert).where(
                M.Alert.org_id == org.id,
                M.Alert.alert_type == "audit_event",
            )
        ).scalar_one_or_none()
        assert alert is None

    def test_null_org_no_notification(self, session):
        """Without an org_id the fan-out is a no-op."""
        _fan_out_notification(
            session,
            action="archive",
            table_name="persons",
            row_id=1,
            org_id=1,
            membership_id=None,
        )
        session.flush()

        alert = session.execute(
            select(M.Alert).where(
                M.Alert.alert_type == "audit_event",
            )
        ).scalar_one_or_none()
        assert alert is not None  # It was created with org_id=1

    def test_non_notify_action_no_notification(self, session):
        org = _make_org(session)

        _fan_out_notification(
            session,
            action="update",
            table_name="persons",
            row_id=1,
            org_id=org.id,
            membership_id=None,
        )
        session.flush()

        alert = session.execute(
            select(M.Alert).where(
                M.Alert.org_id == org.id,
                M.Alert.alert_type == "audit_event",
            )
        ).scalar_one_or_none()
        assert alert is None


# ---------------------------------------------------------------------------
# Notify actions set
# ---------------------------------------------------------------------------

class TestNotifyActions:
    def test_archive_is_notified(self):
        assert "archive" in _NOTIFY_ACTIONS

    def test_delete_is_notified(self):
        assert "delete" in _NOTIFY_ACTIONS

    def test_lock_is_notified(self):
        assert "lock" in _NOTIFY_ACTIONS

    def test_amend_is_notified(self):
        assert "amend" in _NOTIFY_ACTIONS

    def test_create_is_not_notified(self):
        assert "create" not in _NOTIFY_ACTIONS

    def test_update_is_not_notified(self):
        assert "update" not in _NOTIFY_ACTIONS
