#!/usr/bin/env python3
"""Crew Ops Console -- a small API + static server in front of the
challenge-1-build engine, for a browser-based demo of everything built in
this bonus track: resilience-scored recommendations, the four decision
policies, and cross-disruption contention backed by the Postgres ledger.

Stdlib only, matching this repo's own `devui/`-style convention (no new
framework dependency for a debug/demo console). Three controller identities
share one running server and one Postgres ledger -- open this URL in three
browser tabs with a different `?controller=` each to see contention appear
live across them, exactly like the reference UI this was modeled on.

Usage:
    python server.py                  # http://localhost:8600
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Matched against the whole (punctuation-stripped) query, not a substring --
# "hi" answers this; "hi, is C-1042 on reserve" does not, and falls through
# to the real pipeline as it should.
_GREETING_RE = re.compile(
    r"(hi+|hey+|hello+|yo|sup|howdy|good (morning|afternoon|evening)|"
    r"thanks?( you)?|thank you|ty|cheers)",
    re.IGNORECASE,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = REPO_ROOT / "challenge-1-build"
sys.path.insert(0, str(BUILD_DIR))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")

import config
import pii
import pipeline
import router
import verifier
from core_engine import ledger
from core_engine.port import JsonToolPort
from explainer import render as render_narrative
from schemas import AdvisorResponse
from tools import ToolError, dispatch

# `agents.py` imports `azure-ai-projects`/`azure-identity`/`openai` at module
# level -- packages this console has never required before, since everything
# else here is deterministic. A plain top-level import would crash this
# entire console on startup for anyone who hasn't installed the full
# repo-root `requirements.txt` (a bare venv for just the deterministic
# console, say), even though they'd never touch the Advisor Agent fallback.
# Caught here, once, so the rest of the console works exactly as before
# either way; `_init_advisor_agents()` below is what actually decides
# whether the fallback is live.
try:
    from agents import (
        PROJECT_CONNECTION_STRING, ExplainerAgent, ResolutionAdvisorAgent,
        TriageAgent, answer_question,
    )
except ImportError:
    PROJECT_CONNECTION_STRING = None
    ExplainerAgent = ResolutionAdvisorAgent = TriageAgent = answer_question = None

STATIC = Path(__file__).resolve().parent / "static"
PORT = 8600

CONTROLLERS = config.CONTROLLERS
"""Single-sourced from `config.py` so a chat question answered by
`core_engine.port.JsonToolPort.list_controllers()` can never disagree with
what this console's own UI shows."""

# The vendored dataset's own scenario set is the disruption catalog -- no
# fabricated disruptions, everything here is one of `data/scenarios.json`'s
# six engineered cases (S6's two simultaneous events are split into two
# disruption records, since each competes for cover independently).
DISRUPTIONS: list[dict[str, object]] = [
    {"id": "S1", "title": "ATR captain sick call", "event_type": "SICK_CREW",
     "crew_id": "C-3231", "pairing_id": "P-2224"},
    {"id": "S2", "title": "Flagship: Captain C-1042 sick, 2-day pairing", "event_type": "SICK_CREW",
     "crew_id": "C-1042", "pairing_id": "P-2291"},
    {"id": "S3", "title": "BLR station closure 08:00-14:00Z, 17 Sep", "event_type": "STATION_CLOSURE",
     "crew_id": None, "pairing_id": None},
    {"id": "S4", "title": "Tech delay cascades into an FDP breach", "event_type": "DELAY",
     "crew_id": None, "pairing_id": None},
    {"id": "S5", "title": "Certification lapse discovered pre-flight", "event_type": "CERT_EXPIRY",
     "crew_id": "C-5417", "pairing_id": "P-2213"},
    {"id": "S6A", "title": "Two simultaneous sick calls -- VT-DXA captain", "event_type": "MULTI_SICK",
     "crew_id": "C-3940", "pairing_id": "P-2205"},
    {"id": "S6B", "title": "Two simultaneous sick calls -- VT-DXB captain", "event_type": "MULTI_SICK",
     "crew_id": "C-1938", "pairing_id": "P-2212"},
]
for _d in DISRUPTIONS:
    _d["desk"] = config.SCENARIO_DESKS[_d["id"]]

_NARRATIVES = {sc["scenario_id"]: sc for sc in json.loads((BUILD_DIR / "data" / "scenarios.json").read_text())}
_NARRATIVES["S6A"] = _NARRATIVES["S6B"] = _NARRATIVES["S6"]

for _d in DISRUPTIONS:
    _event = _NARRATIVES[_d["id"]]["event"]
    _d["narrative"] = _event.get("narrative") or next(
        (e.get("narrative", "") for e in _event.get("events", [])), "")

PORT_INSTANCE = JsonToolPort()

# Set once by `_init_advisor_agents()` at server startup, and only if
# PROJECT_CONNECTION_STRING is configured -- stay None otherwise, so `_ask()`
# keeps giving its honest "needs the Advisor Agent" decline instead of
# crashing when this console is run with no Azure Foundry deployment.
_TRIAGE_AGENT: TriageAgent | None = None
_ADVISOR_AGENT: ResolutionAdvisorAgent | None = None
_EXPLAINER_AGENT: ExplainerAgent | None = None


def _init_advisor_agents() -> None:
    """Create the three Foundry agents once, at server startup, not per
    request -- agent creation is a network round trip to Foundry, and the
    whole point of doing this here is that a controller's first unclassified
    question doesn't pay for it. Failure here (no deployment, bad
    credentials, Foundry unreachable) is caught and logged, not raised: this
    console must keep serving the deterministic pipeline either way."""
    global _TRIAGE_AGENT, _ADVISOR_AGENT, _EXPLAINER_AGENT
    if not PROJECT_CONNECTION_STRING:
        print("PROJECT_CONNECTION_STRING not set -- chat falls back to "
              "deterministic-only for unclassified questions.")
        return
    try:
        triage = TriageAgent()
        triage.create()
        advisor = ResolutionAdvisorAgent(PORT_INSTANCE)
        advisor.create()
        explainer_agent = ExplainerAgent()
        explainer_agent.create()
    except Exception:
        print("Advisor Agent setup failed -- chat falls back to deterministic-only:")
        traceback.print_exc()
        return
    _TRIAGE_AGENT, _ADVISOR_AGENT, _EXPLAINER_AGENT = triage, advisor, explainer_agent
    print(f"Advisor Agent ready: {advisor.agent.name} (+ Triage, Explainer) "
          f"-- unclassified chat questions now get real model reasoning.")


_GATES_BY_PAIRING: dict[str, list[dict[str, object]]] = {}
for _g in json.loads((BUILD_DIR / "data" / "boarding_gates.json").read_text()):
    _GATES_BY_PAIRING.setdefault(_g["pairing_id"], []).append(_g)


def _requirement_detail(pairing_id: str, unavailable_crew_id: str, role: str) -> dict[str, object]:
    """Everything the reference UI's "What needs covering" card and its
    accordions show, pulled straight from the same vendored dataset
    `find_options` already reasons over -- no new numbers invented for the
    UI, just more of what was already computed or already sitting in
    `data/*.json` (`ripple`'s own ledger-free seat count, the roster, the
    reserve pool, the boarding-gate list) laid out the way the reference app
    lays it out.

    One number that dataset genuinely doesn't have -- a real passenger/
    booking count -- is left out rather than approximated from seats, same
    call as the Command Center's own `seats_at_risk` already makes."""
    world = PORT_INSTANCE.world
    days = world.duty_days(pairing_id)
    flights = world.pairing_flights.get(pairing_id, [])
    unavailable = world.crew[unavailable_crew_id]

    stations = sorted({f["dep_station"] for f in flights} | {f["arr_station"] for f in flights})
    reserve_capacity = [
        {"base": base, "count": world.reserve_pool_size(role, base, days[0].date)}
        for base in sorted({unavailable.base} | set(stations))
    ]

    crew_roster = [
        {"crew_id": cid, "role": crew_role, "name": world.crew[cid].name,
         "base": world.crew[cid].base,
         "status": "unavailable" if cid == unavailable_crew_id else "complement incomplete"}
        for cid, crew_role in world.pairing_crew.get(pairing_id, [])
    ]

    gates = [
        {"flight_id": g["flight_id"], "gate": g["boarding_gate_number"],
         "boarding_start_utc": g["boarding_start_time"], "boarding_end_utc": g["boarding_end_time"]}
        for g in _GATES_BY_PAIRING.get(pairing_id, [])
    ]

    ripple = PORT_INSTANCE.ripple({"pairing_id": pairing_id})
    seats_at_risk = sum(int(f["seats"]) for f in flights)

    return {
        "role": role,
        "aircraft_type": days[0].aircraft_type if days else None,
        "base": unavailable.base,
        "unavailable_crew_id": unavailable_crew_id,
        "unavailable_crew_name": unavailable.name,
        "duty_dates": [d.date.isoformat() for d in days],
        "flights": [
            {"flight_id": f["flight_id"], "flight_no": f["flight_no"], "date": f["date"],
             "dep_station": f["dep_station"], "arr_station": f["arr_station"],
             "dep_utc": f["dep_utc"], "arr_utc": f["arr_utc"], "seats": f["seats"],
             "uncrewed": f["flight_id"] in ripple["uncovered_flights"] + ripple["at_risk_flights"]}
            for f in flights
        ],
        "duty_periods": [
            {"pairing_id": pairing_id, "date": d.date.isoformat(),
             "scheduled_hours": d.fdp_hours, "n_sectors": d.n_sectors}
            for d in days
        ],
        "crew_roster": crew_roster,
        "gates": gates,
        "reserve_capacity": reserve_capacity,
        "seats_at_risk": seats_at_risk,
        "flights_affected": len(flights),
        "stations_affected": len(stations),
    }


def _json(obj: object) -> bytes:
    def default(o: object) -> object:
        if isinstance(o, (date, datetime)):
            return o.isoformat()
        raise TypeError(f"not serialisable: {o!r}")
    return json.dumps(obj, default=default).encode("utf-8")


def _disruption_status(disruption_id: str) -> str:
    for row in ledger.list_open_disruptions():
        if row["disruption_id"] == disruption_id:
            return row["status"]
    return "open"  # never opened yet -- treated as open


def _resolve_disruption(disruption_id: str) -> dict[str, object]:
    return next(d for d in DISRUPTIONS if d["id"] == disruption_id)


def _load_disruption(disruption_id: str, controller: str) -> dict[str, object]:
    """Everything one disruption detail page needs: the recommendation
    engine's output, or -- for an event with no crew_id (a station closure,
    an aircraft delay) -- the honest "no cover requirement can be derived"
    case the reference UI also shows rather than fabricating one."""
    d = _resolve_disruption(disruption_id)
    status = _disruption_status(disruption_id)
    base = {
        "disruption_id": disruption_id, "title": d["title"], "event_type": d["event_type"],
        "narrative": d["narrative"], "pairing_id": d["pairing_id"],
        "status": status,
    }
    if not d["crew_id"]:
        base["no_cover_requirement"] = (
            "No crew cover requirement can be derived from this event -- it "
            "has no unavailable crew member to replace."
        )
        return base

    # Use the scenario's own stated pairing_id, not `assignment_for_crew` --
    # crew fly several pairings across the week, and resolving by crew_id
    # alone can silently pick a different one than the scenario names.
    role = PORT_INSTANCE.world.crew[d["crew_id"]].rank
    result = PORT_INSTANCE.find_options(
        pairing_id=d["pairing_id"], role=role,
        disruption_id=disruption_id, opened_by=controller,
        event_type=d["event_type"], narrative=d["narrative"],
    )
    base.update(result)
    base["requirement"] = _requirement_detail(d["pairing_id"], d["crew_id"], role)

    balanced = (result.get("policies") or {}).get("balanced")
    cheapest = (result.get("policies") or {}).get("cheapest")
    if balanced:
        # `options` is already cost-sorted (port.py's `legal.sort(...)`), so
        # the first entry that isn't Balanced's own pick is its runner-up.
        crew_options = [o for o in result["options"] if o["crew_id"]]
        runner_up = next((o for o in crew_options if o["crew_id"] != balanced["crew_id"]), None)
        base["balanced_comparison"] = {
            "premium_over_cheapest_inr": result.get("next_tier_premium_inr", 0),
            "cheapest_crew_id": cheapest["crew_id"] if cheapest else None,
            "cheapest_cost_inr": cheapest["cost_inr"] if cheapest else None,
            "uses_reserve": "reserve" in (balanced.get("action") or "").lower(),
            "runner_up": (
                {"action": runner_up["action"], "cost_inr": runner_up["cost_inr"],
                 "delay_hours": runner_up["delay_hours"], "resilience": runner_up["resilience"]}
                if runner_up else None
            ),
        }

    if base["status"] == "resolved":
        base["commitment"] = ledger.commitment_for(d["pairing_id"])

    # "Decisions already made" -- every commitment that's already shaped
    # this page's own numbers, gathered from where they naturally surface
    # rather than a fresh query: this disruption's own resolution (above),
    # plus whichever committed candidates now show up in `excluded` as
    # `taken_by` someone. No fabricated timeline, just what's already here.
    seen_pairings: set[str] = set()
    related: list[dict[str, object]] = []
    if base.get("commitment"):
        related.append(base["commitment"])
        seen_pairings.add(base["commitment"]["pairing_id"])
    for entry in base.get("excluded", []):
        taken = entry.get("taken_by")
        if taken and taken["pairing_id"] not in seen_pairings:
            seen_pairings.add(taken["pairing_id"])
            related.append(taken)
    base["related_decisions"] = related
    return base


class Handler(BaseHTTPRequestHandler):
    # Without this, BaseHTTPRequestHandler defaults to HTTP/1.0 while still
    # accepting a client's HTTP/1.1 keep-alive attempt -- a real browser (and
    # Node's fetch) then tries to reuse the same connection for a second
    # request, which the server never actually agreed to keep open. The
    # first request on a connection succeeds; the second intermittently
    # fails with a broken pipe. This is what "clicking approve sometimes
    # does nothing" looks like from the browser: the click's own fetch call
    # can land on exactly that half-dead connection. Every response here
    # already sends a correct Content-Length, which is the one precondition
    # HTTP/1.1 keep-alive needs to work correctly.
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:  # quieter default logging
        sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send(status, _json({"error": message}))

    def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
        parsed = urlparse(self.path)
        path = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        try:
            if path == "/api/controllers":
                return self._send(200, _json(CONTROLLERS))

            if path == "/api/command-center":
                return self._send(200, _json(self._command_center(query.get("controller", ""))))

            if path == "/api/disruptions":
                return self._send(200, _json(self._disruption_list(query.get("scope", "network"),
                                                                    query.get("controller", ""))))

            if path.startswith("/api/disruptions/") and path.endswith("/joint-plan"):
                disruption_id = path.split("/")[3]
                with_ids = [x for x in (query.get("with", "").split(",")) if x]
                return self._send(200, _json(self._joint_plan(disruption_id, with_ids)))

            if path.startswith("/api/disruptions/"):
                disruption_id = path.rsplit("/", 1)[-1]
                return self._send(200, _json(
                    _load_disruption(disruption_id, query.get("controller", "unknown"))))

            if path == "/api/flights":
                return self._send(200, _json(self._flights()))

            if path == "/api/decisions":
                return self._send(200, _json(self._decisions()))

            return self._serve_static(path)
        except StopIteration:
            self._error(404, "not found")
        except ToolError as exc:
            self._error(400, f"{exc.code}: {exc.message}")
        except Exception as exc:  # a bug here must not take the server down
            traceback.print_exc()  # the response may fail to send (a client
            # that already gave up looks identical to a server bug from the
            # client's side); this is what actually explains a 500.
            self._error(500, f"{type(exc).__name__}: {exc}")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")

        try:
            if parsed.path == "/api/ask":
                return self._send(200, _json(self._ask(payload.get("query", ""))))
            if parsed.path.startswith("/api/disruptions/") and parsed.path.endswith("/commit-joint"):
                disruption_id = parsed.path.split("/")[3]
                return self._send(200, _json(self._commit_joint(disruption_id, payload)))
            if parsed.path.startswith("/api/disruptions/") and parsed.path.endswith("/commit"):
                disruption_id = parsed.path.split("/")[3]
                return self._send(200, _json(self._commit(disruption_id, payload)))
            self._error(404, "not found")
        except ToolError as exc:
            self._error(409 if exc.code == "NO_LEGAL_OPTION" else 400, f"{exc.code}: {exc.message}")
        except Exception as exc:
            traceback.print_exc()
            self._error(500, f"{type(exc).__name__}: {exc}")

    # -- handlers -----------------------------------------------------------

    def _command_center(self, controller: str) -> dict[str, object]:
        statuses = {row["disruption_id"]: row for row in ledger.list_open_disruptions()}
        open_ids = [d["id"] for d in DISRUPTIONS if statuses.get(d["id"], {}).get("status", "open") == "open"]
        # `ripple`'s own "passengers" figure is seat capacity on the affected
        # flights, not a real booking count -- the dataset carries no load
        # factor. Reported once, honestly labelled, rather than as two
        # different-sounding numbers derived from the one figure we have.
        seats_at_risk = 0
        for d in DISRUPTIONS:
            if d["id"] not in open_ids or not d["pairing_id"]:
                continue
            try:
                ripple = PORT_INSTANCE.ripple({"pairing_id": d["pairing_id"]})
                seats_at_risk += ripple.get("passengers", 0)
            except ToolError:
                continue
        mine = [d for d in DISRUPTIONS if d["desk"] == controller and d["id"] in open_ids]
        return {
            "controller": controller,
            "open_disruptions": len(open_ids),
            "my_open_disruptions": len(mine),
            "seats_at_risk": seats_at_risk,
            "ledger_enabled": ledger.enabled(),
        }

    def _disruption_list(self, scope: str, controller: str) -> list[dict[str, object]]:
        statuses = {row["disruption_id"]: row["status"] for row in ledger.list_open_disruptions()}
        rows = []
        for d in DISRUPTIONS:
            if scope == "mine" and d["desk"] != controller:
                continue
            if scope == "other" and d["desk"] == controller:
                continue
            rows.append({
                "id": d["id"], "title": d["title"], "event_type": d["event_type"],
                "desk": d["desk"], "narrative": d["narrative"],
                "status": statuses.get(d["id"], "open"),
            })
        return rows

    def _flights(self) -> list[dict[str, object]]:
        flights = json.loads((BUILD_DIR / "data" / "flights.json").read_text())
        gates = json.loads((BUILD_DIR / "data" / "boarding_gates.json").read_text())
        by_flight = {g["flight_id"]: g for g in gates}
        out = []
        for f in flights[:60]:  # first 60 chronologically -- a page, not the whole week
            g = by_flight.get(f["flight_id"], {})
            out.append({
                "flight_id": f["flight_id"], "flight_no": f["flight_no"], "date": f["date"],
                "dep_station": f["dep_station"], "arr_station": f["arr_station"],
                "dep_utc": f["dep_utc"], "aircraft": f["aircraft"],
                "boarding_gate_number": g.get("boarding_gate_number"),
            })
        return out

    def _decisions(self) -> list[dict[str, object]]:
        return ledger.recent_decisions()

    def _ask(self, query: str) -> dict[str, object]:
        """A controller's plain-English question.

        `router.py`'s ~20 deterministic rules settle every one of this
        dataset's 38 gold questions without a model call at all, so that
        path covers the large majority of real questions with zero LLM
        involvement -- not a stand-in for one. The one gap is a question the
        router genuinely can't classify (SIMULATE_WHATIF, RESOLVE_ILLEGAL,
        or free-form phrasing like "hey"): if `_init_advisor_agents()`
        managed to stand up a live Foundry connection at startup, that gap
        is handed off to the real `agents.py` pipeline instead of guessed
        at; if not, it says so plainly, the same discipline
        `pipeline.explain_no_tools()` already applies to its own gaps.
        """
        query = (query or "").strip()
        if not query:
            return {"query": query, "narrative": "Ask a question about a crew member, "
                    "pairing, flight, or rule -- e.g. \"Is C-2087 legal to cover P-2291?\""}

        # A plain greeting is not a crew-ops question, and answering it with
        # a model call risks it reaching for one of the fixed intent labels
        # anyway (see the EXPLAIN_RULE misfire this replaced) -- a fixed
        # template costs nothing and can't hallucinate, unlike a model that
        # has no "just say hi back" option in its own instructions.
        if _GREETING_RE.fullmatch(query.strip(" !.?")):
            return {
                "query": query, "classified": False, "intent": None, "tier": None,
                "narrative": (
                    "Hi -- ask me about a crew member, pairing, flight, or rule, "
                    "e.g. \"Is C-2087 legal to cover P-2291?\" or \"Who is on "
                    "reserve at BLR?\""
                ),
                "verified": None, "tool_calls": [],
            }

        if mismatch := pipeline.stated_attribute_mismatch(query, PORT_INSTANCE):
            # Same pre-flight safety guard `agents.py`'s CLI path already
            # runs -- a stated rank the roster contradicts is a wrong-person
            # risk, so this has to stop the question here rather than let
            # either the deterministic fast path or the Advisor Agent answer
            # around a false premise.
            route = router.route(query)
            return {
                "query": query,
                "classified": route.matched_rule is not None,
                "intent": str(route.intent) if route.matched_rule is not None else None,
                "tier": int(route.tier) if route.matched_rule is not None else None,
                "narrative": pii.redact_pii_text(mismatch),
                "verified": None,
                "tool_calls": [],
            }

        # No triage_fn: deterministic rules, then the semantic hybrid-search
        # fallback inside route() itself -- both run here; only the LLM
        # Triage Agent is excluded from this call (see the answer_question()
        # handoff below for where that still happens).
        route = router.route(query)
        trace, seen = [], set()

        def run_calls(calls):
            for c in calls:
                sig = f"{c.name}:{sorted((c.args or {}).items())!r}"
                if sig in seen:
                    continue
                seen.add(sig)
                trace.append(dispatch(PORT_INSTANCE, c.name, c.args))

        classified = route.matched_rule is not None
        if classified:
            run_calls(pipeline.seed_calls(route, query))
            run_calls(pipeline.followup_calls(route, trace))

        verified = None
        if trace:
            answer = pipeline.build_answer(route, trace)
            response = AdvisorResponse(
                tier=route.tier, intent=route.intent, query=query,
                entities=route.entities.to_dict(), answer=answer, trace=trace,
            )
            narrative = render_narrative(response)
            verified = verifier.verify(narrative, trace).ok
        elif _ADVISOR_AGENT is not None:
            # Empty trace, whether or not the router matched a rule -- a
            # matched rule with nothing for seed_calls()/followup_calls() to
            # act on is exactly as unresolved as no match at all, and only
            # the Advisor Agent's own tool-calling loop (Triage -> tool loop
            # -> render -> Explainer -> verify), the same one `python
            # agents.py` runs, can actually reason its way to an answer
            # instead of declining on partial signal.
            agent_response = answer_question(
                query, PORT_INSTANCE, _TRIAGE_AGENT, _ADVISOR_AGENT, _EXPLAINER_AGENT)
            discarded = any("discarded" in u for u in agent_response.unknowns)
            return {
                "query": query,
                "classified": True,
                "intent": str(agent_response.intent),
                "tier": int(agent_response.tier),
                "narrative": pii.redact_pii_text(agent_response.narrative),
                "verified": not discarded,
                "tool_calls": [
                    {"tool": t.tool, "args": t.args, "error": t.error}
                    for t in agent_response.trace
                ],
                "via_advisor_agent": True,
            }
        elif classified:
            narrative = pipeline.explain_no_tools(route)
        else:
            narrative = (
                "This doesn't match any of the router's deterministic rules, and "
                "the Advisor Agent isn't configured for this console right now -- "
                "so it genuinely can't answer yet rather than guessing. Try "
                "rephrasing with an explicit crew id (C-1042), pairing (P-2291), "
                "or rule (RULE-DUTY-02)."
            )

        return {
            "query": query,
            "classified": classified,
            "intent": str(route.intent) if classified else None,
            "tier": int(route.tier) if classified else None,
            "narrative": pii.redact_pii_text(narrative),
            "verified": verified,
            "tool_calls": [
                {"tool": t.tool, "args": t.args, "error": t.error} for t in trace
            ],
        }

    def _commit(self, disruption_id: str, payload: dict[str, object]) -> dict[str, object]:
        d = _resolve_disruption(disruption_id)
        detail = _load_disruption(disruption_id, payload.get("committed_by", "unknown"))
        balanced_crew_id = (detail.get("policies") or {}).get("balanced", {}).get("crew_id")
        override_reason = (payload.get("override_reason") or "").strip() or None
        if payload["crew_id"] != balanced_crew_id and not override_reason:
            raise ToolError(
                "OVERRIDE_REASON_REQUIRED",
                "This isn't the Balanced recommendation -- say why before committing it.",
            )
        result = PORT_INSTANCE.commit_decision(
            disruption_id=disruption_id, pairing_id=d["pairing_id"],
            crew_id=payload["crew_id"], role=detail.get("role", "Captain"),
            committed_by=payload["committed_by"],
            accepted_rank=payload.get("accepted_rank"),
            presented_options=detail.get("options", []),
            override_reason=override_reason if payload["crew_id"] != balanced_crew_id else None,
        )
        return result

    def _joint_plan(self, disruption_id: str, other_ids: list[str]) -> dict[str, object]:
        """Cost-minimal disjoint cover across this disruption and whichever
        other open disruptions it's contended with -- a thin wrapper around
        the existing `joint_plan` tool (Tool 7), not new solving logic."""
        ids = [disruption_id] + [i for i in other_ids if i != disruption_id]
        disruptions = [_resolve_disruption(i) for i in ids]
        if any(not d["crew_id"] for d in disruptions):
            raise ToolError("UNRESOLVED_ENTITY",
                             "joint plan needs a named crew member on every disruption involved")

        events = [
            {"pairing_id": d["pairing_id"], "role": PORT_INSTANCE.world.crew[d["crew_id"]].rank}
            for d in disruptions
        ]
        result = PORT_INSTANCE.joint_plan(events)

        # Map each pairing back to the disruption it came from, so the UI
        # can say "S6A: assign ..." rather than making the reader translate
        # pairing ids back to disruption titles themselves.
        pairing_to_disruption = {d["pairing_id"]: d["id"] for d in disruptions}
        pairing_to_title = {d["pairing_id"]: d["title"] for d in disruptions}
        assignments = []
        for pairing_id in [d["pairing_id"] for d in disruptions]:
            option = result.get(f"assign_{pairing_id}")
            if option:
                assignments.append({
                    "disruption_id": pairing_to_disruption[pairing_id],
                    "disruption_title": pairing_to_title[pairing_id],
                    "pairing_id": pairing_id, **option,
                })
        return {
            "total_cost_inr": result["total_cost_inr"],
            "equal_cost_alternatives": result["equal_cost_alternatives"],
            "note": result["note"],
            "assignments": assignments,
        }

    def _commit_joint(self, disruption_id: str, payload: dict[str, object]) -> dict[str, object]:
        """One approval, one transaction: `commit_joint_decisions` re-checks
        every assignment and raises before writing anything if even one has
        gone stale, so this either returns every assignment committed or
        raises with none of them recorded -- never a partial result."""
        committed_by = payload["committed_by"]
        assignments = [
            {"disruption_id": a["disruption_id"], "pairing_id": a["pairing_id"],
             "crew_id": a["crew_id"], "role": PORT_INSTANCE.world.crew[a["crew_id"]].rank,
             "accepted_rank": a.get("rank")}
            for a in payload["assignments"]
        ]
        results = PORT_INSTANCE.commit_joint_decisions(assignments, committed_by)
        return {"results": results}

    def _serve_static(self, path: str) -> None:
        if path == "/":
            path = "/index.html"
        file_path = (STATIC / path.lstrip("/")).resolve()
        if STATIC not in file_path.parents and file_path != STATIC:
            return self._error(404, "not found")
        if not file_path.is_file():
            return self._error(404, "not found")
        content_type = {
            ".html": "text/html", ".css": "text/css", ".js": "application/javascript",
        }.get(file_path.suffix, "application/octet-stream")
        self._send(200, file_path.read_bytes(), content_type)


def _warm_ledger() -> None:
    """Register every open disruption's candidate pool in the ledger up
    front, instead of waiting for someone to open its detail page.

    Contention is only detected between two disruptions that have each been
    registered at least once (see `register_and_check_contention` in
    ledger.py) -- so without this, whether S6A/S6B's genuine overlap is
    visible depends on which pages happened to be opened, in which order,
    since the last server restart. That is not something a live demo should
    depend on getting right by accident.
    """
    for d in DISRUPTIONS:
        if not d["crew_id"] or _disruption_status(d["id"]) == "resolved":
            continue
        try:
            _load_disruption(d["id"], d["desk"])
        except Exception:
            print(f"Ledger warm-up failed for {d['id']}:")
            traceback.print_exc()


def main() -> None:
    ledger.ensure_schema()
    print(f"Ledger enabled: {ledger.enabled()}")
    if ledger.enabled():
        _warm_ledger()
    _init_advisor_agents()
    print(f"Serving on http://localhost:{PORT}")
    print("Open in 3 tabs with ?controller=Ananya+Iyer / Rohit+Malhotra / Divya+Rao")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
