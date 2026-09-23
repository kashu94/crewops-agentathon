import sys, json, time, traceback
sys.path.insert(0, "/workspaces/crewops-agentathon/challenge-1-build")
import agents
from core_engine.port import JsonToolPort

FAILED_NUMS = {3, 55, 148, 178, 185, 300, 314}

def main():
    questions = json.load(open("/workspaces/crewops-agentathon/.bank_eval/questions.json"))
    todo = [q for q in questions if q["num"] in FAILED_NUMS]

    port = JsonToolPort()
    triage = agents.TriageAgent(); triage.create()
    advisor = agents.ResolutionAdvisorAgent(port); advisor.create()
    explainer = agents.ExplainerAgent(); explainer.create()
    print(f"Advisor: {advisor.agent.name} v{advisor.agent.version}", flush=True)

    results = {}
    for q in todo:
        print(f"\n{'='*70}\nQ{q['num']}: {q['question']}", flush=True)
        t0 = time.time()
        record = {"num": q["num"], "category_num": q["category_num"],
                   "category_name": q["category_name"], "question": q["question"],
                   "tag": q["tag"], "note": q["note"]}
        try:
            resp = agents.answer_question(q["question"], port, triage, advisor, explainer)
            record["intent"] = str(resp.intent)
            record["tier"] = int(resp.tier)
            record["confidence"] = str(resp.confidence)
            record["narrative"] = resp.narrative
            record["unknowns"] = resp.unknowns
            record["tool_calls"] = [{"tool": t.tool, "args": t.args, "error": t.error} for t in resp.trace]
            print(f"  narrative: {resp.narrative[:300]}")
        except Exception as e:
            record["error"] = f"{type(e).__name__}: {e}"
            print(f"  STILL FAILED: {record['error']}")
            traceback.print_exc()
        record["elapsed_s"] = round(time.time() - t0, 1)
        results[q["num"]] = record

    triage.cleanup(); advisor.cleanup(); explainer.cleanup()

    with open("/workspaces/crewops-agentathon/.bank_eval/rerun_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nDone.")

if __name__ == "__main__":
    main()
