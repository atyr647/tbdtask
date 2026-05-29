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

**Done (read side):** Today / day overview, Personnel (Active / Incoming /
Departed + profile), Qualifications (readiness overview + colour matrix),
Absences (list + calendar grid), Worklists (list + week view), Alerts.

**Not yet ported (still web-only):** the write/edit flows — create &
edit personnel, the worklist creation wizard + per-day task setup, absence
entry, qual assignment, recurring-task templates, lock/amend, carry-over
apply, PRD/snooze/dismiss actions, and **print/PDF** (the landscape
worklist grid). The `write()` helper in `context.py` is the seam these
will hang off. Printing has no Tk equivalent and will need a PDF renderer
(e.g. ReportLab) rather than the browser's print path.

This is intentionally a read-first cut: the slow, frequently-viewed
screens that motivated leaving WebKit are all here and verified against
seeded data. Editing can be ported screen-by-screen behind the same DTO
boundary.
