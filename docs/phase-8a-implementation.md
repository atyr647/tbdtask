# Phase 8a — Passkey + PRF + Step-Up Auth

> **Status: ready to implement.** First slice of the security
> architecture (`docs/security-architecture.md`). Adds passkey
> enrollment, WebAuthn PRF detection, and step-up auth gates on
> existing sensitive actions. **No field encryption yet** — that
> arrives in 8c. This phase establishes the credential layer that
> 8b–8e build on.

## Goals

1. Every member has at least one registered WebAuthn credential after a grace period.
2. PRF support is detected at registration and recorded; non-PRF credentials are usable for step-up but not for future field decrypt (8c+).
3. Sensitive admin actions require a recent passkey verification (step-up), not just a session cookie.
4. The credential layer is audited at the same fidelity as the existing OIDC layer.

## Non-goals (deferred)

- Field-level encryption (Phase 8c).
- KEK derivation / key wrapping (Phase 8b — only the PRF *output handle* is captured, not used).
- Admin recovery flow (Phase 8b).
- Member-removal rotation (Phase 8e).
- Mobile-mediated desktop QR approval (Phase 8b/c interaction).

## Schema additions

```sql
-- Migration: alembic revision -m "phase8a_webauthn_credentials"

CREATE TABLE user_webauthn_credentials (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         bigint NOT NULL REFERENCES user_accounts(id) ON DELETE CASCADE,
    credential_id   bytea NOT NULL UNIQUE,           -- WebAuthn credentialId
    public_key      bytea NOT NULL,                  -- COSE_Key
    sign_count      bigint NOT NULL DEFAULT 0,
    aaguid          uuid,                            -- authenticator family
    transports      text[],                          -- ["usb", "ble", "internal", "hybrid"]
    backup_eligible boolean NOT NULL DEFAULT false,
    backup_state    boolean NOT NULL DEFAULT false,
    prf_supported   boolean NOT NULL DEFAULT false,  -- detected at registration
    nickname        text,                            -- user-facing label, e.g. "iPhone 15"
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_used_at    timestamptz,
    revoked_at      timestamptz
);

CREATE INDEX idx_webauthn_user_active ON user_webauthn_credentials (user_id)
    WHERE revoked_at IS NULL;

CREATE TABLE step_up_grants (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      text NOT NULL REFERENCES user_sessions(id) ON DELETE CASCADE,
    credential_id   uuid NOT NULL REFERENCES user_webauthn_credentials(id),
    granted_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    purpose         text NOT NULL,                   -- 'admin_grant' | 'export' | 'sensitive_decrypt' | 'general'
    consumed_at     timestamptz                      -- single-use grants set this
);

CREATE INDEX idx_step_up_session_purpose
    ON step_up_grants (session_id, purpose, expires_at)
    WHERE consumed_at IS NULL;
```

`AuthEvent` already exists from Phase 1; extend its `event_type` enum with:
- `webauthn_register`
- `webauthn_register_failed`
- `webauthn_verify`
- `webauthn_verify_failed`
- `webauthn_revoke`
- `step_up_grant`
- `step_up_failed`

## Module layout

```
app/
  auth/
    webauthn.py          NEW — registration + verification primitives
    step_up.py           NEW — gate decorators + grant lifecycle
    sessions.py          (existing) — extended with step_up helpers
  routes/
    auth.py              (existing) — add /auth/webauthn/* endpoints
    admin.py             (existing) — wire step_up gates on sensitive actions
  templates/
    auth/
      passkey_register.html   NEW — registration UI
      passkey_manage.html     NEW — list / revoke / nickname devices
      step_up_prompt.html     NEW — modal/page for verification challenge
  static/
    webauthn.js          NEW — navigator.credentials wrappers + PRF probe
```

## Server interfaces

```python
# app/auth/webauthn.py

@dataclass
class RegistrationChallenge:
    challenge: bytes               # 32 random bytes
    user_id: bytes                 # internal user id, opaque to authenticator
    rp_id: str                     # configured RP id (host)
    expires_at: datetime

@dataclass
class RegistrationResult:
    credential_id: bytes
    public_key: bytes              # COSE
    aaguid: UUID
    transports: list[str]
    backup_eligible: bool
    backup_state: bool
    prf_supported: bool
    sign_count: int


def begin_registration(db: Session, *, user: UserAccount,
                       require_resident: bool = True) -> RegistrationChallenge: ...

def finish_registration(db: Session, *, user: UserAccount,
                        client_response: dict,
                        challenge: RegistrationChallenge) -> RegistrationResult:
    """Verify the attestation, persist the credential, return summary.

    Failures raise WebAuthnVerificationError; do NOT swallow.
    """


@dataclass
class AssertionChallenge:
    challenge: bytes
    rp_id: str
    expires_at: datetime
    purpose: str                   # echoed back into the step_up_grant
    allow_credentials: list[bytes] # explicit allowlist for this user


def begin_assertion(db: Session, *, user: UserAccount,
                    purpose: str) -> AssertionChallenge: ...

def finish_assertion(db: Session, *, user: UserAccount,
                     client_response: dict,
                     challenge: AssertionChallenge) -> uuid.UUID:
    """Returns the credential UUID that satisfied the challenge.
    Raises WebAuthnVerificationError on any mismatch.
    """
```

```python
# app/auth/step_up.py

class StepUpRequired(HTTPException):
    """Raised by the gate; routes/middleware turn this into a 403 +
    step_up_prompt redirect."""

def grant_step_up(db: Session, *, session_id: str, credential_id: UUID,
                  purpose: str, ttl: timedelta = timedelta(minutes=10),
                  single_use: bool = False) -> None: ...

def has_step_up(db: Session, *, session_id: str, purpose: str,
                consume: bool = False) -> bool: ...

def require_step_up(purpose: str, *, single_use: bool = False):
    """FastAPI dependency. Raises StepUpRequired if the current session
    has no fresh grant for this purpose. Single-use grants are
    consumed on success."""
    def dep(request: Request, db: Session = Depends(get_db)) -> None:
        session_id = request.cookies.get(SESSION_COOKIE_NAME)
        if not has_step_up(db, session_id=session_id, purpose=purpose,
                           consume=single_use):
            raise StepUpRequired(purpose)
    return dep
```

## Step-up gates wired in 8a

| Existing route | Required purpose | Single-use |
|---|---|---|
| `POST /admin/members/{id}/roles` (grant role) | `admin_grant` | Yes |
| `POST /admin/members/{id}/roles/{grant_id}/revoke` | `admin_grant` | Yes |
| `POST /admin/members/{id}/suspend` | `admin_grant` | Yes |
| `POST /admin/invites` (issue invite) | `admin_grant` | Yes |
| `POST /admin/invites/{id}/revoke` | `admin_grant` | Yes |
| `POST /orgs/delete` | `admin_grant` | Yes |
| `POST /admin/roles/{id}/archive` (custom roles only) | `admin_grant` | Yes |

Step-up grant TTL: 10 minutes. Single-use means the grant is consumed
on success; the user re-prompts for the next sensitive action.
General (non-single-use) grants would only be added in 8c+ for
field-decrypt sessions.

## Client interfaces

```javascript
// app/static/webauthn.js — loaded only on the registration / step-up pages.

export async function probePrfSupport() {
  // navigator.credentials.create({...prf:{eval:{first:zero32}}}) and inspect
  // the clientExtensionResults; return true if the authenticator returned
  // a PRF output, false otherwise. Used at registration only.
}

export async function registerCredential(challenge, options) {
  // Wraps navigator.credentials.create with PublicKeyCredential.
  // Always sets userVerification = "required" and resident = "preferred".
  // Probes PRF support and returns the result alongside the attestation.
}

export async function verifyCredential(challenge, options) {
  // Wraps navigator.credentials.get with PublicKeyCredential.
  // userVerification = "required". Returns clientDataJSON + signature
  // for the server to verify.
}
```

CSP impact: `webauthn.js` is loaded with SRI; no new inline script.
Existing `/static/app.js` continues to handle drawer/dialog/etc.

## UX flow

### First-login enrollment (mandatory after grace period)

1. User completes OAuth login as today.
2. Server checks: does this user have any non-revoked credential? If no, redirect to `/auth/passkey/register`.
3. Page explains: "Add a passkey to keep your account secure." Single primary "Set up passkey" button.
4. Browser invokes `navigator.credentials.create()` with PRF extension probe.
5. Server verifies attestation, persists row in `user_webauthn_credentials` with `prf_supported` recorded.
6. User is redirected to their original destination.
7. **Grace period:** for the first 14 days after this phase ships, users who skip enrollment can continue but see a persistent banner. After the grace period, enrollment is required to use the app.

### Step-up prompt

1. User clicks "Suspend" on a member.
2. Route handler hits `Depends(require_step_up("admin_grant"))`.
3. No fresh grant: server returns 403 with a redirect to `/auth/step-up?purpose=admin_grant&next=/admin/members`.
4. Step-up page invokes `navigator.credentials.get()` with `userVerification: "required"`.
5. On success, server creates a `step_up_grants` row (single-use).
6. Server redirects back to the original action; the form resubmits automatically (CSRF token preserved).
7. The gate finds the grant, consumes it, and processes the action.

### Passkey management

`/auth/passkey/manage` lists the user's credentials with:
- Nickname (editable)
- Last used timestamp
- Authenticator family (from `aaguid`, looked up against the FIDO MDS list)
- PRF support flag (informational in 8a; gates field decrypt in 8c+)
- Revoke button (cannot revoke the last credential without re-enrolling first)

## Tests

```
tests/test_phase8a_webauthn.py

class TestRegistration:
    test_register_persists_credential
    test_register_records_prf_support_when_authenticator_offers_it
    test_register_records_no_prf_when_authenticator_lacks_extension
    test_register_replay_with_used_challenge_fails
    test_register_with_mismatched_origin_fails

class TestAssertion:
    test_verify_with_valid_signature
    test_verify_with_unknown_credential_id_fails
    test_verify_with_replayed_signature_count_fails
    test_verify_with_user_verification_flag_unset_fails

class TestStepUp:
    test_grant_records_purpose_and_ttl
    test_grant_expires_after_ttl
    test_single_use_grant_consumed_on_match
    test_grant_for_different_purpose_does_not_satisfy_gate
    test_revoked_credential_cannot_satisfy_gate

class TestEnrollmentEnforcement:
    test_user_without_credential_redirected_to_register
    test_grace_period_banner_shown_inside_window
    test_grace_period_expired_blocks_app

class TestSensitiveActions:
    test_grant_role_requires_fresh_step_up
    test_grant_role_consumes_single_use_grant
    test_grant_role_with_no_step_up_returns_403
    test_kick_member_requires_step_up
    test_revoke_role_requires_step_up
    test_invite_requires_step_up
    test_org_delete_requires_step_up
```

Use `webauthn` library or hand-rolled COSE/CBOR; reference test
vectors from the WebAuthn spec for round-tripping.

## Audit log additions

Every state change in this phase emits an `AuthEvent`:

| Event | Captured fields |
|---|---|
| `webauthn_register` | user_id, credential_id (uuid), aaguid, prf_supported, transports |
| `webauthn_register_failed` | user_id, failure_reason |
| `webauthn_verify` | user_id, credential_id (uuid), purpose |
| `webauthn_verify_failed` | user_id, claimed_credential_id, failure_reason |
| `webauthn_revoke` | user_id, credential_id (uuid), revoked_by_user_id |
| `step_up_grant` | session_id, credential_id (uuid), purpose, ttl |
| `step_up_failed` | session_id, purpose, failure_reason |

These rows are subject-visible: a user can see their own credential
events; admins can see them for members in scopes they have
audit-read on. Append-only enforcement comes in Phase 8d.

## Configuration

```python
# app/auth/webauthn.py
RP_ID = os.getenv("TBDTASK_WEBAUTHN_RP_ID")  # required, e.g. "tbdtask.example.com"
RP_NAME = os.getenv("TBDTASK_WEBAUTHN_RP_NAME", "tbdtask")
REGISTRATION_TIMEOUT = timedelta(minutes=5)
ASSERTION_TIMEOUT = timedelta(minutes=2)
STEP_UP_TTL = timedelta(minutes=10)
ENROLLMENT_GRACE_PERIOD = timedelta(days=14)
```

Local-dev (`TBDTASK_INSECURE_LOCAL_COOKIES=1`) sets `RP_ID = "localhost"`.

## Dependencies to add

- `webauthn` (PyPI: https://pypi.org/project/webauthn/) — handles attestation parsing, signature verification, COSE keys. Audited, maintained, FIDO Alliance-aligned.

No client-side library; Web Crypto + native `navigator.credentials` are sufficient.

## Risks and mitigations specific to 8a

| Risk | Mitigation |
|---|---|
| User skips enrollment indefinitely | Grace-period banner → hard block after 14 days |
| User loses sole passkey before 8b lands | Allow re-enrollment via OAuth-only path during 8a; tighten in 8b once recovery infra exists |
| AAGUID-based authenticator allowlists block legitimate users | Don't filter by AAGUID at MVP; record it for telemetry only |
| Synced passkeys (iCloud Keychain, Google PWM) accepted | Acceptable in 8a; revisit in 8b when key wrapping makes the question consequential |
| Step-up bypass via direct route call | All gated routes use `Depends(require_step_up(...))`; integration test suite exercises every gate |
| Replay of registration response | Challenge stored server-side with `expires_at`, single-use, deleted on consume |

## Definition of done

- [ ] Migration applied; tables exist; indexes created.
- [ ] `webauthn.py`, `step_up.py` modules implemented with full unit tests.
- [ ] Three new templates render through the existing base + drawer + tab bar shell.
- [ ] All seven sensitive admin routes wired to `require_step_up`.
- [ ] `/auth/passkey/register` and `/auth/passkey/manage` reachable from drawer + admin home.
- [ ] Audit events emitted and subject-visible in user's existing audit timeline.
- [ ] Grace-period banner renders during the window; hard-block triggers after.
- [ ] Test suite green: 261 existing tests + new `test_phase8a_webauthn.py` (≈30 cases).
- [ ] `ruff check` and `ruff format --check` clean.
- [ ] Smoke-tested in mobile (iOS Safari, Android Chrome) and desktop (Chrome, Firefox, Safari) — at least one platform-authenticator and one cross-device hardware-key flow each.

## What 8b will need from this phase

- `user_webauthn_credentials.id` is the FK target for `credential_keys.credential_id`.
- `user_webauthn_credentials.prf_supported` gates whether a credential can be used for KEK derivation.
- Step-up infrastructure (`step_up_grants`, `require_step_up`) is reused unchanged; 8b adds new purposes (`enrollment_wrap`, `recovery_unwrap`).
- The PRF probe in `webauthn.js` returns the actual PRF output for the second eval — 8a captures *whether* PRF works; 8b uses the *output*.

---

*See `docs/security-architecture.md` for the full system spec. This
document is the implementation plan for the first phase only.*
