import sys, json, time, traceback
sys.path.insert(0, "/workspaces/crewops-agentathon/challenge-1-build")

import agents
from core_engine.port import JsonToolPort

QUESTIONS_PATH = "/workspaces/crewops-agentathon/.bank_eval/questions.json"
RESULTS_PATH = "/workspaces/crewops-agentathon/.bank_eval/bank_results.jsonl"


def already_done():
    done = set()
    try:
        with open(RESULTS_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    done.add(json.loads(line)["num"])
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    return done


def main():
    questions = json.load(open(QUESTIONS_PATH))
    done = already_done()
    todo = [q for q in questions if q["num"] not in done]
    print(f"{len(questions)} total, {len(done)} already done, {len(todo)} remaining", flush=True)
    if not todo:
        print("Nothing to do.")
        return

    port = JsonToolPort()
    print("Creating agents...", flush=True)
    triage = agents.TriageAgent(); triage.create()
    advisor = agents.ResolutionAdvisorAgent(port); advisor.create()
    explainer = agents.ExplainerAgent(); explainer.create()
    print(f"Advisor: {advisor.agent.name} v{advisor.agent.version}", flush=True)

    start = time.time()
    try:
        with open(RESULTS_PATH, "a") as out:
            for i, q in enumerate(todo, 1):
                t0 = time.time()
                record = {
                    "num": q["num"], "category_num": q["category_num"],
                    "category_name": q["category_name"], "question": q["question"],
                    "tag": q["tag"], "note": q["note"],
                }
                try:
                    resp = agents.answer_question(q["question"], port, triage, advisor, explainer)
                    record["intent"] = str(resp.intent)
                    record["tier"] = int(resp.tier)
                    record["confidence"] = str(resp.confidence)
                    record["narrative"] = resp.narrative
                    record["unknowns"] = resp.unknowns
                    record["tool_calls"] = [
                        {"tool": t.tool, "args": t.args, "error": t.error} for t in resp.trace
                    ]
                except Exception as e:
                    record["error"] = f"{type(e).__name__}: {e}"
                    traceback.print_exc()
                record["elapsed_s"] = round(time.time() - t0, 1)
                out.write(json.dumps(record) + "\n")
                out.flush()
                elapsed = time.time() - start
                print(f"[{i}/{len(todo)}] Q{q['num']} ({q['category_name']}) "
                      f"{record.get('elapsed_s')}s -- total {elapsed/60:.1f}min", flush=True)
    finally:
        print("Cleaning up agents...", flush=True)
        try:
            triage.cleanup()
        except Exception:
            pass
        try:
            advisor.cleanup()
        except Exception:
            pass
        try:
            explainer.cleanup()
        except Exception:
            pass
        print("Done.", flush=True)


if __name__ == "__main__":
    main()
