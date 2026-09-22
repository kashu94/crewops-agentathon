"""Planning and answer-assembly — the deterministic glue between the Router,
the Resolution Advisor Agent's tool loop, and the Explainer Agent.

    ROUTER -> PLANNER (this file) -> TOOL LOOP -> VERIFIER -> EXPLAINER

None of this is a model call. `seed_calls()` and `followup_calls()` turn the
entities the router already extracted into the tool calls a controller's
question obviously needs, before the Resolution Advisor Agent is consulted at
all — which saves a round trip and guarantees the model sees real data before
it says anything. `build_answer()` folds the resulting trace into the typed
answer object that `explainer.render()` turns into prose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from datetime import date, timedelta
from typing import Any

import config
from entities import stated_ranks
from router import Route
from schemas import (
    BlastRadius,
    ConsequenceAnswer,
    FunnelStage,
    Intent,
    LookupAnswer,
    NotificationAnswer,
    Option,
    ReplacementAnswer,
    RuleVerdict,
    Tier,
    TraceEntry,
    Verdict,
)
from tools import TOOL_SCHEMAS, schemas_for_port


@dataclass(slots=True)
class ToolCall:
    """One requested tool invocation — from a seed, a follow-up, or the model."""

    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Which tools a given intent needs
# --------------------------------------------------------------------------

_INTENT_TOOLS: dict[Intent, tuple[str, ...]] = {
    Intent.LOOKUP_ROSTER: ("lookup",),
    Intent.LOOKUP_RESERVE: ("lookup",),
    Intent.LOOKUP_CREW: ("lookup",),
    Intent.LOOKUP_FLIGHT: ("lookup",),
    Intent.LOOKUP_CERT: ("lookup",),
    Intent.LOOKUP_RISK: ("lookup",),
    Intent.DRAFT_NOTIFICATION: ("notification_brief", "lookup"),
    Intent.LOOKUP_DUTY_CLOCK: ("duty_clock",),
    Intent.EXPLAIN_RULE: ("explain_rule",),
    Intent.CHECK_GATE: ("check_gate",),
    Intent.CHECK_LEGALITY: ("check_legality", "explain_rule"),
    Intent.FIND_REPLACEMENT: ("find_options", "check_legality"),
    Intent.IMPACT_OF_EVENT: ("ripple", "lookup"),
    Intent.RANK_OPTIONS: ("find_options", "ripple", "check_legality"),
    Intent.SIMULATE_WHATIF: ("simulate", "ripple", "find_options"),
    Intent.JOINT_PLAN: ("joint_plan", "find_options"),
    Intent.RESOLVE_ILLEGAL: ("check_legality", "find_options", "ripple"),
}


def tools_for(intent: Intent, port: Any = None) -> list[dict[str, Any]]:
    """Narrow the toolset to what this intent can plausibly need.

    A tier-1 lookup does not get `joint_plan` in its schema list. Fewer, more
    relevant tools measurably improves selection and cuts prompt size.
    """
    schemas = schemas_for_port(port) if port is not None else TOOL_SCHEMAS
    allowed = set(_INTENT_TOOLS.get(intent, ()))
    narrowed = [t for t in schemas if t["name"] in allowed]
    return narrowed or schemas


MISSING_FOR_INTENT: dict[Intent, str] = {
    Intent.CHECK_LEGALITY: "a crew member and the pairing or flight to check them against",
    Intent.FIND_REPLACEMENT: "who is unavailable, or which pairing or flight needs cover",
    Intent.RANK_OPTIONS: "which pairing or flight needs cover",
    Intent.IMPACT_OF_EVENT: "who or what is disrupted",
    Intent.JOINT_PLAN: "at least two pairings or crew members",
    Intent.SIMULATE_WHATIF: "what to change, and which pairing or flight it affects",
    Intent.RESOLVE_ILLEGAL: "the crew member and date whose assignment is in question",
    Intent.EXPLAIN_RULE: "a rule id, e.g. RULE-DUTY-02",
    Intent.LOOKUP_DUTY_CLOCK: "a crew id, e.g. C-1042",
    Intent.CHECK_GATE: "a flight (id, or number with date) and/or a boarding gate number",
    # An unfiltered crew lookup is 150 rows, which is not an answer to
    # anything a controller actually asked.
    Intent.LOOKUP_CREW: ("something to narrow by — a crew id, a rank, a base, "
                         "or an aircraft rating"),
}


def explain_no_tools(route: Route) -> str:
    """Why nothing ran — never an empty answer.

    An empty trace with an empty answer object reads to a controller as "no
    data was returned", which is indistinguishable from "nothing is wrong" —
    the one outcome this system must never produce. So an empty trace reports
    what was understood, what is missing, and what would unblock it.
    """
    ents = route.entities.to_dict()
    lines = [f"I read this as {str(route.intent).replace('_', ' ').lower()} "
             f"but could not run it."]

    if ents:
        def _render(v: Any) -> str:
            return ", ".join(map(str, v)) if isinstance(v, (list, tuple)) else str(v)

        found = "; ".join(f"{k.replace('_', ' ')}: {_render(v)}" for k, v in ents.items())
        lines.append(f"\nI did pick out — {found}")
    else:
        lines.append("\nI could not pick out a single id from that. Crew look "
                     "like C-1042, pairings like P-2291, flights like DX412.")

    if needed := MISSING_FOR_INTENT.get(route.intent):
        lines.append(f"\nTo answer it I need {needed}.")

    lines.append("\nNothing was checked, so this is not evidence that the "
                 "operation is clean.")
    return "\n".join(lines)


def _flight_filters(ents: Any) -> dict[str, Any]:
    """Build a flight filter, honouring a destination when one is named.

    "BLR->BOM" names two stations. Filtering on the first alone returns every
    departure from BLR and silently drops half the question.
    """
    filters: dict[str, Any] = {}
    if ents.stations:
        filters["dep_station"] = ents.stations[0]
    if len(ents.stations) > 1:
        filters["arr_station"] = ents.stations[1]
    if ents.primary_date:
        filters["date"] = ents.primary_date
    if ents.flight_nos:
        filters["flight_no"] = ents.flight_nos[0]
    return filters


def _crew_filters(ents: Any) -> dict[str, Any]:
    """Narrow a crew listing by whatever the question actually pinned down.

    An unfiltered `crew` lookup is 150 rows, which answers "who is available"
    with the entire airline.
    """
    filters: dict[str, Any] = {}
    if ents.primary_crew:
        filters["crew_id"] = ents.primary_crew
    if ents.roles:
        filters["rank"] = ents.roles[0]
    if ents.stations:
        filters["base"] = ents.stations[0]
    if ents.aircraft_types:
        filters["ratings"] = ents.aircraft_types[0]
    return filters


def _cert_filters(ents: Any) -> dict[str, Any]:
    """Build a certification filter, turning "within 30 days" into an interval.

    An expiry question is an interval, not a point. Computing that window here
    keeps the selection below the trust boundary; the alternative is handing
    the model 600 rows and asking it to pick the right ones, which is exactly
    the arithmetic it must never do.
    """
    filters: dict[str, Any] = {}
    if ents.primary_crew:
        filters["crew_id"] = ents.primary_crew
    if ents.cert_types:
        filters["cert_type"] = ents.cert_types[0]
    if ents.horizon_days:
        start = date.fromisoformat(ents.primary_date or config.WEEK_START)
        end = start + timedelta(days=ents.horizon_days)
        filters["valid_to"] = {"gte": start.isoformat(), "lte": end.isoformat()}
    return filters


def seed_calls(route: Route) -> list[ToolCall]:
    """First tool calls implied by the entities, before the model is consulted.

    The router already extracted the ids deterministically, so for the common
    shapes we know the opening move. This saves a round-trip and guarantees
    the model sees real data before it says anything.
    """
    ents = route.entities
    calls: list[ToolCall] = []

    def add(name: str, **args: Any) -> None:
        calls.append(ToolCall(id=f"seed-{len(calls)}", name=name, args=args))

    match route.intent:
        case Intent.EXPLAIN_RULE if ents.rule_ids:
            for rule_id in ents.rule_ids:
                add("explain_rule", rule_id=rule_id)

        case Intent.LOOKUP_DUTY_CLOCK if ents.primary_crew:
            add("duty_clock", crew_id=ents.primary_crew, date=ents.primary_date)

        case Intent.LOOKUP_RESERVE:
            filters = {"base": ents.stations[0]} if ents.stations else {}
            add("lookup", entity="reserves", filters=filters)

        case Intent.LOOKUP_FLIGHT:
            add("lookup", entity="flights", filters=_flight_filters(ents))

        case Intent.CHECK_GATE if (ents.flight_ids or ents.flight_nos
                                    or ents.primary_gate):
            at_utc = (f"{ents.primary_date}T{ents.times[0]}:00Z"
                      if ents.times and ents.primary_date else None)
            add("check_gate",
                flight_id=ents.flight_ids[0] if ents.flight_ids else None,
                flight_no=ents.flight_nos[0] if ents.flight_nos else None,
                date=ents.primary_date,
                boarding_gate_number=ents.primary_gate,
                delay_minutes=ents.delay_minutes or 0.0,
                at_utc=at_utc)
        case Intent.LOOKUP_CERT:
            add("lookup", entity="certifications", filters=_cert_filters(ents))

        case Intent.LOOKUP_RISK:
            add("lookup", entity="risk_signals",
                filters={"crew_id": ents.primary_crew} if ents.primary_crew else {})

        case Intent.LOOKUP_CREW if _crew_filters(ents):
            add("lookup", entity="crew", filters=_crew_filters(ents))

        case Intent.LOOKUP_ROSTER if ents.primary_pairing:
            add("lookup", entity="pairing_crew",
                filters={"pairing_id": ents.primary_pairing})

        case Intent.LOOKUP_ROSTER if ents.primary_crew:
            add("lookup", entity="pairing_crew", filters={"crew_id": ents.primary_crew})

        case Intent.DRAFT_NOTIFICATION if ents.primary_crew and ents.primary_pairing:
            add("notification_brief",
                crew_id=ents.primary_crew, pairing_id=ents.primary_pairing)

        case Intent.CHECK_LEGALITY if ents.primary_crew and ents.primary_pairing:
            add("check_legality",
                crew_id=ents.primary_crew, pairing_id=ents.primary_pairing)

        case Intent.CHECK_LEGALITY if ents.primary_crew and (
                ents.flight_ids or ents.flight_nos):
            # "Move C-2087 onto DX412" names a leg, not a pairing.
            add("check_legality", crew_id=ents.primary_crew,
                flight_id=ents.flight_ids[0] if ents.flight_ids else None,
                flight_no=ents.flight_nos[0] if ents.flight_nos else None,
                date=ents.primary_date)

        case (Intent.FIND_REPLACEMENT | Intent.RANK_OPTIONS) if ents.primary_pairing:
            add("find_options",
                pairing_id=ents.primary_pairing,
                role=ents.roles[0] if ents.roles else "Captain")

        case (Intent.FIND_REPLACEMENT | Intent.RANK_OPTIONS) if ents.primary_crew:
            # "C-1042 is sick" names a person, not a trip. The roster knows
            # which pairing they are on and in what role.
            add("find_options", crew_id=ents.primary_crew)
            add("ripple", event={"type": "SICK_CREW", "crew_id": ents.primary_crew})

        case (Intent.FIND_REPLACEMENT | Intent.RANK_OPTIONS) if ents.stations:
            # A disruption named by route rather than pairing.
            add("lookup", entity="flights", filters=_flight_filters(ents))

        case Intent.IMPACT_OF_EVENT if ents.primary_crew or ents.primary_pairing:
            add("ripple", event={
                "type": "SICK_CREW",
                "crew_id": ents.primary_crew,
                "pairing_id": ents.primary_pairing,
                "reported_utc": ents.primary_date,
            })

        case Intent.JOINT_PLAN if len(ents.pairing_ids) >= 2:
            add("joint_plan", events=[
                {"type": "SICK_CREW", "pairing_id": pid} for pid in ents.pairing_ids
            ])

    return calls


def followup_calls(route: Route, trace: list[TraceEntry]) -> list[ToolCall]:
    """Calls derivable from what the seeds already returned.

    A disruption named by route — "captain of BLR->BOM is out" — needs a leg
    identified before cover can be found. The seed does that lookup, and the
    flight id is then sitting in the trace; asking the model to carry it
    across is asking it to do bookkeeping it is bad at.
    """
    if route.intent not in (Intent.FIND_REPLACEMENT, Intent.RANK_OPTIONS):
        return []
    if any(e.tool == "find_options" for e in trace):
        return []

    flights = [
        row
        for entry in trace
        if entry.tool == "lookup" and isinstance(entry.result, list)
        for row in entry.result
        if isinstance(row, dict) and "flight_id" in row
    ]
    if not flights:
        return []

    role = route.entities.roles[0] if route.entities.roles else "Captain"
    return [ToolCall(
        id="followup-0",
        name="find_options",
        args={"flight_id": flights[0]["flight_id"], "role": role},
    )]


def _coerce(cls: Any, rows: Any) -> list[Any]:
    """Turn raw tool output into the typed objects the renderer expects.

    Unknown keys are dropped rather than raising, so an engine change that
    adds a field cannot break the renderer.
    """
    fields = {f.name for f in dataclass_fields(cls)}
    out = []
    for row in rows or []:
        if isinstance(row, cls):
            out.append(row)
        elif isinstance(row, dict):
            kwargs = {k: v for k, v in row.items() if k in fields}
            if cls is RuleVerdict and "status" in kwargs:
                kwargs["status"] = Verdict(str(kwargs["status"]))
            out.append(cls(**kwargs))
    return out


def build_answer(route: Route, trace: list[TraceEntry]) -> Any:
    """Fold tool results into the typed body for this tier.

    Structured object first; prose is rendered from it in `explainer.py`.
    """
    results = {e.tool: e.result for e in trace if e.result is not None}

    if route.intent is Intent.CHECK_GATE:
        # A single fact-check, not a listing: the seed's call is the one that
        # actually answers the literal question (its args come from the
        # parsed question), so it wins over anything the model re-ran later
        # with different arguments.
        first = next((e.result for e in trace
                     if e.tool == "check_gate" and e.result is not None), None)
        return LookupAnswer(rows=[first] if first else [])

    if route.intent is Intent.DRAFT_NOTIFICATION:
        import notify

        brief = results.get("notification_brief") or {}
        return NotificationAnswer(
            message=notify.render(brief) if brief else "", brief=brief)

    if route.tier is Tier.LOOKUP:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in trace:
            if entry.result is None:
                continue
            found = entry.result if isinstance(entry.result, list) else [entry.result]
            for row in found:
                if not isinstance(row, dict):
                    continue
                # Deduplicate across calls: a seeded lookup and the model's
                # own near-identical one both return the same rows.
                key = repr(sorted(row.items(), key=str))
                if key not in seen:
                    seen.add(key)
                    rows.append(row)
        return LookupAnswer(rows=rows)

    if route.tier is Tier.REPLACEMENT:
        found = results.get("find_options") or {}
        rippled = results.get("ripple") or {}
        # A legality verdict is a complete answer on its own — "does any rule
        # breach?" computes the right verdict and must not report nothing
        # just because find_options/ripple never ran.
        checked = results.get("check_legality") or {}
        rec = _coerce(Option, [found["recommended"]]) if found.get("recommended") else []
        return ReplacementAnswer(
            subject=checked.get("crew_id"),
            subject_name=checked.get("name") or "",
            subject_rank=checked.get("rank") or "",
            legal=checked.get("legal"),
            verdicts=_coerce(RuleVerdict, checked.get("verdicts")),
            recommended=rec[0] if rec else None,
            cancellation_multiple=found.get("cancellation_multiple", 0),
            next_tier_cost_inr=found.get("next_tier_cost_inr", 0),
            next_tier_premium_inr=found.get("next_tier_premium_inr", 0),
            equal_cost_alternatives=found.get("equal_cost_alternatives", 0),
            uncovered_flights=rippled.get("uncovered_flights", []),
            at_risk_flights=rippled.get("at_risk_flights", []),
            passengers_affected=rippled.get("passengers", 0),
            funnel=_coerce(FunnelStage, found.get("funnel")),
            options=_coerce(Option, found.get("options")),
            strategies=_coerce(Option, found.get("strategies")),
            near_misses=_coerce(Option, found.get("near_misses")),
            excluded=found.get("excluded", []),
        )

    found = results.get("find_options") or {}
    blast = (results.get("ripple") or {}).get("blast_radius")
    return ConsequenceAnswer(
        options=_coerce(Option, found.get("options")),
        strategies=_coerce(Option, found.get("strategies")),
        blast_radius=(_coerce(BlastRadius, [blast]) or [None])[0],
        world_diff=results.get("simulate"),
        joint_plan=results.get("joint_plan"),
    )


def rank_mismatch(query: str, port: Any) -> str | None:
    """A rank the query asserts that the roster contradicts.

    A controller typing "FO C-2087" when the roster says C-2087 is a Captain
    has either misremembered the seat or means a different person, and both
    change the answer. Accepting it silently is the failure; the roster
    knows, so it should say.
    """
    for crew_id, claimed in stated_ranks(query):
        try:
            rows = port.lookup("crew", {"crew_id": crew_id})
        except Exception:
            continue
        if not rows:
            continue
        actual = rows[0].get("rank")
        if actual and actual != claimed:
            return (f"{crew_id} is a {actual}, not a {claimed}. "
                    f"Did you mean a different crew member, or shall I "
                    f"proceed with {crew_id} as {actual}?")
    return None
