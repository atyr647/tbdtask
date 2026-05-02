"""
Seed realistic demo data on top of a fresh migration so screenshots show
populated views: an in-progress weekly worklist with tasks and assignments,
a few absences (full + partial day), a PRD that triggers an alert, an
expiring qualification, and a recurring task template.

Run AFTER `python -m tools.migrate_from_xlsx`.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, time as time_t

from app.db import SessionLocal
from app import models as M
from app.services.task_generator import generate_for_worklist
from app.services.alerts import recompute as recompute_alerts


def _next_monday(today: date) -> date:
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def main() -> None:
    today = date.today()
    monday = _next_monday(today)

    with SessionLocal() as s:
        # Pick a handful of personnel to use as actors.
        people = list(s.scalars(
            __import__("sqlalchemy").select(M.Person)
            .where(M.Person.active == True)  # noqa: E712
            .order_by(M.Person.display_order)
        ).all())
        by_name = {p.full_display: p for p in people}

        def _person(name_substr: str) -> M.Person:
            for p in people:
                if name_substr.lower() in p.full_display.lower():
                    return p
            raise RuntimeError(f"no person matching {name_substr!r}")

        # Codes
        abs_codes = {c.code: c for c in s.scalars(__import__("sqlalchemy").select(M.AbsenceCode)).all()}
        cats = {c.name: c for c in s.scalars(__import__("sqlalchemy").select(M.TaskCategory)).all()}
        quals = {q.name: q for q in s.scalars(__import__("sqlalchemy").select(M.Qualification)).all()}

        # Duty sections, drivers licenses for variety
        for i, p in enumerate(people[:18]):
            ds = (i % 6) + 1
            s.add(M.PersonDutySection(person_id=p.id, duty_section=ds, valid_from=today))
            s.add(M.PersonDriversLicense(
                person_id=p.id,
                has_license=(i % 3 != 0),
                expires_on=(today + timedelta(days=400)) if (i % 3 != 0) else None,
                valid_from=today,
            ))

        # PRDs: one urgent (in 5 days), one warning (in 25 days), one info (in 50 days)
        prd_targets = [(_person("Tyrone"), today + timedelta(days=5)),
                       (_person("Sadang"), today + timedelta(days=25)),
                       (_person("Fuentes"), today + timedelta(days=50))]
        for p, d in prd_targets:
            s.add(M.PersonPrd(person_id=p.id, prd_date=d, change_reason="initial", valid_from=today))

        # Qual: make CM2 Tyrone's HMMWV expire in 12 days for an alert
        tyrone = _person("Tyrone")
        hmmwv = quals.get("HMMWV")
        if tyrone and hmmwv:
            row = s.scalars(
                __import__("sqlalchemy").select(M.PersonQual)
                .where(M.PersonQual.person_id == tyrone.id, M.PersonQual.qual_id == hmmwv.id, M.PersonQual.valid_to.is_(None))
            ).first()
            if row:
                row.expires_at = datetime.now() + timedelta(days=12)
                row.achieved_at = datetime.now() - timedelta(days=350)

        # Mark a few quals in_progress so the qual matrix has variety
        for name in ("Sadang", "Valdez", "Moye"):
            p = _person(name)
            for qname in ("Craftsman", "POOW"):
                q = quals.get(qname)
                if not q:
                    continue
                exists = s.scalars(
                    __import__("sqlalchemy").select(M.PersonQual)
                    .where(M.PersonQual.person_id == p.id, M.PersonQual.qual_id == q.id, M.PersonQual.valid_to.is_(None))
                ).first()
                if exists is None:
                    s.add(M.PersonQual(
                        person_id=p.id, qual_id=q.id, status="in_progress",
                        started_at=datetime.now() - timedelta(days=45),
                        valid_from=today,
                    ))

        # Absences --------------------------------------------------------
        # Full-day Leave (Mon-Wed) for one person
        s.add(M.Absence(
            person_id=_person("Jarmillo").id,
            code_id=abs_codes["Leave"].id,
            start_date=monday, end_date=monday + timedelta(days=2),
            reason="Annual leave — family visit",
        ))
        # TAD all week for another
        s.add(M.Absence(
            person_id=_person("Sweney").id,
            code_id=abs_codes["TAD"].id,
            start_date=monday, end_date=monday + timedelta(days=4),
            reason="LARC operator school",
        ))
        # Partial-day Appt (Tuesday 0900-1100) for a third
        s.add(M.Absence(
            person_id=_person("Jones").id,
            code_id=abs_codes["Appt"].id,
            start_date=monday + timedelta(days=1), end_date=monday + timedelta(days=1),
            start_time=time_t(9, 0), end_time=time_t(11, 0),
            reason="Dental",
        ))
        # School later in week for another
        s.add(M.Absence(
            person_id=_person("Espada").id,
            code_id=abs_codes["School"].id,
            start_date=monday + timedelta(days=3), end_date=monday + timedelta(days=4),
            reason="IT-A school refresher",
        ))
        # An older, "carry-source" absence too -- not strictly needed, skip.

        s.flush()

        # Recurring task template ----------------------------------------
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

        # Worklist for next Monday ---------------------------------------
        wl = M.Worklist(
            week_starting=monday,
            name=f"Week {((monday.day - 1) // 7) + 1} {monday.strftime('%B %Y')}",
            version=1,
        )
        s.add(wl)
        s.flush()

        # Generate recurring instances for this worklist
        generate_for_worklist(s, wl)

        # Add ad-hoc tasks across the week
        ad_hoc = [
            (0, "Replace seatbelts on 947", "Maintenance", ["Tyrone", "Moye"], "Tyrone"),
            (0, "Update CESE training records", "Maintenance", ["Sadang"], "Sadang"),
            (1, "Investigate 402 hydraulic leak", "Corrective", ["Tyrone"], "Tyrone"),
            (1, "LSSV hard cards (file & sign)", "General", ["Edwards"], "Edwards"),
            (2, "Flush 685 power steering, refill", "Corrective", ["Fuentes", "Haustein"], "Fuentes"),
            (2, "Inspect 967 seatbelts", "Corrective", ["Massie"], "Massie"),
            (3, "Weapons fam range", "General", ["Tyrone", "Barquilla", "Woodland"], "Barquilla"),
            (3, "MTVR hard cards", "General", ["Sadang"], "Sadang"),
        ]
        for offset, name, cat, assignees, poic in ad_hoc:
            inst = M.TaskInstance(
                worklist_id=wl.id,
                scheduled_date=monday + timedelta(days=offset),
                category_id=cats[cat].id,
                name=name,
                status="open",
            )
            s.add(inst)
            s.flush()
            for who in assignees:
                p = _person(who)
                s.add(M.TaskAssignment(
                    instance_id=inst.id, person_id=p.id,
                    is_poic=(who == poic),
                ))
        s.flush()

        # Carry-over candidate: a previous week's open task
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
            person_id=_person("Potok").id,
            is_poic=True,
        ))

        # Recompute alerts now so the dashboard banner is populated
        s.flush()
        recompute_alerts(s)
        s.commit()

    print("Demo data seeded.")


if __name__ == "__main__":
    main()
