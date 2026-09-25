# Crew Ops Advisor — Microsoft Agent-a-thon Submission

A Microsoft Foundry hands-on lab built around a real system: an AI advisor
for an airline crew-control desk that turns "Captain C-1042 just called in
sick for P-2291, who do I use?" into a legally-verified, cost-ranked, cited
answer — never a plausible-sounding guess.

## Background

**dCortex Air**, hub BLR. Eight stations, six aircraft, 150 crew, 147
flights, 39 pairings across one operating week. A crew controller under time
pressure needs three kinds of answer:

- **Tier 1 — Lookup**: "Who is on reserve at BLR?"
- **Tier 2 — Replacement**: "C-1042 is sick for P-2291 — who do I use?" / "Is
  C-2087 legal to cover P-2291?"
- **Tier 3 — Consequence**: "Both A320 captains are sick — give me the
  optimal joint plan."

Every answer has to cite its sources: a crew id, a duty hour, a cost figure
that isn't traceable to an actual tool call is a hallucination, and in this
domain a hallucination is a real person dispatched to the wrong aircraft.

## Your Mission

Build a Microsoft Foundry agent system that:

1. **Routes** a question to the right kind of answer (mostly for free, via
   regex and a semantic fallback — a model is only asked when both abstain)
2. **Resolves** it using tools backed by a real legality engine — 7 duty/rest/
   qualification/certification rules, a cost model, and cost-ranked candidate
   search — never by the model's own arithmetic
3. **Verifies** that the drafted answer only repeats what the tools actually
   said, before a controller ever sees it
4. **Explains** the verified answer in plain, concise prose

## Challenges

| # | Challenge | What You'll Do | Time |
|---|-----------|-----------------|------|
| 0 | [Setup](./challenge-0-setup/README.md) | Deploy Microsoft Foundry infrastructure | 20 min |
| 1 | [Build Agents](./challenge-1-build/README.md) | Build the Triage, Resolution Advisor (15 tools) and Explainer agents | 45 min |
| 2 | [Monitor](./challenge-2-monitor/README.md) | Enable GenAI tracing with Application Insights | 20 min |
| 3 | [Evaluate](./challenge-3-evaluate/README.md) | Run systematic quality evaluations | 30 min |
| 4 | [Production Workflow](./challenge-4-deploy/README.md) | Multi-agent orchestration + portal workflow | 30 min |
| 5 (bonus) | [Fine-Tune](./challenge-5-finetune/README.md) | Self-distill training data from the deterministic core, fine-tune the Resolution Advisor | 1.5–2 hr |

## Why three agents, not two

The system this lab is based on — dCortex Crew Ops Advisor — rejected a
heavier multi-agent decomposition (a legality agent, a cost agent, a cascade
agent, a coordinator) as pure overhead: that reasoning is deterministic and
exact, so having models confer about it would only add latency, not
correctness. What earns a model call is three things — classify,
choose-tools-and-narrate, rewrite-for-a-human — mapped onto Foundry's
`PromptAgentDefinition` pattern:

| Agent | Tools | Job |
|---|---|---|
| **Triage** | none | Classify tier/intent, only when the regex router and its semantic fallback both abstain |
| **Resolution Advisor** | **15** | Choose tools, never calculate; the only agent touching the legality engine |
| **Explainer** | none | Rewrite a verified template into prose; never adds a fact |

## Why the Challenges Are in This Order

**Build first.** An agent reasoning from general knowledge about "typical"
crew rest rules will produce a plausible, wrong answer — the FDP limit here
shrinks 0.5h per sector past the second, and no model knows that without a
tool telling it. `check_legality` and the other fourteen tools ground every
answer in `core_engine/`'s ported rules engine, not in what "usually" is true
of airline rosters.

**Then monitor.** When the Resolution Advisor recommends a reserve callout,
did it actually call `find_options`, or did it narrate from the question
alone? A trace answers that; a plausible-looking recommendation does not.

**Then evaluate.** Tracing tells you the agents ran. Evaluation tells you
whether the result is any good — and this lab is explicit that "good" here
has two different, non-substitutable meanings: fluent (the portal's LLM-as-
judge) and *sourced* (`verifier.py`'s deterministic gate, which the portal
evaluators cannot check and do not attempt to).

**Then deploy.** Turning three scripts into a callable pipeline — with the
verifier gate wired into the middle of it, not left out — is the difference
between a demo and something a crew-control desk could actually run against.

## Architecture

```
Controller's question
        │
        ▼
   ROUTER (router.py, 21 ordered regex rules, 0 model calls)
        │  every rule abstains?
        ▼
   SEMANTIC FALLBACK (hybrid BM25 + embedding match against the 38 gold
   questions, 0.65 threshold, 0 model calls) ── borrows the closest match's
   intent, never its entities or its answer
        │  no match clears the threshold?
        ▼
  TRIAGE AGENT (Foundry, no tools) ── classifies intent
        │
        ▼
  PLANNER (pipeline.py, 0 model calls) ── seeds the obvious opening tool calls
        │
        ▼
  RESOLUTION ADVISOR AGENT (Foundry, 15 tools)
        │   lookup · notification_brief · duty_clock · check_legality
        │   find_options · ripple · simulate · joint_plan · same_pairing
        │   check_gate · explain_rule · search_rules · suggest_crew_ids
        │   list_controllers · controller_issue_counts
        │        │
        │        ▼
        │   core_engine/  ── 7 legality rules, duty arithmetic, cost model,
        │                    candidate search, boarding-gate occupancy,
        │                    hybrid search over rules/questions/scenarios
        ▼
  VERIFIER (verifier.py, 0 model calls) ── every claim traced to a tool
        │  call, plus hallucination and completeness checks (invented crew
        │  names, malformed flight numbers, miscounted lists, dropped
        │  "excluded"/"uncovered" entries)
        │  fails → discard draft, ship the deterministic template instead
        ▼
  EXPLAINER AGENT (Foundry, no tools) ── verified answer -> controller prose
        │
        ▼
  Cited, cost-ranked, legally-checked answer
```

A sixteenth tool, `commit_decision`, exists for the [Console](./console/README.md)'s
ledger writes but is deliberately withheld from the Resolution Advisor's own
tool loop — an agent only narrates and recommends, it never commits a crew
member on its own.

## Guardrails

Three layers, none aware of the other two: Azure content filters (harmful
content, prompt injection — a portal config, see
[challenge-0-setup](./challenge-0-setup/README.md#content-safety-guardrails-do-this-now-while-youre-already-here)),
`verifier.py` (unsourced claims — see the pipeline above), and `pii.py`
(sensitive-data redaction at the output boundary — see
[challenge-1-build](./challenge-1-build/README.md#guardrails)). A content
filter doesn't know what a sourced number is; the verifier doesn't know what
a slur is. Neither is a substitute for the other two.

## Operational Metrics

**Latency.** Deterministic-path answers respond in well under a second, since
they're pure database lookups with no model call involved. Agentic-path
answers — the ones that go through Triage, the tool-calling loop, and the
Explainer — run roughly 3 to 10 seconds, based on two real traces: a
single-turn call logged 3.7 seconds, and a two-hop trace logged about 8.6
seconds total.

**Cost per tier.** Token usage runs roughly 2,000 to 6,500 tokens per answer,
with Tier 3 questions costing noticeably more than Tier 1 due to extra tool
round-trips.

**Verifier rejection rate.** On the canonical 38-question set, the rejection
rate is effectively zero in the most recent run — every draft was accepted
without falling back. Under harder, adversarial phrasing, development testing
found issues in roughly 13 percent of cases (43 out of 323), the closest real
proxy available, though it blends automatic Verifier catches with
manually-found issues rather than being one clean metric.

**Fallback frequency.** The Explainer's silent fallback-to-template path was
observed triggering in about 1 of 12 sampled runs — a small enough sample to
treat as a signal rather than a solid rate.

**False-acceptance rate.** Not yet measured. It requires comparing every
verified answer's actual content against its expected output across all 38
questions — the next concrete step for evaluation rigor.

## Where this came from

This lab ports the pipeline design, the legality rules, the cost model, the
dataset and the 38-question gold evaluation set from **dCortex Crew Ops
Advisor**, a separate hackathon project built by a four-person team. That
project's own README states its thesis directly:

> "The LLM selects, sequences and narrates. It never calculates."

Everything in `challenge-1-build/core_engine/` — the rules, the duty-hour
arithmetic, the cost ranking — is a from-scratch Python port of that
project's `core/` package, adapted to read the vendored dataset as local JSON
(this lab's convention) instead of from Postgres (that project's production
choice). The tool schemas, the router's regex rules, the verifier's claim-
checking logic and the system prompt are ported closely enough that the
original's worked examples still hold — ask "Captain C-1042 just called in
sick for P-2291, who do I use?" and you get exactly the ranking the original's
own docs quote: **Reserve Captain D. Reddy (C-3310), ₹18,500, clears all seven
rules** as the recommendation, three day-off callouts at ₹24,000 as
alternatives, the DX402 deadhead via C-2210 at ₹41,200 as the cheapest option
if none of those were legal, and cancelling all 6 flights of the pairing at
₹1,500,000 (81× the recommendation) last, exactly as the original phrases it:
*"still far below the ₹250,000 cost of cancelling a single leg."*

## Next Steps

**Deploy as a hosted agent endpoint.** `answer_question()` in
`challenge-1-build/agents.py` already returns a typed `AdvisorResponse` —
wrapping it behind FastAPI or Azure Functions is mostly serialization.

**Replace the dataset with a live feed.** `core_engine/port.py`'s
`JsonToolPort` reads flat JSON files; swapping it for a class that queries a
real crew-scheduling system is the only change the rest of the pipeline needs
to see, because every tool call goes through the same `tools.ToolPort`
interface.

**Add a knowledge base.** Attach the airline's actual ops manual to the
Resolution Advisor Agent as a File Search tool, so `explain_rule` can cite
the regulation text directly instead of the seven rules in
`data/rules.json`.

**Wire evaluation into CI.** Run `challenge-4-deploy/evaluation_dataset.json`
(the full 38-question gold set) on every change to
`challenge-1-build/prompts/system.md` and block the merge if the verified
rate drops.

**Explore advanced patterns.** A held-out scenario set, never tuned against,
scored once as an honest self-check — the discipline the original project
used to keep from overfitting its own prompt to its own gold questions.

---

*Submitted for the Microsoft Agent-a-thon. Built with Microsoft Foundry.*
