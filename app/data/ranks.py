"""
Navy rate / rank catalog.

The catalog drives the rate picker on personnel forms and the paygrade /
group derivation. It is intentionally generous on enlisted ratings rather
than exhaustive: the rate input is free text, so an operator can type any
custom code (the paygrade column will just be blank for unknown rates).

Each entry: (code, label, paygrade, category)
  * code      - what gets stored on PersonRate.rate
  * label     - long-form label shown in the picker
  * paygrade  - "O-1".."O-10", "W-2".."W-5", "E-1".."E-9", or None
  * category  - "Officer" | "CWO" | "Enlisted" — drives the picker grouping

Group rule (derived elsewhere from paygrade):
  * Officer + CWO              -> Khakis
  * Enlisted E-7..E-9          -> Khakis
  * Enlisted E-6               -> E6
  * Enlisted E-5               -> E5
  * Enlisted E-1..E-4          -> Junior
"""
from __future__ import annotations

from typing import Iterable

# ---------------------------------------------------------------------------
# Officers
# ---------------------------------------------------------------------------
OFFICER_RANKS: list[tuple[str, str, str]] = [
    ("ENS",  "Ensign",                 "O-1"),
    ("LTJG", "Lieutenant Junior Grade","O-2"),
    ("LT",   "Lieutenant",             "O-3"),
    ("LCDR", "Lieutenant Commander",   "O-4"),
    ("CDR",  "Commander",              "O-5"),
    ("CAPT", "Captain",                "O-6"),
    ("RDML", "Rear Admiral (lower)",   "O-7"),
    ("RADM", "Rear Admiral",           "O-8"),
    ("VADM", "Vice Admiral",           "O-9"),
    ("ADM",  "Admiral",                "O-10"),
]

# ---------------------------------------------------------------------------
# Chief Warrant Officers
# ---------------------------------------------------------------------------
CWO_RANKS: list[tuple[str, str, str]] = [
    ("CWO2", "Chief Warrant Officer 2", "W-2"),
    ("CWO3", "Chief Warrant Officer 3", "W-3"),
    ("CWO4", "Chief Warrant Officer 4", "W-4"),
    ("CWO5", "Chief Warrant Officer 5", "W-5"),
]

# ---------------------------------------------------------------------------
# Enlisted ratings
# ---------------------------------------------------------------------------
# Curated list of Navy rating roots used in the typical Seabee / surface
# context. Each root expands to E-3 through E-9 via _expand_rating below.
# Operators can still type any rate not in this list — the input is free
# text and the datalist only provides suggestions.
RATING_ROOTS: list[tuple[str, str, str]] = [
    # (root_code, long_name, apprenticeship)
    # apprenticeship is "Seaman" | "Fireman" | "Constructionman" | "Hospitalman" | "Airman"
    ("BM", "Boatswain's Mate",        "Seaman"),
    ("QM", "Quartermaster",           "Seaman"),
    ("OS", "Operations Specialist",   "Seaman"),
    ("YN", "Yeoman",                  "Seaman"),
    ("LS", "Logistics Specialist",    "Seaman"),
    ("PS", "Personnel Specialist",    "Seaman"),
    ("CS", "Culinary Specialist",     "Seaman"),
    ("RP", "Religious Programs Sp.",  "Seaman"),
    ("MA", "Master-at-Arms",          "Seaman"),
    ("IT", "Information Systems Tech","Seaman"),
    ("ET", "Electronics Technician",  "Seaman"),
    ("FC", "Fire Controlman",         "Seaman"),
    ("GM", "Gunner's Mate",           "Seaman"),
    ("MN", "Mineman",                 "Seaman"),
    ("ST", "Sonar Technician",        "Seaman"),
    ("EN", "Engineman",               "Fireman"),
    ("MM", "Machinist's Mate",        "Fireman"),
    ("EM", "Electrician's Mate",      "Fireman"),
    ("IC", "Interior Communications", "Fireman"),
    ("HT", "Hull Maint. Technician",  "Fireman"),
    ("DC", "Damage Controlman",       "Fireman"),
    ("GS", "Gas Turbine System Tech", "Fireman"),
    ("BU", "Builder",                 "Constructionman"),
    ("CE", "Construction Electrician","Constructionman"),
    ("CM", "Construction Mechanic",   "Constructionman"),
    ("EA", "Engineering Aide",        "Constructionman"),
    ("EO", "Equipment Operator",      "Constructionman"),
    ("SW", "Steelworker",             "Constructionman"),
    ("UT", "Utilitiesman",            "Constructionman"),
    ("HM", "Hospital Corpsman",       "Hospitalman"),
    ("AB", "Aviation Boatswain's Mate","Airman"),
    ("AT", "Aviation Electronics T.", "Airman"),
    ("AE", "Aviation Electrician's M.","Airman"),
    ("AM", "Aviation Structural Mech","Airman"),
    ("AO", "Aviation Ordnanceman",    "Airman"),
    ("AS", "Aviation Support Eq. T.", "Airman"),
    ("AZ", "Aviation Maint. Admin.",  "Airman"),
    ("AW", "Naval Aircrewman",        "Airman"),
]

# Apprenticeship suffixes by paygrade tier for the E-3 striker code.
APPRENTICESHIP_E3_SUFFIX = {
    "Seaman": "SN",
    "Fireman": "FN",
    "Constructionman": "CN",
    "Hospitalman": "HN",
    "Airman": "AN",
}

# Generic apprenticeship codes (no rating yet selected) — paygrade E-3..E-1.
GENERIC_APPRENTICE: list[tuple[str, str, str]] = [
    ("SN",   "Seaman",                "E-3"),
    ("SA",   "Seaman Apprentice",     "E-2"),
    ("SR",   "Seaman Recruit",        "E-1"),
    ("FN",   "Fireman",               "E-3"),
    ("FA",   "Fireman Apprentice",    "E-2"),
    ("FR",   "Fireman Recruit",       "E-1"),
    ("CN",   "Constructionman",       "E-3"),
    ("CA",   "Constructionman App.",  "E-2"),
    ("CR",   "Constructionman Rec.",  "E-1"),
    ("HN",   "Hospitalman",           "E-3"),
    ("HA",   "Hospitalman App.",      "E-2"),
    ("HR",   "Hospitalman Rec.",      "E-1"),
    ("AN",   "Airman",                "E-3"),
    ("AA",   "Airman Apprentice",     "E-2"),
    ("AR",   "Airman Recruit",        "E-1"),
]


def _expand_rating(root: str, name: str, apprenticeship: str) -> list[tuple[str, str, str]]:
    """Yield every paygrade variant of a rating root."""
    sn = APPRENTICESHIP_E3_SUFFIX[apprenticeship]
    out: list[tuple[str, str, str]] = []
    # E-3 striker: <root><apprenticeship E-3 suffix>, e.g., BMSN
    out.append((f"{root}{sn}",        f"{name} (E-3 striker)",       "E-3"))
    # E-3 striker apprentice / recruit
    out.append((f"{root}{sn[0]}A",    f"{name} (E-2 striker)",       "E-2"))
    out.append((f"{root}{sn[0]}R",    f"{name} (E-1 striker)",       "E-1"))
    # E-4 / E-5 / E-6 petty officer ranks
    out.append((f"{root}3",            f"{name} 3rd Class (E-4)",     "E-4"))
    out.append((f"{root}2",            f"{name} 2nd Class (E-5)",     "E-5"))
    out.append((f"{root}1",            f"{name} 1st Class (E-6)",     "E-6"))
    # E-7..E-9 chief tiers
    out.append((f"{root}C",            f"{name} Chief (E-7)",         "E-7"))
    out.append((f"{root}CS",           f"{name} Senior Chief (E-8)",  "E-8"))
    out.append((f"{root}CM",           f"{name} Master Chief (E-9)",  "E-9"))
    # Selectee variants (still E-6 by paygrade but treated as Khakis)
    out.append((f"{root}C(Sel)",       f"{name} Chief Selectee",      "E-6"))
    return out


def enlisted_rates() -> list[tuple[str, str, str]]:
    rates: list[tuple[str, str, str]] = []
    rates.extend(GENERIC_APPRENTICE)
    for root, name, app in RATING_ROOTS:
        rates.extend(_expand_rating(root, name, app))
    return rates


# ---------------------------------------------------------------------------
# Public catalog
# ---------------------------------------------------------------------------

def all_entries() -> list[dict]:
    """Flattened catalog with category attached. Officer / CWO entries don't
    follow the rating-root expansion so they're spelled out in the constants
    above."""
    out: list[dict] = []
    for code, label, paygrade in OFFICER_RANKS:
        out.append({"code": code, "label": label, "paygrade": paygrade, "category": "Officer"})
    for code, label, paygrade in CWO_RANKS:
        out.append({"code": code, "label": label, "paygrade": paygrade, "category": "CWO"})
    for code, label, paygrade in enlisted_rates():
        out.append({"code": code, "label": label, "paygrade": paygrade, "category": "Enlisted"})
    return out


def paygrade_for(code: str) -> str | None:
    code = (code or "").strip()
    for entry in all_entries():
        if entry["code"] == code:
            return entry["paygrade"]
    return None


def by_category() -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {"Officer": [], "CWO": [], "Enlisted": []}
    for e in all_entries():
        groups[e["category"]].append(e)
    return groups


def group_for(rate: str | None, paygrade: str | None) -> str:
    """Mil group classification driven by paygrade.

    * Officer (O-*) and CWO (W-*)         -> Khakis
    * Enlisted E-7..E-9                   -> Khakis
    * E-6 (incl. Chief Selectees)         -> E6
    * E-5                                 -> E5
    * E-1..E-4                            -> Junior
    * Unknown                             -> Other
    """
    if not rate:
        return "Other"
    if rate.endswith("(Sel)"):
        return "Khakis"
    if paygrade and (paygrade.startswith("O-") or paygrade.startswith("W-")):
        return "Khakis"
    if paygrade in ("E-7", "E-8", "E-9"):
        return "Khakis"
    if paygrade == "E-6":
        return "E6"
    if paygrade == "E-5":
        return "E5"
    if paygrade in ("E-1", "E-2", "E-3", "E-4"):
        return "Junior"
    return "Other"
