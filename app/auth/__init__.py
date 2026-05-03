"""Auth package — Phase 1 substrate.

Modules:

* ``security`` — CSRF tokens, in-process rate limiter, secure-headers helpers.
* ``sessions`` — server-side session creation, lookup, and rotation.
* ``providers`` — OIDC client registry for Apple / Google / Microsoft.

The package is intentionally small and self-contained so the auth code can
be reviewed in isolation from the rest of the app. Routes that consume
these primitives live under ``app.routes.auth`` and ``app.routes.onboarding``.
"""
