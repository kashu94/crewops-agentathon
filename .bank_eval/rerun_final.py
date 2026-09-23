import sys, json, time
sys.path.insert(0, "/workspaces/crewops-agentathon/challenge-1-build")
import agents
from core_engine.port import JsonToolPort

FAILED_NUMS = {148, 178, 185}

def main():
    print("cooling down 180s before retry...", flush=True)
    time.sleep(180)

    questions = json.load(open("/workspaces/crewops-agentathon/.bank_eval/questions.json"))
    todo = [q for q in questions if q["num"] in FAILED_NUMS]

    port = JsonToolPort()
    triage = agents.TriageAgent(); triage.create()
    advisor = agents.ResolutionAdvisorAgent(port); advisor.create()
    explainer = agents.ExplainerAgent(); explainer.create()
    print(f"Advisor: {advisor.agent.name} v{advisor.agent.version}", flush=True)

    results = json.load(open("/workspaces/crewops-agentathon/.bank_eval/rerun_results.json"))
    for i, q in enumerate(todo):
        if i > 0:
            time.sleep(30)
        print(f"\nQ{q['num']}: {q['question']}", flush=True)
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
            print(f"  narrative: {resp.narrative[:300]}", flush=True)
        except Exception as e:
            record["error"] = f"{type(e).__name__}: {e}"
            print(f"  STILL FAILED: {record['error']}", flush=True)
        record["elapsed_s"] = round(time.time() - t0, 1)
        results[str(q["num"])] = record
        with open("/workspaces/crewops-agentathon/.bank_eval/rerun_results.json", "w") as f:
            json.dump(results, f, indent=2)

    triage.cleanup(); advisor.cleanup(); explainer.cleanup()
    print("\nDone.", flush=True)

if __name__ == "__main__":
    main()
