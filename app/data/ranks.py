"""Navy rank / rate catalog.

The person's military identity is split across two stored columns:

  * ``PersonRate.paygrade`` — the canonical grade: ``E-1``..``E-9`` (enlisted),
    ``W-2``..``W-5`` (warrant), ``O-1``..``O-10`` (officer).
  * ``PersonRate.rate`` — the display token shown before the name. For
    enlisted that's the rating + grade abbreviation (``BM3``, ``ITC``,
    ``GMCM``); for warrant/officer it's the rank abbreviation (``CWO3``,
    ``LT``). ``Person.full_display`` is ``"{rate} {last_name}"``.

The UI flow: pick a **paygrade** first. If it's an enlisted grade, a
**rating** picker appears (Navy ratings like BM/GM/IT); warrant and
officer grades have no rating. ``rate_token(paygrade, rating)`` assembles
the stored ``rate``.

Back-compat: ``all_entries`` / ``by_category`` / ``paygrade_for`` /
``group_for`` keep working for the FastAPI templates, which render a flat
picker rather than the Tk conditional form.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Paygrades
# ---------------------------------------------------------------------------

# (paygrade, kind). kind is one of: enlisted | warrant | officer.
PAYGRADES: list[tuple[str, str]] = [
    ("E-1", "enlisted"),
    ("E-2", "enlisted"),
    ("E-3", "enlisted"),
    ("E-4", "enlisted"),
    ("E-5", "enlisted"),
    ("E-6", "enlisted"),
    ("E-7", "enlisted"),
    ("E-8", "enlisted"),
    ("E-9", "enlisted"),
    ("W-2", "warrant"),
    ("W-3", "warrant"),
    ("W-4", "warrant"),
    ("W-5", "warrant"),
    ("O-1", "officer"),
    ("O-2", "officer"),
    ("O-3", "officer"),
    ("O-4", "officer"),
    ("O-5", "officer"),
    ("O-6", "officer"),
    ("O-7", "officer"),
    ("O-8", "officer"),
    ("O-9", "officer"),
    ("O-10", "officer"),
]

ENLISTED_GRADES: set[str] = {g for g, k in PAYGRADES if k == "enlisted"}
WARRANT_GRADES: set[str] = {g for g, k in PAYGRADES if k == "warrant"}
OFFICER_GRADES: set[str] = {g for g, k in PAYGRADES if k == "officer"}

# Warrant / officer rank abbreviations by paygrade.
WARRANT_RANK: dict[str, str] = {
    "W-2": "CWO2",
    "W-3": "CWO3",
    "W-4": "CWO4",
    "W-5": "CWO5",
}
OFFICER_RANK: dict[str, str] = {
    "O-1": "ENS",
    "O-2": "LTJG",
    "O-3": "LT",
    "O-4": "LCDR",
    "O-5": "CDR",
    "O-6": "CAPT",
    "O-7": "RDML",
    "O-8": "RADM",
    "O-9": "VADM",
    "O-10": "ADM",
}

# Enlisted grade suffixes for E-4 and up. Rated sailors append these to the
# rating: BM + E-4 -> BM3, BM + E-7 -> BMC, GM + E-8 -> GMCS, OS + E-9 -> OSCM.
# E-1..E-3 are handled separately because the suffix depends on the rating's
# apprenticeship community (see below), e.g. EN + E-3 -> ENFN, CM + E-3 -> CMCN.
ENLISTED_SUFFIX: dict[str, str] = {
    "E-4": "3",
    "E-5": "2",
    "E-6": "1",
    "E-7": "C",
    "E-8": "CS",
    "E-9": "CM",
}

# E-1..E-3 apprenticeship: a community letter + a grade letter.
#   grade:  E-1 -> R (recruit), E-2 -> A (apprentice), E-3 -> N (-man)
#   community letter: Seaman S, Fireman F, Airman A, Constructionman C,
#                     Hospitalman H.
# So a designated striker is RATING + community + grade (ENFN = Engineman
# Fireman, E-3); an undesignated sailor is just the apprentice token (FN).
E1E3_GRADE_LETTER: dict[str, str] = {"E-1": "R", "E-2": "A", "E-3": "N"}

COMMUNITIES: list[tuple[str, str]] = [
    ("seaman", "Seaman"),
    ("fireman", "Fireman"),
    ("airman", "Airman"),
    ("constructionman", "Constructionman"),
    ("hospitalman", "Hospitalman"),
]
COMMUNITY_LETTER: dict[str, str] = {
    "seaman": "S",
    "fireman": "F",
    "airman": "A",
    "constructionman": "C",
    "hospitalman": "H",
}
_LETTER_COMMUNITY = {v: k for k, v in COMMUNITY_LETTER.items()}

# Ratings whose apprenticeship community is NOT the default (Seaman).
_FIREMAN = {"MM", "MMN", "EN", "EM", "GSE", "GSM", "HT", "DC", "IC", "MR", "CWT"}
_AIRMAN = {
    "ABE",
    "ABF",
    "ABH",
    "AC",
    "AD",
    "AE",
    "AG",
    "AM",
    "AME",
    "AO",
    "AS",
    "AT",
    "AWF",
    "AWO",
    "AWR",
    "AWS",
    "AWV",
    "AZ",
    "PR",
}
_CONSTRUCTIONMAN = {"BU", "CE", "CM", "EA", "EO", "SW", "UT"}
_HOSPITALMAN = {"HM"}


def community_for_rating(rating: str | None) -> str:
    """The E-1..E-3 apprenticeship community for a rating (default Seaman)."""
    r = (rating or "").strip()
    if r in _FIREMAN:
        return "fireman"
    if r in _AIRMAN:
        return "airman"
    if r in _CONSTRUCTIONMAN:
        return "constructionman"
    if r in _HOSPITALMAN:
        return "hospitalman"
    return "seaman"


# Generic (non-rated) rank token when no rating is chosen. E-1..E-3 default
# to the Seaman apprentice token; the real one is computed in ``rate_token``
# from the chosen community.
NON_RATED: dict[str, str] = {
    "E-1": "SR",
    "E-2": "SA",
    "E-3": "SN",
    "E-4": "PO3",
    "E-5": "PO2",
    "E-6": "PO1",
    "E-7": "CPO",
    "E-8": "SCPO",
    "E-9": "MCPO",
}

# Every bare apprentice token (SR..HN) — used to detect undesignated E-1..E-3.
APPRENTICE_TOKENS: set[str] = {
    COMMUNITY_LETTER[c] + gl
    for c in COMMUNITY_LETTER
    for gl in E1E3_GRADE_LETTER.values()
}

NON_RATED_LABEL = "(non-rated)"

# ---------------------------------------------------------------------------
# Navy ratings (enlisted occupational specialties). Curated common set.
# ---------------------------------------------------------------------------
RATINGS: list[str] = [
    "ABE",
    "ABF",
    "ABH",
    "AC",
    "AD",
    "AE",
    "AG",
    "AM",
    "AME",
    "AO",
    "AS",
    "AT",
    "AWF",
    "AWO",
    "AWR",
    "AWS",
    "AWV",
    "AZ",
    "BM",
    "BU",
    "CE",
    "CM",
    "CS",
    "CSS",
    "CTI",
    "CTM",
    "CTN",
    "CTR",
    "CTT",
    "CWT",
    "DC",
    "EA",
    "EM",
    "EN",
    "EO",
    "EOD",
    "ET",
    "FC",
    "FCA",
    "FT",
    "GM",
    "GSE",
    "GSM",
    "HM",
    "HT",
    "IC",
    "IS",
    "IT",
    "LN",
    "LS",
    "MA",
    "MC",
    "MM",
    "MMN",
    "MN",
    "MR",
    "MT",
    "NC",
    "ND",
    "OS",
    "PR",
    "PS",
    "QM",
    "RP",
    "RW",
    "SB",
    "SO",
    "STG",
    "STS",
    "SW",
    "UT",
    "YN",
]


# ---------------------------------------------------------------------------
# Positions / billets
# ---------------------------------------------------------------------------
POSITIONS: list[str] = [
    "Department Head",
    "DLCPO",
    "LCPO",
    "DLPO",
    "LPO",
    "ALPO",
    "Workcenter Supervisor",
    "RPPO",
    "Career Counselor",
    "DCPO",
    "DRMO",
    "Hardcards",
    "Hazmat",
    "Licensing",
    "Tagout Audit",
    "Muster Report",
    "Sponsorship",
    "Tool Custodian",
    "Training",
    "Watchbills",
]


# ---------------------------------------------------------------------------
# Token assembly + grouping
# ---------------------------------------------------------------------------


def kind_for(paygrade: str | None) -> str | None:
    if not paygrade:
        return None
    for g, k in PAYGRADES:
        if g == paygrade:
            return k
    return None


def is_enlisted(paygrade: str | None) -> bool:
    return paygrade in ENLISTED_GRADES


def rate_token(
    paygrade: str | None, rating: str | None = None, community: str | None = None
) -> str | None:
    """Build the display rate/rank token from a paygrade.

    * officer/warrant -> the rank abbreviation (LT, CWO3).
    * enlisted E-4..E-9, rated -> rating + grade suffix (BM3, GMCS, OSCM).
    * enlisted E-1..E-3, rated striker -> rating + community + grade letter
      (BMSN, ENFN, CMCN, ADAN, HMHN).
    * enlisted E-1..E-3, undesignated -> the bare apprentice token for the
      chosen ``community`` (SN, FN, AN, CN, HN); defaults to Seaman.
    * enlisted E-4..E-9, non-rated -> PO3..MCPO.
    """
    if not paygrade:
        return None
    kind = kind_for(paygrade)
    if kind == "officer":
        return OFFICER_RANK.get(paygrade)
    if kind == "warrant":
        return WARRANT_RANK.get(paygrade)
    # enlisted
    rating = (rating or "").strip()
    rated = bool(rating) and rating != NON_RATED_LABEL
    if paygrade in E1E3_GRADE_LETTER:
        # Community comes from the rating when rated, else the explicit arg.
        comm = community_for_rating(rating) if rated else (community or "seaman")
        appr = COMMUNITY_LETTER[comm] + E1E3_GRADE_LETTER[paygrade]
        return f"{rating}{appr}" if rated else appr
    # E-4..E-9
    if rated:
        return f"{rating}{ENLISTED_SUFFIX[paygrade]}"
    return NON_RATED.get(paygrade)


def rating_of(rate: str | None, paygrade: str | None) -> str | None:
    """Best-effort reverse: extract the rating from a stored enlisted token,
    for pre-filling the edit form. Returns None for officer/warrant/non-rated."""
    if not rate or not is_enlisted(paygrade):
        return None
    if paygrade in E1E3_GRADE_LETTER:
        # Undesignated apprentice tokens (SN, FN, ...) carry no rating.
        if rate in APPRENTICE_TOKENS:
            return None
        # Striker token = RATING + community-letter + grade-letter (2 chars).
        if len(rate) > 2:
            candidate = rate[:-2]
            if candidate in RATINGS:
                return candidate
        return None
    if rate in NON_RATED.values():
        return None
    suffix = ENLISTED_SUFFIX.get(paygrade or "", "")
    if suffix and rate.endswith(suffix):
        candidate = rate[: -len(suffix)]
        if candidate in RATINGS:
            return candidate
    return None


def community_of(rate: str | None, paygrade: str | None) -> str | None:
    """Reverse the apprenticeship community from an E-1..E-3 token, so the
    edit form can re-select it for an undesignated junior sailor."""
    if not rate or paygrade not in E1E3_GRADE_LETTER:
        return None
    gl = E1E3_GRADE_LETTER[paygrade]
    if rate.endswith(gl) and len(rate) >= 2:
        letter = rate[-2]
        return _LETTER_COMMUNITY.get(letter)
    return None


def group_for(rate: str | None, paygrade: str | None = None) -> str:
    """Mess grouping for the active roster.

    Officers / Warrant Officers / Chief's Mess (E7-9) / Petty Officers
    (E4-6) / Junior Enlisted (E1-3). Accepts ``(rate, paygrade)`` for
    back-compat with the web routes; paygrade drives the result.
    """
    pg = paygrade
    if not pg:
        return "Unassigned"
    kind = kind_for(pg)
    if kind == "officer":
        return "Officers"
    if kind == "warrant":
        return "Warrant Officers"
    if pg in ("E-7", "E-8", "E-9"):
        return "Chief's Mess"
    if pg in ("E-4", "E-5", "E-6"):
        return "Petty Officers"
    if pg in ("E-1", "E-2", "E-3"):
        return "Junior Enlisted"
    return "Unassigned"


# Roster section order (mess precedence).
GROUP_ORDER: list[str] = [
    "Officers",
    "Warrant Officers",
    "Chief's Mess",
    "Petty Officers",
    "Junior Enlisted",
    "Unassigned",
]


# ---------------------------------------------------------------------------
# Back-compat surface for the FastAPI templates (flat picker)
# ---------------------------------------------------------------------------


def all_entries() -> list[dict]:
    """Flat list of selectable rank tokens for the web picker.

    Enlisted entries are the generic (non-rated) rank tokens; the Tk app
    uses the richer paygrade+rating flow instead.
    """
    out: list[dict] = []
    for pg in OFFICER_GRADES:
        out.append(
            {
                "code": OFFICER_RANK[pg],
                "label": OFFICER_RANK[pg],
                "paygrade": pg,
                "category": "Officer",
            }
        )
    for pg in WARRANT_GRADES:
        out.append(
            {
                "code": WARRANT_RANK[pg],
                "label": WARRANT_RANK[pg],
                "paygrade": pg,
                "category": "Warrant Officer",
            }
        )
    for pg in sorted(ENLISTED_GRADES):
        out.append(
            {
                "code": NON_RATED[pg],
                "label": NON_RATED[pg],
                "paygrade": pg,
                "category": "Enlisted",
            }
        )
    # Stable order: officer (senior first), warrant, enlisted (junior first).
    return out


_CODE_TO_PAYGRADE = {e["code"]: e["paygrade"] for e in all_entries()}


def paygrade_for(code: str) -> str | None:
    code = (code or "").strip()
    if code in _CODE_TO_PAYGRADE:
        return _CODE_TO_PAYGRADE[code]
    # Try to recover the grade from a rated enlisted token (e.g. BM3 -> E-4).
    for pg, suffix in ENLISTED_SUFFIX.items():
        if suffix and code.endswith(suffix) and code[: -len(suffix)] in RATINGS:
            return pg
    return None


def by_category() -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {
        "Officer": [],
        "Warrant Officer": [],
        "Enlisted": [],
    }
    for entry in all_entries():
        groups[entry["category"]].append(entry)
    return groups
