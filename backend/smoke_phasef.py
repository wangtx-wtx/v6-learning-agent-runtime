# -*- coding: utf-8 -*-
"""Phase F 真实冒烟：混合检索落库、model_calls 审计、prompt 外置 checksum、schema 绑定流。"""
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
        return e.code, {}

def wait_run(run_id, timeout=300):
    for _ in range(timeout // 2):
        _, body = req("GET", f"/api/runs/{run_id}")
        st = body["run"]["status"]
        if st in ("completed", "failed", "cancelled"):
            return st, body
        time.sleep(2)
    return "timeout", body

sys.path.insert(0, ".")
from app.database import fetch_all, fetch_one, reset_connections

# ===== A. 听课流：hybrid 检索 + model_calls 审计 + prompt checksum =====
s, lessons = req("GET", "/api/lessons")
lid = lessons[0]["id"]
s, mats = req("GET", "/api/materials")
mids = [m["id"] for m in (mats if isinstance(mats, list) else [])][:2]
s, r = req("POST", "/api/workflows/lesson",
           {"lesson_id": lid, "material_ids": mids, "transcript": "折射定律与全反射现象讲解。"})
run_id = r["run_id"]
print(f"[F1] lesson run {run_id}: {s}")
st, body = wait_run(run_id)
print(f"[F2] {st}")
assert st == "completed", body["run"].get("error")

rr = fetch_all("SELECT * FROM retrieval_runs WHERE workflow_run_id=?", (run_id,))
print(f"[F3] retrieval_runs: {[(x['retrieval_mode'], x['candidate_count'], len(json.loads(x['selected_chunk_ids']))) for x in rr]}")
assert rr, "retrieval_runs 未落库"
mc = fetch_all("SELECT node_id, prompt_name, model_id, tokens_in, tokens_out, status, prompt_version FROM model_calls WHERE run_id=?", (run_id,))
print(f"[F4] model_calls: {len(mc)} 行")
for m in mc:
    print(f"     {m['node_id']:<18} prompt={m['prompt_name']:<22} model={m['model_id']:<16} in={m['tokens_in']} out={m['tokens_out']} {m['status']}")
assert len(mc) >= 3, "model_calls 审计不足"
nodes = {n["node_name"]: n["status"] for n in body["nodes"]}
print(f"[F5] nodes: {nodes}")
assert all(v == "success" for v in nodes.values()), nodes

# ===== B. 错题流：schema 绑定下的结构化错因（真实模型） =====
s, r = req("POST", "/api/workflows/error",
           {"course_id": 1, "question_text": "全反射发生的条件是什么？",
            "student_answer": "任何角度都会全反射", "correct_answer": "光从光密介质射向光疏介质且入射角大于临界角"})
rid = r["run_id"]
st, body = wait_run(rid)
print(f"[F6] error run {rid}: {st}")
assert st == "completed", body["run"].get("error")
mc2 = fetch_all("SELECT node_id, status FROM model_calls WHERE run_id=?", (rid,))
print(f"[F7] model_calls: {[(m['node_id'], m['status']) for m in mc2]}")
err = fetch_one("SELECT id, ai_error_json FROM errors ORDER BY id DESC LIMIT 1")
cause = json.loads(err["ai_error_json"])
assert cause.get("root_cause"), "结构化错因缺失"
print(f"[F8] structured cause OK: root_cause={cause['root_cause'][:30]}")
reset_connections()
print("PHASE F SMOKE ALL OK")
