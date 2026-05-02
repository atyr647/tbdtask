# Security controls

Living NIST-style mapping of what the system implements today, updated
per-feature in the same diff that introduces it. Phase numbers refer to
the rollout plan in the project conversation. Empty rows are placeholders
and stay until the corresponding phase lands.

| Family | Control | Status | Implementation | Evidence |
|---|---|---|---|---|
| **AC** Access Control | AC-2 Account Management | Phase 1 | OIDC-only sign-in. Accounts created on first verified-email login or via the explicit link flow. Disabled accounts (`user_accounts.disabled_at`) refuse session lookup on next request. | `app/auth/accounts.py`; `tests/test_phase1_auth.py::TestSessions::test_lookup_returns_none_when_user_disabled` |
| AC | AC-3 Access Enforcement | Phase 0 (substrate) → Phase 2 (perms) → Phase 3 (RLS) | `TenantScopedMixin` + `do_orm_execute` listener filters every query for tenant models when a tenant context is active. Phase 2 adds the permission catalog + `@require` decorator. Phase 3 turns on Postgres RLS. | `app/tenancy.py`; `tests/test_phase0_tenancy.py` |
| AC | AC-4 Information Flow | Phase 0 + Phase 3 | Cross-org access is impossible while a tenant context is bound. The platform-admin escape hatch (Phase 4 design) is audit-logged. | `tests/test_phase0_tenancy.py::test_cross_org_*` |
| AC | AC-12 Session Termination | Phase 1 | Server-side sessions with idle (12h) and absolute (30d) timeouts, revocation on logout, rotation on privilege change. Suspending a membership invalidates all sessions bound to it on the next request. | `app/auth/sessions.py`; `TestSessions::test_lookup_returns_none_when_membership_suspended` |
| **AU** Audit & Accountability | AU-2 Audit Events | Phase 1 (auth-layer) → Phase 4 (full audit log) | `auth_events` table records login, logout, link, link-rejected, callback errors, org create/switch, invite accept/reject. Tenant data audit lands in Phase 4. | `app/models.py::AuthEvent`; `app/routes/auth.py`; `app/routes/onboarding.py` |
| AU | AU-9 Protection of Audit Information | Phase 4 | App role gets INSERT only on the audit table; UPDATE/DELETE revoked at the DB layer. | (Phase 4) |
| **CM** Configuration Management | CM-2 Baseline Configuration | Phase 1 | All schema in Alembic migrations; no runtime DDL. CSP/HSTS/cookie flags defined once in `app/auth/security.py`. | `alembic/versions/`; `app/auth/security.py::DEFAULT_CSP` |
| CM | CM-7 Least Functionality | Phase 1 | OIDC providers without configured client_ids are silently absent from the login page. SINGLE_TENANT mode bypasses auth surface entirely on the AppImage. | `app/auth/providers.py::configured_providers` |
| **IA** Identification & Authentication | IA-2 Identification & Authentication (Org Users) | Phase 1 | Apple/Google/Microsoft OIDC. No app-owned passwords, no email-link fallback, no app-side MFA (delegated to upstream IdP). | `app/auth/providers.py`; `app/routes/auth.py` |
| IA | IA-5 Authenticator Management | N/A (delegated) | App stores no authenticator material. OIDC client secrets read from env, never persisted. | `app/auth/providers.py` |
| IA | IA-8 Identification of Non-Org Users | Phase 1 | Same OIDC flow regardless of org affiliation; org binding is per-membership, not per-user. | `app/models.py::OrgMembership` |
| **SC** System & Communications Protection | SC-7 Boundary Protection | Phase 1.5 | Strict CSP, `frame-ancestors 'none'`, `form-action 'self'`, HSTS preload-eligible on HTTPS. | `app/auth/security.py::DEFAULT_CSP`; `secure_response_headers` |
| SC | SC-8 Transmission Confidentiality | Deployment | Cookies marked `Secure`. App relies on a TLS-terminating reverse proxy in production; HSTS is emitted only when `x-forwarded-proto=https`. | `app/middleware.py::SecureHeadersMiddleware` |
| SC | SC-23 Session Authenticity | Phase 1 + 1.5 | `__Host-` cookie prefix, `HttpOnly`, `SameSite=Lax`. CSRF: signed double-submit token bound to session id. OIDC `state` + `nonce` validated on callback. | `app/auth/security.py`; `app/auth/sessions.py::SESSION_COOKIE_NAME` |
| **SI** System & Information Integrity | SI-10 Information Input Validation | Phase 1 | Org names capped at 128 chars, slugified to `[a-z0-9-]`. Invite tokens hashed at rest; raw token never persisted. | `app/routes/onboarding.py::_slugify`; `app/auth/invites.py` |
| SI | SI-11 Error Handling | Phase 1 | Invite-redemption failures return a generic message regardless of the actual reason; the reason is recorded in `auth_events` for the admin who issued it. | `app/routes/onboarding.py::_invite_failure` |
