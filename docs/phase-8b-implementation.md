# Phase 8b — Server-side key hierarchy plumbing

> **Status: in progress.** Builds on Phase 8a (passkey enrollment + step-up).
> 8b lays down the *server-side* halves of the key hierarchy: the operator
> key (KMS in production, local file in dev), the per-org master key, the
> per-credential wrap, and the bootstrap flow that runs at org creation.
>
> **Out of scope here, deferred to 8b.2 (next slice):** the *client-side*
> PRF→HKDF→KEK_credential derivation, the ECDH-mediated bootstrap that
> hands KEK_org_master to the first-enrolling browser, and the wrap-on-
> enrollment flow that closes the loop. 8b.1 establishes the schema and
> the operator-key plumbing without touching the client.

## Why split 8b in half

The full key hierarchy is:

```
WebAuthn PRF  →  HKDF  →  KEK_credential  →  KEK_org_master  →  ...
                              (in browser tab RAM only)
```

KEK_org_master is also wrapped under the operator key for the
recovery/rotation path. The bootstrap question — "how does
KEK_org_master come into existence the first time?" — has two halves:

1. **Server side**: generate KEK_org_master, persist it wrapped under
   the operator key. (8b.1, this slice.)
2. **Client side**: hand the freshly-generated KEK_org_master to the
   first-enrolling browser via an ECDH transport channel; the browser
   re-wraps it under PRF-derived KEK_credential and ships the wrapped
   form back. (8b.2, next slice.)

8b.1 is server-only. The keys exist in the database but no client ever
touches them. 8b.2 closes the loop and makes the key material reachable
from the browser. 8c then uses the established hierarchy to encrypt
`person.notes`.

## Goals (8b.1)

1. Define the server-side key vault abstraction (`KeyVault`) with two
   implementations: `LocalKeyVault` (file on disk, dev/AppImage) and
   placeholder for cloud KMS (8b.3+).
2. Generate `KEK_org_master` at org creation when WebAuthn is enabled,
   wrapped under the operator key, persisted in `org_master_keys`.
3. Define `credential_keys` schema for per-device wraps (rows are
   *empty* in 8b.1; populated in 8b.2 once the bootstrap flow lands).
4. Cover the new code with unit tests including key rotation,
   double-wrap (rotate operator key without touching org keys), and
   audit emission.

## Goals (8b.2, next slice — not in this commit)

1. ECDH transport channel for first-credential bootstrap.
2. `prf_salt` minted per credential×org row.
3. Routes that mint/serve wrapped material to the client.
4. Browser-side: PRF → HKDF → CryptoKey, in-tab key cache, drop-on-tab-close.
5. Multi-device wrap-on-enrollment (old device acts as the rewrapper).

## Non-goals (deferred)

- Field encryption (Phase 8c).
- Member-removal rotation worker (Phase 8e).
- Audit log integrity-chain hardening (Phase 8d).
- AWS/GCP KMS adapters (later — `LocalKeyVault` is enough for testing).

## Schema additions

```sql
-- Migration: phase8b1_key_hierarchy

CREATE TABLE org_master_keys (
    id                          uuid PRIMARY KEY,
    org_id                      bigint NOT NULL REFERENCES organizations(id),
    key_version                 int NOT NULL,
    -- KEK_org_master wrapped under the operator key. Header format:
    -- §6 of docs/security-architecture.md.
    wrapped_org_kek             bytea NOT NULL,
    -- The operator-key id used to wrap. For LocalKeyVault this is the
    -- file's content hash; for KMS it is the key ARN.
    operator_key_id             text NOT NULL,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    retired_at                  timestamptz
);

CREATE INDEX idx_org_master_keys_org
    ON org_master_keys (org_id, retired_at);

CREATE TABLE credential_keys (
    id                          uuid PRIMARY KEY,
    credential_id               uuid NOT NULL REFERENCES user_webauthn_credentials(id),
    org_id                      bigint NOT NULL REFERENCES organizations(id),
    -- KEK_org_master wrapped under KEK_credential (PRF-derived, in-browser).
    -- Populated by 8b.2 — empty in 8b.1.
    wrapped_org_kek             bytea NOT NULL,
    -- The 32-byte salt the client passes to PRF. Stored so the same
    -- credential always derives the same KEK_credential for this org.
    prf_salt                    bytea NOT NULL,
    -- Audit trail: which device performed the wrap. Null on the very
    -- first credential for an org (no prior device existed).
    wrapped_by_credential_id    uuid REFERENCES user_webauthn_credentials(id),
    created_at                  timestamptz NOT NULL DEFAULT now(),
    retired_at                  timestamptz
);

CREATE INDEX idx_credential_keys_credential_org
    ON credential_keys (credential_id, org_id, retired_at);
```

Type notes for cross-database portability: `uuid` becomes `String(36)`,
`bytea` becomes `LargeBinary`, `timestamptz` becomes `DateTime`. The
spec uses Postgres-flavored types; this is the actual implementation.

## Module layout

```
app/
  auth/
    key_vault.py        NEW — abstract KeyVault + LocalKeyVault
    key_hierarchy.py    NEW — bootstrap_org_keys, unwrap_org_kek,
                              rotate_operator_key, helpers
    webauthn.py         (existing) — credential ID is now FK source
    step_up.py          (existing)
  routes/
    onboarding.py       MODIFIED — call bootstrap_org_keys on /orgs/create
                                   when WebAuthn is enabled
```

## KeyVault interface

```python
class KeyVault(Protocol):
    def operator_key_id(self) -> str: ...
    def wrap(self, plaintext: bytes, *, aad: bytes) -> bytes: ...
    def unwrap(self, ciphertext: bytes, *, aad: bytes) -> bytes: ...
    # Rotation: generates a fresh operator key, returns it for the
    # caller to use in re-wrapping existing material.
    def rotate(self) -> "KeyVault": ...
```

`LocalKeyVault`:
- Reads/writes `data/operator_key` (32 random bytes, mode 0600).
- `operator_key_id()` returns `local:<sha256(file_contents)[:16]>` so
  rotation produces a new id.
- `wrap` / `unwrap` use AES-256-GCM with the §6 wire format, fresh IV
  per wrap, AAD passed through unchanged.

`AWSKMSKeyVault`, `GCPKMSKeyVault` — placeholder stubs in 8b.1 that
raise `NotImplementedError`. Real implementations land in 8b.3 once
the local path is proven.

## key_hierarchy module — 8b.1 surface

```python
def bootstrap_org_keys(db: Session, *, org_id: int, vault: KeyVault) -> M.OrgMasterKey:
    """Generate KEK_org_master and persist it wrapped under the operator key.

    Idempotent: returns the existing active row if the org already has
    one. Emits a key_event audit row.
    """

def get_active_org_master_key(db: Session, *, org_id: int) -> Optional[M.OrgMasterKey]: ...

def unwrap_org_kek_via_operator(
    db: Session, *, org_id: int, vault: KeyVault
) -> bytes:
    """Server-side unwrap of KEK_org_master via the operator key.

    Used during recovery/rotation/bootstrap-of-second-credential. Every
    call emits an audit row noting actor + reason. Plaintext is held
    only by the caller's stack frame; nothing on the server persists
    it.
    """

def rotate_operator_key(db: Session, *, vault_old: KeyVault, vault_new: KeyVault) -> int:
    """Re-wrap every active org_master_key from old vault to new vault.

    Returns count of rows touched. Mirrors the §9 operator-key path:
    requires an audited control around it, but the mechanics are
    here.
    """
```

8b.2 will add `wrap_for_credential`, `unwrap_for_credential`, and the
ECDH transport helpers.

## Audit

Every state change emits an `AuthEvent` (we reuse the existing table
since 8d hardens it later). New event kinds:

| Kind | Captured fields |
|---|---|
| `org_kek_bootstrap` | org_id, key_version, operator_key_id |
| `org_kek_unwrapped_by_operator` | org_id, key_version, reason |
| `operator_key_rotated` | rows_rewrapped, old_operator_key_id, new_operator_key_id |
| `org_kek_retired` | org_id, key_version, replaced_by_version |

The "reason" parameter on `unwrap_org_kek_via_operator` is required
and structured (matches the recovery reason enum in §9 of the spec).

## Configuration

```python
# app/auth/key_vault.py
KEY_VAULT_PROVIDER = os.getenv("TBDTASK_KEY_VAULT", "local")  # or "aws_kms" | "gcp_kms"
LOCAL_KEY_PATH = os.getenv("TBDTASK_OPERATOR_KEY_PATH",
                           str(DATA_DIR / "operator_key"))
```

When `TBDTASK_WEBAUTHN_ENABLED` is unset, the vault is never
instantiated — `bootstrap_org_keys` is only called from a feature-
flag-guarded path in `onboarding.py`. Existing orgs created before
the flag was flipped on get a lazy bootstrap on first use (8b.2 adds
this path).

## Tests

```
tests/test_phase8b_key_hierarchy.py

class TestLocalKeyVault:
    test_wrap_unwrap_roundtrip
    test_unwrap_rejects_modified_ciphertext       # GCM tag verifies AAD
    test_unwrap_rejects_modified_aad
    test_operator_key_id_changes_after_rotate
    test_rotate_returns_new_vault_with_new_id
    test_key_file_created_with_restrictive_mode

class TestBootstrapOrgKeys:
    test_creates_org_master_key_row
    test_idempotent_when_called_twice
    test_emits_audit_event
    test_wrapped_blob_unwraps_to_correct_plaintext
    test_uses_current_operator_key_id

class TestUnwrapViaOperator:
    test_returns_plaintext_kek
    test_emits_audit_with_reason
    test_rejects_unknown_org
    test_rejects_retired_key_version

class TestRotateOperatorKey:
    test_rewraps_all_active_org_keys
    test_marks_old_keys_retired_and_creates_new
    test_no_op_when_no_orgs
    test_each_org_now_unwrappable_by_new_vault_only

class TestSchema:
    test_org_master_key_unique_active_per_org
    test_credential_key_fk_to_user_webauthn_credentials
```

## Sequencing

- This commit (8b.1): vault, schema, key_hierarchy primitives, onboarding hook, tests.
- Next commit (8b.2): client-side PRF derivation, ECDH bootstrap, wrap-on-enrollment flow.
- After that: 8c (encrypt person.notes).

## Roll-back

Same posture as 8a. `TBDTASK_WEBAUTHN_ENABLED=0` → bootstrap is never
called, tables sit empty. `alembic downgrade -1` removes the new
tables cleanly. The `LocalKeyVault` file (`data/operator_key`) is
gitignored alongside `data/secret_key`.
