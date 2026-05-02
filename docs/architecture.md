# Architecture — Stateless-Server Hybrid (PARKED)

> **Status: not the current architecture.** This document captures a
> design that was discussed but never built. The shipping app is a
> local Pi tool with server-side SQLite (see the project `README.md`).
>
> This file is kept as a reference for a hypothetical future where the
> deployment model changes — public URL, remote access without VPN,
> compliance demanding "no PII on the server", etc. None of those apply
> right now. If the requirements ever shift, the threat model and
> security plan below are still useful starting points; everything
> implementation-wise is unbuilt.

## Goals

- Personnel data never persists on the server.
- Server holds data only for the lifetime of one HTTP request.
- Server never sees personally identifying free-text (names, reasons,
  notes). It sees structure: IDs, dates, paygrades, status enums,
  counts.
- The browser is the canonical store; backups are explicit, JSON-only,
  user-initiated, and aggressively reminded.
- No rewrite. Routes and templates keep their shape. Persistence is
  swapped out underneath them.

## Threat model

We design against:

- A **compromised host** (server box, the Pi, or hosted VPS). The
  attacker can read disk, memory dumps, systemd journals, and any
  file the app process writes.
- **Network sniffing** on the LAN segment between user and server.
- **Stolen device** (laptop or Pi) with the browser profile intact.
- **Curious sysadmin** with shell on the server but no malicious
  intent — accidental log spillage matters as much as attacker
  intent.

We are **not** designing against:

- A compromised browser process (XSS, malicious extension). Browser
  is trusted; if attacker is in the browser they have everything.
  CSP and "no inline JS" are partial mitigations, not complete.
- A nation-state with rubber hoses.

## Layers

```
+-----------------------------------------------------------+
|                       BROWSER                             |
|                                                           |
|   IndexedDB (canonical, all fields)                       |
|         │                                                 |
|         ▼                                                 |
|   slice-builder.js  ──► fetch('/op')  body = { slice }    |
|         ▲                                                 |
|         │                                                 |
|   decorate.js  (substitutes names into rendered HTML)     |
+-----------------------------------------------------------+
                              ▲
                              │ TLS or VPN
                              ▼
+-----------------------------------------------------------+
|                       SERVER                              |
|                                                           |
|   request handler                                         |
|         │                                                 |
|         ▼                                                 |
|   build :memory: SQLite from request body                 |
|         │                                                 |
|         ▼                                                 |
|   existing route logic, unchanged                         |
|         │                                                 |
|         ▼                                                 |
|   render Jinja with placeholder spans for sensitive       |
|         │                                                 |
|         ▼                                                 |
|   discard engine, return response                         |
|                                                           |
|   no logs, no disk, no temp files                         |
+-----------------------------------------------------------+
```

## Sensitive vs structural

| Category   | Browser-only                                         | Server sees                                    |
|---         |---                                                   |---                                             |
| Person     | first_name, last_name, full_display, position, notes, sponsor | id (UUID), paygrade, duty_section, drivers_license, prd_date |
| Absence    | reason                                               | id, code_id, person_id, start_date, end_date, start_time, end_time |
| Task       | name, description, notes, completion_notes           | id, category_id, scheduled_date, status, hours, is_poic |
| Qualification | name                                              | id (referenced by person_quals)                |
| Worklist   | name, notes                                          | id, week_starting, version, locked             |

IDs are UUIDs (random, not sequential — sequential leaks count).

## Per-operation slice protocol

Every endpoint declares its data dependencies. The browser builds the
slice from IDB and sends it as the request body. Server constructs a
fresh `:memory:` SQLite per request, populates from the slice, runs
the route, returns response, discards engine.

| Operation | Slice |
|---|---|
| `POST /personnel/{id}` | the person + their attribute history |
| `POST /worklists/{id}/tasks` | the worklist row + the new task fields |
| `POST /alerts/{id}/extend-prd` | the alert + the linked person's PRD history |
| `GET /worklists/{id}/print` | the worklist + its tasks/assignments + people referenced + that week's absences |
| `GET /today` | roster + today's absences + tasks scheduled today + recurring templates |
| `GET /personnel/{id}` | that person + qual rows + absences + recent task assignments |

Heavy aggregates (`/quals/matrix`, `/absences/calendar`) genuinely
need most of the DB. Two options handled v1 vs v2:

- **v1:** accept the full slice for these views.
- **v2:** move just these views to client-side rendering — they are
  big tables, JS can render them straight from IDB.

Mutation responses include the diff (changed rows) plus a new
revision number; the browser merges into IDB and bumps its known
revision.

## Authentication / access control

- **Default bind:** `127.0.0.1` only.
- **LAN mode:** explicit toggle in `data/launcher.json`. Restart
  required. Banner displays "🔓 LAN mode bound to 0.0.0.0".
- **IP allowlist:** optional CIDR list in launcher.json.
- **PIN gate:** single shared PIN, hashed at rest in launcher.json,
  prompted on first request, set as `HttpOnly Secure SameSite=Strict`
  cookie. v1 = team-wide PIN. Per-user PINs deferred.
- **CSRF:** every form embeds a token derived from the cookie + a
  per-session salt; verified on every POST. Standard double-submit
  pattern.
- **TLS:** mandatory in LAN mode. **Strong recommendation:** use a
  Tailscale (or comparable WireGuard mesh) instead of self-signed
  certs. Less cert lifecycle, IP allowlist effectively for free.
  Self-signed certs supported but not the documented happy path.

## Strict CSP

```
Content-Security-Policy:
    default-src 'self';
    script-src 'self';
    style-src 'self';
    img-src 'self' data:;
    object-src 'none';
    base-uri 'self';
    form-action 'self';
    frame-ancestors 'none';
```

**No inline scripts. No inline event handlers. No inline styles.** Every
existing `onclick="..."`, `<script>...</script>`, and `style="..."`
gets pulled out:

- Inline scripts → external `.js` files registered via `<script src>`.
- Event handlers → `addEventListener` at module load, using
  `data-*` attributes to carry per-element parameters.
- Inline styles → CSS classes.

Tagged-template HTML construction (lit-html style) for any DOM
manipulation that interpolates user-supplied text. Never `innerHTML
= someUserText`.

## IDB encryption

**v1 — required.** Passphrase-derived key + AES-GCM per record.
Without this, a stolen laptop with the browser profile intact reads
every name, reason, and note in plaintext from disk. Encryption is
not optional.

- **Key derivation:** Argon2id (via a vetted JS implementation —
  `argon2-browser` or the upcoming Web Crypto Argon2 once it ships).
  Fallback to PBKDF2-SHA-256 with ≥600,000 iterations only if Argon2
  cannot be loaded for some reason. Salt stored in IDB alongside
  the encrypted blobs. Memory cost tuned so derivation runs in
  ≤300 ms on a Pi 400 browser.
- **Cipher:** AES-GCM via the Web Crypto API. Per-value random IV
  (12 bytes), authenticated tag bundled with the ciphertext.
- **Key lifetime:** the derived `CryptoKey` lives in JS memory only,
  never in `localStorage` or `IndexedDB` and **never on the wire**.
  The passphrase itself never leaves the browser process — not for
  the server, not for any logger, not even into the page DOM
  beyond the password input event.
- **Session unlock:** browser re-prompts on every fresh page load
  (no `sessionStorage` cache by default). An optional "remember for
  this session" toggle can park the key in `sessionStorage` for the
  current tab only — cleared when the tab closes. Default is
  re-prompt every load; nudges discipline.
- **Idle re-lock:** after N minutes of inactivity, the key is
  zeroed in memory and the user is forced to re-enter the
  passphrase. Default 30 min, configurable.
- **Lost passphrase = lost data.** No recovery, no backdoor, no
  "we'll email you a reset link". The first-time setup screen makes
  this explicit and prompts the user to download a recovery backup
  before encryption is enabled.
- **Backup file format:** by default plaintext JSON, so a forgotten
  passphrase never bricks the data. Power users can opt into a
  passphrase-wrapped backup (same passphrase or a separate one).
  This is documented loudly because it's exactly the place where
  users can lose data through a single click.
- **Slices sent to the server are still plaintext** for the
  fields the server needs to operate on (paygrade, dates, status,
  IDs). Encryption protects fields **at rest in IDB**, not while
  they're in flight to the server. Sensitive fields (names, free
  text) are scrubbed before slicing — that's the anonymization
  layer's job, separate from this encryption layer.

## XSS hardening

The browser now holds the entire sensitive surface. A single
mishandled rendering path can leak the whole dataset. Defense is
layered, in priority order:

**Primary defenses (do these right or nothing else matters):**

1. **Never `innerHTML` user-supplied content.** Trusted text always
   goes through `textContent` or via a tagged-template engine
   (lit-html or comparable) that escapes interpolations by default.
   No `element.innerHTML = …` on anything that contains data.
2. **Escape every template output.** Jinja's autoescape stays on
   for every `*.html` file. Any `|safe` filter usage is a code-review
   blocker.
3. **Strict CSP** as documented above. Browser refuses to execute
   anything that wasn't shipped as a real `.js` file.
4. **No inline scripts.** Including `<script>...</script>` blocks,
   `onclick=`, `style=`, and `javascript:` URLs. Pulled out, audited
   at build time.

**Backup protection — DOMPurify:**

DOMPurify is the safety net for places where the primary rules
might be insufficient or where some rendering path slips through
during refactors. It is **not** the primary defense — primary is
"don't render untrusted HTML at all".

The pattern:

```
trusted text   →  element.textContent = value
untrusted HTML →  element.innerHTML = DOMPurify.sanitize(html, cfg)
```

Where DOMPurify earns its keep:

- Notes / description fields if they're ever rendered as HTML
  preview rather than plain text. (v1 keeps them plain — `textContent`
  only — but DOMPurify is in place if that ever changes.)
- Imported backup files. User uploads a JSON file; if any field
  contains HTML-like content (intentional or smuggled), it gets
  sanitized before any rendering.
- HTMX swap fragments. We don't expect server fragments to carry
  user-supplied HTML, but a `htmx:beforeSwap` hook routes the
  incoming fragment through DOMPurify as a tripwire.
- Anything new that says "let's render rich text" — ship it through
  DOMPurify with a conservative allowlist (`p`, `br`, `strong`,
  `em`, `ul`, `ol`, `li`, no `script`, no `style`, no event
  handlers, no `javascript:` URLs, `a` with `href` allowlist).

**Bundling:**

DOMPurify is bundled with the static assets, not loaded from a CDN.
Subresource integrity is set on the `<script>` tag pointing to the
bundled file, so a tampered file fails to load.

**Other defense-in-depth:**

- For `data-*` attributes that drive `decorate.js`, validate values
  against `^[a-zA-Z0-9_-]{1,64}$` before lookup. An injected
  `data-person="../../etc/passwd"` becomes a no-op, not a path.
- Form fields are HTML-escaped on render and validated as plain
  text on save (reject embedded `<script>`, `javascript:` URLs,
  control characters) as a tripwire even though escaping alone
  suffices.
- An attacker who manages to inject HTML still can't execute
  script — CSP `script-src 'self'` blocks both inline and unknown
  external sources. They'd need to convince the server to serve
  malicious JS, which it doesn't.

## Backups are unavoidable

Browser-owned data dies if the IDB is evicted, the profile is
wiped, or the browser is uninstalled. Backups are mandatory, not
optional.

- **Persistent storage prompt:** call `navigator.storage.persist()`
  on first visit and cache the user's response. Without this, browsers
  can evict IDB under storage pressure.
- **Backup reminder banner:** appears on the dashboard whenever:
  - more than `N` mutations have happened since the last download, OR
  - more than `M` days have elapsed since the last download.
  v1 thresholds: `N = 25`, `M = 7`. Snooze writes a key to settings
  with an expiry.
- **Hard nudge before destructive operations.** Importing,
  "Erase all data," and any "Replace mode" import prompt the user to
  download a backup first. The action is gated until they do or
  confirm an explicit "I understand I am proceeding without a backup."
- **`beforeunload` warning** if mutations since last download exceed
  a threshold. Browser shows the OS confirm dialog.
- **Auto-export hint** every 30 minutes of active editing — surfaces
  a non-blocking toast: "It's been 30 minutes; download a backup?"
  Click downloads the JSON; dismiss snoozes 30 min.
- **One-click backup is always visible** in the dashboard top-bar
  (small "💾 Backup" button), not buried in a settings page.

## Logging policy (brutal)

**Goal:** no PII ever lands on disk via logs. Realized by suppressing
logs entirely where feasible and sanitizing where not.

- Uvicorn:
  - `--access-log=False`
  - `--log-level=warning`
  - Custom log format that includes only timestamp + level + message.
    Never `%(request)s` or anything that can carry a body.
- SQLAlchemy:
  - `engine.echo=False` (already so).
  - No `before_execute` / `before_cursor_execute` listeners.
- FastAPI:
  - Custom exception handler that returns a generic 500 without
    re-raising. Logs only the exception class name + the path,
    never the request body or query string.
  - Disable starlette debug middleware in production.
- Python:
  - No `print()` anywhere. Audit script in CI.
  - `logging.basicConfig(level=logging.WARNING, handlers=[NullHandler()])`
  - `PYTHONFAULTHANDLER` not set.
- systemd:
  - `StandardOutput=null`
  - `StandardError=null` (or `journal` only if needed for crash
    debugging, with an aggressive sanitizer).
  - `LogsDirectory=` removed.
- Disk:
  - Jinja bytecode cache disabled (`bytecode_cache=None`).
  - No `~/.cache`, no `/var/cache`, no `/tmp` artifacts.
  - `tempfile` redirected to `/dev/shm` on Linux if anything
    accidentally uses it.
- Upload spool:
  - FastAPI/starlette spools uploads to `/tmp` over 1 MB. Read
    `UploadFile.read()` immediately into memory and close the
    underlying spooled file. Or set `MAX_PART_SIZE` so it never
    spools.
- Crash dumps:
  - `ulimit -c 0` in the launcher.
  - No core file directory configured.

## Data lifecycle on the server

```
request arrives ──► spawn :memory: engine
              │
              ▼
populate from request body slice
              │
              ▼
existing route logic runs (unchanged)
              │
              ▼
render Jinja with placeholder spans
              │
              ▼
return HTTP response
              │
              ▼
engine garbage collected when handler exits
              │
              ▼
nothing on disk, nothing in shared memory
```

Per-request engine creation costs ~1-2 ms for our small datasets.
Acceptable.

## Migration plan

Implementation is on `claude/stateless-hybrid`, branched from the
current main feature branch.

1. **Storage layer** — replace `engine` + `SessionLocal` with
   `engine_for(slice)` that builds a fresh `:memory:` SQLite from
   the request body and yields a session bound to it.
2. **Schema migrations** — switch primary keys to UUIDs (or add a
   `public_id` UUID alongside the int PK). Slice serializer uses
   public_ids exclusively on the wire.
3. **Slice spec per route** — annotate each route with the entities
   it needs. Consider using a small decorator that declares the
   slice shape.
4. **Sensitive-field strip** — slice serializer drops the sensitive
   columns before sending. Server-side schemas drop them entirely
   (column doesn't exist on the in-memory table).
5. **Templates → placeholders** — every `{{ p.last_name }}` becomes
   `<span data-person="{{ p.public_id }}" data-field="last_name"></span>`.
   `decorate.js` populates from IDB on every render.
6. **Forms → ID resolution** — combobox-style inputs that resolve
   typed names to IDs locally before submit.
7. **CSP + no inline JS** — pull every inline `<script>` and
   `onclick` to external files. Ship CSP header from the FastAPI
   middleware.
8. **Logging sanitization** — apply the brutal-logging policy.
9. **Backup pipeline** — `safeSave`-style download, aggressive
   reminder cadence, gated destructive actions, persistent-storage
   prompt.
10. **Welcome flow** — first-visit page chooses "Start fresh" or
    "Upload backup". No data → server gets nothing to render against
    → welcome page is the one route that doesn't follow the slice
    protocol.

Estimated effort: **2–2.5 weeks** of focused work, shipping in
visible stages so each phase can be reviewed running before the
next.

## v2 backlog (not in initial scope)

- IDB encryption with passphrase-derived key.
- Service-worker pre-cache of the static shell so the app loads
  instantly on next visit.
- Per-user PINs (each visitor sets their own, hashed locally).
- Move qual matrix and absence calendar to client-side rendering so
  even those views never send a full slice.
- Optional encrypted backup file (passphrase-wrapped JSON).

## Open questions

- **PIN model.** Team-wide shared PIN (v1) vs per-user PINs (v2)?
- **Idle session lock.** Re-prompt the PIN/passphrase after N
  minutes of inactivity, or trust the browser session?
- **Backup destination.** Local download only (v1), or also support
  Google Drive / SMB / cloud target as an opt-in delivery channel?
- **Public-internet hosting at all.** Even with everything above,
  do we want to advertise this app as deployable to a public URL,
  or is the documented happy path always Pi + LAN/VPN?

## Things to revisit during implementation

- Per-request engine warmup cost on Pi 400 hardware. Profile
  early. If it's >5 ms for typical operations, consider a
  process-local engine pool with strict reset-on-checkout to keep
  the no-state guarantee.
- BroadcastChannel for multi-tab sync within one browser, since
  IDB is shared across tabs but the rendered DOM is not.
- iOS Safari's `download` attribute quirks — the `safeSave` shim
  from the reference project applies here too.
- CSP nonces vs strict allowlist if any third-party CDN ever sneaks
  in (currently zero, keep it that way).
