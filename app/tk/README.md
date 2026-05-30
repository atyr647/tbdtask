# Native Tk front-end (`app.tk`)

A tkinter desktop UI for the Worklist Tracker, built to replace the
FastAPI + Jinja + **WebKitGTK/Tauri** stack on the Raspberry Pi 400.

## Why

On the Pi 400 the old AppImage's cost was the embedded browser engine —
WebKitGTK is heavy to start and repaint on the VideoCore VI, and every
navigation was a full-page server round-trip the browser had to reparse
and relayout. The Python/SQLite backend was never the bottleneck.

This front-end keeps that backend verbatim and throws away only the
browser: it imports `app.services` / `app.models` / `app.db` **in
process** and renders with native Tk widgets. No HTTP server, no Jinja,
no webview. Startup is near-instant and repaints are cheap.

## Run it

```sh
# Needs Tk. On Debian/Ubuntu: the interpreter must have tkinter.
#   python3 -c "import tkinter"      # should not error
# If it does:  apt-get install python3-tk   (or use a python build with Tk)

pip install -e .            # core deps (sqlalchemy, alembic)
pip install -e '.[print]'   # optional: enables worklist PDF export (ReportLab)
python -m tools.seed_demo   # optional: populate demo data
python -m app.tk            # opens the window
```

Data lives in the same `data/tbdtask.db` as the web app (override with
`TBDTASK_DATA_DIR`). The launcher forces `TBDTASK_SINGLE_TENANT=1`, runs
`init_db()` (Alembic to head + seeds the `default` org), and binds every
read to that org's `tenant_context` — exactly what the web middleware did
per request.

`Ctrl-R` re-runs the current screen's query.

## Architecture

```
context.py   bootstrap + read()/write() — run a callback inside
             tenant_context + session_scope. The ONLY place sessions open.
dto.py       detached value objects handed to widgets. ORM instances never
             leave a session (lazy loads would raise DetachedInstanceError).
queries.py   reuse app.services, return DTOs. The bridge between ORM and UI.
commands.py  write closures (create/edit/archive), mirroring the routes.
forms.py     modal dialog framework (text/date/choice/multichoice/...).
actions.py   PII gate + validation-error handling + refresh-on-success.
pdf.py       landscape worklist PDF via ReportLab (optional dep). Replaces
             the browser print path; degrades gracefully if not installed.
theme.py     palette lifted from app.css + the status colour map.
widgets.py   VScroll, Card, StatTile, badge(), SearchableTree.
screens/     one ttk.Frame per section, each exposing refresh(**params).
app.py       the window: sidebar nav -> swappable content area.
```

The hard rule: **a widget never touches an ORM relationship.** Everything
it renders is a DTO built inside the session by `queries.py`. That's what
keeps the screens dumb and the session lifetime contained.

### Adding a screen

1. Add a query in `queries.py` that returns a DTO (define it in `dto.py`).
2. Add a `Screen` subclass in `screens/` with a `refresh(**params)`.
3. Register it in `app.NAV`.
4. Cover the query in `tests/test_tk_queries.py` (no display needed).

## Porting status

**Read side (done):** Today / day overview, Personnel (Active / Incoming /
Departed + profile), Qualifications (readiness overview + colour matrix),
Absences (list + calendar grid), Worklists (list + week view), Alerts.

**Write side (done):** create person, create incoming, edit person
(effective-dated rate / duty / PRD / license + status), in-processing
checklist, mark-arrived, move-to-departed; create / edit / archive
absence; alert triage (dismiss / snooze / resolve) and PRD actions
(update PRD, archive person from alert); create worklist (auto-seeds
recurring tasks), regenerate recurring, add task (with multi-assignee
picker + POIC defaulting), edit task (fields / status / hours /
completion), lock a worklist; assign qualifications to a person (bulk,
with achieved-date -> expiry derived from the qual's validity window) and
change a person-qual's status (the close-current / append-new effective-
dated pattern); manage a task's assignees after creation (add person or
off-roster lead, set/auto-demote POIC, remove); the qualification catalog
(create / rename / archive a qual); recurring-task templates (create /
edit / archive, with the full recurrence builder — daily / weekday set /
every-N-weeks / day-of-month / Nth-weekday — plus required quals, driver's
license, duty section); and the remaining worklist actions (edit name /
notes, amend a locked week into a new editable version cloning its tasks +
assignments, carry-over review/apply, archive). These go through
`commands.py` (which mirrors the route handlers' validation, effective-
dating, soft-delete, POIC defaulting, recurrence parsing, and alert
recompute), `forms.py` (the modal dialog framework, including the
multi-select field), and `actions.py` (the PII/CUI acknowledge gate +
validation-error handling + refresh).

**Print (done):** "Print PDF" on the week view renders the landscape
worklist grid straight to PDF via `pdf.py` (ReportLab) — the native
replacement for the browser print path. Same layout as the old
`print.html`: Person/Team/day-column table with a header that repeats
across pages, per-day present%, "Lead" markers, shared-assignee notes,
shaded absence cells, an "Out" footer summary, and an "All hands"
section. ReportLab is the optional `print` extra; without it the button
explains how to install it. The PDF uses a real TrueType family
(Liberation Sans, falling back to DejaVu Sans, then ReportLab's built-in
Helvetica) registered once per process in `pdf._register_fonts`.

**Multi-tenant-only routes (intentionally not ported):** login/OIDC,
org selection/creation, invites, members, roles, and workcenters. These
are bypassed entirely in single-tenant mode (the offline Pi tool has no
accounts or orgs), so they have no place in this UI. Everything the
offline tool actually does is now native.
