"""The tool boundary — where the language model stops and Python starts.

This is the trust boundary. Everything below it is deterministic; the
Resolution Advisor Agent above it may only *choose* tools and *narrate* what
they return. It never computes a duty hour or a cost itself.

Tools are coarse and semantically meaningful rather than micro-CRUD, so a
tier-2 question is three calls rather than thirty. There are ten of them —
`TOOL_SCHEMAS` below is the exact list, in vendor-neutral JSON Schema.
`foundry_tools()` adapts that list into `azure.ai.projects.models.FunctionTool`
objects for `PromptAgentDefinition(tools=...)`.

`JsonToolPort` (in `core_engine/port.py`) is the one implementation: it
satisfies `ToolPort` by computing answers from the vendored dataset in
`data/` rather than replaying fixtures, so it works for any pairing rather
than only the ones with a published answer key.
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
            "crew, flights, pairings, reserves, certifications, risk signals."
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
                        "Dates are ISO-8601. A value may instead be a range, "
                        "e.g. {'valid_to': {'gte': '2026-09-15', "
                        "'lte': '2026-10-15'}} for 'expiring within 30 days of "
                        "15 Sep'. Operators: gte, lte, gt, lt."
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
            "RULE-DUTY-02 and RULE-FLT-03. Windows are calendar-day based."
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
            "named directly or via a flight on it. "
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
                "date": {"type": "string", "description": "ISO date, with flight_no"},
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
            "depletion, aircraft rotation knock-on."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event": {
                    "type": "object",
                    "description": (
                        "e.g. {'type':'SICK_CREW','crew_id':'C-1042',"
                        "'pairing_id':'P-2291','reported_utc':'...'}"
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
            "diff. Handles SICK_CREW, STATION_CLOSURE, TECH_DELAY, CERT_LAPSE."
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
            "pairings. Note that ties are common and all are equally correct."
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
            "window, never a bare boolean."
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
                "date": {"type": "string", "description": "ISO date, with flight_no"},
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
        # Deliberately NOT in ADVISOR_TOOL_NAMES / foundry_tools()'s default
        # list — this writes a real roster assignment, and a human clicking
        # "Approve & commit" has to be the one who decides that, not the
        # Resolution Advisor's own tool loop. It's a real tool schema (so
        # `dispatch()` validates and traces it the same as the other ten)
        # called directly by the console UI, never offered to the model.
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


def foundry_tools(names: Any = None) -> list[Any]:
    """`TOOL_SCHEMAS` (or a subset of them) as `azure.ai.projects.models.FunctionTool`.

    Imports the Azure SDK lazily so this module — and everything that reads
    `TOOL_SCHEMAS` for its own purposes, like the router's tool narrowing —
    stays importable without the SDK installed (e.g. under `pytest`).
    """
    from azure.ai.projects.models import FunctionTool

    wanted = TOOL_SCHEMAS if names is None else [t for t in TOOL_SCHEMAS if t["name"] in names]
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
# failures were an invented column name — and not a random guess, but the
# *semantically right* field under a plausible other name (`departure` for
# `dep_station`, `expiry_date` for `valid_to`). So three layers, in order of
# preference:
#
#   1. tell the model the real column names   (schema enrichment, below)
#   2. map a near-miss onto the real one      (FIELD_ALIASES)
#   3. reject loudly, naming what is valid    (resolve_filters)
#
# Guarding alone would only convert a wrong answer into a failed one; the
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
# tier-1 questions are genuinely intervals ("expiring within 30 days of 15
# Sep" is `valid_to` between two dates) and answering one by pulling every
# row and letting the model filter would put the selection back above the
# trust boundary, which is the one thing this system does not do.
RANGE_OPS: dict[str, str] = {"gte": ">=", "lte": "<=", "gt": ">", "lt": "<"}


def resolve_filters(
    entity: str, filters: dict[str, Any] | None, known: frozenset[str] | set[str]
) -> dict[str, Any]:
    """Map filter keys onto real columns, or fail naming the valid ones.

    A value may be a scalar (equality), a list (membership), or a dict of
    `RANGE_OPS` (an interval). An unknown operator is rejected here rather
    than silently ignored — a dropped bound would quietly widen the result
    set, and the caller would have no way to tell.
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

    `reserve_pool` holds a crew id and an on-call window and nothing about the
    person — but "who is on reserve" is unanswerable if you cannot see whether
    they are a Captain. The join belongs here, in the layer whose job is to
    present a domain entity rather than a table.
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

    Deliberately returns every match rather than a best one: several surnames
    repeat across this dataset's 150 crew, sometimes across ranks and bases.
    There is no defensible way to pick one, so the caller asks.
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


def _comparable(value: Any) -> Any:
    """Coerce a value so a row and a filter bound can be ordered together.

    ISO-8601 dates sort correctly as strings, which is why the range filters
    work at all against JSON. Comparing both sides as text is exact for ISO
    dates and harmless for everything else that reaches here.
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

    Without this the model is guessing at column names from the entity name
    alone, which is where nearly every tier-1 tool failure came from.
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
        clone["input_schema"]["properties"]["filters"] = {
            "type": "object",
            "description": (
                "Field equality filters. Use ONLY these field names — any other "
                "key is rejected:\n" + "\n".join(lines)
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
                       date: str | None = None,
                       delay_h: float = 0.0) -> dict[str, Any]: ...
    def find_options(self, role: str | None = None, pairing_id: str | None = None,
                     flight_id: str | None = None, crew_id: str | None = None,
                     flight_no: str | None = None, date: str | None = None,
                     callout_utc: str | None = None) -> dict[str, Any]: ...
    def ripple(self, event: dict[str, Any]) -> dict[str, Any]: ...
    def simulate(self, event: dict[str, Any]) -> dict[str, Any]: ...
    def joint_plan(self, events: list[dict[str, Any]]) -> dict[str, Any]: ...
    def explain_rule(self, rule_id: str) -> dict[str, Any]: ...
    def check_gate(self, flight_id: str | None = None, flight_no: str | None = None,
                   date: str | None = None, boarding_gate_number: str | None = None,
                   delay_minutes: float = 0.0, at_utc: str | None = None) -> dict[str, Any]: ...
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

    The schemas already carry patterns like `^P-[0-9]{4}$`; without this check
    a call like `find_options(pairing_id="BLR->BOM")` would reach the engine
    and fail there with a message about missing fixtures rather than about the
    malformed id. Catching it here says what is actually wrong.
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

    A tool failure has to reach the model as data so it can route around it;
    an exception here would take the whole turn down instead.
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
