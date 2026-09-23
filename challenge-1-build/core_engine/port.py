"""`JsonToolPort` — the real engine behind the tool boundary.

Satisfies `tools.ToolPort` by computing answers from the operation rather
than replaying fixtures, so it works for any pairing rather than only the
ones with a published scenario answer key.

This merges what dCortex Crew Ops Advisor split into `PostgresToolPort` (raw
row access) and `CoreToolPort` (legality-dependent tools) into one class,
because there is exactly one backend here: the vendored dataset JSON files in
`../data/`, loaded once and held in memory. `ResolutionAdvisorAgent` in
`agents.py` is the only caller.
"""

from __future__ import annotations

import itertools
import json
import re
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import config
from entities import CREW_RE, RANK_WORD_RE_FRAGMENT, RANK_WORD_TO_RANK
from tools import (
    ToolError, crew_named, resolve_filters, row_matches, suggest_crew_names,
    with_crew_identity,
)
from core_engine import rules
from core_engine.duty import MAX_DUTY_HOURS_7D, MAX_FLIGHT_HOURS_28D
from core_engine.gates import GateWorld, iso, load_gates, next_at_gate, occupant_at, parse
from core_engine.resolve import resolve
from core_engine.world import (
    STRATEGY_LABELS, Candidate, World, assess, drop_stage, load_world, resilience_score,
)

FUNNEL_ORDER = ("considered", "qualified", "certified", "in position",
                "available", "within limits", "legal")


class JsonToolPort:
    """The vendored dataset for facts, `core_engine/` for judgement."""

    # Raw dataset file each entity is sourced from.
    SOURCES = {
        "crew": "crew", "flights": "flights", "reserves": "reserve_pool",
        "certifications": "certifications", "risk_signals": "risk_signals",
        "costs": "costs", "pairings": "rosters",
        # `rosters.json` nests these; flattening them here keeps one entity
        # set regardless of how the source file is shaped.
        "pairing_crew": "rosters", "pairing_days": "rosters",
    }
    ENTITIES = tuple(SOURCES)

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or config.DATA_DIR
        self._cache: dict[str, Any] = {}
        self._world: World | None = None
        self._gates: GateWorld | None = None

    # -- lazy-loaded, held for the process lifetime -------------------------

    @property
    def world(self) -> World:
        if self._world is None:
            self._world = load_world(self.data_dir)
        return self._world

    @property
    def gates(self) -> GateWorld:
        if self._gates is None:
            rows = json.loads((self.data_dir / "boarding_gates.json").read_text(encoding="utf-8"))
            self._gates = load_gates(rows)
        return self._gates

    def _load(self, name: str) -> Any:
        if name not in self._cache:
            path = self.data_dir / f"{name}.json"
            if not path.exists():
                raise ToolError("INTERNAL", f"dataset file missing: {path.name}")
            self._cache[name] = json.loads(path.read_text(encoding="utf-8"))
        return self._cache[name]

    def _rows(self, entity: str) -> list[dict[str, Any]]:
        source = self.SOURCES.get(entity)
        if source is None:
            raise ToolError("UNRESOLVED_ENTITY", f"unknown entity {entity!r}")
        rows = self._load(source)
        if entity == "pairings":
            rows = rows["pairings"]
        elif entity == "pairing_crew":
            return [{"pairing_id": p["pairing_id"], **c}
                    for p in rows["pairings"] for c in p.get("crew", [])]
        elif entity == "pairing_days":
            return [{"pairing_id": p["pairing_id"], **d}
                    for p in rows["pairings"] for d in p.get("days", [])]
        return [rows] if isinstance(rows, dict) else rows

    def entity_fields(self, entity: str) -> frozenset[str]:
        rows = self._rows(entity)
        return frozenset(rows[0].keys()) if rows else frozenset()

    def pairing_for_flight(self, flight_id: str) -> str:
        """Which pairing operates a given leg.

        A controller names a disruption by route or flight ("captain of
        BLR->BOM is out"), but cover is found per *pairing* — crew fly whole
        pairings, not single legs. Without this hop the model would invent a
        pairing id from the route.
        """
        for pairing in self._rows("pairings"):
            for day in pairing.get("days", []):
                if flight_id in day.get("flights", []):
                    return pairing["pairing_id"]
        raise ToolError("UNRESOLVED_ENTITY", f"no pairing operates {flight_id!r}")

    # -- Tool 1: lookup ------------------------------------------------------

    # Filter keys that name a specific real-world identifier rather than a
    # descriptive attribute -- an equality filter on one of these that
    # matches nothing is far more often a malformed or invented id (a
    # transposed digit, a made-up pairing) than a genuine "no such record",
    # so it is worth telling the id universe apart from an ordinary filter
    # miss (e.g. `{"rank": "Captain", "base": "XYZ"}` legitimately matching
    # zero rows needs no such check).
    _ID_FIELD_KIND = {"crew_id": "crew", "pairing_id": "pairing", "flight_id": "flight"}

    def lookup(self, entity: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        resolved = resolve_filters(entity, filters, self.entity_fields(entity))
        rows = self._rows(entity)
        for key, want in resolved.items():
            rows = [r for r in rows if row_matches(r, key, want)]

        if not rows:
            # An empty result for a real id (just excluded by some other
            # filter) is a legitimate answer and must not raise here --
            # `require()` only raises when the id itself doesn't exist, so
            # this only ever intercepts the invented/malformed-id case.
            for key, want in resolved.items():
                if (kind := self._ID_FIELD_KIND.get(key)) and isinstance(want, str):
                    self.require(kind, want)

            # Same discipline for a crew name that matched nobody -- "A.
            # Nayar" is far more often a typo of a real crew member ("A.
            # Nair") than a genuine "no such person", so this is worth
            # telling apart from an ordinary filter miss the same way a
            # malformed id already is, just above.
            if entity == "crew" and isinstance(resolved.get("name"), str):
                suggestions = suggest_crew_names(self, resolved["name"])
                if suggestions:
                    listing = ", ".join(
                        f"{s['name']} ({s['crew_id']}, {s.get('rank')})" for s in suggestions)
                    raise ToolError(
                        "NEEDS_CONFIRMATION",
                        f"No crew named {resolved['name']!r}. Did you mean "
                        f"{listing}? Confirm which and I will run it — I will not guess.")

        if entity == "reserves":
            rows = with_crew_identity(rows, self._rows("crew"))
        return rows

    # -- Tool 2: notification_brief ------------------------------------------

    def notification_brief(self, crew_id: str, pairing_id: str) -> dict[str, Any]:
        import notify

        crew = self.lookup("crew", {"crew_id": crew_id})
        if not crew:
            raise ToolError("UNRESOLVED_ENTITY", f"no crew {crew_id!r}")
        pairings = self.lookup("pairings", {"pairing_id": pairing_id})
        if not pairings:
            raise ToolError("UNRESOLVED_ENTITY", f"no pairing {pairing_id!r}")
        pairing = pairings[0]

        by_id = {f["flight_id"]: f for f in self._rows("flights")}
        days = [
            {**day, "flights": [by_id[f] for f in day.get("flights", []) if f in by_id]}
            for day in pairing.get("days", [])
        ]
        role = next((c["role"] for c in pairing.get("crew", [])
                     if c["crew_id"] == crew_id), None)
        return notify.assemble(crew[0], pairing_id, pairing.get("aircraft"), days, role)

    # -- Tool 3: duty_clock ---------------------------------------------------

    def duty_clock(self, crew_id: str, date: str | None = None) -> dict[str, Any]:
        self.require("crew", crew_id)
        crew = self.world.crew[crew_id]
        as_of = _parse_date(date) if date else _parse_date(config.SNAPSHOT_UTC[:10])
        duty_7d = crew.duty_in_window(as_of)
        flight_28d = crew.flight_in_window(as_of)
        return {
            "crew_id": crew_id,
            "name": crew.name,
            "rank": crew.rank,
            "as_of": as_of.isoformat(),
            "duty_hours_7d": duty_7d,
            "duty_limit_7d": MAX_DUTY_HOURS_7D,
            "duty_headroom_7d": round(MAX_DUTY_HOURS_7D - duty_7d, 2),
            "flight_hours_28d": flight_28d,
            "flight_limit_28d": MAX_FLIGHT_HOURS_28D,
            "flight_headroom_28d": round(MAX_FLIGHT_HOURS_28D - flight_28d, 2),
        }

    # -- Tool 10: explain_rule ------------------------------------------------

    def explain_rule(self, rule_id: str) -> dict[str, Any]:
        for rule in self._load("rules")["rules"]:
            if rule["rule_id"] == rule_id:
                return rule
        raise ToolError("UNRESOLVED_ENTITY", f"no rule {rule_id!r}")

    # -- Tool 11: search_rules --------------------------------------------
    # Only for a paraphrased legality question that never names a rule id --
    # explain_rule above is the exact-id path and stays the first choice
    # whenever a rule id is actually given. A no-op ([]) when the ledger or
    # the embedding model isn't configured (see core_engine/rule_search.py).

    def search_rules(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        from core_engine import rule_search
        return rule_search.search_rules(query, top_k=top_k)

    # -- Tool 12: suggest_crew_ids -----------------------------------------
    # For a crew id shaped wrong ("C-10", not the dataset's 4-digit ids) --
    # deterministic digit-prefix comparison against the real roster, never
    # embedding-based: identity resolution stays exact everywhere else in
    # this system, and a "did you mean" is no exception -- it only ever
    # NAMES candidates for a human to pick from, never picks one itself.

    def suggest_crew_ids(self, near: str, limit: int = 3) -> list[dict[str, Any]]:
        from core_engine.resolve import _digit_prefix_matches

        crew_rows = {r["crew_id"]: r for r in self._rows("crew")}
        matches = _digit_prefix_matches(near, list(crew_rows), limit)
        return [
            {"crew_id": cid, "name": crew_rows[cid].get("name"),
             "rank": crew_rows[cid].get("rank"), "base": crew_rows[cid].get("base")}
            for cid in matches
        ]

    # -- Tool 14: list_controllers ---------------------------------------------

    def list_controllers(self) -> list[dict[str, Any]]:
        """The controller desks sharing this operation. Not a crew role --
        a controller dispatches, none of them fly, so this is never
        confusable with a `lookup(entity='crew', ...)` result."""
        return [dict(c) for c in config.CONTROLLERS]

    # -- Tool 15: controller_issue_counts ---------------------------------

    def controller_issue_counts(self) -> list[dict[str, Any]]:
        """How many of the dataset's engineered disruption scenarios are
        still open at each desk, from the live ledger -- not a static count,
        since a controller may already have committed a decision on one.
        A scenario never registered in the ledger yet counts as open: the
        console never seeds it as "closed" ahead of a real decision."""
        from core_engine import ledger

        statuses = {row["disruption_id"]: row["status"]
                    for row in ledger.list_open_disruptions()} if ledger.enabled() else {}
        counts = {c["name"]: 0 for c in config.CONTROLLERS}
        for scenario_id, desk in config.SCENARIO_DESKS.items():
            if statuses.get(scenario_id, "open") == "open":
                counts[desk] = counts.get(desk, 0) + 1
        return [{"controller": name, "open_issues": count} for name, count in counts.items()]

    # -- entity resolution ----------------------------------------------------

    def _universe(self, kind: str) -> tuple[dict[str, Any], Any]:
        """The valid id space for one kind, and how to describe a member.

        The label matters as much as the id: "C-1024" alone is not something a
        controller can confirm against, but "C-1024 (First Officer, DEL)" is.
        """
        w = self.world
        if kind == "crew":
            return w.crew, lambda c: f"{c.rank}, {c.base}, {'/'.join(c.ratings)}"
        if kind == "pairing":
            return (
                w.pairing_days,
                lambda days: (f"{len(days)} day{'s' if len(days) > 1 else ''} from "
                              f"{days[0].date}, {days[0].dep_station}"),
            )
        if kind == "flight":
            flights = {f["flight_id"]: f
                       for legs in w.pairing_flights.values() for f in legs}
            return (flights,
                    lambda f: f"{f['dep_station']}->{f['arr_station']} {f['date']}")
        raise ToolError("INTERNAL", f"no id universe for {kind!r}")

    def require(self, kind: str, value: str | None) -> str:
        """Confirm an id is real, or raise something a controller can act on.

        Never substitutes a near match. `C-1042` and `C-1024` differ by one
        transposed digit; in this dataset one is a captain and the other is
        nobody. Silently correcting would dispatch a different human being to
        an aircraft — so a near match is returned as a question, not an
        answer.
        """
        if not value:
            raise ToolError("UNRESOLVED_ENTITY", f"a {kind} id is required")

        universe, label = self._universe(kind)
        result = resolve(kind, value, universe, label)
        if result.exists:
            return value

        raise ToolError(
            "NEEDS_CONFIRMATION" if result.needs_confirmation else "UNRESOLVED_ENTITY",
            result.message(),
        )

    def resolve_flight(self, flight_id: str | None = None,
                       flight_no: str | None = None,
                       date: str | None = None) -> str:
        """A flight id from whatever the controller named.

        A bare flight number is ambiguous — some fly on three separate days
        in this one week, each on a different pairing. Picking one silently
        would answer a question nobody asked, so an undated flight number
        comes back as a request for the date.
        """
        if flight_id:
            self.require("flight", flight_id)
            return flight_id
        if not flight_no:
            raise ToolError("UNRESOLVED_ENTITY", "no flight was named")

        rows = self.lookup("flights", {"flight_no": flight_no})
        if not rows:
            raise ToolError("UNRESOLVED_ENTITY", f"there is no flight {flight_no}")

        if date:
            match = [r for r in rows if str(r["date"]) == str(date)]
            if not match:
                flew = ", ".join(sorted(str(r["date"]) for r in rows))
                raise ToolError(
                    "UNRESOLVED_ENTITY",
                    f"{flight_no} does not operate on {date}. It flies on {flew}.")
            return match[0]["flight_id"]

        if len(rows) == 1:
            return rows[0]["flight_id"]

        flew = ", ".join(sorted(str(r["date"]) for r in rows))
        raise ToolError(
            "AMBIGUOUS_QUERY",
            f"{flight_no} operates on {flew}. Which date do you mean? "
            f"Each is a different pairing, so the answer differs.")

    # -- Tool 9: check_gate -----------------------------------------------

    def check_gate(self, flight_id: str | None = None, flight_no: str | None = None,
                   date: str | None = None, boarding_gate_number: str | None = None,
                   delay_minutes: float = 0.0, at_utc: str | None = None,
                   station: str | None = None) -> dict[str, Any]:
        gates = self.gates

        # Nothing named at all: an aggregate question. Two different
        # aggregates share this branch, told apart by whether a date was
        # given -- "how many boarding gates are there" (static inventory,
        # true regardless of date) versus "how many gates were occupied on
        # <date>" (which flights actually used a gate that day). Answering
        # the second with the first's number silently substitutes "how many
        # gates exist" for "how many were busy", which is a different
        # question with a different, usually larger, answer. Either one can
        # additionally be scoped to a single station -- "how many gates
        # does Bangalore occupy" silently answering for every station would
        # be a different, wrong-looking number.
        if not flight_id and not flight_no and not boarding_gate_number:
            all_gates = sorted(
                g for g in gates.by_gate if not station or g.startswith(f"{station}-")
            )
            if date:
                if not (config.WEEK_START <= str(date) <= config.WEEK_END):
                    raise ToolError(
                        "UNRESOLVED_ENTITY",
                        f"{date} is outside the dataset -- it only covers "
                        f"{config.WEEK_START} to {config.WEEK_END}.")
                occupied = sorted(
                    g for g in all_gates
                    if any(r.get("date") == date for r in gates.by_gate[g])
                )
                by_station: dict[str, int] = {}
                for gate_number in occupied:
                    st = gate_number.split("-", 1)[0]
                    by_station[st] = by_station.get(st, 0) + 1
                return {
                    "date": date,
                    "station": station,
                    "occupied_boarding_gates": len(occupied),
                    "of_boarding_gates_available": len(all_gates),
                    "occupied_by_station": dict(sorted(by_station.items())),
                    "occupied_gate_numbers": occupied,
                }

            by_station = {}
            for gate_number in all_gates:
                st = gate_number.split("-", 1)[0]
                by_station[st] = by_station.get(st, 0) + 1
            return {
                "station": station,
                "total_boarding_gates": len(all_gates),
                "gates_by_station": dict(sorted(by_station.items())),
                "boarding_gate_numbers": all_gates,
            }

        # No flight named: a pure occupancy question -- "is BLR-G1 blocked
        # right now / at this instant". Free ends up meaning no aircraft
        # holds the gate in that instant, not that nothing operates through it.
        if not flight_id and not flight_no and boarding_gate_number:
            if boarding_gate_number not in gates.by_gate:
                raise ToolError(
                    "UNRESOLVED_ENTITY",
                    f"there is no boarding gate {boarding_gate_number!r}. "
                    f"Known gates: {', '.join(sorted(gates.by_gate))}")
            instant = parse(at_utc) if at_utc else parse(config.SNAPSHOT_UTC)
            occupant = occupant_at(gates, boarding_gate_number, instant)
            return {
                "boarding_gate_number": boarding_gate_number,
                "checked_at_utc": at_utc or config.SNAPSHOT_UTC,
                "occupied": occupant is not None,
                "occupying_flight_id": occupant["flight_id"] if occupant else None,
                "occupying_pairing_id": occupant["pairing_id"] if occupant else None,
                "occupied_from": occupant["boarding_start_time"] if occupant else None,
                "occupied_until": occupant["boarding_end_time"] if occupant else None,
                # Every flight scheduled into this gate this week, not just
                # the one instant above -- "which flights use gate X" asks
                # for the whole week, and there is no single instant that
                # answers that question, so both are always returned rather
                # than making the model guess which framing to ask for.
                "schedule": [
                    {"flight_id": r["flight_id"], "pairing_id": r["pairing_id"],
                     "boarding_start_time": r["boarding_start_time"],
                     "boarding_end_time": r["boarding_end_time"]}
                    for r in gates.by_gate.get(boarding_gate_number, ())
                ],
            }

        # Named by flight. resolve_flight already reports a wrong date by
        # naming the dates the flight actually operates on -- exactly the
        # "correct gate, wrong date" case, so nothing extra is needed for it.
        fid = self.resolve_flight(flight_id, flight_no, date)
        record = gates.by_flight.get(fid)
        if record is None:
            raise ToolError("UNRESOLVED_ENTITY",
                            f"{fid} has no boarding-gate record in the mock dataset")

        actual_gate = record["boarding_gate_number"]
        end = parse(record["boarding_end_time"])
        if delay_minutes:
            end += timedelta(minutes=delay_minutes)

        conflict = None
        if delay_minutes:
            nxt = next_at_gate(gates, actual_gate, fid)
            if nxt is not None:
                nxt_start = parse(nxt["boarding_start_time"])
                if end > nxt_start:
                    conflict = {
                        "flight_id": nxt["flight_id"],
                        "pairing_id": nxt["pairing_id"],
                        "its_boarding_start_time": nxt["boarding_start_time"],
                        "overlap_minutes": round((end - nxt_start).total_seconds() / 60, 1),
                    }

        return {
            "flight_id": fid,
            "pairing_id": record["pairing_id"],
            "date": record["date"],
            "actual_boarding_gate_number": actual_gate,
            "claimed_boarding_gate_number": boarding_gate_number,
            "gate_match": boarding_gate_number is None or boarding_gate_number == actual_gate,
            "boarding_start_time": record["boarding_start_time"],
            "boarding_end_time": record["boarding_end_time"],
            "delay_minutes": delay_minutes,
            "delayed_boarding_end_time": iso(end) if delay_minutes else None,
            "gate_conflict": conflict,
        }

    # -- Tool 4: check_legality ------------------------------------------------

    def check_legality(self, crew_id: str, pairing_id: str | None = None,
                       flight_id: str | None = None, flight_no: str | None = None,
                       date: str | None = None,
                       delay_h: float = 0.0) -> dict[str, Any]:
        self.require("crew", crew_id)
        if not pairing_id:
            # "Move C-2087 onto DX412" names a leg. Crew fly whole pairings,
            # so the leg has to be resolved to the trip it belongs to.
            pairing_id = self.pairing_for_flight(
                self.resolve_flight(flight_id, flight_no, date))
        self.require("pairing", pairing_id)
        c = assess(self.world, crew_id, pairing_id, delay_h)
        subject = self.world.crew[crew_id]
        return {
            "crew_id": crew_id,
            "name": subject.name,
            "rank": subject.rank,
            "pairing_id": pairing_id,
            "legal": c.legal,
            "rules_checked": list(rules.ALL_RULES),
            # Every evaluated rule, not just the blocking ones -- a passing
            # rule's used/limit/headroom (e.g. "9.5h of 12.5h") is real data
            # a question can ask about even when a different rule fails.
            # The concise "show failures, else show all" display filtering
            # already happens downstream in explainer.render_verdicts().
            "verdicts": [asdict(v) | {"status": str(v.status)}
                         for v in c.verdicts[:7]],
            "cost_inr": c.cost_inr,
            "delay_hours": c.delay_hours,
        }

    # -- Tool 16: same_pairing -------------------------------------------------

    # A rank word stated ahead of a name -- "Captain A. Nair" -- disambiguates
    # exactly the same way `entities.STATED_RANK_RE` does for "Captain
    # C-2087": several surnames in this roster repeat across ranks (two
    # "A. Nair"s: a Captain and Cabin Crew), so the rank the controller
    # actually said is real signal, not something to discard before matching.
    # Built from the same shared word list `entities.py` owns, rather than a
    # separate copy that could drift out of sync with it.
    _RANK_PREFIX_RE = re.compile(rf"^({RANK_WORD_RE_FRAGMENT})\s+(.+)$", re.I)

    def _resolve_person(self, value: str) -> dict[str, Any]:
        """A crew record from whatever the controller wrote -- a crew id, or
        a name optionally preceded by a rank ("Captain A. Nair"). Never
        guesses between two same-named, same-rank crew; the caller asks."""
        value = (value or "").strip()
        if not value:
            raise ToolError("UNRESOLVED_ENTITY", "a crew id or name is required")

        if CREW_RE.fullmatch(value):
            self.require("crew", value)
            return self.lookup("crew", {"crew_id": value})[0]

        name, rank_hint = value, None
        if m := self._RANK_PREFIX_RE.match(value):
            word = m.group(1).lower().replace(".", "").replace("/", "").strip()
            rank_hint = RANK_WORD_TO_RANK.get(word) or RANK_WORD_TO_RANK.get(word.replace("-", ""))
            if rank_hint:
                name = m.group(2).strip()

        matches = crew_named(self, name)
        if rank_hint:
            matches = [c for c in matches if c.get("rank") == rank_hint] or matches
        if not matches:
            raise ToolError("UNRESOLVED_ENTITY", f"no crew member named {value!r}")
        if len(matches) > 1:
            options = ", ".join(
                f"{m['crew_id']} ({m['rank']}, {m['base']})" for m in matches)
            raise ToolError(
                "NEEDS_CONFIRMATION",
                f"{len(matches)} crew members are named {name!r}: {options}. "
                f"Say which crew id you mean.")
        return matches[0]

    def same_pairing(self, a: str, b: str) -> dict[str, Any]:
        """Whether two crew members are rostered on the same pairing this
        week, each named by crew id or by name.

        "Is Captain X paired with First Officer Y" is two lookups and a
        comparison, not one -- both people have to be resolved to a crew id
        and both pairing assignments fetched before the question is even
        answerable, and splitting that across separate `lookup` calls is
        exactly where a partial answer ("no data on X") comes from. This
        does the whole thing in one call and returns the comparison
        directly, the same way `check_legality` returns a verdict rather
        than leaving the model to add up rule results itself.
        """
        crew_a, crew_b = self._resolve_person(a), self._resolve_person(b)

        def _pairing(crew: dict[str, Any]) -> str | None:
            try:
                pairing_id, role = self.assignment_for_crew(crew["crew_id"])
            except ToolError:
                return None
            crew["role"] = role
            return pairing_id

        pairing_a, pairing_b = _pairing(crew_a), _pairing(crew_b)
        return {
            "crew_a": {"crew_id": crew_a["crew_id"], "name": crew_a["name"],
                       "rank": crew_a["rank"], "pairing_id": pairing_a,
                       "role_on_pairing": crew_a.get("role")},
            "crew_b": {"crew_id": crew_b["crew_id"], "name": crew_b["name"],
                       "rank": crew_b["rank"], "pairing_id": pairing_b,
                       "role_on_pairing": crew_b.get("role")},
            "same_pairing": pairing_a is not None and pairing_a == pairing_b,
        }

    # -- Tool 5: find_options ---------------------------------------------

    def assignment_for_crew(self, crew_id: str) -> tuple[str, str]:
        """Which pairing a crew member is on, and in what role.

        "C-1042 is sick" names a person, not a trip. The roster knows both
        the pairing and the role, so neither has to be guessed — and the role
        matters: replacing a captain with a first officer is not cover.
        """
        self.require("crew", crew_id)
        for pairing_id, members in self.world.pairing_crew.items():
            for member, role in members:
                if member == crew_id:
                    return pairing_id, role
        raise ToolError("UNRESOLVED_ENTITY",
                        f"{crew_id} is not rostered on any pairing this week")

    def risk_scores(self) -> dict[str, float]:
        """Pre-computed disruption risk, per crew member.

        Provided as input, not modelled here: treat it like a weather
        forecast. So it rides alongside an option as information and never
        enters the ranking — a risk score is not a rule, and letting one
        reorder legal options would be exactly the prediction this system was
        not built to make.
        """
        return {r["crew_id"]: float(r["disruption_risk_score"])
                for r in self.lookup("risk_signals")
                if r.get("disruption_risk_score") is not None}

    def _live_assigned(self, crew_id: str, pairing_id: str, this_dates: set[date],
                        other_pairings: list[str] | None = None
                        ) -> tuple[tuple[str, datetime, datetime], ...]:
        """Commitments made via `commit_decision` since the vendored dataset
        was generated, for pairings whose dates overlap this one. Returns
        `()` (a no-op) when the ledger is off or nothing overlaps.

        `other_pairings` lets the caller pass in an already-fetched list
        (see `find_options`, which loads every candidate's live pairings in
        one bulk query rather than one Postgres round trip per candidate --
        with a pool of 20-30 candidates, that difference was measured at
        10+ seconds versus a few hundred milliseconds for one page). Left
        `None`, this fetches for just this one crew_id instead -- the right
        tradeoff for `commit_decision`'s single-candidate re-check, where
        there is only ever one candidate to ask about.

        Same-day is treated as overlapping rather than computing exact
        report/release windows a second time here -- the safe direction to
        be wrong in is treating two same-day duties as conflicting even if a
        precise clock check might have cleared them, not the reverse."""
        from core_engine import ledger

        if not ledger.enabled() or not this_dates:
            return ()
        if other_pairings is None:
            other_pairings = ledger.live_assignments_for(crew_id, exclude_pairing=pairing_id)
        extra: list[tuple[str, datetime, datetime]] = []
        for other_pairing in other_pairings:
            try:
                other_days = self.world.duty_days(other_pairing)
            except ToolError:
                continue
            if not any(d.date in this_dates for d in other_days):
                continue
            first, last = other_days[0], other_days[-1]
            extra.append((other_pairing, first.report_utc, last.release_utc))
        return tuple(extra)

    @staticmethod
    def _taken_by(crew_id: str, live_extra_by_crew: dict[str, tuple],
                  commitments: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        """If `crew_id` is excluded because of a pairing `_live_assigned`
        added (not one of the ~206 rows the vendored dataset shipped with),
        and that pairing was committed through this console, who committed
        it and to which disruption. `None` for every other exclusion reason
        -- a rating gap, a rest-hour breach, or a conflict against the
        dataset's own original roster, none of which any controller "took".

        `commitments` is pre-fetched once for the whole excluded list (see
        `ledger.commitments_bulk`) -- calling `ledger.commitment_for()` here
        per candidate was the same N+1 pattern `live_assignments_bulk`
        exists to avoid, just discovered a call site later."""
        for other_pairing, _report, _release in live_extra_by_crew.get(crew_id, ()):
            if commitment := commitments.get(other_pairing):
                return commitment
        return None

    def find_options(self, role: str | None = None, pairing_id: str | None = None,
                     flight_id: str | None = None, crew_id: str | None = None,
                     flight_no: str | None = None, date: str | None = None,
                     callout_utc: str | None = None,
                     disruption_id: str | None = None, opened_by: str | None = None,
                     event_type: str = "SICK_CREW",
                     narrative: str | None = None) -> dict[str, Any]:
        """`disruption_id`/`opened_by`/`event_type`/`narrative` are not part
        of the model-facing tool schema (`tools.TOOL_SCHEMAS`) -- the console
        UI calls this directly, bypassing the Resolution Advisor's tool
        loop, to register a disruption and check contention against every
        other one currently open. A plain-English question routed through
        the Advisor never sets them, and `disruption_id` then defaults to
        `pairing_id`."""
        if not pairing_id and crew_id:
            pairing_id, rostered_role = self.assignment_for_crew(crew_id)
            role = role or rostered_role
        # "I need a pilot for DX401" names a flight by its NUMBER. A flight_id
        # is DX401-2026-09-15; the number alone is what a controller says, and
        # it needs the same resolution check_legality and check_gate already
        # do — including asking which date when the number flies on several.
        if not pairing_id and (flight_id or flight_no):
            flight_id = self.resolve_flight(flight_id, flight_no, date)
        if not pairing_id and flight_id:
            pairing_id = self.pairing_for_flight(flight_id)
        if pairing_id:
            self.require("pairing", pairing_id)
        if not pairing_id:
            raise ToolError("UNRESOLVED_ENTITY",
                            "find_options needs a pairing_id, flight_id or crew_id")
        if not role:
            raise ToolError("UNRESOLVED_ENTITY",
                            "find_options needs a role, or a crew_id to infer it from")

        world = self.world
        days = world.duty_days(pairing_id)
        first_day = days[0].date
        incumbent = {c for c, r in world.pairing_crew.get(pairing_id, ()) if r == role}

        pool = [c for c in world.crew.values()
                if c.rank == role and c.crew_id not in incumbent]

        this_pairing_dates = {d.date for d in days}

        # One Postgres round trip for the whole candidate pool, not one per
        # candidate -- see `_live_assigned`'s docstring for what the naive
        # version costs.
        from core_engine import ledger
        bulk_live = ledger.live_assignments_bulk(
            [c.crew_id for c in pool], exclude_pairing=pairing_id)

        assessed: list[Candidate] = []
        live_extra_by_crew: dict[str, tuple] = {}
        for crew in pool:
            try:
                extra = self._live_assigned(
                    crew.crew_id, pairing_id, this_pairing_dates,
                    other_pairings=bulk_live.get(crew.crew_id, []))
                if extra:
                    live_extra_by_crew[crew.crew_id] = extra
                assessed.append(assess(world, crew.crew_id, pairing_id, extra_assigned=extra))
            except ToolError:
                continue

        legal = [c for c in assessed if c.legal]
        excluded = [c for c in assessed if not c.legal]

        # Rank by cost then delay.
        legal.sort(key=lambda c: (c.cost_inr, c.delay_hours, c.crew_id))

        risk = self.risk_scores()
        options = []
        for i, c in enumerate(legal, start=1):
            who = world.crew[c.crew_id]
            options.append({
                "action": c.action(who.rank, who.name),
                "crew_id": c.crew_id, "legal": True,
                "name": who.name, "seniority": who.seniority,
                "base": who.base, "reachability_minutes": who.reachability_minutes,
                # Provided input, treated like a weather forecast: reported
                # beside the option, never allowed to change its legality or
                # its rank.
                "disruption_risk_score": risk.get(c.crew_id),
                "rules_checked": list(rules.ALL_RULES),
                "cost_inr": c.cost_inr, "delay_hours": c.delay_hours, "rank": i,
                "cost_breakdown": c.cost_breakdown,
                "resilience": resilience_score(world, c, role, who.base, first_day),
                "verdicts": [], "blast_radius": 0, "unlock": None,
            })

        # Cancelling is always available and almost always wrong; include it
        # so the comparison is explicit rather than implied.
        n_flights = len(world.pairing_flights.get(pairing_id, []))
        cancel_cost_inr = n_flights * world.costs.cancellation_per_flight
        options.append({
            "action": f"Cancel all {n_flights} flights of the pairing",
            "crew_id": None, "legal": True, "rules_checked": [],
            "cost_inr": cancel_cost_inr,
            "delay_hours": 0.0, "rank": len(options) + 1,
            "cost_breakdown": {"cancellation": cancel_cost_inr},
            "verdicts": [], "blast_radius": n_flights, "unlock": None,
        })

        # A policy-backed ranking across the four strategies a controller
        # actually chooses between, one representative each -- the cheapest
        # legal candidate in that strategy, since `legal` is already
        # cost-sorted so the first one seen per strategy is the cheapest.
        # A strategy with no legal candidate is left out rather than shown as
        # unavailable: reporting "no reserve is legal" as a ranked option
        # would be a claim this data doesn't support.
        best_per_strategy: dict[str, Candidate] = {}
        for c in legal:
            if c.strategy not in best_per_strategy:
                best_per_strategy[c.strategy] = c

        strategies = []
        for i, c in enumerate(best_per_strategy.values(), start=1):
            who = world.crew[c.crew_id]
            # The strategy label already says the kind (reserve/day-off/
            # deadhead) -- repeating it via the full `.action()` phrasing
            # would say "Reserve callout — Assign ... (reserve callout)".
            who_text = f"{who.rank} {who.name} ({c.crew_id})" if who.name else f"{who.rank} {c.crew_id}"
            action = f"Assign {who_text}"
            if c.deadhead:
                action += f" — first departure delayed ~{c.delay_hours}h"
            strategies.append({
                "strategy": c.strategy, "strategy_label": STRATEGY_LABELS[c.strategy],
                "action": action, "crew_id": c.crew_id,
                "legal": True, "name": who.name, "cost_inr": c.cost_inr,
                "delay_hours": c.delay_hours, "rank": i,
            })
        strategies.append({
            "strategy": "cancel", "strategy_label": STRATEGY_LABELS["cancel"],
            "action": f"Cancel all {n_flights} flights of the pairing",
            "crew_id": None, "legal": True, "cost_inr": cancel_cost_inr,
            "delay_hours": 0.0, "rank": len(strategies) + 1,
        })

        # Computed here, not in the renderer: a derived figure like "81x the
        # cost of covering" is exactly what the verifier checks for arithmetic
        # against sourced numbers, so it has to be sourced itself.
        cheapest = next((o["cost_inr"] for o in options if o["crew_id"]), 0)
        cancel_cost = options[-1]["cost_inr"]
        recommended = next((o for o in options if o["crew_id"]), None)

        tiers = sorted({o["cost_inr"] for o in options if o["crew_id"]})

        # Four ways to read the same legal pool. "Balanced" applies this
        # system's stated ordering -- safety/legality first (already true of
        # everything in `options`), then network-criticality, passenger
        # impact and cascading risk (properties of *which disruption* this
        # is, constant across every candidate in a single find_options call,
        # so they can't separate candidates here -- they matter when ranking
        # *between* open disruptions, see `commit_decision`/contention) --
        # and cost last. That makes Balanced reduce to (resilience, cost)
        # within one call, same tiebreak as Most Resilient: expected, not a
        # bug, until cross-disruption ranking is layered on top.
        crew_options = [o for o in options if o["crew_id"]]
        policies = {}
        if crew_options:
            def _pick(key, reason):
                best = min(crew_options, key=key)
                return {**best, "why": reason}

            policies["cheapest"] = _pick(
                lambda o: (o["cost_inr"], o["delay_hours"], o["crew_id"]),
                "Lowest direct cost among legal options.")
            policies["fastest"] = _pick(
                lambda o: (o["delay_hours"], o["cost_inr"], o["crew_id"]),
                "Lowest delay among legal options.")
            policies["most_resilient"] = _pick(
                lambda o: (-o["resilience"], o["cost_inr"], o["crew_id"]),
                "Preserves the most on-call reserve buffer for this rank and base.")
            policies["balanced"] = _pick(
                lambda o: (-o["resilience"], o["cost_inr"], o["crew_id"]),
                "This system's stated ordering — legality, then network and "
                "passenger impact, then cost last — reduces to reserve "
                "resilience first within a single disruption's option list.")

        # Cross-disruption contention: register this disruption's current
        # candidate pool, then ask what else already open also wants them.
        # A no-op when LEDGER_DATABASE_URL isn't set (see core_engine/ledger.py),
        # and equally a no-op when there's no real disruption_id -- a plain
        # chat question ("who could cover P-2291?") is an inquiry, not a
        # controller opening a disruption, and registering it anyway would
        # create a phantom disruption keyed by the bare pairing_id that then
        # "contends" with the real one for the exact same crew, purely
        # because both track the same pairing under two different ids.
        from core_engine import ledger

        disruption_key = disruption_id or pairing_id
        contention: dict[str, list[dict[str, Any]]] = {}
        if ledger.enabled() and disruption_id:
            contention = ledger.register_and_check_contention(
                disruption_id=disruption_id, pairing_id=pairing_id, role=role,
                event_type=event_type,
                narrative=narrative or f"{role} needed for {pairing_id}",
                opened_by=opened_by or "unknown",
                candidates=[o for o in options if o["crew_id"]],
            )
            for o in options:
                if o["crew_id"] in contention:
                    o["contended_by"] = contention[o["crew_id"]]
            for s in strategies:
                if s["crew_id"] in contention:
                    s["contended_by"] = contention[s["crew_id"]]

        n_contended = sum(1 for o in options if o["crew_id"] and o["crew_id"] in contention)
        n_with_candidate = sum(1 for o in options if o["crew_id"])

        all_other_pairings = {p for extra in live_extra_by_crew.values() for p, _, _ in extra}
        commitments = ledger.commitments_bulk(list(all_other_pairings)) if ledger.enabled() else {}

        return {
            "pairing_id": pairing_id,
            "disruption_id": disruption_key,
            "role": role,
            "recommended": recommended,
            "next_tier_cost_inr": tiers[1] if len(tiers) > 1 else 0,
            "next_tier_premium_inr": (tiers[1] - tiers[0]) if len(tiers) > 1 else 0,
            "cancellation_multiple": round(cancel_cost / cheapest) if cheapest else 0,
            "equal_cost_alternatives": sum(
                1 for o in options if o["crew_id"] and o["cost_inr"] == cheapest) - 1,
            "funnel": self._funnel(assessed, len(pool)),
            "options": options,
            "strategies": strategies,
            "policies": policies,
            "near_misses": [],
            "excluded": [
                {"crew_id": c.crew_id,
                 "name": world.crew[c.crew_id].name,
                 "rank": world.crew[c.crew_id].rank,
                 "reason": "; ".join(v.detail for v in rules.blocking(c.verdicts)),
                 "rules": [v.rule_id for v in rules.blocking(c.verdicts)],
                 "taken_by": self._taken_by(c.crew_id, live_extra_by_crew, commitments)}
                for c in excluded
            ],
            "contention_summary": (
                f"{n_with_candidate - n_contended} of {n_with_candidate} option(s) "
                f"are uncontended." if ledger.enabled() and n_with_candidate else None
            ),
        }

    def commit_decision(self, disruption_id: str, pairing_id: str, crew_id: str, role: str,
                        committed_by: str, accepted_rank: int | None = None,
                        presented_options: list[dict[str, Any]] | None = None,
                        override_reason: str | None = None
                        ) -> dict[str, Any]:
        """Human-confirmed only (see the schema comment in tools.py). Re-checks
        legality against the live state one more time immediately before
        writing -- the options shown to the controller may be seconds old,
        and another desk could have committed the same person in between."""
        from core_engine import ledger

        if not ledger.enabled():
            raise ToolError("INTERNAL", "LEDGER_DATABASE_URL is not set; nothing to commit to.")

        world = self.world
        self.require("crew", crew_id)
        self.require("pairing", pairing_id)
        days = world.duty_days(pairing_id)
        this_dates = {d.date for d in days}

        extra = self._live_assigned(crew_id, pairing_id, this_dates)
        candidate = assess(world, crew_id, pairing_id, extra_assigned=extra)
        if not candidate.legal:
            blocking = rules.blocking(candidate.verdicts)
            reason = "; ".join(v.detail for v in blocking) or "no longer legal"
            raise ToolError(
                "NO_LEGAL_OPTION",
                f"{crew_id} is no longer a legal cover for {pairing_id}: {reason}. "
                f"Someone else likely committed them to an overlapping pairing "
                f"just now — refresh and pick a different option.",
            )

        try:
            ledger.commit_decision(
                disruption_id=disruption_id, pairing_id=pairing_id, crew_id=crew_id,
                role=role, committed_by=committed_by, accepted_rank=accepted_rank,
                presented_options=presented_options or [],
                override_reason=override_reason,
            )
        except ledger.CommitError as exc:
            raise ToolError("INTERNAL", str(exc)) from exc

        who = world.crew[crew_id]
        return {
            "pairing_id": pairing_id, "disruption_id": disruption_id,
            "crew_id": crew_id, "name": who.name, "rank": who.rank,
            "committed_by": committed_by, "status": "committed",
        }

    def commit_joint_decisions(self, assignments: list[dict[str, Any]],
                               committed_by: str) -> list[dict[str, Any]]:
        """A joint plan's assignments, committed together or not at all.

        Every assignment is re-checked for legality against the live state
        first -- same re-check `commit_decision` does for a single pairing,
        repeated per assignment here. If any one of them is no longer legal
        (another desk committed that person to something else in the
        seconds since the plan was previewed), the whole call raises before
        `ledger.commit_joint` writes anything, so a partially-applied joint
        plan can never exist: "one approval, one transaction."
        """
        from core_engine import ledger

        if not ledger.enabled():
            raise ToolError("INTERNAL", "LEDGER_DATABASE_URL is not set; nothing to commit to.")

        world = self.world
        checked: list[dict[str, Any]] = []
        for a in assignments:
            crew_id, pairing_id = a["crew_id"], a["pairing_id"]
            self.require("crew", crew_id)
            self.require("pairing", pairing_id)
            this_dates = {d.date for d in world.duty_days(pairing_id)}

            extra = self._live_assigned(crew_id, pairing_id, this_dates)
            candidate = assess(world, crew_id, pairing_id, extra_assigned=extra)
            if not candidate.legal:
                blocking = rules.blocking(candidate.verdicts)
                reason = "; ".join(v.detail for v in blocking) or "no longer legal"
                raise ToolError(
                    "NO_LEGAL_OPTION",
                    f"Joint plan aborted, nothing committed: {crew_id} is no longer "
                    f"a legal cover for {pairing_id}: {reason}. Re-plan and try again.",
                )
            checked.append(a)

        try:
            ledger.commit_joint(checked, committed_by)
        except ledger.CommitError as exc:
            raise ToolError("INTERNAL", str(exc)) from exc

        return [
            {"pairing_id": a["pairing_id"], "disruption_id": a["disruption_id"],
             "crew_id": a["crew_id"], "name": world.crew[a["crew_id"]].name,
             "rank": world.crew[a["crew_id"]].rank, "committed_by": committed_by,
             "status": "committed"}
            for a in checked
        ]

    @staticmethod
    def _funnel(assessed: list[Candidate], considered: int) -> list[dict[str, Any]]:
        dropped = Counter(drop_stage(c.verdicts)[0] for c in assessed if not c.legal)
        reasons = {drop_stage(c.verdicts)[0]: drop_stage(c.verdicts)[1]
                   for c in assessed if not c.legal}

        funnel = [{"stage": "considered", "count": considered, "dropped": 0, "reason": ""}]
        remaining = considered
        for stage in FUNNEL_ORDER[1:-1]:
            if not (n := dropped.get(stage, 0)):
                continue
            remaining -= n
            funnel.append({"stage": stage, "count": remaining,
                           "dropped": n, "reason": reasons.get(stage, "")})
        funnel.append({"stage": "legal", "count": remaining, "dropped": 0, "reason": ""})
        return funnel

    # -- Tool 6: ripple -----------------------------------------------------

    def ripple(self, event: dict[str, Any]) -> dict[str, Any]:
        """Blast radius of a disruption, named however the controller
        actually named it -- a pairing or crew id directly, a flight
        (id, or number+date, resolved the same way `check_legality`/
        `find_options` already do), or a station closure window, which
        can touch several pairings at once and returns their combined
        blast radius rather than requiring one `ripple` call per pairing.
        Resolving these here, instead of requiring the caller to already
        have chased flight -> pairing (or station -> every affected
        pairing) through separate `lookup` calls first, is exactly the
        pattern `check_legality`'s own `flight_no`+`date` handling already
        uses -- the model naming what the controller said, not the id it
        implies.
        """
        event = event or {}
        pairing_id = event.get("pairing_id")
        if pairing_id:
            self.require("pairing", pairing_id)
            return self._ripple_one(pairing_id)

        if cid := event.get("crew_id"):
            self.require("crew", cid)
            pairing_id = next(
                (p for p, members in self.world.pairing_crew.items()
                 if any(c == cid for c, _ in members)), None)
            if pairing_id:
                return self._ripple_one(pairing_id)
            raise ToolError("UNRESOLVED_ENTITY",
                            f"{cid} is not rostered on any pairing this week")

        if event.get("flight_id") or event.get("flight_no"):
            flight_id = self.resolve_flight(
                event.get("flight_id"), event.get("flight_no"), event.get("date"))
            return self._ripple_one(self.pairing_for_flight(flight_id))

        station = event.get("station")
        if station and (event.get("from_utc") or event.get("to_utc")):
            from_utc = event.get("from_utc") or "0000-00-00"
            to_utc = event.get("to_utc") or "9999-99-99"
            affected_pairings: dict[str, None] = {}  # ordered set
            for f in self._rows("flights"):
                if station not in (f.get("dep_station"), f.get("arr_station")):
                    continue
                instant = f.get("dep_utc") if f.get("dep_station") == station else f.get("arr_utc")
                if not (from_utc <= str(instant) <= to_utc):
                    continue
                try:
                    affected_pairings[self.pairing_for_flight(f["flight_id"])] = None
                except ToolError:
                    continue
            extra = {"station": station, "from_utc": event.get("from_utc"),
                     "to_utc": event.get("to_utc")}
            return self._ripple_many(affected_pairings, extra)

        aircraft = event.get("aircraft") or event.get("tail_id")
        if aircraft:
            date = event.get("date")
            from_utc = event.get("from_utc") or "0000-00-00"
            to_utc = event.get("to_utc") or "9999-99-99"
            affected_pairings = {}
            for f in self._rows("flights"):
                if f.get("aircraft") != aircraft:
                    continue
                if date and f.get("date") != date:
                    continue
                if not (from_utc <= str(f.get("dep_utc")) <= to_utc):
                    continue
                try:
                    affected_pairings[self.pairing_for_flight(f["flight_id"])] = None
                except ToolError:
                    continue
            extra = {"aircraft": aircraft, "date": date,
                     "from_utc": event.get("from_utc"), "to_utc": event.get("to_utc")}
            return self._ripple_many(affected_pairings, extra)

        raise ToolError(
            "UNRESOLVED_ENTITY",
            "ripple needs a pairing_id, crew_id, flight (id, or number+date), "
            "an aircraft (optionally with a date or from_utc/to_utc window), "
            "or a station with a from_utc/to_utc window")

    def _ripple_many(self, affected_pairings: dict[str, None],
                      extra: dict[str, Any]) -> dict[str, Any]:
        """Aggregate `_ripple_one` across every pairing an event touches."""
        if not affected_pairings:
            return {**extra, "affected_pairings": [],
                    "uncovered_flights": [], "at_risk_flights": [], "passengers": 0,
                    "blast_radius": {"nodes": 0, "flights": 0, "aircraft": 0,
                                     "passengers": 0, "edges": []}}
        per_pairing = [self._ripple_one(p) for p in affected_pairings]
        return {
            **extra,
            "affected_pairings": list(affected_pairings),
            "uncovered_flights": [f for r in per_pairing for f in r["uncovered_flights"]],
            "at_risk_flights": [f for r in per_pairing for f in r["at_risk_flights"]],
            "passengers": sum(r["passengers"] for r in per_pairing),
            "blast_radius": {
                "nodes": sum(r["blast_radius"]["nodes"] for r in per_pairing),
                "flights": sum(r["blast_radius"]["flights"] for r in per_pairing),
                "aircraft": len(per_pairing),
                "passengers": sum(r["blast_radius"]["passengers"] for r in per_pairing),
                "edges": [e for r in per_pairing for e in r["blast_radius"]["edges"]],
            },
        }

    def _ripple_one(self, pairing_id: str) -> dict[str, Any]:
        """The blast radius of losing crew on a single pairing -- the
        original, single-pairing shape of this tool, factored out so a
        station closure (below) can compute it once per affected pairing
        and combine the results, instead of only ever answering for one."""
        world = self.world
        days = world.duty_days(pairing_id)
        by_day: dict[Any, list[dict]] = {}
        for f in world.pairing_flights.get(pairing_id, []):
            by_day.setdefault(f["date"], []).append(f)

        first, rest = days[0], days[1:]
        direct = [f["flight_id"] for f in by_day.get(first.date.isoformat(), [])]
        # Later days of a pairing are at risk because it overnights away from
        # base — replacing day 1 alone strands the aircraft where it slept.
        at_risk = [f["flight_id"] for d in rest for f in by_day.get(d.date.isoformat(), [])]
        pax = sum(int(f["seats"]) for f in by_day.get(first.date.isoformat(), []))

        return {
            "pairing_id": pairing_id,
            "uncovered_flights": direct,
            "at_risk_flights": at_risk,
            "passengers": pax,
            "blast_radius": {
                "nodes": len(direct) + len(at_risk) + len(rest),
                "flights": len(direct) + len(at_risk),
                "aircraft": 1,
                "passengers": pax,
                "edges": ([{"from": pairing_id, "to": f, "kind": "direct"} for f in direct]
                          + [{"from": pairing_id, "to": f, "kind": "orphaned-day"}
                             for f in at_risk]),
            },
        }

    # -- Tool 7: joint_plan ---------------------------------------------------

    def joint_plan(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Cost-minimal cover across simultaneous disruptions.

        The constraint that makes this joint is disjointness: one crew member
        cannot cover two pairings at once. Without it the same cheap reserve
        gets assigned to both, producing an infeasible plan.

        Ties are normal, so the count is reported rather than one assignment
        being presented as uniquely right.
        """
        pairings = [e.get("pairing_id") for e in events or [] if e.get("pairing_id")]
        if len(pairings) < 2:
            raise ToolError("UNRESOLVED_ENTITY",
                            "joint_plan needs at least two pairing_ids")

        role = (events[0] or {}).get("role", "Captain")
        per = [self.find_options(role=role, pairing_id=p)["options"] for p in pairings]
        crewed = [[o for o in opts if o["crew_id"]] for opts in per]

        best: tuple[int, tuple[dict, ...]] | None = None
        ties = 0
        for combo in itertools.product(*(opts[:12] for opts in crewed)):
            ids = [o["crew_id"] for o in combo]
            if len(set(ids)) != len(ids):
                continue  # disjointness
            total = sum(o["cost_inr"] for o in combo)
            if best is None or total < best[0]:
                best, ties = (total, combo), 1
            elif total == best[0]:
                ties += 1

        if best is None:
            raise ToolError("NO_LEGAL_OPTION", "no disjoint assignment covers all pairings")

        total, combo = best
        return {
            "total_cost_inr": total,
            "equal_cost_alternatives": ties,
            **{f"assign_{p}": o for p, o in zip(pairings, combo)},
            "note": ("The same crew member cannot cover two pairings. Where several "
                     "assignments cost the same they are equally correct."),
        }

    # -- Tool 8: simulate -----------------------------------------------------

    def simulate(self, event: dict[str, Any]) -> dict[str, Any]:
        """What-if, expressed as the difference a perturbation makes."""
        delay = float((event or {}).get("delay_hours") or 0)
        pairing_id = (event or {}).get("pairing_id")
        if not pairing_id:
            raise ToolError("UNRESOLVED_ENTITY", "simulate needs a pairing_id")
        # A pairing_id that doesn't exist must fail loudly, not fall through
        # to `world.pairing_crew.get(pairing_id, ())` -- that silently
        # returns an empty crew list, which produces the exact same
        # `changed: []` "nothing breaks" result as a REAL pairing that
        # genuinely has no impact. Those two cases must never look the same.
        self.require("pairing", pairing_id)

        world = self.world
        changed = []
        for crew_id, role in world.pairing_crew.get(pairing_id, ()):
            before = assess(world, crew_id, pairing_id, 0.0)
            after = assess(world, crew_id, pairing_id, delay)
            if before.legal != after.legal:
                changed.append({
                    "crew_id": crew_id, "role": role,
                    "legal_before": before.legal, "legal_after": after.legal,
                    "detail": "; ".join(v.detail for v in rules.blocking(after.verdicts)),
                })
        return {"pairing_id": pairing_id, "delay_hours": delay, "changed": changed}


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)
