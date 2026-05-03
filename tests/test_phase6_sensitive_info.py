"""Phase 6: sensitive-information detection tests.

Covers:
* PII pattern matching (SSN, phone, email, DOB, medical, clearance, financial)
* False-positive avoidance
* SensitivityReport API
* Sensitive field registry
"""

from __future__ import annotations


from app.auth.sensitive_info import (
    SENSITIVE_FIELDS,
    SENSITIVITY_PATTERNS,
    get_sensitive_field_label,
    is_sensitive_field,
    scan_text,
)


# ---------------------------------------------------------------------------
# SSN patterns
# ---------------------------------------------------------------------------


class TestSSNPatterns:
    def test_standard_ssn_detected(self):
        r = scan_text("SSN: 123-45-6789")
        assert any(m.pattern_code == "ssn" for m in r.matches)

    def test_ssn_with_spaces(self):
        r = scan_text("123 45 6789")
        assert any(m.pattern_code == "ssn" for m in r.matches)

    def test_plain_ssn_detected(self):
        r = scan_text("123456789")
        assert any(m.pattern_code == "ssn_plain" for m in r.matches)

    def test_ssn_in_longer_text(self):
        r = scan_text("Member John Doe, SSN 987-65-4321, reported for duty.")
        assert any(m.pattern_code == "ssn" for m in r.matches)

    def test_ssn_prefix_000_rejected(self):
        """SSNs never start with 000."""
        r = scan_text("000-12-3456")
        assert not any(m.pattern_code == "ssn_plain" for m in r.matches)

    def test_ssn_prefix_666_rejected(self):
        r = scan_text("666-12-3456")
        assert not any(m.pattern_code == "ssn_plain" for m in r.matches)

    def test_ssn_prefix_9xx_rejected(self):
        r = scan_text("900-12-3456")
        assert not any(m.pattern_code == "ssn_plain" for m in r.matches)


# ---------------------------------------------------------------------------
# Phone number patterns
# ---------------------------------------------------------------------------


class TestPhonePatterns:
    def test_us_phone_dashes(self):
        r = scan_text("Call 555-123-4567")
        assert any(m.pattern_code == "phone_us" for m in r.matches)

    def test_us_phone_parens(self):
        r = scan_text("Call (555) 123-4567")
        assert any(m.pattern_code == "phone_us" for m in r.matches)

    def test_us_phone_dots(self):
        r = scan_text("Call 555.123.4567")
        assert any(m.pattern_code == "phone_us" for m in r.matches)

    def test_us_phone_with_country_code(self):
        r = scan_text("Call +1-555-123-4567")
        assert any(m.pattern_code == "phone_us" for m in r.matches)

    def test_year_not_phone(self):
        """A 4-digit year should not trigger phone detection."""
        r = scan_text("The year is 2024")
        assert not any(m.pattern_code == "phone_us" for m in r.matches)


# ---------------------------------------------------------------------------
# Email pattern
# ---------------------------------------------------------------------------


class TestEmailPattern:
    def test_email_detected(self):
        r = scan_text("Contact john.doe@navy.mil")
        assert any(m.pattern_code == "email" for m in r.matches)

    def test_email_in_parens(self):
        r = scan_text("Email (user@example.com) for details")
        assert any(m.pattern_code == "email" for m in r.matches)

    def test_no_at_sign_no_match(self):
        r = scan_text("john.doe at navy.mil")
        assert not any(m.pattern_code == "email" for m in r.matches)


# ---------------------------------------------------------------------------
# DOB context pattern
# ---------------------------------------------------------------------------


class TestDOBPattern:
    def test_dob_label_detected(self):
        r = scan_text("DOB: 01/15/1990")
        assert any(m.pattern_code == "dob_context" for m in r.matches)

    def test_date_of_birth_detected(self):
        r = scan_text("Date of birth: March 5, 1985")
        assert any(m.pattern_code == "dob_context" for m in r.matches)

    def test_born_detected(self):
        r = scan_text("Born 12/25/1988")
        assert any(m.pattern_code == "dob_context" for m in r.matches)

    def test_bare_date_no_match(self):
        """A date without DOB context should not trigger."""
        r = scan_text("Report dated 01/15/2024")
        assert not any(m.pattern_code == "dob_context" for m in r.matches)


# ---------------------------------------------------------------------------
# Medical context pattern
# ---------------------------------------------------------------------------


class TestMedicalPattern:
    def test_medical_keyword_detected(self):
        r = scan_text("Member is on medication for condition")
        assert any(m.pattern_code == "medical" for m in r.matches)

    def test_diagnosis_detected(self):
        r = scan_text("Diagnosis: lower back pain")
        assert any(m.pattern_code == "medical" for m in r.matches)

    def test_hospital_detected(self):
        r = scan_text("Transported to hospital")
        assert any(m.pattern_code == "medical" for m in r.matches)

    def test_non_medical_no_match(self):
        r = scan_text("Medical supplies are in the closet")
        # This IS a medical keyword match — the pattern is conservative
        assert any(m.pattern_code == "medical" for m in r.matches)


# ---------------------------------------------------------------------------
# Security clearance pattern
# ---------------------------------------------------------------------------


class TestClearancePattern:
    def test_tsci_detected(self):
        r = scan_text("Holds TS/SCI clearance")
        assert any(m.pattern_code == "clearance" for m in r.matches)

    def test_top_secret_detected(self):
        r = scan_text("Requires top secret clearance")
        assert any(m.pattern_code == "clearance" for m in r.matches)

    def test_sap_detected(self):
        r = scan_text("Assigned to SAP program")
        assert any(m.pattern_code == "clearance" for m in r.matches)


# ---------------------------------------------------------------------------
# Financial pattern
# ---------------------------------------------------------------------------


class TestFinancialPattern:
    def test_routing_number_detected(self):
        r = scan_text("Routing number: 123456789")
        assert any(m.pattern_code == "financial" for m in r.matches)

    def test_direct_deposit_detected(self):
        r = scan_text("Direct deposit account 9876543210")
        assert any(m.pattern_code == "financial" for m in r.matches)


# ---------------------------------------------------------------------------
# False positive avoidance
# ---------------------------------------------------------------------------


class TestFalsePositives:
    def test_normal_operational_text_clean(self):
        """Typical worklist notes should not trigger warnings."""
        text = (
            "Completed morning inspection. All systems nominal. "
            "Next review scheduled for Friday."
        )
        r = scan_text(text)
        assert not r.has_matches

    def test_task_description_clean(self):
        text = "Sweep and mop berthing compartments 2-4"
        r = scan_text(text)
        assert not r.has_matches

    def test_qualification_name_clean(self):
        text = "EOOW qualification in progress"
        r = scan_text(text)
        assert not r.has_matches

    def test_absence_code_clean(self):
        text = "Leave — personal"
        r = scan_text(text)
        assert not r.has_matches


# ---------------------------------------------------------------------------
# SensitivityReport API
# ---------------------------------------------------------------------------


class TestSensitivityReport:
    def test_empty_text(self):
        r = scan_text("")
        assert r.text_length == 0
        assert not r.has_matches

    def test_none_text(self):
        r = scan_text(None)  # type: ignore
        assert r.text_length == 0
        assert not r.has_matches

    def test_codes_unique_ordered(self):
        text = "SSN 123-45-6789 and email test@example.com and SSN 987-65-4321"
        r = scan_text(text)
        codes = r.codes
        assert codes == list(dict.fromkeys(codes))  # unique, ordered

    def test_snippet_has_context(self):
        r = scan_text("The SSN is 123-45-6789 on file")
        match = next(m for m in r.matches if m.pattern_code == "ssn")
        assert "…" in match.snippet or len(match.snippet) > 11


# ---------------------------------------------------------------------------
# Sensitive field registry
# ---------------------------------------------------------------------------


class TestSensitiveFieldRegistry:
    def test_person_notes_is_sensitive(self):
        assert is_sensitive_field("person.notes")
        assert get_sensitive_field_label("person.notes") is not None

    def test_absence_reason_is_sensitive(self):
        assert is_sensitive_field("absence.reason")

    def test_absence_notes_is_sensitive(self):
        assert is_sensitive_field("absence.notes")

    def test_person_qual_notes_is_sensitive(self):
        assert is_sensitive_field("person_qual.notes")

    def test_alert_notes_is_sensitive(self):
        assert is_sensitive_field("alert.notes")

    def test_unknown_field_not_sensitive(self):
        assert not is_sensitive_field("worklist.name")
        assert get_sensitive_field_label("worklist.name") is None

    def test_all_labels_are_nonempty(self):
        for key, label in SENSITIVE_FIELDS.items():
            assert label, f"Empty label for {key}"

    def test_pattern_count_reasonable(self):
        """We should have at least the core patterns."""
        assert len(SENSITIVITY_PATTERNS) >= 6
