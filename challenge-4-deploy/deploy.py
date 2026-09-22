"""
Challenge 4: Production Workflow -- SDK Track
Multi-agent orchestration workflow for dCortex Air's Crew Ops Advisor.

Two orchestration patterns, side by side, exactly like the other scenarios in
this repo:

  Part A -- sequential SDK orchestration (this file drives the three agents
            directly: Triage -> Resolution Advisor tool loop -> deterministic
            verifier gate -> Explainer -> re-verify). Runs the full 38-question
            gold set from evaluation_dataset.json and prints a scorecard.
  Part B -- a declarative Foundry Workflow (WorkflowAgentDefinition), a
            3-step InvokeAzureAgent chain visible in the portal's visual
            workflow builder.

Part B cannot reproduce the verifier gate -- a Workflow's steps are agent
invocations with no deterministic Python step in between -- so it chains the
three agents directly and is explicitly the smaller-fidelity of the two
paths. That trade-off is called out below and in the README, not hidden.
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def _find_repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / ".env").exists():
            return parent
    return Path(__file__).resolve().parents[1]


REPO_ROOT = _find_repo_root()
load_dotenv(REPO_ROOT / ".env")

# The engine, agents and pipeline glue all live in challenge-1-build/ --
# referenced from here rather than duplicated, the same way the other
# scenarios' challenge-4-deploy.py reaches back into challenge-1-build for
# their data file.
CHALLENGE_1_DIR = REPO_ROOT / "challenge-1-build"
sys.path.insert(0, str(CHALLENGE_1_DIR))

import config
from agents import ExplainerAgent, ResolutionAdvisorAgent, TriageAgent, answer_question
from core_engine.port import JsonToolPort

PROJECT_CONNECTION_STRING = os.getenv("PROJECT_CONNECTION_STRING")
MODEL_DEPLOYMENT_NAME = os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-5.4")
EVAL_DATASET_PATH = Path(__file__).resolve().parent / "evaluation_dataset.json"

TRIAGE_AGENT_NAME = "crew-triage-agent"
ADVISOR_AGENT_NAME = "crew-resolution-advisor-agent"
EXPLAINER_AGENT_NAME = "crew-explainer-agent"
WORKFLOW_AGENT_NAME = os.getenv("WORKFLOW_AGENT_NAME", "")


def _load_eval_questions() -> list[dict]:
    return json.loads(EVAL_DATASET_PATH.read_text(encoding="utf-8"))


# =============================================================================
# Part A: sequential SDK orchestration
# =============================================================================

def ensure_agents_deployed(port: JsonToolPort) -> tuple[TriageAgent, ResolutionAdvisorAgent, ExplainerAgent]:
    """Create all three agents if not already deployed; reuse existing ones."""
    print("=== Step 1: Ensure Agents Are Deployed ===")

    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    probe = AIProjectClient(endpoint=PROJECT_CONNECTION_STRING, credential=DefaultAzureCredential())
    existing = {a.name for a in probe.agents.list()}
    probe.close()

    triage = TriageAgent()
    if TRIAGE_AGENT_NAME in existing:
        print(f"  Found existing: {TRIAGE_AGENT_NAME}")
        triage.client = AIProjectClient(endpoint=PROJECT_CONNECTION_STRING, credential=DefaultAzureCredential())
        triage.openai = triage.client.get_openai_client()
        triage.agent = next(a for a in triage.client.agents.list() if a.name == TRIAGE_AGENT_NAME)
    else:
        triage.create()
        print(f"  Deployed: {TRIAGE_AGENT_NAME}")

    advisor = ResolutionAdvisorAgent(port)
    if ADVISOR_AGENT_NAME in existing:
        print(f"  Found existing: {ADVISOR_AGENT_NAME}")
        advisor.client = AIProjectClient(endpoint=PROJECT_CONNECTION_STRING, credential=DefaultAzureCredential())
        advisor.openai = advisor.client.get_openai_client()
        advisor.agent = next(a for a in advisor.client.agents.list() if a.name == ADVISOR_AGENT_NAME)
    else:
        advisor.create()
        print(f"  Deployed: {ADVISOR_AGENT_NAME}")

    explainer_agent = ExplainerAgent()
    if EXPLAINER_AGENT_NAME in existing:
        print(f"  Found existing: {EXPLAINER_AGENT_NAME}")
        explainer_agent.client = AIProjectClient(endpoint=PROJECT_CONNECTION_STRING, credential=DefaultAzureCredential())
        explainer_agent.openai = explainer_agent.client.get_openai_client()
        explainer_agent.agent = next(a for a in explainer_agent.client.agents.list() if a.name == EXPLAINER_AGENT_NAME)
    else:
        explainer_agent.create()
        print(f"  Deployed: {EXPLAINER_AGENT_NAME}")

    return triage, advisor, explainer_agent


def run_gold_question_scorecard(port, triage, advisor, explainer_agent) -> dict:
    """Run every question in evaluation_dataset.json through the full
    pipeline and report a per-tier verified rate.

    This is a *sourcing* scorecard, not a correctness grader: it reports
    how many answers passed verifier.py (every claim traced to a tool call),
    which is the property this whole architecture exists to guarantee.
    Comparing the verified narrative's content against each question's
    `expected_output` by hand is the next step, same as the other scenarios'
    Coherence/Fluency portal evaluation is a starting point, not the final
    word.
    """
    print("\n=== Step 2: Gold-Question Scorecard ===")
    questions = _load_eval_questions()

    by_tier: dict[int, list[bool]] = {1: [], 2: [], 3: []}
    for q in questions:
        response = answer_question(q["input"], port, triage, advisor, explainer_agent)
        verified = response.confidence.value != "low" and not response.awaiting
        by_tier[q["tier"]].append(verified)
        mark = "PASS" if verified else "  ? "
        print(f"  [{mark}] {q['id']} (tier {q['tier']}): {q['input'][:70]}")

    return by_tier


def print_scorecard(by_tier: dict[int, list[bool]]) -> None:
    print("\n" + "=" * 60)
    print("CREW OPS ADVISOR — GOLD QUESTION SCORECARD")
    print("=" * 60)
    total_ok = total_n = 0
    for tier, results in sorted(by_tier.items()):
        ok, n = sum(results), len(results)
        total_ok += ok
        total_n += n
        label = {1: "Tier 1 — Lookup", 2: "Tier 2 — Replacement", 3: "Tier 3 — Consequence"}[tier]
        print(f"  {label:<24} {ok}/{n} verified")
    print(f"  {'TOTAL':<24} {total_ok}/{total_n} verified")
    print("=" * 60)


# =============================================================================
# Part B: declarative Foundry Workflow
# =============================================================================

def create_workflow_agent(workflow_agent_name: str = "crew-ops-advisor-workflow") -> str:
    """Create a workflow agent via the SDK using WorkflowAgentDefinition.

    Chains the three agents directly: Triage -> Resolution Advisor ->
    Explainer. Visible in the Foundry portal under Build -> Agents
    (kind: workflow), and editable there with the visual workflow builder.

    Note: a Workflow step is an agent invocation; it cannot run the plain
    Python verifier gate that Part A runs between the tool loop and the
    Explainer. That gate is what makes this architecture's answers trust-
    worthy, so treat this path as a portal-friendly demo of the *shape* of
    the pipeline, not a drop-in replacement for Part A's SDK orchestration.
    """
    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import WorkflowAgentDefinition
    from azure.identity import DefaultAzureCredential

    client = AIProjectClient(
        endpoint=PROJECT_CONNECTION_STRING,
        credential=DefaultAzureCredential(),
        allow_preview=True,
    )

    workflow_yaml = (
        "kind: Workflow\n"
        f"name: {workflow_agent_name}\n"
        "description: dCortex Crew Ops Advisor - triage, resolve, explain\n"
        "trigger:\n"
        "  kind: OnConversationStart\n"
        "  id: trigger_start\n"
        "  actions:\n"
        "    - kind: InvokeAzureAgent\n"
        "      id: step_triage\n"
        "      agent:\n"
        f"        name: {TRIAGE_AGENT_NAME}\n"
        "      conversationId: =System.ConversationId\n"
        "      input:\n"
        '        messages: ""\n'
        "      output:\n"
        "        autoSend: true\n"
        "    - kind: InvokeAzureAgent\n"
        "      id: step_resolve\n"
        "      agent:\n"
        f"        name: {ADVISOR_AGENT_NAME}\n"
        "      conversationId: =System.ConversationId\n"
        "      input:\n"
        '        messages: ""\n'
        "      output:\n"
        "        autoSend: true\n"
        "    - kind: InvokeAzureAgent\n"
        "      id: step_explain\n"
        "      agent:\n"
        f"        name: {EXPLAINER_AGENT_NAME}\n"
        "      conversationId: =System.ConversationId\n"
        "      input:\n"
        '        messages: ""\n'
        "      output:\n"
        "        autoSend: true\n"
        "    - kind: EndConversation\n"
        "      id: step_end\n"
    )

    existing_names = {a.name for a in client.agents.list()}
    result = client.agents.create_version(
        agent_name=workflow_agent_name,
        definition=WorkflowAgentDefinition(workflow=workflow_yaml),
        description="dCortex Crew Ops Advisor workflow (SDK-created)",
    )
    verb = "Updated" if workflow_agent_name in existing_names else "Created"
    print(f"  {verb} workflow agent: {result.name} (version {result.version})")
    print("  Visible in Foundry portal -> Build -> Agents (kind: workflow)")
    client.close()
    return result.name


def run_portal_workflow(workflow_name: str, query: str) -> str:
    """Invoke a WorkflowAgentDefinition agent via the Responses API
    (background + poll, the same pattern used elsewhere in this repo for
    long-running workflow invocation)."""
    import time
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    client = AIProjectClient(
        endpoint=PROJECT_CONNECTION_STRING,
        credential=DefaultAzureCredential(),
        allow_preview=True,
    )
    openai_client = client.get_openai_client()

    print(f"\n=== Portal Workflow: {workflow_name} ===")
    print(f"  Query: {query}")

    conversation = openai_client.conversations.create()
    resp = openai_client.responses.create(
        conversation=conversation.id,
        extra_body={"agent_reference": {"name": workflow_name, "type": "agent_reference"}},
        input=query,
        background=True,
    )
    print(f"  Response ID : {resp.id}")

    output_text = ""
    for attempt in range(12):
        time.sleep(8)
        r = openai_client.responses.retrieve(resp.id)
        print(f"  [{attempt + 1}] status={r.status}")
        if r.status in ("completed", "failed", "cancelled"):
            output_text = r.output_text
            break

    if output_text:
        print("\nWorkflow output:")
        print(output_text)
    else:
        print("\n  No text output via the API yet -- check the Foundry portal's Traces panel.")

    openai_client.conversations.delete(conversation_id=conversation.id)
    client.close()
    return output_text


def main():
    if not PROJECT_CONNECTION_STRING:
        print("PROJECT_CONNECTION_STRING not set. Run challenge 0 first!")
        sys.exit(1)

    port = JsonToolPort()

    # --- Part A: Python orchestration with the verifier gate in the loop ---
    triage, advisor, explainer_agent = ensure_agents_deployed(port)
    by_tier = run_gold_question_scorecard(port, triage, advisor, explainer_agent)
    print_scorecard(by_tier)

    # --- Part B: SDK workflow creation + portal invocation ---
    print("\n" + "=" * 60)
    print("CREATING WORKFLOW AGENT VIA SDK")
    print("=" * 60)
    workflow_name = WORKFLOW_AGENT_NAME if WORKFLOW_AGENT_NAME and not WORKFLOW_AGENT_NAME.startswith("<") else "crew-ops-advisor-workflow"
    workflow_name = create_workflow_agent(workflow_agent_name=workflow_name)

    print("\n" + "=" * 60)
    print("INVOKING WORKFLOW (BACKGROUND POLL)")
    print("=" * 60)
    run_portal_workflow(workflow_name, "Who is on reserve at BLR?")

    print("\n" + "=" * 60)
    print("CHALLENGE 4 COMPLETE")
    print("=" * 60)
    print("  Part A: Sequential SDK orchestration + verifier gate  DONE")
    print(f"  Part B: Workflow agent deployed                      DONE  ({workflow_name})")
    print("          -> View in Foundry portal -> Build -> Agents")


if __name__ == "__main__":
    main()
