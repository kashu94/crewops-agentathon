"""The trust gate.

Before any answer reaches a controller, every identifier and every number in
its prose must be traceable to something a tool actually returned. Anything
unsupported is a hallucination by definition, because the model has no other
source of facts.

This is what makes the demo credible, and it is deliberately not a language
model: it is set membership over the trace. Deterministic, fast, and it
cannot itself hallucinate. It runs after every Explainer Agent call, and its
rejection is what triggers the deterministic-template fallback in
`explainer.render()`.
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

# Non-capturing so `findall` yields whole matches. A date is one claim, not
# three numbers — checking 2026-09-15 as "2026" + "09" + "15" both floods the
# ledger and lets a wrong date pass on the strength of its year.
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

# A whole ISO timestamp, matched before anything looks for a time inside it.
ISO_DT_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)

# A time of day, once timestamps are out of the way. `\b` cannot open this:
# a bare time may be preceded by `-` in a range like `06:00-18:00`.
CLOCK_RE = re.compile(r"(?<![\d:])\d{1,2}:\d{2}(?::\d{2})?Z?(?![\d:])")


def times_and_rest(text: str) -> tuple[list[str], str]:
    """Times of day named by any timestamps, and the text with them removed.

    Used on both sides of the gate so a report time is one fact whichever way
    it is written — the dataset stores `2026-09-15T06:00:00+00:00`, a
    controller reads `06:00Z`, and the model turning one into the other is
    doing its job rather than inventing something.
    """
    found: list[str] = []
    for match in ISO_DT_RE.findall(text):
        stamp = match.split("T")[-1].split(" ")[-1]
        if norm := normalise_clock(stamp):
            found.append(norm)
    return found, ISO_DT_RE.sub(" ", text)


def normalise_clock(value: str) -> str | None:
    """`6:00`, `06:00Z`, `06:00:00+00:00` -> `06:00`. Anything else -> None.

    Deliberately narrow on both edges: this is the one place the gate treats
    two different strings as the same fact.

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

# A name stated immediately before the id it belongs to -- "A. Nair
# (C-1042)" -- in the `Initial. Surname` shape every one of this dataset's
# 150 crew names actually has (verified: zero exceptions). This is the one
# place a name becomes a checkable claim rather than free prose: the id
# next to it is exactly what evidence.names_by_id below is keyed on.
NAME_ID_RE = re.compile(r"([A-Z]\.\s?[A-Z][a-zA-Z'-]+)\s*\((C-\d{4})\)")

# A date written the way a controller writes it. `DATE_RE` above catches the
# ISO form the tools return, but the model answers "on 15 Sep" — and the bare
# `15` then reads as an unsourced quantity, because no tool output contains
# the number 15 on its own.
#
# A calendar reference is not a claim about the world. The date itself is
# still checked wherever it appears in ISO form.
PROSE_DATE_RE = re.compile(
    r"\b\d{1,2}\s*(?:st|nd|rd|th)?\s+"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+"
    r"\d{1,2}\s*(?:st|nd|rd|th)?\b",
    re.I,
)

# "in the next 30 days", "over the last 14 days" — the window the question
# itself named, echoed back. It describes the scope of the answer, not a
# measured value, and no tool returns it as a number to match against.
WINDOW_RE = re.compile(
    r"\b(?:next|last|past|coming|previous|preceding)\s+\d+\s*"
    r"(?:day|week|month|hour|hr)s?\b",
    re.I,
)

# A rule id being *cited* is not, by itself, evidence that the claim about it
# is true — RULE-DUTY-02 appearing in a trace could mean it passed. Set
# membership over identifiers cannot tell "RULE-DUTY-02 was mentioned" from
# "RULE-DUTY-02 is why this fails", so a claim naming a rule alongside a
# fail/pass verb is checked against that rule's actual status wherever the
# trace recorded one — the one place a plausible-sounding misattribution
# ("breaches RULE-DUTY-02" when RULE-FDP-01 was the one that failed) is
# otherwise invisible to this gate.
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
    """Set when the value was not returned by any tool but follows from two
    that were, e.g. "24000 - 18500". The claim is still auditable: a reader
    can check the arithmetic against numbers the tools did produce."""

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
    """Yield every scalar in an arbitrarily nested tool result.

    Collection lengths are yielded too. "12 records" is a count the explainer
    derived from what a tool returned, not a number the model invented, so
    the cardinality of every result is legitimate evidence.
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
    """Blank out ids before scanning for numbers.

    Without this, "C-1042" contributes 1042 — which both invents a claim on
    the narrative side and, worse, would let a fabricated "1042 hours" pass
    verification on the evidence side.
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
    """rule_id -> every status ("PASS"/"FAIL") a verdict for it actually
    carried anywhere in the trace. Populated only from dicts shaped like a
    `RuleVerdict` (both `rule_id` and `status` present) -- see `_rule_walk`."""
    names_by_id: dict[str, str] = field(default_factory=dict)
    """crew_id -> the name a tool actually returned for it. A real id with a
    name that doesn't match this is exactly as unsourced as an invented id
    -- the model has no other way to know anyone's name than a tool telling
    it, the same rule that already applies to every number and id."""

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
        """Whether `value` follows from two evidence numbers by simple arithmetic.

        A price difference, a total, a multiple, a percentage — these are
        things a controller genuinely wants said, and a model will compute
        them whether or not a tool did. Rejecting a correct derived value is a
        false positive that costs as much as a false negative, so it is
        accepted *and labelled with its derivation* — auditable against the
        numbers the tools did return.

        Deliberately narrow: pairs only, no chaining, both operands must
        themselves be evidence.
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
    """Yield (crew_id, name) for every dict anywhere in a nested tool result
    that carries both -- `lookup(crew)` rows, `check_legality`/`duty_clock`'s
    own result, `find_options`'s options/excluded entries, `same_pairing`'s
    crew_a/crew_b, `suggest_crew_ids`/`suggest_crew_names`'s candidates, all
    already share this shape without any tool needing to change."""
    if isinstance(node, dict):
        if isinstance(node.get("crew_id"), str) and isinstance(node.get("name"), str):
            yield node["crew_id"], node["name"]
        for value in node.values():
            yield from _crew_names(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _crew_names(item)


def _rule_verdicts(node: Any) -> Iterable[tuple[str, str]]:
    """Yield (rule_id, status) for every dict shaped like a `RuleVerdict`
    anywhere in a nested tool result -- `check_legality`'s and
    `find_options`'s option/excluded entries both carry these."""
    if isinstance(node, dict):
        if "rule_id" in node and "status" in node:
            yield str(node["rule_id"]), str(node["status"]).upper()
        for value in node.values():
            yield from _rule_verdicts(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _rule_verdicts(item)


def build_evidence(trace: Iterable[TraceEntry]) -> Evidence:
    """Index every scalar every tool returned.

    Arguments are indexed too: an id the model passed *in* came from an
    earlier result or from the controller's own question, so echoing it back
    is fine.
    """
    evidence = Evidence()

    for entry in trace:
        for rule_id, status in _rule_verdicts(entry.result):
            evidence.rule_statuses.setdefault(rule_id, set()).add(status)
        for crew_id, name in _crew_names(entry.result):
            evidence.names_by_id.setdefault(crew_id, name)

    for entry in trace:
        # `error` counts as evidence: it is text a tool produced, and an id it
        # names is sourced exactly the way an id in a successful result is.
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
        # Small integers are prose ("all 7 rules", "the 2 options"), not
        # claims about the world. Costs, hours and counts that matter clear
        # the floor.
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
    """A rule id cited with a fail verb whose evidence never showed FAIL for
    it (or a pass verb that never showed PASS) is unsupported -- the rule id
    itself may well be in evidence, just not for the reason claimed. This is
    the one context-sensitive check in an otherwise pure membership test,
    because "which rule actually caused this failure" is exactly the kind of
    plausible-sounding misattribution this domain cannot tolerate."""
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
        # Not kind="identifier" -- the main loop below would then re-derive
        # `supported` from `evidence.has_identifier(rule_id)`, which is True
        # (the rule id genuinely was mentioned by a tool) and would silently
        # undo this claim's whole point. "rule_context" claims are already
        # finalised here and the loop leaves any non-"identifier" claim's
        # `supported` flag alone (its number-parse attempt fails harmlessly).
        claim = Claim("rule_context", f"{rule_id} ({wanted.lower()})")
        claim.supported = False
        violations.append(claim)
    return violations


def _crew_name_violations(narrative: str, evidence: Evidence) -> list[Claim]:
    """A name stated next to a real crew id that no tool result ever paired
    with that id -- because it names a different person entirely, or
    because that id only ever appeared in the trace bare (a `pairing_crew`
    row has a crew_id and a role, never a name). The id being real and
    genuinely sourced is not enough: the model has no other way to know
    anyone's name than a tool telling it, the same rule already enforced
    for every number and id, just never checked for the one piece of
    information a controller actually reads first.

    Skips any crew_id that isn't sourced at all -- a fully invented id is
    already caught by the ordinary "identifier" claim check, so this stays
    scoped to its own job: a real id under the wrong (or unverified) name.
    """
    violations: list[Claim] = []
    seen: set[str] = set()
    for name, crew_id in NAME_ID_RE.findall(narrative):
        if crew_id not in evidence.identifiers:
            continue
        if evidence.names_by_id.get(crew_id) == name:
            continue
        key = f"{crew_id}:{name}"
        if key in seen:
            continue
        seen.add(key)
        # Not kind="identifier" for the same reason `_rule_context_violations`
        # isn't: the main loop would re-derive `supported` from the crew_id
        # alone (True, it is real) and silently undo this claim.
        claim = Claim("crew_name", f"{name} ({crew_id})")
        claim.supported = False
        violations.append(claim)
    return violations


def verify(narrative: str, trace: Iterable[TraceEntry]) -> VerificationResult:
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
