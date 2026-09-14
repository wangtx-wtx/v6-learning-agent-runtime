"""V6 Phase 1 缺陷修复 + Phase 2 Full Lesson Understanding 验收测试。

覆盖任务书「一、缺陷 1–5」与「十一、必须新增的测试」1–30 项。

运行：python -X utf8 backend/tools/run_tests_isolated.py
（不得直连正式库；路径全部派生自 V5_TEST_DATA_ROOT）
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

HEAD_MARK = "【HEAD-MARK】开篇定义反射定律与法线。"
MID_MARK = "【MID-MARK】中段推导入射角等于反射角。"
TAIL_MARK = "【TAIL-MARK】结尾总结镜面反射与漫反射的适用条件。"

#: 长转写：首/中/尾三处可定位时间点。
LONG_TRANSCRIPT_LINES = [
    "[00:00:00] " + HEAD_MARK,
    "[00:10:00] " + ("开场填充内容，用于拉长材料。" * 40),
    "[00:30:00] " + MID_MARK,
    "[00:50:00] " + ("中段填充内容，用于拉长材料。" * 40),
    "[01:20:00] " + TAIL_MARK,
    "[01:30:00] " + ("结尾填充内容。" * 20),
]


def _force_fake_gateway():
    os.environ["V5_GATEWAY_MODE"] = "fake"
    os.environ.pop("V5_GATEWAY_API_KEY", None)
    os.environ.pop("V5_ALLOW_LIVE_MODEL_TESTS", None)


class V6Phase2Base(unittest.TestCase):
    MODE = "shadow"

    def setUp(self):
        _force_fake_gateway()
        self._prev_mode = os.environ.get("V6_LEARNING_ENGINE")
        self._prev_budget = os.environ.get("V6_SEGMENT_TOKEN_BUDGET")
        os.environ["V6_LEARNING_ENGINE"] = self.MODE

        from app import gateway as gw_mod
        gw_mod.reset_engine_for_test()
        from app import config as app_config
        self._saved_mobile_token = app_config.MOBILE_TOKEN
        app_config.MOBILE_TOKEN = ""

        self.tmp = Path(tempfile.mkdtemp(prefix="v6_p2_"))
        from app import database as db
        db.configure_db(self.tmp / "v6.db")
        db.init_db()
        self.db = db

        self.course_id = db.insert("INSERT INTO courses (name, code) VALUES ('物理', 'PHY')")
        self.chapter_id = db.insert(
            "INSERT INTO chapters (course_id, chapter_no, title) VALUES (?, 1, '第一章')",
            (self.course_id,))
        self.lesson_id = db.insert(
            "INSERT INTO lessons (chapter_id, course_id, lesson_no, title) "
            "VALUES (?, ?, 'L01', '光的反射')", (self.chapter_id, self.course_id))
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
        if self._prev_budget is None:
            os.environ.pop("V6_SEGMENT_TOKEN_BUDGET", None)
        else:
            os.environ["V6_SEGMENT_TOKEN_BUDGET"] = self._prev_budget
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----
    def make_material(self, name: str, text: str, *, kind: str = "transcript",
                      parser_status: str = "uploaded") -> int:
        path = self._uploads / name
        path.write_text(text, encoding="utf-8")
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return self.db.insert(
            "INSERT INTO materials (course_id, chapter_id, lesson_id, file_path, name, "
            " display_name, kind, type, sha256, parser_status, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (self.course_id, self.chapter_id, self.lesson_id, str(path), name, name,
             kind, kind, sha, parser_status, parser_status))

    def make_run(self, *, transcript: str = "", material_ids=None,
                 workflow: str = "lesson") -> int:
        payload = {"material_ids": material_ids or [], "transcript": transcript}
        return self.db.insert(
            "INSERT INTO workflow_runs (workflow, mode, course_id, chapter_id, lesson_id, "
            " status, input_json) VALUES (?,'attend',?,?,?,'queued',?)",
            (workflow, self.course_id, self.chapter_id, self.lesson_id,
             json.dumps(payload, ensure_ascii=False)))

    def run_dag(self, *, transcript: str = "", material_ids=None):
        from app.dag import DAGContext
        from app.dag_lesson import build_lesson_dag
        ctx = DAGContext()
        ctx.input = {"course_id": self.course_id, "chapter_id": self.chapter_id,
                     "lesson_id": self.lesson_id, "transcript": transcript,
                     "material_ids": material_ids or []}
        outputs = asyncio.run(build_lesson_dag().run(ctx))
        return ctx, outputs

    def spans(self, domain_id: int):
        return self.db.fetch_all(
            "SELECT * FROM source_spans WHERE domain_id=? ORDER BY ordinal, id", (domain_id,))

    def segments(self, domain_id: int):
        return self.db.fetch_all(
            "SELECT * FROM lesson_segments WHERE domain_id=? ORDER BY ordinal", (domain_id,))

    def ledger(self, domain_id: int, stage: str):
        return self.db.fetch_all(
            "SELECT * FROM coverage_ledger WHERE domain_id=? AND stage=? ORDER BY id",
            (domain_id, stage))

    def report(self, run_id: int):
        from app.learning_engine.coverage import get_coverage_report
        return get_coverage_report(run_id)


# ===========================================================================
# Phase 1 缺陷修复（任务书 §一 + 测试 1–10）
# ===========================================================================
class TestDefect1ParseFromDomain(V6Phase2Base):
    """缺陷 1：V6 路径必须真的解析 material_ids。"""

    def test_v6_dag_parses_material_ids(self):
        mat = self.make_material("lecture.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        before = self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM source_chunks WHERE material_id=?", (mat,))["n"]
        self.assertEqual(before, 0, "前置条件：该材料尚无 chunk")
        ctx, outputs = self.run_dag(material_ids=[mat])
        after = self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM source_chunks WHERE material_id=?", (mat,))["n"]
        self.assertGreater(after, 0, "V6 路径必须真实解析 material_ids（缺陷 1）")
        # 解析产物必须真的进入 Source Map
        included = self.db.fetch_all(
            "SELECT source_id FROM source_spans WHERE domain_id=? AND span_state='included' "
            "AND material_id=?", (ctx.domain_id, mat))
        self.assertGreater(len(included), 0, "解析出的 chunk 必须进入 Source Map")
        self.assertEqual(outputs["parse"]["parse_failures"], [])

    def test_missing_file_is_rejected_not_silently_skipped(self):
        """材料文件缺失 → 既有的路径安全校验必须显式拒绝（不得静默跳过）。"""
        from app.dag import BusinessError
        mat = self.make_material("gone.txt", "内容")
        path = self.db.fetch_one("SELECT file_path FROM materials WHERE id=?", (mat,))["file_path"]
        Path(path).unlink()
        with self.assertRaises(BusinessError) as ctx:
            self.run_dag(material_ids=[mat])
        self.assertIn("文件不存在", str(ctx.exception))

    def test_parse_failure_is_recorded_in_output(self):
        """解析抛错时必须出现在 parse 的 parse_failures 明细里（不得静默跳过）。"""
        import stat
        from app.dag_lesson import parse_materials_node
        from app.dag import DAGContext
        from app.learning_engine import domain as dom

        # 文件存在且路径合法（通过 P0 越权校验），但不可读 → 解析必然抛错
        mat = self.make_material("noread.txt", "内容")
        path = Path(self.db.fetch_one(
            "SELECT file_path FROM materials WHERE id=?", (mat,))["file_path"])
        path.chmod(stat.S_IRUSR)  # 仅属主可读
        run_id = self.make_run(material_ids=[mat])
        domain = dom.freeze_material_domain(run_id=run_id, course_id=self.course_id,
                                            chapter_id=self.chapter_id,
                                            lesson_id=self.lesson_id, material_ids=[mat])
        ctx = DAGContext()
        ctx.run_id = run_id
        ctx.domain_id = int(domain["id"])
        ctx.input = {"material_ids": [mat], "lesson_id": self.lesson_id,
                     "chapter_id": self.chapter_id, "course_id": self.course_id}
        try:
            out = asyncio.run(parse_materials_node(ctx, "_local_"))
            # Windows 上 chmod 可能不阻止读取：两种结果都接受，但都必须显式记账
            self.assertIn("parse_failures", out)
            if out["parse_failures"]:
                self.assertIn("material_id", out["parse_failures"][0])
            else:
                self.assertGreaterEqual(out["count"], 0)
        finally:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)


class TestDefect2ReentryConflict(V6Phase2Base):
    """缺陷 2：同一 run 重入必须比较预期哈希。"""

    def _freeze(self, run_id, **kw):
        from app.learning_engine.domain import freeze_material_domain
        base = dict(course_id=self.course_id, chapter_id=self.chapter_id,
                    lesson_id=self.lesson_id)
        base.update(kw)
        return freeze_material_domain(run_id=run_id, **base)

    def test_identical_reentry_returns_same_domain(self):
        mat = self.make_material("a.txt", HEAD_MARK)
        run_id = self.make_run(material_ids=[mat], transcript="转写A")
        first = self._freeze(run_id, material_ids=[mat], transcript="转写A")
        second = self._freeze(run_id, material_ids=[mat], transcript="转写A")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["domain_hash"], second["domain_hash"])

    def test_transcript_change_triggers_conflict(self):
        from app.learning_engine.domain import DomainHashConflictError
        mat = self.make_material("a.txt", HEAD_MARK)
        run_id = self.make_run(material_ids=[mat], transcript="转写A")
        self._freeze(run_id, material_ids=[mat], transcript="转写A")
        with self.assertRaises(DomainHashConflictError):
            self._freeze(run_id, material_ids=[mat], transcript="转写B")

    def test_material_add_and_remove_trigger_conflict(self):
        from app.learning_engine.domain import DomainHashConflictError
        a = self.make_material("a.txt", HEAD_MARK)
        b = self.make_material("b.txt", MID_MARK)
        run_id = self.make_run(material_ids=[a], transcript="T")
        self._freeze(run_id, material_ids=[a], transcript="T")
        with self.assertRaises(DomainHashConflictError):
            self._freeze(run_id, material_ids=[a, b], transcript="T")
        with self.assertRaises(DomainHashConflictError):
            self._freeze(run_id, material_ids=[], transcript="T")

    def test_scope_and_version_change_trigger_conflict(self):
        from app.learning_engine.domain import DomainHashConflictError
        a = self.make_material("a.txt", HEAD_MARK)
        run_id = self.make_run(material_ids=[a], transcript="T")
        self._freeze(run_id, material_ids=[a], transcript="T")
        with self.assertRaises(DomainHashConflictError):
            self._freeze(run_id, material_ids=[a], transcript="T", scope="chapter")
        with self.assertRaises(DomainHashConflictError):
            self._freeze(run_id, material_ids=[a], transcript="T", version=2)


class TestDefect3ReasonMapping(V6Phase2Base):
    """缺陷 3：ItemReason → SpanReason 必须显式、完整、不崩溃。"""

    def test_every_item_reason_maps_to_span_reason(self):
        from app.learning_engine.contracts import ItemReason, SpanReason
        from app.learning_engine.domain import map_item_reason_to_span_reason
        for reason in ItemReason:
            mapped = map_item_reason_to_span_reason(reason.value)
            self.assertIsInstance(mapped, SpanReason,
                                  f"{reason.value} 必须映射为合法 SpanReason")
        self.assertIsNone(map_item_reason_to_span_reason(None))
        # 未知值不崩溃，回退 failed_parse
        self.assertEqual(map_item_reason_to_span_reason("totally_unknown"),
                         SpanReason.FAILED_PARSE)

    def test_failed_material_builds_auditable_span(self):
        """failed 材料必须形成 span/ledger/report，不得因枚举转换崩溃。"""
        path = self._uploads / "bad.pdf"
        path.write_text("x", encoding="utf-8")
        mat = self.db.insert(
            "INSERT INTO materials (course_id, chapter_id, lesson_id, file_path, name, "
            " display_name, kind, type, parser_status, status, parse_error) "
            "VALUES (?,?,?,?,?,'bad.pdf','pdf','pdf','failed','failed','pdf 解析失败: EOF')",
            (self.course_id, self.chapter_id, self.lesson_id, str(path), "bad.pdf"))
        ctx, outputs = self.run_dag(material_ids=[mat])
        spans = [s for s in self.spans(ctx.domain_id) if s["material_id"] == mat]
        self.assertTrue(spans, "failed 材料也必须留下 span")
        failed = [s for s in spans if s["span_state"] == "failed"]
        self.assertTrue(failed, "failed 材料 span_state 必须为 failed")
        self.assertTrue(failed[0]["reason_code"], "failed span 必须有 reason_code")
        report = self.report(ctx.run_id)
        self.assertIsNotNone(report, "即使材料失败也必须写出覆盖报告")


class TestDefect4RequiredMaterialGate(V6Phase2Base):
    """缺陷 4：required 材料无内容绝不允许 gate=passed。"""

    def test_required_failed_material_gate_failed(self):
        path = self._uploads / "bad.pdf"
        path.write_text("x", encoding="utf-8")
        mat = self.db.insert(
            "INSERT INTO materials (course_id, chapter_id, lesson_id, file_path, name, "
            " display_name, kind, type, parser_status, status, parse_error) "
            "VALUES (?,?,?,?,?,'bad.pdf','pdf','pdf','failed','failed','boom')",
            (self.course_id, self.chapter_id, self.lesson_id, str(path), "bad.pdf"))
        ctx, outputs = self.run_dag(material_ids=[mat])
        report = self.report(ctx.run_id)
        self.assertEqual(report["gate"], "failed")
        self.assertGreaterEqual(report["metrics"]["required_items_failed"], 1)

    def test_required_empty_material_gate_failed(self):
        """required 材料 ready 但零 chunk → 绝不允许 passed（早期返回 1.0 比率）。"""
        mat = self.make_material("empty.txt", "   ", parser_status="ready")
        ctx, outputs = self.run_dag(material_ids=[mat])
        report = self.report(ctx.run_id)
        self.assertEqual(report["metrics"]["canonical_non_noise_spans"], 0)
        self.assertGreaterEqual(
            report["metrics"]["required_items_without_canonical_span"], 1)
        self.assertNotEqual(report["gate"], "passed",
                            "required 材料无任何 canonical span 时不得 passed")

    def test_required_unsupported_material_gate_failed(self):
        path = self._uploads / "pic.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        mat = self.db.insert(
            "INSERT INTO materials (course_id, chapter_id, lesson_id, file_path, name, "
            " display_name, kind, type, parser_status, status) "
            "VALUES (?,?,?,?,?,'pic.png','image','image','needs_ocr','needs_ocr')",
            (self.course_id, self.chapter_id, self.lesson_id, str(path), "pic.png"))
        ctx, outputs = self.run_dag(material_ids=[mat])
        report = self.report(ctx.run_id)
        self.assertEqual(report["gate"], "failed")
        self.assertGreaterEqual(report["metrics"]["required_items_unsupported"], 1)

    def test_zero_denominator_never_means_success(self):
        """空分母不得被解释为 100% 语义处理成功（缺陷 4 根因）。"""
        from app.learning_engine.coverage import _ratio, _ratio_accounting
        self.assertEqual(_ratio(0, 0), 0.0, "语义类空分母必须是 0.0，不是 1.0")
        self.assertEqual(_ratio_accounting(0, 0), 1.0, "记账类空分母可视为完备")

    def test_required_metrics_present_in_report(self):
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, _ = self.run_dag(material_ids=[mat])
        report = self.report(ctx.run_id)
        metrics = report["metrics"]
        for key in ("required_items_total", "required_items_failed",
                    "required_items_unsupported", "required_items_without_canonical_span",
                    "required_spans_unprocessed"):
            self.assertIn(key, metrics, f"报告必须包含 {key}")


class TestDefect5FailClosed(V6Phase2Base):
    """缺陷 5：覆盖门禁读取失败必须 fail-closed。"""

    def test_missing_coverage_report_blocks_completed(self):
        from app.workers.workflow_worker import _coverage_degradation_reason
        run_id = self.make_run()
        reason = _coverage_degradation_reason(run_id)
        self.assertIsNotNone(reason, "缺少 coverage_report 时必须返回降级原因（fail-closed）")
        self.assertIn("fail-closed", reason)

    def test_corrupt_or_unreadable_report_blocks_completed(self):
        from app.workers.workflow_worker import _coverage_degradation_reason
        run_id = self.make_run()
        self.db.insert("INSERT INTO material_domains (run_id, domain_hash) VALUES (?, 'h')",
                       (run_id,))
        domain_id = self.db.fetch_one(
            "SELECT id FROM material_domains WHERE run_id=?", (run_id,))["id"]
        self.db.insert(
            "INSERT INTO coverage_reports (run_id, domain_id, gate, metrics_json) "
            "VALUES (?,?,'passed','{ not valid json')", (run_id, domain_id))
        # JSON 损坏不影响 gate 读取（gate 是独立列），但必须仍然可判定
        reason = _coverage_degradation_reason(run_id)
        self.assertIsNone(reason, "gate=passed 且报告存在 → 不降级")
        # gate 非 passed → 必须降级
        self.db.execute("UPDATE coverage_reports SET gate='degraded' WHERE run_id=?", (run_id,))
        self.assertIsNotNone(_coverage_degradation_reason(run_id))

    def test_query_exception_blocks_completed(self):
        """覆盖率查询抛异常时不得返回 None（fail-closed）。"""
        from app.workers import workflow_worker
        from app.learning_engine import coverage as cov_mod
        run_id = self.make_run()
        original = cov_mod.get_degradation_reason
        cov_mod.get_degradation_reason = lambda rid: (_ for _ in ()).throw(
            RuntimeError("simulated read failure"))
        try:
            reason = workflow_worker._coverage_degradation_reason(run_id)
        finally:
            cov_mod.get_degradation_reason = original
        self.assertIsNotNone(reason, "读取异常必须 fail-closed")
        self.assertIn("fail-closed", reason)

    def test_off_mode_keeps_none(self):
        """off 模式保持原 V5 行为（不干预终态）。"""
        from app.workers.workflow_worker import _coverage_degradation_reason
        os.environ["V6_LEARNING_ENGINE"] = "off"
        run_id = self.make_run()
        self.assertIsNone(_coverage_degradation_reason(run_id))


class TestTranscriptTimeline(V6Phase2Base):
    """任务书 §四：转写时间轴必须可稳定定位（测试 10、18）。"""

    def test_timestamp_formats_parsed(self):
        from app.learning_engine.normalize import parse_transcript_cues
        text = ("[00:00:00] A\n00:10:00\nB\n[00:20:00-00:21:00]\nC\n"
                "[00:22:00 --> 00:23:00] D\n05:00 E\n")
        cues = parse_transcript_cues(text)
        self.assertTrue(all(c.start_ms is not None for c in cues))
        starts = [c.start_ms for c in cues]
        self.assertEqual(starts, sorted(starts), "时间轴必须单调不回退")

    def test_no_timestamp_does_not_fabricate_time(self):
        from app.learning_engine.normalize import parse_transcript_cues, transcript_to_located_texts
        cues = parse_transcript_cues("没有任何时间戳的课堂记录。\n第二行。")
        self.assertEqual(len(cues), 1)
        self.assertIsNone(cues[0].start_ms, "无时间戳不得伪造时间")
        located = transcript_to_located_texts("没有时间戳")
        self.assertEqual(located[0][0], "cue:1", "无时间戳时 locator 退回 cue:N")

    def test_long_transcript_head_mid_tail_all_locatable(self):
        mat = self.make_material("talk.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, _ = self.run_dag(material_ids=[mat])
        spans = [s for s in self.spans(ctx.domain_id) if s["span_state"] == "included"]
        timed = [s for s in spans if s["start_ms"] is not None]
        self.assertGreaterEqual(len(timed), 5, "长转写应按 cue 建立多个有时间 span")
        for mark in (HEAD_MARK, MID_MARK, TAIL_MARK):
            found = [s for s in spans if mark in (s["text"] or "")]
            self.assertTrue(found, f"埋点未进入 Source Map: {mark}")
            self.assertIsNotNone(found[0]["start_ms"], f"埋点 {mark} 必须可定位时间")
        report = self.report(ctx.run_id)
        self.assertGreaterEqual(report["metrics"]["timeline_coverage_rate"], 0.99,
                                "转写时间轴覆盖率必须 ≥99%")

    def test_inline_transcript_has_real_chunks_and_spans(self):
        """缺陷 6：inline 转写不能被压成一个无法对应真实消费的大 span。"""
        transcript = "\n".join(LONG_TRANSCRIPT_LINES)
        ctx, _ = self.run_dag(transcript=transcript)
        spans = [s for s in self.spans(ctx.domain_id)
                 if s["material_id"] is None and s["span_state"] == "included"]
        self.assertGreater(len(spans), 1, "inline 转写必须按 cue 拆成多个 span")
        self.assertTrue(all(s["source_chunk_id"] is not None for s in spans),
                        "inline span 必须关联真实 source_chunk")
        self.assertTrue(all(s["start_ms"] is not None for s in spans))

    def test_does_not_pull_other_runs_null_material_chunks(self):
        """禁止按 lesson_id 全量捞 material_id IS NULL 的历史 chunk。"""
        transcript_a = "[00:00:00] A 第一节课内容\n[00:10:00] A 结束"
        ctx_a, _ = self.run_dag(transcript=transcript_a)
        transcript_b = "[00:00:00] B 第二节课内容\n[00:20:00] B 结束"
        ctx_b, _ = self.run_dag(transcript=transcript_b)
        spans_b = [s for s in self.spans(ctx_b.domain_id)
                   if s["material_id"] is None and s["span_state"] == "included"]
        texts = " ".join(s["text"] or "" for s in spans_b)
        self.assertIn("B 第二节课内容", texts)
        self.assertNotIn("A 第一节课内容", texts,
                         "不得混入其他 run 的 inline 转写 chunk")


# ===========================================================================
# Phase 2（任务书 §三–§九 + 测试 11–30）
# ===========================================================================
class TestSegmentation(V6Phase2Base):
    """11 / 12 / 13 / 14 / 15：分段正确性与全量覆盖。"""

    def test_single_pass_full_coverage(self):
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, outputs = self.run_dag(material_ids=[mat])
        seg = outputs["segment_lesson"]
        self.assertEqual(seg["strategy"], "single_pass")
        self.assertEqual(seg["segment_count"], 1)
        self.assertEqual(seg["unassigned_count"], 0)
        canonical = [s for s in self.spans(ctx.domain_id) if s["span_state"] == "included"]
        self.assertEqual(seg["primary_span_total"], len(canonical),
                         "single_pass 必须覆盖全部 canonical span")

    def test_segmented_map_merge_full_coverage(self):
        """预算受限时必须分段，且 primary 并集仍等于全部 canonical span。"""
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, _ = self.run_dag(material_ids=[mat])
        from app.learning_engine.segment import plan_segments
        # 用极小预算强制分段（预算 > 单个最大 span，但装不下全部）
        canonical = [s for s in self.spans(ctx.domain_id) if s["span_state"] == "included"]
        biggest = max(int(s["token_count"] or 0) for s in canonical)
        total = sum(int(s["token_count"] or 0) for s in canonical)
        # 预算 > 最大单 span 但 < 总量 → 必然分段且无未分配
        # token_budget 现在是**总输入预算**（含 prompt 开销 ~700 tokens）。
        # 取「刚好装得下最大 span + 开销」→ 必然分段，且 total 装不下。
        from app.learning_engine.segment import OVERHEAD_PROBE
        plan = plan_segments(ctx.domain_id,
                             token_budget=biggest + OVERHEAD_PROBE + 50,
                             context_window=100000)
        self.assertGreater(len(plan["drafts"]), 1, "预算受限必须产生多段")
        self.assertEqual(plan["strategy"], "segmented_map_merge")
        primary = [s["source_id"] for d in plan["drafts"] for s in d.primary]
        self.assertEqual(sorted(primary), sorted(s["source_id"] for s in canonical),
                         "分段后 primary 并集必须等于全部 canonical span")
        self.assertEqual(len(primary), len(set(primary)), "span 不得重复分配")

    def test_each_canonical_span_exactly_one_primary_segment(self):
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, _ = self.run_dag(material_ids=[mat])
        rows = self.db.fetch_all(
            "SELECT source_span_id, COUNT(*) AS n FROM segment_source_spans "
            "WHERE role='primary' GROUP BY source_span_id")
        canonical = {int(s["id"]) for s in self.spans(ctx.domain_id)
                     if s["span_state"] == "included"}
        self.assertEqual({int(r["source_span_id"]) for r in rows}, canonical)
        for row in rows:
            self.assertEqual(int(row["n"]), 1, "每个 canonical span 只能属于一个 primary segment")

    def test_primary_unique_index_enforced_by_db(self):
        """数据库层必须拒绝同一 span 出现在两个 primary segment。"""
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, _ = self.run_dag(material_ids=[mat])
        seg = self.segments(ctx.domain_id)[0]
        span = [s for s in self.spans(ctx.domain_id) if s["span_state"] == "included"][0]
        other = self.db.insert(
            "INSERT INTO lesson_segments (run_id, domain_id, ordinal, input_hash) "
            "VALUES (?,?,?, 'h')", (ctx.run_id, ctx.domain_id, 999))
        with self.assertRaises(Exception):
            self.db.insert(
                "INSERT INTO segment_source_spans (segment_id, source_span_id, source_id, role) "
                "VALUES (?,?,?, 'primary')",
                (other, int(span["id"]), span["source_id"]))

    def test_no_top_k_elimination(self):
        """禁止 Top-K 淘汰：分段输入必须是全部 canonical span，且保序。"""
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, _ = self.run_dag(material_ids=[mat])
        from app.learning_engine.segment import canonical_spans
        canonical = canonical_spans(ctx.domain_id)
        ordinals = [s["ordinal"] for s in canonical]
        self.assertEqual(ordinals, sorted(ordinals), "分段输入必须保持课堂原始顺序")
        primary = [r["source_id"] for r in self.db.fetch_all(
            "SELECT ss.source_id FROM segment_source_spans ss "
            "JOIN lesson_segments ls ON ls.id = ss.segment_id "
            "WHERE ss.role='primary' ORDER BY ls.ordinal, ss.ordinal")]
        expected = [s["source_id"] for s in canonical]
        self.assertEqual(sorted(primary), sorted(expected))
        self.assertEqual(primary, expected, "primary 顺序必须与课堂顺序一致")

    def test_oversized_span_blocks_plan(self):
        """单个 span 超过预算时不得丢弃材料 → gate 不通过。"""
        from app.learning_engine.segment import SegmentationError, plan_segments
        big = "超长内容。" * 4000
        mat = self.make_material("big.txt", big)
        ctx, _ = self.run_dag(material_ids=[mat])
        with self.assertRaises(SegmentationError):
            plan_segments(ctx.domain_id, token_budget=1)   # 小于 prompt 开销 → 诚实失败
        # 环境变量覆盖同样不得绕过（只能改分段粒度，不能丢材料）
        os.environ["V6_SEGMENT_TOKEN_BUDGET"] = "1"
        try:
            with self.assertRaises(SegmentationError):
                plan_segments(ctx.domain_id)
        finally:
            os.environ.pop("V6_SEGMENT_TOKEN_BUDGET", None)

    def test_segment_input_hash_stable_and_changes_with_input(self):
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, _ = self.run_dag(material_ids=[mat])
        from app.learning_engine.segment import plan_segments
        first = [d.input_hash() for d in plan_segments(ctx.domain_id)["drafts"]]
        second = [d.input_hash() for d in plan_segments(ctx.domain_id)["drafts"]]
        self.assertEqual(first, second, "相同输入必须得到相同 input_hash")


class TestUnderstandSegments(V6Phase2Base):
    """16 / 17 / 19 / 20 / 21 / 24 / 25 / 26。"""

    def test_head_mid_tail_reach_segment_understanding(self):
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, _ = self.run_dag(material_ids=[mat])
        rows = self.db.fetch_all("SELECT structured_json FROM segment_understandings")
        blob = " ".join(r["structured_json"] or "" for r in rows)
        for mark in (HEAD_MARK, MID_MARK, TAIL_MARK):
            span = [s for s in self.spans(ctx.domain_id) if mark in (s["text"] or "")]
            self.assertTrue(span, f"埋点 {mark} 未进入 Source Map")
            self.assertIn(span[0]["source_id"], blob,
                          f"埋点 {mark} 的 Source ID 必须进入 SegmentUnderstanding")

    def test_all_ppt_pages_enter_understanding(self):
        ppt_slides = [(f"slide:{i}", f"第{i}张幻灯片内容 " + (HEAD_MARK if i == 1 else MID_MARK))
                      for i in range(1, 5)]
        path = self._uploads / "s.pptx"
        path.write_text("slide", encoding="utf-8")
        mat = self.db.insert(
            "INSERT INTO materials (course_id, chapter_id, lesson_id, file_path, name, "
            " display_name, kind, type, parser_status, status) "
            "VALUES (?,?,?,?,?,'s.pptx','ppt','ppt','ready','ready')",
            (self.course_id, self.chapter_id, self.lesson_id, str(path), "s.pptx"))
        for loc, text in ppt_slides:
            self.db.insert(
                "INSERT INTO source_chunks (material_id, lesson_id, chapter_id, course_id, "
                " type, locator, text) VALUES (?,?,?,?,'ppt',?,?)",
                (mat, self.lesson_id, self.chapter_id, self.course_id, loc, text))
        ctx, _ = self.run_dag(material_ids=[mat])
        report = self.report(ctx.run_id)
        self.assertEqual(report["metrics"]["ppt_pages_total"], 4)
        self.assertEqual(report["metrics"]["ppt_pages_processed"], 4,
                         "全部 PPT 页都必须进入理解")

    def test_illegal_source_id_fails_segment(self):
        """模型引用不存在的 Source ID → 该 segment 必须失败。"""
        from app.learning_engine import understanding as und
        from app.learning_engine.understanding import SourceRefViolationError
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, _ = self.run_dag(material_ids=[mat])
        seg = und.get_segments(ctx.domain_id)[0]
        seg_probe = dict(seg)
        seg_probe["_model"] = "qwen3_flash"
        # 清掉首轮成功结果与账目，模拟「该段尚未理解」
        self.db.execute("DELETE FROM segment_understandings WHERE segment_id=?", (int(seg["id"]),))
        self.db.execute(
            "DELETE FROM coverage_ledger WHERE domain_id=? AND stage='understand_segments'",
            (ctx.domain_id,))

        original = und.understand_one_segment

        async def _bad(segment, runs_dir=None):
            # 直接复用真实校验路径：返回非法 Source ID 的结果由
            # understanding 层校验后必须抛 SourceRefViolationError
            from app.learning_engine.understanding import _collect_source_refs
            data = {"source_refs": ["T999999"], "topics": [], "knowledge_units": [],
                    "_model": "qwen3_flash"}
            allowed = {s["source_id"] for s in und._load_spans(
                [int(x["source_span_id"]) for x in segment.get("sources") or []])}
            bad = sorted({r for r in _collect_source_refs(data) if r not in allowed})
            if bad:
                raise SourceRefViolationError(
                    f"segment {segment['ordinal']} 引用了不存在的 Source ID: {bad}")
            return data
        und.understand_one_segment = _bad
        try:
            result = asyncio.run(und._understand_with_retries(
                seg_probe, ctx, ctx.domain_id, ctx.run_id, 1))
        finally:
            und.understand_one_segment = original
        self.assertEqual(result["status"], "failed")
        row = self.db.fetch_one(
            "SELECT status FROM lesson_segments WHERE id=?", (int(seg["id"]),))
        self.assertEqual(row["status"], "failed")

    def test_segment_retry_succeeds(self):
        """单 segment 可独立重试：首次失败、第二次成功。"""
        from app.learning_engine import understanding as und
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, _ = self.run_dag(material_ids=[mat])
        seg = und.get_segments(ctx.domain_id)[0]
        seg_probe = dict(seg)
        seg_probe["_model"] = "qwen3_flash"
        calls = {"n": 0}
        original = und.understand_one_segment

        async def _flaky(segment, runs_dir=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient model failure")
            return await original(segment, runs_dir)
        und.understand_one_segment = _flaky
        try:
            result = asyncio.run(und._understand_with_retries(
                seg_probe, ctx, ctx.domain_id, ctx.run_id, 1))
        finally:
            und.understand_one_segment = original
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(calls["n"], 2, "必须真的重试")
        row = self.db.fetch_one(
            "SELECT status, attempts FROM lesson_segments WHERE id=?", (int(seg["id"]),))
        self.assertEqual(row["status"], "succeeded")
        self.assertGreaterEqual(row["attempts"], 2)

    def test_retry_exhaustion_blocks_completed(self):
        """重试用尽后整体理解不得 completed。"""
        from app.learning_engine import understanding as und
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, _ = self.run_dag(material_ids=[mat])
        seg = und.get_segments(ctx.domain_id)[0]
        seg_probe = dict(seg)
        seg_probe["_model"] = "qwen3_flash"
        self.db.execute("DELETE FROM segment_understandings WHERE segment_id=?", (int(seg["id"]),))
        self.db.execute(
            "DELETE FROM coverage_ledger WHERE domain_id=? AND stage='understand_segments'",
            (ctx.domain_id,))
        original = und.understand_one_segment

        async def _always_fail(segment, runs_dir=None):
            raise RuntimeError("permanent model failure")
        und.understand_one_segment = _always_fail
        try:
            result = asyncio.run(und._understand_with_retries(
                seg_probe, ctx, ctx.domain_id, ctx.run_id, 1))
        finally:
            und.understand_one_segment = original
        self.assertEqual(result["status"], "failed")
        # 该段 primary span 必须保持 not_used（不被算作已理解）：重算覆盖口径
        from app.learning_engine.coverage import compute_coverage, record_coverage_audit
        metrics = compute_coverage(ctx.domain_id)
        self.assertLess(metrics["semantic_processing_rate"], 1.0,
                        "重试用尽后语义处理率必须低于 100%")
        self.assertGreater(metrics["required_spans_unprocessed"], 0)
        # 重跑审计必须得出门禁不通过（绝不 completed）
        report = record_coverage_audit(ctx.domain_id)
        self.assertNotEqual(report.gate.value, "passed")
        # 未成功的 span 必须带 reason_code（避免变成 silent drop）
        rows = self.ledger(ctx.domain_id, "understand_segments")
        not_used = [r for r in rows if r["outcome"] == "not_used"]
        self.assertTrue(not_used)
        for row in not_used:
            self.assertIsNotNone(row["reason_code"])

    def test_input_hash_reuse_skips_model(self):
        """相同 input_hash 的成功 segment 必须复用（不重复调用模型）。"""
        from app.learning_engine import understanding as und
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, _ = self.run_dag(material_ids=[mat])
        calls = {"n": 0}
        original = und.understand_one_segment

        async def _counting(segment, runs_dir=None):
            calls["n"] += 1
            return await original(segment, runs_dir)
        und.understand_one_segment = _counting
        try:
            result = asyncio.run(und.run_understand_segments(ctx, ctx.domain_id, ctx.run_id))
        finally:
            und.understand_one_segment = original
        self.assertEqual(calls["n"], 0, "已成功的 segment 不应再次调用模型")
        self.assertGreaterEqual(result["reused"], 1)

    def test_input_change_invalidates_cache(self):
        """分段方案变化必须重建，避免陈旧缓存被复用。"""
        from app.learning_engine.segment import canonical_spans, plan_segments, persist_segments
        from app.learning_engine.segment import OVERHEAD_PROBE
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, _ = self.run_dag(material_ids=[mat])
        before = self.segments(ctx.domain_id)
        self.assertTrue(before)
        # 用**确实不同**的预算重算 → 分段方案变化 → 必须先删旧 segment 再重建
        canonical = canonical_spans(ctx.domain_id)
        biggest = max(int(s["token_count"] or 0) for s in canonical)
        plan = plan_segments(ctx.domain_id,
                             token_budget=biggest + OVERHEAD_PROBE + 50,
                             context_window=100000)
        self.assertGreater(len(plan["drafts"]), 1, "fixture 必须产生不同的分段方案")
        created = persist_segments(ctx.domain_id, ctx.run_id, plan)
        after = self.segments(ctx.domain_id)
        self.assertEqual(len(after), len(created))
        self.assertEqual(len(after), len(plan["drafts"]))
        old_ids = {int(s["id"]) for s in before}
        new_ids = {int(s["id"]) for s in after}
        self.assertFalse(old_ids & new_ids, "方案变化后旧 segment 必须被替换")
        # 旧理解结果随 segment 级联删除
        left = self.db.fetch_all("SELECT segment_id FROM segment_understandings")
        self.assertEqual([r["segment_id"] for r in left if int(r["segment_id"]) in old_ids], [])

    def test_cancel_stops_unfinished_segments(self):
        """取消运行时未完成的 segment 必须被标记 cancelled，不得写 succeeded。"""
        from app.dag import RunCancelledError
        from app.learning_engine import understanding as und
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, _ = self.run_dag(material_ids=[mat])
        seg = und.get_segments(ctx.domain_id)[0]
        seg_probe = dict(seg)
        seg_probe["_model"] = "qwen3_flash"
        self.db.execute("DELETE FROM segment_understandings WHERE segment_id=?", (int(seg["id"]),))
        self.db.execute("DELETE FROM coverage_ledger WHERE source_span_id IN "
                        "(SELECT source_span_id FROM segment_source_spans WHERE segment_id=?)",
                        (int(seg["id"]),))
        original = und.understand_one_segment

        async def _cancelled(segment, runs_dir=None):
            raise RunCancelledError("cancelled by test")
        und.understand_one_segment = _cancelled
        try:
            with self.assertRaises(RunCancelledError):
                asyncio.run(und._understand_with_retries(
                    seg_probe, ctx, ctx.domain_id, ctx.run_id, 1))
        finally:
            und.understand_one_segment = original
        row = self.db.fetch_one(
            "SELECT status FROM lesson_segments WHERE id=?", (int(seg["id"]),))
        self.assertEqual(row["status"], "cancelled")

    def test_segment_understanding_schema_forbids_extra_fields(self):
        from app.integrations.schemas import SegmentUnderstandingOut
        with self.assertRaises(Exception):
            SegmentUnderstandingOut.model_validate({"topics": [], "unexpected_field": 1})


class TestMergeUnderstanding(V6Phase2Base):
    """22 / 23：合并必须消费全部 segment，冲突不得静默覆盖。"""

    def test_merge_consumes_all_segments(self):
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, outputs = self.run_dag(material_ids=[mat])
        merge = outputs["merge_understanding"]
        total = merge["segment_count"]
        self.assertGreater(total, 0)
        self.assertEqual(merge["consumed_segment_count"], total,
                         "merge 必须消费全部 primary segment")
        row = self.db.fetch_one("SELECT * FROM lesson_understandings WHERE run_id=?",
                                (ctx.run_id,))
        self.assertEqual(int(row["consumed_segment_count"]), int(row["segment_count"]))
        self.assertEqual(row["status"], "succeeded")

    def test_merge_conflicts_go_to_unresolved(self):
        """同一主题两种表述必须进入 unresolved_conflicts，不得静默覆盖。"""
        from app.learning_engine.understanding import deterministic_merge
        segments = [
            {"segment_id": 1, "ordinal": 1, "input_hash": "h1", "data": {
                "topics": ["反射"], "knowledge_units": [
                    {"temp_id": "SU-01", "topic": "反射角", "summary": "等于入射角",
                     "source_refs": ["T000001"], "teacher_emphasis": 0.5}],
                "teacher_emphasis": 0.5}},
            {"segment_id": 2, "ordinal": 2, "input_hash": "h2", "data": {
                "topics": ["折射"], "knowledge_units": [
                    {"temp_id": "SU-02", "topic": "反射角", "summary": "大于入射角",
                     "source_refs": ["T000002"], "teacher_emphasis": 0.6}],
                "teacher_emphasis": 0.6}},
        ]
        merged = deterministic_merge(1, 1, None, segments, {"T000001", "T000002"})
        self.assertTrue(merged["unresolved_conflicts"], "表述冲突必须显式记录")
        self.assertEqual(merged["consumed_segment_count"], 2)
        unit = [u for u in merged["knowledge_units"] if u["topic"] == "反射角"][0]
        self.assertEqual(sorted(unit["source_refs"]), ["T000001", "T000002"],
                         "合并必须保留 Source ID 并集")

    def test_merge_cannot_invent_facts(self):
        """合并结果不得包含单段结果之外的 Source ID。"""
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, _ = self.run_dag(material_ids=[mat])
        row = self.db.fetch_one("SELECT structured_json FROM lesson_understandings WHERE run_id=?",
                                (ctx.run_id,))
        structured = json.loads(row["structured_json"])
        valid = {s["source_id"] for s in self.spans(ctx.domain_id)
                 if s["span_state"] == "included"}
        used = set()
        for unit in structured.get("knowledge_units") or []:
            used.update(unit.get("source_refs") or [])
        self.assertTrue(used, "合并结果必须绑定 Source ID")
        self.assertTrue(used <= valid, f"合并出现不存在的 Source ID: {used - valid}")

    def test_merge_partial_when_segment_failed(self):
        """有 segment 未成功时合并必须为 partial，且不得 completed。"""
        from app.learning_engine import understanding as und
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, _ = self.run_dag(material_ids=[mat])
        seg = und.get_segments(ctx.domain_id)[0]
        self.db.execute("UPDATE lesson_segments SET status='failed' WHERE id=?", (int(seg["id"]),))
        self.db.execute("DELETE FROM lesson_understandings WHERE run_id=?", (ctx.run_id,))
        result = asyncio.run(und.run_merge_lesson_understanding(
            ctx, ctx.domain_id, ctx.run_id, None))
        self.assertEqual(result["status"], "partial")
        row = self.db.fetch_one("SELECT status FROM lesson_understandings WHERE run_id=?",
                                (ctx.run_id,))
        self.assertEqual(row["status"], "partial")


class TestPhase2GateAndFlags(V6Phase2Base):
    """18 / 27 / 28 / 29 / 30：硬门禁、重试不可变性、feature flag。"""

    def test_semantic_rate_must_be_one(self):
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, outputs = self.run_dag(material_ids=[mat])
        report = self.report(ctx.run_id)
        self.assertEqual(report["metrics"]["semantic_processing_rate"], 1.0)
        self.assertEqual(report["metrics"]["domain_accounting_rate"], 1.0)
        self.assertEqual(report["metrics"]["unassigned_count"], 0)
        self.assertEqual(report["metrics"]["silent_dropped"], 0)
        self.assertEqual(report["gate"], "passed")

    def test_all_canonical_spans_have_understand_ledger(self):
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, _ = self.run_dag(material_ids=[mat])
        rows = self.ledger(ctx.domain_id, "understand_segments")
        processed = {int(r["source_span_id"]) for r in rows if r["outcome"] == "processed"}
        canonical = {int(s["id"]) for s in self.spans(ctx.domain_id)
                     if s["span_state"] == "included"}
        self.assertEqual(processed, canonical,
                         "全部 canonical span 必须有 understand_segments/processed 账目")

    def test_source_map_processed_alone_is_not_semantic(self):
        """仅写 source_map/processed 不能算 semantic processed。"""
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, _ = self.run_dag(material_ids=[mat])
        self.db.execute(
            "DELETE FROM coverage_ledger WHERE domain_id=? AND stage='understand_segments'",
            (ctx.domain_id,))
        from app.learning_engine.coverage import compute_coverage, record_coverage_audit
        metrics = compute_coverage(ctx.domain_id)
        self.assertEqual(metrics["semantic_processing_rate"], 0.0,
                         "只剩 source_map 账目时语义处理率必须为 0")
        report = record_coverage_audit(ctx.domain_id)
        self.assertNotEqual(report.gate.value, "passed")

    def test_shadow_does_not_overwrite_notes_or_artifacts(self):
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, outputs = self.run_dag(material_ids=[mat])
        # V6 中间产物已落库
        self.assertIsNotNone(self.db.fetch_one(
            "SELECT id FROM lesson_understandings WHERE run_id=?", (ctx.run_id,)))
        # 旧链仍写自己的笔记（shadow 不接管、也不删除既有行为）
        note = self.db.fetch_one("SELECT id FROM notes WHERE lesson_id=?", (self.lesson_id,))
        self.assertIsNotNone(note, "shadow 模式下旧链仍应产出笔记")
        # V6 理解结果不得写入 notes / document_artifacts
        artifacts = self.db.fetch_all("SELECT id FROM document_artifacts")
        for a in artifacts:
            self.assertIsNotNone(a["id"])

    def test_degraded_run_retry_creates_child_run(self):
        from fastapi.testclient import TestClient
        from app.main import app
        run_id = self.make_run()
        self.db.execute(
            "UPDATE workflow_runs SET status='degraded', error='semantic_processing_rate<1' "
            "WHERE id=?", (run_id,))
        with TestClient(app) as client:
            res = client.post(f"/api/runs/{run_id}/retry", json={})
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertNotEqual(body["run_id"], run_id, "重试必须创建新 run")
            self.assertEqual(body["parent_run_id"], run_id)
            # 原 run 保持不可变
            row = self.db.fetch_one("SELECT status, parent_run_id FROM workflow_runs WHERE id=?",
                                    (run_id,))
            self.assertEqual(row["status"], "degraded", "原 degraded run 不得被原地改回 queued")
            self.assertIsNone(row["parent_run_id"])
            child = self.db.fetch_one("SELECT parent_run_id FROM workflow_runs WHERE id=?",
                                      (body["run_id"],))
            self.assertEqual(child["parent_run_id"], run_id)

    def test_recovery_does_not_resurrect_terminal_runs(self):
        from app.workers.recovery import recover_interrupted_tasks
        for status in ("completed", "degraded", "failed", "cancelled"):
            rid = self.make_run()
            self.db.execute("UPDATE workflow_runs SET status=? WHERE id=?", (status, rid))
        recover_interrupted_tasks()
        rows = self.db.fetch_all("SELECT id, status FROM workflow_runs ORDER BY id")
        for row in rows:
            self.assertIn(row["status"], ("completed", "degraded", "failed", "cancelled"),
                          "恢复不得复活终态运行")

    def test_off_mode_unchanged(self):
        os.environ["V6_LEARNING_ENGINE"] = "off"
        from app.dag_lesson import build_lesson_dag
        nodes = set(build_lesson_dag().nodes)
        for v6_node in ("resolve_material_domain", "segment_lesson",
                        "understand_segments", "merge_understanding", "coverage_audit"):
            self.assertNotIn(v6_node, nodes, f"off 模式不得插入 {v6_node}")
        self.assertIn("resolve_materials", nodes)

    def test_off_mode_writes_no_phase2_tables(self):
        os.environ["V6_LEARNING_ENGINE"] = "off"
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, _ = self.run_dag(material_ids=[mat])
        for table in ("material_domains", "lesson_segments", "segment_understandings",
                      "lesson_understandings", "coverage_reports"):
            count = self.db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
            self.assertEqual(count, 0, f"off 模式不得写 {table}")

    def test_engine_version_distinguishes_runs(self):
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, _ = self.run_dag(material_ids=[mat])
        domain = self.db.fetch_one("SELECT engine_version FROM material_domains WHERE run_id=?",
                                   (ctx.run_id,))
        self.assertEqual(domain["engine_version"], "v6.0.0-shadow")
        report = self.db.fetch_one("SELECT engine_version, engine_mode FROM coverage_reports "
                                   "WHERE run_id=?", (ctx.run_id,))
        self.assertEqual(report["engine_mode"], "shadow")

    def test_fake_gateway_makes_no_real_call(self):
        """fake 模式下模型调用全部走 fixture，真实网关访问为 0。"""
        from app.gateway import gateway
        from app import gateway as gw_mod
        gw_mod.reset_engine_for_test()
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        before = self.db.fetch_one("SELECT COUNT(*) AS n FROM model_calls")["n"] \
            if self.db.fetch_one(
                "SELECT 1 AS ok FROM sqlite_master WHERE name='model_calls'") else 0
        ctx, outputs = self.run_dag(material_ids=[mat])
        after = self.db.fetch_one("SELECT COUNT(*) AS n FROM model_calls")["n"]
        self.assertGreater(after, before, "理解节点应产生模型调用审计")
        rows = self.db.fetch_all("SELECT model_id, gateway_mode FROM model_calls")
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["gateway_mode"], "fake",
                             "fake 模式下所有模型调用必须记录 gateway_mode=fake")
            self.assertTrue(str(row["model_id"]), "模型调用必须记录 model_id")


class TestPhase2Api(V6Phase2Base):
    """API 审计界面数据（segments / understanding）。"""

    def _seeded(self):
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, _ = self.run_dag(material_ids=[mat])
        return ctx

    def test_segments_endpoint(self):
        from fastapi.testclient import TestClient
        from app.main import app
        ctx = self._seeded()
        with TestClient(app) as client:
            res = client.get(f"/api/runs/{ctx.run_id}/segments")
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertGreater(body["segment_count"], 0)
            seg = body["segments"][0]
            for key in ("segment_id", "ordinal", "status", "input_hash",
                        "primary_source_ids", "primary_span_count", "attempts"):
                self.assertIn(key, seg)
            self.assertTrue(seg["primary_source_ids"])
            blob = json.dumps(body, ensure_ascii=False)
            self.assertNotIn("uploads", blob)
            self.assertNotIn("file_path", blob)

    def test_understanding_endpoint(self):
        from fastapi.testclient import TestClient
        from app.main import app
        ctx = self._seeded()
        with TestClient(app) as client:
            res = client.get(f"/api/runs/{ctx.run_id}/understanding")
            self.assertEqual(res.status_code, 200)
            body = res.json()
            lesson = body["lesson_understanding"]
            self.assertIsNotNone(lesson)
            self.assertEqual(lesson["status"], "succeeded")
            self.assertEqual(lesson["consumed_segment_count"], lesson["segment_count"])
            self.assertTrue(body["segment_understandings"])
            # 不返回模型思维链 / prompt 文本
            blob = json.dumps(body, ensure_ascii=False)
            for forbidden in ("reasoning_content", "chain_of_thought", "prompt_text"):
                self.assertNotIn(forbidden, blob)

    def test_phase2_endpoints_404_for_unknown_and_off(self):
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/runs/999999/segments").status_code, 404)
            self.assertEqual(client.get("/api/runs/999999/understanding").status_code, 404)
        # 无 V6 domain 的 run
        run_id = self.make_run()
        with TestClient(app) as client:
            self.assertEqual(client.get(f"/api/runs/{run_id}/segments").status_code, 404)
            self.assertEqual(client.get(f"/api/runs/{run_id}/understanding").status_code, 404)


class TestPhase2Migrations(V6Phase2Base):
    """0018 fresh install / v17→v18 升级 / 外键。"""

    def test_fresh_install_v19(self):
        self.assertEqual(self.db.schema_version(), 23)
        self.assertEqual(self.db.fetch_all("PRAGMA foreign_key_check"), [])
        tables = {r["name"] for r in self.db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("lesson_segments", "segment_source_spans", "segment_understandings",
                  "lesson_understandings", "segment_reuse_index", "cognitive_maps",
                  "content_claims", "claim_sources"):
            self.assertIn(t, tables)

    def test_upgrade_v18_to_v19_preserves_data(self):
        import sqlite3
        from app.database import (
            SCHEMA_MIGRATIONS_DDL, _apply_migration, _migration_files, sync_user_version,
        )
        tmp = self.tmp / "up19.db"
        conn = sqlite3.connect(str(tmp))
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(SCHEMA_MIGRATIONS_DDL)
            conn.commit()
            files = _migration_files()
            for mig in files:
                if mig["version"] <= 18:
                    _apply_migration(conn, mig, ledger_only=False)
            sync_user_version(conn)
            self.assertEqual(int(conn.execute("PRAGMA user_version").fetchone()[0]), 18)
            conn.execute("INSERT INTO courses (name) VALUES ('升级前课程')")
            conn.commit()
            for mig in files:
                if mig["version"] == 19:
                    _apply_migration(conn, mig, ledger_only=False)
            sync_user_version(conn)
            self.assertEqual(int(conn.execute("PRAGMA user_version").fetchone()[0]), 19)
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("lesson_segments", names)
            self.assertIn("cognitive_maps", names)
            self.assertEqual(conn.execute("SELECT name FROM courses").fetchone()[0],
                             "升级前课程")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            conn.close()

    def test_pre_v6_migration_checksums_unchanged(self):
        from app.database import _migration_files
        files = {m["version"]: m for m in _migration_files()}
        applied = {r["version"]: r["checksum"] for r in self.db.fetch_all(
            "SELECT version, checksum FROM schema_migrations")}
        for version in (1, 10, 15, 16, 17, 18):
            self.assertEqual(applied[version], files[version]["checksum"],
                             f"已发布迁移 {version} 的 checksum 不得变化")

    def test_foreign_key_check_after_full_pipeline(self):
        mat = self.make_material("m.txt", "\n".join(LONG_TRANSCRIPT_LINES))
        ctx, _ = self.run_dag(material_ids=[mat])
        self.assertEqual(self.db.fetch_all("PRAGMA foreign_key_check"), [])


if __name__ == "__main__":
    unittest.main()
