# -*- coding: utf-8 -*-
"""Phase B 真实启动冒烟：lifespan + worker 并发 + 取消 + 重试。"""
import io, json, sys, time, urllib.request, urllib.error

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8801"

def req(method, path, payload=None, timeout=15):
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

# 1. 健康检查
s, health = req("GET", "/api/health")
print("[1] health:", s, json.dumps(health, ensure_ascii=False)[:120])
assert s == 200

# 2. 定位真实课时与材料（materials 为课程级，lesson 流通过 material_ids 带入）
s, lessons = req("GET", "/api/lessons")
assert s == 200 and isinstance(lessons, list) and lessons, f"lessons {s}"
lid = lessons[0]["id"]
s, mats = req("GET", "/api/materials")
mids = [m["id"] for m in (mats if isinstance(mats, list) else mats.get("items", []))][:2]
print(f"[2] lesson={lid} material_ids={mids}")
LESSON_PAYLOAD = {"lesson_id": lid, "material_ids": mids,
                  "transcript": "光的折射：入射角与折射角关系实验，记录入射角30度折射角20度。"}

# 3. 两个 lesson run 真并发（202 + supervisor 领取）
t0 = time.time()
s1, r1 = req("POST", "/api/workflows/lesson", LESSON_PAYLOAD)
s2, r2 = req("POST", "/api/workflows/lesson", LESSON_PAYLOAD)
print(f"[3] 两个 lesson run: {s1} id={r1.get('run_id')} | {s2} id={r2.get('run_id')} ({time.time()-t0:.2f}s)")
assert s1 == 202 and s2 == 202
id1, id2 = r1["run_id"], r2["run_id"]

def wait_runs(ids, statuses=("completed", "failed"), timeout=300):
    deadline = time.time() + timeout
    out = {}
    while time.time() < deadline:
        for rid in ids:
            if rid in out:
                continue
            _, body = req("GET", f"/api/runs/{rid}")
            run = body.get("run") if isinstance(body, dict) else None
            st = (run or {}).get("status")
            if st in statuses:
                out[rid] = st
        if len(out) == len(ids):
            return out
        time.sleep(2.0)
    return out

res = wait_runs([id1, id2], timeout=300)
el = time.time() - t0
print(f"[4] 并发执行结果: {res} 耗时 {el:.1f}s")
assert res.get(id1) == "completed" and res.get(id2) == "completed", "runs 未完成"

# 5. 取消语义：run 可能已被秒领（running），取消后应到达 cancelled 终态
s, r3 = req("POST", "/api/workflows/lesson", LESSON_PAYLOAD)
rid3 = r3["run_id"]
s, c = req("POST", f"/api/runs/{rid3}/cancel")
print(f"[5] 取消 run {rid3}: {s} {c}")
assert s == 200 and c["status"] in ("cancelled", "cancellation_requested")
st3 = wait_runs([rid3], statuses=("cancelled",), timeout=60).get(rid3)
_, run3 = req("GET", f"/api/runs/{rid3}")
assert run3["run"]["status"] == "cancelled", f"取消未生效: {run3['run']['status']}"

# 6. 终态 run 取消 → 409
s, _ = req("POST", f"/api/runs/{id1}/cancel")
print(f"[6] 取消终态 run: {s}（期望 409）")
assert s == 409

# 7. 不可变重试：对完成的 run 建 child（复用成功依赖，从 persist_note 重试）
s, rr = req("POST", f"/api/runs/{id2}/retry", {"from_node": "persist_note", "reuse_successful_dependencies": True})
print(f"[7] retry: {s} {json.dumps(rr, ensure_ascii=False)[:150]}")
assert s == 200 and rr.get("parent_run_id") == id2
child = rr["run_id"]
res = wait_runs([child], timeout=300)
print(f"[8] 重试 run {child}: {res}")
assert res.get(child) == "completed"
_, orig = req("GET", f"/api/runs/{id2}")
_, ch2 = req("GET", f"/api/runs/{child}")
assert ch2["run"].get("parent_run_id") == id2, "parent_run_id 缺失"
# 原运行不可变：输出/节点数不变
_, orig2 = req("GET", f"/api/runs/{id2}")
assert orig2["run"]["output_json"] == orig["run"]["output_json"], "原运行被修改"

print("SMOKE ALL OK")
