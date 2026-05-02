"""
Seed a populated SQLite database for demos and screenshots.

Creates fictional personnel, qualifications, an in-progress weekly worklist
with tasks and assignments, a few absences (full + partial day), PRDs that
trigger alerts, an expiring qualification, and a recurring task template.

Run on a fresh install (or after deleting data/tbdtask.db) to get a populated
view immediately. Real data goes through the UI.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, time as time_t

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app import models as M
from app.services.task_generator import generate_for_worklist
from app.services.alerts import recompute as recompute_alerts


# (rate, last_name, group_label) — fictional Navy-style roster.
ROSTER = [
    # Khakis
    ("CWO2", "Reyes", "Khakis"),
    ("BMC", "Holland", "Khakis"),
    ("BMC(Sel)", "Brennan", "Khakis"),
    # E-6
    ("EN1", "Banks", "E6"),
    ("BM1", "Wallace", "E6"),
    ("EN1", "Spencer", "E6"),
    ("CM1", "Hartman", "E6"),
    ("QM1", "Carrington", "E6"),
    # E-5
    ("ET2", "Mason", "E5"),
    ("EN2", "Foster", "E5"),
    ("CM2", "Tanner", "E5"),
    ("CM2", "Sandoval", "E5"),
    ("BM2", "Avalos", "E5"),
    ("EN2", "Vega", "E5"),
    ("EN2", "Juarez", "E5"),
    ("GM2", "Garrison", "E5"),
    ("BM2", "Garcia", "E5"),
    # Junior
    ("MM3", "Sutton", "Junior"),
    ("MM3", "Pearce", "Junior"),
    ("BM3", "Tate", "Junior"),
    ("ITSN", "Estrada", "Junior"),
    ("BMSN", "Riley", "Junior"),
    ("SN", "Mendoza", "Junior"),
    ("SN", "Beck", "Junior"),
    ("SN", "Adler", "Junior"),
    ("SN", "Ellis", "Junior"),
    ("SN", "Greer", "Junior"),
    ("SN", "Townsend", "Junior"),
    ("SN", "Sloane", "Junior"),
    ("SN", "Hayward", "Junior"),
    ("SN", "Acevedo", "Junior"),
    ("ENFN", "Yates", "Junior"),
    ("SN", "Sims", "Junior"),
    ("BMSR", "Olson", "Junior"),
    ("CMCR", "Marsh", "Junior"),
    ("CM2", "Reilly", "Junior"),
]

PAYGRADE = {
    "CWO2": "W-2", "BMC": "E-7", "BMC(Sel)": "E-6",
    "BM1": "E-6", "EN1": "E-6", "CM1": "E-6", "QM1": "E-6", "ET1": "E-6",
    "BM2": "E-5", "EN2": "E-5", "CM2": "E-5", "GM2": "E-5", "ET2": "E-5",
    "BM3": "E-4", "EN3": "E-4", "CM3": "E-4", "MM3": "E-4",
    "SN": "E-3", "BMSN": "E-3", "ITSN": "E-3", "ENFN": "E-3",
    "BMSR": "E-1", "CMCR": "E-1",
}

# Qualification catalog — same shape as the legacy structure but renamed
# generically: WC (workcenter), DS (duty section), BPT (beach party team
# style), Vehicles.
QUALS = [
    ("Craftsman", "WC"), ("3M", "WC"), ("RPPO", "WC"), ("Workcenter", "WC"),
    ("POOW", "DS"), ("Rover", "DS"),
    ("SUROB", "BPT"), ("TC", "BPT"), ("Flank", "BPT"), ("RTO", "BPT"),
    ("SALPO", "BPT"), ("SALO", "BPT"), ("BPTC", "BPT"),
    ("HMMWV", "Vehicles"), ("MTVR", "Vehicles"), ("DOZER", "Vehicles"),
    ("LARC Crew", "Vehicles"), ("LARC Eng.", "Vehicles"), ("LARC Cdr.", "Vehicles"),
]
PINNED = {"3M", "HMMWV", "MTVR", "DOZER", "LARC Crew", "LARC Eng.", "LARC Cdr."}


ABSENCE_CODES = [
    ("Leave", "Long-form leave"),
    ("TAD", "Temporary additional duty"),
    ("School", "School / formal training"),
    ("Medical", "Medical event (full day)"),
    ("Appt", "Appointment (often partial day)"),
    ("Other", "Free-text reason"),
]
TASK_CATEGORIES = ["Maintenance", "Corrective", "General"]


def _next_monday(today: date) -> date:
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def main() -> None:
    init_db()
    today = date.today()
    monday = _next_monday(today)

    with SessionLocal() as s:
        # Clean slate for the seeder so it's idempotent.
        s.query(M.TaskAssignment).delete()
        s.query(M.TaskInstance).delete()
        s.query(M.Worklist).delete()
        s.query(M.TaskTemplateRequiredQual).delete()
        s.query(M.TaskTemplate).delete()
        s.query(M.Absence).delete()
        s.query(M.PersonQual).delete()
        s.query(M.Qualification).delete()
        s.query(M.PersonDriversLicense).delete()
        s.query(M.PersonRosterStatus).delete()
        s.query(M.PersonPrd).delete()
        s.query(M.PersonDutySection).delete()
        s.query(M.PersonRate).delete()
        s.query(M.Person).delete()
        s.query(M.AbsenceCode).delete()
        s.query(M.TaskCategory).delete()
        s.query(M.Alert).delete()
        s.commit()

        # Codes / categories ------------------------------------------------
        for i, (code, desc) in enumerate(ABSENCE_CODES):
            s.add(M.AbsenceCode(code=code, description=desc, display_order=i))
        for i, name in enumerate(TASK_CATEGORIES):
            s.add(M.TaskCategory(name=name, display_order=i))
        s.flush()
        abs_codes = {c.code: c for c in s.scalars(select(M.AbsenceCode)).all()}
        cats = {c.name: c for c in s.scalars(select(M.TaskCategory)).all()}

        # Roster -----------------------------------------------------------
        people: list[M.Person] = []
        for i, (rate, last, group) in enumerate(ROSTER):
            display = f"{rate} {last}"
            p = M.Person(
                last_name=last, full_display=display,
                notes=f"Demo / {group}", display_order=i,
            )
            s.add(p)
            s.flush()
            s.add(M.PersonRate(
                person_id=p.id, rate=rate,
                paygrade=PAYGRADE.get(rate),
                valid_from=today,
            ))
            s.add(M.PersonRosterStatus(
                person_id=p.id, status="active", valid_from=today,
            ))
            ds = (i % 6) + 1
            s.add(M.PersonDutySection(person_id=p.id, duty_section=ds, valid_from=today))
            s.add(M.PersonDriversLicense(
                person_id=p.id,
                has_license=(i % 3 != 0),
                expires_on=(today + timedelta(days=400)) if (i % 3 != 0) else None,
                valid_from=today,
            ))
            people.append(p)

        # Pick named actors by last_name for readable references below.
        by_last = {p.last_name: p for p in people}

        # PRDs hitting each alert window
        s.add(M.PersonPrd(person_id=by_last["Tanner"].id, prd_date=today + timedelta(days=5),
                          change_reason="initial", valid_from=today))
        s.add(M.PersonPrd(person_id=by_last["Sandoval"].id, prd_date=today + timedelta(days=25),
                          change_reason="initial", valid_from=today))
        s.add(M.PersonPrd(person_id=by_last["Foster"].id, prd_date=today + timedelta(days=50),
                          change_reason="initial", valid_from=today))

        # Qualifications + per-person status -----------------------------
        quals: dict[str, M.Qualification] = {}
        for i, (name, cat) in enumerate(QUALS):
            q = M.Qualification(
                name=name, category=cat, code=name,
                pinned_column=(name in PINNED), display_order=i,
            )
            s.add(q)
            s.flush()
            quals[name] = q

        # Make Tanner broadly qualified, including HMMWV expiring in 12 days
        for qname in ("Craftsman", "3M", "RPPO", "POOW", "Rover",
                      "SUROB", "TC", "Flank", "HMMWV", "DOZER",
                      "LARC Crew", "LARC Eng."):
            q = quals[qname]
            ach = datetime.now() - timedelta(days=200)
            exp = None
            if qname == "HMMWV":
                ach = datetime.now() - timedelta(days=350)
                exp = datetime.now() + timedelta(days=12)
            s.add(M.PersonQual(
                person_id=by_last["Tanner"].id, qual_id=q.id, status="qualified",
                achieved_at=ach, expires_at=exp, valid_from=today,
            ))

        # A few in-progress and dinq examples
        for last, qname, status in [
            ("Sandoval", "Craftsman", "in_progress"),
            ("Sandoval", "POOW", "in_progress"),
            ("Vega", "POOW", "in_progress"),
            ("Mason", "Craftsman", "dinq"),
            ("Marsh", "3M", "dinq"),
        ]:
            s.add(M.PersonQual(
                person_id=by_last[last].id, qual_id=quals[qname].id,
                status=status,
                started_at=datetime.now() - timedelta(days=45) if status == "in_progress" else None,
                valid_from=today,
            ))

        s.flush()

        # Absences --------------------------------------------------------
        s.add(M.Absence(
            person_id=by_last["Juarez"].id,
            code_id=abs_codes["Leave"].id,
            start_date=monday, end_date=monday + timedelta(days=2),
            reason="Annual leave — family visit",
        ))
        s.add(M.Absence(
            person_id=by_last["Spencer"].id,
            code_id=abs_codes["TAD"].id,
            start_date=monday, end_date=monday + timedelta(days=4),
            reason="LARC operator school",
        ))
        s.add(M.Absence(
            person_id=by_last["Holland"].id,
            code_id=abs_codes["Appt"].id,
            start_date=monday + timedelta(days=1), end_date=monday + timedelta(days=1),
            start_time=time_t(9, 0), end_time=time_t(11, 0),
            reason="Dental",
        ))
        s.add(M.Absence(
            person_id=by_last["Estrada"].id,
            code_id=abs_codes["School"].id,
            start_date=monday + timedelta(days=3), end_date=monday + timedelta(days=4),
            reason="IT-A school refresher",
        ))

        # Recurring templates --------------------------------------------
        morning_quarters = M.TaskTemplate(
            name="Morning quarters",
            category_id=cats["General"].id,
            description="0700 muster and pass-down.",
            recurrence_rule={"kind": "weekdays", "weekdays": [0, 1, 2, 3, 4]},
            carry_over_policy="never",
            estimated_hours=0.5,
        )
        s.add(morning_quarters)
        tagout_audit = M.TaskTemplate(
            name="Tagout audit",
            category_id=cats["General"].id,
            description="Walk the tagout log and verify every active tag.",
            recurrence_rule={"kind": "weekdays", "weekdays": [0]},
            carry_over_policy="auto_same_person",
            estimated_hours=1.0,
        )
        s.add(tagout_audit)
        s.flush()

        # Worklist + ad-hoc tasks -----------------------------------------
        wl = M.Worklist(
            week_starting=monday,
            name=f"Week {((monday.day - 1) // 7) + 1} {monday.strftime('%B %Y')}",
            version=1,
        )
        s.add(wl)
        s.flush()
        generate_for_worklist(s, wl)

        ad_hoc = [
            (0, "Replace seatbelts on 947", "Maintenance", ["Tanner", "Mason"], "Tanner"),
            (0, "Update CESE training records", "Maintenance", ["Sandoval"], "Sandoval"),
            (1, "Investigate 402 hydraulic leak", "Corrective", ["Tanner"], "Tanner"),
            (1, "LSSV hard cards (file & sign)", "General", ["Ellis"], "Ellis"),
            (2, "Flush 685 power steering, refill", "Corrective", ["Foster", "Hayward"], "Foster"),
            (2, "Inspect 967 seatbelts", "Corrective", ["Marsh"], "Marsh"),
            (3, "Weapons fam range", "General", ["Tanner", "Banks", "Wallace"], "Banks"),
            (3, "MTVR hard cards", "General", ["Sandoval"], "Sandoval"),
        ]
        for offset, name, cat, assignees, poic in ad_hoc:
            inst = M.TaskInstance(
                worklist_id=wl.id,
                scheduled_date=monday + timedelta(days=offset),
                category_id=cats[cat].id,
                name=name, status="open",
            )
            s.add(inst)
            s.flush()
            for who in assignees:
                s.add(M.TaskAssignment(
                    instance_id=inst.id, person_id=by_last[who].id,
                    is_poic=(who == poic),
                ))

        # Carry-over candidate from the prior week
        prev_monday = monday - timedelta(days=14)
        prev = M.Worklist(
            week_starting=prev_monday,
            name=f"Week {((prev_monday.day - 1) // 7) + 1} {prev_monday.strftime('%B %Y')}",
            version=1,
        )
        s.add(prev)
        s.flush()
        prev_task = M.TaskInstance(
            worklist_id=prev.id,
            scheduled_date=prev_monday,
            category_id=cats["Maintenance"].id,
            name="Order 947 replacement seatbelts",
            status="in_progress",
        )
        s.add(prev_task)
        s.flush()
        s.add(M.TaskAssignment(
            instance_id=prev_task.id,
            person_id=by_last["Pearce"].id,
            is_poic=True,
        ))

        s.flush()
        recompute_alerts(s)
        s.commit()

    print("Demo data seeded.")


if __name__ == "__main__":
    main()
