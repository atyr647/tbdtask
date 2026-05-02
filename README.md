# Worklist Tracker

Offline weekly worklist and personnel tracker. Designed to run on a Raspberry
Pi 400 as an AppImage with no network dependencies. Data lives in a single
SQLite file; nothing leaves the device.

## Features

- **Personnel** — effective-dated rate, paygrade, duty section, PRD, drivers
  license, roster status. Soft-archive with reason; history preserved.
- **Qualifications** — catalog with category, validity period, pinned-column
  flag. Per-person assignment supports the full status set
  (`not_assigned` / `assigned` / `in_progress` / `qualified` / `dinq` /
  `expired` / `waived`) with audit trail on every change.
- **Absences** — Leave / TAD / School / Medical / Appt / Other, with
  partial-day windows for appointments.
- **Today** — daily availability page: who's out, why, partial-day windows,
  percent present, code breakdown.
- **Absence calendar overview** — people × days grid with code-coloured
  cells, hatched partial-day, configurable window length.
- **Qual overview** — per-qual rollup, coverage gap flag, expanding detail
  with names per status and "expiring soon" listing.
- **Weekly worklists** — create a week (snaps to Monday, auto-named), tasks
  grouped by day → person → tasks, POIC marker, multi-assignee labels,
  unassigned bucket, per-day "out today" chip strip.
- **Single-page print** — landscape Letter, person × day grid, hatched cells
  for partial-day absence, auto-trigger `window.print()` on load.
- **Lock + amend** — finalize a week to freeze it; amendments clone with
  task instances duplicated and a parent link recorded for audit.
- **Carry-forward** — banner on each open worklist surfaces incomplete tasks
  from earlier weeks; review page lets you carry, reassign, mark complete,
  discard, or leave for next time. Defaults follow each task's
  carry-over policy.
- **Recurring tasks** — TaskTemplate CRUD with recurrence rules: daily,
  weekday set, every-N-weeks, day-of-month, Nth weekday of month. Worklist
  creation auto-generates instances; "Generate recurring" button is
  idempotent.
- **Alerts engine** — PRD warnings (2-month / 1-month / weekly inside one
  month / passed), qualification expirations and expirations within 30
  days, pending carry-over per worklist. Dashboard banner surfaces the
  active set; `/alerts` is the full queue with dismiss / resolve actions
  and history view.
- **Assignment helper** — task edit page suggests qualified candidates
  filtered by required quals / drivers license / duty section and
  scheduled-date availability.
- **Archive** — archived worklists grouped by month, amendment chain
  visible inline.

## Local dev

```sh
pip install -e '.[dev]'
python -m tools.seed_demo                  # optional: populate sample data
python -m uvicorn app.main:app --reload    # dev server on :8000
# or:
python -m app.main                         # opens default browser on :8765
```

The first request to the app creates an empty SQLite DB at `data/tbdtask.db`
(override with `TBDTASK_DATA_DIR`). Personnel, qualifications, and tasks
are entered through the UI.

## Build the AppImage

The Pi 400 is **aarch64**, so that's the default target:

```sh
tools/build_appimage.sh                 # defaults to ARCH=aarch64 (Pi 400)
ARCH=x86_64 tools/build_appimage.sh     # desktop testing
```

Output lands in `dist/tbdtask-<version>-<arch>.AppImage`.

Cross-building from an x86_64 dev box for the Pi works too — the script
fetches a portable Python interpreter for the target arch via
`python-build-standalone` and uses `pip --platform manylinux2014_aarch64`
to fetch architecture-correct wheels. Runtime deps are pure-Python
wherever possible; the only native dependency is `pydantic-core` (Rust),
for which manylinux2014_aarch64 wheels are published on PyPI.

To install on the Pi:

```sh
chmod +x tbdtask-0.1.0-aarch64.AppImage
./tbdtask-0.1.0-aarch64.AppImage
```

The AppImage opens the default browser to `http://127.0.0.1:8765/` and
keeps its data in `~/.local/share/tbdtask/`.

## Data

- DB lives at `data/tbdtask.db` (override with `TBDTASK_DATA_DIR`).
- App listens on `127.0.0.1:8765` by default. LAN access is an explicit
  toggle, not the default.
- No auth, no per-user attribution. Snapshot lock + amendment metadata
  cover the audit needs.
