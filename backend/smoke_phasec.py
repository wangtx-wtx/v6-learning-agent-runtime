# -*- coding: utf-8 -*-
"""Phase C 真实冒烟：blob 去重引用 / GC / 审计 / 类型矩阵。"""
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

def upload(filename: str, content: bytes, course_id=1):
    boundary = "----smokec"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + content + \
           f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"course_id\"\r\n\r\n{course_id}\r\n--{boundary}--\r\n".encode()
    r = urllib.request.Request(BASE + "/api/materials/upload", data=body, method="POST",
                               headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))

def wait_material(mid, states=("ready", "failed", "needs_ocr", "transcribing"), timeout=30):
    for _ in range(timeout):
        _, m = req("GET", f"/api/materials/{mid}")
        m2 = m.get("material", m)
        st = m2.get("parser_status") or m2.get("status")
        if st in states:
            return m2
        time.sleep(1)
    return m2

import time as _t
CONTENT = ("波粒二象性：光既表现出波动性，又表现出粒子性，光电效应证明了光的粒子性。" * 30
           + f"||UNIQUE-{_t.time()}||").encode("utf-8")

# 1. 首次上传 → 新 blob
up1 = upload("blob_test_v1.txt", CONTENT)
m1 = wait_material(up1["id"])
print(f"[1] 首次上传: id={up1['id']} deduped={up1.get('deduped', False)} status={m1['parser_status']}")
assert not up1.get("deduped")
assert m1["parser_status"] == "ready", f"文本应正常解析: {m1}"

# 2. 同内容再次上传（不同文件名/归属）→ 新材料引用同 blob，旧材料不变
up2 = upload("blob_test_v2.txt", CONTENT)
assert up2.get("deduped") is True, f"去重未生效: {up2}"
m2 = wait_material(up2["id"])
print(f"[2] 去重上传: id={up2['id']} deduped=True status={m2['parser_status']}")

sys.path.insert(0, ".")
from app.database import fetch_one, reset_connections
b = fetch_one("SELECT fb.ref_count, fb.gc_state FROM file_blobs fb WHERE fb.sha256 IN "
              "(SELECT sha256 FROM materials WHERE id IN (?,?))", (up1["id"], up2["id"]))
print(f"[3] blob ref_count={b['ref_count']} gc_state={b['gc_state']}")
assert b["ref_count"] == 2 and b["gc_state"] == "live"
m1row = fetch_one("SELECT name, course_id FROM materials WHERE id=?", (up1["id"],))
assert m1row["name"] == "blob_test_v1.txt", "旧材料被改动！"

# 3. 删除材料2 → blob ref=1，物理文件仍在
s, d = req("DELETE", f"/api/materials/{up2['id']}")
print(f"[4] 删除材料2: {s} {json.dumps(d, ensure_ascii=False)[:130]}")
assert s == 200
b = fetch_one("SELECT ref_count, gc_state FROM file_blobs WHERE sha256 IN "
              "(SELECT sha256 FROM materials WHERE id=?)", (up1["id"],))
assert b["ref_count"] == 1 and b["gc_state"] == "live", f"共享 blob 不应被 GC: {b}"
blob_path = fetch_one("SELECT storage_path FROM file_blobs WHERE sha256 IN "
                      "(SELECT sha256 FROM materials WHERE id=?)", (up1["id"],))["storage_path"]
import os
assert os.path.exists(blob_path), "共享 blob 物理文件被误删！"

# 4. 删除材料1 → blob 归零 → GC 物理删除
s, d = req("DELETE", f"/api/materials/{up1['id']}")
print(f"[5] 删除材料1: {s} {json.dumps(d, ensure_ascii=False)[:150]}")
assert s == 200 and d.get("gc", {}).get("result") == "deleted", d
assert not os.path.exists(blob_path), "GC 未删除物理文件"
reset_connections()

# 4b. GC 后重传同内容 → 死 blob 复活，材料可正常解析（文件必须真实存在）
up5 = upload("blob_test_revive.txt", CONTENT)
m5 = wait_material(up5["id"])
print(f"[5b] 复活重传: id={up5['id']} status={m5['parser_status']}")
assert m5["parser_status"] == "ready", f"复活 blob 解析失败: {m5}"
sys.path.insert(0, ".")
from app.database import fetch_one as _fq, reset_connections as _rc
m5row = _fq("SELECT file_path, blob_id FROM materials WHERE id=?", (up5["id"],))
assert os.path.exists(m5row["file_path"]), "复活后物理文件缺失"
b5 = _fq("SELECT gc_state, ref_count FROM file_blobs WHERE id=?", (m5row["blob_id"],))
assert b5["gc_state"] == "live" and b5["ref_count"] == 1, f"blob 状态异常: {dict(b5)}"
_rc()
req("DELETE", f"/api/materials/{up5['id']}")

# 5. 存储审计 + 手动 GC
s, audit = req("GET", "/api/admin/storage/audit")
print(f"[6] 审计: {s} blobs={audit['blobs']} materials={audit['materials']}")
assert s == 200 and "anomalies" in audit
s, gc = req("POST", "/api/admin/storage/gc")
print(f"[7] 手动GC: {s} {gc}")
assert s == 200

# 6. 类型矩阵：png → needs_ocr（无占位 chunk），mp3 → transcribing
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 512
up3 = upload("fake_img.png", PNG)
m3 = wait_material(up3["id"])
print(f"[8] PNG 上传: id={up3['id']} parser_status={m3['parser_status']}")
assert m3["parser_status"] == "needs_ocr", m3
s, chunks = req("GET", f"/api/materials/{up3['id']}/chunks")
assert isinstance(chunks, list) and len(chunks) == 0, f"图片不得产生占位 chunk: {chunks}"

MP3 = b"ID3" + b"0" * 512
up4 = upload("fake_audio.mp3", MP3)
m4 = wait_material(up4["id"])
print(f"[9] MP3 上传: id={up4['id']} parser_status={m4['parser_status']}")
assert m4["parser_status"] == "transcribing", m4

# 清理冒烟材料
for mid in (up3["id"], up4["id"]):
    req("DELETE", f"/api/materials/{mid}")

print("PHASE C SMOKE ALL OK")
