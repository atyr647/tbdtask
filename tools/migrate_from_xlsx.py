"""
One-shot migration: read the legacy 'Weekly Crew Worklist.xlsx' workbook and
seed the SQLite database. NOT used at runtime; openpyxl is a dev-time
dependency only.

Scope: only the *current state* worth keeping is migrated -- personnel roster,
qualification catalog, and per-person qual status. The legacy leave tracker
and historical worklists are intentionally skipped: per the workbook owner
they are stale and would just bring noise into a clean DB.

Usage:
    python -m tools.migrate_from_xlsx [path/to/workbook.xlsx]
"""
from __future__ import annotations

import difflib
import json
import re
import sys
import warnings
from datetime import date, datetime
from pathlib import Path
from typing import Optional

warnings.filterwarnings("ignore")

import openpyxl  # type: ignore
from openpyxl.utils import get_column_letter  # type: ignore

from app.db import SessionLocal, init_db
from app import models as M


# Default workbook lives at repo root
DEFAULT_WORKBOOK = Path(__file__).resolve().parent.parent / "Weekly Crew Worklist.xlsx"

# Default catalog seeds.
ABSENCE_CODES = [
    ("Leave", "Long-form leave"),
    ("TAD", "Temporary additional duty"),
    ("School", "School / formal training"),
    ("Medical", "Medical event (full day)"),
    ("Appt", "Appointment (often partial day)"),
    ("Other", "Free-text reason"),
]
TASK_CATEGORIES = ["Maintenance", "Corrective", "General"]

# Excel layout constants
ROSTER_GROUPS = [
    # (group_label, column_letter)
    ("Khakis", "A"),
    ("E6", "D"),
    ("E5", "G"),
    ("Junior", "J"),
]
ROSTER_DATA_START_ROW = 9  # row 8 = header, names begin row 9

QUAL_LIST_HEADER_ROW = 2
QUAL_LIST_CATEGORY_ROW = 1
QUAL_LIST_NAME_COL = 1  # column A
QUAL_LIST_DATA_START_ROW = 3
# Pinned columns on the worklist (3M plus all vehicle quals).
PINNED_QUAL_NAMES = {"3M", "HMMWV", "MTVR", "DOZER", "LARC Crew", "LARC Eng.", "LARC Cdr."}

# Lightweight rate -> paygrade lookup. Unknowns leave paygrade NULL.
PAYGRADE_BY_RATE = {
    # Warrants / Officers
    "CWO2": "W-2", "CWO3": "W-3", "CWO4": "W-4", "CWO5": "W-5",
    "ENS": "O-1", "LTJG": "O-2", "LT": "O-3", "LCDR": "O-4",
    # Chiefs
    "BMC": "E-7", "BMC(Sel)": "E-6", "BMCS": "E-8", "BMCM": "E-9",
    "ENC": "E-7", "CMC": "E-7", "QMC": "E-7", "ETC": "E-7", "GMC": "E-7",
    # E-6
    "BM1": "E-6", "EN1": "E-6", "CM1": "E-6", "QM1": "E-6",
    "ET1": "E-6", "GM1": "E-6", "MM1": "E-6", "IT1": "E-6",
    # E-5
    "BM2": "E-5", "EN2": "E-5", "CM2": "E-5", "GM2": "E-5",
    "ET2": "E-5", "MM2": "E-5", "QM2": "E-5", "IT2": "E-5",
    # E-4
    "BM3": "E-4", "EN3": "E-4", "CM3": "E-4", "GM3": "E-4",
    "ET3": "E-4", "MM3": "E-4", "QM3": "E-4", "IT3": "E-4",
    # E-3
    "SN": "E-3", "BMSN": "E-3", "ENFN": "E-3", "CMCN": "E-3",
    "ITSN": "E-3", "GMSN": "E-3", "MMFN": "E-3", "QMSN": "E-3",
    # E-2
    "SA": "E-2", "FA": "E-2", "CN": "E-2",
    # E-1
    "SR": "E-1", "FR": "E-1", "BMSR": "E-1", "CMCR": "E-1",
    "ENFR": "E-1", "ITSR": "E-1", "GMSR": "E-1",
}


def split_rate_and_name(value: str) -> tuple[Optional[str], str]:
    """'CM2 Tyrone' -> ('CM2', 'Tyrone'). 'BMC (Sel) Borges' -> ('BMC(Sel)', 'Borges')."""
    s = value.strip()
    # Collapse parenthesised modifier into the rate token but keep a separator
    # before the surname, e.g. 'BMC (Sel) Borges' -> 'BMC(Sel) Borges'.
    s = re.sub(r"\s*\(([^)]+)\)\s*", r"(\1) ", s, count=1)
    parts = s.split(None, 1)
    if len(parts) == 1:
        return None, parts[0]
    rate, rest = parts
    return rate, rest.strip()


def normalize_name(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = s.replace(" (sel)", "").replace("(sel)", "")
    # Strip rate prefix to get bare last name for fuzzy matching.
    parts = s.split(None, 1)
    if len(parts) == 2 and parts[0].rstrip(".") in {p.lower() for p in PAYGRADE_BY_RATE}:
        return parts[1]
    return s


def fuzzy_lookup(norm: str, by_norm: dict) -> Optional["M.Person"]:
    """Fall back to closest-match if exact normalize doesn't hit. Tolerates
    typos like 'Sweney' vs 'Sweeney' (one-edit-distance), but never collapses
    distinct surnames."""
    direct = by_norm.get(norm)
    if direct is not None:
        return direct
    matches = difflib.get_close_matches(norm, by_norm.keys(), n=1, cutoff=0.9)
    if matches:
        return by_norm[matches[0]]
    return None


def run(workbook_path: Path) -> dict:
    init_db()
    wb = openpyxl.load_workbook(workbook_path, data_only=True)
    counts: dict[str, int] = {}
    today = date.today()

    with SessionLocal() as session:
        # Wipe any previous import data so the script is idempotent for dev.
        # We only delete tables this importer populates.
        session.query(M.PersonQual).delete()
        session.query(M.Qualification).delete()
        session.query(M.PersonDriversLicense).delete()
        session.query(M.PersonRosterStatus).delete()
        session.query(M.PersonPrd).delete()
        session.query(M.PersonDutySection).delete()
        session.query(M.PersonRate).delete()
        session.query(M.Person).delete()
        session.query(M.AbsenceCode).delete()
        session.query(M.TaskCategory).delete()
        session.query(M.ImportBatch).delete()
        session.commit()

        batch = M.ImportBatch(
            source_file=str(workbook_path),
            source_workbook_version=str(workbook_path.stat().st_mtime_ns),
            status="pending",
            notes="Initial migration from legacy xlsx",
        )
        session.add(batch)
        session.flush()

        # Catalogs ---------------------------------------------------------
        for i, (code, desc) in enumerate(ABSENCE_CODES):
            session.add(M.AbsenceCode(code=code, display_order=i, description=desc))
        for i, name in enumerate(TASK_CATEGORIES):
            session.add(M.TaskCategory(name=name, display_order=i))
        session.flush()

        # Personnel --------------------------------------------------------
        roster_ws = wb["Personnel Roster"]
        person_count = 0
        for group_label, col_letter in ROSTER_GROUPS:
            col = openpyxl.utils.column_index_from_string(col_letter)
            row = ROSTER_DATA_START_ROW
            order = 0
            while row <= roster_ws.max_row:
                cell = roster_ws.cell(row, col).value
                if not cell:
                    break
                rate, last_name = split_rate_and_name(str(cell))
                full_display = str(cell).strip()
                p = M.Person(
                    last_name=last_name,
                    full_display=full_display,
                    notes=f"Imported group: {group_label}",
                    display_order=person_count,
                    import_batch_id=batch.id,
                )
                session.add(p)
                session.flush()
                if rate:
                    paygrade = PAYGRADE_BY_RATE.get(rate)
                    session.add(M.PersonRate(
                        person_id=p.id, rate=rate, paygrade=paygrade,
                        valid_from=today, import_batch_id=batch.id,
                    ))
                session.add(M.PersonRosterStatus(
                    person_id=p.id, status="active",
                    valid_from=today, import_batch_id=batch.id,
                ))
                person_count += 1
                order += 1
                row += 1
        counts["persons"] = person_count
        session.flush()

        # Build a lookup table by normalized last-name fragments for cross-sheet matching.
        person_by_norm: dict[str, M.Person] = {}
        for p in session.query(M.Person).all():
            person_by_norm[normalize_name(p.full_display)] = p
            person_by_norm[p.last_name.lower()] = p

        # Qualifications ---------------------------------------------------
        qual_ws = wb["Qual List"]
        # Walk the header row to find each qual + its category.
        category_carry: Optional[str] = None
        qual_id_by_col: dict[int, int] = {}
        qual_count = 0
        for col in range(2, qual_ws.max_column + 1):
            cat = qual_ws.cell(QUAL_LIST_CATEGORY_ROW, col).value
            if cat:
                category_carry = str(cat).strip()
            name_val = qual_ws.cell(QUAL_LIST_HEADER_ROW, col).value
            if not name_val:
                continue
            name = str(name_val).strip()
            # Skip the legend cells ("1- Qualified", "2- Dinq") and totals column.
            if re.match(r"^\d+\s*-", name) or name.lower().startswith("total"):
                continue
            q = M.Qualification(
                name=name,
                code=name if len(name) <= 16 else None,
                category=category_carry,
                pinned_column=(name in PINNED_QUAL_NAMES),
                display_order=qual_count,
                import_batch_id=batch.id,
            )
            session.add(q)
            session.flush()
            qual_id_by_col[col] = q.id
            qual_count += 1
        counts["qualifications"] = qual_count

        # Person-quals (status from cell value: 1=qualified, 2=dinq).
        pq_count = 0
        added_from_quals = 0
        for row in range(QUAL_LIST_DATA_START_ROW, qual_ws.max_row + 1):
            name_cell = qual_ws.cell(row, QUAL_LIST_NAME_COL).value
            if not name_cell:
                break
            norm = normalize_name(str(name_cell))
            person = fuzzy_lookup(norm, person_by_norm)
            if not person:
                # Auto-create personnel that appear on the Qual List but were
                # missing from the Personnel Roster sheet (e.g. transfers in).
                rate, last_name = split_rate_and_name(str(name_cell))
                full_display = str(name_cell).strip()
                person = M.Person(
                    last_name=last_name,
                    full_display=full_display,
                    notes="Imported from Qual List (not on Personnel Roster sheet)",
                    display_order=person_count + added_from_quals,
                    import_batch_id=batch.id,
                )
                session.add(person)
                session.flush()
                if rate:
                    session.add(M.PersonRate(
                        person_id=person.id, rate=rate,
                        paygrade=PAYGRADE_BY_RATE.get(rate),
                        valid_from=today, import_batch_id=batch.id,
                    ))
                session.add(M.PersonRosterStatus(
                    person_id=person.id, status="active",
                    valid_from=today, import_batch_id=batch.id,
                ))
                person_by_norm[norm] = person
                person_by_norm[person.last_name.lower()] = person
                added_from_quals += 1
            for col, qual_id in qual_id_by_col.items():
                v = qual_ws.cell(row, col).value
                if v is None or v == "":
                    continue
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    continue
                if iv == 1:
                    status = "qualified"
                elif iv == 2:
                    status = "dinq"
                else:
                    continue
                session.add(M.PersonQual(
                    person_id=person.id, qual_id=qual_id, status=status,
                    valid_from=today, import_batch_id=batch.id,
                    achieved_at=datetime.combine(today, datetime.min.time()) if status == "qualified" else None,
                ))
                pq_count += 1
        counts["person_quals"] = pq_count
        counts["persons_added_from_qual_list"] = added_from_quals
        session.flush()

        # Absences and historical worklists are intentionally NOT imported.
        # Per the workbook owner the existing entries are stale and would
        # dirty the new database. Catalogs are seeded above so the app has
        # the right vocabulary out of the gate.

        # Finalize batch ---------------------------------------------------
        batch.status = "success"
        batch.finished_at = datetime.now()
        batch.row_counts = counts
        session.commit()

    return counts


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_WORKBOOK
    if not path.exists():
        print(f"workbook not found: {path}", file=sys.stderr)
        return 2
    counts = run(path)
    print(json.dumps(counts, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
