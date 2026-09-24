# Challenge 4: Production Workflow

Time: ~30 minutes

## Objectives

By the end of this challenge, you will have:

- ✅ Run the full pipeline — Triage → Resolution Advisor → verifier gate →
  Explainer — over all **38** gold questions and produced a scorecard
- ✅ Built a declarative Foundry **Workflow** chaining the three agents,
  visible in the portal's visual workflow builder
- ✅ Understood exactly where the SDK path and the portal Workflow path
  diverge, and why

## Two orchestration patterns

`deploy.py` demonstrates both:

**Part A — sequential SDK orchestration.** Plain Python calls each agent in
turn and puts the deterministic verifier gate *between* the tool loop and the
Explainer — exactly like `challenge-1-build/agents.py::answer_question()`,
run here over the full 38-question set from `evaluation_dataset.json` instead
of four demo questions. This is the pattern to actually deploy: a
Function/Container App calling `answer_question()` behind an HTTP endpoint.

**Part B — a declarative Workflow.** `WorkflowAgentDefinition` with three
`InvokeAzureAgent` steps (Triage → Resolution Advisor → Explainer), created
via the SDK and editable afterwards in the portal's drag-and-drop workflow
designer. This is genuinely useful for demoing the *shape* of the pipeline to
a non-technical stakeholder — but read the caveat below before treating it as
equivalent to Part A.

## The trade-off, stated plainly

A Workflow step is an agent invocation. There is no slot in the YAML for a
plain Python function — so the portal Workflow **cannot run `verifier.py`
between the Resolution Advisor and the Explainer**. That gate is what makes
every number in this system's answers traceable to a real tool call; skip it
and you have three agents talking to each other with nothing checking that
the last one didn't just make something up.

This is not a Foundry limitation to work around — it is the same conclusion
dCortex Crew Ops Advisor's own team reached when they evaluated (and
rejected) a multi-agent decomposition: *"the LLM selects, sequences and
narrates; it never calculates."* A workflow of agents talking to each other
is the wrong shape for work that a deterministic gate can check exactly and
instantly. Use Part B to demo the pipeline. Use Part A — or a hosted endpoint
built the same way — to actually serve controllers.

## Get Started

```bash
cd challenge-4-deploy
python deploy.py
```

This will:

1. Ensure all three agents from Challenge 1 are deployed (idempotent — reuses
   them if they already exist).
2. Run every question in `evaluation_dataset.json` through the full pipeline
   and print a per-tier verified scorecard.
3. Create (or update) `crew-ops-advisor-workflow` as a Foundry Workflow.
4. Invoke it once via the Responses API in background mode, polling for
   completion, and print the result.

## Beyond the Lab

- **Hosted endpoint** — wrap `answer_question()` in a FastAPI/Azure Functions
  handler; the SDK path already returns a typed `AdvisorResponse`, so the
  handler is mostly serialization.
- **Streaming** — the original project's `docs/API_CONTRACT.md` defines an
  SSE contract (`token | tool_call | tool_result | done`) for a console to
  render the pipeline live; worth reproducing if you build the Angular-style
  console this scenario's dataset was designed for.
- **CI evaluation gate** — run `evaluation_dataset.json` on every prompt
  change to `challenge-1-build/prompts/system.md` and fail the build if the
  verified rate drops.
- **Held-out check** — the original dataset ships two scenarios that were
  never tuned against, scored once as an honest self-check. Worth keeping
  that discipline here too: don't let the 38 gold questions become the thing
  you optimise the prompt to pass.

## Success Criteria

- [ ] `deploy.py` prints a scorecard with a verified rate for each tier
- [ ] `crew-ops-advisor-workflow` is visible in the Foundry portal under
      **Build → Agents** as `kind: workflow`
- [ ] You can explain, out loud, why Part B skips the verifier gate and Part
      A does not

You've completed the Crew Ops Agent-a-thon lab. See [`../wrapup.md`](../wrapup.md)
for cleanup and a recap.
