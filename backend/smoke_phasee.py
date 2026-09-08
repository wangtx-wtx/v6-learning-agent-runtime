# -*- coding: utf-8 -*-
"""Phase E 真实冒烟：听课(critic+修订+Obsidian真实路径)、错题(结构化+事件)、作业(状态机+先做后看)。"""
import io, json, sys, time, urllib.request, urllib.error, base64

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

def wait_run(run_id, timeout=300):
    for _ in range(timeout // 2):
        _, body = req("GET", f"/api/runs/{run_id}")
        st = body["run"]["status"]
        if st in ("completed", "failed", "cancelled"):
            return st, body
        time.sleep(2)
    return "timeout", body

# ===== A. 听课流：真实 critic + 修订闭环 + Obsidian 真实路径 =====
s, lessons = req("GET", "/api/lessons")
lid = lessons[0]["id"]
s, mats = req("GET", "/api/materials")
mids = [m["id"] for m in (mats if isinstance(mats, list) else [])][:2]
s, r = req("POST", "/api/workflows/lesson",
           {"lesson_id": lid, "material_ids": mids,
            "transcript": "折射定律演示实验：测量入射角与折射角，验证 sin i / sin r 为常数。"})
print(f"[A1] lesson run: {s} id={r['run_id']}")
st, body = wait_run(r["run_id"])
print(f"[A2] {st}")
assert st == "completed", body["run"].get("error")
nodes = {n["node_name"]: n["status"] for n in body["nodes"]}
critic_node = next(n for n in body["nodes"] if n["node_name"] == "critic")
print(f"[A3] nodes: {nodes}")

sys.path.insert(0, ".")
from app.database import fetch_one, fetch_all, reset_connections
note = fetch_one("SELECT * FROM notes ORDER BY id DESC LIMIT 1")
print(f"[A4] note {note['id']} status={note['status']} markdown_path={note['markdown_path']}")
assert note["markdown_path"], "未同步 vault"
assert f"[note-{note['id']}]" in note["markdown_path"], f"缺少稳定ID命名: {note['markdown_path']}"
assert "Optics" in note["markdown_path"] or "01 Courses" in note["markdown_path"], note["markdown_path"]
import os
vault = os.path.join("data", "obsidian_vault")
full = os.path.join(vault, note["markdown_path"])
assert os.path.exists(full), f"vault 文件不存在: {full}"
critic_out = json.loads(critic_node["output_json"]) if critic_node["output_json"] else {}
print(f"[A5] critic: passed={critic_out.get('quality_review', {}).get('passed')} revisions={critic_out.get('revisions')} score={critic_out.get('quality_review', {}).get('score')}")
assert "quality_review" in critic_out

# ===== B. 错题流：文本错题 → 结构化错因 → confirm/reject + 事件 =====
s, r = req("POST", "/api/workflows/error",
           {"course_id": 1, "question_text": "光从空气射入水中，折射角是否可能大于入射角？为什么？",
            "student_answer": "可能，因为光很强", "correct_answer": "不可能，光从光疏介质射入光密介质时折射角小于入射角"})
print(f"[B1] error run: {s} id={r['run_id']}")
st, body = wait_run(r["run_id"])
print(f"[B2] {st}")
assert st == "completed", body["run"].get("error")
err = fetch_one("SELECT * FROM errors ORDER BY id DESC LIMIT 1")
cause = json.loads(err["ai_error_json"])
print(f"[B3] error {err['id']} status={err['status']} root_cause={cause.get('root_cause','')[:40]}")
assert "root_cause" in cause and err["status"] == "provisional"
s, c = req("POST", f"/api/errors/{err['id']}/confirm")
print(f"[B4] confirm: {s}")
assert s == 200
s, _ = req("POST", f"/api/errors/{err['id']}/reject")
assert s == 409
events = fetch_all("SELECT * FROM error_events WHERE error_id=?", (err["id"],))
print(f"[B5] events: {[e['event_type'] for e in events]}")
assert [e["event_type"] for e in events] == ["confirmed"]

# ===== C. 作业流：文本 → 求解 → 先做后看 =====
s, r = req("POST", "/api/workflows/homework",
           {"course_id": 1, "homework_text": "一、光的折射定律内容是什么？\n二、计算：入射角30度，折射率1.5，求折射角。"})
print(f"[C1] homework run: {s} id={r['run_id']}")
st, body = wait_run(r["run_id"])
print(f"[C2] {st}")
assert st == "completed", body["run"].get("error")
hw = fetch_one("SELECT * FROM homeworks ORDER BY id DESC LIMIT 1")
print(f"[C3] homework {hw['id']} status={hw['status']}")
assert hw["status"] in ("solved", "needs_review")
# 先做后看：未提交 → 无解答
s, d = req("GET", f"/api/homeworks/{hw['id']}")
assert all("answer" not in q for q in d["questions"]), "未提交答案却看到解答！"
# 提交第一题
s, a = req("POST", f"/api/homeworks/{hw['id']}/answer",
           {"question_id": d["questions"][0]["id"], "student_answer": "sin i / sin r = n"})
assert s == 200
s, d2 = req("GET", f"/api/homeworks/{hw['id']}")
q0 = next(q for q in d2["questions"] if q["id"] == d["questions"][0]["id"])
assert "answer" in q0, "提交后仍看不到解答"
assert all("answer" not in q for q in d2["questions"] if q["id"] != d["questions"][0]["id"]), "未提交题不应揭示"
print(f"[C4] 先做后看 OK: 题1已揭示, 其余仍隐藏")
reset_connections()

print("PHASE E SMOKE ALL OK")
