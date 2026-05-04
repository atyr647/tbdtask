# tbdtask — auth, encryption, and visibility design (v5)

This is the canonical design for the auth + encryption + permission
surface as of v5. It supersedes the UX framing in
``docs/phase-8a-implementation.md`` and ``docs/phase-8b-implementation.md``;
those remain as the implementation record for already-shipped phases
(8a primitives, 8b.1 server-side key plumbing). The lower-level crypto
spec in ``docs/security-architecture.md`` (wire format, AAD, KEK
rotation primitives) still applies — v5 changes the policy layer on
top of it, not the primitives.

## Goal
As secure as possible, encrypted without feeling encrypted. A brand-
new user signs in via OAuth and gets to work without ever seeing the
words "passkey," "encryption," "key," or "recovery code."

## Honest framing (read this first)
- **Tier A** is server/operator recoverable. Operator key holders can
  decrypt it through a deliberate, audited process. This is the cost
  of "log in and just work" for line users.
- **Tier B** is client-side encrypted, with no operator escape hatch.
  Recovery is exclusively through approval by another admin holding
  ``org.recover``. Because the org cannot operate Tier B without at
  least 3 such admins, recovery always has a path.
- **No "zero-knowledge" wording** appears in this spec or UI. The
  server still ships the JS that handles plaintext after PRF unlock; a
  malicious or compromised server could exfiltrate during the active
  window. Defenses are build integrity (SRI, signed bundles, strict
  CSP), not a cryptographic claim.

## Auth flow
- Sign in with Google / Microsoft / Apple (OAuth).
- On a device the user hasn't used before, the OAuth completion page
  triggers a single OS biometric prompt ("Use Touch ID to continue").
  This is the WebAuthn registration ceremony, but it's never named as
  one — to the user it looks like the same dialog the OS shows for
  unlocking 1Password or approving a purchase.
- Subsequent sign-ins on the same device:
  - **Tier A session** (user holds only self-scope perms): OAuth
    only, no biometric prompt.
  - **Tier B session** (user holds any cross-user view perm): OAuth
    plus a biometric tap to derive PRF, **every sign-in, no
    exceptions**. PRF-derived keys live in tab RAM only and never
    survive tab close, browser restart, sign-out, or session expiry.
    There is no "remember this device" affordance.
- **Login never blocks on Tier B key state.** A returning admin
  whose device is no longer wrapped for Tier B still signs in via
  OAuth, lands on ``/today``, sees their own week. Tier B
  unavailability surfaces inside the app as an inline banner with a
  one-click access request, not a denied login.
- No "set up a passkey" page, no banner, no opt-in/opt-out.

## Step-up tiers
Biometric prompts on top of the per-sign-in unlock are tiered:
- **Routine** — no prompt. Most reads/writes (mark task done, add a
  note, edit own absence).
- **Sensitive** — re-prompt if the last verification is older than
  ~30 minutes; the resulting grant is reusable inside the window.
  Examples: editing personnel, lock/amend a worklist.
- **Destructive (reversible)** — single-use re-prompt at action time.
  Examples: archive a single personnel record, suspend a member,
  archive a workcenter.
- **Two-party (catastrophic / boundary-shifting)** — initiator + one
  co-approver + 24h cooling-off + multi-channel notification.
  Detailed below.

## Two-tier data model
Encryption boundary aligns with role surface, not abstract
sensitivity.

- **Tier A — server-encrypted at rest, operator-recoverable.** The
  user's own week of tasks, their own dashboard, alerts about them.
  Tiny blast radius. No biometric needed to decrypt. Lost device →
  sign back in → you're working again. Operator-key access to Tier A
  is audited per use.
- **Tier B — client-side encrypted, PRF-gated, co-admin recoverable
  only.** All cross-user views: personnel roster, qual matrix,
  absence calendar, org-wide worklists, alerts queue, admin pages.
  Real PII, real blast radius. Decryption requires an enrolled
  device's WebAuthn PRF output. Recovery to a new device requires
  approval from another admin in the same org.

## Org bootstrap and admin quorum
Tier B for an org is gated behind a quorum of admin people holding
the recovery-approval permission.

- **Quorum floor: N = 3 distinct holders of ``org.recover``.** Per-
  org configuration may raise this above 3, never below.
- **Quorum is over distinct UserAccounts holding ``org.recover``.**
  Multiple devices held by the same person count as one. Three
  different humans must each hold the permission for an org to
  operate Tier B.
- **``KEK_org_master`` is generated on the enrollment that crosses
  quorum**, wrapped under each founding ``org.recover`` holder's
  current credential. Until then the org runs Tier-A-only.
- **Solo founders run Tier A only.** A one-person org has
  ``/today``, dashboard, and self-scope alerts — and that's it.
  Adding Tier B requires inviting two more admins who accept and
  enroll.
- **Each new admin's first credential wraps in via 8b.2 ECDH
  transport from an existing admin's credential.** No operator-
  mediated wrap.
- **All admin accounts equal.** No "owner" tier above admin.
- **Quorum floor is enforced at every relevant transition.** Cannot
  revoke ``org.recover`` if it would drop the org below 3. Cannot
  remove or demote an admin if it would drop the org below 3. UI
  says "Add another admin first."

## Key hierarchy
```
KEK_credential   ← PRF → HKDF (browser tab RAM only, never persisted)
    │ unwraps
    ▼
KEK_org_master   ← one per org, wrapped per credential ONLY.
    │              No operator-key wrap. No server-side recovery.
    │ unwraps
    ▼
KEK_scope / DEK  (Tier B columns)
```
- The operator key (``KeyVault``) survives, but only as the data-at-
  rest key for Tier A columns. It has no role in Tier B.
- AAD-bound AEAD envelope wrapping (AES-256-GCM), not RFC 3394.
- Wire format: ``[v:1][algo:1][key_id:16][iv:12][ct:N][tag:16]``.
- Random 96-bit GCM IVs for every encryption operation; tests assert
  uniqueness across runs.

## Recovery
The full matrix:

| Scenario | Login | Tier A | Tier B |
|---|---|---|---|
| Line user, lost device | OAuth + biometric → in | Immediate | n/a |
| Admin, lost device | OAuth → in (no biometric needed for Tier A) | Immediate | Inline banner + one-click "Send request" → any other ``org.recover`` holder taps Approve → ECDH wrap → biometric prompt at next entry to a Tier B page → in |
| Removing/demoting an admin | n/a | n/a | Two-party + 24h flow; on execution ``KEK_org_master`` rotates and remaining credentials get re-wrapped; quorum floor enforced |

OAuth and Tier A never block on Tier B key state.

**UX flow on lost-device admin sign-in:**
1. OAuth completes; user lands on dashboard normally.
2. One-time inline notification: "This device hasn't been verified
   for personnel records yet. Send a request to your team?"
3. Single button: "Send request." No form.
4. Every other ``org.recover`` holder sees a drawer badge and a
   ``/admin/recovery-requests`` row showing **device, browser,
   approximate location/IP, time, requesting user, and reason.**
5. **Out-of-band code confirmation:** the requester's screen shows a
   short code (e.g. ``482-913``) immediately after submission. The
   approver's UI says "Confirm with [name] that they see the code
   ``482-913`` before approving." This is the social-engineering
   defense. The code is bound to the request row and rotates if the
   request is reissued.
6. On approve, the approver's browser performs the ECDH wrap to the
   requester's new credential.
7. On the requester's next page navigation, Tier B becomes
   available; biometric prompt fires.
8. Pending Tier B pages show "Waiting for verification from a
   teammate" — no 403, no scary error.

**Suspicious recovery types** trigger an automatic 24-hour cooling-
off even with approval: ``device_stolen``, or any request where the
IP geolocates outside the org's typical region. The request still
fires after the window unless cancelled.

**Subject-visible permanent audit:** every recovery request — created,
approved, denied, cancelled — is permanently visible to the subject
on their own ``/account/security`` page. Admins cannot hide rows
from the subject's view.

**Structured reason enum:** ``lost_device``, ``device_replaced``,
``device_stolen``, ``account_locked``, plus an optional bounded
free-text note.

## Two-party actions
Catastrophic and boundary-shifting actions all go through the same
flow: initiator + one co-approver + 24h cooling-off + multi-channel
notifications.

**What counts as two-party:**
- Org destroy.
- Lower the org's quorum target (cannot go below 3).
- Export full org data.
- Remove a member (suspension is a separate, single-party action).
- Promote a member to admin.
- Remove or demote an admin.
- Grant or revoke ``org.recover`` on an admin.
- Bulk destructive operation: any single submission that archives
  more than N records (default N=10, per-org configurable).
- Manual ``KEK_org_master`` rotation outside the auto-rotate path.

**Flow:**
1. **Initiator** holds the relevant permission and submits the
   request, naming the subject (or scope).
2. **Co-approver** — any other holder of the same permission taps
   Approve. The initiator cannot self-approve.
3. **24-hour cooling-off** — once co-approved, the change is queued.
   During the window:
   - Pending changes appear in every admin's drawer with a countdown.
   - Email + in-app notification fires to all admins, to the subject
     (where one exists), and via SMS if per-org policy is configured.
   - Any qualified admin, the initiator, or the subject can cancel.
   - Subject can flag as unauthorized — auto-cancels and triggers a
     security event on the initiator's session.
4. **Execution** at end of window. Auto-rotation, ECDH wrap-on-
   enrollment, member-row removal run as appropriate.

**Quorum reservation** at submission time: if executing the request
would leave the org below the 3-floor, reject with "Add another admin
first." Stacked pending requests: first-write-wins.

**Single-party actions (for contrast):** suspend a member, archive a
single record, lock or amend a worklist, archive a workcenter, edit
org settings, issue/revoke an invite. These use the destructive-tier
biometric step-up.

**No fast-path for compromise in v1.** Suspect-compromise scenarios
use suspend-session + revoke-credential (single-party, immediate),
not the two-party path.

## Permissions and roles
- **Permission catalog is a closed set in code.** Each is
  ``<resource>.<action>``.
- **Roles are fully admin-configurable per org.** Custom name, custom
  permission bag, optional workcenter scoping.
- **No seed role templates.** The org-create flow seeds only an
  admin role granting every permission; founding admins all hold it.
- **Page visibility, nav drawer, and tier are all derived from
  permissions held — never from role name.**
- **The admin UI for granting permissions is a wizard, not a raw
  permission grid.** See next section.

## Permission wizard
Users assigning access — at invite time, or later from member
management — see a single natural-language wizard titled
**"What should this person be able to do?"** Each row is a page or
capability with sensible options; the wizard translates to the
underlying permission codes.

| Row | Options |
|---|---|
| Their own week and tasks | **Always on** (membership floor) |
| Personnel records | Off / View / Edit / Edit + archive |
| Qualifications | Off / View / Edit / Edit + archive |
| Absences | Off / View / Edit / Edit + archive |
| Tasks & templates | Off / View / Edit / Edit + archive |
| Worklists | Off / View / Edit / Edit + lock / Edit + lock + amend / Full + archive |
| Org admin (members, roles, workcenters, invites) | Off / On |
| Approve teammate access requests | Off / On (= ``org.recover``) |
| Workcenter scope | Optional dropdown — restricts above to one workcenter |

- Same wizard at invite time and from ``/admin/members/<id>/access``.
- Granting ``org.recover`` or ``org.admin`` routes through the
  two-party flow because both shift the security boundary.
- Other rows are single-party.
- "Advanced — show raw permissions" toggle for power users.

## Self-scope floor (Tier A, ungated)
Every active member of an org gets these regardless of role:
- ``/today`` — their tasks for the current week, filtered to
  ``user_id == self``.
- ``/`` — dashboard summarizing their own week.
- Alerts that pertain to them.

These pages query plaintext columns only.

## Page → permission → tier matrix
| Page | Gating permission | Tier |
|---|---|---|
| ``/today``, ``/`` (self-filtered) | none — membership only | A |
| ``/alerts`` | none — membership only; contents filtered by topic perms | A/B per row |
| ``/personnel`` | ``personnel.view`` | B |
| ``/quals`` | ``quals.view`` | B |
| ``/absences`` | ``absences.view`` | B |
| ``/worklists`` | ``worklists.view`` | B |
| ``/tasks``, ``/templates`` | ``tasks.view`` | B |
| ``/admin/*`` | ``org.admin`` and friends | B |

Drawer iterates permissions: ``if has_perm("quals.view"): show
"Qualifications"``.

**Tier-for-session rule:** if the user holds *any* permission outside
the self-scope set, this session is Tier B and the per-sign-in
biometric unlock activates. Sessions with only self-scope perms stay
Tier A.

## Alerts behavior
Alert visibility derives from resource permissions, not a dedicated
alerts permission.

- Each alert has a ``topic`` (the resource it pertains to) and
  optional ``subject_user_id``.
- Visible if subject OR holds matching ``<topic>.view``.
- ``alerts.view`` is removed from the catalog as redundant.
- ``alerts.triage`` and ``alerts.act`` remain as button gates.
- Tier follows topic.

## What's user-visible vs internal
- **Visible:** "Sign in," "Use Touch ID to continue" (OS dialog),
  "Trusted devices," "Send request" / "Waiting for verification from
  a teammate," "Invite a co-admin to enable personnel records," the
  permission wizard, the pending-actions panel.
- **Internal only — never in UI:** "passkey," "WebAuthn," "PRF,"
  "encryption," "key," "KEK," "recovery code," "step-up,"
  "credential," "zero-knowledge," "operator key," "quorum,"
  "two-party."

## Threat-model limits (honest)
- **Tier A** is recoverable by anyone with the operator key. Per-use
  audit events fire whenever the operator key decrypts user data.
- **Tier B** has no standing server-side or client-side plaintext
  access. PRF-derived keys exist only inside an active browser tab.
  Every Tier B sign-in reprompts.
- **Tier B server-side recovery does not exist.** Recovery to a new
  device requires a co-admin's PRF.
- **OAuth account compromise alone does not give Tier B access.** A
  co-admin must still approve the new device.
- **Compromise of one admin account does not enable durable damage.**
  Two-party + 24h cooling-off prevents silent self-elevation,
  co-admin removal, data export, org destroy, or quorum lowering.
- **Malicious/compromised server** could ship altered client JS that
  exfiltrates plaintext during an active Tier B window. Defense is
  build integrity (next section), not a cryptographic claim.

## Defense-in-depth hardening checklist

### Build integrity
- Strict CSP: ``script-src 'self'``, ``style-src 'self'``, no
  ``unsafe-inline``, no ``unsafe-eval``.
- No third-party scripts. All vendored or none.
- SRI hashes on every script and style tag.
- Immutable bundle URLs (``/static/app.<contenthash>.js``).
- Signed build manifest published at ``/security/manifest.json``;
  service worker pins the expected manifest hash.
- Service-worker build pinning with explicit user-prompted updates
  (no silent code swap). Updates defer if the user is in an active
  Tier B session.
- Public ``/security/build`` page showing the running bundle hash,
  build timestamp, and links to the manifest. Anyone can verify the
  client-side code they're executing.
- Server verifies the manifest signature before serving any bundle
  it claims to know.
- Trusted Types policy on the main app document (and on the viewer
  origin once v6 lands). Eliminates string-to-DOM XSS sinks.
- Strong HTTP headers everywhere: HSTS preload, restrictive CORS
  (allowlist app origin only), restrictive Permissions Policy
  (``publickey-credentials-get`` delegated only to the viewer
  origin once it exists), ``Cross-Origin-Opener-Policy: same-origin``,
  ``Cross-Origin-Resource-Policy: same-origin``, secure + HttpOnly
  + SameSite=Lax cookies, no mixed content.
- Every Tier B audit event records the running bundle hash and
  manifest version. Provenance per action; mismatch with the signed
  manifest tail is a security event.

### Runtime memory hygiene
- No persistent plaintext cache on the client. ``localStorage`` /
  IndexedDB / OPFS hold only ciphertext.
- Tier B keys live in a single in-memory object scoped to the
  current tab.
- Idle timer wipes Tier B keys after N minutes of inactivity
  (default 10) regardless of session expiry.
- ``visibilitychange`` and ``beforeunload`` events trigger
  immediate wipe.
- Decrypted plaintext for a row is held only as long as the view
  needs it; explicitly cleared on navigation.
- ``/account/devices`` always offers a "Sign out everywhere now"
  button that revokes every credential and sessions.
- **Kill plaintext observability.** Tier B field types are tagged so
  the type-checker rejects passing them to logger calls, error
  reporters, metrics, traces, audit-event detail, analytics, or any
  serialization sink not on an explicit allowlist. The single
  exception is the encrypted-column wrapper, which only ever sees
  ciphertext at rest. CI lint test scans audit-event detail JSON and
  log statements for known-Tier-B field names.

### Crypto discipline
- Web Crypto only client-side. No JS crypto libraries.
- PyCA cryptography only server-side. No homegrown primitives.
- Fixed wire format (above). Backward-compat is a version byte.
- Random 96-bit GCM IVs every encryption.
- AAD on every encrypted field — typed, structured, includes
  ``org_id`` and ``key_version`` so cross-key-version replay fails.
- PRF output is **never** stored, logged, transmitted, or sent
  through the audit pipeline. Only the boolean fact "PRF supported
  for this credential" is captured.
- Schema-level encrypted column types (``EncryptedColumn``) so
  developers don't manually call wrap/unwrap. Type-checker enforces
  AAD argument is passed.

### Recovery social-engineering mitigations
- Approval UI shows: device, browser, approximate location/IP,
  time, requesting user, reason, audit history of prior requests
  from this user.
- All ``org.recover`` holders get notified.
- Out-of-band code confirmation: requester sees a 6-digit code
  (e.g. ``482-913``) immediately; approver's UI requires confirming
  the code matches what the requester reads back.
- 24h cooling-off on suspicious request types (``device_stolen`` or
  unusual location).
- Optional per-org "high-risk mode" requires two approvers instead
  of one.
- Any admin can cancel a pending recovery.
- Subject-visible permanent audit on ``/account/security``; admins
  cannot redact.

### Tests
At minimum:
- IV uniqueness across N encryptions (no collision in 1M trials).
- AAD swap fails decryption (org_id mismatch, key_version mismatch).
- Wrong-key fails decryption with no plaintext leak in error path.
- Plaintext never appears in logs (lint test scans audit-event
  detail fields).
- Permission-bypass attempts on every gated route return 403, not
  500 or 200.
- Tier A vs Tier B route separation: Tier A endpoints reject
  encrypted-column queries; Tier B endpoints reject session that
  hasn't passed biometric unlock.
- Recovery approval flow: out-of-band code matches; mismatched code
  blocks approval; 24h delay enforces; cooling-off cancels propagate.
- Two-party action flow: initiator can't self-approve; quorum
  reservation rejects sub-floor cases; cancellation propagates.
- Operator key Tier A read emits per-use audit event.

### Supply chain / build pipeline
- Signed commits and tags (GPG or sigstore).
- Protected branches, mandatory reviews on the deploy branch.
- Least-privilege deploy tokens; short-lived credentials only.
- Artifact signing (cosign or equivalent) on every published bundle.
- Reproducible-ish builds: pinned base images, lockfile-only
  dependency resolution.
- Dependency lockfiles checked in; SCA scanning on every PR.
- Secret scanning on every PR and on push.
- Separate manual approval gate for production deploys.
- Server verifies signed build manifests before serving; mismatch
  fails closed.

## v6 architecture: split-host + cross-origin iframe viewer

v6 is a future architecture phase that materially raises the bar
against malicious-server-JS. It is *not* required for v5 to ship; v5
is correct and useful on its own. v6 is a follow-up that swaps the
client-side topology for Tier B rendering.

**The model:**

- ``app.tbdtask.com`` — the normal PWA shell + API. Hosts everything
  except Tier B plaintext rendering.
- ``secure-viewer.tbdtask-static.com`` — separate origin that serves
  only the immutable, independently-signed crypto/viewer bundle.
  Static, cacheable, content-addressed, no API.

**Tier B plaintext lives only inside the cross-origin iframe** served
from the secure-viewer origin. The PWA shell embeds the iframe and
talks to it through a tightly-locked-down ``postMessage`` protocol;
because the iframe is genuinely cross-origin, the same-origin policy
prevents the shell from reaching into the iframe's DOM or JS heap.

```
app.tbdtask.com
  └── normal PWA shell (forms, navigation, lists of identifiers)
      └── <iframe
            src="https://secure-viewer.tbdtask-static.com/viewer"
            sandbox="allow-scripts"
            allow="publickey-credentials-get"
          ></iframe>
            ↑ this iframe handles:
              - WebAuthn PRF unlock
              - ciphertext fetch
              - decrypt
              - render of sensitive fields
              - encryption of edits
```

**The viewer fetches its own ciphertext.** The shell tells it "show
person 123"; the viewer authenticates to the API directly and pulls
ciphertext. The shell never holds plaintext or decryption-relevant
state.

**postMessage protocol discipline (mandatory):**
- Exact ``targetOrigin`` on every send. Never ``*``.
- Every message carries: schema version, message id, nonce,
  request-expiration timestamp.
- Schema-validated on receipt; unknown fields rejected.
- No ``GET_PLAINTEXT``-style message exists. Allowed shell→viewer
  verbs: ``LOAD_RECORD``, ``CLEAR``, ``LOCK``. Allowed viewer→shell
  verbs: ``READY``, ``HEIGHT_CHANGED``, ``DIRTY``, ``SAVE_COMPLETE``.
- Responses to ``LOAD_RECORD`` carry no field data, only a status.

**Independently signed crypto/WASM module.** The viewer bundle and
its WASM crypto are signed by a separate signing key with its own
release cadence. The signed manifest pins the exact SHA-256 hashes.
Tier B decrypt refuses to run if the loaded bundle hash does not
match the signed manifest.

**Updates require explicit user acceptance** for users with Tier B
permissions. The shell can refresh silently. The viewer never
silently swaps.

**Audit-log provenance:** every Tier B action records bundle hash +
manifest version on both sides (shell and viewer). Mismatches raise
a security event.

**What v6 protects against** (vs v5):
- Compromise of ``app.tbdtask.com`` no longer directly leaks Tier B
  plaintext. A malicious shell JS can ask the viewer to load records
  but cannot exfiltrate the rendered text — same-origin policy blocks
  DOM read; postMessage has no plaintext-export verb.
- Compromise of the app deploy pipeline doesn't automatically
  compromise the viewer (different signing key, different host).

**What v6 does not protect against:**
- Compromise of the viewer signing key or pipeline. That remains
  game-over for Tier B confidentiality.
- UI tricks from the shell — overlays, fake controls, social-engineer
  the user into approving things. Mitigated by the viewer rendering
  approval-critical UI itself, not the shell.
- Native-level threats (compromised browser, OS, hardware).

**Effort estimate:** ~3-5 focused days for the first end-to-end Tier
B view through the iframe (DNS, TLS cert, build pipeline split,
manifest signing, postMessage handshake, viewer scaffolding,
WebAuthn-in-iframe smoke test). Another few days to migrate every
Tier B template (personnel notes, absence reasons, generated docs,
audit detail, etc.).

## Implementation surface (what changes from current code)

### Phase A — catalog and quorum plumbing (this commit)
1. Add ``org.recover`` to the permission catalog.
2. Drop ``alerts.view`` from the catalog as redundant.
3. Add ``min_recoverers`` column to ``Organization``, default 3,
   floor 3 (migration).
4. Add ``tier_for_session(perms) -> Literal["A", "B"]`` helper.
5. Tests for new permission, helper, migration roundtrip.

### Phase B — UI plumbing
6. Drawer template iterates permissions instead of hardcoding links.
7. ``Depends(require(...))`` on ungated page routes.
8. Drop existing seed role templates except ``org_owner``-equivalent.

### Phase C — alerts model
9. Add ``topic`` and ``subject_user_id`` columns.
10. Backfill existing rows from alert kind.
11. Visibility query: subject OR ``<topic>.view``.

### Phase D — recovery flow + biometric tier rename + devices page
12. Drop ``/passkey/register`` page; inline biometric into OAuth
    completion.
13. Rename ``require_step_up`` → ``require_biometric(tier=...)``.
14. Drop ``TBDTASK_WEBAUTHN_ENFORCED_AFTER`` env var.
15. Replace ``/passkey/manage`` with ``/account/devices``.
    Auto-name from User-Agent.
16. Implement co-admin recovery flow with one-click submission, OOB
    code confirmation, drawer badge, ``/admin/recovery-requests``.
17. Strip "passkey" / "encryption" / "key" / "step-up" /
    "zero-knowledge" / "operator key" / "quorum" / "two-party" from
    user-facing strings.

### Phase E — two-party actions
18. ``two_party_requests`` table + lifecycle.
19. Per-kind handlers for org destroy, admin promote/remove,
    recovery-permission grant/revoke, full export, bulk archive,
    manual key rotation, lowering quorum target.
20. Notification fan-out (email + in-app + optional SMS).
21. Pending-actions UI panel with countdowns, Approve/Cancel/Flag-
    as-unauthorized.

### Phase F — permission wizard
22. ``/admin/access-wizard?subject=<user_or_invite>`` rendering.
23. Wizard at invite time; same view from member management.
24. Translate wizard rows to permission codes; route boundary
    grants through two-party flow.

### Phase G — build integrity
25. ``/security/build`` page with bundle hash + manifest link.
26. SRI hashes generated at build time, injected into templates.
27. Service worker version pinning + explicit update prompt.
28. Strict CSP audit (no inline anywhere).
29. Signed build manifest published; server verifies before serving.

### Phase H — tests
30. IV uniqueness / AAD swap / wrong-key tests.
31. Plaintext-not-logged lint test.
32. Permission-bypass per-route tests.
33. Tier A/B route separation tests.
34. Recovery approval flow with OOB code.
35. Two-party flow with self-approve rejection and quorum
    reservation.
36. Operator key Tier A audit per-use tests.

### Phase I — v6 architecture (deferred, future phase)
37. Provision ``secure-viewer.tbdtask-static.com`` origin and TLS.
38. Split the build pipeline: separate signing key for the viewer
    bundle; separate publish workflow.
39. Scaffold the viewer bundle: WebAuthn PRF, ciphertext fetch,
    decrypt, render, edit primitives. Strict CSP on the viewer
    origin (no third-party scripts, no analytics, no dynamic
    imports).
40. Cross-origin iframe wiring in the PWA shell:
    ``allow="publickey-credentials-get"``, ``sandbox="allow-scripts"``
    (test exact flags for WebAuthn PRF compatibility), responsive
    height via postMessage.
41. postMessage protocol: schema-versioned, nonce-bearing, exact
    ``targetOrigin``, no plaintext-returning verbs.
42. Migrate Tier B templates: personnel notes, absence reasons,
    generated docs, audit-event detail viewer.
43. Audit-log provenance: every Tier B action records bundle hash +
    manifest version on both sides.
44. Update the threat model and ``/security/build`` page to surface
    both bundles independently.

## What's already in place (Phase 8a + 8b.1, kept)
- WebAuthn primitives, PRF probe, server-side challenge handling.
- StepUpRequired exception + HTML/JSON content negotiation (will
  be renamed to BiometricRequired in Phase D).
- AuthEvent audit log with structured ``kind`` strings.
- KeyVault abstraction (LocalKeyVault for dev; KMS stubs) — kept
  for Tier A only.
- KEK_org_master plumbing: bootstrap, rewrap, rotation. Bootstrap
  reworked in Phase A to fire on quorum-cross.
- Migrations are additive and revertable.
- All gated behind ``TBDTASK_WEBAUTHN_ENABLED``; no-op when off.

## Decisions baked in
1. No silent OAuth-only Tier B re-grant.
2. No operator-mediated Tier B recovery.
3. Admin quorum: floor of 3 distinct people holding ``org.recover``.
4. All admin accounts equal; no owner tier above admin.
5. Login never blocks on Tier B key state.
6. ``org.recover`` is its own permission, separate from ``org.admin``.
7. Recovery requests carry a structured reason; default infers
   ``lost_device``.
8. Operator-key Tier A decrypts emit per-use audit events.
9. No "zero-knowledge" claim anywhere.
10. Every Tier B sign-in reprompts for biometric.
11. Two-party + 24h + multi-channel notification covers all
    catastrophic / boundary-shifting actions.
12. Reversible destructive actions stay single-party.
13. Subject of a two-party action can flag as unauthorized.
14. No fast-path for compromise in v1.
15. Permission assignment is wizard-first.
16. Out-of-band code confirmation on every recovery approval.
17. ``/security/build`` is a public endpoint; clients verify the
    manifest matches.
18. **v6 is queued, not part of v5.** The split-host + cross-origin
    iframe viewer + independently-signed crypto module is the
    next architectural phase after Phases A–H land. v5 ships
    valuable improvements without it; v6 raises the bar against
    malicious-server-JS specifically.
19. **No plaintext observability.** Tier B fields cannot flow into
    logs, traces, metrics, error reporters, audit detail, or
    analytics. Enforced at the type level + CI lint test.
20. **Audit provenance includes bundle hash + manifest version.**
    Per-action, both sides (shell and viewer once v6 lands).
21. **postMessage between shell and viewer (v6) is a strict
    protocol** — schema-versioned, nonced, expiring, no
    plaintext-export verbs.
