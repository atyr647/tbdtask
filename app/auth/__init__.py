"""Auth package — Phases 1 + 2.

Modules:

* ``security`` — CSRF tokens, in-process rate limiter, secure-headers helpers.
* ``sessions`` — server-side session creation, lookup, and rotation.
* ``providers`` — OIDC client registry for Apple / Google / Microsoft.
* ``accounts`` — identity → user_account resolution + linking rules.
* ``invites`` — issue + redeem org invite tokens.
* ``permissions`` — closed catalog of permission codes + role templates.
* ``authorization`` — permission checking helpers + ``@require`` dependency.

The package is intentionally small and self-contained so the auth code can
be reviewed in isolation from the rest of the app. Routes that consume
these primitives live under ``app.routes.auth``, ``app.routes.onboarding``,
and ``app.routes.admin``.
"""
