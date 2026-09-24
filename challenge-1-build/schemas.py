"""Typed answer objects.

The agent builds one of these, and prose is rendered *from* it, never the
other way round. That's what lets the deterministic renderer in
`explainer.py` and the trust gate in `verifier.py` both work off one object.

`Option` keeps its field names stable (`action`, `crew_id`, `legal`,
`rules_checked`, `cost_inr`, `delay_hours`, `rank`) because they mirror the
crew-ops-advisor-dataset's own scenario answer keys. Fields may be added,
but not renamed, without breaking evaluation against `data/scenarios.json`.
"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from typing import Any


class Tier(enum.IntEnum):
    LOOKUP = 1
    REPLACEMENT = 2
    CONSEQUENCE = 3


class Intent(enum.StrEnum):
    # tier 1
    LOOKUP_ROSTER = "LOOKUP_ROSTER"
    LOOKUP_RESERVE = "LOOKUP_RESERVE"
    LOOKUP_DUTY_CLOCK = "LOOKUP_DUTY_CLOCK"
    LOOKUP_CERT = "LOOKUP_CERT"
    LOOKUP_RISK = "LOOKUP_RISK"
    LOOKUP_FLIGHT = "LOOKUP_FLIGHT"
    LOOKUP_CREW = "LOOKUP_CREW"
    EXPLAIN_RULE = "EXPLAIN_RULE"
    CHECK_GATE = "CHECK_GATE"
    LOOKUP_CONTROLLERS = "LOOKUP_CONTROLLERS"
    # tier 2
    FIND_REPLACEMENT = "FIND_REPLACEMENT"
    CHECK_LEGALITY = "CHECK_LEGALITY"
    IMPACT_OF_EVENT = "IMPACT_OF_EVENT"
    # tier 3
    RANK_OPTIONS = "RANK_OPTIONS"
    SIMULATE_WHATIF = "SIMULATE_WHATIF"
    JOINT_PLAN = "JOINT_PLAN"
    RESOLVE_ILLEGAL = "RESOLVE_ILLEGAL"
    DRAFT_NOTIFICATION = "DRAFT_NOTIFICATION"

    @property
    def tier(self) -> Tier:
        return _INTENT_TIER[self]


_INTENT_TIER: dict[Intent, Tier] = {
    Intent.LOOKUP_ROSTER: Tier.LOOKUP,
    Intent.LOOKUP_RESERVE: Tier.LOOKUP,
    Intent.LOOKUP_DUTY_CLOCK: Tier.LOOKUP,
    Intent.LOOKUP_CERT: Tier.LOOKUP,
    Intent.LOOKUP_RISK: Tier.LOOKUP,
    Intent.LOOKUP_FLIGHT: Tier.LOOKUP,
    Intent.LOOKUP_CREW: Tier.LOOKUP,
    Intent.EXPLAIN_RULE: Tier.LOOKUP,
    Intent.CHECK_GATE: Tier.LOOKUP,
    Intent.LOOKUP_CONTROLLERS: Tier.LOOKUP,
    Intent.FIND_REPLACEMENT: Tier.REPLACEMENT,
    Intent.CHECK_LEGALITY: Tier.REPLACEMENT,
    Intent.IMPACT_OF_EVENT: Tier.REPLACEMENT,
    Intent.RANK_OPTIONS: Tier.CONSEQUENCE,
    Intent.SIMULATE_WHATIF: Tier.CONSEQUENCE,
    Intent.JOINT_PLAN: Tier.CONSEQUENCE,
    Intent.RESOLVE_ILLEGAL: Tier.CONSEQUENCE,
    Intent.DRAFT_NOTIFICATION: Tier.CONSEQUENCE,
}


class Verdict(enum.StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class Confidence(enum.StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# --------------------------------------------------------------------------
# Core value objects
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleVerdict:
    """One rule's evaluation. Always carries the numbers, never a bare bool."""

    rule_id: str
    status: Verdict
    detail: str = ""
    used: float | None = None
    limit: float | None = None
    headroom: float | None = None
    date: str | None = None

    @property
    def failed(self) -> bool:
        """Compare by value, not identity. `status` may arrive as the plain
        string "FAIL" once it has crossed a tool-call JSON boundary, and
        `is` returns False for that."""
        return str(self.status) == str(Verdict.FAIL)


@dataclass(slots=True)
class Option:
    """A candidate resolution."""

    action: str
    crew_id: str | None
    legal: bool
    rules_checked: list[str] = field(default_factory=list)
    cost_inr: int = 0
    delay_hours: float = 0.0
    rank: int = 0

    cost_breakdown: dict[str, int] = field(default_factory=dict)
    blast_radius: int = 0
    verdicts: list[RuleVerdict] = field(default_factory=list)
    unlock: str | None = None
    """Near-miss only: what would make this legal, e.g. 'departure slips 35 min'."""

    # Who the candidate is. A controller acts on a person, not an id.
    name: str = ""
    seniority: int | None = None
    base: str = ""
    reachability_minutes: int | None = None
    disruption_risk_score: float | None = None
    """Provided input, reported beside the option and never used to rank it."""

    strategy: str = ""
    """One of "reserve_callout" / "day_off_callout" / "deadhead_reposition" /
    "cancel" — set only on the one-per-strategy entries in `strategies`
    (below), empty on the full per-candidate `options` list."""
    strategy_label: str = ""

    @property
    def is_near_miss(self) -> bool:
        return not self.legal and self.unlock is not None


@dataclass(slots=True)
class FunnelStage:
    """One narrowing step, with the reason for every drop."""

    stage: str
    count: int
    dropped: int = 0
    reason: str = ""


@dataclass(slots=True)
class BlastRadius:
    nodes: int = 0
    flights: int = 0
    aircraft: int = 0
    passengers: int = 0
    edges: list[dict[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class Citation:
    kind: str            # "rule" | "record"
    id: str
    source: str | None = None


@dataclass(slots=True)
class TraceEntry:
    """One tool invocation. The verifier checks claims against these."""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    ms: int = 0
    error: str | None = None


# --------------------------------------------------------------------------
# Tier-specific answers
# --------------------------------------------------------------------------


@dataclass(slots=True)
class LookupAnswer:
    kind: str = "lookup"
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.rows)


@dataclass(slots=True)
class NotificationAnswer:
    """A message to a crew member, and the roster facts it was built from."""

    kind: str = "notification"
    message: str = ""
    brief: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ReplacementAnswer:
    kind: str = "replacement"
    recommended: Option | None = None
    cancellation_multiple: int = 0
    equal_cost_alternatives: int = 0
    next_tier_cost_inr: int = 0
    next_tier_premium_inr: int = 0
    uncovered_flights: list[str] = field(default_factory=list)
    at_risk_flights: list[str] = field(default_factory=list)
    passengers_affected: int = 0
    funnel: list[FunnelStage] = field(default_factory=list)
    options: list[Option] = field(default_factory=list)
    strategies: list[Option] = field(default_factory=list)
    """A policy-backed ranking across the four strategies a controller
    actually chooses between (reserve callout, day-off callout, deadhead/
    reposition, cancel): one representative each, the cheapest legal
    candidate per strategy, cancel always last. `options` above still
    carries every individual legal candidate; this is the four-line
    executive summary."""
    near_misses: list[Option] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)
    subject_name: str = ""
    subject_rank: str = ""

    # A direct legality question answers with verdicts rather than candidates.
    subject: str | None = None
    legal: bool | None = None
    verdicts: list[RuleVerdict] = field(default_factory=list)


@dataclass(slots=True)
class ConsequenceAnswer:
    kind: str = "consequence"
    options: list[Option] = field(default_factory=list)
    strategies: list[Option] = field(default_factory=list)
    blast_radius: BlastRadius | None = None
    world_diff: dict[str, Any] | None = None
    joint_plan: dict[str, Any] | None = None


AnswerBody = LookupAnswer | ReplacementAnswer | ConsequenceAnswer


@dataclass(slots=True)
class AdvisorResponse:
    """What the Resolution Advisor pipeline returns for one question."""

    tier: Tier
    intent: Intent
    query: str = ""
    entities: dict[str, Any] = field(default_factory=dict)
    answer: AnswerBody | None = None
    narrative: str = ""
    citations: list[Citation] = field(default_factory=list)
    confidence: Confidence = Confidence.HIGH
    unknowns: list[str] = field(default_factory=list)
    trace: list[TraceEntry] = field(default_factory=list)

    awaiting: str | None = None
    """Set when this answer is a question back to the controller.

    `"confirmation"` — an id or rank needs confirming before anything runs.
    `"detail"`       — something is missing, e.g. which date.
    """

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tier"] = int(self.tier)
        d["intent"] = str(self.intent)
        d["confidence"] = str(self.confidence)
        return d


ERROR_CODES = (
    "UNRESOLVED_ENTITY",
    "NEEDS_CONFIRMATION",   # a near match was found; ask before acting
    "AMBIGUOUS_QUERY",
    "NO_LEGAL_OPTION",
    "OUT_OF_SCOPE",
    "INTERNAL",
)
