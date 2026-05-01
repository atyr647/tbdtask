# tbdtask

Offline weekly worklist and personnel tracker. Designed to run on a Raspberry
Pi 400 as an AppImage with no network dependencies. The legacy
`Weekly Crew Worklist.xlsx` is treated as a one-shot migration source; the app
itself never reads or writes Excel at runtime.

## Stage 1 status

- SQLite schema with effective-dated personnel attributes, soft-delete,
  display ordering, and import provenance.
- One-shot migration script (`tools/migrate_from_xlsx.py`) that seeds
  Personnel Roster + Qualification catalog + per-person qual status from the
  legacy workbook. Absences and historical worklists are intentionally
  skipped (the legacy entries are stale).
- FastAPI + Jinja2 + SQLAlchemy app with read-only views for:
  - `/` &mdash; dashboard
  - `/personnel` and `/personnel/<id>`
  - `/quals`, `/quals/matrix`
  - `/admin/verify` &mdash; import provenance and live row counts

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

## Coming next

Stage 2: editable roster, quals, and absence tracking with partial-day
support.
