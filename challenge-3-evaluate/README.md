# Challenge 3: Evaluate

Time: ~30 minutes

## Objectives

By the end of this challenge, you will have:

- ✅ Uploaded a gold-question dataset to the Foundry portal
- ✅ Run an LLM-as-judge evaluation (Coherence + Fluency) against the
  Resolution Advisor Agent
- ✅ A repeatable score to compare before and after any prompt or model change

## Context

`../challenge-1-build/data/questions.json` is the real gold-question set this
scenario is built around: **38 questions** — 16 tier-1 lookups, 14 tier-2
replacement/legality questions, 8 tier-3 consequence questions — each with a
worked `expected_answer`. It exists precisely so a claim like "the Resolution
Advisor Agent handles legality checks well" is a number, not a vibe.

Two things are worth separating, and the original project this is ported from
is explicit about not blurring them:

1. **Correctness** — does the final *answer* match `expected_answer`? That is
   what `../challenge-4-deploy/evaluation_dataset.json` (all 38 questions, in
   `{id, input, expected_output}` form) is for — see Challenge 4.
2. **Coherence / Fluency** — is the *prose* well-formed and readable? That is
   what this challenge's portal-based LLM-as-judge evaluation checks.

Neither one substitutes for the other, and neither substitutes for
`verifier.py`'s own deterministic check, which is stricter than both: it does
not ask whether an answer is fluent or plausible, only whether every number
and id in it actually came from a tool call. A fluent, coherent, wrong answer
passes both evaluators here and is still rejected by the verifier before it
ever reaches a controller.

`eval_portal.jsonl` holds a 12-question cross-tier sample, and
`eval_portal_full38.jsonl` all 38, both in the flattened
`{"query": ..., "ground_truth": ...}` shape the Foundry portal's dataset
upload expects. Start with the 12-question sample; switch to the full 38 once
that runs cleanly.

## Get Started

1. In the **Foundry portal** → your project → **Evaluation** → **Datasets**,
   upload `eval_portal.jsonl` (or `eval_portal_full38.jsonl` for the full set).

2. Create a new evaluation run against the **Resolution Advisor Agent**
   (`crew-resolution-advisor-agent`), selecting the **Coherence** and
   **Fluency** evaluators.

3. Run it, and open the per-question breakdown. For any question scoring
   low, open its trace (Challenge 2 wired this up) and check: did the right
   tools get called? Was the draft rejected by `verifier.py` and replaced
   with the deterministic template — which is correct, but reads tersely,
   and would legitimately score lower on "fluency" than a good model polish?

4. Re-run after any change to `../challenge-1-build/prompts/system.md` or
   `explainer.py`'s polish instructions, and compare.

## Success Criteria

- [ ] `eval_portal.jsonl` uploaded as a Foundry dataset
- [ ] An evaluation run completed against the Resolution Advisor Agent
- [ ] You can point to one low-scoring question and explain, from its trace,
      why it scored the way it did

Next: [Challenge 4 — Production Workflow](../challenge-4-deploy/README.md)
