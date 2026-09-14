"""V6 Learning Engine Phase 1 — Material Integrity 验收测试。

覆盖任务书 §十二 的 21 项要求，并额外构造**长材料 fixture**（首/中/尾三处埋点）：

* 首/中/尾埋点必须都进入 Source Map；
* 人为漏掉一个 canonical span → silent_dropped > 0 → 门禁必须阻断。

运行：python -X utf8 backend/tools/run_tests_isolated.py
（本文件不得直接连正式库；所有路径派生自 V5_TEST_DATA_ROOT）
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
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

#: 长材料埋点（首/中/尾）——测试断言依赖这三个标记。
HEAD_MARK = "【HEAD-MARK】开篇定义反射定律与法线。"
MID_MARK = "【MID-MARK】中段推导入射角等于反射角。"
TAIL_MARK = "【TAIL-MARK】结尾总结镜面反射与漫反射的适用条件。"


def _force_fake_gateway():
    os.environ["V5_GATEWAY_MODE"] = "fake"
    os.environ.pop("V5_GATEWAY_API_KEY", None)
    os.environ.pop("V5_ALLOW_LIVE_MODEL_TESTS", None)


class V6Base(unittest.TestCase):
    """隔离测试基类：临时数据根 + shadow 模式 + 一套最小课程层级。"""

    MODE = "shadow"

    def setUp(self):
        _force_fake_gateway()
        self._prev_mode = os.environ.get("V6_LEARNING_ENGINE")
        os.environ["V6_LEARNING_ENGINE"] = self.MODE

        from app import gateway as gw_mod
        gw_mod.reset_engine_for_test()

        # TestClient 的 host 非 127.0.0.1；若 .env 配了 V5_MOBILE_TOKEN，
        # 业务 API 会 403。测试期间统一关闭（各既有测试同样处理）。
        from app import config as app_config
        self._saved_mobile_token = app_config.MOBILE_TOKEN
        app_config.MOBILE_TOKEN = ""

        self.tmp = Path(tempfile.mkdtemp(prefix="v6_material_"))
        from app import database as db
        db.configure_db(self.tmp / "v6.db")
        db.init_db()
        self.db = db

        self.course_id = db.insert("INSERT INTO courses (name, code) VALUES ('物理', 'PHY101')")
        self.chapter_id = db.insert(
            "INSERT INTO chapters (course_id, chapter_no, title) VALUES (?, 1, '第一章')",
            (self.course_id,),
        )
        self.lesson_id = db.insert(
            "INSERT INTO lessons (chapter_id, course_id, lesson_no, title) "
            "VALUES (?, ?, 'L01', '光的反射')",
            (self.chapter_id, self.course_id),
        )
        # 材料文件必须落在配置的 UPLOAD_DIR 内，否则 resolve_material_path 会
        # 判为路径越权（这是既有的 P0 安全边界，测试不得绕过）。
        self._uploads = Path(app_config.UPLOAD_DIR)
        self._uploads.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        from app import database as db
        db.reset_connections()
        from app import gateway as gw_mod
        gw_mod.reset_engine_for_test()
        from app import config as app_config
        app_config.MOBILE_TOKEN = self._saved_mobile_token
        if self._prev_mode is None:
            os.environ.pop("V6_LEARNING_ENGINE", None)
        else:
            os.environ["V6_LEARNING_ENGINE"] = self._prev_mode
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----
    def make_material(self, name: str, text: str, *, kind: str = "text",
                      course_id=None, chapter_id=None, lesson_id=None,
                      parser_status: str = "ready", write_file: bool = True) -> int:
        """建一条材料记录（可选写出真实文件 + source_chunks）。"""
        path = self._uploads / name
        if write_file:
            path.write_text(text, encoding="utf-8")
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return self.db.insert(
            "INSERT INTO materials (course_id, chapter_id, lesson_id, file_path, name, "
            " display_name, kind, type, sha256, parser_status, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (course_id if course_id is not None else self.course_id,
             chapter_id if chapter_id is not None else self.chapter_id,
             lesson_id if lesson_id is not None else self.lesson_id,
             str(path) if write_file else None, name, name, kind, kind, sha, parser_status,
             parser_status),
        )

    def add_chunk(self, material_id: int, locator: str, text: str,
                  chunk_type: str = "text") -> int:
        return self.db.insert(
            "INSERT INTO source_chunks (material_id, lesson_id, chapter_id, course_id, "
            " type, locator, text) VALUES (?,?,?,?,?,?,?)",
            (material_id, self.lesson_id, self.chapter_id, self.course_id,
             chunk_type, locator, text),
        )

    def make_run(self, *, transcript: str = "", material_ids=None,
                 course_id=None, chapter_id=None, lesson_id=None) -> int:
        cid = course_id if course_id is not None else self.course_id
        chid = chapter_id if chapter_id is not None else self.chapter_id
        lid = lesson_id if lesson_id is not None else self.lesson_id
        payload = {"material_ids": material_ids or [], "transcript": transcript}
        return self.db.insert(
            "INSERT INTO workflow_runs (workflow, mode, course_id, chapter_id, lesson_id, "
            " status, input_json) VALUES ('lesson','attend',?,?,?,'running',?)",
            (cid, chid, lid, json.dumps(payload, ensure_ascii=False)),
        )

    def seed_chunks(self, material_id: int, segments) -> None:
        for i, seg in enumerate(segments, 1):
            self.add_chunk(material_id, f"seg:{i}", seg)

    def freeze(self, run_id, **kw):
        from app.learning_engine.domain import freeze_material_domain
        return freeze_material_domain(run_id=run_id, course_id=self.course_id,
                                      chapter_id=self.chapter_id,
                                      lesson_id=self.lesson_id, **kw)

    def build_map(self, domain_id, **kw):
        from app.learning_engine.domain import build_source_map
        return build_source_map(domain_id, **kw)

    def plan_and_ledger(self, domain_id, run_id):
        from app.learning_engine.coverage import build_coverage_plan, mark_map_ledger
        plan = build_coverage_plan(domain_id)
        mark_map_ledger(domain_id, run_id)
        return plan

    def audit(self, domain_id):
        from app.learning_engine.coverage import record_coverage_audit
        return record_coverage_audit(domain_id)

    def spans(self, domain_id):
        return self.db.fetch_all(
            "SELECT * FROM source_spans WHERE domain_id=? ORDER BY ordinal, id", (domain_id,))

    def span_by_text(self, domain_id, needle: str):
        for row in self.spans(domain_id):
            if needle in (row["text"] or ""):
                return row
        return None


class TestLongMaterialFixture(V6Base):
    """长材料 fixture：首/中/尾三处埋点必须全部进入 Source Map。"""

    def _long_material_case(self):
        filler_a = "填充内容甲。" * 400
        filler_b = "填充内容乙。" * 400
        long_text = "\n\n".join([HEAD_MARK, filler_a, MID_MARK, filler_b, TAIL_MARK])
        mid = self.make_material("lecture.txt", long_text)
        self.seed_chunks(mid, [HEAD_MARK, filler_a, MID_MARK, filler_b, TAIL_MARK])
        run_id = self.make_run(transcript="[00:00:00] 开场\n[01:30:00] 结束",
                               material_ids=[mid])
        domain = self.freeze(run_id, material_ids=[mid],
                             transcript="[00:00:00] 开场\n[01:30:00] 结束")
        self.build_map(domain["id"])
        return mid, domain, long_text

    def test_head_mid_tail_all_present_in_source_map(self):
        _mid, domain, long_text = self._long_material_case()
        self.assertGreater(len(long_text), 3000, "fixture 必须是长材料，不能只测短文本 happy path")
        for mark in (HEAD_MARK, MID_MARK, TAIL_MARK):
            span = self.span_by_text(domain["id"], mark)
            self.assertIsNotNone(span, f"埋点未进入 Source Map: {mark}")
            self.assertEqual(span["span_state"], "included")

    def test_long_material_accounting_complete(self):
        """正常路径：全部 canonical span 有账目 → silent_dropped=0。"""
        _mid, domain, _text = self._long_material_case()
        run_id = domain["run_id"]
        plan = self.plan_and_ledger(domain["id"], run_id)
        self.assertEqual(plan["unassigned_count"], 0)
        report = self.audit(domain["id"])
        self.assertEqual(report.silent_dropped, 0)
        self.assertEqual(report.domain_accounting_rate, 1.0)

    def test_retry_writes_coverage_report_to_current_run(self):
        """复用父任务 Material Domain 时，报告必须归属当前重试任务。"""
        _mid, domain, _text = self._long_material_case()
        self.plan_and_ledger(domain["id"], domain["run_id"])
        retry_run_id = self.make_run()
        from app.learning_engine.coverage import record_coverage_audit_for_run
        report = record_coverage_audit_for_run(domain["id"], retry_run_id)
        self.assertEqual(report.run_id, retry_run_id)
        self.assertIsNotNone(self.db.fetch_one(
            "SELECT id FROM coverage_reports WHERE run_id=?", (retry_run_id,)))
        self.assertIsNone(self.db.fetch_one(
            "SELECT id FROM coverage_reports WHERE run_id=?", (domain["run_id"],)))

    def test_deliberately_dropped_span_blocks_gate(self):
        """人为漏掉一个 canonical span → silent_dropped>0 → 门禁必须阻断。

        这是本阶段最重要的证伪测试：先证明「正常建图 + 记账」下 silent_dropped=0，
        再注入**记账遗漏**（真实 silent drop），确认审计能检出并阻断。
        """
        filler_a = "填充内容甲。" * 400
        filler_b = "填充内容乙。" * 400
        parts = [HEAD_MARK, filler_a, MID_MARK, filler_b, TAIL_MARK]
        mid = self.make_material("lecture2.txt", "\n\n".join(parts))
        self.seed_chunks(mid, parts)
        run_id = self.make_run(material_ids=[mid])
        domain = self.freeze(run_id, material_ids=[mid])
        self.build_map(domain["id"])
        from app.learning_engine.coverage import mark_map_ledger

        # 1) 正常路径：全部 span 有账目 → 无 silent drop
        mark_map_ledger(domain["id"], run_id)
        clean = self.audit(domain["id"])
        self.assertEqual(clean.silent_dropped, 0)

        # 2) 注入遗漏：故意不给 TAIL 埋点所在 span 记账
        tail_span = [r for r in self.spans(domain["id"]) if TAIL_MARK in (r["text"] or "")]
        self.assertEqual(len(tail_span), 1, "fixture 必须包含唯一的 TAIL 埋点 span")
        target = tail_span[0]
        self.assertEqual(target["span_state"], "included")
        counts = mark_map_ledger(domain["id"], run_id, omit_span_ids={int(target["id"])})
        self.assertGreater(counts.get("omitted", 0), 0)
        # 清掉该 span 的所有既有账目，模拟"完全没有处理记录"
        self.db.execute("DELETE FROM coverage_ledger WHERE source_span_id=?",
                        (int(target["id"]),))

        report = self.audit(domain["id"])
        self.assertEqual(report.silent_dropped, 1, "被漏记的 canonical span 必须被检出")
        self.assertNotEqual(report.gate.value, "passed", "silent drop 存在时门禁不得通过")
        self.assertIn("silent_dropped", report.degradation_reason or "")
        # 运行不得显示 completed：worker 必须拿到降级原因
        from app.workers.workflow_worker import _coverage_degradation_reason
        self.assertIsNotNone(_coverage_degradation_reason(run_id))

    def test_unassigned_span_is_explicitly_recorded_not_silent(self):
        """未建图 span 走显式记账（excluded_with_reason），因此不算 silent drop。"""
        filler_a = "填充内容甲。" * 400
        filler_b = "填充内容乙。" * 400
        parts = [HEAD_MARK, filler_a, MID_MARK, filler_b, TAIL_MARK]
        mid = self.make_material("lecture3.txt", "\n\n".join(parts))
        self.seed_chunks(mid, parts)
        run_id = self.make_run(material_ids=[mid])
        domain = self.freeze(run_id, material_ids=[mid])
        self.build_map(domain["id"], covered_span_ids={1, 2, 3, 4})
        self.plan_and_ledger(domain["id"], run_id)
        excluded = [r for r in self.spans(domain["id"])
                    if r["span_state"] == "excluded_with_reason"]
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]["reason_code"], "excluded_unassigned")
        report = self.audit(domain["id"])
        self.assertEqual(report.silent_dropped, 0, "显式记账的排除不构成 silent drop")
        self.assertEqual(report.canonical_non_noise_spans, 4)
        metrics = self.db.fetch_one(
            "SELECT metrics_json FROM coverage_reports WHERE run_id=?", (run_id,))
        stored = json.loads(metrics["metrics_json"])
        self.assertEqual(stored["excluded_spans"], 1)

    def test_timeline_range_and_ppt_pages_reported(self):
        """转写时间轴与 PPT 页覆盖必须给出可核对的口径。"""
        talk = self.make_material("talk.txt", "带时间轴的课堂转写", kind="transcript")
        for loc, txt in [
            ("00:00:00-00:05:00", "开场：" + HEAD_MARK),
            ("00:05:00-00:20:00", "中段：" + MID_MARK),
            ("00:20:00-00:30:00", "结尾：" + TAIL_MARK),
        ]:
            self.add_chunk(talk, loc, txt, chunk_type="transcript")
        slides = self.make_material("slides.txt", HEAD_MARK, kind="ppt")
        self.add_chunk(slides, "slide:1", HEAD_MARK, chunk_type="ppt")
        self.add_chunk(slides, "slide:2", MID_MARK, chunk_type="ppt")
        run_id = self.make_run(material_ids=[talk, slides])
        domain = self.freeze(run_id, material_ids=[talk, slides])
        self.build_map(domain["id"])
        from app.learning_engine.coverage import compute_coverage
        metrics = compute_coverage(domain["id"])
        rows = self.db.fetch_all(
            "SELECT source_id, start_ms, end_ms, span_state FROM source_spans "
            "WHERE domain_id=? AND source_kind='transcript' ORDER BY ordinal",
            (domain["id"],))
        self.assertEqual(len(rows), 3, "三条转写 span 都应建立")
        self.assertTrue(all(r["source_id"].startswith("T") for r in rows),
                        f"transcript Source ID 必须以 T 开头: {[r['source_id'] for r in rows]}")
        self.assertTrue(all(r["span_state"] == "included" for r in rows))
        self.assertEqual(rows[0]["start_ms"], 0)
        self.assertEqual(rows[0]["end_ms"], 5 * 60 * 1000)
        self.assertEqual(metrics["timeline_span_total"], 3)
        self.assertEqual(metrics["timeline_start_ms"], 0)
        self.assertEqual(metrics["timeline_end_ms"], 30 * 60 * 1000)
        ppt = self.db.fetch_all(
            "SELECT source_id FROM source_spans WHERE domain_id=? AND source_kind='ppt'"
            " ORDER BY ordinal", (domain["id"],))
        self.assertEqual([r["source_id"] for r in ppt], ["P0001.01", "P0002.01"])
        self.assertEqual(metrics["ppt_pages_total"], 2)


class TestDomainFreeze(V6Base):
    """1 / 2 / 3：冻结稳定性、域内材料唯一、跨课程材料拒绝。"""

    def test_domain_frozen_and_stable_across_reentry(self):
        mat = self.make_material("a.txt", HEAD_MARK)
        self.seed_chunks(mat, [HEAD_MARK])
        run_id = self.make_run(material_ids=[mat])
        first = self.freeze(run_id, material_ids=[mat])
        self.assertEqual(first["state"], "frozen")
        self.assertIsNotNone(first["frozen_at"])
        second = self.freeze(run_id, material_ids=[mat])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["domain_hash"], second["domain_hash"])
        count = self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM material_domains WHERE run_id=?", (run_id,))
        self.assertEqual(count["n"], 1, "重入不得创建第二个 domain")

    def test_domain_hash_changes_when_member_set_changes(self):
        a = self.make_material("a.txt", HEAD_MARK)
        b = self.make_material("b.txt", MID_MARK)
        run1 = self.make_run(material_ids=[a])
        run2 = self.make_run(material_ids=[a, b])
        d1 = self.freeze(run1, material_ids=[a])
        d2 = self.freeze(run2, material_ids=[a, b])
        self.assertNotEqual(d1["domain_hash"], d2["domain_hash"])

    def test_same_material_cannot_join_domain_twice(self):
        mat = self.make_material("a.txt", HEAD_MARK)
        run_id = self.make_run(material_ids=[mat, mat])
        domain = self.freeze(run_id, material_ids=[mat, mat])
        ids = [i["material_id"] for i in domain["items"] if i["material_id"] is not None]
        self.assertEqual(ids.count(mat), 1, "同一材料不得在同一 domain 重复加入")
        # 数据库层同样禁止
        from app import database as db
        with self.assertRaises(Exception):
            db.insert(
                "INSERT INTO material_domain_items (domain_id, material_id, source_kind, ordinal) "
                "VALUES (?,?, 'text', 99)", (domain["id"], mat))

    def test_foreign_course_material_rejected(self):
        from app.learning_engine.domain import DomainScopeMismatchError
        other_course = self.db.insert("INSERT INTO courses (name) VALUES ('化学')")
        other_chapter = self.db.insert(
            "INSERT INTO chapters (course_id, chapter_no, title) VALUES (?, 1, '化学章')",
            (other_course,))
        foreign = self.make_material(
            "chem.txt", TAIL_MARK, course_id=other_course,
            chapter_id=other_chapter, lesson_id=None)
        run_id = self.make_run(material_ids=[foreign])
        with self.assertRaises(DomainScopeMismatchError):
            self.freeze(run_id, material_ids=[foreign])
        # 反向：显式声明 foreign 课程范围时可以纳入
        run2 = self.make_run(material_ids=[foreign], course_id=other_course,
                             chapter_id=other_chapter, lesson_id=None)
        from app.learning_engine.domain import freeze_material_domain
        dom = freeze_material_domain(run_id=run2, course_id=other_course,
                                     chapter_id=other_chapter, material_ids=[foreign])
        self.assertEqual(len(dom["items"]), 1)

    def test_frozen_domain_member_removal_refused(self):
        from app.learning_engine.domain import FrozenDomainMutationError, remove_domain_item
        mat = self.make_material("a.txt", HEAD_MARK)
        run_id = self.make_run(material_ids=[mat])
        domain = self.freeze(run_id, material_ids=[mat])
        item_id = domain["items"][0]["id"]
        with self.assertRaises(FrozenDomainMutationError):
            remove_domain_item(item_id)
        still_there = self.db.fetch_one(
            "SELECT id FROM material_domain_items WHERE id=?", (item_id,))
        self.assertIsNotNone(still_there, "冻结后成员不得被移除")

    def test_transcript_only_creates_traceable_item(self):
        transcript = "[00:00:00] 开场\n[00:10:00] 结束"
        run_id = self.make_run(transcript=transcript)
        domain = self.freeze(run_id, transcript=transcript)
        self.assertEqual(len(domain["items"]), 1)
        item = domain["items"][0]
        self.assertIsNone(item["material_id"])
        self.assertEqual(item["source_kind"], "transcript")
        self.assertEqual(item["state"], "included")
        self.assertGreater(item["raw_chars"], 0)


class TestDeduplication(V6Base):
    """4 / 5：重复文件只算一个 unique item；重复 span 指向 canonical。"""

    def test_duplicate_file_counts_once_but_both_accounted(self):
        content = HEAD_MARK + "\n\n" + ("重复正文。" * 200) + "\n\n" + TAIL_MARK
        a = self.make_material("lecture.txt", content)
        b = self.make_material("lecture_copy.txt", content)  # 同内容、不同文件名
        run_id = self.make_run(material_ids=[a, b])
        domain = self.freeze(run_id, material_ids=[a, b])
        states = {i["material_id"]: i for i in domain["items"] if i["material_id"]}
        self.assertEqual(states[a]["state"], "included")
        self.assertEqual(states[b]["state"], "duplicate")
        self.assertEqual(states[b]["duplicate_of_item_id"], states[a]["id"])
        self.assertEqual(states[b]["reason_code"], "duplicate_content_hash")
        # 两个引用都保留账目
        self.assertEqual(len(domain["items"]), 2)
        # unique 计数
        unique = self.db.fetch_all(
            "SELECT COUNT(*) AS n FROM material_domain_items WHERE domain_id=? AND state<>'duplicate'",
            (domain["id"],))
        self.assertEqual(unique[0]["n"], 1)

    def test_duplicate_span_points_to_canonical(self):
        mat = self.make_material("a.txt", HEAD_MARK)
        body = "同一段被解析两次。" * 60
        self.seed_chunks(mat, [body, body])  # 两个完全相同 chunk
        run_id = self.make_run(material_ids=[mat])
        domain = self.freeze(run_id, material_ids=[mat])
        self.build_map(domain["id"])
        rows = self.spans(domain["id"])
        self.assertEqual(len(rows), 2)
        canonical = [r for r in rows if r["span_state"] == "included"]
        dup = [r for r in rows if r["span_state"] == "duplicate"]
        self.assertEqual(len(canonical), 1)
        self.assertEqual(len(dup), 1)
        self.assertEqual(dup[0]["canonical_span_id"], canonical[0]["id"])
        self.assertIsNotNone(dup[0]["reason_code"])
        self.assertNotEqual(dup[0]["id"], canonical[0]["id"])

    def test_noise_material_recorded_with_reason(self):
        empty = self.make_material("empty.txt", "   \n  \t ")
        self.add_chunk(empty, "seg:1", "   ")
        run_id = self.make_run(material_ids=[empty])
        domain = self.freeze(run_id, material_ids=[empty])
        self.build_map(domain["id"])
        rows = self.spans(domain["id"])
        noise = [r for r in rows if r["span_state"] == "noise"]
        self.assertEqual(len(noise), 1)
        self.assertEqual(noise[0]["reason_code"], "noise_empty_text")
        self.assertEqual(noise[0]["text"], "")

    def test_physical_deletion_never_happens(self):
        content = HEAD_MARK + "重复" * 300
        a = self.make_material("x.txt", content)
        b = self.make_material("y.txt", content)
        keep = self.make_material("keep.txt", MID_MARK)
        self.add_chunk(a, "seg:1", HEAD_MARK)
        self.add_chunk(b, "seg:1", HEAD_MARK)
        self.add_chunk(keep, "seg:1", MID_MARK)
        run_id = self.make_run(material_ids=[a, b, keep])
        self.freeze(run_id, material_ids=[a, b, keep])
        rows = self.db.fetch_all("SELECT id, file_path FROM materials")
        self.assertEqual(len(rows), 3, "去重不得物理删除材料记录")
        for r in rows:
            self.assertTrue(Path(r["file_path"]).exists(), "材料文件不得被删除")
        chunks = self.db.fetch_all("SELECT id FROM source_chunks")
        self.assertEqual(len(chunks), 3, "source_chunks 不得被去重删除")


class TestSourceIds(V6Base):
    """6 / 7：Transcript 与 PPT/PDF 页码 Source ID 稳定。"""

    def test_transcript_source_ids_stable(self):
        talk = self.make_material("talk.txt", HEAD_MARK)
        self.seed_chunks(talk, [MID_MARK, TAIL_MARK, HEAD_MARK])
        run_id = self.make_run(material_ids=[talk])
        domain = self.freeze(run_id, material_ids=[talk])
        self.build_map(domain["id"])
        first = [r["source_id"] for r in self.spans(domain["id"])]
        self.assertEqual(first, ["T000001", "T000002", "T000003"])
        # 重跑同一 domain version：结果必须稳定（幂等，不重排）
        self.build_map(domain["id"])
        again = [r["source_id"] for r in self.spans(domain["id"])]
        self.assertEqual(first, again)
        # 独立复算（新 run/新 domain，同一输入顺序）→ 相同 ID 序列
        run2 = self.make_run(material_ids=[talk])
        dom2 = self.freeze(run2, material_ids=[talk])
        self.build_map(dom2["id"])
        second = [r["source_id"] for r in self.spans(dom2["id"])]
        self.assertEqual(first, second)

    def test_ppt_and_pdf_page_source_ids_stable(self):
        ppt = self.make_material("s.pptx", "slide text", kind="ppt")
        pdf = self.make_material("h.pdf", "handout text", kind="pdf")
        self.add_chunk(ppt, "slide:17", "第十七张幻灯片内容")
        self.add_chunk(ppt, "slide:17", "第十七张第二段")
        self.add_chunk(ppt, "slide:3", "第三张内容")
        self.add_chunk(pdf, "page:2", "第二页讲义内容")
        run_id = self.make_run(material_ids=[ppt, pdf])
        domain = self.freeze(run_id, material_ids=[ppt, pdf])
        self.build_map(domain["id"])
        ids = [r["source_id"] for r in self.spans(domain["id"])
               if r["source_kind"] in ("ppt", "pdf")]
        self.assertEqual(ids, ["P0017.01", "P0017.02", "P0003.01", "D0002.01"])

    def test_source_id_collision_fails_loudly(self):
        from app.learning_engine.source_map import (
            SourceIdCollisionError,
            SourceIdRequest,
            assign_source_ids,
        )
        from app.learning_engine.contracts import SourceKind
        with self.assertRaises(SourceIdCollisionError):
            assign_source_ids([
                SourceIdRequest(ordinal=1, source_kind=SourceKind.TRANSCRIPT),
                SourceIdRequest(ordinal=1, source_kind=SourceKind.TRANSCRIPT),
            ])

    def test_invalid_source_id_format_rejected(self):
        from app.learning_engine.source_map import SourceIdFormatError, parse_source_id
        for bad in ("", "X000001", "T1", "T000001.999", "17.01"):
            with self.assertRaises(SourceIdFormatError):
                parse_source_id(bad)
        self.assertEqual(parse_source_id("T000128").prefix, "T")
        self.assertEqual(parse_source_id("P0017.02").segment, 2)


class TestContracts(V6Base):
    """9 / 13：reason_code 强制；覆盖率不可伪造。"""

    def test_reason_code_required_states(self):
        from app.learning_engine.contracts import (
            DomainItem,
            SourceSpan,
            SpanState,
            DomainItemState,
        )
        for state in (DomainItemState.UNSUPPORTED, DomainItemState.FAILED,
                      DomainItemState.EXCLUDED_WITH_REASON):
            with self.assertRaises(Exception):
                DomainItem(material_id=1, source_kind="pdf", ordinal=1, state=state)
            ok = DomainItem(material_id=1, source_kind="pdf", ordinal=1, state=state,
                            reason_code="parse_failed")
            self.assertEqual(ok.state, state)
        with self.assertRaises(Exception):
            SourceSpan(source_id="T000001", domain_id=1, source_kind="transcript",
                       ordinal=1, normalized_text_hash="h", span_state=SpanState.NOISE)
        with self.assertRaises(Exception):
            SourceSpan(source_id="T000002", domain_id=1, source_kind="transcript",
                       ordinal=2, normalized_text_hash="h", span_state=SpanState.DUPLICATE)

    def test_extra_fields_forbidden(self):
        from app.learning_engine.contracts import DomainItem
        with self.assertRaises(Exception):
            DomainItem(material_id=1, source_kind="pdf", ordinal=1, not_a_field=1)

    def test_coverage_rate_cannot_be_forged_by_caller(self):
        """覆盖率只能由程序计算；伪造 passed 报告必须被拒绝。"""
        from app.learning_engine.contracts import Gate, MaterialCoverageReport
        with self.assertRaises(Exception):
            MaterialCoverageReport(run_id=1, domain_id=1, gate=Gate.PASSED, silent_dropped=5)
        with self.assertRaises(Exception):
            MaterialCoverageReport(run_id=1, domain_id=1, gate=Gate.PASSED,
                                   domain_accounting_rate=0.5)
        with self.assertRaises(Exception):
            MaterialCoverageReport(run_id=1, domain_id=1, gate=Gate.PASSED, unassigned_count=2)
        # record_coverage_audit 不接受任何指标入参
        import inspect
        from app.learning_engine.coverage import record_coverage_audit
        params = list(inspect.signature(record_coverage_audit).parameters)
        self.assertEqual(params, ["domain_id"],
                         "审计函数不得暴露指标入参（否则调用方可伪造覆盖率）")

    def test_unassigned_count_blocks_gate(self):
        mat = self.make_material("a.txt", HEAD_MARK)
        self.seed_chunks(mat, [HEAD_MARK])
        run_id = self.make_run(material_ids=[mat])
        domain = self.freeze(run_id, material_ids=[mat])
        self.build_map(domain["id"])
        from app.learning_engine.coverage import build_coverage_plan, evaluate_gate
        # 把输入预算压到极小（context_window=1）模拟「放不下」
        build_coverage_plan(domain["id"], context_window=1)
        plan = self.db.fetch_one(
            "SELECT unassigned_count FROM coverage_plans WHERE domain_id=?", (domain["id"],))
        self.assertGreater(plan["unassigned_count"], 0, "预算不足时必须记为未分配")
        gate, reason, issues = evaluate_gate({
            "silent_dropped": 0, "domain_accounting_rate": 1.0,
            "unassigned_count": plan["unassigned_count"],
            "semantic_processing_rate": 1.0,
            "canonical_non_noise_spans": 1,
        })
        # Phase 2：未分配 span 属于硬门禁 → failed（早期为 degraded，会放过 completed）
        self.assertEqual(gate, "failed")
        self.assertIn("unassigned_count", reason)

    def test_silent_drop_blocks_gate(self):
        from app.learning_engine.coverage import evaluate_gate
        gate, reason, issues = evaluate_gate({
            "silent_dropped": 1, "domain_accounting_rate": 1.0,
            "unassigned_count": 0, "semantic_processing_rate": 1.0,
            "canonical_non_noise_spans": 5,
        })
        self.assertEqual(gate, "failed")
        self.assertIn("silent_dropped", reason)

    def test_low_semantic_rate_is_degraded_not_completed(self):
        """Phase 2：语义处理率阈值固定 1.0，低于它一律 degraded（不可配置放宽）。"""
        from app.learning_engine.coverage import evaluate_gate, semantic_rate_floor
        self.assertEqual(semantic_rate_floor(), 1.0)
        gate, reason, _issues = evaluate_gate({
            "silent_dropped": 0, "domain_accounting_rate": 1.0,
            "unassigned_count": 0, "semantic_processing_rate": 0.05,
            "canonical_non_noise_spans": 20,
        })
        self.assertEqual(gate, "degraded")
        # 环境变量不得放宽阈值
        os.environ["V6_SEMANTIC_RATE_FLOOR"] = "0.1"
        try:
            self.assertEqual(semantic_rate_floor(), 1.0)
            gate2, _, _ = evaluate_gate({
                "silent_dropped": 0, "domain_accounting_rate": 1.0,
                "unassigned_count": 0, "semantic_processing_rate": 0.5,
                "canonical_non_noise_spans": 20,
            })
            self.assertEqual(gate2, "degraded", "环境变量不得把阈值降到 0.6/0.1")
        finally:
            os.environ.pop("V6_SEMANTIC_RATE_FLOOR", None)


class TestCoverageLedger(V6Base):
    """Coverage Ledger 可查询；未使用必须有原因。"""

    def _case(self):
        content = HEAD_MARK + "\n\n" + ("正文。" * 300) + "\n\n" + TAIL_MARK
        mat = self.make_material("a.txt", content)
        self.seed_chunks(mat, [HEAD_MARK, "正文。" * 300, TAIL_MARK])
        run_id = self.make_run(material_ids=[mat])
        domain = self.freeze(run_id, material_ids=[mat])
        self.build_map(domain["id"])
        self.plan_and_ledger(domain["id"], run_id)
        return mat, domain, run_id

    def test_ledger_queryable_and_complete(self):
        _mat, domain, _run = self._case()
        rows = self.db.fetch_all(
            "SELECT * FROM coverage_ledger WHERE domain_id=? AND stage='source_map'",
            (domain["id"],))
        span_total = self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM source_spans WHERE domain_id=?", (domain["id"],))
        self.assertEqual(len(rows), span_total["n"], "每个 span 必须有 source_map 账目")

    def test_legacy_processing_marks_processed_and_not_used(self):
        _mat, domain, run_id = self._case()
        from app.learning_engine.coverage import mark_legacy_processing
        spans = self.spans(domain["id"])
        used = spans[0]["source_id"]
        counts = mark_legacy_processing(domain["id"], run_id, {used})
        self.assertEqual(counts["processed"], 1)
        self.assertEqual(counts["not_used"], len(spans) - 1)
        not_used = self.db.fetch_all(
            "SELECT * FROM coverage_ledger WHERE domain_id=? AND outcome='not_used'",
            (domain["id"],))
        self.assertTrue(not_used)
        for row in not_used:
            self.assertIsNotNone(row["reason_code"], "not_used 必须带 reason_code")

    def test_reported_semantic_rate_is_honest_not_faked(self):
        _mat, domain, run_id = self._case()
        from app.learning_engine.coverage import mark_legacy_processing
        spans = self.spans(domain["id"])
        mark_legacy_processing(domain["id"], run_id, {spans[0]["source_id"]})
        report = self.audit(domain["id"])
        self.assertLess(report.semantic_processing_rate, 1.0,
                        "Phase 1 不得把 semantic_processing_rate 伪造成 100%")
        self.assertAlmostEqual(report.semantic_processing_rate,
                               round(1 / len(spans), 6))
        self.assertEqual(report.gate.value, "degraded")

    def test_not_used_without_reason_rejected_by_db(self):
        _mat, domain, run_id = self._case()
        row = self.spans(domain["id"])[0]
        with self.assertRaises(Exception):
            self.db.insert(
                "INSERT INTO coverage_ledger (run_id, domain_id, source_span_id, source_id, "
                " stage, outcome) VALUES (?,?,?,?, 'self_test', 'not_used')",
                (run_id, domain["id"], row["id"], row["source_id"]))


class TestShadowModeAndV5Compat(V6Base):
    """14 / 15：shadow 不覆盖正式笔记；off 模式兼容原 V5 行为。"""

    def test_shadow_mode_does_not_touch_notes_or_artifacts(self):
        """shadow 下跑完整 lesson DAG：V6 表有数据，notes/artifacts 不被 V6 写入。"""
        from app.workers import build_flow
        from app.dag import DAGContext

        mat = self.make_material("a.txt", HEAD_MARK)
        self.seed_chunks(mat, [HEAD_MARK, TAIL_MARK])
        dag = build_flow("lesson")
        ctx = DAGContext()
        ctx.input = {"course_id": self.course_id, "chapter_id": self.chapter_id,
                     "lesson_id": self.lesson_id,
                     "transcript": "本节讲反射定律。", "material_ids": [mat]}
        outputs = asyncio.run(dag.run(ctx))
        self.assertIn("coverage_audit", outputs)
        self.assertIn("resolve_material_domain", outputs)
        # V6 表已写入
        domain = self.db.fetch_one("SELECT * FROM material_domains WHERE run_id=?", (ctx.run_id,))
        self.assertIsNotNone(domain)
        report = self.db.fetch_one("SELECT * FROM coverage_reports WHERE run_id=?", (ctx.run_id,))
        self.assertIsNotNone(report)
        self.assertIn(report["gate"], ("passed", "degraded", "failed"))
        # 旧链仍然产出正式笔记（shadow 不接管），且 notes 行数与 DAG 行为一致
        note = self.db.fetch_one("SELECT * FROM notes WHERE lesson_id=?", (self.lesson_id,))
        self.assertIsNotNone(note, "shadow 模式下旧链仍应产出笔记（不删除既有行为）")

    def test_off_mode_keeps_v5_dag_shape(self):
        os.environ["V6_LEARNING_ENGINE"] = "off"
        from app.dag_lesson import build_lesson_dag
        nodes = set(build_lesson_dag().nodes)
        self.assertIn("resolve_materials", nodes)
        for v6_node in ("resolve_material_domain", "normalize_materials",
                        "deduplicate_materials", "build_source_map", "plan_coverage",
                        "coverage_audit"):
            self.assertNotIn(v6_node, nodes, f"off 模式不得插入 V6 节点 {v6_node}")

    def test_off_mode_run_stays_completed_without_v6_tables(self):
        """off：既有 V5 行为不变（无 V6 数据、无 degraded 干预）。"""
        from app.workers import build_flow
        from app.dag import DAGContext
        os.environ["V6_LEARNING_ENGINE"] = "off"
        mat = self.make_material("a.txt", HEAD_MARK)
        self.seed_chunks(mat, [HEAD_MARK])
        dag = build_flow("lesson")
        ctx = DAGContext()
        ctx.input = {"course_id": self.course_id, "chapter_id": self.chapter_id,
                     "lesson_id": self.lesson_id, "transcript": "反射定律。",
                     "material_ids": [mat]}
        asyncio.run(dag.run(ctx))
        self.assertIsNone(
            self.db.fetch_one("SELECT id FROM material_domains WHERE run_id=?", (ctx.run_id,)),
            "off 模式不得写 V6 表")

    def test_shadow_mode_flag_reports_shadow(self):
        from app.learning_engine import config as v6cfg
        self.assertEqual(v6cfg.engine_mode(), "shadow")
        self.assertTrue(v6cfg.engine_enabled())
        self.assertTrue(v6cfg.shadow_mode())
        self.assertFalse(v6cfg.real_notes_publish_enabled())
        self.assertEqual(v6cfg.engine_version(), "v6.0.0-shadow")

    def test_invalid_mode_rejected(self):
        from app.learning_engine import config as v6cfg
        os.environ["V6_LEARNING_ENGINE"] = "bogus"
        with self.assertRaises(RuntimeError):
            v6cfg.engine_mode()


class TestDegradedStatus(V6Base):
    """16 / 运行终态：degraded 状态机、API 与 worker 判定。"""

    def test_state_machine_allows_degraded(self):
        from app.dag import ALLOWED_RUN_TRANSITIONS, TERMINAL_RUN_STATUSES, is_terminal_run_status
        self.assertIn("degraded", ALLOWED_RUN_TRANSITIONS["running"])
        self.assertEqual(ALLOWED_RUN_TRANSITIONS["degraded"], set())
        self.assertIn("degraded", TERMINAL_RUN_STATUSES)
        self.assertTrue(is_terminal_run_status("degraded"))
        self.assertFalse(is_terminal_run_status("running"))

    def test_mark_degraded_writes_status_and_reason(self):
        from app.dag import DAGContext
        run_id = self.make_run()
        ctx = DAGContext()
        ctx.run_id = run_id
        ctx.mark_running()
        ctx.mark_degraded({"ok": True}, reason="silent_dropped=1")
        row = self.db.fetch_one("SELECT status, error FROM workflow_runs WHERE id=?", (run_id,))
        self.assertEqual(row["status"], "degraded")
        self.assertIn("silent_dropped", row["error"])
        # degraded 之后不允许再转 completed
        ctx.mark_completed({"ok": True})
        row = self.db.fetch_one("SELECT status FROM workflow_runs WHERE id=?", (run_id,))
        self.assertEqual(row["status"], "degraded", "degraded 是终态，不得被 completed 覆盖")

    def test_worker_degrades_run_when_gate_blocks(self):
        """覆盖门禁未通过 → worker 必须写 degraded 而不是 completed。"""
        from app.workers.workflow_worker import _coverage_degradation_reason
        mat = self.make_material("a.txt", HEAD_MARK)
        self.seed_chunks(mat, [HEAD_MARK, MID_MARK, TAIL_MARK])
        run_id = self.make_run(material_ids=[mat])
        domain = self.freeze(run_id, material_ids=[mat])
        self.build_map(domain["id"])
        self.plan_and_ledger(domain["id"], run_id)
        report = self.audit(domain["id"])
        self.assertEqual(report.gate.value, "degraded")
        reason = _coverage_degradation_reason(run_id)
        self.assertIsNotNone(reason, "门禁 degraded 时 worker 必须拿到降级原因")
        self.assertIn("semantic_processing_rate", reason)

    def test_worker_returns_none_when_gate_passes(self):
        from app.workers.workflow_worker import _coverage_degradation_reason
        # 构造一个所有 canonical span 都被处理的场景 → gate passed
        mat = self.make_material("a.txt", HEAD_MARK)
        self.seed_chunks(mat, [HEAD_MARK, MID_MARK])
        run_id = self.make_run(material_ids=[mat])
        domain = self.freeze(run_id, material_ids=[mat])
        self.build_map(domain["id"])
        self.plan_and_ledger(domain["id"], run_id)
        from app.learning_engine.coverage import mark_legacy_processing
        all_ids = {r["source_id"] for r in self.spans(domain["id"])
                   if r["span_state"] == "included"}
        mark_legacy_processing(domain["id"], run_id, all_ids)
        report = self.audit(domain["id"])
        self.assertEqual(report.gate.value, "passed")
        self.assertIsNone(_coverage_degradation_reason(run_id))

    def test_degraded_visible_via_api(self):
        from fastapi.testclient import TestClient
        run_id = self.make_run()
        self.db.execute(
            "UPDATE workflow_runs SET status='degraded', error='silent_dropped=1' WHERE id=?",
            (run_id,))
        from app.main import app
        with TestClient(app) as client:
            res = client.get(f"/api/runs/{run_id}")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["run"]["status"], "degraded")
            # 取消终态运行必须 409（degraded 也属于终态）
            res = client.post(f"/api/runs/{run_id}/cancel")
            self.assertEqual(res.status_code, 409)


class TestMigrationsAndIntegrity(V6Base):
    """17 / 18 / 19：fresh install、v15→v17 升级、外键检查。"""

    def test_fresh_install_has_v6_tables(self):
        tables = {r["name"] for r in self.db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for expected in ("material_domains", "material_domain_items", "source_spans",
                         "coverage_plans", "coverage_ledger", "coverage_reports",
                         # Phase 2（0018）
                         "lesson_segments", "segment_source_spans",
                         "segment_understandings", "lesson_understandings",
                         "segment_reuse_index"):
            self.assertIn(expected, tables)
        self.assertEqual(self.db.schema_version(), 23)

    def test_foreign_key_check_clean(self):
        mat = self.make_material("a.txt", HEAD_MARK)
        self.seed_chunks(mat, [HEAD_MARK])
        run_id = self.make_run(material_ids=[mat])
        domain = self.freeze(run_id, material_ids=[mat])
        self.build_map(domain["id"])
        self.plan_and_ledger(domain["id"], run_id)
        self.audit(domain["id"])
        self.assertEqual(self.db.fetch_all("PRAGMA foreign_key_check"), [])

    def test_upgrade_from_v15_preserves_data(self):
        """模拟 v15 → v17：只应用 0016/0017，既有业务数据不变。"""
        import sqlite3
        from app.database import SCHEMA_MIGRATIONS_DDL, _apply_migration, _migration_files, sync_user_version
        tmp = self.tmp / "upgrade.db"
        conn = sqlite3.connect(str(tmp))
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(SCHEMA_MIGRATIONS_DDL)
            conn.commit()
            files = _migration_files()
            # 1) 先只应用 <=15（模拟旧版库）
            for mig in files:
                if mig["version"] <= 15:
                    _apply_migration(conn, mig)
            sync_user_version(conn)
            self.assertEqual(
                int(conn.execute("PRAGMA user_version").fetchone()[0]), 15)
            before_tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("material_domains", before_tables)
            # 2) 写入一个业务行，确认升级后仍在
            conn.execute("INSERT INTO courses (name) VALUES ('升级前课程')")
            conn.commit()
            # 3) 前向应用 16/17
            for mig in files:
                if mig["version"] in (16, 17):
                    _apply_migration(conn, mig)
            sync_user_version(conn)
            self.assertEqual(
                int(conn.execute("PRAGMA user_version").fetchone()[0]), 17)
            after_tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("material_domains", after_tables)
            self.assertIn("coverage_reports", after_tables)
            kept = conn.execute("SELECT name FROM courses").fetchone()[0]
            self.assertEqual(kept, "升级前课程")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            conn.close()

    def test_checksum_of_pre_v6_migrations_unchanged(self):
        """已应用迁移的 checksum 必须来自既有文件，未被本阶段改写。"""
        from app.database import _migration_files
        files = {m["version"]: m for m in _migration_files()}
        for version in (1, 5, 10, 15):
            self.assertIn(version, files)
        applied = {r["version"]: r["checksum"] for r in self.db.fetch_all(
            "SELECT version, checksum FROM schema_migrations")}
        for version in (1, 5, 10, 15):
            self.assertEqual(applied[version], files[version]["checksum"],
                             f"迁移 {version} 的 checksum 与文件不一致")


class TestApiEndpoints(V6Base):
    """API：404 / 409 / 422 语义与越权保护。"""

    def _seeded_run(self):
        mat = self.make_material("a.txt", HEAD_MARK)
        self.seed_chunks(mat, [HEAD_MARK, MID_MARK])
        run_id = self.make_run(material_ids=[mat])
        domain = self.freeze(run_id, material_ids=[mat])
        self.build_map(domain["id"])
        self.plan_and_ledger(domain["id"], run_id)
        self.audit(domain["id"])
        return run_id, domain

    def test_material_domain_endpoint(self):
        from fastapi.testclient import TestClient
        run_id, domain = self._seeded_run()
        from app.main import app
        with TestClient(app) as client:
            res = client.get(f"/api/runs/{run_id}/material-domain")
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertEqual(body["domain"]["domain_id"], domain["id"])
            self.assertGreaterEqual(body["counts"]["total_items"], 1)
            self.assertEqual(body["engine"]["mode"], "shadow")
            # 不泄露文件系统路径
            blob = json.dumps(body, ensure_ascii=False)
            self.assertNotIn("uploads", blob)
            self.assertNotIn("file_path", blob)
            # 未知 run
            self.assertEqual(client.get("/api/runs/999999/material-domain").status_code, 404)

    def test_coverage_endpoint(self):
        from fastapi.testclient import TestClient
        run_id, _domain = self._seeded_run()
        from app.main import app
        with TestClient(app) as client:
            res = client.get(f"/api/runs/{run_id}/coverage")
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertIn(body["report"]["gate"], ("passed", "degraded", "failed"))
            self.assertIn("metrics", body["report"])
            self.assertTrue(body["ledger"])
            self.assertLess(body["report"]["metrics"]["semantic_processing_rate"], 1.0)

    def test_source_span_endpoint_scoping(self):
        from fastapi.testclient import TestClient
        run_id, domain = self._seeded_run()
        span = self.spans(domain["id"])[0]
        from app.main import app
        with TestClient(app) as client:
            res = client.get(f"/api/source-spans/{span['source_id']}",
                             params={"run_id": run_id})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["source_id"], span["source_id"])
            # 缺少 run_id → 422（不得退化为全库读取入口）
            self.assertEqual(client.get(f"/api/source-spans/{span['source_id']}").status_code, 422)
            # 跨 run 越权 → 404
            other_run = self.make_run(material_ids=[])
            res = client.get(f"/api/source-spans/{span['source_id']}",
                             params={"run_id": other_run})
            self.assertEqual(res.status_code, 404)
            # 未知 source_id → 404
            res = client.get("/api/source-spans/T999999", params={"run_id": run_id})
            self.assertEqual(res.status_code, 404)

    def test_coverage_404_for_unknown_run(self):
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/runs/999999/coverage").status_code, 404)


class TestNormalizationHelpers(V6Base):
    """规范化与噪声判定的保守性（不把有效内容判成噪声）。"""

    def test_transcript_locator_parsing(self):
        from app.learning_engine.normalize import parse_transcript_locator
        self.assertEqual(parse_transcript_locator("00:31:18-00:31:51"),
                         (31 * 60_000 + 18_000, 31 * 60_000 + 51_000))
        self.assertEqual(parse_transcript_locator("[00:31:18 - 00:31:51]"),
                         (31 * 60_000 + 18_000, 31 * 60_000 + 51_000))
        self.assertIsNone(parse_transcript_locator("seg:3"))
        self.assertIsNone(parse_transcript_locator(""))

    def test_noise_detection_is_conservative(self):
        from app.learning_engine.normalize import classify_noise
        self.assertTrue(classify_noise("   ").is_noise)
        self.assertTrue(classify_noise("").is_noise)
        self.assertTrue(classify_noise("[图片]").is_noise)
        self.assertTrue(classify_noise("[docx 解析失败: x]").is_noise)
        # 有实义内容一律不是噪声
        for text in ("法线方向", "图 1 显示了入射角", "略述光的反射定律", "N/A 说明见教材"):
            self.assertFalse(classify_noise(text).is_noise, f"误判为噪声: {text}")

    def test_token_estimate_is_deterministic(self):
        from app.learning_engine.normalize import estimate_tokens, normalize_text
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("光"), 1)
        self.assertEqual(estimate_tokens("abcd"), 1)
        self.assertEqual(estimate_tokens("abcdefgh"), 2)
        self.assertEqual(estimate_tokens("光"), estimate_tokens(normalize_text("光")))

    def test_normalization_preserves_content(self):
        from app.learning_engine.normalize import normalize_text
        self.assertEqual(normalize_text("光 的 反 射"), "光的反射")
        self.assertEqual(normalize_text("a\r\nb"), "a\nb")
        self.assertEqual(normalize_text("  x  "), "x")
        # 不删减实义内容
        self.assertIn("反射定律", normalize_text("  反射定律  "))


if __name__ == "__main__":
    unittest.main()
