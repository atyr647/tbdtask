"""
Seed a populated SQLite database for demos and screenshots.

Creates fictional personnel, qualifications, an in-progress weekly worklist
with tasks and assignments, a few absences (full + partial day), departure dates that
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


# (title, last_name, group_label) — fictional civilian roster.
ROSTER = [
    ("Director", "Reyes", "Leadership"),
    ("Manager", "Holland", "Leadership"),
    ("Supervisor", "Brennan", "Senior"),
    ("Team Lead", "Banks", "Senior"),
    ("Team Lead", "Wallace", "Senior"),
    ("Coordinator", "Spencer", "Professional"),
    ("Planner", "Hartman", "Professional"),
    ("Scheduler", "Carrington", "Professional"),
    ("Analyst", "Mason", "Professional"),
    ("Technician", "Foster", "Professional"),
    ("Coordinator", "Tanner", "Professional"),
    ("Coordinator", "Sandoval", "Professional"),
    ("Operator", "Avalos", "Associate"),
    ("Technician", "Vega", "Professional"),
    ("Technician", "Juarez", "Professional"),
    ("Specialist", "Garrison", "Professional"),
    ("Operator", "Garcia", "Associate"),
    ("Associate", "Sutton", "Associate"),
    ("Associate", "Pearce", "Associate"),
    ("Assistant", "Tate", "Associate"),
    ("Assistant", "Estrada", "Associate"),
    ("Associate", "Riley", "Associate"),
    ("Associate", "Mendoza", "Associate"),
    ("Associate", "Beck", "Associate"),
    ("Associate", "Adler", "Associate"),
    ("Associate", "Ellis", "Associate"),
    ("Associate", "Greer", "Associate"),
    ("Associate", "Townsend", "Associate"),
    ("Associate", "Sloane", "Associate"),
    ("Associate", "Hayward", "Associate"),
    ("Associate", "Acevedo", "Associate"),
    ("Assistant", "Yates", "Associate"),
    ("Associate", "Sims", "Associate"),
    ("Intern", "Olson", "Associate"),
    ("Intern", "Marsh", "Associate"),
    ("Coordinator", "Reilly", "Professional"),
]

PAYGRADE = {
    "Director": "L5", "Manager": "L4", "Supervisor": "L3", "Team Lead": "L3",
    "Coordinator": "L2", "Planner": "L2", "Scheduler": "L2", "Analyst": "L2",
    "Technician": "L2", "Specialist": "L2", "Operator": "L1", "Associate": "L1",
    "Assistant": "L1", "Intern": "L0",
}

# Qualification catalog — same shape as the production structure, but with
# civilian certification names.
QUALS = [
    ("Safety Certified", "Core"), ("Quality Review", "Core"), ("Purchasing", "Core"),
    ("Team Operations", "Core"), ("Front Desk", "Coverage"), ("Field Rover", "Coverage"),
    ("Site Survey", "Field"), ("Traffic Control", "Field"), ("Communications", "Field"),
    ("Incident Lead", "Field"), ("Vehicle", "Equipment"), ("Forklift", "Equipment"),
    ("Bulldozer", "Equipment"), ("Boat Crew", "Equipment"), ("Boat Engineer", "Equipment"),
    ("Boat Lead", "Equipment"),
]
PINNED = {"Quality Review", "Vehicle", "Forklift", "Bulldozer", "Boat Crew", "Boat Engineer", "Boat Lead"}


ABSENCE_CODES = [
    ("Leave", None),
    ("Travel", "Work travel / offsite assignment"),
    ("School", "School / formal training"),
    ("Medical", None),
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

        # Planned departure dates hitting each alert window
        s.add(M.PersonPrd(person_id=by_last["Tanner"].id, prd_date=today + timedelta(days=5),
                          change_reason="initial", valid_from=today))
        s.add(M.PersonPrd(person_id=by_last["Sandoval"].id, prd_date=today + timedelta(days=25),
                          change_reason="initial", valid_from=today))
        s.add(M.PersonPrd(person_id=by_last["Foster"].id, prd_date=today + timedelta(days=50),
                          change_reason="initial", valid_from=today))
        # 10 months out — triggers the departure-planning window alert
        s.add(M.PersonPrd(person_id=by_last["Vega"].id, prd_date=today + timedelta(days=300),
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
        for qname in ("Safety Certified", "Quality Review", "Purchasing", "Front Desk", "Field Rover",
                      "Site Survey", "Traffic Control", "Communications", "Vehicle", "Bulldozer",
                      "Boat Crew", "Boat Engineer"):
            q = quals[qname]
            ach = datetime.now() - timedelta(days=200)
            exp = None
            if qname == "Vehicle":
                ach = datetime.now() - timedelta(days=350)
                exp = datetime.now() + timedelta(days=12)
            s.add(M.PersonQual(
                person_id=by_last["Tanner"].id, qual_id=q.id, status="qualified",
                achieved_at=ach, expires_at=exp, valid_from=today,
            ))

        # A few in-progress and dinq examples
        for last, qname, status in [
            ("Sandoval", "Safety Certified", "in_progress"),
            ("Sandoval", "Front Desk", "in_progress"),
            ("Vega", "Front Desk", "in_progress"),
            ("Mason", "Safety Certified", "dinq"),
            ("Marsh", "Quality Review", "dinq"),
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
            code_id=abs_codes["Travel"].id,
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
