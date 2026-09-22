# Challenge 1: Build Agents

Time: ~45 minutes

## Objectives

By the end of this challenge, you will have:

- ✅ A **Triage Agent** that classifies a controller's question when regex
  rules can't (no tools)
- ✅ A **Resolution Advisor Agent** wired to all **10** real crew-ops tools —
  the only agent that ever touches the legality engine
- ✅ An **Explainer Agent** that rewrites a verified answer into controller
  prose (no tools — pure reasoning)
- ✅ All three tested end-to-end against the real crew-ops-advisor-dataset

## Context

**dCortex Air**, hub BLR. A crew controller gets a call: a captain is sick,
a flight is delayed, a certification just lapsed. They ask a plain-English
question and need a legally-verified, cost-ranked, cited answer in seconds —
not a hallucinated one.

This scenario is a Microsoft Foundry port of a real hackathon project,
**dCortex Crew Ops Advisor**. That project's own documentation describes its
architecture as "a pipeline with up to three model calls" and states its one
hard rule up front:

> **The LLM selects, sequences and narrates. It never calculates.**

Every number, id and legality verdict has to come from a tool call into
`core_engine/` — a from-scratch port of the original's legality rules, duty
arithmetic, cost model and candidate search. A deterministic **verifier**
(`verifier.py`) checks every claim in the final answer against what the tools
actually returned, and rejects anything unsourced. That is why this scenario
ships **three** agents and **ten** tools instead of the two-agent,
one-tool shape used elsewhere in this repo: the number follows from the
problem, not from the template.

## The pipeline

```
ROUTER (regex, 0 model calls) ──▶ TRIAGE AGENT (only if the router abstains)
        │
        ▼
PLANNER — pipeline.py's seed_calls() (0 model calls, pure Python)
        │
        ▼
RESOLUTION ADVISOR AGENT — the tool loop (1..8 model calls, 10 tools)
        │
        ▼
VERIFIER — verifier.py (0 model calls, set-membership over the tool trace)
        │
        ▼
EXPLAINER AGENT — rewrites the verified template into prose (0-1 model calls)
```

`router.py` runs ~20 ordered regex rules first — every one of the dataset's
38 gold questions routes with zero model calls. Only when every rule abstains
does the **Triage Agent** get consulted, and even then it only classifies
*which kind* of question this is; it never touches a tool.

## Agents and Tools

### Triage Agent — `agents.py::TriageAgent`

A `PromptAgentDefinition` with **no tools**. Its entire job is to read a
sentence the regex router couldn't classify and return
`{"intent": "...", "confidence": "..."}`. Cheap enough to run on a smaller
model deployment (`TRIAGE_MODEL_DEPLOYMENT_NAME` in `.env`, optional).

### Resolution Advisor Agent — `agents.py::ResolutionAdvisorAgent`

The only agent with tools — all **ten** of them, defined once as
vendor-neutral JSON Schema in `tools.py::TOOL_SCHEMAS` and adapted into
`FunctionTool` objects by `tools.py::foundry_tools()`:

| # | Tool | What it does |
|---|------|---------------|
| 1 | `lookup` | Row retrieval across 10 entities — crew, flights, pairings, reserves, certifications, risk signals... |
| 2 | `notification_brief` | Every fact a callout message needs, read from the roster |
| 3 | `duty_clock` | Accrued duty/flight hours and headroom under RULE-DUTY-02 / RULE-FLT-03 |
| 4 | `check_legality` | Evaluate all **7 rules** for one crew member against one pairing |
| 5 | `find_options` | Every legal candidate, cost-ranked, **plus** a policy-backed summary across the four strategies (reserve callout, day-off callout, deadhead/reposition, cancel — cheapest of each, cancel always last) |
| 6 | `ripple` | Blast radius of a disruption: uncovered flights, at-risk downstream days, passengers |
| 7 | `simulate` | Fork the world, apply a what-if, return the diff |
| 8 | `joint_plan` | Cost-minimal assignment across simultaneous disruptions (disjoint — one crew member, one pairing) |
| 9 | `check_gate` | Verify or inspect a boarding-gate assignment (bonus tool) |
| 10 | `explain_rule` | The text and parameters of one rule |

Every tool is backed by `core_engine/` — a JSON-dataset port of the original
project's Postgres-backed rules engine:

- **`core_engine/rules.py`** — the seven legality rules (`RULE-FDP-01`
  through `RULE-BASE-07`), each returning *why*, never a bare boolean
- **`core_engine/duty.py`** — calendar-day duty windows and the sector-count-
  dependent FDP limit
- **`core_engine/world.py`** — candidate search and cost ranking (`assess()`,
  `find_options`'s pool-and-rank logic)
- **`core_engine/gates.py`**, **`core_engine/resolve.py`** — boarding-gate
  occupancy, and "did you mean?" id resolution that never auto-corrects

`agents.py::ResolutionAdvisorAgent.run_intent()` seeds the obvious opening
tool calls deterministically (`pipeline.py::seed_calls()`) before the model
is even consulted, then runs the standard Foundry function-call loop —
capped at 8 iterations — for anything more it needs.

### Explainer Agent — `agents.py::ExplainerAgent`

A `PromptAgentDefinition` with **no tools** — pure reasoning, matching the
"Agent 2: diagnosis, no tools" pattern used elsewhere in this repo. It never
sees raw data; it only rewrites `explainer.py`'s deterministic template into
shorter, controller-facing prose. Its output is fed straight back into
`verifier.py::verify()` — if it added or changed a single number or id, the
draft is discarded and the deterministic template ships instead.

## Guardrails

Three independent layers, none of which knows about the other two:

- **`verifier.py`** — deterministic, runs on every answer. Rejects anything
  claiming a number or id no tool call in the trace actually returned.
- **`pii.py`** — deterministic, runs at the output boundary
  (`agents.py::print_response()`). Redacts email/phone/PAN/passport/Aadhaar/
  card numbers by content pattern, and address/DOB/health/financial fields by
  name, wherever they appear — defense-in-depth for if such fields are ever
  added to the dataset, since none exist in it today. Ported from dCortex
  Crew Ops Advisor's `api/pii.py`.
- **Azure content filters** — not code at all. Configured once per model
  deployment in the Foundry portal; see
  [`challenge-0-setup/README.md`](../challenge-0-setup/README.md#content-safety-guardrails-do-this-now-while-youre-already-here)
  for exactly what to check and why Prompt Shields matters here specifically
  (the Resolution Advisor reads tool *results* back into its own context,
  which is the channel indirect prompt injection targets).

## Try it

```bash
cd challenge-1-build
python agents.py
```

This creates all three agents in your Foundry project and runs them against
four representative questions (one per tier, plus a legality check), printing
the routed intent, the tools called, the verified narrative, and the
confidence level for each.

To explore the deterministic layers on their own — no Azure credentials
needed — run:

```bash
python -c "
from core_engine.port import JsonToolPort
from tools import dispatch
port = JsonToolPort()
print(dispatch(port, 'check_legality', {'crew_id': 'C-2087', 'pairing_id': 'P-2291'}).result)
"
```

## Success Criteria

- [ ] All three agents appear in the Foundry portal under **Agents**
- [ ] `python agents.py` prints a verified, cited answer for each demo question
- [ ] You can explain, for one tool call, exactly which line of `core_engine/`
      produced the number in the final answer

Next: [Challenge 2 — Monitor](../challenge-2-monitor/README.md)
