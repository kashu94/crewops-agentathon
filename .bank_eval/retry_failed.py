import sys, json, time, traceback
sys.path.insert(0, "/workspaces/crewops-agentathon/challenge-1-build")
import agents
from core_engine.port import JsonToolPort

RESULTS_PATH = "/workspaces/crewops-agentathon/.bank_eval/bank_results.jsonl"
FAILED_NUMS = {3, 55, 148, 178, 185, 314}  # 300 handled separately -- deterministic content-filter block
MAX_ATTEMPTS = 4


def rewrite(rows):
    with open(RESULTS_PATH, "w") as out:
        for r in rows:
            out.write(json.dumps(r) + "\n")


def main():
    rows = [json.loads(l) for l in open(RESULTS_PATH)]
    by_num = {r["num"]: r for r in rows}
    targets = [n for n in FAILED_NUMS if "error" in by_num[n]]
    print(f"Retrying {len(targets)} failed questions: {sorted(targets)}", flush=True)

    port = JsonToolPort()
    triage = agents.TriageAgent(); triage.create()
    advisor = agents.ResolutionAdvisorAgent(port); advisor.create()
    explainer = agents.ExplainerAgent(); explainer.create()

    for i, num in enumerate(sorted(targets), 1):
        r = by_num[num]
        print(f"[{i}/{len(targets)}] Q{num}: {r['question']}", flush=True)
        record = {k: v for k, v in r.items() if k != "error"}
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = agents.answer_question(r["question"], port, triage, advisor, explainer)
                record["intent"] = str(resp.intent)
                record["tier"] = int(resp.tier)
                record["confidence"] = str(resp.confidence)
                record["narrative"] = resp.narrative
                record["unknowns"] = resp.unknowns
                record["tool_calls"] = [
                    {"tool": t.tool, "args": t.args, "error": t.error} for t in resp.trace
                ]
                record.pop("error", None)
                break
            except Exception as e:
                record["error"] = f"{type(e).__name__}: {e}"
                is_rate_limit = "RateLimitError" in type(e).__name__
                print(f"  attempt {attempt} failed: {type(e).__name__}", flush=True)
                if attempt < MAX_ATTEMPTS and is_rate_limit:
                    time.sleep(20 * attempt)
                    continue
                traceback.print_exc()
                break
        by_num[num] = record
        rewrite([by_num[r["num"]] for r in rows])  # flush after every question
        time.sleep(8)

    triage.cleanup(); advisor.cleanup(); explainer.cleanup()
    print("Done.")


if __name__ == "__main__":
    main()
