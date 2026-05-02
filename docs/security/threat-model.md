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

* Authorization above "logged in" — Phase 2's permission catalog.
* Audit log for tenant data writes — Phase 4.
* Notification leakage — Phase 4.
* Sensitive-info acknowledgment + soft-warn regex — Phase 6.
* Dependency scanning, restore drills, monitoring — Phase 7.
