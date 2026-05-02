"""Tests for the availability service — % present math, partial-day handling."""
from __future__ import annotations

from datetime import date, time as time_t, timedelta

from app.services.availability import get_day_report
from tests.conftest import (
    make_absence, make_absence_codes, make_person,
)


def test_no_absences_means_100_percent_present(session):
    for i in range(4):
        make_person(session, f"Person{i}", display_order=i)
    session.commit()

    rep = get_day_report(session, date(2026, 5, 4))
    assert rep.total == 4
    assert rep.present_full == 4
    assert rep.full_absent == 0
    assert rep.partial_absent == 0
    assert rep.percent_present == 100.0


def test_full_day_absences_subtract_from_present(session):
    codes = make_absence_codes(session)
    out_today = date(2026, 5, 4)
    p1 = make_person(session, "Out", display_order=0)
    make_person(session, "Here1", display_order=1)
    make_person(session, "Here2", display_order=2)
    make_person(session, "Here3", display_order=3)
    make_absence(session, person_id=p1.id, code_id=codes["Leave"].id,
                 start_date=out_today, end_date=out_today)
    session.commit()

    rep = get_day_report(session, out_today)
    assert rep.total == 4
    assert rep.full_absent == 1
    assert rep.partial_absent == 0
    assert rep.present_full == 3
    assert rep.percent_present == 75.0


def test_partial_day_counts_as_half_present(session):
    """Person with a partial-day appt counts as 0.5 absent, 0.5 present."""
    codes = make_absence_codes(session)
    today = date(2026, 5, 5)
    for i in range(4):
        make_person(session, f"P{i}", display_order=i)
    p_partial = make_person(session, "Appt", display_order=4)
    # 5 people total; 1 with partial-day Appt 09:00–11:00
    make_absence(session, person_id=p_partial.id, code_id=codes["Appt"].id,
                 start_date=today, end_date=today,
                 start_time=time_t(9, 0), end_time=time_t(11, 0),
                 reason="dental")
    session.commit()

    rep = get_day_report(session, today)
    assert rep.total == 5
    assert rep.full_absent == 0
    assert rep.partial_absent == 1
    assert rep.present_full == 4
    # (5 - 0 - 0.5) / 5 * 100 = 90.0
    assert rep.percent_present == 90.0


def test_mixed_full_and_partial_combine_correctly(session):
    """36 personnel, 2 full out, 1 partial-day out → 93.06% ≈ 93.1%."""
    codes = make_absence_codes(session)
    today = date(2026, 5, 5)
    persons = [make_person(session, f"P{i}", display_order=i) for i in range(36)]
    make_absence(session, person_id=persons[0].id, code_id=codes["Leave"].id,
                 start_date=today, end_date=today)
    make_absence(session, person_id=persons[1].id, code_id=codes["TAD"].id,
                 start_date=today, end_date=today)
    make_absence(session, person_id=persons[2].id, code_id=codes["Appt"].id,
                 start_date=today, end_date=today,
                 start_time=time_t(9, 0), end_time=time_t(11, 0))
    session.commit()

    rep = get_day_report(session, today)
    assert rep.total == 36
    assert rep.full_absent == 2
    assert rep.partial_absent == 1
    assert rep.present_full == 33
    # (36 - 2 - 0.5) / 36 * 100 ≈ 93.06 → rounded to 93.1
    assert rep.percent_present == 93.1


def test_multi_day_absence_covers_each_day_in_range(session):
    """A 5-day Leave block should reduce present count on each day."""
    codes = make_absence_codes(session)
    p1 = make_person(session, "OnLeave", display_order=0)
    make_person(session, "Here", display_order=1)
    monday = date(2026, 5, 4)
    make_absence(session, person_id=p1.id, code_id=codes["Leave"].id,
                 start_date=monday, end_date=monday + timedelta(days=4))
    session.commit()

    for offset in range(5):
        rep = get_day_report(session, monday + timedelta(days=offset))
        assert rep.full_absent == 1, f"day +{offset} should have 1 out"

    # The day after the range ends — they're back
    rep_after = get_day_report(session, monday + timedelta(days=5))
    assert rep_after.full_absent == 0


def test_archived_personnel_not_counted_in_total(session):
    from datetime import datetime as _dt
    p1 = make_person(session, "Active", display_order=0)
    p2 = make_person(session, "Departed", display_order=1)
    p2.active = False
    p2.archived_at = _dt.now()
    session.commit()

    rep = get_day_report(session, date(2026, 5, 4))
    assert rep.total == 1
    assert {r.person.id for r in rep.rows} == {p1.id}


def test_by_code_breakdown_groups_correctly(session):
    codes = make_absence_codes(session)
    today = date(2026, 5, 4)
    pa = make_person(session, "A", display_order=0)
    pb = make_person(session, "B", display_order=1)
    pc = make_person(session, "C", display_order=2)
    make_absence(session, person_id=pa.id, code_id=codes["Leave"].id,
                 start_date=today, end_date=today)
    make_absence(session, person_id=pb.id, code_id=codes["Leave"].id,
                 start_date=today, end_date=today)
    make_absence(session, person_id=pc.id, code_id=codes["TAD"].id,
                 start_date=today, end_date=today)
    session.commit()

    rep = get_day_report(session, today)
    assert rep.by_code == {"Leave": 2, "TAD": 1}
