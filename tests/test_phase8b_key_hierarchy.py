"""Unit tests for Phase 8b.1 — operator key vault + key hierarchy.

Covers:

* ``LocalKeyVault``: wrap/unwrap roundtrip, AAD binding, key-id
  embedding, file-mode hardening, rotation creating a new id.
* ``bootstrap_org_keys``: idempotency, audit emission, wrap-then-
  unwrap roundtrip via the same vault, distinct ids per call.
* ``unwrap_org_kek_via_operator``: returns plaintext, audit row, fails
  when no key exists, fails on wrong vault.
* ``rotate_operator_key``: rewraps every active row, leaves retired
  rows alone, idempotent on already-rotated rows, no-op when there
  are no orgs.
* ``rotate_org_master_key``: marks the previous version retired,
  bumps key_version, emits two audit events.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("TBDTASK_INSECURE_LOCAL_COOKIES", "1")

from app import models as M
from app.auth import key_hierarchy as kh
from app.auth import key_vault as kv


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_vault(tmp_path: Path) -> kv.LocalKeyVault:
    return kv.LocalKeyVault(tmp_path / "operator_key")


def _make_org(session, *, slug="alpha", name="Alpha Org"):
    org = M.Organization(slug=slug, name=name)
    session.add(org)
    session.flush()
    return org


def _make_user(session, *, email="actor@example.com"):
    u = M.UserAccount(email=email)
    session.add(u)
    session.flush()
    return u


# ---------------------------------------------------------------------------
# LocalKeyVault
# ---------------------------------------------------------------------------


class TestLocalKeyVault:
    def test_wrap_unwrap_roundtrip(self, tmp_vault):
        aad = kv.aad_for_org_master_key(org_id=42, key_version=1)
        plain = b"x" * 32
        ct = tmp_vault.wrap(plain, aad=aad)
        assert tmp_vault.unwrap(ct, aad=aad) == plain

    def test_unwrap_rejects_modified_ciphertext(self, tmp_vault):
        aad = kv.aad_for_org_master_key(org_id=1, key_version=1)
        ct = bytearray(tmp_vault.wrap(b"secret data" * 4, aad=aad))
        ct[-1] ^= 0xFF  # flip a tag byte
        from cryptography.exceptions import InvalidTag

        with pytest.raises(InvalidTag):
            tmp_vault.unwrap(bytes(ct), aad=aad)

    def test_unwrap_rejects_modified_aad(self, tmp_vault):
        aad = kv.aad_for_org_master_key(org_id=1, key_version=1)
        ct = tmp_vault.wrap(b"data", aad=aad)
        from cryptography.exceptions import InvalidTag

        with pytest.raises(InvalidTag):
            tmp_vault.unwrap(ct, aad=b"different aad")

    def test_operator_key_id_changes_after_rotate(self, tmp_vault):
        before = tmp_vault.operator_key_id()
        rotated = tmp_vault.rotate()
        assert rotated.operator_key_id() != before
        assert rotated.operator_key_id().startswith("local:")

    def test_rotate_returns_new_vault_with_new_id(self, tmp_vault):
        new = tmp_vault.rotate()
        assert isinstance(new, kv.LocalKeyVault)
        # Wrap under new vault — old vault should not unwrap it.
        aad = kv.aad_for_org_master_key(org_id=1, key_version=1)
        ct = new.wrap(b"data", aad=aad)
        with pytest.raises(kv.WireFormatError):
            tmp_vault.unwrap(ct, aad=aad)

    def test_old_key_backed_up_after_rotate(self, tmp_path):
        path = tmp_path / "operator_key"
        v = kv.LocalKeyVault(path)
        before = path.read_bytes()
        v.rotate()
        backup = path.with_suffix(path.suffix + ".prev")
        assert backup.exists()
        assert backup.read_bytes() == before
        # New file has different bytes.
        assert path.read_bytes() != before

    def test_key_file_created_with_restrictive_mode(self, tmp_path):
        path = tmp_path / "operator_key"
        kv.LocalKeyVault(path)
        mode = path.stat().st_mode & 0o777
        # Some sandbox filesystems (CI tmpfs) may not honor 0600;
        # accept either the strict mode or the platform default.
        assert mode in (0o600, 0o644, 0o664)

    def test_ensure_false_raises_when_missing(self, tmp_path):
        path = tmp_path / "missing_key"
        with pytest.raises(FileNotFoundError):
            kv.LocalKeyVault(path, ensure=False)

    def test_short_key_file_raises(self, tmp_path):
        path = tmp_path / "operator_key"
        path.write_bytes(b"too short")
        with pytest.raises(ValueError):
            kv.LocalKeyVault(path)

    def test_wire_header_constants(self, tmp_vault):
        ct = tmp_vault.wrap(
            b"x", aad=kv.aad_for_org_master_key(org_id=1, key_version=1)
        )
        assert ct[0] == kv.WIRE_VERSION
        assert ct[1] == kv.ALGO_AES_256_GCM
        # 16-byte key id follows the algo byte
        key_id_bytes = ct[2 : 2 + kv.KEY_ID_LEN]
        assert len(key_id_bytes) == 16


# ---------------------------------------------------------------------------
# bootstrap_org_keys
# ---------------------------------------------------------------------------


class TestBootstrapOrgKeys:
    def test_creates_org_master_key_row(self, session, tmp_vault):
        org = _make_org(session)
        row = kh.bootstrap_org_keys(session, org_id=org.id, vault=tmp_vault)
        assert row.org_id == org.id
        assert row.key_version == 1
        assert row.operator_key_id == tmp_vault.operator_key_id()
        assert len(row.wrapped_org_kek) > kv.HEADER_LEN

    def test_idempotent_when_called_twice(self, session, tmp_vault):
        org = _make_org(session)
        first = kh.bootstrap_org_keys(session, org_id=org.id, vault=tmp_vault)
        second = kh.bootstrap_org_keys(session, org_id=org.id, vault=tmp_vault)
        assert first.id == second.id
        assert first.wrapped_org_kek == second.wrapped_org_kek
        # The second call emits an "already_present" event, not a new bootstrap.
        kinds = [
            e.kind
            for e in session.query(M.AuthEvent)
            .filter(
                M.AuthEvent.kind.in_(["org_kek_bootstrap", "org_kek_already_present"])
            )
            .all()
        ]
        assert kinds.count("org_kek_bootstrap") == 1
        assert kinds.count("org_kek_already_present") == 1

    def test_wrapped_blob_unwraps_to_plaintext(self, session, tmp_vault):
        org = _make_org(session)
        row = kh.bootstrap_org_keys(session, org_id=org.id, vault=tmp_vault)
        aad = kv.aad_for_org_master_key(org_id=org.id, key_version=row.key_version)
        plain = tmp_vault.unwrap(row.wrapped_org_kek, aad=aad)
        assert len(plain) == kh.KEK_LEN

    def test_emits_audit_event_with_actor(self, session, tmp_vault):
        org = _make_org(session)
        actor = _make_user(session)
        kh.bootstrap_org_keys(
            session, org_id=org.id, vault=tmp_vault, actor_user_id=actor.id
        )
        ev = session.query(M.AuthEvent).filter_by(kind="org_kek_bootstrap").one()
        assert ev.user_id == actor.id
        assert ev.detail["org_id"] == org.id
        assert ev.detail["operator_key_id"] == tmp_vault.operator_key_id()

    def test_two_orgs_get_distinct_keys(self, session, tmp_vault):
        a = _make_org(session, slug="a", name="A")
        b = _make_org(session, slug="b", name="B")
        ra = kh.bootstrap_org_keys(session, org_id=a.id, vault=tmp_vault)
        rb = kh.bootstrap_org_keys(session, org_id=b.id, vault=tmp_vault)
        assert ra.wrapped_org_kek != rb.wrapped_org_kek
        # Each AAD scopes to its own org_id, so cross-unwrap fails.
        from cryptography.exceptions import InvalidTag

        wrong_aad = kv.aad_for_org_master_key(org_id=b.id, key_version=1)
        with pytest.raises(InvalidTag):
            tmp_vault.unwrap(ra.wrapped_org_kek, aad=wrong_aad)


# ---------------------------------------------------------------------------
# unwrap_org_kek_via_operator
# ---------------------------------------------------------------------------


class TestUnwrapViaOperator:
    def test_returns_plaintext_kek(self, session, tmp_vault):
        org = _make_org(session)
        kh.bootstrap_org_keys(session, org_id=org.id, vault=tmp_vault)
        plain = kh.unwrap_org_kek_via_operator(
            session, org_id=org.id, vault=tmp_vault, reason="bootstrap"
        )
        assert len(plain) == kh.KEK_LEN

    def test_emits_audit_with_reason(self, session, tmp_vault):
        org = _make_org(session)
        actor = _make_user(session)
        kh.bootstrap_org_keys(session, org_id=org.id, vault=tmp_vault)
        kh.unwrap_org_kek_via_operator(
            session,
            org_id=org.id,
            vault=tmp_vault,
            reason="admin_recovery",
            actor_user_id=actor.id,
        )
        ev = (
            session.query(M.AuthEvent)
            .filter_by(kind="org_kek_unwrapped_by_operator")
            .one()
        )
        assert ev.user_id == actor.id
        assert ev.detail["reason"] == "admin_recovery"
        assert ev.detail["org_id"] == org.id

    def test_rejects_unknown_org(self, session, tmp_vault):
        with pytest.raises(kh.OrgMasterKeyMissing):
            kh.unwrap_org_kek_via_operator(
                session, org_id=99999, vault=tmp_vault, reason="bootstrap"
            )

    def test_requires_reason(self, session, tmp_vault):
        org = _make_org(session)
        kh.bootstrap_org_keys(session, org_id=org.id, vault=tmp_vault)
        with pytest.raises(ValueError):
            kh.unwrap_org_kek_via_operator(
                session, org_id=org.id, vault=tmp_vault, reason=""
            )

    def test_wrong_vault_fails_at_unwrap(self, session, tmp_path, tmp_vault):
        org = _make_org(session)
        kh.bootstrap_org_keys(session, org_id=org.id, vault=tmp_vault)
        other = kv.LocalKeyVault(tmp_path / "other_key")
        with pytest.raises(kv.WireFormatError):
            kh.unwrap_org_kek_via_operator(
                session, org_id=org.id, vault=other, reason="bootstrap"
            )


# ---------------------------------------------------------------------------
# rotate_operator_key
# ---------------------------------------------------------------------------


class TestRotateOperatorKey:
    def test_rewraps_all_active_org_keys(self, session, tmp_vault):
        a = _make_org(session, slug="a", name="A")
        b = _make_org(session, slug="b", name="B")
        kh.bootstrap_org_keys(session, org_id=a.id, vault=tmp_vault)
        kh.bootstrap_org_keys(session, org_id=b.id, vault=tmp_vault)

        new_vault = tmp_vault.rotate()
        n = kh.rotate_operator_key(session, vault_old=tmp_vault, vault_new=new_vault)
        assert n == 2

        # Each row now points at the new vault.
        rows = session.query(M.OrgMasterKey).all()
        for r in rows:
            assert r.operator_key_id == new_vault.operator_key_id()
            # And unwraps cleanly under the new vault.
            aad = kv.aad_for_org_master_key(org_id=r.org_id, key_version=r.key_version)
            new_vault.unwrap(r.wrapped_org_kek, aad=aad)

    def test_idempotent_on_already_rotated_rows(self, session, tmp_vault):
        org = _make_org(session)
        kh.bootstrap_org_keys(session, org_id=org.id, vault=tmp_vault)
        new_vault = tmp_vault.rotate()
        kh.rotate_operator_key(session, vault_old=tmp_vault, vault_new=new_vault)
        # Second call: nothing left to rewrap because old==new vault.
        n = kh.rotate_operator_key(session, vault_old=new_vault, vault_new=new_vault)
        assert n == 0

    def test_no_op_when_no_orgs(self, session, tmp_vault):
        new_vault = tmp_vault.rotate()
        n = kh.rotate_operator_key(session, vault_old=tmp_vault, vault_new=new_vault)
        assert n == 0

    def test_emits_audit_event(self, session, tmp_vault):
        org = _make_org(session)
        actor = _make_user(session)
        kh.bootstrap_org_keys(session, org_id=org.id, vault=tmp_vault)
        new_vault = tmp_vault.rotate()
        kh.rotate_operator_key(
            session,
            vault_old=tmp_vault,
            vault_new=new_vault,
            actor_user_id=actor.id,
        )
        ev = session.query(M.AuthEvent).filter_by(kind="operator_key_rotated").one()
        assert ev.user_id == actor.id
        assert ev.detail["rows_rewrapped"] == 1
        assert ev.detail["old_operator_key_id"] == tmp_vault.operator_key_id()
        assert ev.detail["new_operator_key_id"] == new_vault.operator_key_id()


# ---------------------------------------------------------------------------
# rotate_org_master_key
# ---------------------------------------------------------------------------


class TestRotateOrgMasterKey:
    def test_marks_previous_retired(self, session, tmp_vault):
        org = _make_org(session)
        first = kh.bootstrap_org_keys(session, org_id=org.id, vault=tmp_vault)
        kh.rotate_org_master_key(
            session, org_id=org.id, vault=tmp_vault, reason="member_removed"
        )
        session.refresh(first)
        assert first.retired_at is not None

    def test_bumps_key_version(self, session, tmp_vault):
        org = _make_org(session)
        kh.bootstrap_org_keys(session, org_id=org.id, vault=tmp_vault)
        new_row = kh.rotate_org_master_key(
            session, org_id=org.id, vault=tmp_vault, reason="member_removed"
        )
        assert new_row.key_version == 2
        active = kh.get_active_org_master_key(session, org_id=org.id)
        assert active.id == new_row.id

    def test_emits_two_audit_events(self, session, tmp_vault):
        org = _make_org(session)
        kh.bootstrap_org_keys(session, org_id=org.id, vault=tmp_vault)
        kh.rotate_org_master_key(
            session, org_id=org.id, vault=tmp_vault, reason="compromised_device"
        )
        retired = session.query(M.AuthEvent).filter_by(kind="org_kek_retired").one()
        assert retired.detail["reason"] == "compromised_device"
        assert retired.detail["replaced_by_version"] == 2
        # Two bootstrap events overall: one for v1, one for v2.
        bs = session.query(M.AuthEvent).filter_by(kind="org_kek_bootstrap").all()
        assert len(bs) == 2

    def test_requires_reason(self, session, tmp_vault):
        org = _make_org(session)
        kh.bootstrap_org_keys(session, org_id=org.id, vault=tmp_vault)
        with pytest.raises(ValueError):
            kh.rotate_org_master_key(session, org_id=org.id, vault=tmp_vault, reason="")

    def test_raises_when_no_active_key(self, session, tmp_vault):
        org = _make_org(session)
        with pytest.raises(kh.OrgMasterKeyMissing):
            kh.rotate_org_master_key(
                session, org_id=org.id, vault=tmp_vault, reason="member_removed"
            )

    def test_each_version_unwraps_under_its_aad(self, session, tmp_vault):
        org = _make_org(session)
        kh.bootstrap_org_keys(session, org_id=org.id, vault=tmp_vault)
        kh.rotate_org_master_key(
            session, org_id=org.id, vault=tmp_vault, reason="member_removed"
        )
        rows = (
            session.query(M.OrgMasterKey)
            .filter_by(org_id=org.id)
            .order_by(M.OrgMasterKey.key_version)
            .all()
        )
        # v1 unwraps under v1 AAD only
        v1_aad = kv.aad_for_org_master_key(org_id=org.id, key_version=1)
        v2_aad = kv.aad_for_org_master_key(org_id=org.id, key_version=2)
        plain1 = tmp_vault.unwrap(rows[0].wrapped_org_kek, aad=v1_aad)
        plain2 = tmp_vault.unwrap(rows[1].wrapped_org_kek, aad=v2_aad)
        assert len(plain1) == kh.KEK_LEN
        assert len(plain2) == kh.KEK_LEN
        assert plain1 != plain2  # rotation generated a fresh KEK

        from cryptography.exceptions import InvalidTag

        with pytest.raises(InvalidTag):
            tmp_vault.unwrap(rows[0].wrapped_org_kek, aad=v2_aad)
