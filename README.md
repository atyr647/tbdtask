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
- The Pi 400 AppImage bundles a Python interpreter, a dynamically-linked
  Tcl/Tk runtime, and the app — it runs the native tkinter UI offline with
  no system dependencies; see `tools/build_appimage.sh`.

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

### Native Tk front-end (Pi 400 performance)

The web view runs inside WebKitGTK, which is the dominant cost on the
Pi 400. `app/tk/` is a native **tkinter** UI over the same
services/SQLite backend — no browser engine, no HTTP — so it starts
instantly and repaints cheaply on that hardware:

```sh
python -m app.tk            # opens the native window (needs tkinter)
```

It's a read-first port today (Today, Personnel, Qualifications,
Absences, Worklists, Alerts). See `app/tk/README.md` for the
architecture and porting status.

The schema is managed by Alembic. `init_db()` runs `alembic upgrade head`
on every startup, so the app self-migrates. To create a new migration
after editing `app/models.py`:

```sh
alembic revision --autogenerate -m "describe the change"
alembic upgrade head      # apply locally
```

Existing pre-Alembic databases are detected and stamped as
up-to-date on first run; no manual migration required.

Run the test suite with:

```sh
python -m pytest tests/
# or:
make test
```

## Hosted deployment (Docker)

```sh
# Copy and fill in environment variables
cp .env.example .env
# Build and run
docker compose up -d
```

The container runs as a non-root user, exposes `/healthz` for liveness
probes, and validates `x-forwarded-*` headers against trusted proxy CIDRs.
See `docs/security/` for the full threat model and controls catalog.

## Backup & restore

```sh
# Backup
make backup                              # SQLite: copies data/tbdtask.db
tools/backup.sh                          # also supports Postgres via pg_dump

# Restore (creates a pre-restore backup first)
make restore BACKUP_FILE=data/backups/tbdtask-20240101-120000.db
tools/restore.sh data/backups/tbdtask-20240101-120000.db
```

## Dependency scanning

```sh
make audit                               # pip-audit scans for known CVEs
make freeze                              # generate pinned requirements.txt
```

## Get the AppImage

Prebuilt aarch64 AppImages are attached to [GitHub Releases](../../releases).
Download the latest `tbdtask-<version>-aarch64.AppImage` (and optionally
`SHA256SUMS` to verify), then on the Pi:

```sh
chmod +x tbdtask-0.1.0-aarch64.AppImage
./tbdtask-0.1.0-aarch64.AppImage
```

The AppImage opens the **native tkinter window** directly (no browser, no
WebKitGTK) and keeps its data in `~/.local/share/tbdtask/`. It is fully
self-contained — a bundled Python + Tcl/Tk + the app — so there are no
system packages to install on the Pi.

## Build the AppImage locally

The AppImage bundles a **dynamically-linked Tk** runtime assembled from
Debian/Ubuntu packages. (python-build-standalone's statically-linked Tk
aborts with an xcb assertion the moment a widget renders, on real X
servers too — see the header comment in `tools/build_appimage.sh`.)

```sh
ARCH=x86_64 tools/build_appimage.sh     # default: assembles from the build host's python+tk
ARCH=aarch64 tools/build_appimage.sh    # Pi 400: fetches arm64 .debs (or set DEB_DIR=...)
WITH_PDF=0 ARCH=... tools/build_appimage.sh   # omit ReportLab (smaller; Print explains how to add it)
```

Output lands in `dist/tbdtask-<version>-<arch>.AppImage`. The aarch64
build needs `ar`/`tar` and network to fetch the arm64 runtime .debs; point
`DEB_DIR` at a directory of pre-downloaded .debs to build offline.

## Cut a release

Tag a commit on `main` and push; the
[`Release` workflow](.github/workflows/release.yml) cross-builds the
aarch64 AppImage and attaches it to the matching GitHub Release:

```sh
git tag v0.1.1
git push origin v0.1.1
```

Manual runs from the Actions tab also work — useful for ad-hoc dev builds.

## Future considerations

`docs/architecture.md` sketches a "browser-owns-the-data, server-is-stateless"
redesign for a hypothetical hosted/remote-access deployment. **It is not
the current architecture.** The current app is a local Pi tool with
server-side SQLite, which is the right design for the documented use
case (one team, one device, no public internet).

The doc is kept as a reference in case the deployment story ever changes.
If you're trying to understand how the app works *today*, ignore that file
and read this README plus the source.
