"""The trust gate.

Every identifier and number in an answer must trace back to a real tool
result before it reaches a controller — if it doesn't, it's a hallucination,
since the model has no other source of facts.

This check is deterministic (plain set membership over the trace), not
another model call, so it can't hallucinate itself. It runs after every
Explainer Agent call; a rejection triggers the fallback to
`explainer.render()`'s plain template.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

import config
from entities import (
    AIRCRAFT_RE,
    CREW_RE,
    FLIGHT_ID_RE,
    FLIGHT_NO_RE,
    PAIRING_RE,
    RULE_RE,
)
from schemas import TraceEntry

# Numbers as a controller would write them: 18,500 / ₹18500 / 61.33 / 1h20m
NUMBER_RE = re.compile(r"(?<![\w.])(?:₹\s*)?(\d[\d,]*(?:\.\d+)?)(?![\w])")

# Matches a whole date as one claim, not three separate numbers — splitting
# 2026-09-15 into 2026/09/15 would let a wrong date pass just because the
# year matches.
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

# A whole ISO timestamp, matched before anything looks for a time inside it.
ISO_DT_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)

# A time of day, checked after full timestamps are removed. Can't start with
# `\b` since a time may follow a `-` in a range like `06:00-18:00`.
CLOCK_RE = re.compile(r"(?<![\d:])\d{1,2}:\d{2}(?::\d{2})?Z?(?![\d:])")


def times_and_rest(text: str) -> tuple[list[str], str]:
    """Pulls the time-of-day out of every timestamp, and returns the text
    with those timestamps removed.

    The dataset stores full timestamps (`2026-09-15T06:00:00+00:00`) but a
    controller writes `06:00Z` — converting between the two is the model
    doing its job, not inventing a number.
    """
    found: list[str] = []
    for match in ISO_DT_RE.findall(text):
        stamp = match.split("T")[-1].split(" ")[-1]
        if norm := normalise_clock(stamp):
            found.append(norm)
    return found, ISO_DT_RE.sub(" ", text)


def normalise_clock(value: str) -> str | None:
    """`6:00`, `06:00Z`, `06:00:00+00:00` -> `06:00`. Anything else -> None.

    Deliberately narrow: this is the one place two different strings count
    as the same fact.

    **Seconds are only dropped when they are zero.** `06:00:30` is not
    `06:00`. Every timestamp in this dataset ends `:00`, but the check does
    not depend on that staying true.

    **Only UTC normalises.** `06:00+05:30` is a different instant from
    `06:00Z`, so anything but Z or a zero offset returns None and has to
    match literally. The dataset is entirely UTC.
    """
    match = re.fullmatch(
        r"(\d{1,2}):(\d{2})(?::(\d{2}))?(Z|[+-]\d{2}:?\d{2})?", value.strip())
    if not match:
        return None
    hour, minute, second, zone = match.groups()

    if second not in (None, "00"):
        return None
    if zone not in (None, "Z", "+00:00", "-00:00", "+0000", "-0000"):
        return None
    return f"{int(hour):02d}:{minute}" if int(hour) < 24 else None


ID_PATTERNS = (
    CREW_RE, PAIRING_RE, FLIGHT_ID_RE, FLIGHT_NO_RE, RULE_RE, AIRCRAFT_RE,
    DATE_RE, CLOCK_RE,
)

# A name right before its id, like "A. Nair (C-1042)" — every one of the
# 150 real crew names is "Initial. Surname" (checked, no exceptions). This
# turns a name into a checkable claim, keyed by the id next to it.
NAME_ID_RE = re.compile(r"([A-Z]\.\s?[A-Z][a-zA-Z'-]+)\s*\((C-\d{4})\)")

# A flight number named after the word "flight" (e.g. "Flight X134"). Any
# real flight number is shaped DX### and already caught by the identifier
# check via FLIGHT_NO_RE; this catches the ones that aren't that shape,
# which the identifier check never looks at, so a fake "Flight X134" was
# otherwise invisible.
FLIGHT_MENTION_RE = re.compile(r"\bflight\s+([A-Za-z]{1,4}\d{1,4})\b", re.I)

# Every real name is "Initial. Surname" (checked, no exceptions), so a full
# first name next to a real id -- "Ananya Gupta (C-2111)" instead of
# "A. Gupta (C-2111)" -- has to be made up. NAME_ID_RE doesn't match that
# shape at all. Rank words are excluded from the first position so an
# informal "Captain Nair (C-1042)" isn't flagged as an invented name.
_RANK_WORDS = {"captain", "first", "officer", "senior", "cabin", "crew", "fo"}
GENERAL_NAME_ID_RE = re.compile(
    r"\b([A-Z][a-zA-Z'-]{2,}\s+[A-Z][a-zA-Z'-]+)\s*\((C-\d{4})\)")

# A date written the way a controller writes it, like "15 Sep" -- DATE_RE
# above only catches the ISO form tools return. Without this, the bare "15"
# would look like an unsourced number, since no tool result contains a lone
# 15. A calendar reference isn't a claim about the world; the date is still
# checked wherever it appears in ISO form.
PROSE_DATE_RE = re.compile(
    r"\b\d{1,2}\s*(?:st|nd|rd|th)?\s+"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+"
    r"\d{1,2}\s*(?:st|nd|rd|th)?\b",
    re.I,
)

# "in the next 30 days", "over the last 14 days" -- this echoes back the
# window the question itself asked about. It isn't a measured value, so no
# tool result needs to contain it.
WINDOW_RE = re.compile(
    r"\b(?:next|last|past|coming|previous|preceding)\s+\d+\s*"
    r"(?:day|week|month|hour|hr)s?\b",
    re.I,
)

# Citing a rule id isn't proof the claim about it is true -- RULE-DUTY-02
# appearing in the trace could just mean it passed. So a rule id paired
# with a pass/fail word is checked against that rule's real status, to
# catch a wrong attribution like "breaches RULE-DUTY-02" when RULE-FDP-01
# was the one that actually failed.
_FAIL_VERBS_RE = re.compile(
    r"\b(?:breach\w*|violat\w*|exceed\w*|illegal|fails?|failed|non-?compliant)\b", re.I)
_PASS_VERBS_RE = re.compile(
    r"\b(?:clears?|cleared|passes|passed|legal|compliant|within\s+limits?|meets?)\b", re.I)
_RULE_CONTEXT_WINDOW = 40  # characters either side of a RULE-xxx-nn mention


@dataclass(slots=True)
class Claim:
    """One checkable assertion lifted out of the narrative."""

    kind: str          # "identifier" | "number" | "rule_context"
    value: str
    supported: bool = False
    source_tool: str | None = None
    derivation: str | None = None
    """Set when the value wasn't returned directly but follows from two that
    were, e.g. "24000 - 18500". Still auditable -- check the arithmetic
    against the tools' own numbers."""

    @property
    def status(self) -> str:
        if not self.supported:
            return "unsupported"
        return "derived" if self.derivation else "sourced"


@dataclass(slots=True)
class VerificationResult:
    ok: bool
    claims: list[Claim] = field(default_factory=list)
    unsupported: list[Claim] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    had_trace: bool = True

    @property
    def derived(self) -> list[Claim]:
        return [c for c in self.claims if c.derivation]

    def summary(self) -> str:
        if self.unsupported:
            bad = ", ".join(c.value for c in self.unsupported)
            return f"UNVERIFIED: {len(self.unsupported)} untraced claim(s): {bad}"
        if not self.had_trace:
            return "no tool ran — nothing in this answer has a source"
        if not self.claims:
            return "nothing asserted — no checkable claim was made"
        if derived := self.derived:
            return (f"verified: {len(self.claims)} claims — {len(derived)} derived "
                    f"by arithmetic from tool output, the rest returned directly")
        return f"verified: {len(self.claims)} claims all traced to tool output"


# --------------------------------------------------------------------------
# Building the evidence set
# --------------------------------------------------------------------------


def _walk(node: Any) -> Iterable[Any]:
    """Yields every scalar inside a nested tool result, including
    collection lengths -- "12 records" is a real count from the tool, not
    a number the model made up, so it counts as evidence too.
    """
    if isinstance(node, dict):
        yield len(node)
        for key, value in node.items():
            yield key
            yield from _walk(value)
    elif isinstance(node, (list, tuple, set)):
        yield len(node)
        for item in node:
            yield from _walk(item)
    elif isinstance(node, (dt.date, dt.time, dt.datetime)):
        yield str(node)
        yield node.isoformat()
    else:
        yield node


def _norm_number(raw: str) -> float | None:
    try:
        return float(raw.replace(",", "").replace("₹", "").strip())
    except ValueError:
        return None


def _strip_identifiers(text: str) -> str:
    """Removes ids before scanning for numbers.

    Without this, "C-1042" would contribute the number 1042 -- inventing a
    false claim, and worse, letting a fake "1042 hours" pass verification.
    """
    for pattern in ID_PATTERNS:
        text = pattern.sub(" ", text)
    text = PROSE_DATE_RE.sub(" ", text)
    text = WINDOW_RE.sub(" ", text)
    return text


def _numbers_in(text: str) -> list[float]:
    return [
        value
        for raw in NUMBER_RE.findall(_strip_identifiers(text))
        if (value := _norm_number(raw)) is not None
    ]


@dataclass(slots=True)
class Evidence:
    """Everything the tools actually said, indexed for membership tests."""

    identifiers: dict[str, str] = field(default_factory=dict)  # id -> tool
    numbers: list[tuple[float, str]] = field(default_factory=list)
    rule_statuses: dict[str, set[str]] = field(default_factory=dict)
    """rule_id -> every status ("PASS"/"FAIL") seen for it anywhere in the
    trace. Only built from dicts shaped like a `RuleVerdict` (has both
    rule_id and status) -- see `_rule_verdicts`."""
    names_by_id: dict[str, str] = field(default_factory=dict)
    """crew_id -> the name a tool actually gave for it. A real id with the
    wrong name is just as unsourced as a fake id -- the model can only know
    a name if a tool said it."""

    def has_identifier(self, value: str) -> str | None:
        if found := self.identifiers.get(value):
            return found
        if norm := normalise_clock(value):
            return self.identifiers.get(norm)
        return None

    def has_number(self, value: float) -> str | None:
        for known, tool in self.numbers:
            if abs(known - value) <= config.VERIFIER_FLOAT_TOLERANCE:
                return tool
        return None

    def derive(self, value: float) -> tuple[str, str] | None:
        """Whether `value` follows from two evidence numbers by simple math
        (a difference, a total, a multiple, a percentage) -- things a
        controller wants stated whether or not a tool computed them
        directly. Accepted and labelled with its derivation so it stays
        auditable against the numbers the tools did return.

        Deliberately narrow: only pairs, no chaining, and both numbers must
        already be evidence.
        """
        if abs(value) < config.VERIFIER_NUMERIC_FLOOR:
            return None

        tol = config.VERIFIER_FLOAT_TOLERANCE
        seen: list[tuple[float, str]] = []
        for number, tool in self.numbers:
            if not any(abs(number - n) <= tol for n, _ in seen):
                seen.append((number, tool))

        for a, tool_a in seen:
            for b, tool_b in seen:
                if a is b:
                    continue
                for expr, result in (
                    (f"{a:g} - {b:g}", a - b),
                    (f"{a:g} + {b:g}", a + b),
                    (f"{a:g} x {b:g}", a * b),
                    (f"{a:g} / {b:g}", a / b if b else None),
                    (f"100 x {a:g} / {b:g}", 100 * a / b if b else None),
                ):
                    if result is None:
                        continue
                    if abs(result - value) <= tol:
                        return expr, f"{tool_a}+{tool_b}"
        return None


def _crew_names(node: Any) -> Iterable[tuple[str, str]]:
    """Yields (crew_id, name) for every dict in a nested tool result that
    has both -- crew lookups, check_legality/duty_clock's own result,
    find_options' options/excluded entries, and more all already share this
    shape."""
    if isinstance(node, dict):
        if isinstance(node.get("crew_id"), str) and isinstance(node.get("name"), str):
            yield node["crew_id"], node["name"]
        for value in node.values():
            yield from _crew_names(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _crew_names(item)


def _rule_verdicts(node: Any) -> Iterable[tuple[str, str]]:
    """Yields (rule_id, status) for every dict shaped like a `RuleVerdict`
    anywhere in a nested tool result -- check_legality and find_options'
    entries both have these."""
    if isinstance(node, dict):
        if "rule_id" in node and "status" in node:
            yield str(node["rule_id"]), str(node["status"]).upper()
        for value in node.values():
            yield from _rule_verdicts(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _rule_verdicts(item)


def build_evidence(trace: Iterable[TraceEntry]) -> Evidence:
    """Indexes every scalar every tool returned.

    Arguments count too: an id the model passed in came from an earlier
    result or the controller's own question, so echoing it back is fine.
    """
    evidence = Evidence()

    for entry in trace:
        for rule_id, status in _rule_verdicts(entry.result):
            evidence.rule_statuses.setdefault(rule_id, set()).add(status)
        for crew_id, name in _crew_names(entry.result):
            evidence.names_by_id.setdefault(crew_id, name)

    for entry in trace:
        # An error message counts too -- it's text a tool produced, so an
        # id it names is just as sourced as one from a successful result.
        for payload in (entry.result, entry.args, entry.error):
            if payload is None:
                continue
            for scalar in _walk(payload):
                if isinstance(scalar, bool) or scalar is None:
                    continue
                if isinstance(scalar, (int, float, Decimal)):
                    evidence.numbers.append((float(scalar), entry.tool))
                    continue
                if isinstance(scalar, (dt.datetime, dt.date, dt.time)):
                    scalar = scalar.isoformat()
                if not isinstance(scalar, str):
                    continue

                stamped, rest = times_and_rest(scalar)
                for hhmm in stamped:
                    evidence.identifiers.setdefault(hhmm, entry.tool)

                for pattern in ID_PATTERNS:
                    for match in pattern.findall(scalar if pattern is not CLOCK_RE
                                                 else rest):
                        evidence.identifiers.setdefault(match, entry.tool)
                        if norm := normalise_clock(match):
                            evidence.identifiers.setdefault(norm, entry.tool)

                for num in _numbers_in(scalar):
                    evidence.numbers.append((num, entry.tool))

    return evidence


# --------------------------------------------------------------------------
# Extracting claims
# --------------------------------------------------------------------------


def extract_claims(narrative: str) -> list[Claim]:
    """Everything in the prose that has to be backed by evidence."""
    claims: list[Claim] = []
    seen: set[tuple[str, str]] = set()

    stamped, rest = times_and_rest(narrative)
    for pattern in ID_PATTERNS:
        for value in pattern.findall(narrative if pattern is not CLOCK_RE else rest):
            key = ("identifier", value)
            if key not in seen:
                seen.add(key)
                claims.append(Claim("identifier", value))

    for raw in NUMBER_RE.findall(_strip_identifiers(narrative)):
        value = _norm_number(raw)
        if value is None:
            continue
        # Small whole numbers are usually just prose ("all 7 rules", "the 2
        # options"), not real claims. Costs, hours, and counts big enough
        # to matter still get checked.
        if abs(value) < config.VERIFIER_NUMERIC_FLOOR and value.is_integer():
            continue
        key = ("number", raw)
        if key not in seen:
            seen.add(key)
            claims.append(Claim("number", raw))

    return claims


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def _rule_context_violations(narrative: str, evidence: Evidence) -> list[Claim]:
    """A rule id cited with a fail word (or pass word) whose evidence never
    actually showed that status is unsupported -- the rule id itself might
    be real, just not for the reason claimed. The only context-sensitive
    check here, because misattributing which rule failed is exactly the
    kind of plausible-but-wrong claim this can't allow.
    """
    violations: list[Claim] = []
    seen: set[str] = set()
    for match in RULE_RE.finditer(narrative):
        rule_id = match.group(0)
        start, end = match.span()
        window = narrative[max(0, start - _RULE_CONTEXT_WINDOW):end + _RULE_CONTEXT_WINDOW]
        claims_fail = bool(_FAIL_VERBS_RE.search(window))
        claims_pass = bool(_PASS_VERBS_RE.search(window))
        if not (claims_fail or claims_pass):
            continue

        wanted = "FAIL" if claims_fail else "PASS"
        if wanted in evidence.rule_statuses.get(rule_id, set()):
            continue

        key = f"{rule_id}:{wanted}"
        if key in seen:
            continue
        seen.add(key)
        # Not kind="identifier": the main loop below would look up the
        # rule id itself (which IS real) and undo this check. Keeping it
        # as its own kind means the main loop leaves `supported` alone.
        claim = Claim("rule_context", f"{rule_id} ({wanted.lower()})")
        claim.supported = False
        violations.append(claim)
    return violations


def _crew_name_violations(narrative: str, evidence: Evidence) -> list[Claim]:
    """A name next to a real crew id that no tool ever paired with that id
    -- either it's someone else's id, or the id only ever appeared bare
    (a `pairing_crew` row has a crew_id and role, never a name). A real id
    isn't enough -- a name only counts if a tool actually gave it.

    Skips any crew_id that isn't sourced at all -- a fully invented id is
    already caught by the ordinary "identifier" claim check, so this stays
    scoped to its own job: a real id under the wrong (or unverified) name.
    """
    violations: list[Claim] = []
    seen: set[str] = set()
    matches = list(NAME_ID_RE.findall(narrative))
    for name, crew_id in GENERAL_NAME_ID_RE.findall(narrative):
        if name.split()[0].lower() not in _RANK_WORDS:
            matches.append((name, crew_id))
    for name, crew_id in matches:
        if crew_id not in evidence.identifiers:
            continue
        if evidence.names_by_id.get(crew_id) == name:
            continue
        key = f"{crew_id}:{name}"
        if key in seen:
            continue
        seen.add(key)
        # Not kind="identifier", same reason as above: the main loop would
        # re-derive `supported` from the crew_id alone (real) and undo this.
        claim = Claim("crew_name", f"{name} ({crew_id})")
        claim.supported = False
        violations.append(claim)
    return violations


def _malformed_flight_violations(narrative: str) -> list[Claim]:
    """A flight number named after the word "flight" that isn't shaped like
    a real one (DX + 3 digits) at all -- see `FLIGHT_MENTION_RE`."""
    violations: list[Claim] = []
    seen: set[str] = set()
    for token in FLIGHT_MENTION_RE.findall(narrative):
        upper = token.upper()
        if FLIGHT_NO_RE.fullmatch(upper) or upper in seen:
            continue
        seen.add(upper)
        claim = Claim("flight_no", upper)
        claim.supported = False
        violations.append(claim)
    return violations


_UNCREWED_RE = re.compile(r"\buncrewed\b", re.I)


def _uncrewed_claim_violations(narrative: str, trace: list[TraceEntry]) -> list[Claim]:
    """"Uncrewed" is `ripple`'s own word. Using it without a `ripple` call
    means the draft invented an impact story -- e.g. turning a single
    person's rule failure from `check_legality` into "Flight X is
    uncrewed... cascading risk." Since none of that is an id or a number,
    nothing else here would catch it.
    """
    if not _UNCREWED_RE.search(narrative):
        return []
    if any(e.tool == "ripple" and not e.error for e in trace):
        return []
    claim = Claim("uncrewed_claim", "uncrewed")
    claim.supported = False
    return [claim]


_COUNT_ASSERTION_RE = re.compile(
    r"\b(\d[\d,]*)\s+(?:captains?|first officers?|pilots?|crew(?: members?)?|"
    r"cabin crew|senior cabin crew|pairings?|flights?|records?|options?|"
    r"gates?|rules?)\b", re.I)


def _count_violations(narrative: str, trace: list[TraceEntry]) -> list[Claim]:
    """The first stated headcount ("There are 26 captains...") must match
    how many rows a `lookup` actually returned.

    Without this, a wrong count can pass just because it coincidentally
    matches some unrelated field among dozens of rows -- a real tool once
    returned 27 captains while a draft said "26," and passed anyway,
    because 26 happened to be someone's seniority elsewhere in that result.

    Only checks the first such number -- a later subset count ("of these,
    N are ATR-rated") needs its own filtered lookup, which this doesn't
    assume exists.
    """
    row_counts = {len(e.result) for e in trace
                  if e.tool == "lookup" and isinstance(e.result, list) and e.result}
    if not row_counts:
        return []
    match = _COUNT_ASSERTION_RE.search(narrative)
    if not match:
        return []
    n = _norm_number(match.group(1))
    if n is None or n in row_counts:
        return []
    claim = Claim("headcount", match.group(0).strip())
    claim.supported = False
    return [claim]


def _enumerable_id_fields(answer: Any) -> list[tuple[str, list[str]]]:
    """The `ReplacementAnswer` fields that ARE the direct answer to a
    question -- "who got excluded," "which flights are uncrewed." Leaves
    out `rows`/`options` on purpose, since those can legitimately be a
    subset or a bare count depending on how the question was asked.
    """
    out: list[tuple[str, list[str]]] = []
    if answer is None:
        return out
    excluded = getattr(answer, "excluded", None)
    if excluded:
        ids = [e.get("crew_id") for e in excluded
               if isinstance(e, dict) and e.get("crew_id")]
        if ids:
            out.append(("excluded", ids))
    for field_name in ("uncovered_flights", "at_risk_flights"):
        flights = getattr(answer, field_name, None)
        if flights:
            out.append((field_name, list(flights)))
    return out


def _completeness_violations(narrative: str, answer: Any) -> list[Claim]:
    """Every id in `_enumerable_id_fields` must actually be named in the
    narrative -- not just some of them.

    The ordinary identifier check can't catch this: 4 real names out of 22
    excluded candidates passes cleanly, since each of the 4 really is real.
    Omitting isn't fabricating, so only a count comparison catches it.
    """
    violations: list[Claim] = []
    for field_name, ids in _enumerable_id_fields(answer):
        if len(ids) < 2:
            continue
        mentioned = sum(1 for i in ids if i in narrative)
        if mentioned < len(ids):
            claim = Claim("completeness",
                          f"{field_name}: named {mentioned}/{len(ids)}")
            claim.supported = False
            violations.append(claim)
    return violations


def verify(narrative: str, trace: Iterable[TraceEntry],
           answer: Any = None) -> VerificationResult:
    """Check a drafted answer against the tools that produced it.

    >>> from schemas import TraceEntry
    >>> t = [TraceEntry(tool="lookup", result={"crew_id": "C-1042", "cost": 18500})]
    >>> verify("C-1042 costs 18,500.", t).ok
    True
    >>> verify("C-9999 costs 18,500.", t).ok
    False
    """
    trace = list(trace)
    evidence = build_evidence(trace)
    claims = extract_claims(narrative)
    claims += _rule_context_violations(narrative, evidence)
    claims += _crew_name_violations(narrative, evidence)
    claims += _malformed_flight_violations(narrative)
    claims += _uncrewed_claim_violations(narrative, trace)
    claims += _count_violations(narrative, trace)
    claims += _completeness_violations(narrative, answer)

    for claim in claims:
        if claim.kind == "identifier":
            claim.source_tool = evidence.has_identifier(claim.value)
            claim.supported = claim.source_tool is not None
            continue

        value = _norm_number(claim.value)
        if value is None:
            continue

        if source := evidence.has_number(value):
            claim.supported, claim.source_tool = True, source
        elif derived := evidence.derive(value):
            claim.supported = True
            claim.derivation, claim.source_tool = derived

    unsupported = [c for c in claims if not c.supported]

    notes: list[str] = []
    if not trace:
        notes.append("no tools were called — nothing in this answer is sourced")
    for entry in trace:
        if entry.error:
            notes.append(f"tool {entry.tool} errored: {entry.error}")

    return VerificationResult(
        ok=not unsupported and bool(trace),
        claims=claims,
        unsupported=unsupported,
        notes=notes,
        had_trace=bool(trace),
    )


RETRY_INSTRUCTION = """\
Your draft contained claims that no tool output supports: {bad}.

Every identifier and number you state must come from a tool result. Either
call the tool that would establish these, or rewrite the answer without them.
Do not restate them from memory.
"""


def retry_prompt(result: VerificationResult) -> str:
    """What to send back to the model when the gate rejects a draft."""
    return RETRY_INSTRUCTION.format(
        bad=", ".join(c.value for c in result.unsupported) or "(none)"
    )
