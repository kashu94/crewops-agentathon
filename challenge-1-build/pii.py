"""PII/sensitive-data redaction — applied at the output boundary, not deep in
business logic, so it can never be forgotten on a newly added field.

Ported from dCortex Crew Ops Advisor's `api/pii.py`. That project redacts at
Pydantic serialization time; this one has no Pydantic layer (`schemas.py` is
plain dataclasses), so the same two-layer design is reapplied at the point an
`AdvisorResponse` is about to leave the process — printed to a controller,
returned from an API handler, or written to a log — via `redact_response()`.

The current vendored dataset (`data/crew.json` etc.) has NONE of these fields
today. This module is defense-in-depth for when such fields get added, not a
fix for an existing leak.

Categories covered, modeled on India's DPDP Act 2023 / the earlier SPDI Rules
2011 definition of sensitive personal data (financial info, health records,
government IDs, biometric info):
  - email, phone                    -- regex, reliable
  - Aadhaar, PAN, passport number   -- regex, format-specific (India context)
  - payment card numbers            -- regex + Luhn check, to avoid
                                        false-positiving on arbitrary long
                                        digit sequences
  - physical/home address, DOB,
    health/medical notes, bank
    details, other government IDs,
    emergency contact info          -- field-name only, NEVER generic
                                        free-text pattern matching: address
                                        text looks like ordinary prose and a
                                        DOB is indistinguishable from any
                                        other date, so no content pattern
                                        reliably catches these.

Deliberately NOT redacted: crew name, rank, base, crew_id. These are
operationally necessary — a controller must know who to call. PII handling
here means stripping data that has no operational purpose in an answer, not
hiding identity itself.
"""

from __future__ import annotations

import re
from typing import Any

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# +91 98765 43210 / (080) 1234-5678 style (leading + or parens), or a bare
# 7-15 digit run. Deliberately does NOT match hyphenated digit groups without
# a leading +/parens -- indistinguishable from an ISO date (2026-09-15) or a
# flight/pairing ID suffix in this domain. Order matters: this must run AFTER
# the more specific patterns below, or it would swallow them under the
# generic PHONE label instead of their correct one.
_PHONE_RE = re.compile(r"(?<!\d)(?:\+\d[\d\s\-().]{6,14}\d|\d{7,15})(?!\d)")

# Aadhaar: 12 digits, conventionally displayed in 3 groups of 4 (space or
# hyphen). An ungrouped bare 12-digit Aadhaar still gets caught by the
# generic phone pattern above (labeled PHONE instead of AADHAAR) -- accepted
# tradeoff, since requiring the grouped display format keeps this pattern
# from misfiring on other 12-digit sequences.
_AADHAAR_RE = re.compile(r"(?<!\d{4}[\s-])(?<!\d)\d{4}[\s-]\d{4}[\s-]\d{4}(?!\d)(?![\s-]\d{4})")

# PAN: 5 letters, 4 digits, 1 letter -- e.g. ABCDE1234F.
_PAN_RE = re.compile(r"(?<![A-Z0-9])[A-Z]{5}[0-9]{4}[A-Z](?![A-Z0-9])")

# Indian passport: 1 letter + 7 digits, no separator -- e.g. A1234567.
# Doesn't collide with crew_id (C-1042, hyphenated), aircraft tails (VT-DXA,
# no digits), or flight numbers (DX401, 3 digits not 7).
_PASSPORT_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]\d{7}(?![A-Za-z0-9])")

_IPV4_RE = re.compile(r"(?<!\d)(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?!\d)")

# Candidate payment-card sequences (13-19 digits, optionally grouped by
# spaces/hyphens) -- verified with a Luhn checksum before redacting, so an
# arbitrary long numeric ID doesn't get mislabeled as a card number.
_CARD_CANDIDATE_RE = re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)")

# Field names that are unconditionally redacted regardless of content --
# these categories (address, DOB, health, financial, other government IDs)
# have no generic content pattern that reliably distinguishes them from
# ordinary text or from other operational dates/numbers in this domain.
_SENSITIVE_FIELD_NAMES = {
    "address", "physical_address", "home_address", "residential_address",
    "date_of_birth", "dob", "birth_date",
    "medical_notes", "medical_condition", "health_notes", "diagnosis",
    "bank_account", "bank_account_number", "ifsc_code", "iban",
    "national_id", "ssn", "tax_id", "government_id",
    "emergency_contact", "next_of_kin", "emergency_contact_number",
    "photo", "biometric_id", "signature",
}


def _luhn_valid(digits: str) -> bool:
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _redact_cards(text: str) -> str:
    def repl(m: re.Match) -> str:
        digits = re.sub(r"[ \-]", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            return "[REDACTED_CARD]"
        return m.group(0)

    return _CARD_CANDIDATE_RE.sub(repl, text)


def redact_pii_text(value: str) -> str:
    """Applies every content-pattern redaction, most specific first (so a PAN
    or passport number gets its correct label instead of being swallowed by
    the generic phone pattern). Never call this on structured IDs (crew_id,
    flight_id, etc.) -- it's for free-text/narrative fields only, and it must
    run AFTER the verifier, never before: the verifier checks the narrative
    against the tool trace, and a redacted narrative would make every real
    number look unsourced."""
    value = _EMAIL_RE.sub("[REDACTED_EMAIL]", value)
    value = _redact_cards(value)
    value = _PAN_RE.sub("[REDACTED_PAN]", value)
    value = _PASSPORT_RE.sub("[REDACTED_PASSPORT]", value)
    value = _AADHAAR_RE.sub("[REDACTED_AADHAAR]", value)
    value = _IPV4_RE.sub("[REDACTED_IP]", value)
    value = _PHONE_RE.sub("[REDACTED_PHONE]", value)
    return value


def _is_sensitive_field(field_name: str) -> bool:
    return field_name.lower() in _SENSITIVE_FIELD_NAMES


def redact_value(value: Any, _key: str | None = None) -> Any:
    """Recursively redact a plain dict/list/str structure (the output of
    `AdvisorResponse.to_dict()` or any tool result). Field-name-based
    detection covers the address/DOB/health/financial categories at any
    nesting depth; content-pattern detection covers email/phone/PAN/
    passport/Aadhaar/cards wherever free text appears."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if _is_sensitive_field(k):
                out[k] = _redact_sensitive_leaf(v)
            else:
                out[k] = redact_value(v, k)
        return out
    if isinstance(value, list):
        return [redact_value(v, _key) for v in value]
    if isinstance(value, str):
        return redact_pii_text(value)
    return value


def _redact_sensitive_leaf(value: Any) -> Any:
    """Unconditional full redaction for a value already identified as a
    sensitive field -- a bare string, or every string inside a list."""
    if isinstance(value, str):
        return "[REDACTED_ADDRESS]"
    if isinstance(value, list):
        return [_redact_sensitive_leaf(v) for v in value]
    return value


def redact_response(response: Any) -> dict[str, Any]:
    """`AdvisorResponse.to_dict()`, redacted. Call this at the boundary where
    an answer leaves the process -- an API handler's return, a print to a
    controller's screen, a log line -- never on the object used internally
    (the verifier must check the unredacted narrative against the unredacted
    trace, or every real number looks unsourced)."""
    return redact_value(response.to_dict())
