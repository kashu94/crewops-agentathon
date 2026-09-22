"""Tier and intent classification — the Triage Agent's job.

Deterministic rules run first and settle the overwhelming majority of real
questions (38/38 on this dataset's gold question set) with zero model calls,
which keeps the common path free, fast and reproducible — and keeps the
classifier auditable, since every rule-based decision reports which pattern
fired. The **Triage Agent** (`agents.py::TriageAgent`, a Foundry
`PromptAgentDefinition` with no tools) is only consulted when every rule
abstains.

Tier comes from the intent, never independently: they cannot disagree if only
one of them is ever decided.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

import config
from entities import Entities, extract
from schemas import Confidence, Intent, Tier

# --------------------------------------------------------------------------
# Rules, most specific first. First match wins.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rule:
    intent: Intent
    pattern: re.Pattern[str]
    name: str
    requires_multi_event: bool = False


def _p(*alts: str) -> re.Pattern[str]:
    return re.compile("|".join(alts), re.I)


# --------------------------------------------------------------------------
# Disruption vocabulary
#
# A controller stating that someone cannot fly is asking for cover, however
# they phrase it. Grouped by how the desk actually talks, so gaps are visible.
# --------------------------------------------------------------------------

_SICK = (
    r"\bsick\b", r"\bsick ?call\b", r"\bcall(s|ed|ing)? in\b", r"\bwent sick\b",
    r"\bgone sick\b", r"\boff sick\b", r"\bunwell\b", r"\bill\b", r"\billness\b",
    r"\bmedical\b.*\b(issue|problem)\b", r"\binjured\b", r"\bhurt\b",
)

_UNAVAILABLE = (
    r"\bnot available\b", r"\bunavailable\b", r"\bno longer available\b",
    r"\bcan'?t (fly|operate|make|do|take)\b", r"\bcannot (fly|operate|make|take)\b",
    r"\bunable to (fly|operate|report|make)\b", r"\bunable\b",
    r"\bno[- ]?show\b", r"\bdidn'?t show\b", r"\bfailed to report\b",
    r"\babsent\b", r"\bmissing\b", r"\bawol\b",
    r"\bis out\b", r"\bare out\b", r"\bout for\b", r"\bdropped out\b",
    r"\bbailed\b", r"\bfell (out|through)\b", r"\bpulled\b", r"\bwithdrawn\b",
    r"\btaken off\b", r"\bremoved from\b", r"\bstood down\b", r"\bstand down\b",
)

_OUT_OF_HOURS = (
    r"\btimed? out\b", r"\btiming out\b", r"\bout of hours\b", r"\bran out of hours\b",
    r"\bmaxed out\b", r"\bfatigued?\b", r"\bbust(ed|s)?\b", r"\bblown\b",
    r"\bover (the )?limit\b", r"\bexceeded\b",
)

_NOT_LEGAL = (
    # Past tense only. "expired" is a disruption; "which certifications expire
    # next month" is a listing query.
    r"\bexpired\b", r"\bhas expired\b",
    r"\blapsed\b", r"\bout of date\b", r"\binvalid\b",
    r"\bnot qualified\b", r"\bunqualified\b", r"\bnot rated\b", r"\bno rating\b",
    r"\bout of currency\b", r"\blost currency\b", r"\bgrounded\b",
    r"\bnon[- ]?compliant\b", r"\billegal\b",
)

_ROSTER_GAP = (
    r"\buncrewed\b", r"\buncovered\b", r"\bunassigned\b", r"\bvacant\b",
    r"\bshort[- ]?(crewed|staffed|a|of)?\b", r"\bunderstaffed\b",
    r"\bgap\b", r"\bhole\b", r"\bopen (slot|seat|position)\b",
    r"\bdown a\b", r"\ba man down\b", r"\bone short\b",
    r"\bneeds? (a |an )?(captain|first officer|fo|pilot|crew|cover|someone|body)\b",
    r"\bno (captain|first officer|fo|pilot|crew)\b",
)

_NEEDS_COVER = (
    r"\bwho (can|could|should|else)\b", r"\bwho'?s (available|free|around)\b",
    r"\bwho do i (use|call|send)\b", r"\breplacement\b", r"\bcover(ing)? (for|the|this|that)\b",
    r"\bneed (a |an |some ?)?(cover|replacement|body|one|captain|fo|pilot|crew)\b",
    r"\bfind (me )?(a |an |someone|somebody)\b", r"\bget me\b", r"\bwarm body\b",
    r"\bstand[- ]?in\b", r"\bback[- ]?fill\b", r"\bsub(stitute)?\b", r"\bswap\b",
    r"\bcall out\b", r"\bcallout\b", r"\bwho takes\b", r"\bwho'?ll (take|fly)\b",
)

_AIRCRAFT_EVENT = (
    r"\baog\b", r"\btech(nical)? (issue|problem|delay|fault)\b", r"\bsnag\b",
    r"\bdefect\b", r"\bunserviceable\b", r"\bu/?s\b", r"\bbroken\b",
    r"\bdelayed?\b", r"\bslipp(ed|ing)\b", r"\brunning late\b",
    r"\bcancel(l?ed|lation)?\b", r"\bscrubbed\b", r"\bdiverted?\b",
    r"\bclosed?\b", r"\bclosure\b", r"\bshut\b", r"\bweather\b", r"\bwx\b",
)

DISRUPTION = _SICK + _UNAVAILABLE + _OUT_OF_HOURS + _NOT_LEGAL + _ROSTER_GAP
DISRUPTION_RE = _p(*DISRUPTION)
NEEDS_COVER_RE = _p(*_NEEDS_COVER)
AIRCRAFT_EVENT_RE = _p(*_AIRCRAFT_EVENT)

# A question word turns a bare disruption into a question about consequences
# rather than a request for cover.
ASKS_IMPACT_RE = _p(
    r"\bwhich flights?\b", r"\bwhat (flights?|happens|is affected)\b",
    r"\bhow many (flights?|passengers?|pax)\b", r"\baffected\b", r"\bat risk\b",
    r"\bimpact\b", r"\bknock[- ]?on\b", r"\bdownstream\b",
)

# Boarding-gate vocabulary. Checked before every disruption/legality rule so
# "DX401 is delayed 90 minutes — does it still clear its gate?" routes to
# CHECK_GATE rather than FIND_REPLACEMENT ("delayed" alone reads as a
# disruption) or CHECK_LEGALITY ("exceed"/"limit" wording overlaps).
GATE_RE = _p(r"\bgates?\b", r"\b[a-z]{3}-g\d+\b")


RULES: tuple[Rule, ...] = (
    # Gate questions first: distinctive vocabulary that would otherwise be
    # caught by "delayed" (disruption) or "exceed"/"limit" (legality).
    Rule(Intent.CHECK_GATE, GATE_RE, "gate"),
    # ---- tier 3 -----------------------------------------------------------
    Rule(
        # First, because "draft the callout notification" also contains
        # "callout" — a cover request everywhere else in this table. The verb
        # is what distinguishes them: the controller has already decided who,
        # and wants the message written.
        Intent.DRAFT_NOTIFICATION,
        _p(r"\bdraft\b", r"\bnotification\b", r"\bnotify\b", r"\bmessage (to|for)\b",
           r"\bwrite (the |a )?(message|note|text|sms)\b", r"\binform\b",
           r"\blet (them|him|her|the crew) know\b"),
        "notification",
    ),
    Rule(
        Intent.JOINT_PLAN,
        _p(r"\bjoint\b", r"\bboth\b.*\bsick\b", r"\bsimultaneous", r"\bacross both\b",
           r"\bboth\b.*\bcall(ed)? in\b"),
        "joint",
    ),
    Rule(
        Intent.SIMULATE_WHATIF,
        _p(r"\bwhat if\b", r"\bsuppose\b", r"\binstead\b", r"\bwould happen\b",
           r"\bif .* were\b"),
        "whatif",
    ),
    Rule(
        Intent.RESOLVE_ILLEGAL,
        _p(r"\blapsed\b", r"\bexpired\b.*\bresolve\b", r"\billegal\b",
           r"\bresolve their\b", r"\bnon-?compliant\b"),
        "resolve_illegal",
    ),
    Rule(
        Intent.RANK_OPTIONS,
        # Note: no bare `\brank\b` — "what is C-2087's rank" is a tier-1
        # lookup about seniority, not a request to rank anything.
        _p(r"\branked\b", r"\brank (the |these )?options\b", r"\brank them\b",
           r"\brecommend", r"\bwhat should\b", r"\bshould (it|we|they|the desk)\b",
           r"\bbest (option|course|plan)\b", r"\bresolution options\b",
           r"\boptions with costs?\b", r"\boptimal\b", r"\bproduce .*options\b",
           r"\brecovery plan\b", r"\boutline .*\bplan\b", r"\bbriefing\b",
           r"\bcheapest\b", r"\bleast (cost|expensive)\b",
           r"\bdraft the\b", r"\bnotification\b"),
        "rank",
    ),
    # ---- tier 2 -----------------------------------------------------------
    Rule(
        Intent.CHECK_LEGALITY,
        _p(r"\bbreach", r"\bis it legal\b", r"\bdoes any rule\b", r"\bany rule\b",
           r"\blegal(ly)? (to |for )?(assign|cover|operate)", r"\bviolat",
           r"\bexceed", r"\bwithin (the )?limits?\b",
           r"\blegal\s*\?", r"\bproposed to (cover|operate|fly)\b", r"\bproposed\b",
           r"\bincluding any planned\b", r"\bif (assigned|rostered)\b",
           r"\bearliest .*\breport\b", r"\bmay report\b", r"\bwhen can .*report\b"),
        "legality",
    ),
    # Impact is checked before cover: a disruption plus "which flights" is a
    # question about consequences, not a request for a name.
    Rule(
        Intent.IMPACT_OF_EVENT,
        _p(r"\baffected\b", r"\bat risk\b", r"\bimmediately\b.*\bflights?\b",
           r"\bwhich flights\b.*\b(lose|lost|uncrewed|uncovered)\b",
           r"\bknock[- ]?on\b", r"\bdownstream\b", r"\bimpact\b",
           r"\bclosure\b", r"\bcloses?\b.*\b\d{2}:\d{2}\b"),
        "impact",
    ),
    Rule(Intent.FIND_REPLACEMENT, NEEDS_COVER_RE, "cover-request"),
    # A bare statement that someone cannot fly IS a request for cover.
    Rule(Intent.FIND_REPLACEMENT, DISRUPTION_RE, "disruption"),
    # ---- tier 1 -----------------------------------------------------------
    Rule(
        Intent.LOOKUP_DUTY_CLOCK,
        _p(r"\bduty hours?\b", r"\bblock hours?\b", r"\bflight hours?\b",
           r"\bheadroom\b", r"\baccrued\b", r"\bduty clock\b", r"\brest\b.*\bhow much\b"),
        "duty_clock",
    ),
    Rule(
        Intent.LOOKUP_RESERVE,
        _p(r"\breserves?\b", r"\bon-?call\b", r"\bstandby\b"),
        "reserve",
    ),
    Rule(
        Intent.LOOKUP_CERT,
        _p(r"\bcertificat", r"\bexpir", r"\bmedical\b", r"\blicence\b", r"\blicense\b",
           r"\brecurrent\b", r"\bdangerous goods\b", r"\bvalid(ity)?\b"),
        "cert",
    ),
    Rule(
        Intent.EXPLAIN_RULE,
        _p(r"\bwhat (is|does)\b.*\brule\b", r"\bexplain\b.*\brule\b",
           r"\bwhich rule\b", r"\brule\b.*\bmean\b"),
        "explain_rule",
    ),
    Rule(
        # Risk scores are provided input, not something modelled here — but a
        # question about one is still a lookup, and must not fall through.
        Intent.LOOKUP_RISK,
        _p(r"\brisk score\b", r"\bdisruption[- ]risk\b", r"\brisk signal",
           r"\bwhat drives\b"),
        "risk",
    ),
    Rule(
        Intent.LOOKUP_FLIGHT,
        _p(r"\bwhich flights?\b", r"\bflights? (depart|arrive|from|to|operate)\b",
           r"\bhow many flights\b", r"\bhow many seats\b",
           r"\bwhich aircraft (operates|flies)\b", r"\bblock time\b",
           r"\bdepartures?\b", r"\barrivals?\b", r"\bschedule\b",
           r"\bstations?\b.*\b(serve|network|nonstop|non-stop)\b",
           r"\bnetwork\b", r"\bnonstop\b", r"\bnon-stop\b", r"\broutes?\b"),
        "flight",
    ),
    Rule(
        Intent.LOOKUP_ROSTER,
        _p(r"\brostered?\b", r"\bpairing\b", r"\bwho is (flying|operating|on)\b",
           r"\bcrew (complement|list|of)\b"),
        "roster",
    ),
    Rule(
        Intent.LOOKUP_CREW,
        # A confident wrong table is worse than an empty one, so a bare "how
        # many" has to name what is being counted before it lands here.
        _p(r"\bwho\b", r"\blist all\b", r"\bqualified\b",
           r"\brating\b", r"\bbased at\b",
           r"\bhow many\b\s+(crew|captains?|first officers?|pilots?|cabin crew|"
           r"senior cabin crew|people|reserves?)\b"),
        "crew",
    ),
)


# Tier-1 rules that each answer ONE aspect of one crew member, and each offer a
# single tool. A controller opening a person's file asks for several at once —
# "duty headroom, expiring certs, and any risk" is three of these in one
# sentence. First-match-wins gives that question the duty clock alone, and the
# other two thirds cannot be answered because their tools were never offered.
_PERSON_ASPECTS = frozenset({
    Intent.LOOKUP_DUTY_CLOCK,
    Intent.LOOKUP_CERT,
    Intent.LOOKUP_RISK,
    Intent.LOOKUP_ROSTER,
})


@dataclass(slots=True)
class Route:
    """The router's decision, with its reasoning attached."""

    intent: Intent
    tier: Tier
    entities: Entities
    confidence: Confidence = Confidence.HIGH
    matched_rule: str | None = None
    used_llm: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "intent": str(self.intent),
            "tier": int(self.tier),
            "entities": self.entities.to_dict(),
            "confidence": str(self.confidence),
            "matched_rule": self.matched_rule,
            "used_llm": self.used_llm,
            "notes": self.notes,
        }


def _is_multi_event(text: str, ents: Entities) -> bool:
    """Two disruptions at once.

    Two crew ids alone is not enough ("can C-1042 cover for C-2087?" names two
    and is a single event), so require a disruption verb as well.
    """
    if not DISRUPTION_RE.search(text):
        return False

    named_twice = (
        len(ents.crew_ids) >= 2
        or len(ents.pairing_ids) >= 2
        or len(ents.aircraft) >= 2
    )
    # "both captains are sick" is two events without naming either one.
    said_twice = re.search(r"\bboth\b|\bsimultaneous|\btwo\b.*\bcaptains?\b", text, re.I)
    return named_twice or bool(said_twice)


def route_deterministic(text: str, ents: Entities | None = None) -> Route | None:
    """Classify by pattern. Returns None when no rule is confident enough."""
    ents = ents if ents is not None else extract(text)
    multi = _is_multi_event(text, ents)

    for rule in RULES:
        if rule.requires_multi_event and not multi:
            continue
        if not rule.pattern.search(text):
            continue

        intent = rule.intent
        notes: list[str] = []

        # A ranking ask over two concurrent disruptions is a joint plan, even
        # when the word "joint" never appears.
        if intent is Intent.RANK_OPTIONS and multi:
            intent = Intent.JOINT_PLAN
            notes.append("upgraded to JOINT_PLAN: two concurrent events")

        # A disruption plus an impact question asks what breaks, not who covers.
        elif intent is Intent.FIND_REPLACEMENT and ASKS_IMPACT_RE.search(text):
            intent = Intent.IMPACT_OF_EVENT
            notes.append("IMPACT_OF_EVENT: disruption stated with an impact question")

        # Two simultaneous disruptions are one joint problem, not two.
        elif intent is Intent.FIND_REPLACEMENT and multi:
            intent = Intent.JOINT_PLAN
            notes.append("upgraded to JOINT_PLAN: two concurrent events")

        # A question naming several aspects of one person is a dossier, not
        # the first aspect that happened to match. Requires a crew id — "which
        # flights are at risk" names an aspect but no person.
        elif intent in _PERSON_ASPECTS and ents.crew_ids:
            also = sorted(
                rule.name for rule in RULES
                if rule.intent in _PERSON_ASPECTS
                and rule.intent is not intent
                and rule.pattern.search(text)
            )
            if also:
                intent = Intent.LOOKUP_CREW
                notes.append(
                    f"widened to LOOKUP_CREW: asks for {rule.name} and "
                    f"{', '.join(also)} together")

        return Route(
            intent=intent,
            tier=intent.tier,
            entities=ents,
            confidence=Confidence.HIGH,
            matched_rule=rule.name,
            notes=notes,
        )
    return None


ROUTER_INSTRUCTIONS = """\
You classify crew-control questions. Reply with JSON only:
{"intent": "<INTENT>", "confidence": "high|medium|low"}

Valid intents:
  Tier 1  LOOKUP_ROSTER LOOKUP_RESERVE LOOKUP_DUTY_CLOCK LOOKUP_CERT
          LOOKUP_FLIGHT LOOKUP_CREW EXPLAIN_RULE CHECK_GATE
  Tier 2  FIND_REPLACEMENT CHECK_LEGALITY IMPACT_OF_EVENT
  Tier 3  RANK_OPTIONS SIMULATE_WHATIF JOINT_PLAN RESOLVE_ILLEGAL

Entities are already extracted deterministically; classify the *kind* of ask.
"""

TriageFn = Callable[[str, str], str]
"""What `TriageAgent.run(system, user_text)` looks like: one Foundry Agent
call with no tools, returning the raw response text."""


def route_llm(text: str, ents: Entities, triage: TriageFn) -> Route:
    """Fallback path. Only reached when every deterministic rule abstained.

    Calls the Triage Agent — a `PromptAgentDefinition` with no tools — and
    parses its JSON reply. An unparseable reply, or no Triage Agent at all,
    degrades to the safest guess: a lookup, which reads data and changes
    nothing.
    """
    raw = triage(ROUTER_INSTRUCTIONS, text)

    try:
        payload = json.loads(raw)
        intent = Intent(payload["intent"])
        confidence = Confidence(payload.get("confidence", "medium"))
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return Route(
            intent=Intent.LOOKUP_CREW,
            tier=Tier.LOOKUP,
            entities=ents,
            confidence=Confidence.LOW,
            used_llm=True,
            notes=["Triage Agent gave no usable classification; defaulted to lookup"],
        )

    return Route(
        intent=intent,
        tier=intent.tier,
        entities=ents,
        confidence=confidence,
        used_llm=True,
    )


def route(text: str, triage: TriageFn | None = None) -> Route:
    """Classify a controller's question.

    >>> r = route("Who is on reserve at BLR on 2026-09-15?")
    >>> r.intent, r.tier
    (<Intent.LOOKUP_RESERVE: 'LOOKUP_RESERVE'>, <Tier.LOOKUP: 1>)

    >>> route("Both A320 captains are sick. Give the optimal joint plan.").tier
    <Tier.CONSEQUENCE: 3>
    """
    ents = extract(text)
    if decided := route_deterministic(text, ents):
        return decided

    if triage is None:
        # No Triage Agent wired up (e.g. running offline / under test):
        # degrade to the same safe default as an unparseable reply.
        return Route(
            intent=Intent.LOOKUP_CREW, tier=Tier.LOOKUP, entities=ents,
            confidence=Confidence.LOW, used_llm=False,
            notes=["no deterministic rule matched and no Triage Agent was given"],
        )
    return route_llm(text, ents, triage)
