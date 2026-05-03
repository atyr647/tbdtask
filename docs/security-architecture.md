# Security Architecture

> **Status: working spec — not yet implemented.** Captures the design
> for client-side encryption with passkey-gated local decryption,
> scoped key hierarchy, authorized admin recovery, and subject-visible
> audit. Implementation is sequenced as Phase 8a → 8e (see §15).
>
> Scope: this document covers *sensitive-field* encryption, identity
> hardening (WebAuthn PRF), and key management. The existing
> tenant-scoping, RLS, and Phase 1–7 security controls remain in
> force; encryption is defense-in-depth on top of authorization, not
> a replacement for it.

## 1. Threat Model

### Protects against
- Database dump exposure (cloud provider snoop, lost backup tape, S3 misconfiguration).
- Encrypted backups exposed in transit or at rest.
- Accidental plaintext logging of sensitive fields. On the **normal request path**, the server handles ciphertext only — encrypted columns never appear as plaintext in app logs, error reports, or APM traces, because the application code never decrypts them.
- Sideways/upward access blocked by the existing authz layer; if that layer leaks rows, the leaked rows are still ciphertext under a key the leaker doesn't hold.
- Removed members accessing **future** encrypted writes (post-compromise security).
- Ordinary server-side authorization bugs leaking encrypted rows: ciphertext alone is useless without the key.

### Server's role on the key path

The server's relationship to key material is path-dependent:

| Path | Server access to key material |
|---|---|
| **Normal request** (read/write a sensitive field) | Ciphertext only. Decryption is client-side after passkey unlock. The server never holds an unwrapped DEK or KEK on this path. |
| **Recovery / rotation** (admin recovery, member-removal rotation, KEK rollover) | The server uses the KMS-held operator key (§9) to unwrap and re-wrap key material. This is a deliberate, audited, rate-limited capability — not a general decryption capability for sensitive fields. The operator key wraps `KEK_org_master`; it does not directly wrap individual DEKs or any field ciphertext. |
| **Backup / DR restore** | Wrapped DEKs and wrapped KEKs are restored as ciphertext. Restoring a backup does not give the server access to unwrapped keys. |

This is why the system is honestly framed as "client-side encrypted with
authorized admin recovery," not zero-knowledge: the operator-key path means
the server *can* (under the controls in §8) participate in unwrapping. It
does not mean the server *does* on every request.

### Does not fully protect against
- A malicious server operator shipping altered client JS that exfiltrates plaintext from RAM after unlock. This is the fundamental ceiling of "encrypted SaaS in a browser" — see §13 (Build Integrity) for what's done to raise the bar.
- A malicious *authorized* admin using the recovery path. Mitigation is audit visibility (§9), not crypto.
- Users who already saw or copied plaintext during a previous session. Rotation cuts off future writes only.
- Compromised endpoint *after* unlock — keys live in tab RAM during the active window.
- Historical backups taken before a crypto-shred event still contain wrapped DEKs alongside the ciphertext. See §12 for the explicit limit.

## 2. Security Model Summary

> Sensitive fields are locally encrypted with passkey-gated access,
> scoped keys, authorized admin recovery, and subject-visible audit
> logs.

This is **not** marketed or labelled as zero-knowledge or end-to-end
encrypted. The honest tagline is:

> "Sensitive fields are locally encrypted with passkey-gated access,
> authorized admin recovery, and subject-visible audit logs."

## 3. Key Hierarchy

```text
WebAuthn PRF(authenticator, salt) (32 bytes, deterministic per credential)
        │
        ▼
HKDF-SHA256(prf_output, salt=org_id, info="tbdtask-kek-v1")
        │
        ▼
KEK_credential                    (in-memory only, never stored)
        │   unwraps
        ▼
KEK_org_master                    (one per org, wrapped per credential)
        │   unwraps
        ▼
KEK_scope                         (per workcenter / per HR scope / etc.)
        │   unwraps
        ▼
DEK_field / DEK_person / DEK_doc  (per row / per person / per doc)
        │   decrypts
        ▼
ciphertext
```

Notes:
- `KEK_org_master` is also wrapped under `KEK_admin_recovery` for the recovery path (§8).
- Per-person DEKs are the granularity that makes crypto-shred and member-removal rotation cheap.
- All "wrap" operations use AES-256-GCM key wrapping with the wrapping key as KEK and a fresh IV.

## 4. Data Classification

Every new field gets classified at creation. Reviewable in PRs.

| Class | Examples | Encrypted? | Key scope |
|---|---|---|---|
| Org-public workflow | task name, status, qual name, due date, assigned user ID, role/permission metadata | No | — (tenant-scoped only) |
| Workcenter-scoped sensitive | personnel notes, team-sensitive comments, internal remarks | Yes | per-workcenter / per-person |
| HR-sensitive | PRD reason, archive reason, medical absence detail, departure notes | Yes | HR scope / per-person |
| Generated artifacts | exports, PDFs, attachments | Yes | per-document DEK |
| Audit metadata (who/when/what) | actor id, timestamp, action type, target id | No | — |
| Audit sensitive body | diffs containing values from encrypted columns | Encrypted, or redacted | matching scope |

Person `last_name` / `first_name` stay plaintext by default. Orgs that
need them encrypted can flip a per-org policy flag in a later phase;
not part of MVP.

## 5. Crypto Primitives

- **AEAD: AES-256-GCM.** Chosen for universal Web Crypto API support and battle-tested implementations on both Python (`cryptography`) and the browser. Pinned in the wire-format `algo` byte (§6) so future migration to ChaCha20-Poly1305 or post-quantum AEADs is mechanical.
- **KDF: HKDF-SHA256** for deriving KEKs from PRF output and for recovery-phrase paths.
- **Server crypto:** `cryptography` (PyCA) only. No hand-rolled primitives.
- **Client crypto:** Web Crypto API only (`subtle.encrypt`, `subtle.deriveKey`). No `sjcl`, no random NPM packages, no userland CryptoJS.
- **Envelope wrapping:** wrap operations encrypt the wrappee (a DEK or KEK) as the *plaintext input* of an AES-256-GCM encryption under the wrapping key, with a fresh random IV and the same wire format as §6. This is **AEAD envelope wrapping**, *not* RFC 3394 AES-KW (NIST SP 800-38F). The distinction matters: AES-KW is a deterministic block-cipher mode without a public IV; what we use is GCM with a fresh IV per wrap and the standard auth tag. The Web Crypto API exposes GCM directly; AES-KW is exposed separately as `AES-KW` and we deliberately do **not** use it. Wrapped blobs include the §6 header, so a wrapped key is byte-compatible with any other ciphertext on the wire.

## 6. Wire Format

Every encrypted blob on the wire and at rest:

```
[version:1][algo:1][key_id:16][iv:12][ciphertext:N][tag:16]
```

| Field | Bytes | Notes |
|---|---|---|
| `version` | 1 | Currently `0x01` |
| `algo` | 1 | `0x01` = AES-256-GCM |
| `key_id` | 16 | UUIDv4 of the `data_keys` row (or KEK row, depending on context). Encoded **big-endian (network byte order)** as defined by RFC 4122 §4.1.2 — `time_low` (4 bytes BE), `time_mid` (2 BE), `time_hi_and_version` (2 BE), `clock_seq_hi_and_reserved` (1), `clock_seq_low` (1), `node` (6). Same layout PostgreSQL uses for `uuid` on the wire. Never store as hex or as Python's `bytes_le`. |
| `iv` | 12 | Random per-encrypt; never reused with the same key |
| `ciphertext` | N | Variable |
| `tag` | 16 | AES-GCM auth tag |

### AAD (Additional Authenticated Data)

Constructed at encrypt and verified at decrypt:

```
column_name + ":" + row_pk + ":" + schema_version
```

Prevents an attacker with DB write access from swapping ciphertexts
between rows or columns. AAD is not stored — it's recomputed from row
context at decrypt time, so any swap fails the GCM tag check.

## 7. Key Storage Tables

```sql
credential_keys
  id                          uuid pk
  credential_id               fk → user_webauthn_credentials.id
  org_id                      fk → organizations
  wrapped_org_kek             bytea            -- KEK_org_master wrapped under KEK_credential
  prf_salt                    bytea (32)       -- input to PRF for this credential×org
  wrapped_by_credential_id    fk → credential_keys.id  -- which device performed the wrap (audit)
  created_at                  timestamptz
  retired_at                  timestamptz NULL

data_keys
  id                          uuid pk
  org_id                      fk → organizations
  scope_kind                  text             -- 'person' | 'workcenter' | 'hr' | 'document'
  scope_id                    text NULL        -- person_id, workcenter_id, etc.
  key_version                 int
  wrapped_dek                 bytea            -- DEK wrapped under KEK_scope (or KEK_org_master)
  parent_kek_id               uuid             -- the KEK that wrapped this DEK
  created_at                  timestamptz
  retired_at                  timestamptz NULL

key_rotation_jobs
  id                          uuid pk
  org_id                      fk → organizations
  trigger                     text             -- 'member_removed' | 'admin_recovery' | 'scheduled' | 'manual'
  status                      text             -- 'pending' | 'in_progress' | 'completed' | 'failed'
  old_key_version             int
  new_key_version             int
  affected_scope_kind         text
  affected_scope_id           text NULL
  authorized_by               fk → org_memberships
  operator_key_id             text NULL        -- KMS key ARN/URI used (if operator path)
  reason                      text             -- one of the structured enum values
  started_at                  timestamptz
  completed_at                timestamptz NULL
  rewritten_count             int

admin_recovery_events
  id                          uuid pk
  org_id                      fk → organizations
  recovered_membership_id     fk → org_memberships
  performed_by_membership_id  fk → org_memberships
  reason                      text             -- structured enum
  authorized_via              text             -- 'operator_key' | 'cooling_off_complete' | 'manual_review'
  cooling_off_started_at      timestamptz NULL
  notified_contacts           jsonb            -- list of email addresses notified
  created_at                  timestamptz
  completed_at                timestamptz NULL
```

## 8. Admin Recovery

Admin recovery is **accepted by design**. The system is not zero-knowledge.

### Recovery path

```
KMS-held operator recovery key (KEK_admin_recovery)
        │   unwraps (audited, rate-limited, requires fresh OAuth + step-up)
        ▼
KEK_org_master
        │   rewraps under
        ▼
new credential KEK / scope KEKs
```

### Required controls per recovery event

- **Reason required**, structured enum (§9).
- **Fresh OAuth** at recovery time (not just an existing session).
- **Step-up auth** with passkey if the recoverer has any registered credential.
- **Rate limits**:
  - Per-org: max **3 recoveries per 90 days**, then auto-lock pending human review.
  - Per-user (recovered identity): max 1 active recovery in flight; subsequent attempts denied until the prior one resolves.
  - Per-IP: throttle to N attempts/hour to defeat automated abuse.
- **Cooling-off period** for sole-owner recovery: **72 h delay** with notifications to every email contact across all linked OAuth identities of the recovered user. Bypass only via verified support escalation (manual, out-of-band).
- **Audit log entry** in `admin_recovery_events` and the main audit log, both append-only and integrity-chained.
- **Notification fanout**: all current org owners + admins with audit-read capability + every email contact on file for the recovered user.
- **Subject-visible**: when the recovery is complete, the recovered user sees a banner on next login showing who recovered them, when, and why.
- **Post-recovery rotation**: `KEK_admin_recovery` itself is rotated after any recovery event that touched an org owner.

### Post-recovery restricted state

A successful recovery just minted a new path into the org with elevated
privileges. Treat the post-recovery state as temporarily high-risk.

For **24–48 hours** after a successful recovery, the recovered session
must:

- Carry a `recovery_origin = true` flag visible in audit log entries.
- **Block** the following operations outright (require waiting out the window or a separate fresh-passkey re-auth + cooling-off):
  - Delete organization
  - Full data export
  - Transfer ownership to another member
  - Rotate `KEK_admin_recovery`
  - Bulk permission changes (≥N role grants/revokes in one batch)
  - Issue/revoke OAuth integrations or webhook secrets
- **Step-up auth** required for any other sensitive action (member kick, single role grant, individual data export, sensitive-field decrypt).

This prevents the "recover → immediately exfiltrate everything" pattern.

### Sole-owner edge case

A sole org owner with no other owners or admins to notify falls back
to either:
1. **Cooling-off + notification to every linked OAuth contact** (default).
2. **Manual out-of-band verification** (support-mediated).

Pick per-deployment policy. For the hosted product the default is (1).

### Recovery is not break-glass

Recovery is for **device loss, same human**. Identity handoff to a
different human is a separate flow (current owner explicitly grants a
new owner) and does not use the crypto recovery path. Do not overload
recovery as "any way to read someone else's data."

## 9. Member Removal and Rotation

### Immediate (synchronous with the kick)
- Revoke `OrgMembership.status = 'suspended'`.
- Revoke all WebAuthn credentials tied to the membership.
- Invalidate all sessions for the user (existing `revoke_all_for_membership`).

### Asynchronous (background `key_rotation_jobs`)
- Generate `KEK_org_master_v(n+1)`.
- Rewrap KEK_org_master under all remaining credential KEKs.
- For high-sensitivity scopes (HR, archive reasons, medical absence detail): generate fresh DEKs and re-encrypt eagerly. Per-person granularity keeps this cheap.
- For lower-risk scopes: lazy rotate on next read or via a slow background sweep.
- Mark `data_keys.retired_at` on the previous version.
- Log the rotation in `key_rotation_jobs` with `trigger='member_removed'` and `authorized_by` = the admin who performed the kick.

### Operator-key path
Rotation requires both the old `KEK_org_master_v(n)` (to unwrap existing DEKs) and the new `KEK_org_master_v(n+1)` (to re-wrap them). The job is performed server-side under the KMS-held **operator key**, with every use logged. This is a deliberate trade-off: it breaks the "server never holds the master key" property in exchange for reliable, automated rotation that doesn't require a live admin browser.

### Reason enum

| Reason | Default policy |
|---|---|
| `lost_device` | Standard recovery, no scope-key rotation |
| `new_device_only` | Treated as normal enrollment, no recovery needed |
| `compromised_device` | Recovery + immediate scope-key rotation |
| `emergency_access` | Admin recovering for missing user; heavier audit; subject-visible |
| `role_transfer` | Different flow entirely — not recovery |
| `member_removed` | Triggered by kick; not a recovery, but uses the rotation infra |

### Important wording

This is **post-compromise security / eventual confidentiality**, not
classic forward secrecy. Removed members retain access to *plaintext
they already saw*; rotation only protects *future* writes after the
rotation completes.

## 10. New-Device Enrollment / Migration

A new device cannot recover existing data on its own — its
`KEK_credential` is unrelated to any existing credential's KEK.

**PRF outputs stay device-local.** The old device never receives the new
device's PRF output, and the new device never receives the old device's PRF
output. They establish an authenticated ephemeral channel and pass only
`KEK_org_master` (the wrappee), which the new device then wraps under its
*own* locally-derived `KEK_credential_new`.

Flow:
1. **Old device** opens migration page, performs biometric/passkey unlock with PRF, derives `KEK_credential_old` *in-tab* (PRF output never leaves the device).
2. **Server** creates a short-lived (≤60 s) single-use migration token bound to: source OAuth `sub`, `org_id`, and TLS channel hash. Token does **not** carry any key material.
3. **Old device** displays a QR encoding `{ session_id, migration_token, rp_id }`. No key material in the QR.
4. **New device** scans the QR, completes OAuth login (must match source `sub`), registers a passkey, and proves PRF support. The new device derives `KEK_credential_new` *in-tab* (PRF output never leaves the device).
5. **New device** generates an ephemeral X25519 (or P-256) key pair `(eph_priv_B, eph_pub_B)` *in-tab* and posts `eph_pub_B` to the server, scoped to the migration token.
6. **Old device** fetches `eph_pub_B`, derives a one-shot transport key via ECDH(`eph_priv_A` ephemeral generated in-tab, `eph_pub_B`) → HKDF, unwraps `KEK_org_master` locally with `KEK_credential_old`, and re-encrypts it under the transport key.
7. **Old device** posts `eph_pub_A` and the transport-encrypted `KEK_org_master` to the server, addressed to the migration token.
8. **New device** fetches `eph_pub_A` and the transport-encrypted blob, completes ECDH, decrypts to recover `KEK_org_master` *in-tab*, re-wraps it under `KEK_credential_new` *locally*, and posts the new wrapped blob to the server.
9. **Server** stores the new wrapped blob in `credential_keys` with `wrapped_by_credential_id` = old device's credential. Server has only ever seen the transport-encrypted intermediate and the final ciphertext-wrapped blob.
10. Migration token is burned. Audit log records the new device registration.

Properties:
- Each device's PRF output is consumed only inside the device that produced it.
- The transport channel is one-shot, ephemeral, and authenticated by the migration token.
- The server holds only ciphertext at every step of the handoff.
- Replay or token theft yields nothing decryptable: the transport key is bound to the ephemeral `(A, B)` ECDH pair the legitimate parties generated.

If old device is unavailable: only the admin recovery path (§8) can
mint a new wrap.

## 11. Build Integrity

Because the client decrypts data, release hygiene is security-critical.

Required:

- **Strict CSP** (already in place — no inline `<script>` or `<style>`, `script-src 'self'`).
- **SRI on every script tag.** Bundle hashes computed at build time, embedded in templates.
- **Immutable versioned bundle URLs.** `/static/app.<sha>.js` rather than `/static/app.js`. Old versions remain reachable for cache-pinned PWAs.
- **Signed build manifest.** Build pipeline emits a signed manifest mapping route → bundle hashes. Server verifies on serve.
- **Service worker pins build hash.** PWA install does not auto-update without explicit user acceptance for security-sensitive sessions.
- **`/security/build` page.** Visible to all users; shows current bundle SHA, build timestamp, manifest signature. Helps establish a tamper-evident history.
- **Decrypt-event audit includes bundle version.** Every audit log entry that records a decryption stamps the bundle hash that performed it.

These don't stop a determined hostile operator, but they shift the
threat from "trivial script swap" to "leaves a tamper-evident trail
that customers can audit."

## 12. Backup and DR

| Decision | Setting | Rationale |
|---|---|---|
| KMS provider | AWS KMS (production) / GCP KMS (multi-cloud) | Boring, audited, FIPS-validated; HSM-backed without operating Vault |
| Cross-region key replication | Yes — KMS multi-region keys for `KEK_admin_recovery` and operator keys | Single-region failure must not brick recovery |
| Wrapped DEKs in DB backups | Yes (they are part of `data_keys`) | Restore is impossible without them |
| Wrapped KEK_org_master in backups | Yes (in `credential_keys`) | Same |
| Crypto-shred semantics | Live DB only | See below |
| Backup retention | 90 days rolling, encrypted | Operational |
| Restore test cadence | Quarterly to a clean environment | Catch backup regressions |

### Crypto-shred limit (honest framing)

> Crypto-shred applies immediately to the live database. Historical
> backups taken before the shred event still contain the ciphertext
> *and* the wrapped DEK; they age out according to the retention
> policy. Customers who need stricter shred semantics on backups must
> either set retention to zero (no historical recovery possible) or
> request a manual backup purge cycle.

Document this in customer-facing privacy / data-handling docs.

## 13. Audit Log Hardening

The audit log is a *trust* feature, not just a security feature.

Required properties:

- **Append-only.** Separate database role with `INSERT`-only grant; no `UPDATE` or `DELETE`. Application connection cannot modify or remove audit rows.
- **Integrity-chained.** Each row's hash includes the previous row's hash; the chain head is signed periodically by a build-time key. Detects after-the-fact excision.
- **Subject-visible.** When an admin decrypts, recovers, or accesses a user's sensitive data, that user can see the event from their own audit timeline. This is what differentiates "admin recovery exists with consent" from "admins quietly read everything."
- **Bundle-stamped.** Every entry records the JS bundle hash that performed the action (§11).
- **Decryption-granular.** Each unwrap of a DEK or recovery-key use is its own row, not aggregated.

Audit-log diff bodies that include encrypted-column values are
themselves encrypted under the matching scope key, or redacted. They
are never stored as cleartext.

## 14. Decisions Log

Pinned decisions with rationale; revisit only with explicit ADRs.

| # | Decision | Rationale |
|---|---|---|
| D1 | Field-level encryption over fragment-level | Preserves search, sort, dashboard, qual matrix; sensitive PII is opaque to the server |
| D2 | Per-person / per-scope DEK granularity | Cheap rotation; cheap crypto-shred; localized blast radius |
| D3 | HKDF after PRF output | Enables salt rotation; standard practice; never use PRF bytes as a key directly |
| D4 | Admin recovery accepted | Matches workplace expectations; "sole owner loses device" is unworkable otherwise |
| D5 | Operator key path for rotation/recovery | Reliable, automated, auditable; explicit trade-off vs. "no server-held key" |
| D6 | Not marketed as zero-knowledge | The browser-served-by-server JS is a real ceiling; honest framing builds trust |
| D7 | Mobile-first PRF support required | Strongest UV + biometric + secure enclave story is on mobile platforms |
| D8 | Desktop access mediated by mobile QR approval | Compensates for desktop's weaker authenticator landscape |
| D9 | AES-256-GCM as MVP AEAD | Universal Web Crypto support; pin via wire-format `algo` byte |
| D10 | AWS/GCP KMS over Vault | Boring, audited, no operational burden of running Vault |
| D11 | Recovery is not break-glass | Device loss, same human; identity handoff is a separate explicit flow |
| D12 | Live-DB-only crypto-shred semantics | Honest about backup limits; document in customer-facing privacy notice |

## 15. Implementation Sequencing

| Phase | Scope | Approx. effort |
|---|---|---|
| **8a** | Passkey enrollment + PRF detection + `KEK_credential` derivation. Step-up auth on existing sensitive actions (admin grants, exports, role changes). No field encryption yet. | 1–1.5 weeks |
| **8b** | Key hierarchy plumbing: `credential_keys`, `data_keys` schema; KEK_org_master + KEK_admin_recovery generation at org creation; wrap-on-enrollment flow; KMS integration for operator key. No field encryption yet. | 1.5–2 weeks |
| **8c** | Migrate `Person.notes` end-to-end as the vertical-slice proof: encrypted at rest, decrypted client-side, AAD enforced, sensitive-info warning UI preserved. | 1 week |
| **8d** | Audit log hardening: append-only role, integrity chain, subject-visible UI, bundle-version stamping. | 1 week |
| **8e** | Member removal → key rotation via operator key. `key_rotation_jobs` worker. | 1 week |
| **8f+** | Roll out to remaining sensitive columns (absence reasons, archive reasons, attachments, generated docs, etc.) at customer-driven cadence. | Ongoing |

Total for 8a–8e: roughly 5.5–6.5 focused weeks.

## 16. Open Items / Not Yet Decided

- **Recovery phrase as additional admin-recovery path.** Optional 24-word phrase derived via Argon2id, displayed once at org creation. Strengthens recovery against a compromised KMS account but adds UX burden. Defer to post-MVP.
- **Per-org policy for encrypting personal names.** Default plaintext; some customer segments (military, medical, legal-sensitive workplaces) may want encrypted. Add per-org flag in 8f+.
- **Hardware-key second-credential enrollment for org owners.** Strong recommendation, not required at MVP.
- **Real-time anomaly detection on recovery + admin-decrypt patterns.** Rate-limit + audit are MVP; ML-driven anomaly is post-MVP.

---

## References

- WebAuthn Level 3 PRF extension: https://www.w3.org/TR/webauthn-3/#prf-extension
- HKDF: RFC 5869
- AES-GCM: NIST SP 800-38D
- This codebase's existing security work: `docs/security/threat-model.md`, `docs/security/controls.md`
