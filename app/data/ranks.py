"""Civilian title catalog.

The underlying model still stores the value in ``PersonRate.rate`` for schema
compatibility, but the UI presents it as a person's title. The input remains
free text: organizations can type any local title not listed here.

Each entry: (code, label, level, category)
  * code      - short title stored on PersonRate.rate
  * label     - long-form label shown in the picker
  * level     - optional broad level used for grouping
  * category  - drives the picker grouping
"""

from __future__ import annotations


TITLE_ENTRIES: list[tuple[str, str, str, str]] = [
    ("Director", "Director", "L5", "Leadership"),
    ("Manager", "Manager", "L4", "Leadership"),
    ("Supervisor", "Supervisor", "L3", "Leadership"),
    ("Team Lead", "Team Lead", "L3", "Leadership"),
    ("Coordinator", "Coordinator", "L2", "Operations"),
    ("Scheduler", "Scheduler", "L2", "Operations"),
    ("Planner", "Planner", "L2", "Operations"),
    ("Operator", "Operator", "L1", "Operations"),
    ("Specialist", "Specialist", "L2", "Technical"),
    ("Technician", "Technician", "L2", "Technical"),
    ("Analyst", "Analyst", "L2", "Technical"),
    ("Engineer", "Engineer", "L3", "Technical"),
    ("Administrator", "Administrator", "L2", "Support"),
    ("Assistant", "Assistant", "L1", "Support"),
    ("Associate", "Associate", "L1", "Support"),
    ("Intern", "Intern", "L0", "Support"),
]


def all_entries() -> list[dict]:
    return [
        {"code": code, "label": label, "paygrade": level, "category": category}
        for code, label, level, category in TITLE_ENTRIES
    ]


def paygrade_for(code: str) -> str | None:
    code = (code or "").strip()
    for entry in all_entries():
        if entry["code"] == code:
            return entry["paygrade"]
    return None


def by_category() -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {
        "Leadership": [],
        "Operations": [],
        "Technical": [],
        "Support": [],
    }
    for entry in all_entries():
        groups[entry["category"]].append(entry)
    return groups


def group_for(rate: str | None, paygrade: str | None) -> str:
    """Civilian roster grouping driven by title level."""
    if not rate:
        return "Other"
    if paygrade in ("L5", "L4"):
        return "Leadership"
    if paygrade == "L3":
        return "Senior"
    if paygrade == "L2":
        return "Professional"
    if paygrade in ("L1", "L0"):
        return "Associate"
    return "Other"
