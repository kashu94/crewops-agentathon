"""Pre-generates a `response` column for eval_portal*.jsonl by running the
real pipeline (Router -> Planner -> Resolution Advisor Agent tool loop ->
Verifier -> Explainer Agent) locally, end to end, for each question.

Why this exists: pointing a Foundry portal evaluation directly at the
Resolution Advisor Agent as the "target" invokes it for exactly one turn.
The agent's first turn is almost always a tool call (e.g. `lookup`), which
the portal has no way to execute -- these are custom Python tools backed by
this repo's own Postgres-backed core_engine, not something Foundry can run
on its own. The agent's turn ends there with no text, so every evaluator
(Coherence, Fluency, ...) fails with "Response string cannot be empty."

The fix is to run the full local pipeline first (it already knows how to
dispatch every tool call and loop until the Explainer Agent produces real
prose), capture the final narrative per question, and upload a dataset that
already has `response` filled in. The portal then scores the existing text
instead of trying to re-invoke the agent live.
"""

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

CHALLENGE_1_DIR = REPO_ROOT / "challenge-1-build"
sys.path.insert(0, str(CHALLENGE_1_DIR))

from agents import ExplainerAgent, ResolutionAdvisorAgent, TriageAgent, answer_question  # noqa: E402
from core_engine.port import JsonToolPort  # noqa: E402

PROJECT_CONNECTION_STRING = None
TRIAGE_AGENT_NAME = "crew-triage-agent"
ADVISOR_AGENT_NAME = "crew-resolution-advisor-agent"
EXPLAINER_AGENT_NAME = "crew-explainer-agent"


def _connect_existing_agents(port: JsonToolPort):
    """Reuses whatever agents are already deployed -- same pattern as
    deploy.py's ensure_agents_deployed, but never creates a new version."""
    import os

    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    project_connection = os.getenv("PROJECT_CONNECTION_STRING")

    def _attach(agent_obj, name):
        agent_obj.client = AIProjectClient(endpoint=project_connection, credential=DefaultAzureCredential())
        agent_obj.openai = agent_obj.client.get_openai_client()
        agent_obj.agent = next(a for a in agent_obj.client.agents.list() if a.name == name)
        return agent_obj

    triage = _attach(TriageAgent(), TRIAGE_AGENT_NAME)
    advisor = _attach(ResolutionAdvisorAgent(port), ADVISOR_AGENT_NAME)
    explainer_agent = _attach(ExplainerAgent(), EXPLAINER_AGENT_NAME)
    return triage, advisor, explainer_agent


def main(src_path: str, dst_path: str):
    src = Path(src_path)
    dst = Path(dst_path)

    port = JsonToolPort()
    triage, advisor, explainer_agent = _connect_existing_agents(port)

    rows = [json.loads(line) for line in src.read_text(encoding="utf-8").splitlines() if line.strip()]
    out_rows = []
    for i, row in enumerate(rows, 1):
        query = row["query"]
        print(f"  [{i}/{len(rows)}] {query[:70]}")
        response = answer_question(query, port, triage, advisor, explainer_agent)
        out_rows.append({
            "query": query,
            "response": response.narrative,
            "ground_truth": row.get("ground_truth", ""),
        })

    with dst.open("w", encoding="utf-8") as f:
        for r in out_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    empty = sum(1 for r in out_rows if not r["response"].strip())
    print(f"\nWrote {len(out_rows)} rows to {dst} ({empty} with an empty response)")


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "eval_portal.jsonl"
    dst = sys.argv[2] if len(sys.argv) > 2 else "eval_portal_with_responses.jsonl"
    main(src, dst)
