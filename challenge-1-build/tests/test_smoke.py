"""Smoke tests for the ported crew-ops pipeline.

Not a re-creation of the original project's full test suite — just enough to
prove the JSON-backed port of `core_engine/` agrees with the numbers its own
documentation quotes, that the router settles real questions without a
model, and that the verifier actually rejects an unsourced claim.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import router
import pipeline
import verifier
from core_engine.duty import calendar_window, fdp_limit
from core_engine.port import JsonToolPort
from datetime import date
from explainer import render
from schemas import AdvisorResponse, TraceEntry
from tools import dispatch


def _run(query, port):
    route = router.route(query)
    trace = []
    seen = set()

    def run_calls(calls):
        for c in calls:
            sig = f"{c.name}:{sorted((c.args or {}).items())!r}"
            if sig in seen:
                continue
            seen.add(sig)
            trace.append(dispatch(port, c.name, c.args))

    run_calls(pipeline.seed_calls(route))
    run_calls(pipeline.followup_calls(route, trace))
    answer = pipeline.build_answer(route, trace)
    response = AdvisorResponse(tier=route.tier, intent=route.intent, query=query,
                               answer=answer, trace=trace)
    narrative = render(response) if trace else pipeline.explain_no_tools(route)
    return route, trace, narrative


def test_fdp_limit_shrinks_with_sectors():
    assert [fdp_limit(n) for n in (1, 2, 3, 4, 6)] == [13.0, 13.0, 12.5, 12.0, 11.0]


def test_calendar_window_is_inclusive():
    assert calendar_window(date(2026, 9, 14), 7) == (date(2026, 9, 8), date(2026, 9, 14))


def test_duty_clock_canary():
    """C-1042 accrues 20.93 duty hours over the 7 days ending 2026-09-14,
    leaving 39.07h of headroom -- the canary the original project's
    core_engine/duty.py names explicitly. If this is right, the window
    maths ported correctly."""
    port = JsonToolPort()
    result = dispatch(port, "duty_clock", {"crew_id": "C-1042"}).result
    assert result["duty_hours_7d"] == 20.93
    assert result["duty_headroom_7d"] == 39.07


def test_router_settles_gold_shapes_without_a_model():
    from schemas import Intent, Tier

    cases = [
        ("Who is on reserve at BLR on 2026-09-15?", Intent.LOOKUP_RESERVE, Tier.LOOKUP),
        ("What does RULE-DUTY-02 say?", Intent.EXPLAIN_RULE, Tier.LOOKUP),
        ("Captain C-1042 just called in sick for P-2291, who do I use?",
         Intent.FIND_REPLACEMENT, Tier.REPLACEMENT),
        ("Is C-2087 legal to cover P-2291?", Intent.CHECK_LEGALITY, Tier.REPLACEMENT),
        ("Both A320 captains are sick. Give the optimal joint plan.",
         Intent.JOINT_PLAN, Tier.CONSEQUENCE),
    ]
    for text, intent, tier in cases:
        route = router.route(text)
        assert route.intent is intent, text
        assert route.tier is tier, text
        assert route.used_llm is False, f"{text!r} should not need the Triage Agent"


def test_find_options_recommends_the_documented_reserve_callout():
    """The system prompt's own worked example for P-2291: C-3310's reserve
    callout is the cheapest legal cover at ~18,500 INR; C-2210's DX402
    deadhead from DEL is a legal but pricier fallback at ~41,200 INR, kept
    in the running for exactly the case where no reserve is free."""
    port = JsonToolPort()
    result = dispatch(port, "find_options", {"pairing_id": "P-2291", "role": "Captain"}).result
    legal_ids = [o["crew_id"] for o in result["options"] if o["crew_id"]]
    assert legal_ids[0] == "C-3310"  # the cheapest option overall
    assert "C-2210" in legal_ids     # the deadhead fallback, further down


def test_find_options_ranks_four_strategies_with_cancel_last():
    """A policy-backed ranking across four strategies: reserve callout,
    day-off callout, deadhead/reposition, cancel -- cheapest of each first,
    cancel always last regardless of relative cost."""
    port = JsonToolPort()
    result = dispatch(port, "find_options", {"pairing_id": "P-2291", "role": "Captain"}).result
    strategies = result["strategies"]

    assert [s["strategy"] for s in strategies] == [
        "reserve_callout", "day_off_callout", "deadhead_reposition", "cancel",
    ]
    assert strategies[-1]["crew_id"] is None  # cancel has no assignee
    # Cheapest-first ordering, strictly increasing cost across the four lines.
    costs = [s["cost_inr"] for s in strategies]
    assert costs == sorted(costs)
    assert strategies[-1]["cost_inr"] == max(costs)  # cancel is the most expensive


def test_verifier_rejects_an_unsourced_number():
    trace = [TraceEntry(tool="lookup", result={"crew_id": "C-1042", "cost_inr": 18500})]
    assert verifier.verify("C-1042 costs 18,500.", trace).ok is True
    assert verifier.verify("C-1042 costs 22,750.", trace).ok is False
    assert verifier.verify("C-9999 costs 18,500.", trace).ok is False


def test_explain_no_tools_never_crashes_on_scalar_entities():
    """SIMULATE_WHATIF seeds no deterministic tool calls (it needs the
    Resolution Advisor's own judgement), so a run with no Triage/Advisor
    agent wired up falls through to explain_no_tools -- which must handle
    a bare float entity (delay_minutes) as gracefully as a list one."""
    route = router.route("What if DX401 is delayed by 90 minutes on P-2201?")
    text = pipeline.explain_no_tools(route)
    assert "delay minutes: 90.0" in text


def test_date_range_words_turn_into_a_comparison_not_an_exact_match():
    """"before"/"after"/"since"/"between" next to a date used to be dropped
    entirely, so "flights before 19 Sep" silently matched the same equality
    filter as "flights on 19 Sep" and returned identical rows."""
    import entities

    assert entities.extract("flights on 19 Sep").date_range is None
    assert entities.extract("flights before 19 Sep").date_range == {"lt": "2026-09-19"}
    assert entities.extract("flights after 16 Sep").date_range == {"gt": "2026-09-16"}
    assert entities.extract("flights on or before 17 Sep").date_range == {"lte": "2026-09-17"}
    assert entities.extract("flights between 15 Sep and 18 Sep").date_range == {
        "gte": "2026-09-15", "lte": "2026-09-18",
    }


def test_flights_before_a_date_returns_a_different_count_than_on_it():
    port = JsonToolPort()

    def flight_count(query):
        route = router.route(query)
        assert route.matched_rule, query
        trace = []
        for c in pipeline.seed_calls(route, query):
            trace.append(dispatch(port, c.name, c.args))
        return len(trace[0].result)

    on_count = flight_count("how many flights available on sep 19?")
    before_count = flight_count("how many flights available before sep 19")
    after_count = flight_count("how many flights available after sep 16")

    assert on_count == 21
    assert before_count not in (0, on_count)
    assert after_count not in (0, on_count)


def test_end_to_end_pipeline_produces_a_verified_answer():
    port = JsonToolPort()
    route, trace, narrative = _run(
        "Captain C-1042 just called in sick for P-2291, who do I use?", port)
    assert trace, "expected at least one tool call"
    result = verifier.verify(narrative, trace)
    assert result.ok, result.summary()
