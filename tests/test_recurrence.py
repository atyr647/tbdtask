"""Pure-function tests for the recurrence engine."""
from __future__ import annotations

from datetime import date

from app.services.recurrence import dates_in_window


def _monday(year, month, day):
    d = date(year, month, day)
    assert d.weekday() == 0, f"{d} is not a Monday"
    return d


# Daily ----------------------------------------------------------------------

def test_daily_covers_every_day_in_window():
    start = _monday(2026, 5, 4)
    out = dates_in_window({"kind": "daily"}, start, days=7)
    assert len(out) == 7
    assert out[0] == start
    assert out[-1] == date(2026, 5, 10)


# Weekday set ----------------------------------------------------------------

def test_weekdays_only_picks_specified_days():
    start = _monday(2026, 5, 4)
    out = dates_in_window({"kind": "weekdays", "weekdays": [0, 2, 4]}, start, 7)
    assert out == [date(2026, 5, 4), date(2026, 5, 6), date(2026, 5, 8)]


def test_weekdays_empty_means_no_dates():
    start = _monday(2026, 5, 4)
    assert dates_in_window({"kind": "weekdays", "weekdays": []}, start, 7) == []


# Every-N-weeks --------------------------------------------------------------

def test_every_2_weeks_with_anchor():
    anchor = _monday(2026, 5, 4)  # parity reference
    rule = {"kind": "every_n_weeks", "n": 2, "weekday": 0,
            "anchor": anchor.isoformat()}
    # Anchor week: fires
    assert dates_in_window(rule, anchor, 7) == [anchor]
    # Next week: does NOT fire (we want every other)
    next_monday = date(2026, 5, 11)
    assert dates_in_window(rule, next_monday, 7) == []
    # Two weeks later: fires
    fortnight = date(2026, 5, 18)
    assert dates_in_window(rule, fortnight, 7) == [fortnight]


def test_every_3_weeks_with_anchor_skips_two_weeks():
    anchor = _monday(2026, 5, 4)
    rule = {"kind": "every_n_weeks", "n": 3, "weekday": 0,
            "anchor": anchor.isoformat()}
    assert dates_in_window(rule, anchor, 7) == [anchor]
    assert dates_in_window(rule, date(2026, 5, 11), 7) == []
    assert dates_in_window(rule, date(2026, 5, 18), 7) == []
    assert dates_in_window(rule, date(2026, 5, 25), 7) == [date(2026, 5, 25)]


def test_every_n_weeks_before_anchor_does_not_fire():
    anchor = _monday(2026, 5, 11)
    rule = {"kind": "every_n_weeks", "n": 2, "weekday": 0,
            "anchor": anchor.isoformat()}
    # Window before the anchor — no firings.
    assert dates_in_window(rule, _monday(2026, 5, 4), 7) == []


# Monthly day-of-month -------------------------------------------------------

def test_monthly_date_fires_on_the_target_day():
    rule = {"kind": "monthly_date", "day": 15}
    out = dates_in_window(rule, date(2026, 5, 1), days=31)
    assert out == [date(2026, 5, 15)]


def test_monthly_date_handles_short_months():
    """Day 31 in February should fall on the last day of February."""
    rule = {"kind": "monthly_date", "day": 31}
    out = dates_in_window(rule, date(2026, 2, 1), days=28)
    # 2026 is not a leap year — Feb has 28 days
    assert out == [date(2026, 2, 28)]


def test_monthly_date_leap_year():
    rule = {"kind": "monthly_date", "day": 31}
    out = dates_in_window(rule, date(2024, 2, 1), days=29)
    assert out == [date(2024, 2, 29)]


# Monthly Nth weekday --------------------------------------------------------

def test_monthly_nth_weekday_first_monday():
    rule = {"kind": "monthly_nth_weekday", "n": 1, "weekday": 0}
    # May 2026: 1st Monday is May 4
    out = dates_in_window(rule, date(2026, 5, 1), days=14)
    assert out == [date(2026, 5, 4)]


def test_monthly_nth_weekday_last_friday():
    """n=-1 means the last weekday of that month."""
    rule = {"kind": "monthly_nth_weekday", "n": -1, "weekday": 4}  # Friday
    # May 2026: last Friday is May 29
    out = dates_in_window(rule, date(2026, 5, 1), days=31)
    assert out == [date(2026, 5, 29)]


def test_monthly_nth_weekday_third_thursday():
    rule = {"kind": "monthly_nth_weekday", "n": 3, "weekday": 3}  # Thursday
    # May 2026: Thursdays are 7, 14, 21, 28 — 3rd is May 21
    out = dates_in_window(rule, date(2026, 5, 1), days=31)
    assert out == [date(2026, 5, 21)]


# Pathological inputs --------------------------------------------------------

def test_unknown_kind_returns_empty():
    assert dates_in_window({"kind": "made-up"}, date(2026, 5, 4), 7) == []


def test_empty_rule_returns_empty():
    assert dates_in_window({}, date(2026, 5, 4), 7) == []


def test_none_rule_returns_empty():
    assert dates_in_window(None, date(2026, 5, 4), 7) == []
