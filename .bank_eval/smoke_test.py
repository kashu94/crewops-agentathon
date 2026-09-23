import sys, traceback
sys.path.insert(0, "/workspaces/crewops-agentathon/challenge-1-build")
import agents
from core_engine.port import JsonToolPort

QUESTIONS = [
    "Captain C-1042 just called in sick for P-2291, who do I use?",   # tier 2, find_options
    "What's the rule about flying an aircraft you're not rated on?",   # search_rules paraphrase
    "Is C-2087 legal to cover P-2291?",                                 # tier 2, check_legality
    "Both A320 captains on VT-DXA and VT-DXB are sick. Plan it.",       # tier 3, joint_plan
    "What if VT-DXA is 90 minutes late off DX401 on 16 Sep?",           # tier 3, simulate
    "Who's the busiest controller right now?",                         # LOOKUP_CONTROLLERS
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
            print(f"  narrative: {resp.narrative[:400]}")
        except Exception:
            traceback.print_exc()

    triage.cleanup(); advisor.cleanup(); explainer.cleanup()
    print("\nDone.")

if __name__ == "__main__":
    main()
