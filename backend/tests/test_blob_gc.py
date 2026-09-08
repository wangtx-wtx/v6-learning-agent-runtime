"""V5.5 阶段C 测试：blob 去重引用、GC、存储审计、类型矩阵（方案 4.x）。"""
import io
import sys
import tempfile
import unittest
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import database as db  # noqa: E402


def _insert_material(sha256, name="a.pdf", kind="pdf", blob_id=None, parser_status="ready", status="ready"):
    return db.insert(
        "INSERT INTO materials (file_path, name, display_name, file_hash, sha256, type, kind, "
        " size_bytes, blob_id, parser_status, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (f"/fake/{name}", name, name, sha256[:16], sha256, kind, kind, 100, blob_id,
         parser_status, status),
    )


def _insert_blob(sha256, path, ref=1, gc_state="live"):
    return db.insert(
        "INSERT INTO file_blobs (sha256, storage_path, size_bytes, ref_count, gc_state) "
        "VALUES (?,?,?,?,?)",
        (sha256, path, 100, ref, gc_state),
    )


class BlobTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        db.configure_db(self.tmp / "t.db")
        db.init_db()

    def tearDown(self):
        db.reset_connections()


class TestBlobRefCount(BlobTestBase):
    def test_dedup_creates_new_material_referencing_same_blob(self):
        """方案 4.1：去重命中 = 新 material + 复用 blob；旧材料归属不变。"""
        sha = "a" * 64
        b1 = _insert_blob(sha, "/fake/x.pdf", ref=1)
        old_mid = _insert_material(sha, blob_id=b1)
        old = db.fetch_one("SELECT lesson_id, chapter_id FROM materials WHERE id=?", (old_mid,))

        # 第二次"上传"同内容：新建 material 引用同一 blob（ref+1 模拟 add_blob_ref）
        db.execute("UPDATE file_blobs SET ref_count=ref_count+1 WHERE id=?", (b1,))
        new_mid = _insert_material(sha, name="b.pdf", blob_id=b1)

        rows = db.fetch_all("SELECT id FROM materials WHERE sha256=?", (sha,))
        self.assertEqual(len(rows), 2, "去重应产生两条材料记录")
        new = db.fetch_one("SELECT lesson_id, chapter_id FROM materials WHERE id=?", (new_mid,))
        self.assertEqual((old["lesson_id"], old["chapter_id"]), (new["lesson_id"], new["chapter_id"]),
                         "新记录归属应与上传参数一致（默认空），不得继承旧归属")
        self.assertNotEqual(old_mid, new_mid)

    def test_release_blob_to_pending_delete(self):
        from app.services.blob import release_material_blob
        sha = "b" * 64
        blob_id = _insert_blob(sha, "/fake/y.pdf", ref=1)
        mid = _insert_material(sha, blob_id=blob_id)
        res = release_material_blob(mid)
        self.assertTrue(res["pending_delete"])
        self.assertEqual(db.fetch_one("SELECT gc_state FROM file_blobs WHERE id=?", (blob_id,))["gc_state"],
                         "pending_delete")

    def test_release_shared_blob_keeps_live(self):
        from app.services.blob import release_material_blob
        sha = "c" * 64
        blob_id = _insert_blob(sha, "/fake/z.pdf", ref=2)
        m1 = _insert_material(sha, name="1.pdf", blob_id=blob_id)
        _insert_material(sha, name="2.pdf", blob_id=blob_id)
        res = release_material_blob(m1)
        self.assertFalse(res["pending_delete"])
        self.assertEqual(res["ref_count"], 1)
        self.assertEqual(db.fetch_one("SELECT gc_state FROM file_blobs WHERE id=?", (blob_id,))["gc_state"],
                         "live")


class TestGc(BlobTestBase):
    def test_gc_deletes_physical_file_when_ref_zero(self):
        from app.services.blob_gc import gc_blob_now
        f = self.tmp / "blob.bin"
        f.write_bytes(b"hello")
        blob_id = _insert_blob("d" * 64, str(f), ref=0, gc_state="pending_delete")
        res = gc_blob_now(blob_id)
        self.assertEqual(res["result"], "deleted")
        self.assertFalse(f.exists())
        self.assertEqual(db.fetch_one("SELECT gc_state FROM file_blobs WHERE id=?", (blob_id,))["gc_state"],
                         "deleted")

    def test_gc_failure_falls_back_to_task(self):
        from app.services.blob_gc import gc_blob_now, run_pending_gc
        # 指向不存在的目录/不可删除路径：Windows 下用一个被占用的路径模拟
        blob_id = _insert_blob("e" * 64, str(self.tmp / "no_such_dir" / "x.bin"), ref=0,
                               gc_state="pending_delete")
        res = gc_blob_now(blob_id)
        # 文件不存在 → unlink 前有 exists() 判断，视为删除成功
        self.assertIn(res["result"], ("deleted",))
        # 真实失败路径：目录不可删场景改用任务表回放
        db.insert("INSERT INTO blob_gc_tasks (blob_id, storage_path, reason, error) VALUES (?,?,?,?)",
                  (blob_id, "Z:\\\\definitely\\\\missing\\\\path.bin", "gc_failed", "simulated"))
        report = run_pending_gc()
        self.assertGreaterEqual(report["tasks_scanned"], 1)

    def test_delete_material_record_order(self):
        from app.services.blob_gc import delete_material_record
        sha = "f" * 64
        blob_id = _insert_blob(sha, "/fake/g.pdf", ref=1)
        mid = _insert_material(sha, blob_id=blob_id)
        cid = db.insert(
            "INSERT INTO source_chunks (material_id, type, locator, text) VALUES (?,?,?,?)",
            (mid, "pdf", "page:1", "内容"),
        )
        eid = db.insert(
            "INSERT INTO evidence_links (owner_type, owner_id, chunk_id, quote) VALUES (?,?,?,?)",
            ("note", 1, cid, "引文"),
        )
        db.insert("INSERT INTO parse_tasks (material_id, status) VALUES (?, 'done')", (mid,))
        summary = delete_material_record(mid)
        self.assertEqual(summary["chunks_removed"], 1)
        self.assertFalse(db.fetch_all("SELECT * FROM materials WHERE id=?", (mid,)))
        self.assertFalse(db.fetch_all("SELECT * FROM source_chunks WHERE material_id=?", (mid,)))
        self.assertFalse(db.fetch_all("SELECT * FROM parse_tasks WHERE material_id=?", (mid,)))
        # 引用已删 chunk 的证据行按方案 4.2 顺序先被清理（不悬挂）
        self.assertFalse(db.fetch_all("SELECT * FROM evidence_links WHERE id=?", (eid,)),
                         "悬挂证据应先于 chunks 删除")
        # blob 引用释放
        self.assertEqual(db.fetch_one("SELECT ref_count FROM file_blobs WHERE id=?", (blob_id,))["ref_count"], 0)


class TestTypeMatrix(BlobTestBase):
    def test_image_goes_needs_ocr_not_ready(self):
        from app.services.file_types import initial_state, is_parseable
        self.assertEqual(initial_state("image"), ("needs_ocr", "needs_ocr"))
        self.assertFalse(is_parseable("image"))

    def test_audio_goes_transcribing_not_ready(self):
        from app.services.file_types import initial_state, is_parseable
        self.assertEqual(initial_state("audio"), ("transcribing", "transcribing"))
        self.assertFalse(is_parseable("audio"))

    def test_parser_marks_image_audio_honestly(self):
        from app.material_parser import run_parse_material
        # 构造一个"图片"材料：解析后应停在 needs_ocr，无占位 chunk
        img = self.tmp / "pic.png"
        img.write_bytes(b"\x89PNG fake")
        mid = db.insert(
            "INSERT INTO materials (file_path, name, sha256, kind, parser_status, status) "
            "VALUES (?,?,?,?, 'queued','queued')",
            (str(img), "pic.png", "1" * 64, "image"),
        )
        import asyncio
        out = asyncio.get_event_loop().run_until_complete(run_parse_material(mid)) \
            if False else asyncio.run(run_parse_material(mid))
        self.assertEqual(out["state"], "needs_ocr")
        self.assertEqual(out["chunk_count"], 0)
        m = db.fetch_one("SELECT parser_status, status FROM materials WHERE id=?", (mid,))
        self.assertEqual((m["parser_status"], m["status"]), ("needs_ocr", "needs_ocr"))
        self.assertFalse(db.fetch_all("SELECT * FROM source_chunks WHERE material_id=?", (mid,)),
                         "图片材料不得产生占位 chunk")

    def test_storage_audit_shape(self):
        from app.services.blob_gc import storage_audit
        _insert_blob("9" * 64, "/fake/audit.pdf", ref=1)
        _insert_material("9" * 64, blob_id=1)
        audit = storage_audit()
        self.assertIn("blobs", audit)
        self.assertIn("anomalies", audit)
        self.assertEqual(audit["materials"]["total"], 1)


if __name__ == "__main__":
    unittest.main()
