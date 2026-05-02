# Threat model

Maintained alongside the controls catalog. Each row names a specific
threat, the existing mitigation, and the test that locks the behaviour
in. New features add or update rows in the same diff.

## Tenancy

| Threat | Mitigation | Test |
|---|---|---|
| User in Org A reads Org B's data via SELECT | `TenantScopedMixin` + `do_orm_execute` listener filters every tenant-model query when a tenant context is active. Phase 3 adds Postgres RLS as belt-and-braces. | `test_phase0_tenancy.py::test_cross_org_read_denied_via_select` |
| Cross-org leak via `Session.get` (identity map cache) | Documented limitation in Phase 0; closed by Phase 3 RLS at the DB level. Test pins current behaviour. | `test_session_get_identity_map_caveat_phase3_will_close` |
| Forgotten `org_id` on a new model | Source-of-truth tuple `TENANT_SCOPED_TABLES` checked against the registry by an automated test. | `test_tenant_scoped_tables_list_matches_models` |

## Authentication

| Threat | Mitigation | Test |
|---|---|---|
| Provider login auto-merges to the wrong account by email collision | Email-based linking returns `LinkRequired` outcome; never silent merge. The user must explicitly re-auth with the existing provider. | `TestAccountResolution::test_email_match_with_different_provider_returns_link_required` |
| Apple private-relay address used to merge accounts | `normalize_userinfo` overrides Apple's `email_verified` to `False` for `@privaterelay.appleid.com` addresses. | `TestNormalizeUserinfo::test_apple_private_relay_marked_unverified` |
| Unverified email on one IdP linking to a verified-email account | Email-based linking only fires when the IdP returns `email_verified=True`. Unverified email creates a fresh account. | `TestAccountResolution::test_unverified_email_does_not_link` |
| Identity claim re-attached to a different user | Unique `(provider, subject)` constraint; `link_identity_to_user` raises if the identity already belongs elsewhere. | `TestAccountResolution::test_link_to_other_user_blocked` |
| OIDC callback replay / mix-up | Signed `state` payload includes provider name; callback rejects mismatched state. Authlib validates `nonce` against the id_token. | (manual review of callback path; integration test deferred) |
| OIDC callback CSRF | Signed `state` token tied to the redirect; expires in 10 min. | `TestOIDCState::test_invalid_state_rejected` |

## Sessions

| Threat | Mitigation | Test |
|---|---|---|
| Session token theft via XSS | `__Host-` prefix, `HttpOnly`, `Secure`, `SameSite=Lax`; strict CSP blocks inline scripts. | (CSP integration deferred to follow-up) |
| Stale permissions after suspension/demotion | Per-request membership status check. Setting `status='suspended'` invalidates the session on the next request without explicit revoke call. | `TestSessions::test_lookup_returns_none_when_membership_suspended` |
| Session not rotated on privilege change | `rotate()` revokes old + issues new id; called on login, logout, identity link/unlink, org switch. | `TestSessions::test_rotate_revokes_old_and_issues_new` |
| Idle session reused weeks later | 12h idle timeout + 30d absolute timeout enforced by `lookup_session`. | `TestSessions::test_idle_timeout`, `test_absolute_timeout` |
| Disabled user keeps active sessions | `lookup_session` refuses if `user_accounts.disabled_at` is set. Bulk revoke helper available for explicit cleanup. | `TestSessions::test_lookup_returns_none_when_user_disabled` |

## Cross-Site Request Forgery

| Threat | Mitigation | Test |
|---|---|---|
| Cross-site form submission triggers state change | Signed double-submit CSRF token bound to session id. Form + cookie must match AND signature must be valid for the active session. | `TestCSRF::test_token_for_other_session_rejected` |
| CSRF token reuse from another session | Token is HMAC-bound to the session id at issue time; rotation issues a fresh token. | `TestCSRF::test_token_for_other_session_rejected` |

## Invites

| Threat | Mitigation | Test |
|---|---|---|
| Leaked invite link redeemed by the wrong user | `intended_email` is mandatory; redemption refuses if the authenticated user's canonical email differs. | `TestInvites::test_create_*` + route-level `_invite_failure(reason='email_mismatch')` |
| Endlessly valid invite | Mandatory `expires_at`; admin UI capped at `MAX_INVITE_TTL_DAYS = 30`. | `TestInvites::test_create_rejects_ttl_over_cap` |
| Invite redeemed twice | `accepted_at` set on first success; subsequent redemptions return generic failure. | `TestInvites` + route logic |
| DB leak exposes redeemable invites | Tokens hashed (SHA-256) at rest. Raw token shown to admin once at issue, never persisted. | `TestInvites::test_create_returns_raw_token_only_once` |
| Token-existence probing | All redeem failures return identical generic message; reasons logged only to `auth_events`. | (route logic; HTTP-level test deferred) |

## Rate limiting

| Threat | Mitigation | Test |
|---|---|---|
| Auth-endpoint flood / credential stuffing | In-process per-IP fixed-window limiter at 10 attempts / 5 min on `/auth/{provider}/{login,callback}`. Phase 7 swaps to Redis. | `TestRateLimiter` |

## Out of scope (Phase 1)

* Audit log for tenant data writes — Phase 4.
* Notification leakage — Phase 4.
* Sensitive-info acknowledgment + soft-warn regex — Phase 6.

## Authorization (Phase 2)

| Threat | Mitigation | Test |
|---|---|---|
| Permission escalation via unknown code | Closed `Permission` enum; `is_known_permission()` rejects anything not declared. Custom roles can only grant from the catalog. | `test_phase2_admin.py::TestRoleCatalogDefence` |
| Workcenter-scoped bypass — user accesses sibling workcenter | `workcenter_ancestors()` walks the tree; permission check only matches grants on the target or an ancestor. | `test_phase2_authz.py::TestWorkcenterScopedDependency` |
| Cycle in workcenter hierarchy causes infinite loop | Ancestor walker stops at the first repeated id; re-parenting validates the new parent is not a descendant. | `test_phase2_admin.py::TestWorkcenterReparentValidation` |
| Last owner lockout — org loses its sole owner | `_last_owner_id()` refuses to revoke `org_owner` from the only remaining owner. Admin UI blocks the action. | `test_phase2_admin.py::TestLastOwnerGuard` |
| Role template drift — migration seeds diverge from Python catalog | Parity test compares migration `_TEMPLATE_SEEDS` against `ROLE_TEMPLATES` at runtime. | `test_phase2_authz.py::TestMigrationSeedParity` |
| Ad-hoc DB session in `@require` leaks tenant context | `@require` opens its own `SessionLocal()`; `tenant_context` is a contextvar, so the ad-hoc session still sees the active org filter. | `test_phase2_authz.py::TestRequireDependency` |

## Deployment hardening (Phase 7)

| Threat | Mitigation | Test |
|---|---|---|
| Client spoofs IP via ``x-forwarded-for`` to bypass rate limits | `TrustedProxyMiddleware` strips forwarded headers when the direct connection is not from a trusted proxy CIDR. Default: loopback only; production proxies must be configured explicitly. | `test_phase7_deploy.py::TestClientIP::test_untrusted_direct_ignores_forwarded` |
| Client forges ``x-forwarded-proto=https`` to trigger HSTS on plain HTTP | Same middleware strips ``x-forwarded-proto`` for untrusted connections. HSTS only emitted when the proxy is trusted. | `test_phase7_deploy.py::TestTrustedProxyParsing` |
| Container runs as root, escalating a breakout | Dockerfile uses `USER appuser` (UID 1000). Base image pinned by SHA256 digest. | `Dockerfile` |
| Dependency supply-chain attack (malicious wheel) | `pip-audit` scans for known CVEs. `pip-compile --generate-hashes` pins wheels by hash. | `Makefile audit` target |
| Data loss with no backup | `tools/backup.sh` creates WAL-aware SQLite copies or Postgres dumps. `tools/restore.sh` performs pre-restore backup. | Manual drill (documented in README) |
| Multi-worker rate limiter ineffective (per-worker counters) | `RedisLimiter` shares counters across workers via Redis. Activated by setting `REDIS_URL`. In-process fallback for single-worker. | `app/auth/rate_limit.py` |

## Data audit (Phase 4)

| Threat | Mitigation | Test |
|---|---|---|
| Unauthorized data modification goes undetected | `DataAuditEvent` records every write on tenant-scoped tables: actor, action, table, row, before/after snapshots. Admins can review the audit trail. | `test_phase4_audit.py::TestRecordAuditEvent` |
| Audit trail tampered after the fact | Audit rows are INSERT-only at the app layer. Phase 3 RLS enforces INSERT-only at the DB level. No UPDATE/DELETE route targets the audit table. | `app/services/audit.py` design |
| Significant changes (archive, delete) happen without admin awareness | Notification fan-out creates in-app alerts for org admins/owners when archive, delete, lock, or amend actions occur. | `test_phase4_audit.py::TestNotificationFanOut` |

## Sensitive-info awareness (Phase 6)

| Threat | Mitigation | Test |
|---|---|---|
| Operators accidentally enter PII/CUI in operational notes | Regex-based scanner detects SSN patterns, phone numbers, emails, DOB context, medical references, clearance mentions, and financial account patterns in free-text fields. | `test_phase6_sensitive_info.py` |
| Operators ignore sensitive-info warnings | Server-side gate requires explicit acknowledgment checkbox when patterns are detected. Form submission is blocked until the operator confirms compliance with data-handling policy. | `app/routes/personnel.py` + `app/routes/absences.py` |
