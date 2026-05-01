# tbdtask

Offline weekly worklist and personnel tracker. Designed to run on a Raspberry
Pi 400 as an AppImage with no network dependencies. The legacy
`Weekly Crew Worklist.xlsx` is treated as a one-shot migration source; the
runtime app does not read or write Excel.

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
python -m tools.migrate_from_xlsx          # one-shot seed from xlsx
python -m uvicorn app.main:app --reload    # dev server on :8000
# or:
python -m app.main                         # opens default browser on :8765
```

The runtime app does not depend on `openpyxl`. Migration is the only place
the workbook is read.

## Build the AppImage

```sh
ARCH=aarch64 tools/build_appimage.sh   # for the Pi 400
ARCH=x86_64  tools/build_appimage.sh   # for x86_64 testing
```

The script fetches a portable Python via `python-build-standalone`, installs
runtime dependencies into the bundle, and assembles the AppImage with
`appimagetool`. Output lands in `dist/`.

## Data

- DB lives at `data/tbdtask.db` (override with `TBDTASK_DATA_DIR`).
- App listens on `127.0.0.1:8765` by default. LAN access is an explicit
  toggle, not the default.
- No auth, no per-user attribution. Snapshot lock + amendment metadata
  cover the audit needs.
