# Architecture & trust boundaries

Last updated: end of Phase 2. Revisions land alongside the phases that
change the diagrams.

## Deployment topologies

The codebase ships in two deployment shapes that share the same Python
package and the same migration history:

```
┌──────────────────────────┐         ┌──────────────────────────────────────┐
│  AppImage (single-tenant)│         │  Hosted (multi-tenant)               │
│                          │         │                                      │
│  Browser ── localhost ── │         │  Browser ── HTTPS ── Reverse proxy ──│
│           ─ Uvicorn ──── │         │           ─ Uvicorn (N workers) ──── │
│           ─ FastAPI ──── │         │           ─ FastAPI ──────────────── │
│           ─ SQLite ──── │         │           ─ Postgres ──────────────── │
│                          │         │                                      │
│  TBDTASK_SINGLE_TENANT=1 │         │  DATABASE_URL=postgres://...         │
│  No auth surface         │         │  TBDTASK_SECRET_KEY=...              │
│                          │         │  OIDC_*_CLIENT_ID/SECRET=...         │
└──────────────────────────┘         └──────────────────────────────────────┘
```

In SINGLE_TENANT mode, all auth + tenancy middleware short-circuits.
There's literally one tenant; the listener never enters a tenant context.

## Request pipeline (hosted mode)

```
Request
  ↓
TrustedProxyMiddleware ── strips x-forwarded-* from untrusted IPs
  ↓
SecureHeadersMiddleware ── attaches CSP, HSTS, X-Frame-Options, etc.
  ↓
AuthRateLimitMiddleware ── per-IP fixed-window on /auth/* paths
  ↓
SessionMiddleware ───────── cookie → session row → user → membership;
  │                          enters tenant_context(membership.org_id)
  │                          for the duration of the request
  ↓
CSRFMiddleware ──────────── validates form/header against signed cookie
  │                          (skipped on /auth/*/callback — state validates)
  ↓
@require / @require_any ── opens ad-hoc DB session; checks
  │                          membership_roles + role_permissions for the
  │                          active membership; workcenter scoping via
  │                          ancestor walk; raises 403 on mismatch
  ↓
Route handler ───────────── runs inside tenant_context; queries to
                              tenant-scoped models auto-filter by org_id
                              via the do_orm_execute listener
```

## Trust boundaries

```
┌──────────── User ─────────────┐
│                                │
│  Browser ◀═══════════════════╗ │
│   ▲                         ║ │
│   ║                         ║ │
└═══║═════════════════════════║═┘
    ║                         ║
   HTTPS (TLS at proxy)       ║
    ║                         ║
┌═══▼═════════════════════════║═┐
│  Reverse proxy              ║ │
│  (boundary 1: TLS,          ║ │
│   X-Forwarded-* trust)      ║ │
└═══│═════════════════════════║═┘
    ║                         ║
┌═══▼═════════════════════════║═┐
│  FastAPI / Uvicorn          ║ │
│  (boundary 2: app code)     ║ │
│                             ║ │
│  ┌───────────────┐          ║ │
│  │ OIDC clients  │ ◀════════╣ │
│  │  (Authlib)    │ ─── HTTPS ────▶ Apple / Google / Microsoft IdP
│  └───────────────┘            │      (boundary 4: third-party)
└═══│═══════════════════════════┘
    ║
   localhost / VPC
    ║
┌═══▼═══════════════════════════┐
│  Postgres                     │
│  (boundary 3: data plane;     │
│   Phase 3 enforces RLS at     │
│   this boundary)              │
└═══════════════════════════════┘
```

### Boundary 1: edge

Reverse proxy terminates TLS, adds `x-forwarded-proto` /
`x-forwarded-for`. The app only trusts these headers when the direct
connection originates from a configured CIDR range (default: loopback +
RFC-1918). Untrusted connections have forwarded headers stripped,
preventing IP spoofing and HSTS downgrade attacks. Configured via
`TBDTASK_TRUSTED_PROXIES`; set to empty string when the app is directly
exposed to the internet.

### Boundary 2: app

All authorization and tenancy enforcement happens here. Phase 2 adds the
`@require` decorator (permission catalog, role templates, workcenter
scoping, last-owner guard). Compromise here is "game over" until Phase 3's
RLS provides defence-in-depth at the DB boundary.

### Boundary 3: data

Phase 0: nullable `org_id` everywhere; tenancy enforced only at the app
layer via `do_orm_execute`. Phase 3: `org_id` becomes NOT NULL, RLS
turned on, and a misconfigured app role can no longer read another
tenant's rows even with hand-crafted SQL.

### Boundary 4: identity providers

Apple, Google, Microsoft. Trust contract: we accept the `id_token` after
verifying signature, issuer, audience, expiry, and `nonce`. Userinfo
claims drive `(provider, subject)` lookup; email is **never** used as
the primary identifier for sign-in (only as a candidate for explicit
linking when verified, with Apple-relay addresses pre-flagged unsafe).

## Data classifications

| Class | Examples | Where stored | Posture |
|---|---|---|---|
| Identity material | OIDC `subject`, email, display name | `identities`, `user_accounts` | Standard rows; subject + (subject, email) audit-logged on link |
| Session material | `user_sessions.id` | DB; cookie carries id only | Cookie `__Host-`, `HttpOnly`, `Secure`, `SameSite=Lax` |
| Invite tokens (raw) | 256-bit URL-safe tokens | Out-of-band only | Never persisted in plaintext; SHA-256 in `org_invites.token_hash` |
| App secrets | `TBDTASK_SECRET_KEY`, OIDC client secrets | Environment variables | Never in code, never in DB, never in logs |
| Tenant data | Persons, tasks, worklists, etc. | All tenant tables | Org-scoped; sensitive-info posture (Phase 6) discourages PII/CUI |
| Authorization | Role grants, workcenter assignments | `membership_roles`, `roles`, `role_permissions` | Seeded from closed template catalog; custom roles limited to declared permissions |
| Audit | `auth_events` | DB | INSERT-only (Phase 4 enforces at the DB layer) |

## Data flow: sign-in

```
1. User clicks "Sign in with Google" on /login
2. App issues signed OIDC state {provider, intent=login, next}
3. App redirects browser to accounts.google.com with state + nonce
4. Google authenticates user, redirects to /auth/google/callback?code=...&state=...
5. App validates state signature + provider match
6. Authlib exchanges code → token; verifies id_token signature, iss, aud, exp, nonce
7. App normalizes userinfo (Apple-relay flagging applies here)
8. accounts.resolve_identity:
     a) (provider, subject) match → LoggedIn
     b) email match + verified → LinkRequired (no silent merge)
     c) otherwise → CreatedAccount
9. App creates server-side session bound to membership (or none)
10. App redirects: /no-orgs (zero), /orgs/select (multi), or next (single)
```

## Future evolution

* Phase 2 (complete): Permission catalog, role templates, workcenter
  scoping, `@require` decorator, admin UI for invites/members/roles/
  workcenters, migration backfill with founder promotion.
* Phase 3 enforces tenancy at the DB layer (RLS) so boundary 3 closes.
* Phase 4 (complete): Immutable audit log for all tenant data writes
  (`data_audit_events` table) with before/after snapshots. Significant
  events (archive, delete, lock, amend) fan out in-app notifications to
  org admins.
* Phase 6 (complete): Sensitive-info awareness — PII/CUI regex scanner
  on free-text fields (personnel notes, absence reason/notes) with
  mandatory operator acknowledgment when patterns are detected.
* Phase 7 (complete): Deployment hardening — trusted proxy validation,
  container base-image pinning, non-root user, healthcheck, dependency
  scanning (`pip-audit`), backup/restore scripts, Redis rate limiter
  for multi-worker deployments.
