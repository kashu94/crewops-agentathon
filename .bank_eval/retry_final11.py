import sys, json, time, traceback
sys.path.insert(0, "/workspaces/crewops-agentathon/challenge-1-build")
import agents
from core_engine.port import JsonToolPort

RETEST_PATH = "/workspaces/crewops-agentathon/.bank_eval/retest_results.jsonl"
BANK_PATH = "/workspaces/crewops-agentathon/.bank_eval/bank_results.jsonl"
OUT_PATH = "/workspaces/crewops-agentathon/.bank_eval/retry_final11_results.jsonl"

TARGETS = [253, 254, 272, 275, 289, 297, 305, 313, 148, 146, 147]


def main():
    by_num = {}
    for path in (RETEST_PATH, BANK_PATH):
        for l in open(path):
            r = json.loads(l)
            by_num.setdefault(r["num"], r)

    port = JsonToolPort()
    triage = agents.TriageAgent(); triage.create()
    advisor = agents.ResolutionAdvisorAgent(port); advisor.create()
    explainer = agents.ExplainerAgent(); explainer.create()
    print(f"Advisor: {advisor.agent.name} v{advisor.agent.version}", flush=True)
    print(f"Explainer: {explainer.agent.name} v{explainer.agent.version}", flush=True)

    with open(OUT_PATH, "w") as out:
        for i, num in enumerate(TARGETS, 1):
            old = by_num[num]
            q = old["question"]
            print(f"[{i}/{len(TARGETS)}] Q{num}: {q}", flush=True)
            record = {"num": num, "question": q,
                      "prior_error": old.get("new_error", old.get("error", ""))}
            for attempt in range(1, 4):
                try:
                    resp = agents.answer_question(q, port, triage, advisor, explainer)
                    record["narrative"] = resp.narrative
                    record["intent"] = str(resp.intent)
                    record["tool_calls"] = [
                        {"tool": t.tool, "args": t.args, "error": t.error} for t in resp.trace
                    ]
                    break
                except Exception as e:
                    record["error"] = f"{type(e).__name__}: {e}"
                    if attempt < 3:
                        time.sleep(20 * attempt)
                        continue
                    traceback.print_exc()
                    break
            out.write(json.dumps(record) + "\n")
            out.flush()
            time.sleep(4)

    triage.cleanup(); advisor.cleanup(); explainer.cleanup()
    print("Done.", flush=True)


if __name__ == "__main__":
    main()
