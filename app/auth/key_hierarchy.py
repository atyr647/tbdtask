"""Key hierarchy operations (Phase 8b.1, server side).

The key hierarchy in this app is:

    KEK_credential   ←  PRF → HKDF (browser tab RAM only)
        │ unwraps
        ▼
    KEK_org_master   ←  one per org, wrapped per credential, also wrapped
        │              under the operator key (vault) for recovery/rotation
        │ unwraps
        ▼
    KEK_scope / DEK  (8c+)

This module manages the *server-side* halves only:

* ``bootstrap_org_keys`` — generate ``KEK_org_master`` for an org and
  persist it wrapped under the operator key. Idempotent.
* ``get_active_org_master_key`` — fetch the current row.
* ``unwrap_org_kek_via_operator`` — server-side unwrap, audited.
  Reserved for recovery / rotation / first-credential bootstrap. Plain
  KEK material exists only in the caller's stack frame; nothing else.
* ``rotate_operator_key`` — re-wrap every active ``OrgMasterKey`` under
  a new vault. Used when the operator key itself rotates.
* ``rotate_org_master_key`` — generate a new ``KEK_org_master`` (e.g.
  on member removal). Marks the previous version retired and creates
  a fresh row. Note: this only rotates the org-master KEK *under the
  operator key*; the per-credential wraps in ``credential_keys`` are
  invalidated and must be re-issued by 8b.2 wrap-on-rewrap.

Browser-side helpers (PRF derivation, ECDH bootstrap, wrap-on-
enrollment) land in 8b.2.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as M
from .key_vault import KeyVault, aad_for_org_master_key


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KEK_LEN = 32  # AES-256


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class KeyHierarchyError(Exception):
    """Base class for key-hierarchy errors."""


class OrgMasterKeyMissing(KeyHierarchyError):
    """Raised when an operation requires a bootstrapped org but none exists."""


class OrgMasterKeyRetired(KeyHierarchyError):
    """Raised when caller asks for a specific retired key version."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _emit_event(
    db: Session,
    *,
    kind: str,
    user_id: Optional[int] = None,
    detail: Optional[dict] = None,
) -> None:
    db.add(
        M.AuthEvent(
            kind=kind,
            user_id=user_id,
            detail=detail,
        )
    )
    db.flush()


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def get_active_org_master_key(db: Session, *, org_id: int) -> Optional[M.OrgMasterKey]:
    """Return the org's current (non-retired) master-key row, or None."""
    return (
        db.query(M.OrgMasterKey)
        .filter(
            M.OrgMasterKey.org_id == org_id,
            M.OrgMasterKey.retired_at.is_(None),
        )
        .order_by(M.OrgMasterKey.key_version.desc())
        .first()
    )


def bootstrap_org_keys(
    db: Session,
    *,
    org_id: int,
    vault: KeyVault,
    actor_user_id: Optional[int] = None,
) -> M.OrgMasterKey:
    """Generate ``KEK_org_master`` for an org and persist it wrapped.

    Idempotent: returns the existing active row if one is already
    present. The plaintext KEK is generated and immediately wrapped in
    the same function call; it never persists outside this stack frame.

    Audit: emits ``org_kek_bootstrap`` (or ``org_kek_already_present``
    on the idempotent path).
    """
    existing = get_active_org_master_key(db, org_id=org_id)
    if existing is not None:
        _emit_event(
            db,
            kind="org_kek_already_present",
            user_id=actor_user_id,
            detail={
                "org_id": org_id,
                "key_version": existing.key_version,
                "operator_key_id": existing.operator_key_id,
            },
        )
        return existing

    key_version = 1
    plaintext_kek = secrets.token_bytes(KEK_LEN)
    aad = aad_for_org_master_key(org_id=org_id, key_version=key_version)
    wrapped = vault.wrap(plaintext_kek, aad=aad)
    # Best effort to scrub the plaintext from this stack frame. Python
    # bytes are immutable so there's no in-place zero we can do; the
    # binding gets dropped at function return and the GC eventually
    # collects it. This `del` is a hint, not a guarantee.
    del plaintext_kek

    row = M.OrgMasterKey(
        id=str(uuid.uuid4()),
        org_id=org_id,
        key_version=key_version,
        wrapped_org_kek=wrapped,
        operator_key_id=vault.operator_key_id(),
    )
    db.add(row)
    db.flush()

    _emit_event(
        db,
        kind="org_kek_bootstrap",
        user_id=actor_user_id,
        detail={
            "org_id": org_id,
            "key_version": key_version,
            "operator_key_id": row.operator_key_id,
        },
    )
    return row


def unwrap_org_kek_via_operator(
    db: Session,
    *,
    org_id: int,
    vault: KeyVault,
    reason: str,
    actor_user_id: Optional[int] = None,
) -> bytes:
    """Server-side unwrap of ``KEK_org_master`` via the operator key.

    Reserved for the operator-key paths in §8 / §9 of the security
    spec: admin recovery, member-removal rotation, first-credential
    bootstrap. The plaintext is held only by the caller's stack frame
    and is never written to disk / log / response body.

    ``reason`` is required and structured (one of the recovery-reason
    enum values, plus ``bootstrap`` for first-credential setup).
    """
    if not reason:
        raise ValueError("reason is required for operator-key unwrap")

    row = get_active_org_master_key(db, org_id=org_id)
    if row is None:
        raise OrgMasterKeyMissing(f"No active KEK_org_master for org_id={org_id}")
    aad = aad_for_org_master_key(org_id=org_id, key_version=row.key_version)
    plaintext = vault.unwrap(row.wrapped_org_kek, aad=aad)

    _emit_event(
        db,
        kind="org_kek_unwrapped_by_operator",
        user_id=actor_user_id,
        detail={
            "org_id": org_id,
            "key_version": row.key_version,
            "operator_key_id": row.operator_key_id,
            "reason": reason,
        },
    )
    return plaintext


def rotate_operator_key(
    db: Session,
    *,
    vault_old: KeyVault,
    vault_new: KeyVault,
    actor_user_id: Optional[int] = None,
) -> int:
    """Re-wrap every active org-master row from old vault to new vault.

    Returns the count of rows touched. The plaintext KEK material for
    each org passes through process memory once; nothing persists
    outside the wrapped row in either DB state.

    Atomicity: each row is rewrapped in its own UPDATE. If the process
    crashes mid-loop, the audit log shows partial completion; resuming
    is safe because rewrapping a row already-bound to ``vault_new`` is
    a no-op (the new vault's operator_key_id differs and the unwrap
    under ``vault_old`` will simply fail — caller can detect that and
    skip).
    """
    rows = db.query(M.OrgMasterKey).filter(M.OrgMasterKey.retired_at.is_(None)).all()
    rewrapped = 0
    for row in rows:
        if row.operator_key_id == vault_new.operator_key_id():
            # Already bound to the new vault — leftover from a previous
            # partial rotation. Skip.
            continue
        aad = aad_for_org_master_key(org_id=row.org_id, key_version=row.key_version)
        plaintext = vault_old.unwrap(row.wrapped_org_kek, aad=aad)
        try:
            row.wrapped_org_kek = vault_new.wrap(plaintext, aad=aad)
            row.operator_key_id = vault_new.operator_key_id()
        finally:
            del plaintext
        rewrapped += 1
    if rewrapped:
        db.flush()

    _emit_event(
        db,
        kind="operator_key_rotated",
        user_id=actor_user_id,
        detail={
            "rows_rewrapped": rewrapped,
            "old_operator_key_id": vault_old.operator_key_id(),
            "new_operator_key_id": vault_new.operator_key_id(),
        },
    )
    return rewrapped


def rotate_org_master_key(
    db: Session,
    *,
    org_id: int,
    vault: KeyVault,
    reason: str,
    actor_user_id: Optional[int] = None,
) -> M.OrgMasterKey:
    """Mark the current ``KEK_org_master`` retired and create a fresh row.

    Used on member removal (§9 of the security spec) and on any other
    "post-compromise" event. The new row is wrapped under the same
    operator key. Per-credential wraps in ``credential_keys`` for the
    *previous* version are not touched here — they remain valid for
    decrypting historical ciphertext until the lazy/eager re-encrypt
    pass completes (8e).

    Returns the newly-created active row.
    """
    if not reason:
        raise ValueError("reason is required for org-master rotation")

    current = get_active_org_master_key(db, org_id=org_id)
    if current is None:
        raise OrgMasterKeyMissing(
            f"No active KEK_org_master to rotate for org_id={org_id}"
        )
    new_version = current.key_version + 1
    plaintext_kek = secrets.token_bytes(KEK_LEN)
    aad = aad_for_org_master_key(org_id=org_id, key_version=new_version)
    wrapped = vault.wrap(plaintext_kek, aad=aad)
    del plaintext_kek

    current.retired_at = _utcnow()
    new_row = M.OrgMasterKey(
        id=str(uuid.uuid4()),
        org_id=org_id,
        key_version=new_version,
        wrapped_org_kek=wrapped,
        operator_key_id=vault.operator_key_id(),
    )
    db.add(new_row)
    db.flush()

    _emit_event(
        db,
        kind="org_kek_retired",
        user_id=actor_user_id,
        detail={
            "org_id": org_id,
            "key_version": current.key_version,
            "replaced_by_version": new_version,
            "reason": reason,
        },
    )
    _emit_event(
        db,
        kind="org_kek_bootstrap",
        user_id=actor_user_id,
        detail={
            "org_id": org_id,
            "key_version": new_version,
            "operator_key_id": new_row.operator_key_id,
            "reason": reason,
        },
    )
    return new_row


# ---------------------------------------------------------------------------
# Selectors used by 8b.2+ for the credential-side wrap
# ---------------------------------------------------------------------------


def get_active_credential_key(
    db: Session, *, credential_uuid: str, org_id: int
) -> Optional[M.CredentialKey]:
    """Return the credential×org wrapped row, or None."""
    stmt = select(M.CredentialKey).where(
        M.CredentialKey.credential_id == credential_uuid,
        M.CredentialKey.org_id == org_id,
        M.CredentialKey.retired_at.is_(None),
    )
    return db.execute(stmt).scalar_one_or_none()


def org_has_any_credential_key(db: Session, *, org_id: int) -> bool:
    """Used by 8b.2's enrollment routing.

    First credential to enroll for an org takes the operator-key
    bootstrap path. Subsequent credentials use a wrap-on-rewrap from
    an already-enrolled device.
    """
    stmt = (
        select(M.CredentialKey.id)
        .where(
            M.CredentialKey.org_id == org_id,
            M.CredentialKey.retired_at.is_(None),
        )
        .limit(1)
    )
    return db.execute(stmt).first() is not None
