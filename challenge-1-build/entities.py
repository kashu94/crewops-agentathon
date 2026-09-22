"""Deterministic entity extraction.

Identifiers come out by pattern, never by embedding or by asking a model.
`C-1042` and `C-1024` are near-identical in vector space and resolving the
wrong captain at 05:00 is the worst failure this system can have, so nothing
here is statistical.

Split of labour:

    regex  ->  WHICH entities        (exact, this module)
    model  ->  WHAT KIND of ask      (fuzzy, router.py's Triage Agent fallback)
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field, fields
from datetime import date
from typing import Any

import config

# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------

CREW_RE = re.compile(r"\bC-\d{4}\b")
PAIRING_RE = re.compile(r"\bP-\d{4}\b")
FLIGHT_ID_RE = re.compile(r"\bDX\d{3}-\d{4}-\d{2}-\d{2}\b")
FLIGHT_NO_RE = re.compile(r"\bDX\d{3}\b")
RULE_RE = re.compile(r"\bRULE-[A-Z]{3,4}-\d{2}\b")
AIRCRAFT_RE = re.compile(r"\bVT-DX[A-F]\b")
AC_TYPE_RE = re.compile(r"\b(A320|ATR-?72)\b", re.I)
STATION_RE = re.compile(r"\b[A-Z]{3}\b")
ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*Z?\b", re.I)

# Boarding-gate labels: "<station>-G<n>".
GATE_RE = re.compile(r"\b([A-Z]{3}-G\d+)\b", re.I)

# "delayed by 90 minutes", "delayed 2 hours", "a 45 min delay" -> minutes.
DELAY_RE = re.compile(
    r"\bdelay(?:ed)?\b[^.?!\n]{0,20}?(\d+(?:\.\d+)?)\s*(hours?|hrs?|h\b|minutes?|mins?|m\b)"
    r"|(\d+(?:\.\d+)?)\s*(hours?|hrs?|h\b|minutes?|mins?|m\b)[^.?!\n]{0,20}?\bdelay(?:ed)?\b",
    re.I,
)

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTHS |= {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))

# "15 Sep", "15 September 2026", "Sep 15"
DAY_MONTH_RE = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_ALT})\b(?:\s+(\d{{4}}))?", re.I
)
MONTH_DAY_RE = re.compile(
    rf"\b({_MONTH_ALT})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", re.I
)
# "the 15th" — only meaningful because the dataset is one fixed week
BARE_DAY_RE = re.compile(r"\bthe\s+(\d{1,2})(?:st|nd|rd|th)\b", re.I)

ROLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Senior Cabin Crew", re.compile(r"\b(senior cabin crew|sccs?|pursers?)\b", re.I)),
    ("First Officer", re.compile(r"\b(first officers?|f/?os?|co-?pilots?)\b", re.I)),
    ("Captain", re.compile(r"\b(captains?|cpts?|capts?|commanders?|skippers?)\b", re.I)),
    ("Cabin Crew", re.compile(r"\b(cabin crew|flight attendants?|cc)\b", re.I)),
)

CERT_RE = re.compile(
    r"\b(dangerous[_ ]goods|licence|license|medical[_ ]class ?1|recurrent[_ ]training)\b",
    re.I,
)

# Words that would otherwise be swallowed by the 3-letter station pattern.
_STATION_FALSE_FRIENDS = frozenset(
    {"FDP", "UTC", "AND", "THE", "FOR", "WHO", "NOT", "ALL", "ANY", "CAN", "HOW"}
)


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Entities:
    """Everything the router pulled out of a query, deduplicated, order-stable."""

    crew_ids: list[str] = field(default_factory=list)
    pairing_ids: list[str] = field(default_factory=list)
    flight_ids: list[str] = field(default_factory=list)
    flight_nos: list[str] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)
    aircraft: list[str] = field(default_factory=list)
    aircraft_types: list[str] = field(default_factory=list)
    stations: list[str] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)
    times: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    cert_types: list[str] = field(default_factory=list)
    gate_numbers: list[str] = field(default_factory=list)
    delay_minutes: float | None = None
    horizon_days: int | None = None
    names: list[str] = field(default_factory=list)
    """Words that look like a crew member's name. Candidates only — nothing
    here is a person until the roster says so."""

    def to_dict(self) -> dict[str, Any]:
        return {f.name: v for f in fields(self) if (v := getattr(self, f.name))}

    def is_empty(self) -> bool:
        return not self.to_dict()

    @property
    def primary_crew(self) -> str | None:
        return self.crew_ids[0] if self.crew_ids else None

    @property
    def primary_pairing(self) -> str | None:
        return self.pairing_ids[0] if self.pairing_ids else None

    @property
    def primary_date(self) -> str | None:
        return self.dates[0] if self.dates else None

    @property
    def primary_gate(self) -> str | None:
        return self.gate_numbers[0] if self.gate_numbers else None


def _dedupe(items: list[str]) -> list[str]:
    """Order-preserving dedupe — first mention wins, which is what a reader means."""
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------


def _mk_date(day: int, month: int, year: int | None) -> str | None:
    try:
        return date(year or config.DEFAULT_YEAR, month, day).isoformat()
    except ValueError:
        return None


def extract_dates(text: str) -> list[str]:
    """Return ISO dates. Bare days resolve into the dataset's fixed week."""
    found: list[str] = []

    for y, m, d in ISO_DATE_RE.findall(text):
        if iso := _mk_date(int(d), int(m), int(y)):
            found.append(iso)

    for day, mon, year in DAY_MONTH_RE.findall(text):
        if iso := _mk_date(int(day), _MONTHS[mon.lower()], int(year) if year else None):
            found.append(iso)

    for mon, day, year in MONTH_DAY_RE.findall(text):
        if iso := _mk_date(int(day), _MONTHS[mon.lower()], int(year) if year else None):
            found.append(iso)

    if not found:
        # "the 15th" is unambiguous only because the dataset is one week long.
        week_start = date.fromisoformat(config.WEEK_START)
        week_end = date.fromisoformat(config.WEEK_END)
        for day in BARE_DAY_RE.findall(text):
            candidate = _mk_date(int(day), week_start.month, week_start.year)
            if candidate and week_start <= date.fromisoformat(candidate) <= week_end:
                found.append(candidate)

    return _dedupe(found)


_HORIZON_RE = re.compile(
    r"\b(?:with)?in\s+(?:the\s+)?(?:next\s+)?(\d{1,3})\s*(day|week|month)s?\b"
    r"|\bnext\s+(\d{1,3})\s*(day|week|month)s?\b",
    re.I,
)

_HORIZON_UNIT_DAYS = {"day": 1, "week": 7, "month": 30}


# A person written the way the system writes them back — "A. Nair" — or a
# bare capitalised surname. These are *candidates*: the roster decides which
# are people, because the alternative is a hardcoded name list that goes stale
# the moment the dataset changes.
PERSON_RE = re.compile(r"\b([A-Z]\.\s*[A-Z][a-z]{2,})\b|\b([A-Z][a-z]{2,})\b")

# Capitalised words that open a sentence or name a concept, not a person.
_NOT_A_NAME = frozenset({
    "The", "This", "That", "Who", "Which", "What", "When", "Where", "Why",
    "How", "Can", "Does", "Draft", "List", "Show", "Give", "Find", "Move",
    "Captain", "First", "Officer", "Senior", "Cabin", "Crew", "Reserve",
    "Pairing", "Flight", "Rule", "Duty", "Sick", "Both", "Please", "Would",
    "Should", "Their", "There", "They", "Available", "Legal", "Any", "All",
    # Words that open a follow-up about the options already on the table.
    "Next", "Cheapest", "Other", "Another", "Option", "Options", "Same",
    "Take", "Assign", "Call", "Book", "Choose", "Pick", "Instead", "Cost",
    "Alternative", "Alternatives", "Second", "Third", "Last", "Best",
})


def extract_names(text: str) -> list[str]:
    """Names a controller might have used instead of an id.

    Only candidates. Several surnames repeat across this dataset's 150 crew,
    so nothing here resolves to a person without the roster — and where it is
    ambiguous the controller is asked, never guessed at.
    """
    found: list[str] = []
    for full, bare in PERSON_RE.findall(text):
        value = (full or bare).strip()
        if not value or value in _NOT_A_NAME:
            continue
        found.append(re.sub(r"\.\s+", ". ", value))
    return _dedupe(found)


def extract_horizon(text: str) -> int | None:
    """A forward-looking window in days — "within 30 days", "next 2 weeks".

    A question about expiry is an interval, not a point. A month is taken as
    30 days — what "within a month" means to a controller reading a roster,
    and it keeps the arithmetic checkable.
    """
    match = _HORIZON_RE.search(text)
    if not match:
        return None
    count, unit = (match.group(1), match.group(2)) if match.group(1) else (
        match.group(3), match.group(4))
    return int(count) * _HORIZON_UNIT_DAYS[unit.lower()]


def extract_times(text: str) -> list[str]:
    """Return HH:MM strings. The dataset is entirely UTC, so no zone handling."""
    out = []
    for hh, mm in TIME_RE.findall(text):
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            out.append(f"{h:02d}:{m:02d}")
    return _dedupe(out)


def extract_delay_minutes(text: str) -> float | None:
    """A hypothetical delay duration, in minutes, when one is stated.

    >>> extract_delay_minutes("if DX401 is delayed by 90 minutes")
    90.0
    >>> extract_delay_minutes("a 2 hour delay on DX401")
    120.0
    """
    m = DELAY_RE.search(text)
    if not m:
        return None
    value_s, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    value, unit = float(value_s), unit.lower()
    return value * 60 if unit.startswith("h") else value


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------


def extract(text: str) -> Entities:
    """Pull every recognisable entity out of a controller's question.

    >>> e = extract("Can C-1042 cover P-2291 on 15 Sep out of BLR?")
    >>> e.crew_ids, e.pairing_ids, e.dates, e.stations
    (['C-1042'], ['P-2291'], ['2026-09-15'], ['BLR'])
    """
    flight_ids = FLIGHT_ID_RE.findall(text)

    # A flight id contains a flight no; don't report the same mention twice.
    consumed = " ".join(flight_ids)
    flight_nos = [n for n in FLIGHT_NO_RE.findall(text) if n not in consumed]

    stations = [
        s
        for s in STATION_RE.findall(text)
        if s in config.STATIONS and s not in _STATION_FALSE_FRIENDS
    ]

    roles = [name for name, pat in ROLE_PATTERNS if pat.search(text)]

    ac_types = [t.upper().replace("-", "") for t in AC_TYPE_RE.findall(text)]

    certs = [c.lower().replace(" ", "_").replace("license", "licence")
             for c in CERT_RE.findall(text)]

    gates = [g.upper() for g in GATE_RE.findall(text)]

    return Entities(
        crew_ids=_dedupe(CREW_RE.findall(text)),
        pairing_ids=_dedupe(PAIRING_RE.findall(text)),
        flight_ids=_dedupe(flight_ids),
        flight_nos=_dedupe(flight_nos),
        rule_ids=_dedupe(RULE_RE.findall(text)),
        aircraft=_dedupe(AIRCRAFT_RE.findall(text)),
        aircraft_types=_dedupe(ac_types),
        stations=_dedupe(stations),
        dates=extract_dates(text),
        times=extract_times(text),
        roles=roles,
        cert_types=_dedupe(certs),
        gate_numbers=_dedupe(gates),
        delay_minutes=extract_delay_minutes(text),
        horizon_days=extract_horizon(text),
        names=extract_names(text),
    )


# --------------------------------------------------------------------------
# A rank used to *describe* a named crew member — "FO C-2087", "Captain
# C-1042" — as opposed to specifying a seat to be filled ("cover as Captain").
# Only the descriptive form asserts something about that person that the
# roster can contradict.
STATED_RANK_RE = re.compile(
    r"\b(captain|cpt|capt|commander|skipper|first officer|f/?o|co-?pilot|"
    r"senior cabin crew|scc|purser|cabin crew|flight attendant)\s+(C-\d{4})\b",
    re.I,
)

RANK_WORD_TO_RANK = {
    "captain": "Captain", "cpt": "Captain", "capt": "Captain",
    "commander": "Captain", "skipper": "Captain",
    "first officer": "First Officer", "fo": "First Officer",
    "f/o": "First Officer", "co-pilot": "First Officer",
    "copilot": "First Officer",
    "senior cabin crew": "Senior Cabin Crew", "scc": "Senior Cabin Crew",
    "purser": "Senior Cabin Crew",
    "cabin crew": "Cabin Crew", "flight attendant": "Cabin Crew",
}


def stated_ranks(text: str) -> list[tuple[str, str]]:
    """(crew_id, rank the query claims they hold), for descriptive uses only.

    >>> stated_ranks("If I move FO C-2087 onto DX412")
    [('C-2087', 'First Officer')]
    >>> stated_ranks("who can cover P-2291 as Captain")
    []
    """
    out = []
    for word, crew_id in STATED_RANK_RE.findall(text):
        key = word.lower().replace("/", "").replace(" ", " ").strip()
        if rank := RANK_WORD_TO_RANK.get(key) or RANK_WORD_TO_RANK.get(key.replace("-", "")):
            out.append((crew_id, rank))
    return out
