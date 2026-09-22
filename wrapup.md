# Wrap-Up

## What you built

A crew-control desk advisor for dCortex Air with the exact agent shape its
own team arrived at — not a generic two-agent template, but three agents
matched to the three points in the pipeline where a model call actually earns
its place:

- **Triage Agent** — classifies a question when ~20 ordered regex rules
  couldn't (`router.py` settles the overwhelming majority of real questions
  with zero model calls)
- **Resolution Advisor Agent** — the only agent with tools, all **10** of
  them, backed by a from-scratch port of the legality rules (7 rules), duty
  arithmetic, cost model and candidate search in `challenge-1-build/core_engine/`
- **Explainer Agent** — rewrites a verified template into controller prose,
  and is never trusted to add a fact

Between the tool loop and the Explainer sits `verifier.py` — a deterministic
gate, not a model, that checks every identifier and number in the drafted
answer against what the tools actually returned. That is the one piece of
this system with no model in it at all, and it is the piece that makes
everything else trustworthy.

Two more guardrail layers sit alongside it: `pii.py` redacts sensitive data
at the output boundary, and Azure's content filters (configured per model
deployment in challenge 0) catch harmful content and prompt injection before
either agent or verifier ever sees it. If you did challenge 5, there's also
a fine-tuned Resolution Advisor deployment — its whole job is fewer, more
consistent tool calls, not different verified rates, since `verifier.py`
pins those regardless of which model is behind it.

## Skills practiced

- Building Foundry `PromptAgentDefinition` agents with and without tools
- Writing a multi-tool `FunctionTool` interface for a real reasoning engine,
  not a single lookup function
- OpenTelemetry tracing across a three-agent call chain
- Portal-based LLM-as-judge evaluation, and where it stops being the
  relevant question (see `challenge-3-evaluate/README.md`)
- Sequential SDK orchestration vs. declarative Foundry Workflows — and the
  concrete thing you give up choosing the second

## Clean up your Azure resources

```bash
./cleanup.sh
```

This deletes the resource group `challenge-0-setup/deploy.sh` created. Or, in
the Azure Portal: find the resource group named `crewops-agentathon-rg-*` and
delete it directly. Or via CLI:

```bash
az group delete --name <your-resource-group> --yes --no-wait
```

## Where the real logic came from

This lab ports the pipeline, the rules and the dataset from **dCortex Crew
Ops Advisor**, a from-scratch multi-stage crew-control advisor built for a
different hackathon. Its own documentation is worth reading if you want the
reasoning behind decisions like "why seven rules and not a general rules
engine" or "why reject a multi-agent decomposition" — most of that reasoning
is preserved in the docstrings throughout `challenge-1-build/`.
