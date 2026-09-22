#!/usr/bin/env python3
"""Self-distillation training data for fine-tuning the Resolution Advisor.

What this fine-tune is *for* — read before running
----------------------------------------------------
This system's one hard rule is "the LLM selects, sequences and narrates; it
never calculates" (see `challenge-1-build/README.md`). `verifier.py` already
guarantees that a wrong or unsourced number never reaches a controller,
regardless of what the model does. So fine-tuning here is NOT about teaching
the model to be more *correct* — it already can't be wrong in a way that
survives the verifier. It is about teaching it to be more *efficient and
consistent*: choosing the right tool call on the first try instead of
iterating, and never inventing a tool call the deterministic planner
(`pipeline.py::seed_calls`) would not have made.

That reframes the whole data problem as **self-distillation**: the teacher is
not a bigger model, it is `challenge-1-build`'s own deterministic core.
`router.py` classifies a question with zero model calls for every shape in
the gold set, and `pipeline.seed_calls()` / `followup_calls()` already know
the exact, correct opening tool calls for most intents. So instead of hand-
labeling training examples, this script:

  1. Takes the 38 real questions in `challenge-1-build/data/questions.json`
     and generates many grounded variants of each by substituting every
     entity found in it (crew id, pairing id, station, date, rule id, gate
     number) for a different real value of the same type, drawn from the
     actual vendored dataset. The wording is untouched -- only identifiers
     change -- so every variant stays grammatically real and semantically
     valid.
  2. Re-classifies each variant with the real `router.py` (deterministic; no
     model call) and computes the oracle tool-call trace with the real
     `pipeline.seed_calls()` / `followup_calls()` and `tools.dispatch()`.
  3. Keeps a variant only if: the router matched a rule with confidence (not
     the LLM-fallback default), the resulting trace is non-empty, no tool
     call in it errored, and `verifier.py` confirms nothing is unsourced.
     Anything that fails any of those checks is discarded, not patched --
     the fine-tuning set must be as trustworthy as the pipeline itself.
  4. Emits one multi-turn function-calling example per surviving variant:
     system prompt + question in, the exact tool_calls the deterministic
     planner made (across as many rounds as `seed_calls`/`followup_calls`
     produced) with their real results, out.

Two intents are deliberately absent from the result: SIMULATE_WHATIF and
RESOLVE_ILLEGAL. `pipeline.seed_calls()` seeds no opening call for either --
by design, they are exactly the cases meant to need the Resolution Advisor's
own judgment. There is no local oracle for them, so they are not faked;
fine-tuning cannot help with what this script has no ground truth for.

Usage:
    python generate_training_data.py [--variants-per-question 8] [--seed 42]

Writes `data/training.jsonl` and `data/validation.jsonl` (85/15 split) next
to this file.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = REPO_ROOT / "challenge-1-build"
sys.path.insert(0, str(BUILD_DIR))

import config
import pipeline
import router
import verifier
from core_engine.port import JsonToolPort
from entities import (
    AIRCRAFT_RE, CREW_RE, GATE_RE, ISO_DATE_RE, PAIRING_RE, RULE_RE,
    STATION_RE, extract,
)
from explainer import render
from schemas import AdvisorResponse
from tools import TOOL_SCHEMAS, dispatch

OUT_DIR = Path(__file__).resolve().parent / "data"

# --------------------------------------------------------------------------
# Substitution pools -- every real value of each entity type, read straight
# out of the vendored dataset so every substitution stays grounded.
# --------------------------------------------------------------------------


def _load_pools() -> dict[str, list[str]]:
    crew = json.loads((BUILD_DIR / "data" / "crew.json").read_text())
    flights_text = (BUILD_DIR / "data" / "flights.json").read_text()
    rosters_text = (BUILD_DIR / "data" / "rosters.json").read_text()
    gates_text = (BUILD_DIR / "data" / "boarding_gates.json").read_text()

    return {
        "crew_ids": sorted({c["crew_id"] for c in crew}),
        "pairing_ids": sorted(set(re.findall(r"P-\d{4}", flights_text + rosters_text))),
        "flight_nos": sorted(set(re.findall(r"DX\d{3}", flights_text))),
        "gate_numbers": sorted(set(re.findall(r"[A-Z]{3}-G\d+", gates_text))),
        "rule_ids": list(config.ALL_RULE_IDS),
        "stations": sorted(config.STATIONS),
        "aircraft": sorted(config.AIRCRAFT),
        "dates": [
            (
                __import__("datetime").date.fromisoformat(config.WEEK_START)
                + __import__("datetime").timedelta(days=i)
            ).isoformat()
            for i in range((
                __import__("datetime").date.fromisoformat(config.WEEK_END)
                - __import__("datetime").date.fromisoformat(config.WEEK_START)
            ).days + 1)
        ],
    }


_SUBSTITUTORS: tuple[tuple[re.Pattern[str], str], ...] = (
    (CREW_RE, "crew_ids"),
    (PAIRING_RE, "pairing_ids"),
    (RULE_RE, "rule_ids"),
    (GATE_RE, "gate_numbers"),
    (AIRCRAFT_RE, "aircraft"),
    (ISO_DATE_RE, "dates"),
)


def substitute(text: str, pools: dict[str, list[str]], rng: random.Random) -> str:
    """Replace every occurrence of each recognisable entity with a different
    real value of the same type. A value that appears twice ("C-1042 ...
    C-1042") is replaced consistently, since both mentions are the same
    person. Stations are handled separately (see `_substitute_stations`)
    because the 3-letter pattern also matches non-station words."""
    out = text
    for pattern, pool_key in _SUBSTITUTORS:
        found = _dedupe(m.group(0) for m in pattern.finditer(out))
        pool = pools[pool_key]
        for original in found:
            choices = [v for v in pool if v != original]
            if not choices:
                continue
            replacement = rng.choice(choices)
            out = re.sub(re.escape(original), replacement, out)
    return _substitute_stations(out, pools["stations"], rng)


def _substitute_stations(text: str, station_pool: list[str], rng: random.Random) -> str:
    found = _dedupe(s for s in STATION_RE.findall(text) if s in config.STATIONS)
    out = text
    for original in found:
        choices = [v for v in station_pool if v != original]
        if not choices:
            continue
        replacement = rng.choice(choices)
        out = re.sub(rf"\b{original}\b", replacement, out)
    return out


def _dedupe(items) -> list[str]:
    seen, result = set(), []
    for x in items:
        if x not in seen:
            seen.add(x)
            result.append(x)
    return result


# --------------------------------------------------------------------------
# Oracle: the deterministic pipeline, used as the fine-tuning teacher
# --------------------------------------------------------------------------


def oracle_trace(route, port) -> list:
    """Exactly what `agents.py::ResolutionAdvisorAgent.run_intent()` seeds
    before ever consulting a model -- reused here as ground truth."""
    trace, seen = [], set()

    def run_calls(calls):
        for call in calls:
            sig = f"{call.name}:{sorted((call.args or {}).items())!r}"
            if sig in seen:
                continue
            seen.add(sig)
            trace.append(dispatch(port, call.name, call.args))

    run_calls(pipeline.seed_calls(route))
    run_calls(pipeline.followup_calls(route, trace))
    return trace


def to_rounds(route, port) -> list[list]:
    """The same oracle trace, but split back into the rounds it was produced
    in (seed round, then follow-up round if any) -- a fine-tuning example
    should show the model making a batch of calls, seeing results, then
    deciding whether it needs more, not one flat list."""
    seed = pipeline.seed_calls(route)
    round1 = [dispatch(port, c.name, c.args) for c in seed]
    seen = {f"{e.tool}:{sorted((e.args or {}).items())!r}" for e in round1}

    followups = pipeline.followup_calls(route, round1)
    round2 = []
    for c in followups:
        sig = f"{c.name}:{sorted((c.args or {}).items())!r}"
        if sig in seen:
            continue
        round2.append(dispatch(port, c.name, c.args))

    return [r for r in (round1, round2) if r]


# --------------------------------------------------------------------------
# Fine-tuning example assembly (OpenAI/Azure OpenAI chat + tools format)
# --------------------------------------------------------------------------

TOOLS_FIELD = [
    {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        },
    }
    for t in TOOL_SCHEMAS
]

_CLOSING_TEXT = (
    "Retrieved the information needed to answer this question."
)
"""Deliberately fact-free. `agents.py::run_intent()` never reads the
Resolution Advisor's own final text -- `pipeline.build_answer()` builds the
typed answer purely from the tool trace -- so training the model to add any
specific claim here would teach a habit the real pipeline throws away, and
risk teaching it to state things the verifier would otherwise catch."""


def build_example(route, query: str, rounds: list[list]) -> dict[str, Any]:
    from prompts import INTENT_GUIDANCE, base_system_prompt

    guidance = INTENT_GUIDANCE.get(route.intent, "").strip()
    opening = (f"{guidance}\n\n" if guidance else "") + f"Controller's question: {query}"

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": base_system_prompt()},
        {"role": "user", "content": opening},
    ]

    call_id = 0
    for entries in rounds:
        tool_calls = []
        for entry in entries:
            call_id += 1
            tool_calls.append({
                "id": f"call_{call_id}",
                "type": "function",
                "function": {"name": entry.tool, "arguments": json.dumps(entry.args)},
            })
        messages.append({"role": "assistant", "tool_calls": tool_calls})
        for tc, entry in zip(tool_calls, entries):
            content = entry.result if entry.error is None else {"error": entry.error}
            messages.append({
                "role": "tool", "tool_call_id": tc["id"], "content": json.dumps(content),
            })

    messages.append({"role": "assistant", "content": _CLOSING_TEXT})

    return {"messages": messages, "tools": TOOLS_FIELD}


# --------------------------------------------------------------------------
# The gold question set (`data/questions.json`) has no worked example at all
# for EXPLAIN_RULE or CHECK_GATE, even though `router.py` classifies both
# correctly (see the DEMO_QUESTIONS in `challenge-1-build/agents.py`) and
# `pipeline.seed_calls()` seeds both deterministically. A handful of simple,
# hand-written templates close that gap the same honest way the rest of this
# script does: real values, real oracle trace, same filters.
# --------------------------------------------------------------------------

EXTRA_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("What does {rule_id} say?", "rule_ids"),
    ("Explain {rule_id} in plain terms.", "rule_ids"),
    ("Which rule is {rule_id} and what does it require?", "rule_ids"),
    ("Is gate {gate_number} free right now?", "gate_numbers"),
    ("What is currently assigned to gate {gate_number}?", "gate_numbers"),
    ("Does flight {flight_no} conflict with anything at its gate?", "flight_nos"),
)


def _process_candidate(
    text: str, port, stats: Counter, per_intent: dict[str, int],
    seen_signatures: set[str], examples: list[dict[str, Any]],
) -> None:
    route = router.route(text)
    if route.matched_rule is None:
        stats["no_deterministic_match"] += 1
        return

    rounds = to_rounds(route, port)
    flat = [e for r in rounds for e in r]
    if not flat:
        stats["no_oracle_trace"] += 1
        return
    if any(e.error for e in flat):
        stats["tool_error_in_trace"] += 1
        return

    answer = pipeline.build_answer(route, flat)
    response = AdvisorResponse(
        tier=route.tier, intent=route.intent, query=text,
        answer=answer, trace=flat,
    )
    narrative = render(response)
    result = verifier.verify(narrative, flat)
    if not result.ok:
        stats["failed_verification"] += 1
        return

    # Cap examples per intent so one heavily-templated intent (e.g.
    # LOOKUP_CREW, which many rules can widen into) doesn't crowd out the
    # rest of the training set.
    if per_intent[str(route.intent)] >= 40:
        stats["intent_cap_reached"] += 1
        return

    shape = sorted((e.tool, tuple(sorted((e.args or {}).items()))) for e in flat)
    signature = f"{route.intent}:{shape!r}"
    if signature in seen_signatures:
        stats["duplicate_shape"] += 1
        return
    seen_signatures.add(signature)

    examples.append(build_example(route, text, rounds))
    per_intent[str(route.intent)] += 1
    stats["kept"] += 1


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def generate(variants_per_question: int, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    pools = _load_pools()
    port = JsonToolPort()
    questions = json.loads((BUILD_DIR / "data" / "questions.json").read_text())

    examples: list[dict[str, Any]] = []
    stats = Counter()
    per_intent: dict[str, int] = defaultdict(int)
    seen_signatures: set[str] = set()

    for q in questions:
        base_text = q["prompt"]
        candidates = {base_text} | {
            substitute(base_text, pools, rng) for _ in range(variants_per_question)
        }
        for text in candidates:
            _process_candidate(text, port, stats, per_intent, seen_signatures, examples)

    for template, pool_key in EXTRA_TEMPLATES:
        for value in pools[pool_key]:
            text = template.format(**{pool_key[:-1]: value})
            _process_candidate(text, port, stats, per_intent, seen_signatures, examples)

    print("Generation summary:")
    for k, v in stats.most_common():
        print(f"  {k:24s} {v}")
    print("\nPer-intent kept counts:")
    for intent, n in sorted(per_intent.items()):
        print(f"  {intent:20s} {n}")

    return examples


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variants-per-question", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--val-fraction", type=float, default=0.15)
    args = parser.parse_args()

    examples = generate(args.variants_per_question, args.seed)
    if not examples:
        print("No examples survived generation -- nothing written.")
        sys.exit(1)

    rng = random.Random(args.seed)
    rng.shuffle(examples)
    n_val = max(1, int(len(examples) * args.val_fraction))
    val, train = examples[:n_val], examples[n_val:]

    OUT_DIR.mkdir(exist_ok=True)
    for name, rows in (("training.jsonl", train), ("validation.jsonl", val)):
        path = OUT_DIR / name
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        print(f"Wrote {len(rows):4d} examples -> {path}")


if __name__ == "__main__":
    main()
