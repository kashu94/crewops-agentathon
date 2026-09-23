import sys, traceback
sys.path.insert(0, "/workspaces/crewops-agentathon/challenge-1-build")
import agents
from core_engine.port import JsonToolPort

QUESTIONS = [
    "is C-1024 available?",                                  # crew id near-miss -> C-1042
    "is A. Nayar available?",                                 # name typo -> A. Nair
    "Which flights fly Bangalore to Bombay on 17 Sep?",       # city-name resolution
    "who is C-9999?",                                         # genuinely fake, must not hallucinate
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
