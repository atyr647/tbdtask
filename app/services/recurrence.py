"""
Recurrence rule evaluator.

Rules are stored as JSON on ``TaskTemplate.recurrence_rule``. Supported
shapes:

  {"kind": "daily"}                          # every day in the window
  {"kind": "weekdays", "weekdays": [0,2,4]}  # Mon=0 .. Sun=6
  {"kind": "every_n_weeks",
   "n": 2,
   "weekday": 0,
   "anchor": "YYYY-MM-DD"}                   # parity computed from anchor's Monday
  {"kind": "monthly_date", "day": 15}        # day-of-month
  {"kind": "monthly_nth_weekday",
   "n": 1, "weekday": 0}                     # 1st Monday of the month (n in 1..5, -1=last)

The evaluator returns the list of dates in the requested window that
match the rule. Window defaults to a 7-day Mon..Sun span.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from typing import Optional


def _isoparse(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def dates_in_window(rule: dict, start: date, days: int = 7) -> list[date]:
    if not rule or not isinstance(rule, dict):
        return []
    kind = rule.get("kind")
    window = [start + timedelta(days=i) for i in range(days)]
    out: list[date] = []
    if kind == "daily":
        return list(window)
    if kind == "weekdays":
        weekdays = set(int(x) for x in rule.get("weekdays", []))
        return [d for d in window if d.weekday() in weekdays]
    if kind == "every_n_weeks":
        n = max(1, int(rule.get("n", 1)))
        weekday = int(rule.get("weekday", 0))
        anchor = _isoparse(rule.get("anchor")) or start
        # Anchor Monday for parity.
        anchor_monday = anchor - timedelta(days=anchor.weekday())
        for d in window:
            if d.weekday() != weekday:
                continue
            d_monday = d - timedelta(days=d.weekday())
            weeks_apart = (d_monday - anchor_monday).days // 7
            if weeks_apart % n == 0 and weeks_apart >= 0:
                out.append(d)
        return out
    if kind == "monthly_date":
        day = int(rule.get("day", 1))
        for d in window:
            last = monthrange(d.year, d.month)[1]
            target = min(day, last)
            if d.day == target:
                out.append(d)
        return out
    if kind == "monthly_nth_weekday":
        n = int(rule.get("n", 1))
        weekday = int(rule.get("weekday", 0))
        for d in window:
            if d.weekday() != weekday:
                continue
            if n == -1:
                last = monthrange(d.year, d.month)[1]
                # Find last `weekday` of the month.
                final = date(d.year, d.month, last)
                final = final - timedelta(days=(final.weekday() - weekday) % 7)
                if d == final:
                    out.append(d)
            else:
                first = date(d.year, d.month, 1)
                offset = (weekday - first.weekday()) % 7
                nth = first + timedelta(days=offset + 7 * (n - 1))
                if d == nth:
                    out.append(d)
        return out
    return []


def describe(rule: dict) -> str:
    if not rule:
        return "(none)"
    kind = rule.get("kind", "?")
    if kind == "daily":
        return "Every day"
    if kind == "weekdays":
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        wds = sorted(int(x) for x in rule.get("weekdays", []))
        return (
            "Weekdays: " + ", ".join(names[i] for i in wds)
            if wds
            else "Weekdays: (none)"
        )
    if kind == "every_n_weeks":
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return f"Every {rule.get('n', 1)} weeks on {names[int(rule.get('weekday', 0))]}"
    if kind == "monthly_date":
        return f"Day {rule.get('day', '?')} of each month"
    if kind == "monthly_nth_weekday":
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        n = rule.get("n", 1)
        prefix = (
            "Last"
            if n == -1
            else f"{n}{['st', 'nd', 'rd', 'th', 'th'][min(int(n) - 1, 4)]}"
        )
        return f"{prefix} {names[int(rule.get('weekday', 0))]} of each month"
    return f"({kind})"
