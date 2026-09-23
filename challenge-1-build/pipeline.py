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
from entities import stated_bases, stated_ranks, stated_rank_names, stated_ratings
from router import Route
from tools import crew_named
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

@dataclass(slots=True)
class ToolCall:
    """One requested tool invocation — from a seed, a follow-up, or the model."""

    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)


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


def seed_calls(route: Route, query: str = "") -> list[ToolCall]:
    """First tool calls implied by the entities, before the model is consulted.

    The router already extracted the ids deterministically, so for the common
    shapes we know the opening move. This saves a round-trip and guarantees
    the model sees real data before it says anything.

    `query` is the controller's raw text -- only needed for EXPLAIN_RULE's
    no-rule-id fallback (search_rules() has no id to work from, only the
    question itself). Every other case here works from `route.entities`
    alone, same as before this parameter existed.
    """
    ents = route.entities
    calls: list[ToolCall] = []

    def add(name: str, **args: Any) -> None:
        calls.append(ToolCall(id=f"seed-{len(calls)}", name=name, args=args))

    match route.intent:
        case Intent.EXPLAIN_RULE if ents.rule_ids:
            for rule_id in ents.rule_ids:
                add("explain_rule", rule_id=rule_id)

        case Intent.EXPLAIN_RULE if query:
            # No rule id named -- a paraphrase ("can duty run long on a
            # short day"). search_rules() is a real hybrid-search tool
            # call, not a guess: it returns [] (not a wrong answer) if the
            # ledger or embedding model isn't configured, same discipline
            # as every other Postgres-backed feature in this repo.
            add("search_rules", query=query)

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

        case Intent.CHECK_GATE:
            # No flight or gate named -- an aggregate question ("how many
            # boarding gates are there", or, with a date, "how many were
            # occupied on 16 Sep"). Any station or date the question DID
            # name still scopes the aggregate -- "how many gates does
            # Bangalore occupy" silently answering for every station would
            # be a different, wrong-looking number. check_gate() returns
            # real counts from the dataset either way (see
            # core_engine/port.py), so this still answers from real data
            # instead of falling through to explain_no_tools()'s "I need a
            # flight or gate" decline for a question that was never about
            # one specific gate.
            add("check_gate",
                station=ents.stations[0] if ents.stations else None,
                date=ents.primary_date)

        # Deliberately no seed here for Intent.LOOKUP_CONTROLLERS: whether
        # the question needs the roster (list_controllers) or the workload
        # (controller_issue_counts) is a real choice between two tools with
        # different meanings, not a fact the entities already pinned down --
        # exactly the kind of decision the Resolution Advisor Agent's own
        # tool loop exists to make, not something to pre-empt with a second
        # keyword regex here.

        case Intent.LOOKUP_CERT:
            add("lookup", entity="certifications", filters=_cert_filters(ents))

        case Intent.LOOKUP_RISK:
            add("lookup", entity="risk_signals",
                filters={"crew_id": ents.primary_crew} if ents.primary_crew else {})

        case Intent.LOOKUP_CREW if _crew_filters(ents):
            add("lookup", entity="crew", filters=_crew_filters(ents))

        case Intent.LOOKUP_CREW if ents.malformed_crew_ids:
            # "C-10" isn't a real id shape -- nothing to look up, but real
            # neighbours are, so the answer can offer them instead of
            # reading as though nothing was asked at all.
            add("suggest_crew_ids", near=ents.malformed_crew_ids[0])

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
        # with different arguments -- including when the seed's call is the
        # one that *failed* (e.g. a date outside the dataset). Skipping past
        # that error to any later, differently-scoped success would present
        # an answer to a question nobody asked as though it answered the one
        # that was asked; an empty answer here instead falls through to
        # `render_unavailable()`, which surfaces the seed call's real error.
        first_entry = next((e for e in trace if e.tool == "check_gate"), None)
        first = first_entry.result if first_entry and not first_entry.error else None
        return LookupAnswer(rows=[first] if first else [])

    if route.intent is Intent.DRAFT_NOTIFICATION:
        import notify

        brief = results.get("notification_brief") or {}
        return NotificationAnswer(
            message=notify.render(brief) if brief else "", brief=brief)

    if "same_pairing" in results:
        # A verdict, not a listing -- same failure mode as CHECK_GATE above.
        # The model often runs an exploratory `lookup` (e.g. "list all
        # Captains") before or after the decisive call, and folding both
        # into one rows list risks the actual verdict getting pushed past
        # ROW_LIMIT by an unrelated large table and silently truncated out
        # of what the Explainer ever sees.
        return LookupAnswer(rows=[results["same_pairing"]])

    if "suggest_crew_ids" in results:
        # A "did you mean" list, not a set of matches -- none of these rows
        # answer the question that named the malformed id, they're
        # candidates for the controller to pick from. A bare table of
        # real-looking crew rows with nothing saying so reads as "here are
        # the matches" to the Explainer, which then (correctly, given what
        # it was shown) summarizes it as "the id isn't in the data" -- true,
        # but throwing away the one useful thing this tool call was for.
        malformed = route.entities.malformed_crew_ids
        return LookupAnswer(rows=[{
            "requested_id": malformed[0] if malformed else None,
            "exists_in_dataset": False,
            "nearest_real_crew_ids": results["suggest_crew_ids"] or [],
        }])

    if "search_rules" in results:
        # A paraphrased rule question can land on almost any intent
        # depending on its wording ("how long do crew have to rest between
        # duties" reads as CHECK_LEGALITY to the router's own regex, not
        # EXPLAIN_RULE), so the tier-specific branches below -- keyed to
        # what a *legality* or *lookup* question needs -- never think to
        # look for this tool's result at all, even though it ran and
        # succeeded. Surfaced here unconditionally so a real answer never
        # gets silently dropped just because the intent guess was off.
        return LookupAnswer(rows=results["search_rules"] or [])

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


def stated_attribute_mismatch(query: str, port: Any) -> str | None:
    """An attribute the query asserts about a named crew member that the
    roster contradicts -- rank, base, or aircraft rating.

    A controller typing "FO C-2087" when the roster says C-2087 is a
    Captain, or "the DEL-based C-1042" when C-1042 is BLR-based, has either
    misremembered or means a different person, and both change the answer.
    Accepting it silently is the failure; the roster knows, so it should
    say -- the same check, just not limited to rank, the one attribute this
    used to be scoped to (the function was named `rank_mismatch`).
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

    for name, claimed in stated_rank_names(query):
        try:
            matches = crew_named(port, name)
        except Exception:
            continue
        if not matches or any(m.get("rank") == claimed for m in matches):
            # No one by that name at all is a different tool's problem to
            # report; at least one real match holding the claimed rank means
            # this isn't a mismatch, even if a same-named person elsewhere
            # holds a different one.
            continue
        candidates = ", ".join(f"{m['crew_id']} ({m.get('rank')})" for m in matches)
        return (f"{name} is not a {claimed} in the roster -- {candidates}. "
                f"Did you mean one of those, under their real rank, or a "
                f"different person?")

    for crew_id, claimed in stated_bases(query):
        try:
            rows = port.lookup("crew", {"crew_id": crew_id})
        except Exception:
            continue
        if not rows:
            continue
        actual = rows[0].get("base")
        if actual and actual != claimed:
            return (f"{crew_id} is based at {actual}, not {claimed}. "
                    f"Did you mean a different crew member, or shall I "
                    f"proceed with {crew_id} as {actual}-based?")

    for crew_id, claimed in stated_ratings(query):
        try:
            rows = port.lookup("crew", {"crew_id": crew_id})
        except Exception:
            continue
        if not rows:
            continue
        ratings = rows[0].get("ratings") or []
        if ratings and claimed not in ratings:
            have = "/".join(ratings)
            return (f"{crew_id} is rated on {have}, not {claimed}. "
                    f"Did you mean a different crew member, or shall I "
                    f"proceed with {crew_id} as {have}-rated?")
    return None
