import sys, json, time, traceback
sys.path.insert(0, "/workspaces/crewops-agentathon/challenge-1-build")
import agents
from core_engine.port import JsonToolPort

RESULTS_PATH = "/workspaces/crewops-agentathon/.bank_eval/bank_results.jsonl"
RETEST_PATH = "/workspaces/crewops-agentathon/.bank_eval/retest_results.jsonl"

# All 43 confirmed hallucinations from the scoring pass, plus the full
# "Impact of event" category (141-151, ripple's own category) since the
# ripple() resolution fix targets it directly.
HALLUCINATIONS = [
    15, 16, 19, 26, 55, 61, 63, 64, 66, 211, 253, 254,
    3, 8, 13, 21, 42, 56, 67, 79, 122, 206, 207, 209, 289,
    99, 313, 162, 132, 163, 179, 212, 216, 269, 275, 305, 316,
    46, 228, 238, 249, 272, 297,
]
IMPACT_CATEGORY = list(range(141, 152))
TARGETS = sorted(set(HALLUCINATIONS + IMPACT_CATEGORY))


def main():
    old_by_num = {json.loads(l)["num"]: json.loads(l) for l in open(RESULTS_PATH)}
    print(f"Re-testing {len(TARGETS)} targeted questions", flush=True)

    port = JsonToolPort()
    triage = agents.TriageAgent(); triage.create()
    advisor = agents.ResolutionAdvisorAgent(port); advisor.create()
    explainer = agents.ExplainerAgent(); explainer.create()
    print(f"Advisor: {advisor.agent.name} v{advisor.agent.version}", flush=True)

    with open(RETEST_PATH, "w") as out:
        for i, num in enumerate(TARGETS, 1):
            old = old_by_num[num]
            q = old["question"]
            print(f"[{i}/{len(TARGETS)}] Q{num}: {q}", flush=True)
            record = {"num": num, "category_name": old["category_name"],
                      "question": q, "tag": old["tag"], "note": old["note"],
                      "old_narrative": old.get("narrative", old.get("error", ""))}
            for attempt in range(1, 4):
                try:
                    resp = agents.answer_question(q, port, triage, advisor, explainer)
                    record["new_narrative"] = resp.narrative
                    record["new_intent"] = str(resp.intent)
                    record["new_tool_calls"] = [
                        {"tool": t.tool, "args": t.args, "error": t.error} for t in resp.trace
                    ]
                    break
                except Exception as e:
                    record["new_error"] = f"{type(e).__name__}: {e}"
                    if attempt < 3 and "RateLimit" in type(e).__name__:
                        time.sleep(15 * attempt)
                        continue
                    traceback.print_exc()
                    break
            out.write(json.dumps(record) + "\n")
            out.flush()
            time.sleep(3)

    triage.cleanup(); advisor.cleanup(); explainer.cleanup()
    print("Done.", flush=True)


if __name__ == "__main__":
    main()
