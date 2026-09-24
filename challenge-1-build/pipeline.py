"""Planning and answer-assembly — the deterministic glue between the Router,
the Resolution Advisor Agent's tool loop, and the Explainer Agent.

    ROUTER -> PLANNER (this file) -> TOOL LOOP -> VERIFIER -> EXPLAINER

None of this is a model call. `seed_calls()` and `followup_calls()` turn the
entities the router already extracted into the tool calls a controller's
question obviously needs, before the Resolution Advisor Agent is even
consulted. That saves a round trip and guarantees the model sees real data
before it says anything. `build_answer()` folds the resulting trace into the
typed answer object that `explainer.render()` turns into prose.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from datetime import date, timedelta
from typing import Any

import config
from entities import (
    extract, stated_bases, stated_pairing_dates, stated_pairing_days,
    stated_ranks, stated_rank_names, stated_ratings,
)
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
    # An unfiltered crew lookup returns 150 rows, which doesn't answer
    # anything a controller actually asked.
    Intent.LOOKUP_CREW: ("something to narrow by — a crew id, a rank, a base, "
                         "or an aircraft rating"),
}


def explain_no_tools(route: Route) -> str:
    """Why nothing ran — never an empty answer.

    An empty trace with an empty answer object would read to a controller as
    "no data was returned", which is indistinguishable from "nothing is
    wrong" — the one outcome this system must never produce. So an empty
    trace instead reports what was understood, what is missing, and what
    would unblock it.
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

    "BLR->BOM" names two stations. Filtering on the first alone would return
    every departure from BLR and silently drop half the question.
    """
    filters: dict[str, Any] = {}
    if ents.stations:
        filters["dep_station"] = ents.stations[0]
    if len(ents.stations) > 1:
        filters["arr_station"] = ents.stations[1]
    if ents.date_range:
        filters["date"] = ents.date_range
    elif ents.primary_date:
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

    An expiry question is about a date range, not a single point. Computing
    that range here keeps the selection below the trust boundary — the
    alternative is handing the model 600 rows and asking it to pick the
    right ones, which is exactly the arithmetic it must never do.
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
    question shapes we already know the opening move. This saves a
    round-trip and guarantees the model sees real data before it says
    anything.

    `query` is the controller's raw text. It's only needed for EXPLAIN_RULE's
    no-rule-id fallback, since search_rules() has no id to work from, just
    the question itself. Every other case here works from `route.entities`
    alone, same as before this parameter existed.
    """
    ents = route.entities
    calls: list[ToolCall] = []

    def add(name: str, **args: Any) -> None:
        calls.append(ToolCall(id=f"seed-{len(calls)}", name=name, args=args))

    # Malformed pairing/flight ids are a cross-cutting concern -- they can
    # show up under any intent, not just one -- so they're checked here,
    # before the intent-specific cases below, the same way a malformed crew
    # id seeds `suggest_crew_ids` regardless of what else the question asks.
    if ents.malformed_pairing_ids:
        add("suggest_pairing_ids", near=ents.malformed_pairing_ids[0])
    if ents.malformed_flight_nos:
        add("suggest_flight_nos", near=ents.malformed_flight_nos[0])

    match route.intent:
        case Intent.EXPLAIN_RULE if ents.rule_ids:
            for rule_id in ents.rule_ids:
                add("explain_rule", rule_id=rule_id)

        case Intent.EXPLAIN_RULE if query:
            # No rule id named — a paraphrase ("can duty run long on a
            # short day"). search_rules() is a real hybrid-search tool call,
            # not a guess: it returns [] (not a wrong answer) if the ledger
            # or embedding model isn't configured, same as every other
            # Postgres-backed feature in this repo.
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
            # No flight or gate named — an aggregate question ("how many
            # boarding gates are there", or, with a date, "how many were
            # occupied on 16 Sep"). Any station or date the question DID
            # name still scopes the aggregate; answering "how many gates
            # does Bangalore occupy" for every station would give a
            # different, wrong-looking number. check_gate() returns real
            # counts from the dataset either way (see core_engine/port.py),
            # so this still answers from real data instead of falling
            # through to explain_no_tools()'s "I need a flight or gate"
            # decline — which would be wrong for a question that was never
            # about one specific gate.
            add("check_gate",
                station=ents.stations[0] if ents.stations else None,
                date=ents.primary_date)

        # Deliberately no seed here for Intent.LOOKUP_CONTROLLERS. Whether
        # the question needs the roster (list_controllers) or the workload
        # (controller_issue_counts) is a real choice between two tools with
        # different meanings, not something the entities already pinned
        # down. That's exactly the kind of decision the Resolution Advisor
        # Agent's own tool loop exists to make, not something to pre-empt
        # with a second keyword regex here.

        case Intent.LOOKUP_CERT:
            add("lookup", entity="certifications", filters=_cert_filters(ents))

        case Intent.LOOKUP_RISK:
            add("lookup", entity="risk_signals",
                filters={"crew_id": ents.primary_crew} if ents.primary_crew else {})

        case Intent.LOOKUP_CREW if _crew_filters(ents):
            add("lookup", entity="crew", filters=_crew_filters(ents))

        case Intent.LOOKUP_CREW if ents.malformed_crew_ids:
            # "C-10" isn't a real id shape, so there's nothing to look up —
            # but real neighbours are, so we can offer them instead of
            # acting as though nothing was asked at all.
            add("suggest_crew_ids", near=ents.malformed_crew_ids[0])

        case Intent.LOOKUP_ROSTER if ents.primary_pairing:
            add("lookup", entity="pairing_crew",
                filters={"pairing_id": ents.primary_pairing})

        case Intent.LOOKUP_ROSTER if ents.primary_crew:
            add("lookup", entity="pairing_crew", filters={"crew_id": ents.primary_crew})

        case Intent.LOOKUP_ROSTER if ents.aircraft and ents.primary_date:
            # "the Senior Cabin Crew on VT-DXB's pairing on 2026-09-16"
            # names an aircraft and a date, not a pairing or crew id.
            # lookup()'s own aircraft+date resolution (core_engine.port)
            # finds the right pairing; passing role too (when the
            # question names one) narrows straight to the answer instead
            # of the model getting back a whole crew list and having to
            # pick the right row out of it itself.
            filters: dict[str, Any] = {
                "aircraft": ents.aircraft[0], "date": ents.primary_date,
            }
            if ents.roles:
                filters["role"] = ents.roles[0]
            add("lookup", entity="pairing_crew", filters=filters)

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
    flight id then sits in the trace. Asking the model to carry it across
    would ask it to do bookkeeping it's bad at.
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
        # A single fact-check, not a listing. The seed's call is the one
        # that actually answers the literal question (its args come from
        # the parsed question), so it wins over anything the model re-ran
        # later for a DIFFERENT flight — including when the seed's call is
        # the one that *failed* (e.g. a date outside the dataset). Skipping
        # past that error to a later, differently-scoped success would
        # present an answer to a question nobody asked as if it answered the
        # real one; an empty answer here instead falls through to
        # `render_unavailable()`, which surfaces the seed call's real error.
        #
        # But a later call for the SAME flight is a refinement, not a
        # different question. The seed's own parse of "30 minutes late"
        # often can't extract delay_minutes, so its baseline probe comes
        # back delay_minutes=0 (no conflict, by construction), while the
        # model's own follow-up adds the delay the question was actually
        # about. That refinement must win, or the real question never gets
        # answered even though the right tool call is sitting right there.
        gate_entries = [e for e in trace if e.tool == "check_gate"]
        first_entry = gate_entries[0] if gate_entries else None
        chosen = first_entry
        if first_entry and not first_entry.error:
            seed_flight = first_entry.result.get("flight_id")
            same_flight = [
                e for e in gate_entries
                if seed_flight and not e.error and e.result
                and e.result.get("flight_id") == seed_flight
            ]
            if same_flight:
                chosen = same_flight[-1]
            elif len(gate_entries) > 1 and gate_entries[-1].error:
                # The seed's own aggregate succeeded, but the model tried
                # again with something more specific (an instant, a gate)
                # that the seed's parse never captured — and THAT attempt is
                # what failed. Keeping the seed's coarser answer here would
                # hide the real problem (e.g. a malformed time) behind an
                # answer to an easier question than the one actually asked.
                # An empty answer instead falls through to
                # `render_unavailable()`, surfacing that real error.
                chosen = gate_entries[-1]
        first = chosen.result if chosen and not chosen.error else None
        return LookupAnswer(rows=[first] if first else [])

    if route.intent is Intent.DRAFT_NOTIFICATION:
        import notify

        brief = results.get("notification_brief") or {}
        return NotificationAnswer(
            message=notify.render(brief) if brief else "", brief=brief)

    if "same_pairing" in results:
        # A verdict, not a listing — same failure mode as CHECK_GATE above.
        # The model often runs an exploratory `lookup` (e.g. "list all
        # Captains") before or after the decisive call. Folding both into
        # one rows list risks the actual verdict getting pushed past
        # ROW_LIMIT by an unrelated large table and silently truncated out
        # of what the Explainer ever sees.
        return LookupAnswer(rows=[results["same_pairing"]])

    if "suggest_crew_ids" in results:
        # A "did you mean" list, not a set of matches. None of these rows
        # answer the question that named the malformed id — they're
        # candidates for the controller to pick from. A bare table of
        # real-looking crew rows with nothing saying so would read as "here
        # are the matches" to the Explainer, which would then (correctly,
        # given what it was shown) summarize it as "the id isn't in the
        # data" — true, but throwing away the one useful thing this tool
        # call was for.
        malformed = route.entities.malformed_crew_ids
        return LookupAnswer(rows=[{
            "requested_id": malformed[0] if malformed else None,
            "exists_in_dataset": False,
            "nearest_real_crew_ids": results["suggest_crew_ids"] or [],
        }])

    if "suggest_pairing_ids" in results:
        # Same reasoning as `suggest_crew_ids` above, for a malformed
        # pairing id instead of a crew id.
        malformed = route.entities.malformed_pairing_ids
        return LookupAnswer(rows=[{
            "requested_id": malformed[0] if malformed else None,
            "exists_in_dataset": False,
            "nearest_real_pairing_ids": results["suggest_pairing_ids"] or [],
        }])

    if "suggest_flight_nos" in results:
        # Same reasoning again, for a malformed flight number.
        malformed = route.entities.malformed_flight_nos
        return LookupAnswer(rows=[{
            "requested_flight_no": malformed[0] if malformed else None,
            "exists_in_dataset": False,
            "nearest_real_flight_nos": results["suggest_flight_nos"] or [],
        }])

    if "search_rules" in results:
        # A paraphrased rule question can land on almost any intent
        # depending on its wording ("how long do crew have to rest between
        # duties" reads as CHECK_LEGALITY to the router's own regex, not
        # EXPLAIN_RULE). The tier-specific branches below, keyed to what a
        # *legality* or *lookup* question needs, would never think to look
        # for this tool's result at all, even though it ran and succeeded.
        # Surfaced here unconditionally so a real answer never gets silently
        # dropped just because the intent guess was off.
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
        # A legality verdict is a complete answer on its own. "Does any rule
        # breach?" computes the right verdict, and must not report nothing
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
    """Something the query states that isn't true, checked before any
    tool runs -- a crew member's rank/base/rating, a pairing's real dates,
    or (see the bottom) a station code that doesn't exist.

    A controller typing "FO C-2087" when the roster says C-2087 is a
    Captain, or "the DEL-based C-1042" when C-1042 is BLR-based, has either
    misremembered or means a different person — either way it changes the
    answer. Silently accepting it would be the failure; the roster knows,
    so it should say. This is the same check as before, just no longer
    limited to rank, the one attribute it used to be scoped to (the
    function was named `rank_mismatch`).
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
            # report. If at least one real match holds the claimed rank,
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

    for pairing_id, claimed_date in stated_pairing_dates(query):
        try:
            rows = port.lookup("pairing_days", {"pairing_id": pairing_id})
        except Exception:
            continue
        if not rows:
            continue
        if not any(r["date"] == claimed_date for r in rows):
            real_dates = ", ".join(sorted(r["date"] for r in rows))
            return (f"{pairing_id} doesn't run on {claimed_date} -- it runs "
                    f"{real_dates}. Did you mean one of those, or a "
                    f"different pairing?")

    for pairing_id, claimed in stated_pairing_days(query):
        try:
            rows = port.lookup("pairing_days", {"pairing_id": pairing_id})
        except Exception:
            continue
        if not rows:
            continue
        actual = len(rows)
        if actual != claimed:
            return (f"{pairing_id} is a {actual}-day pairing, not "
                    f"{claimed}-day -- it runs "
                    f"{', '.join(sorted(r['date'] for r in rows))}. "
                    f"Shall I proceed on that basis?")

    if malformed := extract(query).malformed_stations:
        token = malformed[0]
        suggestion = difflib.get_close_matches(token, config.STATIONS, n=1)
        if suggestion:
            return (f"{token!r} isn't a real station -- did you mean "
                    f"{suggestion[0]}? Confirm which, or rephrase without it.")
        return (f"{token!r} isn't a real station (real ones: "
                f"{', '.join(sorted(config.STATIONS))}). Confirm what you "
                f"meant, or rephrase without it.")

    return None
