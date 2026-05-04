# Handoff — auth/encryption rebuild

This is the entry point for any session picking up the
auth/encryption/permissions rebuild. Read this first, then the
canonical design.

## TL;DR

We are mid-rebuild of the auth, encryption, and permission surface.
A long design conversation produced a consolidated v5 spec and a
queued v6 architecture phase. **Phase 8a primitives + 8b.1
server-side key plumbing already shipped** (behind the
``TBDTASK_WEBAUTHN_ENABLED`` feature flag, no-op when off). The next
implementation step is **Phase A** of v5: permission catalog +
quorum plumbing.

No production user has touched the new auth path yet. The flag is
off. Rolling back v5/v6 is a flag flip plus ``alembic downgrade``.

## Where the canonical design lives

- ``docs/auth-and-encryption-v5.md`` — the policy-layer spec.
  Two-tier data model, admin quorum (floor 3 holders of
  ``org.recover``), co-admin recovery with out-of-band code
  confirmation, two-party + 24h cooling-off for boundary-shifting
  actions, permission wizard, permission-derived nav, hardening
  checklists, and the queued **v6 architecture phase** (split-host +
  cross-origin iframe viewer + independently signed crypto/WASM
  module). Implementation surface laid out as Phases A–I.
- ``docs/security-architecture.md`` — the original lower-level
  security spec. Crypto primitives, wire format, AAD construction,
  KEK rotation, threat model. v5 builds on top; primitives are
  unchanged.
- ``docs/phase-8a-implementation.md`` — historical record of what
  shipped in 8a (passkey enrollment + step-up gates +
  enrollment-redirect middleware). UX framing here is superseded by
  v5; the schema and route shapes still apply where untouched.
- ``docs/phase-8b-implementation.md`` — historical record of 8b
  split (8b.1 server-side key plumbing shipped, 8b.2 client-side
  PRF/ECDH wrap-on-enrollment is pending).
- ``docs/phase-8a-smoke-test.md`` — Mac smoke-test guide for the
  current 8a code path. Will need a v5 rewrite once Phases A–D land
  (the ``/passkey/register`` page goes away, ``/passkey/manage``
  becomes ``/account/devices``, the soft-enrollment banner is
  dropped).

## Branch and current state

- **Branch:** ``claude/fix-failing-tests-C5Vid`` (the long-lived
  feature branch for this rebuild).
- **Tests:** 346 passing, 4 skipped (postgres-only). ``ruff`` clean,
  ``ruff format`` clean.
- **Feature flag:** ``TBDTASK_WEBAUTHN_ENABLED=1`` to enable. Off in
  prod, off in default test config.
- **Local smoke testing:** ``TBDTASK_DEV_LOGIN=1`` plus
  ``TBDTASK_INSECURE_LOCAL_COOKIES=1`` enables the ``/dev-login``
  shortcut. See ``docs/phase-8a-smoke-test.md``.

Recent commits (newest first):

```
a5dd3f6  Add v5 auth + encryption design doc
e540524  Add /dev-login + smoke-test guide for local 8a verification
4d999e1  Phase 8b.1: server-side key hierarchy plumbing
a788414  Phase 8a polish: drawer link + soft-enrollment banner
853316a  Phase 8a: passkey routes, step-up gates, enrollment redirect
15637bf  Phase 8a: WebAuthn primitives + step-up grants
```

## What's already shipped (unchanged by v5/v6)

- WebAuthn primitives in ``app/auth/webauthn.py``: PRF probe,
  challenge handling, registration + assertion flows. Captures only
  the boolean ``prf_supported`` fact, never the PRF bytes.
- Step-up grants in ``app/auth/step_up.py``: single-use, scoped to
  purpose, TTL-windowed. Will be **renamed** to
  ``require_biometric(purpose, tier=...)`` in Phase D.
- ``StepUpRequired`` exception with HTML→redirect / JSON→403
  content negotiation in ``app/main.py``. Will be renamed.
- ``KeyVault`` abstraction in ``app/auth/key_vault.py``. Local file
  vault for dev, AWS/GCP KMS stubs. **In v5 the operator key serves
  Tier A only** — its Tier B unwrap path
  (``unwrap_org_kek_via_operator``) is deleted in Phase A.
- ``KEK_org_master`` plumbing in ``app/auth/key_hierarchy.py``:
  bootstrap, rewrap, rotation. **Bootstrap is reworked in Phase A**
  to fire on quorum-cross (3 holders of ``org.recover``) rather
  than on first credential.
- Models: ``UserWebauthnCredential``, ``StepUpGrant``,
  ``OrgMasterKey``, ``CredentialKey``. Migrations
  ``e8a1c0d34f5b_phase8a_webauthn`` and
  ``f9b2d04ce8a1_phase8b1_key_hierarchy``. All additive,
  ``alembic downgrade`` clean.
- Audit log via ``AuthEvent`` with structured ``kind`` strings.
- Permission catalog in ``app/auth/permissions.py``. **In Phase A
  we add ``org.recover`` and drop ``alerts.view``.**
- ``/dev-login`` shortcut for local smoke testing (gated by both
  ``TBDTASK_DEV_LOGIN=1`` and ``TBDTASK_INSECURE_LOCAL_COOKIES=1``).

## What's planned

Each phase is meant to be a single commit (or two) on the feature
branch. Each is revertable on its own.

### Phase A — catalog and quorum plumbing
1. Add ``org.recover`` to the permission catalog.
2. Drop ``alerts.view`` from the catalog as redundant.
3. Add ``min_recoverers`` column to ``Organization``, default 3,
   floor 3 (migration).
4. Add ``tier_for_session(perms) -> Literal["A", "B"]`` helper.
5. Tests for new permission, helper, migration roundtrip.

### Phase B — UI plumbing
6. Drawer template iterates permissions instead of hardcoding
   links.
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
16. Implement co-admin recovery flow with one-click submission,
    out-of-band code confirmation, drawer badge,
    ``/admin/recovery-requests``.
17. Strip "passkey" / "encryption" / "key" / "step-up" /
    "zero-knowledge" / "operator key" / "quorum" / "two-party" from
    user-facing strings.

### Phase E — two-party actions
18. ``two_party_requests`` table + lifecycle.
19. Per-kind handlers for org destroy, admin promote/remove,
    recovery-permission grant/revoke, full export, bulk archive,
    manual key rotation, lowering quorum target.
20. Notification fan-out (email + in-app + optional SMS).
21. Pending-actions UI panel.

### Phase F — permission wizard
22. ``/admin/access-wizard`` rendering.
23. Wizard at invite time; same view from member management.
24. Translate wizard rows to permission codes; route boundary
    grants through two-party flow.

### Phase G — build integrity
25. ``/security/build`` page.
26. SRI hashes generated at build time, injected into templates.
27. Service worker version pinning + explicit update prompt.
28. Strict CSP audit (already mostly clean — verify).
29. Signed build manifest published; server verifies before
    serving.
30. Trusted Types policy.
31. Strong HTTP headers (HSTS preload, restrictive CORS,
    Permissions Policy, COOP/CORP, secure cookies).
32. Kill plaintext observability (type-tagged Tier B fields + CI
    lint test).

### Phase H — tests
33. IV uniqueness, AAD swap, wrong-key tests.
34. Plaintext-not-logged lint test.
35. Permission-bypass per-route tests.
36. Tier A/B route separation tests.
37. Recovery approval flow with OOB code mismatch path.
38. Two-party flow with self-approve rejection and quorum
    reservation.
39. Operator key Tier A audit per-use tests.

### Phase I — v6 architecture (deferred)
40. Provision ``secure-viewer.tbdtask-static.com`` origin + TLS.
41. Split the build pipeline; separate signing key for the viewer.
42. Viewer bundle scaffolding: WebAuthn PRF, ciphertext fetch,
    decrypt, render, edit primitives.
43. Cross-origin iframe wiring with correct sandbox flags +
    Permissions Policy delegation for ``publickey-credentials-get``.
44. postMessage protocol: schema-versioned, nonced, exact
    ``targetOrigin``, no plaintext-returning verbs.
45. Migrate Tier B templates into the viewer.
46. Audit-log provenance: bundle hash + manifest version on both
    sides.

## Decisions baked in (most contested ones)

These came out of multiple rounds of review and shouldn't be
reopened without justification:

1. **No silent OAuth-only Tier B re-grant.** Co-admin approval is
   the only Tier B recovery path.
2. **No operator-mediated Tier B recovery.** Operator key serves
   Tier A only.
3. **Admin quorum: floor of 3 distinct people holding
   ``org.recover``.** Solo founders run Tier A only until they
   invite two co-admins.
4. **All admin accounts equal.** No owner tier above admin.
5. **Login never blocks on Tier B key state.** OAuth + Tier A
   always work; Tier B unavailability surfaces as an inline banner.
6. **No "zero-knowledge" claim anywhere.** PWA threat model
   honestly acknowledges malicious-server-JS as a real risk; v6
   addresses it architecturally.
7. **Every Tier B sign-in reprompts for biometric.** No
   remember-this-device, no PRF caching across tab close.
8. **Two-party + 24h cooling-off** for catastrophic /
   boundary-shifting actions.
9. **Out-of-band code confirmation** on every recovery approval.
10. **No fast-path for compromise in v1.** Suspect-compromise
    scenarios use suspend-session + revoke-credential, not the
    two-party path.
11. **Permission assignment is wizard-first.** Raw permission grid
    is behind an "Advanced" toggle.

## How to resume

If you're picking this up cold:

1. **Read** ``docs/auth-and-encryption-v5.md`` end to end. The
   "Goal", "Honest framing", and "Decisions baked in" sections at
   the top and bottom are load-bearing.
2. **Skim** ``docs/security-architecture.md`` for the crypto
   primitives v5 builds on (wire format, AAD, key hierarchy,
   threat model).
3. **Verify** the branch is clean: ``git status`` on
   ``claude/fix-failing-tests-C5Vid``.
4. **Run the test suite** to confirm baseline:
   ``pytest`` should report 346 passing, 4 skipped.
5. **Start Phase A.** It's the smallest, most foundational change:
   permission catalog edits + ``min_recoverers`` migration +
   ``tier_for_session`` helper + tests. Single commit, easy to
   review, easy to revert.

Each subsequent phase builds on the previous. If something doesn't
fit cleanly, stop and revisit the spec rather than papering over
it — most "this is awkward to implement" moments in this rebuild
have been spec problems, not code problems.

## Open items not yet decided

These didn't come up in the design conversation explicitly and
probably need a quick check before Phase E or Phase G:

- **SMS as a notification channel for two-party cooling-off** — the
  spec says "optionally via per-org SMS policy" but there's no
  provider chosen. Twilio? Skip SMS in v1, email + in-app only?
- **High-risk org mode requiring two approvers on recovery** —
  spec mentions it as a knob; default value and UI affordance not
  specified.
- **Bulk-destructive threshold default of 10** is a guess. Maybe
  surface this in the org settings page rather than baking in the
  default.
- **Operator key audit aggregate counters cadence** — daily? hourly?
  per-request? Probably daily rollup is fine; confirm before
  building.
- **Trusted Types violation reporting** — collect on the server, or
  surface only in dev console? Probably both, with opt-in
  reporting URL configured per-org.
