"""The tool boundary — where the language model stops and Python starts.

This is the trust boundary. Everything below it is deterministic; the
Resolution Advisor Agent above it may only *choose* tools and *narrate* what
they return. It never computes a duty hour or a cost itself.

Tools are coarse and semantically meaningful rather than micro-CRUD, so a
tier-2 question takes three calls instead of thirty. There are fifteen of
them — `TOOL_SCHEMAS` below is the exact list, in vendor-neutral JSON Schema.
`foundry_tools()` adapts that list into `azure.ai.projects.models.FunctionTool`
objects for `PromptAgentDefinition(tools=...)`.

`JsonToolPort` (in `core_engine/port.py`) is the one implementation. It
satisfies `ToolPort` by computing answers from the vendored dataset in
`data/`, instead of replaying fixtures, so it works for any pairing, not
just the ones with a published answer key.
"""

from __future__ import annotations

import re
import time
from typing import Any, Protocol, runtime_checkable

from schemas import TraceEntry

# --------------------------------------------------------------------------
# Tool schemas — vendor-neutral JSON Schema, adapted per-backend below
# --------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "lookup",
        "description": (
            "Retrieve rows from the operational dataset. The tier-1 workhorse: "
            "crew, flights, pairings, reserves, certifications, risk signals. "
            "For 'how many X are Y' or 'how many of those are Z' -- filter on "
            "every dimension in ONE call (they combine as AND) and take "
            "len(rows) of the result. Do not filter on only some dimensions "
            "and then count the rest by eye across the returned rows; that is "
            "where a real count like 'ATR-rated captains at BLR' goes wrong."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {
                    "type": "string",
                    "enum": [
                        "crew", "flights", "pairings", "pairing_crew",
                        "pairing_days", "reserves", "certifications",
                        "risk_signals", "duty_clocks", "costs",
                    ],
                },
                "filters": {
                    "type": "object",
                    "description": (
                        "Field filters, e.g. {'base': 'BLR', 'rank': 'Captain'}. "
                        "A list-valued field (e.g. crew.ratings) matches by "
                        "containment -- {'ratings': 'ATR72'} finds every "
                        "captain who HAS that rating among possibly several, "
                        "not just crew whose ratings list equals it exactly. "
                        "For entity pairing_crew/pairing_days/pairings, "
                        "'aircraft'+'date' together (e.g. 'the Senior Cabin "
                        "Crew on VT-DXB's pairing on 2026-09-16') resolve to "
                        "the right pairing_id for you -- neither entity has "
                        "an aircraft or date field to filter on directly, so "
                        "pass aircraft+date as filters rather than first "
                        "calling lookup(pairings, {aircraft}) and inspecting "
                        "the nested days yourself. Dates are ISO-8601. A "
                        "value may instead be a range, "
                        "e.g. {'valid_to': {'gte': '2026-09-15', "
                        "'lte': '2026-10-15'}} for 'expiring within 30 days of "
                        "15 Sep'. Operators: gte, lte, gt, lt."
                    ),
                },
                "sort_by": {
                    "type": "string",
                    "description": (
                        "For 'the fastest/highest/lowest X' or 'top N by X' "
                        "questions: the field to sort by (e.g. "
                        "'reachability_minutes', 'disruption_risk_score'). "
                        "Sorts and ties every row server-side -- use this "
                        "instead of eyeballing a min/max across a long raw "
                        "result yourself, which is where ties get missed and "
                        "the wrong row gets picked."
                    ),
                },
                "sort_desc": {
                    "type": "boolean",
                    "description": "True for highest-first; omit/false for lowest-first.",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Cap the sorted rows returned, e.g. 'top 5'. Omit to "
                        "see every row -- needed to catch a tie at the "
                        "boundary you'd otherwise cut off."
                    ),
                },
                "group_by": {
                    "type": ["string", "array"],
                    "description": (
                        "For 'headcount by rank and base' or any 'how many "
                        "per X' question: one field name, or a list for "
                        "several (e.g. ['rank', 'base']). Returns one row per "
                        "distinct combination with a 'count' field already "
                        "computed -- use this instead of fetching every row "
                        "and counting by eye, which is where a real count "
                        "goes wrong across more than a handful of rows."
                    ),
                },
            },
            "required": ["entity"],
        },
    },
    {
        "name": "notification_brief",
        "description": (
            "Every fact a callout message needs for one crew member on one "
            "pairing: report times and stations per day, the legs in order, "
            "overnight stations, and an acknowledgement deadline derived from "
            "the crew member's own reachability. Read from the roster — call "
            "this before drafting a notification rather than writing times "
            "from memory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "crew_id": {"type": "string"},
                "pairing_id": {"type": "string"},
            },
            "required": ["crew_id", "pairing_id"],
        },
    },
    {
        "name": "duty_clock",
        "description": (
            "A crew member's accrued duty and block hours with headroom under "
            "RULE-DUTY-02 and RULE-FLT-03. Returns both duty_hours_this_date "
            "(that single day's own hours -- use this for 'what were X's duty "
            "hours on <date>') and duty_hours_7d (the rolling 7-day window "
            "ending on date -- a different, usually larger, number). Windows "
            "are calendar-day based."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "crew_id": {"type": "string", "pattern": "^C-[0-9]{4}$"},
                "date": {"type": "string", "description": "ISO date; defaults to snapshot"},
            },
            "required": ["crew_id"],
        },
    },
    {
        "name": "check_legality",
        "description": (
            "Evaluate all 7 rules for assigning a crew member to a pairing, "
            "named directly, via a flight on it, or via 'their rostered "
            "duty on <aircraft> on <date>' (pass aircraft+date, resolved "
            "against the crew member's own roster -- do not first look up "
            "which of their pairings uses that aircraft yourself, this "
            "does it correctly, including picking the right one by date). "
            "Returns a verdict per rule with the numbers, never a bare boolean. "
            "This is the only legal authority in the system."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "crew_id": {"type": "string", "pattern": "^C-[0-9]{4}$"},
                "pairing_id": {"type": "string", "pattern": "^P-[0-9]{4}$"},
                "flight_id": {
                    "type": "string",
                    "pattern": "^DX[0-9]{3}-[0-9]{4}-[0-9]{2}-[0-9]{2}$",
                    "description": "Alternative to pairing_id; its pairing is resolved.",
                },
                "flight_no": {
                    "type": "string", "pattern": "^DX[0-9]{3}$",
                    "description": (
                        "A flight number without a date is ambiguous — a "
                        "number can fly on three different days — so pass "
                        "`date` alongside it."
                    ),
                },
                "aircraft": {
                    "type": "string", "pattern": "^VT-DX[A-F]$",
                    "description": (
                        "An aircraft tail instead of a flight or pairing -- "
                        "pass `date` alongside it, e.g. 'C-5417's rostered "
                        "VT-DXB duty on 19 Sep' -> aircraft='VT-DXB', "
                        "date='2026-09-19'."
                    ),
                },
                "date": {"type": "string", "description": "ISO date, with flight_no or aircraft"},
                "delay_h": {
                    "type": "number",
                    "description": "Hypothetical departure delay, for near-miss checks",
                },
            },
            "required": ["crew_id"],
        },
    },
    {
        "name": "find_options",
        "description": (
            "Enumerate and rank every way to cover an uncrewed pairing: reserve "
            "callout, day-off callout, deadhead positioning, delay, cancel. "
            "Returns the candidate funnel with a reason for every drop. "
            "Identify by pairing_id, or by flight_id if you only know the leg. "
            "Never construct an id from a route like 'BLR->BOM' — look it up."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pairing_id": {"type": "string", "pattern": "^P-[0-9]{4}$"},
                "flight_id": {
                    "type": "string",
                    "pattern": "^DX[0-9]{3}-[0-9]{4}-[0-9]{2}-[0-9]{2}$",
                    "description": (
                        "Alternative to pairing_id when the disruption is named "
                        "by flight. The pairing is resolved for you."
                    ),
                },
                "crew_id": {
                    "type": "string", "pattern": "^C-[0-9]{4}$",
                    "description": (
                        "The crew member who is unavailable. Their pairing AND "
                        "role are resolved from the roster, so neither has to "
                        "be supplied or guessed."
                    ),
                },
                "flight_no": {
                    "type": "string", "pattern": "^DX[0-9]{3}$",
                    "description": (
                        "A flight named by number alone — 'a pilot for DX401'. "
                        "Pass `date` with it where you have one: a number can "
                        "fly on several days, and you will be asked which."
                    ),
                },
                "date": {"type": "string", "description": "ISO date, with flight_no"},
                "role": {
                    "type": "string",
                    "enum": ["Captain", "First Officer", "Senior Cabin Crew", "Cabin Crew"],
                    "description": "Inferred from crew_id when that is given.",
                },
                "callout_utc": {"type": "string", "description": "ISO-8601 UTC"},
            },
            "required": [],
        },
    },
    {
        "name": "ripple",
        "description": (
            "Blast radius of a disruption: directly uncovered flights, orphaned "
            "downstream pairing days, passengers affected, reserve pool "
            "depletion, aircraft rotation knock-on. Name the disruption however "
            "the controller actually said it -- this resolves it for you: "
            "pairing_id or crew_id directly; flight_id, or flight_no+date "
            "('what if we cancel DX404 on 16 Sep'); station+from_utc/to_utc "
            "for a closure window ('BLR shuts 08:00-14:00Z on 17 Sep'); or "
            "aircraft (tail id), optionally with a date or from_utc/to_utc "
            "window ('if VT-DXC goes tech on 16 Sep' -- date scopes it to "
            "that day; omit both for the whole week, e.g. 'the whole VT-DXE "
            "line is grounded for the week'). station and aircraft both cover "
            "every pairing that touches them in that window and combine their "
            "blast radius, not just one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event": {
                    "type": "object",
                    "description": (
                        "e.g. {'type':'SICK_CREW','crew_id':'C-1042',"
                        "'pairing_id':'P-2291'}, or {'type':'TECH_DELAY',"
                        "'flight_no':'DX404','date':'2026-09-16'}, or "
                        "{'type':'STATION_CLOSURE','station':'BLR',"
                        "'from_utc':'2026-09-17T08:00:00Z','to_utc':"
                        "'2026-09-17T14:00:00Z'}, or {'type':'TECH_EVENT',"
                        "'aircraft':'VT-DXC','date':'2026-09-16'}, or "
                        "{'type':'AOG','aircraft':'VT-DXE'} for the whole "
                        "week. Pass whichever of these you actually have -- "
                        "never a placeholder id."
                    ),
                }
            },
            "required": ["event"],
        },
    },
    {
        "name": "simulate",
        "description": (
            "Fork the world, apply a perturbation, re-evaluate, and return the "
            "diff: every crew member on the pairing whose legal/illegal status "
            "flips. event.pairing_id must be a real pairing id -- if the "
            "question named a flight or aircraft instead, resolve it with "
            "`lookup` first (flight -> its pairing, or aircraft + date -> "
            "pairing). A delay ('X minutes/hours late') goes in "
            "event.delay_hours as a float number of HOURS -- not 'delay', "
            "and convert minutes yourself (90 minutes late is delay_hours: "
            "1.5). e.g. {'type':'TECH_DELAY','pairing_id':'P-2203',"
            "'delay_hours':1.5}. Never put a placeholder in event."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"event": {"type": "object"}},
            "required": ["event"],
        },
    },
    {
        "name": "joint_plan",
        "description": (
            "Cost-minimal assignment across several simultaneous disruptions, "
            "under the constraint that one crew member cannot cover two "
            "pairings. Note that ties are common and all are equally correct. "
            "Every event needs a real pairing_id -- 'both A320 captains "
            "(VT-DXA and VT-DXB) are sick' names aircraft, not pairings, so "
            "resolve each one with `lookup` (e.g. entity=pairings, "
            "filters={'aircraft': 'VT-DXA'}, plus the date) before calling "
            "this. Never put a placeholder in events."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "events": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["events"],
        },
    },
    {
        "name": "same_pairing",
        "description": (
            "Whether two crew members are rostered on the same pairing this "
            "week. Name each one by crew id, or by name -- both are resolved "
            "and both pairing assignments are compared for you. This is the "
            "right tool for 'is Captain X paired with First Officer Y' and "
            "any other question comparing two named crew members' rosters; "
            "do not try to answer it from two separate `lookup` calls. "
            "Pass each name exactly as the question stated it, including any "
            "rank word ('Captain A. Nair', not just 'A. Nair') -- several "
            "surnames repeat across ranks, and the rank the question already "
            "gave you is what tells them apart. If it still comes back "
            "NEEDS_CONFIRMATION, that means the question's own wording "
            "wasn't enough to disambiguate either -- report the choices "
            "given, don't guess one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "a": {"type": "string", "description": "First crew member: crew id, or '<rank> <name>' / name."},
                "b": {"type": "string", "description": "Second crew member: crew id, or '<rank> <name>' / name."},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "check_gate",
        "description": (
            "Verify a claim about a flight's boarding gate against the "
            "boarding-gate dataset, or inspect gate occupancy directly. "
            "Name the flight by flight_id, or flight_no+date (a wrong date is "
            "reported as such, naming the dates it actually operates). Pass "
            "boarding_gate_number to check it against the actual assignment, "
            "or by itself (no flight) with at_utc to ask who currently holds "
            "that gate. Pass delay_minutes to test whether delaying this "
            "flight's departure would collide with whatever is booked into "
            "the same gate afterwards. Returns the actual assignment and "
            "window, never a bare boolean. Called with no flight/gate named, "
            "returns aggregate counts instead: with no date, the static "
            "inventory (total boarding gates, how many per station) for "
            "questions like 'how many boarding gates are there'; with a "
            "date, how many of those gates actually had a flight boarding "
            "that day -- a different, usually smaller number than the "
            "inventory. Pass station (e.g. 'BLR') to scope either aggregate "
            "to one station instead of all of them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "flight_id": {
                    "type": "string",
                    "pattern": "^DX[0-9]{3}-[0-9]{4}-[0-9]{2}-[0-9]{2}$",
                },
                "flight_no": {
                    "type": "string", "pattern": "^DX[0-9]{3}$",
                    "description": "Pass `date` alongside it — a flight number alone is ambiguous.",
                },
                "date": {
                    "type": "string",
                    "description": ("ISO date. With flight_no, which day's flight to check. "
                                     "With no flight/gate at all, which day's occupancy to "
                                     "count instead of the static gate inventory."),
                },
                "station": {
                    "type": "string",
                    "description": "e.g. 'BLR' — scope the aggregate counts to one station.",
                },
                "boarding_gate_number": {
                    "type": "string",
                    "description": "e.g. 'BLR-G1', as claimed by the controller.",
                },
                "delay_minutes": {
                    "type": "number",
                    "description": "Hypothetical departure delay, for gate-conflict checks",
                },
                "at_utc": {
                    "type": "string",
                    "description": "ISO-8601 UTC instant, for an occupancy-only query",
                },
            },
            "required": [],
        },
    },
    {
        "name": "explain_rule",
        "description": "The text, parameters and plain-English gloss of one rule.",
        "input_schema": {
            "type": "object",
            "properties": {
                "rule_id": {"type": "string", "pattern": "^RULE-[A-Z]{3,4}-[0-9]{2}$"}
            },
            "required": ["rule_id"],
        },
    },
    {
        "name": "search_rules",
        "description": (
            "Find the rule(s) closest to a paraphrased legality question that "
            "names no rule id -- e.g. 'can duty run long on a short day'. "
            "Call explain_rule instead whenever a rule id (RULE-XXX-00) is "
            "already known; this is only for when one isn't. Each candidate "
            "carries its own blended_score in [0, 1] (BM25 keyword rank "
            "blended with semantic similarity) -- below 0.65 the match is "
            "weak, so say so rather than treating the top result as certain."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The controller's own question, verbatim."},
                "top_k": {"type": "integer", "description": "How many candidates to return (default 3)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "suggest_crew_ids",
        "description": (
            "Real crew ids closest to one that doesn't match the dataset's "
            "C-#### shape (e.g. 'C-10') -- for offering the controller real "
            "alternatives to pick from. Deterministic digit-prefix matching "
            "against the actual roster, not a guess: never treat a "
            "suggestion as the crew member actually meant, only list them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "near": {"type": "string", "description": "The malformed id as typed, e.g. 'C-10'."},
                "limit": {"type": "integer", "description": "How many candidates to return (default 3)."},
            },
            "required": ["near"],
        },
    },
    {
        "name": "suggest_pairing_ids",
        "description": (
            "Real pairing ids closest to one that doesn't match the "
            "dataset's P-#### shape (e.g. 'P-22') -- for offering the "
            "controller real alternatives to pick from. Deterministic "
            "digit-prefix matching against the actual roster, not a guess: "
            "never treat a suggestion as the pairing actually meant, only "
            "list them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "near": {"type": "string", "description": "The malformed id as typed, e.g. 'P-22'."},
                "limit": {"type": "integer", "description": "How many candidates to return (default 3)."},
            },
            "required": ["near"],
        },
    },
    {
        "name": "suggest_flight_nos",
        "description": (
            "Real flight numbers closest to one that doesn't match the "
            "dataset's DX### shape (e.g. 'DX9999') -- for offering the "
            "controller real alternatives to pick from. Deterministic "
            "digit-prefix matching against the actual schedule, not a "
            "guess: never treat a suggestion as the flight actually meant, "
            "only list them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "near": {"type": "string", "description": "The malformed flight number as typed, e.g. 'DX9999'."},
                "limit": {"type": "integer", "description": "How many candidates to return (default 3)."},
            },
            "required": ["near"],
        },
    },
    {
        "name": "list_controllers",
        "description": (
            "The controller desks working this operation, by name and which "
            "aircraft each covers -- often TWO aircraft per desk. Call this "
            "first for 'the aircraft [controller name] covers/handles', "
            "'who covers VT-DXA', or any question naming a controller by "
            "first name alone (Ananya, Rohit, Divya are controllers, not "
            "crew -- `lookup(entity='crew', filters={'name': 'Ananya'})` "
            "will correctly find nobody). If the desk covers two aircraft, "
            "the question is asking about both, not whichever one you check "
            "first. A controller dispatches; none of them are crew, so this "
            "is never what answers a question about a crew member even if a "
            "name looks similar."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "controller_issue_counts",
        "description": (
            "How many open disruption cases each controller desk currently "
            "has, from the live ledger. Use this for 'how many issues does "
            "each controller have' -- do not try to count it from `lookup`, "
            "which has no notion of a controller or a disruption case."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        # Deliberately NOT in ADVISOR_TOOL_NAMES / foundry_tools()'s default
        # list. This writes a real roster assignment, and a human clicking
        # "Approve & commit" has to be the one who decides that, not the
        # Resolution Advisor's own tool loop. It's still a real tool schema
        # (so `dispatch()` validates and traces it the same as the other
        # ten), just called directly by the console UI, never offered to
        # the model.
        "name": "commit_decision",
        "description": (
            "Commit a candidate to a pairing: writes the roster assignment "
            "and closes the disruption. Only for a human-confirmed decision, "
            "never for the Resolution Advisor Agent's own tool loop."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "disruption_id": {"type": "string"},
                "pairing_id": {"type": "string", "pattern": "^P-[0-9]{4}$"},
                "crew_id": {"type": "string", "pattern": "^C-[0-9]{4}$"},
                "role": {"type": "string"},
                "committed_by": {"type": "string", "description": "The controller's name."},
                "accepted_rank": {"type": "integer"},
                "presented_options": {
                    "type": "array", "items": {"type": "object"},
                    "description": "The option list shown when this was decided, for the audit log.",
                },
            },
            "required": ["disruption_id", "pairing_id", "crew_id", "role", "committed_by"],
        },
    },
]

TOOL_NAMES: frozenset[str] = frozenset(t["name"] for t in TOOL_SCHEMAS)

ADVISOR_TOOL_NAMES: frozenset[str] = TOOL_NAMES - {"commit_decision"}
"""What the Resolution Advisor Agent's own tool loop is offered. See
`commit_decision`'s schema comment above for why it's excluded."""


def foundry_tools(names: Any = None, schemas: list[dict[str, Any]] | None = None) -> list[Any]:
    """`TOOL_SCHEMAS` (or `schemas`, or a subset of either by `names`) as
    `azure.ai.projects.models.FunctionTool`.

    `schemas` lets a caller pass `schemas_for_port(port)`'s enriched list
    (real column names baked into `lookup`'s own description) instead of the
    generic one. Every tool is still offered to the model either way; only
    the wording of `lookup`'s own schema differs. Imports the Azure SDK
    lazily so this module stays importable without the SDK installed (e.g.
    under `pytest`).
    """
    from azure.ai.projects.models import FunctionTool

    base = schemas if schemas is not None else TOOL_SCHEMAS
    wanted = base if names is None else [t for t in base if t["name"] in names]
    return [
        FunctionTool(
            name=t["name"],
            description=t["description"],
            parameters=t["input_schema"],
            strict=False,
        )
        for t in wanted
    ]


# --------------------------------------------------------------------------
# Filter guard rails
#
# Measured over the tier-1 gold questions on a small local model: most
# failures were an invented column name. Not a random guess either, but the
# *semantically right* field under a plausible other name (`departure` for
# `dep_station`, `expiry_date` for `valid_to`). So there are three layers,
# in order of preference:
#
#   1. tell the model the real column names   (schema enrichment, below)
#   2. map a near-miss onto the real one      (FIELD_ALIASES)
#   3. reject loudly, naming what is valid    (resolve_filters)
#
# Guarding alone would only turn a wrong answer into a failed one; the
# model still has to be able to succeed.
# --------------------------------------------------------------------------

FIELD_ALIASES: dict[str, str] = {
    # station fields
    "departure": "dep_station", "origin": "dep_station", "from": "dep_station",
    "departure_station": "dep_station", "dep": "dep_station",
    "destination": "arr_station", "arrival": "arr_station", "to": "arr_station",
    "arrival_station": "arr_station", "arr": "arr_station",
    # identity
    "crew": "crew_id", "crewid": "crew_id", "employee_id": "crew_id",
    "pairing": "pairing_id", "flight": "flight_no", "flight_number": "flight_no",
    "aircraft_registration": "aircraft", "tail": "aircraft", "registration": "aircraft",
    # certifications
    "expiry_date": "valid_to", "expiry": "valid_to", "expires": "valid_to",
    "expires_on": "valid_to", "valid_until": "valid_to", "cert": "cert_type",
    "certification": "cert_type", "type": "cert_type",
    # misc
    "station": "base", "home_base": "base", "rank_name": "rank",
    "role": "rank", "position": "rank", "job": "rank",
    "aircraft_rating": "ratings", "rating": "ratings",
}


# Range operators. Equality stays the default — these exist because some
# tier-1 questions are genuinely about ranges ("expiring within 30 days of
# 15 Sep" is `valid_to` between two dates). Answering that by pulling every
# row and letting the model filter would put the selection back above the
# trust boundary, which is the one thing this system does not do.
RANGE_OPS: dict[str, str] = {"gte": ">=", "lte": "<=", "gt": ">", "lt": "<"}


def resolve_filters(
    entity: str, filters: dict[str, Any] | None, known: frozenset[str] | set[str]
) -> dict[str, Any]:
    """Map filter keys onto real columns, or fail naming the valid ones.

    A value may be a scalar (equality), a list (membership), or a dict of
    `RANGE_OPS` (a range). An unknown operator is rejected here rather than
    silently ignored — a dropped bound would quietly widen the result set,
    with no way for the caller to tell.
    """
    resolved: dict[str, Any] = {}
    for key, value in (filters or {}).items():
        if key in known:
            column = key
        else:
            alias = FIELD_ALIASES.get(key.lower().replace(" ", "_"))
            if not (alias and alias in known):
                raise ToolError(
                    "UNRESOLVED_ENTITY",
                    f"{entity} has no field {key!r}. "
                    f"Valid fields: {', '.join(sorted(known))}",
                )
            column = alias

        if isinstance(value, dict):
            bad = sorted(set(value) - set(RANGE_OPS))
            if bad:
                raise ToolError(
                    "UNRESOLVED_ENTITY",
                    f"unknown filter operator{'s' if len(bad) > 1 else ''} "
                    f"{', '.join(repr(b) for b in bad)} on {column!r}. "
                    f"Valid operators: {', '.join(sorted(RANGE_OPS))}",
                )
        resolved[column] = value
    return resolved


def with_crew_identity(rows: list[dict[str, Any]],
                       crew: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach name and rank to rows keyed by crew_id.

    `reserve_pool` holds a crew id and an on-call window, nothing about the
    person — but "who is on reserve" can't be answered if you can't see
    whether they're a Captain. The join belongs here, in the layer whose
    job is to present a domain entity rather than a table.
    """
    by_id = {c["crew_id"]: c for c in crew}
    out = []
    for row in rows:
        who = by_id.get(row.get("crew_id"))
        out.append({**row, "name": who.get("name"), "rank": who.get("rank")}
                   if who else dict(row))
    return out


def crew_named(port: Any, name: str) -> list[dict[str, Any]]:
    """Roster entries whose name matches, by surname or in full.

    Deliberately returns every match rather than picking a best one. Several
    surnames repeat across this dataset's 150 crew, sometimes across ranks
    and bases, and there's no defensible way to pick one — so the caller
    asks.
    """
    wanted = name.strip().lower()
    if not wanted:
        return []
    out = []
    for row in port.lookup("crew"):
        full = str(row.get("name") or "").lower()
        if full == wanted or full.split()[-1:] == [wanted]:
            out.append(row)
    return out


def suggest_crew_names(port: Any, name: str, limit: int = 3) -> list[dict[str, Any]]:
    """Real crew names closest to one that matched nobody — for a typo
    ("A. Nayar" for "A. Nair"), the same "suggest, never substitute"
    discipline `core_engine/resolve.py` already applies to ids. Uses plain
    character-level closeness (`difflib`), not BM25: a single misspelled
    surname is too short for term-frequency ranking to mean anything, and
    this dataset's 150 names is small enough that a direct closeness scan
    is instant anyway.
    """
    import difflib

    wanted = name.strip().lower()
    if not wanted:
        return []
    rows = port.lookup("crew")
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_name.setdefault(str(row.get("name") or "").lower(), []).append(row)
    close = difflib.get_close_matches(wanted, by_name, n=limit, cutoff=0.6)
    return [row for key in close for row in by_name[key]][:limit]


def _comparable(value: Any) -> Any:
    """Coerce a value so a row and a filter bound can be ordered together.

    ISO-8601 dates sort correctly as strings, which is why the range filters
    work at all against JSON. Comparing both sides as text is exact for ISO
    dates, and harmless for everything else that reaches here.
    """
    return value if isinstance(value, (int, float)) else str(value)


def row_matches(row: dict[str, Any], column: str, want: Any) -> bool:
    """Whether one row satisfies one resolved filter."""
    have = row.get(column)
    if isinstance(want, dict):
        if have is None:
            return False
        for op, bound in want.items():
            left, right = _comparable(have), _comparable(bound)
            if op == "gte" and not left >= right:
                return False
            if op == "lte" and not left <= right:
                return False
            if op == "gt" and not left > right:
                return False
            if op == "lt" and not left < right:
                return False
        return True
    if isinstance(want, (list, tuple)):
        # A list means membership, except against a list-valued column
        # (`ratings`, `dates`), where it means containment.
        if isinstance(have, (list, tuple)):
            return all(w in have for w in want)
        return have in want
    if isinstance(have, (list, tuple)):
        return want in have
    return have == want


def schemas_for_port(port: Any) -> list[dict[str, Any]]:
    """`TOOL_SCHEMAS` with `lookup` enriched by the backend's real field names.

    Without this, the model is guessing at column names from the entity
    name alone, which is where nearly every tier-1 tool failure came from.
    """
    describe = getattr(port, "entity_fields", None)
    if describe is None:
        return TOOL_SCHEMAS

    lines = []
    for entity in sorted(getattr(port, "ENTITIES", ()) or ()):
        try:
            fields = sorted(describe(entity))
        except Exception:
            continue
        lines.append(f"  {entity}: {', '.join(fields)}")
    if not lines:
        return TOOL_SCHEMAS

    enriched = []
    for tool in TOOL_SCHEMAS:
        if tool["name"] != "lookup":
            enriched.append(tool)
            continue
        clone = {**tool, "input_schema": {**tool["input_schema"],
                                          "properties": {**tool["input_schema"]["properties"]}}}
        original = tool["input_schema"]["properties"]["filters"]
        clone["input_schema"]["properties"]["filters"] = {
            **original,
            "description": (
                original["description"] + "\n\nUse ONLY these field names per "
                "entity — any other key is rejected:\n" + "\n".join(lines)
            ),
        }
        enriched.append(clone)
    return enriched


# --------------------------------------------------------------------------
# The seam to core_engine/
# --------------------------------------------------------------------------


@runtime_checkable
class ToolPort(Protocol):
    """What the JSON-backed engine must provide. Mirrors TOOL_SCHEMAS one-for-one."""

    def lookup(self, entity: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...
    def duty_clock(self, crew_id: str, date: str | None = None) -> dict[str, Any]: ...
    def notification_brief(self, crew_id: str, pairing_id: str) -> dict[str, Any]: ...
    def check_legality(self, crew_id: str, pairing_id: str | None = None,
                       flight_id: str | None = None, flight_no: str | None = None,
                       date: str | None = None, aircraft: str | None = None,
                       delay_h: float = 0.0) -> dict[str, Any]: ...
    def same_pairing(self, a: str, b: str) -> dict[str, Any]: ...
    def find_options(self, role: str | None = None, pairing_id: str | None = None,
                     flight_id: str | None = None, crew_id: str | None = None,
                     flight_no: str | None = None, date: str | None = None,
                     callout_utc: str | None = None) -> dict[str, Any]: ...
    def ripple(self, event: dict[str, Any]) -> dict[str, Any]: ...
    def simulate(self, event: dict[str, Any]) -> dict[str, Any]: ...
    def joint_plan(self, events: list[dict[str, Any]]) -> dict[str, Any]: ...
    def explain_rule(self, rule_id: str) -> dict[str, Any]: ...
    def search_rules(self, query: str, top_k: int = 3) -> list[dict[str, Any]]: ...
    def suggest_crew_ids(self, near: str, limit: int = 3) -> list[dict[str, Any]]: ...
    def suggest_pairing_ids(self, near: str, limit: int = 3) -> list[dict[str, Any]]: ...
    def suggest_flight_nos(self, near: str, limit: int = 3) -> list[dict[str, Any]]: ...
    def list_controllers(self) -> list[dict[str, Any]]: ...
    def controller_issue_counts(self) -> list[dict[str, Any]]: ...
    def check_gate(self, flight_id: str | None = None, flight_no: str | None = None,
                   date: str | None = None, boarding_gate_number: str | None = None,
                   delay_minutes: float = 0.0, at_utc: str | None = None,
                   station: str | None = None) -> dict[str, Any]: ...
    def commit_decision(self, disruption_id: str, pairing_id: str, crew_id: str, role: str,
                        committed_by: str, accepted_rank: int | None = None,
                        presented_options: list[dict[str, Any]] | None = None,
                        override_reason: str | None = None) -> dict[str, Any]: ...


class ToolError(RuntimeError):
    """A tool failed in a way the model should see and route around."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


def validate_args(name: str, args: dict[str, Any]) -> None:
    """Check arguments against the tool's own JSON Schema before calling it.

    The schemas already carry patterns like `^P-[0-9]{4}$`. Without this
    check, a call like `find_options(pairing_id="BLR->BOM")` would reach the
    engine and fail there with a message about missing fixtures, rather than
    about the malformed id. Catching it here says what's actually wrong.
    """
    schema = next((t["input_schema"] for t in TOOL_SCHEMAS if t["name"] == name), None)
    if not schema:
        return
    props = schema.get("properties", {})

    for key, value in args.items():
        spec = props.get(key)
        if not spec or value is None:
            continue
        if (pattern := spec.get("pattern")) and isinstance(value, str):
            if not re.fullmatch(pattern, value):
                raise ToolError(
                    "UNRESOLVED_ENTITY",
                    f"{name}: {key}={value!r} is not a valid identifier "
                    f"(expected {pattern}). Look the value up first rather "
                    f"than constructing it.",
                )
        if (allowed := spec.get("enum")) and value not in allowed:
            raise ToolError(
                "UNRESOLVED_ENTITY",
                f"{name}: {key}={value!r} is not one of {', '.join(map(str, allowed))}",
            )

    for required in schema.get("required", []):
        if required not in args:
            raise ToolError("UNRESOLVED_ENTITY", f"{name}: {required!r} is required")


def dispatch(port: ToolPort, name: str, args: dict[str, Any]) -> TraceEntry:
    """Invoke one tool and record it. Never raises — errors become trace rows.

    A tool failure has to reach the model as data so it can route around it.
    An exception here would take the whole turn down instead.
    """
    started = time.perf_counter()

    if name not in TOOL_NAMES:
        return TraceEntry(tool=name, args=args, error=f"unknown tool {name!r}")

    try:
        validate_args(name, args)
        result = getattr(port, name)(**args)
        error = None
    except ToolError as exc:
        result, error = None, f"{exc.code}: {exc.message}"
    except TypeError as exc:
        result, error = None, f"bad arguments for {name}: {exc}"
    except Exception as exc:  # a tool bug must not kill the turn
        result, error = None, f"{type(exc).__name__}: {exc}"

    return TraceEntry(
        tool=name,
        args=args,
        result=result,
        ms=int((time.perf_counter() - started) * 1000),
        error=error,
    )
