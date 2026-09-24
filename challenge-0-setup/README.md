# Challenge 0: Setup

Time: ~20 minutes

## Objectives

By the end of this challenge, you will have:

- ✅ A Microsoft Foundry project provisioned in your Azure subscription
- ✅ A chat-completion model deployed (default `gpt-5.4`)
- ✅ Application Insights wired up for Challenge 2's tracing
- ✅ A `.env` file at the repo root with everything the later challenges need

## Context

Everything downstream — the three agents in Challenge 1, the tracing in
Challenge 2, the evaluation in Challenge 3, the orchestration in Challenge 4 —
reads its configuration from one `.env` file at the repo root. This challenge
creates it.

## Get Started

1. **Fork or clone this repository**, then open it in a Codespace or locally
   (`az login` first if running locally).

2. **Confirm your Azure role.** You need **Contributor** on the subscription
   or resource group (control plane, to create resources) **and** the
   **Azure AI User** / **Foundry User** role (data plane, to build and run
   agents once the project exists). Contributor alone is not enough — it will
   let you provision the project but not create or invoke an agent inside it.

3. **Run the deploy script:**

   ```bash
   cd challenge-0-setup
   chmod +x deploy.sh
   ./deploy.sh
   ```

   This provisions, in `swedencentral`:
   - a Microsoft Foundry account + project (`crewops-project` by default)
   - a `gpt-5.4` model deployment
   - a Log Analytics workspace + Application Insights instance, connected to
     the Foundry account

   It writes the result to `../.env` — every script in every later challenge
   discovers this file by walking up its own parent directories, so it only
   has to exist once, at the repo root.

4. **Verify in the Azure / Foundry portals** that the resource group, the
   Foundry project and the model deployment all show up before moving on.

## Content safety guardrails (do this now, while you're already here)

Every model deployment in Azure AI Foundry ships with a **default content
filter** already attached — Hate & Fairness, Sexual, Violence and Self-Harm,
each blocking at Medium severity on both prompt and completion. That part
needs no action. Two things are worth doing explicitly before Challenge 1:

1. **Foundry portal → your project → Safety + Security → Content filters.**
   Review the default policy, or clone it into a custom one if you want to
   raise a category's severity threshold, or turn on **Prompt Shields**
   (jailbreak / indirect-prompt-injection detection — relevant here because
   the Resolution Advisor reads tool *results*, which is exactly the kind of
   untrusted-content channel prompt injection targets) and **Protected
   Material** detection.
2. **Deployments → your model deployment → Edit → content filter** — attach
   your custom policy (or confirm the default is attached) to
   `MODEL_DEPLOYMENT_NAME`, and to `TRIAGE_MODEL_DEPLOYMENT_NAME` /
   `ADVISOR_MODEL_DEPLOYMENT_NAME` / `EXPLAINER_MODEL_DEPLOYMENT_NAME`
   separately if you've split them onto different deployments.

No code change is required for this layer — it runs before a request
reaches the model and after a response leaves it, entirely inside Azure. It
sits alongside this system's other two guardrails, not in place of them:

| Layer | Catches | Where |
|---|---|---|
| Azure content filters | Harmful/unsafe content, prompt injection (Prompt Shields) | Foundry deployment config, this step |
| `verifier.py` | An answer claiming a number/id no tool call actually returned | `challenge-1-build/verifier.py`, every answer |
| `pii.py` | Email/phone/PAN/passport/Aadhaar/card numbers, address/DOB/health fields, if such fields are ever added to the dataset | `challenge-1-build/pii.py`, at the output boundary |

None of the three substitutes for another: content filters don't know what a
"sourced number" means, `verifier.py` doesn't know what a slur is, and
neither knows what a phone number looks like in free text.

## Success Criteria

- [ ] `deploy.sh` completed without error
- [ ] `../.env` exists and has non-empty `PROJECT_CONNECTION_STRING` and
      `MODEL_DEPLOYMENT_NAME` values
- [ ] The project is visible at `ai.azure.com/nextgen`
- [ ] You've reviewed the content filter attached to your model deployment
      (default is fine — the point is knowing it's there, not customizing it)

Next: [Challenge 1 — Build Agents](../challenge-1-build/README.md)
