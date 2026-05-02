# Worklist Tracker

Offline weekly worklist and personnel tracker. Designed to run on a Raspberry
Pi 400 as an AppImage with no network dependencies. Data lives in a single
SQLite file; nothing leaves the device.

## Current architecture

- **FastAPI + Jinja + SQLAlchemy + SQLite**, all server-side, all on the
  same machine.
- The app binds to `127.0.0.1:8765` by default and opens the operator's
  default browser to it. Pure local tool.
- Data lives at `data/tbdtask.db`. Override the location with
  `TBDTASK_DATA_DIR`.
- The runtime app does not depend on `openpyxl`. Personnel, qualifications
  and tasks are entered through the UI.
- The Pi 400 AppImage just bundles a portable Python interpreter and the
  app; see `tools/build_appimage.sh`.

## Features

- **Personnel** — effective-dated rate, paygrade, duty section, PRD,
  drivers license. Active / Incoming / Departed tabs. Incoming has
  arrival date, sponsor (searchable from current personnel), and a
  4-step in-processing checklist (Orders / Itinerary / AOB / Barracks).
  Soft-archive ("Move to departed") with reason and full history kept.
  Free-text Position field (LPO, Watchbill Coordinator, etc.).
- **Qualifications** — catalog with usage rollup (assigned / qualified /
  in progress / dinq counts). Per-person assignment with the full
  status set, audit trail on every change. Qual matrix mirrors the
  legacy spreadsheet with a colour-coded legend.
- **Absences** — Leave / TAD / School / Medical / Appt / Other with
  partial-day windows. Calendar grid with month bands, alternating
  rows, per-row click-to-pin highlight, search by name / rank / rate,
  daily full / partial / % present totals.
- **Today & day overview** — `/today` and `/day/<iso>` show who's out,
  why, partial-day windows, percent present, code breakdown, and
  every task scheduled for that date *plus* recurring templates that
  fire that day even if no worklist exists yet.
- **Weekly worklists** — wizard-style creation that flows directly into
  per-day task setup with a searchable assignee picker. Day-then-person
  layout on screen, single-page landscape grid for print, daily out-row
  footer that repeats on every printed page. Locked weeks are immutable;
  amendments clone into a new versioned snapshot.
- **Carry-forward** — open or in-progress tasks from earlier weeks
  surface as a banner on the new worklist; review page lets you carry,
  reassign, mark complete, discard, or leave for next time.
- **Recurring tasks** — TaskTemplate with daily / weekday set /
  every-N-weeks / day-of-month / Nth-weekday cadences. Streamlined form
  reveals only the fields the chosen cadence needs, with weekday
  pill-checkboxes and an "Advanced constraints" disclosure for required
  quals / drivers license / duty section.
- **Alerts** — PRD windows (12-month "apply for orders" → 2-month →
  1-month → weekly inside one month → passed), pending carry-over per
  worklist. Dashboard banner + dedicated `/alerts` queue with date-picker
  Update PRD, snooze, dismiss, resolve.
- **Backups** — every personnel, qual, absence, task, and worklist row
  lives in `data/tbdtask.db`. Copy the file to back up. JSON
  export/import is on the future-considerations list (`docs/`); not
  shipped because the offline-Pi use case doesn't need it yet.

## Local dev

```sh
pip install -e '.[dev]'
python -m tools.seed_demo                  # optional: populate sample data
python -m uvicorn app.main:app --reload    # dev server on :8000
# or:
python -m app.main                         # opens default browser on :8765
```

## Build the AppImage

The Pi 400 is **aarch64**, so that's the default target:

```sh
tools/build_appimage.sh                 # defaults to ARCH=aarch64 (Pi 400)
ARCH=x86_64 tools/build_appimage.sh     # desktop testing
```

Output lands in `dist/tbdtask-<version>-<arch>.AppImage`.

To install on the Pi:

```sh
chmod +x tbdtask-0.1.0-aarch64.AppImage
./tbdtask-0.1.0-aarch64.AppImage
```

The AppImage opens the default browser to `http://127.0.0.1:8765/` and
keeps its data in `~/.local/share/tbdtask/`.

## Future considerations

`docs/architecture.md` sketches a "browser-owns-the-data, server-is-stateless"
redesign for a hypothetical hosted/remote-access deployment. **It is not
the current architecture.** The current app is a local Pi tool with
server-side SQLite, which is the right design for the documented use
case (one team, one device, no public internet).

The doc is kept as a reference in case the deployment story ever changes.
If you're trying to understand how the app works *today*, ignore that file
and read this README plus the source.
