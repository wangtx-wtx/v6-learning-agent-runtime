# -*- coding: utf-8 -*-
"""Phase D 真实冒烟：复习包生成（真实模型）→ 作答 SM-2 → 完成。"""
import io, json, sys, time, urllib.request, urllib.error

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8801"

def req(method, path, payload=None, timeout=20):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}

# 1. 找有笔记的章节
s, courses = req("GET", "/api/courses")
cid = (courses[0] if isinstance(courses, list) else courses["items"][0])["id"]
s, chapters = req("GET", f"/api/chapters?course_id={cid}")
chid = chapters[0]["id"] if isinstance(chapters, list) else None
print(f"[0] course={cid} chapter={chid}")

# 2. 触发真实 review 流
s, r = req("POST", "/api/workflows/review", {"course_id": cid, "chapter_id": chid, "kind": "chapter"})
print(f"[1] review 触发: {s} {r}")
assert s == 202
rid = r["run_id"]

def wait_run(run_id, timeout=240):
    for _ in range(timeout // 2):
        _, body = req("GET", f"/api/runs/{run_id}")
        st = body["run"]["status"]
        if st in ("completed", "failed", "cancelled"):
            return st, body
        time.sleep(2)
    return "timeout", body

st, body = wait_run(rid)
print(f"[2] run {rid}: {st}")
assert st == "completed", body["run"].get("error")

# 3. 拿到 review_id
sys.path.insert(0, ".")
from app.database import fetch_one, reset_connections
review = fetch_one("SELECT id, status FROM reviews ORDER BY id DESC LIMIT 1")
review_id = review["id"]
print(f"[3] review {review_id} status={review['status']}")
assert review["status"] == "generated", review["status"]

# 4. 复习详情（答案隐藏）
s, detail = req("GET", f"/api/reviews/{review_id}")
qs = detail["questions"]
print(f"[4] 复习包: {len(qs)} 题, 答案隐藏={all('answer' not in q for q in qs)}")
assert len(qs) > 0 and all("answer" not in q for q in qs), qs

# 5. 逐题作答（先乱答第一题，再正确作答下一题/同题）
s, a1 = req("POST", f"/api/reviews/{review_id}/attempts",
            {"question_no": str(qs[0]["question_no"]), "user_answer": "我不知道", "self_rating": 1})
print(f"[5] 答错: correct={a1['is_correct']} mastery {a1['mastery_before']}→{a1['mastery_after']} interval→{a1['interval_after_days']}d")
assert s == 200 and a1["is_correct"] is False and a1["interval_after_days"] == 1.0
# 作答后答案揭示
_, d2 = req("GET", f"/api/reviews/{review_id}")
q0 = next(q for q in d2["questions"] if str(q["question_no"]) == str(qs[0]["question_no"]))
assert "answer" in q0, "作答后答案应揭示"
# 第二次作答：用揭示的答案答对（自评 4 → 间隔 ×2.5）
target = qs[1] if len(qs) > 1 else qs[0]
_, d3 = req("GET", f"/api/reviews/{review_id}?reveal=1")
q1 = next(q for q in d3["questions"] if str(q["question_no"]) == str(target["question_no"]))
s, a2 = req("POST", f"/api/reviews/{review_id}/attempts",
            {"question_no": str(target["question_no"]), "user_answer": q1["answer"], "self_rating": 4})
print(f"[6] 答对: correct={a2['is_correct']} interval→{a2['interval_after_days']}d")
assert s == 200 and a2["is_correct"] and a2["interval_after_days"] == 2.5

# 6. 完成
s, comp = req("POST", f"/api/reviews/{review_id}/complete")
print(f"[7] 完成: {comp}")
assert s == 200 and comp["attempts"] == 2 and comp["score"] == 0.5
s, _ = req("POST", f"/api/reviews/{review_id}/complete")
assert s == 409
reset_connections()
print("PHASE D SMOKE ALL OK")
