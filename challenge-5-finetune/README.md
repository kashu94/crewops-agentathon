# Challenge 5 (Bonus): Fine-Tune the Resolution Advisor

Time: ~1.5–2 hours end-to-end (mostly waiting on the training job, not on you)

## Objectives

By the end of this challenge, you will have:

- ✅ Generated a 190+-example fine-tuning set by **self-distillation** — no
  hand-labeling, no external annotator
- ✅ Understood exactly what this fine-tune can and cannot improve, and why
- ✅ Run an Azure OpenAI fine-tuning job against it and deployed the result
- ✅ A `deploy.py` scorecard (challenge 4) you can compare, before vs. after

## What this fine-tune is for — read this before running anything

This system's one hard rule is *"the LLM selects, sequences and narrates; it
never calculates"* (`challenge-1-build/README.md`). `verifier.py` already
rejects any answer with an unsourced claim, **regardless of which model is
behind the Resolution Advisor.** So this fine-tune cannot make answers more
*correct* — that was never the base model's job to guarantee, and it isn't
this fine-tuned model's job either.

What it targets instead is **efficiency and consistency of tool selection**:
does the Advisor call the right tool on the first try, in as few round trips
as `config.MAX_TOOL_ITERATIONS` allows, without inventing a call the
deterministic planner (`pipeline.seed_calls()`) wouldn't have made? That is
a real, measurable difference (fewer tool round-trips per question = lower
latency and lower token cost per answer), and it is honestly the whole
value proposition — if you are looking for a fine-tune that changes
correctness, this system's own architecture says that tune shouldn't exist.

## Why self-distillation, not hand-labeled examples

Hand-labeling 150–300 examples well is most of a fine-tuning project's
actual cost. This system doesn't need to pay it: `router.py` classifies
every one of the 38 real gold questions with zero model calls, and
`pipeline.seed_calls()` / `followup_calls()` already compute the *exact*
correct opening tool calls for most intents. That means the deterministic
core is already a perfect labeler for its own training data — the "teacher"
is this repo, not a bigger model or a human.

`generate_training_data.py`:

1. Takes each of the 38 questions in `challenge-1-build/data/questions.json`
   and generates real variants by substituting every entity in it (crew id,
   pairing id, station, date, rule id, gate number) for a different real
   value of the same type, drawn from the actual vendored dataset. Wording
   is untouched — only identifiers change — so every variant is
   grammatically real.
2. Re-classifies each variant with the real router and computes the oracle
   tool trace with the real planner and `tools.dispatch()`.
3. Keeps a variant only if the router matched confidently, the trace is
   non-empty, no tool call errored, and `verifier.py` confirms the resulting
   answer is fully sourced. Anything else is discarded, not patched.
4. A handful of hand-written templates fill in `EXPLAIN_RULE` and
   `CHECK_GATE`, which the 38-question gold set happens not to exercise even
   though the router classifies both correctly — see `EXTRA_TEMPLATES` in
   the script.

**Two intents are deliberately absent: `SIMULATE_WHATIF` and
`RESOLVE_ILLEGAL`.** `seed_calls()` seeds no opening call for either, by
design — they are exactly the cases meant to need the Advisor's own
judgment. There is no local oracle for them, so none is faked. `JOINT_PLAN`
coverage also depends on how a question names the disruption: "both A320
captains (VT-DXA and VT-DXB) are sick" names aircraft, not pairing ids, so
`seed_calls()` has nothing to seed from it either — the same honest limit,
not a bug.

Running it with the default settings produces **~210 examples across 13 of
17 intents** (179 train / 31 validation after an 85/15 split):

| Intent | Examples | Intent | Examples |
|---|---|---|---|
| LOOKUP_FLIGHT | 40 | LOOKUP_CREW | 17 |
| CHECK_LEGALITY | 33 | RANK_OPTIONS | 15 |
| LOOKUP_DUTY_CLOCK | 22 | CHECK_GATE | 13 |
| DRAFT_NOTIFICATION | 11 | LOOKUP_ROSTER | 10 |
| IMPACT_OF_EVENT | 11 | FIND_REPLACEMENT | 8 |
| LOOKUP_RISK | 11 | EXPLAIN_RULE | 7 |
| LOOKUP_CERT | 6 | LOOKUP_RESERVE | 6 |

## Get Started

1. **Generate the training data** (no Azure needed — this step is entirely
   local, same as `challenge-1-build`'s own tests):

   ```bash
   cd challenge-5-finetune
   python generate_training_data.py --variants-per-question 10
   ```

   Prints a generation summary (kept / discarded, and why) and writes
   `data/training.jsonl` + `data/validation.jsonl`.

2. **Confirm a fine-tunable base model is available to you.** In the
   **Foundry portal → your project → Fine-tuning → Create**, check the base
   model dropdown — this list is region- and subscription-specific and
   changes over time. Set `FINETUNE_BASE_MODEL` in `.env` to match (default:
   `gpt-4o-mini-2024-07-18`).

3. **Run the job:**

   ```bash
   python finetune.py
   ```

   Uploads both files, creates the job, and polls until it finishes,
   printing training events as they arrive. This step genuinely takes time —
   Azure OpenAI fine-tuning jobs are commonly 30 minutes to a few hours
   depending on queue depth and base model, not something to babysit
   continuously.

4. **Deploy the result.** `finetune.py` prints the fine-tuned model id on
   success. In the Foundry portal, **Deployments → Create**, select it, and
   deploy under a new name.

5. **Point the Advisor at it** — add to `.env`:

   ```
   ADVISOR_MODEL_DEPLOYMENT_NAME=<your new deployment name>
   ```

   `challenge-1-build/config.py` already reads this exact variable, so
   nothing else in the codebase changes.

6. **Compare.** Re-run `challenge-4-deploy/deploy.py`'s scorecard against
   the new deployment and compare it to a run from before this challenge.
   Look at tool-call count and iteration count per question, not the
   verified rate — the verified rate is `verifier.py`'s job, and it is
   supposed to look identical either way.

## Success Criteria

- [ ] `generate_training_data.py` writes a non-empty `training.jsonl` and
      `validation.jsonl`
- [ ] The fine-tuning job reaches `status=succeeded` in the Foundry portal
- [ ] The fine-tuned model is deployed and `ADVISOR_MODEL_DEPLOYMENT_NAME`
      points at it
- [ ] You can explain, out loud, why a higher verified rate would *not* be
      evidence the fine-tune worked, and what would be

Back to [challenge-4-deploy](../challenge-4-deploy/README.md) · Up to the
[top-level README](../README.md)
