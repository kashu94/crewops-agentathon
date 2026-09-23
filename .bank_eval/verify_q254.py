import sys
sys.path.insert(0, "/workspaces/crewops-agentathon/challenge-1-build")
import agents
from core_engine.port import JsonToolPort

def main():
    port = JsonToolPort()
    triage = agents.TriageAgent(); triage.create()
    advisor = agents.ResolutionAdvisorAgent(port); advisor.create()
    explainer = agents.ExplainerAgent(); explainer.create()

    q = "Who's on C-2291?"
    resp = agents.answer_question(q, port, triage, advisor, explainer)
    print(f"intent={resp.intent} tier={int(resp.tier)} confidence={resp.confidence}")
    for t in resp.trace:
        print(f"  {t.tool}({t.args}) -> error={t.error!r}")
    print(f"narrative: {resp.narrative}")
    print(f"unknowns: {resp.unknowns}")

    triage.cleanup(); advisor.cleanup(); explainer.cleanup()

if __name__ == "__main__":
    main()
