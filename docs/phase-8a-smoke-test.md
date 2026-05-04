# Phase 8a smoke test — local Mac

Five-minute flow for verifying that passkey enrollment, step-up gates,
and the enrollment redirect all work end-to-end against a real
authenticator (Touch ID on a Mac, Windows Hello on a PC, hardware key
elsewhere). No hosting required — `http://localhost` is treated as a
secure context for WebAuthn by every modern browser.

## Prerequisites

- macOS with Touch ID (or any system with a platform authenticator).
- Python 3.11+.
- Git.

## One-time setup

```bash
git clone <your-fork-url> tbdtask
cd tbdtask
git checkout claude/fix-failing-tests-C5Vid

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running the server (with all the right flags)

The smoke test needs four env vars. Easiest is to drop them into a
local script you don't commit:

```bash
cat > run-smoke.sh <<'SH'
#!/usr/bin/env bash
set -e
export TBDTASK_INSECURE_LOCAL_COOKIES=1     # http cookies for localhost
export TBDTASK_WEBAUTHN_ENABLED=1            # turn the feature on
export TBDTASK_DEV_LOGIN=1                   # /dev-login shortcut
# Optional: choose your own dev email
# export TBDTASK_DEV_LOGIN_EMAIL=you@example.com
exec python3 -m uvicorn app.main:create_app --factory \
  --host 127.0.0.1 --port 8765 --reload
SH
chmod +x run-smoke.sh
./run-smoke.sh
```

Server starts at `http://127.0.0.1:8765`. **Always** use `127.0.0.1`
or `localhost`, never your LAN IP — WebAuthn rejects the latter.

## The flow

### 1. Sign in via the dev shortcut

Visit `http://127.0.0.1:8765/dev-login`. You'll be redirected to the
dashboard, signed in as `dev@example.com` with full Org Owner
permissions.

You should see a yellow banner at the top:

> **Add a passkey to keep your account secure.** Sensitive actions
> will start requiring a passkey check soon. *[Set up passkey]*

### 2. Register a passkey

Click **Set up passkey** in the banner (or visit `/passkey/register`
directly).

- Optional: give it a nickname like "MacBook Touch ID".
- Click **Set up passkey**.
- macOS prompts for Touch ID. Touch the sensor.
- Page redirects to `/`. Banner is gone — credential is registered.

Verify in the drawer (☰ menu top-right): there's now a **Manage
passkeys** link.

### 3. Confirm the credential persisted

Visit `/passkey/manage`. You should see one row:

| Nickname | Status | Added |
|---|---|---|
| MacBook Touch ID | **PRF** (green pill) | 2026-05-04 |

The **PRF** pill is the critical one — that's what 8b builds on. If
it shows **no PRF** (gray), your authenticator doesn't support the
extension; Mac Touch ID via Safari 18+ / Chrome should always show
PRF.

### 4. Trigger a step-up gate

Visit `/admin/members`. You'll see your `dev@example.com` member
card with a **Suspend** button. (Won't actually let you suspend
yourself, but the click triggers the gate.)

Click **Suspend**.

Expected: redirect to `/step-up?purpose=admin_grant&next=/admin/members`
with a "Verify it's you" page. Click **Use passkey**, touch the
sensor, redirect resolves back to `/admin/members`. The actual suspend
gets blocked by the "can't suspend yourself" rule, but you'll see the
redirect chain in the network tab.

Try a different sensitive action — invite someone:
- Visit `/admin/invites`.
- Fill in any name + email, click **Issue invite**.
- Step-up redirect → Touch ID → invite issues.

### 5. Test the post-redirect single-use behavior

Try **Issue invite** twice in a row:
- First time: step-up prompt → Touch ID → success.
- Second time *immediately after*: step-up prompt again. Each
  sensitive action consumes its grant (single-use).

### 6. Test the enrollment redirect

Stop the server. Edit `run-smoke.sh` to add:

```bash
export TBDTASK_WEBAUTHN_ENFORCED_AFTER=2025-01-01    # any past date
```

Restart. Open a new private/incognito window, visit
`http://127.0.0.1:8765/dev-login`. The dashboard *redirects* to
`/passkey/register?next=/` — because the brand-new dev session has
no credential and the cutoff date has passed.

Register again, then verify other pages (`/today`, `/personnel`)
load normally.

### 7. Test the audit trail

Visit `http://127.0.0.1:8765/admin` and open the SQLite DB to confirm
the events landed:

```bash
sqlite3 data/tbdtask.db "SELECT kind, detail FROM auth_events ORDER BY created_at DESC LIMIT 20"
```

You should see rows like:

- `webauthn_register` (with `prf_supported: true`)
- `webauthn_verify` (purpose: `admin_grant`)
- `step_up_grant` (purpose: `admin_grant`)
- `org_kek_bootstrap` (Phase 8b.1 — when WebAuthn was first enabled)

## Multi-browser checklist

Repeat steps 1–4 in each browser to confirm PRF support:

| Browser | Authenticator | Expected |
|---|---|---|
| Safari 18+ on macOS | Touch ID | PRF: yes |
| Chrome 130+ on macOS | Touch ID | PRF: yes |
| Firefox 130+ on macOS | Touch ID | PRF: depends on build |
| Chrome on iOS Safari | Face ID | PRF: yes |
| Chrome on Android | Fingerprint | PRF: yes (varies by OEM) |

To use Touch ID with multiple browsers: register a separate passkey
in each. They show up as distinct rows in `/passkey/manage`.

## Tearing it down

```bash
# Stop the server (Ctrl-C), then:
unset TBDTASK_DEV_LOGIN
unset TBDTASK_WEBAUTHN_ENABLED
unset TBDTASK_WEBAUTHN_ENFORCED_AFTER

# To drop the dev DB entirely:
rm -f data/tbdtask.db data/tbdtask.db-shm data/tbdtask.db-wal

# To revert to pre-8a behavior in code without throwing the DB away:
alembic downgrade -2     # drops phase8a + phase8b1 tables
```

The `/dev-login` route returns 404 once the env var is unset, so
leaving the code in place but the flag off is safe.

## What you're verifying

A successful smoke test confirms:

- `/passkey/register` correctly probes PRF support and stores the
  boolean fact (not the bytes).
- `/passkey/manage` correctly shows the credential.
- `/step-up` correctly mints + consumes single-use grants.
- The gate on `/admin/members/.../suspend` correctly redirects to
  `/step-up` instead of returning JSON 403 (HTML negotiation).
- The post-step-up redirect resolves to the intended next path.
- The enrollment-redirect middleware fires after the cutoff date
  and respects the exempt-path list.
- All audit events land in `auth_events` with the right kinds.

If any of those are wrong on Touch ID, the bug is in 8a code that
8b.2 will build on — finding it now saves time later.

## Common gotchas

- **"navigator.credentials is undefined"** — you're not on
  `localhost`/`127.0.0.1` or you're not on https. Check the address bar.
- **"Origin mismatch"** — your `expected_origins` list doesn't
  include the URL the browser is actually on. Default origins are
  `http://localhost:8765` and `http://127.0.0.1:8765`. If you change
  the port, set `TBDTASK_WEBAUTHN_ORIGINS=http://localhost:NNNN`.
- **"User-verification required" but Touch ID isn't prompting** —
  on Chrome, ensure your Mac's biometric is unlocked recently.
  Sometimes the OS caches a no-prompt window.
- **`/dev-login` returns 404** — both `TBDTASK_DEV_LOGIN=1` and
  `TBDTASK_INSECURE_LOCAL_COOKIES=1` are required. Check `env | grep
  TBDTASK`.
- **Banner doesn't disappear after registration** — refresh the page;
  the banner state is computed at request time and the previous
  request was cached.

## Reporting back

If something doesn't work, the most useful info to share is:

```bash
sqlite3 data/tbdtask.db "
SELECT created_at, kind, json_extract(detail, '$') AS detail
FROM auth_events ORDER BY id DESC LIMIT 30
"
```

That plus a copy of the browser DevTools "Network" panel (with the
failed request expanded to show response body) is enough to root-
cause almost anything in 8a.
