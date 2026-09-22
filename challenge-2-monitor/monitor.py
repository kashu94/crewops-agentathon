"""
Challenge 2: Monitor with Application Insights — SDK Track
Enable GenAI tracing and verify traces appear in App Insights.

Usage:
    python monitor.py

IMPORTANT: Environment variables must be set BEFORE importing azure.ai.projects!
"""

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv


def _find_repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / ".env").exists():
            return parent
    return Path(__file__).resolve().parents[1]


env_path = _find_repo_root() / ".env"
load_dotenv(env_path)

if os.getenv("AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING") != "true":
    print("AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING is not set to 'true' in .env")
    print("   Add: AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING=true")
    sys.exit(1)

PROJECT_CONNECTION_STRING = os.getenv("PROJECT_CONNECTION_STRING")
MODEL_DEPLOYMENT_NAME = os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-5.4")
APPINSIGHTS_CONN_STRING = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")


def setup_tracing():
    """Configure OpenTelemetry instrumentation and Azure Monitor export."""
    print("=== Setting up tracing ===")
    print("AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING is enabled")

    from azure.ai.projects.telemetry import AIProjectInstrumentor
    AIProjectInstrumentor().instrument()
    print("AIProjectInstrumentor configured")

    from azure.monitor.opentelemetry import configure_azure_monitor
    configure_azure_monitor(
        connection_string=APPINSIGHTS_CONN_STRING,
        enable_live_metrics=True,
    )
    print("Azure Monitor exporter connected")


def run_traced_agent_call():
    """Make an agent call that will be captured as a trace.

    A throwaway agent, deliberately not one of the three from Challenge 1 —
    this challenge is about the tracing pipeline itself, not about crew-ops
    logic, so it stays self-contained and disposable.
    """
    print("\n=== Running traced agent call ===")

    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import PromptAgentDefinition
    from azure.identity import DefaultAzureCredential

    client = AIProjectClient(
        endpoint=PROJECT_CONNECTION_STRING,
        credential=DefaultAzureCredential(),
    )
    openai_client = client.get_openai_client()

    agent = client.agents.create_version(
        agent_name="crew-tracing-test-agent",
        definition=PromptAgentDefinition(
            model=MODEL_DEPLOYMENT_NAME,
            instructions=(
                "You are a crew-control monitoring assistant for dCortex Air. "
                "Summarise the operational snapshot you are given in two sentences."
            ),
        ),
    )

    conversation = openai_client.conversations.create()
    response = openai_client.responses.create(
        input=(
            "Snapshot at 2026-09-14T18:00Z: 150 crew, 147 flights, 39 pairings, "
            "16 reserves on call. One flagged exception: a recurrent-training "
            "certification expiring 2026-09-17 against a duty on 2026-09-19. "
            "Summarise the state of the operation."
        ),
        conversation=conversation.id,
        extra_body={"agent_reference": {"name": agent.name, "type": "agent_reference"}},
    )
    print(f"Agent responded: {response.output_text[:120]}...")

    openai_client.conversations.delete(conversation_id=conversation.id)
    client.agents.delete_version(agent_name=agent.name, agent_version=agent.version)
    client.close()


def verify_traces():
    """Wait for traces to propagate and verify they appear in App Insights."""
    print("\n=== Verifying traces in App Insights ===")

    if not APPINSIGHTS_CONN_STRING:
        print("APPLICATIONINSIGHTS_CONNECTION_STRING not set — skipping verification")
        print("You can still check traces manually in the Azure Portal")
        return

    print("Waiting for traces to propagate (30 seconds)...")
    time.sleep(30)

    print("Traces should now be visible in Application Insights")
    print("   Go to: Azure Portal -> Application Insights -> Transaction search")
    print("   Filter by: Last 5 minutes, Event type: Dependency")


def main():
    if not PROJECT_CONNECTION_STRING:
        print("PROJECT_CONNECTION_STRING not set. Run challenge 0 first!")
        sys.exit(1)

    setup_tracing()
    run_traced_agent_call()
    verify_traces()

    print("\nMonitoring is active! Check App Insights for the full trace view.")
    print("Once this works, re-run challenge-1-build/agents.py with tracing")
    print("enabled (same env vars) to see the Triage -> Resolution Advisor ->")
    print("Explainer call chain as one connected trace per question.")


if __name__ == "__main__":
    main()
