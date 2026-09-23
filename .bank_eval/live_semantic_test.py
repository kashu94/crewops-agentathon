import sys, traceback
sys.path.insert(0, "/workspaces/crewops-agentathon/challenge-1-build")
import agents
from core_engine.port import JsonToolPort

QUESTIONS = [
    "how long do crew have to rest between duties?",   # search_rules paraphrase
    "what is the name of C-10?",                         # malformed id -> suggest_crew_ids
    "how many controllers are there?",                   # LOOKUP_CONTROLLERS
    "how many issues does each controller have?",        # LOOKUP_CONTROLLERS
    "is captain A Nair available?",                      # route_semantic fallback
]

def main():
    port = JsonToolPort()
    triage = agents.TriageAgent(); triage.create()
    advisor = agents.ResolutionAdvisorAgent(port); advisor.create()
    explainer = agents.ExplainerAgent(); explainer.create()
    print(f"Advisor: {advisor.agent.name} v{advisor.agent.version}", flush=True)

    for i, q in enumerate(QUESTIONS, 1):
        print(f"\n{'='*70}\nQ{i}: {q}", flush=True)
        try:
            resp = agents.answer_question(q, port, triage, advisor, explainer)
            print(f"  intent={resp.intent} tier={int(resp.tier)} confidence={resp.confidence}")
            for t in resp.trace:
                print(f"    - {t.tool}({t.args}) -> error={t.error!r}")
            print(f"  narrative: {resp.narrative}")
        except Exception:
            traceback.print_exc()

    triage.cleanup(); advisor.cleanup(); explainer.cleanup()
    print("\nDone.")

if __name__ == "__main__":
    main()
