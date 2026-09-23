import re, json

TEXT = open("/workspaces/crewops-agentathon/.bank_eval/test_question_bank.md", encoding="utf-8").read()

CATEGORY_RE = re.compile(r"^# (\d+)\. (.+?) — \d+\s*$", re.MULTILINE)
Q_RE = re.compile(
    r'^(\d+)\.\s+"(.*?)"(?:\s+\([^)]*\))?\s+—\s+\*\*\[([^\]]+)\]\*\*\s+—\s+(.*)$',
    re.MULTILINE,
)

categories = []
for m in CATEGORY_RE.finditer(TEXT):
    categories.append((m.start(), int(m.group(1)), m.group(2).strip()))

def category_for(pos):
    best = None
    for start, num, name in categories:
        if start <= pos:
            best = (num, name)
        else:
            break
    return best

questions = []
for m in Q_RE.finditer(TEXT):
    num = int(m.group(1))
    qtext = m.group(2)
    tag = m.group(3)
    note = m.group(4)
    cat = category_for(m.start())
    questions.append({
        "num": num,
        "category_num": cat[0] if cat else None,
        "category_name": cat[1] if cat else None,
        "question": qtext,
        "tag": tag,
        "note": note,
    })

print(f"Extracted {len(questions)} questions")
nums = [q["num"] for q in questions]
missing = sorted(set(range(1, 319)) - set(nums))
print("Missing numbers:", missing)
dupes = [n for n in set(nums) if nums.count(n) > 1]
print("Duplicate numbers:", dupes)

with open("/workspaces/crewops-agentathon/.bank_eval/questions.json", "w") as f:
    json.dump(questions, f, indent=2)
