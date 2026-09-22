"""Tests for the PII redaction guardrail (`pii.py`).

Ported behavior from dCortex Crew Ops Advisor's `api/pii.py` tests, adapted
to the plain-dict `redact_response()`/`redact_value()` API this repo uses in
place of the original's Pydantic serializer.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pii


def test_operational_ids_survive_untouched():
    text = "Assign A. Nair (C-1042) to P-2291, deadhead via DX402 on 2026-09-14."
    assert pii.redact_pii_text(text) == text


def test_email_is_redacted():
    assert pii.redact_pii_text("contact a.nair@dcortexair.example") == \
        "contact [REDACTED_EMAIL]"


def test_phone_is_redacted_but_flight_and_pairing_ids_are_not():
    text = "Call +91 98765 43210 about P-2291 and DX402."
    out = pii.redact_pii_text(text)
    assert "[REDACTED_PHONE]" in out
    assert "P-2291" in out and "DX402" in out


def test_pan_is_redacted():
    assert pii.redact_pii_text("PAN ABCDE1234F on file") == "PAN [REDACTED_PAN] on file"


def test_valid_card_number_is_redacted_by_luhn():
    # 4111 1111 1111 1111 is a well-known Luhn-valid test Visa number.
    assert pii.redact_pii_text("card 4111 1111 1111 1111") == "card [REDACTED_CARD]"


def test_luhn_invalid_long_digit_run_is_left_alone():
    # Same shape, fails Luhn -- must not be flagged as a card.
    text = "reference 1234 5678 9012 3456"
    assert pii.redact_pii_text(text) == text


def test_sensitive_field_name_is_redacted_regardless_of_content():
    value = {"crew_id": "C-1042", "home_address": "12 MG Road, Bengaluru"}
    assert pii.redact_value(value) == {
        "crew_id": "C-1042",
        "home_address": "[REDACTED_ADDRESS]",
    }


def test_sensitive_field_redacted_inside_nested_structures():
    value = {"crew": [{"crew_id": "C-1042", "date_of_birth": "1990-01-01"}]}
    out = pii.redact_value(value)
    assert out["crew"][0]["date_of_birth"] == "[REDACTED_ADDRESS]"
    assert out["crew"][0]["crew_id"] == "C-1042"


def test_redact_response_preserves_structure():
    from schemas import AdvisorResponse, Intent, Tier

    response = AdvisorResponse(
        tier=Tier.LOOKUP, intent=Intent.LOOKUP_CREW, query="who is C-1042?",
        narrative="A. Nair (C-1042) is a Captain based at BLR.",
    )
    out = pii.redact_response(response)
    assert out["narrative"] == response.narrative  # nothing sensitive here
    assert out["intent"] == "LOOKUP_CREW"
