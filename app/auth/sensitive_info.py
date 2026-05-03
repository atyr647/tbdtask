"""Sensitive-information detection (Phase 6).

Two complementary concerns:

1. **PII pattern scanning** — regex-based heuristics that flag content
   resembling Social Security Numbers, phone numbers, email addresses,
   and other patterns that should not appear in operational notes.
   These are *soft* warnings: the user can proceed after acknowledging.

2. **Sensitive-info acknowledgment** — a server-side gate that requires
   the operator to explicitly confirm they understand the data-classification
   policy before writing to high-sensitivity fields (Person.notes,
   Absence.reason, etc.).

The patterns are intentionally conservative. False positives are preferred
over false negatives because the consequence of missing PII in operational
notes is far worse than a harmless warning on a innocuous string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# PII/CUI pattern definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SensitivityPattern:
    """A regex pattern that, when matched, flags potential PII/CUI."""

    code: str
    """Short machine-readable identifier (e.g. 'ssn', 'phone_us')."""

    label: str
    """Human-readable description shown in the warning."""

    pattern: re.Pattern
    """Compiled regex. Should use word boundaries where appropriate."""

    severity: str = "warning"
    """'warning' for advisory, 'block' for hard-stop (future)."""


# SSN: xxx-xx-xxxx or xxx xx xxxx (with optional surrounding context)
_PAT_SSN = re.compile(
    r"(?<!\d)"  # not preceded by a digit
    r"\d{3}[-\s]\d{2}[-\s]\d{4}"
    r"(?!\d)"  # not followed by a digit
)

# US phone: (xxx) xxx-xxxx, xxx-xxx-xxxx, xxx.xxx.xxxx, +1 formats
_PAT_PHONE_US = re.compile(
    r"(?:(?:\+1|1)[-\s]?)?"
    r"(?:\(?\d{3}\)?[-\s.]?\d{3}[-\s.]?\d{4})"
)

# Email address — catches anything that looks like an email
_PAT_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Date of birth context: "DOB:", "date of birth", "born" followed by a date
_PAT_DOB_CONTEXT = re.compile(
    r"(?:DOB|date\s+of\s+birth|born|birth\s+date)\s*[:\-]?\s*"
    r"(?:\d{1,2}[-/]\d{1,2}[-/]\d{2,4}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)

# SSN-like without dashes: 9 consecutive digits (but not a year like 2024...)
_PAT_SSN_PLAIN = re.compile(
    r"(?<!\d)"
    r"(?!000|666|9\d{2})"  # SSN never starts with 000, 666, or 9xx
    r"\d{3}"
    r"(?!00)"  # middle group never 00
    r"\d{2}"
    r"(?!0000)"  # last group never 0000
    r"\d{4}"
    r"(?!\d)"
)

# Medical context keywords followed by free text
_PAT_MEDICAL = re.compile(
    r"\b(?:diagnosis|treatment|medication|prescription|therapy|patient|medical|clinic|hospital|doctor|physician|nurse|surgery|procedure|condition|disorder|disease|symptom)\b",
    re.IGNORECASE,
)

# Security-clearance context
_PAT_CLEARANCE = re.compile(
    r"\b(?:TS/SCI|TS-SCI|top secret|secret clearance|confidential|SCI|SAP|special\s+access|clearance\s+level)\b",
    re.IGNORECASE,
)

# Financial account patterns (routing number + account number context)
_PAT_FINANCIAL = re.compile(
    r"(?:routing|account|bank|direct\s+deposit|ACH|wire)\s*(?:number|num|#)?\s*[:\-]?\s*\d{8,}",
    re.IGNORECASE,
)


SENSITIVITY_PATTERNS: tuple[SensitivityPattern, ...] = (
    SensitivityPattern(
        code="ssn",
        label="Possible Social Security Number",
        pattern=_PAT_SSN,
        severity="warning",
    ),
    SensitivityPattern(
        code="ssn_plain",
        label="Possible SSN (no dashes)",
        pattern=_PAT_SSN_PLAIN,
        severity="warning",
    ),
    SensitivityPattern(
        code="phone_us",
        label="Possible US phone number",
        pattern=_PAT_PHONE_US,
        severity="warning",
    ),
    SensitivityPattern(
        code="email",
        label="Email address",
        pattern=_PAT_EMAIL,
        severity="warning",
    ),
    SensitivityPattern(
        code="dob_context",
        label="Date of birth with context",
        pattern=_PAT_DOB_CONTEXT,
        severity="warning",
    ),
    SensitivityPattern(
        code="medical",
        label="Medical/health context",
        pattern=_PAT_MEDICAL,
        severity="warning",
    ),
    SensitivityPattern(
        code="clearance",
        label="Security clearance reference",
        pattern=_PAT_CLEARANCE,
        severity="warning",
    ),
    SensitivityPattern(
        code="financial",
        label="Financial account reference",
        pattern=_PAT_FINANCIAL,
        severity="warning",
    ),
)


# ---------------------------------------------------------------------------
# Scanning API
# ---------------------------------------------------------------------------


@dataclass
class SensitivityMatch:
    """A single pattern match within scanned text."""

    pattern_code: str
    label: str
    severity: str
    snippet: str
    """Up to 60 characters around the match for context."""


@dataclass
class SensitivityReport:
    """Result of scanning text against all sensitivity patterns."""

    text_length: int
    matches: list[SensitivityMatch] = field(default_factory=list)

    @property
    def has_matches(self) -> bool:
        return len(self.matches) > 0

    @property
    def codes(self) -> list[str]:
        """Unique pattern codes that matched, in order of first occurrence."""
        seen: set[str] = set()
        result = []
        for m in self.matches:
            if m.pattern_code not in seen:
                seen.add(m.pattern_code)
                result.append(m.pattern_code)
        return result


def scan_text(text: Optional[str]) -> SensitivityReport:
    """Scan *text* against all sensitivity patterns.

    Returns a report with every match, including a short snippet for
    context. A None or empty text returns an empty report.
    """
    if not text:
        return SensitivityReport(text_length=0)

    report = SensitivityReport(text_length=len(text))

    for sp in SENSITIVITY_PATTERNS:
        for m in sp.pattern.finditer(text):
            start = max(0, m.start() - 20)
            end = min(len(text), m.end() + 20)
            snippet = text[start:end]
            if start > 0:
                snippet = "…" + snippet
            if end < len(text):
                snippet = snippet + "…"
            report.matches.append(
                SensitivityMatch(
                    pattern_code=sp.code,
                    label=sp.label,
                    severity=sp.severity,
                    snippet=snippet,
                )
            )

    # Sort by position in text (first occurrence order).
    report.matches.sort(key=lambda m: text.find(m.snippet.lstrip("…")))
    return report


# ---------------------------------------------------------------------------
# High-sensitivity field registry
# ---------------------------------------------------------------------------

# Fields that require the operator to acknowledge the sensitive-info
# policy before writing. The key is the route+field identifier used in
# the server-side gate.
SENSITIVE_FIELDS: dict[str, str] = {
    "person.notes": "Personnel notes — may contain personal, medical, or disciplinary information about individuals.",
    "absence.reason": "Absence reason — may contain medical or personal information.",
    "absence.notes": "Absence notes — may contain medical or personal information.",
    "person_qual.notes": "Qualification notes — may contain performance or medical information.",
    "alert.notes": "Alert resolution notes — may contain personnel action details.",
}


def is_sensitive_field(field_key: str) -> bool:
    """Return True if *field_key* is a registered sensitive field."""
    return field_key in SENSITIVE_FIELDS


def get_sensitive_field_label(field_key: str) -> Optional[str]:
    """Return the policy label for a sensitive field, or None."""
    return SENSITIVE_FIELDS.get(field_key)
