"""
Challenge 1: Build Agents — SDK Track
Triage Agent, Resolution Advisor Agent and Explainer Agent for dCortex Air's
Crew Ops Advisor.

Usage:
    python agents.py

Ports dCortex Crew Ops Advisor's pipeline — ROUTER -> PLANNER -> TOOL LOOP ->
VERIFIER -> EXPLAINER — onto three Microsoft Foundry `PromptAgentDefinition`
agents, following the same 2 h template as the other scenarios in this repo
(a tool-using agent + a pure-reasoning agent) but with the exact agent count
and tool set dCortex's own team shipped: their docs describe the system as
"a pipeline with up to three model calls", which is exactly what these three
classes are.

  TriageAgent              no tools   — classifies tier/intent, only called
                                         when router.py's regex rules abstain
  ResolutionAdvisorAgent   10 tools   — the tool loop; every number, id and
                                         verdict in the final answer comes
                                         from one of its tool calls
  ExplainerAgent           no tools   — rewrites the verified template into
                                         controller prose; never adds a fact

The legality math (7 rules), duty-hour arithmetic, cost model and candidate
search live in `core_engine/`, ported from dCortex's own `core/` package —
the model never computes any of it, only chooses which tool answers the
question in front of it.
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition
from azure.identity import DefaultAzureCredential
from openai.types.responses.response_input_param import FunctionCallOutput


# Resolve repo root by finding .env in parent directories.
def _find_repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / ".env").exists():
            return parent
    return Path(__file__).resolve().parents[1]


REPO_ROOT = _find_repo_root()
load_dotenv(REPO_ROOT / ".env")

# challenge-1-build's own modules (schemas, tools, core_engine/, ...) live
# next to this file, so running `python agents.py` from inside this folder
# puts them on sys.path automatically.
import config
import pii
import pipeline
import router
import verifier
from core_engine.port import JsonToolPort
from explainer import polish, render, collect_citations
from prompts import INTENT_GUIDANCE, base_system_prompt
from schemas import AdvisorResponse, Confidence, TraceEntry
from tools import TOOL_SCHEMAS, dispatch, foundry_tools

PROJECT_CONNECTION_STRING = os.getenv("PROJECT_CONNECTION_STRING")
MODEL_DEPLOYMENT_NAME = os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-5.4")


# =============================================================================
# Triage Agent — no tools, only reached when router.py's regex rules abstain
# =============================================================================

class TriageAgent:
    def __init__(self):
        self.agent = None
        self.client = None
        self.openai = None

    def create(self):
        """Create the triage agent in Foundry."""
        self.client = AIProjectClient(
            endpoint=PROJECT_CONNECTION_STRING,
            credential=DefaultAzureCredential(),
        )
        self.openai = self.client.get_openai_client()

        self.agent = self.client.agents.create_version(
            agent_name="crew-triage-agent",
            definition=PromptAgentDefinition(
                model=config.TRIAGE_MODEL_DEPLOYMENT_NAME,
                instructions=router.ROUTER_INSTRUCTIONS,
            ),
        )
        return self.agent

    def run(self, system: str, user_text: str) -> str:
        """Classify one question. `system` is accepted to match the
        `router.TriageFn` signature; it is already baked into the agent's
        instructions above, so only the question itself is sent."""
        conversation = self.openai.conversations.create()

        response = self.openai.responses.create(
            input=user_text,
            conversation=conversation.id,
            extra_body={"agent_reference": {"name": self.agent.name, "type": "agent_reference"}},
        )

        self.openai.conversations.delete(conversation_id=conversation.id)
        return response.output_text

    def cleanup(self):
        if self.agent:
            self.client.agents.delete_version(
                agent_name=self.agent.name,
                agent_version=self.agent.version,
            )
        if self.client:
            self.client.close()


# =============================================================================
# Resolution Advisor Agent — all 10 real tools, backed by core_engine/
# =============================================================================

class ResolutionAdvisorAgent:
    def __init__(self, port: JsonToolPort):
        self.port = port
        self.agent = None
        self.client = None
        self.openai = None

    def create(self):
        """Create the resolution advisor agent in Foundry."""
        self.client = AIProjectClient(
            endpoint=PROJECT_CONNECTION_STRING,
            credential=DefaultAzureCredential(),
        )
        self.openai = self.client.get_openai_client()

        self.agent = self.client.agents.create_version(
            agent_name="crew-resolution-advisor-agent",
            definition=PromptAgentDefinition(
                model=config.ADVISOR_MODEL_DEPLOYMENT_NAME,
                instructions=base_system_prompt(),
                tools=foundry_tools(),
            ),
        )
        return self.agent

    def run_intent(self, route, query_text: str) -> list[TraceEntry]:
        """Seed deterministic calls, then let the model request more.

        Returns the full trace. The model's own final text is never read —
        `pipeline.build_answer()` builds the typed answer purely from the
        trace, so the model's only job here is choosing which tools to call.
        """
        trace: list[TraceEntry] = []
        seen: set[str] = set()

        def run_calls(calls) -> None:
            for call in calls:
                signature = f"{call.name}:{sorted((call.args or {}).items())!r}"
                if signature in seen:
                    continue
                seen.add(signature)
                trace.append(dispatch(self.port, call.name, call.args))

        run_calls(pipeline.seed_calls(route))
        run_calls(pipeline.followup_calls(route, trace))

        # A PromptAgentDefinition's instructions are fixed for the agent
        # version, unlike a raw chat completion's system message — so the
        # per-intent guidance that dCortex varies per call is folded into the
        # input text instead of a system override.
        guidance = INTENT_GUIDANCE.get(route.intent, "").strip()
        opening = (f"{guidance}\n\n" if guidance else "") + f"Controller's question: {query_text}"

        conversation = self.openai.conversations.create()
        agent_ref = {"agent_reference": {"name": self.agent.name, "type": "agent_reference"}}

        response = self.openai.responses.create(
            input=opening, conversation=conversation.id, extra_body=agent_ref,
        )

        iterations = 0
        while iterations < config.MAX_TOOL_ITERATIONS:
            function_calls = [item for item in response.output if item.type == "function_call"]
            if not function_calls:
                break
            iterations += 1

            outputs, new_entries = [], 0
            for item in function_calls:
                args = json.loads(item.arguments or "{}")
                signature = f"{item.name}:{sorted(args.items())!r}"
                if signature in seen:
                    # Small models loop: results are deterministic, so a
                    # repeat adds nothing but latency.
                    entry = TraceEntry(tool=item.name, args=args, error="repeat call skipped")
                else:
                    seen.add(signature)
                    entry = dispatch(self.port, item.name, args)
                    trace.append(entry)
                    new_entries += 1
                outputs.append(FunctionCallOutput(
                    type="function_call_output",
                    call_id=item.call_id,
                    output=str(entry.result if entry.result is not None else entry.error),
                ))

            if new_entries == 0:
                break
            response = self.openai.responses.create(
                input=outputs, conversation=conversation.id, extra_body=agent_ref,
            )

        self.openai.conversations.delete(conversation_id=conversation.id)
        return trace

    def cleanup(self):
        if self.agent:
            self.client.agents.delete_version(
                agent_name=self.agent.name,
                agent_version=self.agent.version,
            )
        if self.client:
            self.client.close()


# =============================================================================
# Explainer Agent — no tools, pure reasoning over an already-verified answer
# =============================================================================

class ExplainerAgent:
    def __init__(self):
        self.agent = None
        self.client = None
        self.openai = None

    def create(self):
        """Create the explainer agent in Foundry."""
        self.client = AIProjectClient(
            endpoint=PROJECT_CONNECTION_STRING,
            credential=DefaultAzureCredential(),
        )
        self.openai = self.client.get_openai_client()

        self.agent = self.client.agents.create_version(
            agent_name="crew-explainer-agent",
            definition=PromptAgentDefinition(
                model=config.EXPLAINER_MODEL_DEPLOYMENT_NAME,
                instructions=(
                    "You rewrite a deterministic crew-ops answer into concise, "
                    "controller-facing prose, following the specific "
                    "instructions given in each message. "
                    "You may reword, reorder and compress. "
                    "You may NOT add any identifier, number, name or claim "
                    "that is not already present in what you are given — a "
                    "verifier checks this afterwards and discards your "
                    "answer if you do."
                ),
            ),
        )
        return self.agent

    def run(self, system: str, user_content: str) -> str:
        """One tool-free call: `system` is this request's specific
        instructions, `user_content` is the deterministic template to
        rewrite. Matches `explainer.ExplainFn`."""
        conversation = self.openai.conversations.create()

        response = self.openai.responses.create(
            input=f"{system}\n\n---\n\n{user_content}",
            conversation=conversation.id,
            extra_body={"agent_reference": {"name": self.agent.name, "type": "agent_reference"}},
        )

        self.openai.conversations.delete(conversation_id=conversation.id)
        return response.output_text

    def cleanup(self):
        if self.agent:
            self.client.agents.delete_version(
                agent_name=self.agent.name,
                agent_version=self.agent.version,
            )
        if self.client:
            self.client.close()


# =============================================================================
# The full pipeline, wired together — ROUTER -> PLANNER -> TOOL LOOP ->
# VERIFIER -> EXPLAINER
# =============================================================================

def answer_question(
    query: str,
    port: JsonToolPort,
    triage: TriageAgent,
    advisor: ResolutionAdvisorAgent,
    explainer_agent: ExplainerAgent,
) -> AdvisorResponse:
    """One controller question, start to finish."""
    if mismatch := pipeline.rank_mismatch(query, port):
        route = router.route(query, triage.run)
        response = AdvisorResponse(tier=route.tier, intent=route.intent, query=query)
        response.narrative = mismatch
        response.awaiting = "confirmation"
        response.confidence = Confidence.HIGH
        return response

    route = router.route(query, triage.run)
    trace = advisor.run_intent(route, query)

    response = AdvisorResponse(
        tier=route.tier,
        intent=route.intent,
        query=query,
        entities=route.entities.to_dict(),
        answer=pipeline.build_answer(route, trace),
        trace=trace,
    )

    if not trace:
        response.narrative = pipeline.explain_no_tools(route)
        response.confidence = Confidence.LOW
        return response

    narrative = render(response)
    narrative = polish(response, explainer_agent.run)

    result = verifier.verify(narrative, trace)
    if not result.ok:
        # The Explainer Agent's draft made an unsourced claim: fall back to
        # the deterministic template, which can only restate tool output.
        narrative = render(response)
        if response.confidence is Confidence.HIGH:
            response.confidence = Confidence.MEDIUM
        if bad := ", ".join(c.value for c in result.unsupported):
            response.unknowns.append(
                f"The Explainer Agent's draft claimed {bad}, which no tool "
                f"output supports. That draft was discarded — what is shown "
                f"above is rendered directly from the tool results."
            )

    response.narrative = narrative
    response.citations = collect_citations(response)
    return response


def print_response(query: str, response: AdvisorResponse) -> None:
    """Prints to the controller's screen -- the output boundary. PII
    redaction runs here (see `pii.py`), never on `response` itself: the
    verifier has already checked the unredacted narrative against the
    unredacted trace by this point, and redacting first would make every
    real number in it look unsourced."""
    print(f"\n{'=' * 70}\nQ: {query}")
    print(f"   intent={response.intent}  tier={int(response.tier)}  "
          f"confidence={response.confidence}  tool_calls={len(response.trace)}")
    print("-" * 70)
    print(pii.redact_pii_text(response.narrative))
    if response.unknowns:
        print("\n[unknowns]")
        for note in response.unknowns:
            print(f"  - {pii.redact_pii_text(note)}")


# =============================================================================
# Main — build all three agents, run a smoke test against real questions
# =============================================================================

DEMO_QUESTIONS = [
    "Who is on reserve at BLR?",                                    # tier 1
    "What does RULE-DUTY-02 say?",                                  # tier 1
    "Captain C-1042 just called in sick for P-2291, who do I use?", # tier 2
    "Is C-2087 legal to cover P-2291?",                             # tier 2
]


def main():
    if not PROJECT_CONNECTION_STRING:
        print("PROJECT_CONNECTION_STRING not set. Run challenge 0 first!")
        sys.exit(1)

    port = JsonToolPort()

    print("=== Triage Agent ===")
    triage = TriageAgent()
    triage.create()
    print(f"Created: {triage.agent.name} (version {triage.agent.version})")

    print("\n=== Resolution Advisor Agent ===")
    advisor = ResolutionAdvisorAgent(port)
    advisor.create()
    print(f"Created: {advisor.agent.name} (version {advisor.agent.version})  "
          f"— {len(TOOL_SCHEMAS)} tools")

    print("\n=== Explainer Agent ===")
    explainer_agent = ExplainerAgent()
    explainer_agent.create()
    print(f"Created: {explainer_agent.agent.name} (version {explainer_agent.agent.version})")

    print("\nRunning the pipeline against sample questions from data/questions.json...")
    for query in DEMO_QUESTIONS:
        response = answer_question(query, port, triage, advisor, explainer_agent)
        print_response(query, response)

    # Cleanup — comment out to keep agents visible in the Foundry portal
    # triage.cleanup()
    # advisor.cleanup()
    # explainer_agent.cleanup()


if __name__ == "__main__":
    main()
