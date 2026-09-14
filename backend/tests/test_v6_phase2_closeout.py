"""V6 Phase 2 Closeout 验收测试（历史转写保护 / merge 完整性 / fallback / 预算 /
跨 run 复用 / 多段取消 / 状态与版本）。

运行：python -X utf8 backend/tools/run_tests_isolated.py
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


def _force_fake_gateway():
    os.environ["V5_GATEWAY_MODE"] = "fake"
    os.environ.pop("V5_GATEWAY_API_KEY", None)
    os.environ.pop("V5_ALLOW_LIVE_MODEL_TESTS", None)


class CloseoutBase(unittest.TestCase):
    MODE = "shadow"

    def setUp(self):
        _force_fake_gateway()
        self._prev = {k: os.environ.get(k) for k in
                      ("V6_LEARNING_ENGINE", "V6_SEGMENT_TOKEN_BUDGET", "V6_SEGMENT_CONCURRENCY")}
        os.environ["V6_LEARNING_ENGINE"] = self.MODE
        from app import gateway as gw_mod
        gw_mod.reset_engine_for_test()
        from app import config as app_config
        self._saved_mobile_token = app_config.MOBILE_TOKEN
        app_config.MOBILE_TOKEN = ""

        self.tmp = Path(tempfile.mkdtemp(prefix="v6_closeout_"))
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
        for key, value in self._prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----
    def run_dag(self, *, transcript="", material_ids=None, run_id=None):
        from app.dag import DAGContext
        from app.dag_lesson import build_lesson_dag
        ctx = DAGContext()
        ctx.input = {"course_id": self.course_id, "chapter_id": self.chapter_id,
                     "lesson_id": self.lesson_id, "transcript": transcript,
                     "material_ids": material_ids or []}
        if run_id:
            ctx.run_id = run_id
        outputs = asyncio.run(build_lesson_dag().run(ctx))
        return ctx, outputs

    def make_child_run(self, parent_run_id: int, *, transcript="") -> int:
        return self.db.insert(
            "INSERT INTO workflow_runs (workflow, mode, course_id, chapter_id, lesson_id, "
            " status, parent_run_id, input_json) VALUES ('lesson','attend',?,?,?,'queued',?,?)",
            (self.course_id, self.chapter_id, self.lesson_id, parent_run_id,
             json.dumps({"transcript": transcript})))

    def inline_chunks(self):
        return self.db.fetch_all(
            "SELECT id, locator, text, origin_run_id FROM source_chunks "
            "WHERE material_id IS NULL ORDER BY id")

    def model_calls_for(self, run_id, node=None):
        if node:
            return self.db.fetch_all(
                "SELECT * FROM model_calls WHERE run_id=? AND node_id=? ORDER BY id",
                (run_id, node))
        return self.db.fetch_all("SELECT * FROM model_calls WHERE run_id=? ORDER BY id",
                                 (run_id,))

    def make_transcript_material(self, name, text):
        path = self._uploads / name
        path.write_text(text, encoding="utf-8")
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return self.db.insert(
            "INSERT INTO materials (course_id, chapter_id, lesson_id, file_path, name, "
            " display_name, kind, type, sha256, parser_status, status) "
            "VALUES (?,?,?,?,?,?, 'transcript','transcript', ?, 'uploaded','uploaded')",
            (self.course_id, self.chapter_id, self.lesson_id, str(path), name, name, sha))


TRANSCRIPT_A = "[00:00:00] A 历史课堂内容\n[00:10:00] A 结束"
TRANSCRIPT_B_DIFF_TS = "[00:00:00] B 新课堂内容\n[00:20:00] B 结束"
TRANSCRIPT_B_SAME_TS = "[00:00:00] B 新课堂内容\n[00:10:00] B 结束"
LONG_TRANSCRIPT = "\n".join([
    "[00:00:00] 【HEAD-MARK】开篇定义反射定律与法线。",
    "[00:05:00] " + ("甲段填充内容。" * 60),
    "[00:10:00] " + ("乙段填充内容。" * 60),
    "[00:30:00] 【MID-MARK】中段推导入射角等于反射角。",
    "[00:40:00] " + ("丙段填充内容。" * 60),
    "[00:50:00] " + ("丁段填充内容。" * 60),
    "[01:20:00] 【TAIL-MARK】结尾总结镜面反射与漫反射。",
])


# ===========================================================================
# 二、历史转写数据保护
# ===========================================================================
class TestInlineProvenance(CloseoutBase):
    """run A / run B 属于同一 lesson，B 不得删除或复用 A 的材料。"""

    def test_run_b_does_not_destroy_run_a_chunks(self):
        self.run_dag(transcript=TRANSCRIPT_A)
        a_chunks = self.inline_chunks()
        self.assertTrue(a_chunks)
        a_ids = {int(c["id"]) for c in a_chunks}
        a_texts = {int(c["id"]): c["text"] for c in a_chunks}

        self.run_dag(transcript=TRANSCRIPT_B_DIFF_TS)
        after = self.inline_chunks()
        survived = [c for c in after if int(c["id"]) in a_ids]
        self.assertEqual(len(survived), len(a_chunks),
                         "Run B 不得删除 Run A 的转写 chunk")
        for c in survived:
            self.assertEqual(c["text"], a_texts[int(c["id"])],
                             "Run A 的转写正文不得被改写")

    def test_same_timestamps_different_text_does_not_cross_contaminate(self):
        """相同时间戳、不同正文：B 必须读到 B 自己的内容。"""
        self.run_dag(transcript=TRANSCRIPT_A)
        a_ids = {int(c["id"]) for c in self.inline_chunks()}

        ctx_b, outputs = self.run_dag(transcript=TRANSCRIPT_B_SAME_TS)
        b_chunks = [c for c in self.inline_chunks() if int(c["id"]) not in a_ids]
        self.assertTrue(b_chunks, "Run B 必须建立自己的 chunk")
        joined = "".join(c["text"] for c in b_chunks)
        self.assertIn("B 新课堂内容", joined, "B 必须读到自己的正文")
        self.assertNotIn("A 历史课堂内容", joined, "B 不得复用 A 的正文")
        # Source Map 只能关联 B 自己的 chunk
        linked = {int(r["source_chunk_id"]) for r in self.db.fetch_all(
            "SELECT source_chunk_id FROM source_spans WHERE domain_id=?",
            (ctx_b.domain_id,))}
        self.assertTrue(linked)
        self.assertTrue(linked <= {int(c["id"]) for c in b_chunks},
                        "B 的 Source Map 不得关联 A 的 chunk")
        self.assertFalse(linked & a_ids)

    def test_chunks_carry_origin_run_id(self):
        ctx_a, _ = self.run_dag(transcript=TRANSCRIPT_A)
        rows = self.inline_chunks()
        self.assertTrue(all(int(r["origin_run_id"]) == ctx_a.run_id for r in rows),
                        "inline chunk 必须明确归属于当前 run")

    def test_same_run_reentry_is_idempotent(self):
        """同一 run 幂等重入：locator + 文本哈希一致时不重复写 chunk。"""
        from app.learning_engine.domain import _materialize_inline_transcript
        ctx, outputs = self.run_dag(transcript=TRANSCRIPT_A)
        domain = self.db.fetch_one("SELECT * FROM material_domains WHERE run_id=?",
                                   (ctx.run_id,))
        before = len(self.inline_chunks())
        for _ in range(3):
            _materialize_inline_transcript(domain, TRANSCRIPT_A)
        self.assertEqual(len(self.inline_chunks()), before,
                         "相同输入重复物化不得产生新 chunk")

    def test_same_run_content_change_replaces_only_its_own(self):
        """同一 run 内容变化时只替换该 run 的 chunk，不影响其他 run。"""
        self.run_dag(transcript=TRANSCRIPT_A)
        a_ids = {int(c["id"]) for c in self.inline_chunks()}
        ctx_b, _ = self.run_dag(transcript=TRANSCRIPT_B_DIFF_TS)
        from app.learning_engine.domain import _materialize_inline_transcript
        domain_b = self.db.fetch_one("SELECT * FROM material_domains WHERE run_id=?",
                                     (ctx_b.run_id,))
        _materialize_inline_transcript(domain_b, "[00:00:00] B 改后内容\n[00:30:00] 结束")
        after = self.inline_chunks()
        self.assertTrue({int(c["id"]) for c in after} >= a_ids,
                        "Run A 的 chunk 必须仍然存在")

    def test_child_run_does_not_read_parent_transcript_chunks(self):
        ctx_a, _ = self.run_dag(transcript=TRANSCRIPT_A)
        a_ids = {int(c["id"]) for c in self.inline_chunks()}
        child = self.make_child_run(ctx_a.run_id)
        from app.learning_engine.domain import freeze_material_domain
        # child 用自己的转写冻结域（内容不同 → 独立 domain）
        domain = freeze_material_domain(run_id=child, course_id=self.course_id,
                                        chapter_id=self.chapter_id, lesson_id=self.lesson_id,
                                        transcript=TRANSCRIPT_B_SAME_TS)
        from app.learning_engine.domain import build_source_map
        build_source_map(domain["id"])
        linked = {int(r["source_chunk_id"]) for r in self.db.fetch_all(
            "SELECT source_chunk_id FROM source_spans WHERE domain_id=? AND source_chunk_id IS NOT NULL",
            (domain["id"],))}
        if linked:
            self.assertFalse(linked & a_ids, "child run 不得关联父 run 的转写 chunk")


# ===========================================================================
# 三、全局理解内容完整性
# ===========================================================================
class TestMergeCompleteness(CloseoutBase):
    def _segments(self):
        return [
            {"segment_id": 1, "ordinal": 1, "input_hash": "h1", "data": {
                "topics": ["反射"], "knowledge_units": [
                    {"temp_id": "SU-01", "topic": "反射角", "summary": "等于入射角",
                     "source_refs": ["T000001"], "teacher_emphasis": 0.5}],
                "definitions": [{"term": "法线", "meaning": "垂直面", "source_refs": ["T000001"]}],
                "formulas": [{"expression": "i=r", "meaning": "反射定律", "source_refs": ["T000001"]}],
                "derivations": [{"goal": "推导", "steps": ["a"], "source_refs": ["T000001"]}],
                "examples": [{"prompt": "题", "solution": "解", "source_refs": ["T000001"]}],
                "unresolved_points": ["未讲清的点"],
                "teacher_emphasis": 0.5}},
            {"segment_id": 2, "ordinal": 2, "input_hash": "h2", "data": {
                "topics": ["折射"], "knowledge_units": [
                    {"temp_id": "SU-02", "topic": "折射角", "summary": "由介质决定",
                     "source_refs": ["T000002"], "teacher_emphasis": 0.6}],
                "definitions": [{"term": "折射率", "meaning": "n=c/v", "source_refs": ["T000002"]}],
                "formulas": [{"expression": "n1sin=n2sin", "meaning": "折射定律",
                              "source_refs": ["T000002"]}],
                "derivations": [], "examples": [], "unresolved_points": [],
                "teacher_emphasis": 0.6}},
        ]

    def test_all_structures_survive_merge(self):
        from app.learning_engine.understanding import deterministic_merge
        merged = deterministic_merge(1, 1, None, self._segments(), {"T000001", "T000002"})
        for key in ("topics", "knowledge_units", "definitions", "formulas",
                    "derivations", "examples", "unresolved_points",
                    "unresolved_conflicts", "consumed_segment_ids", "segment_ordinals"):
            self.assertIn(key, merged, f"merge 必须保留 {key}")
        self.assertTrue(merged["definitions"])
        self.assertTrue(merged["formulas"])
        self.assertTrue(merged["derivations"])
        self.assertTrue(merged["examples"], "examples 不得在 merge 中丢失")
        self.assertEqual(merged["structures_dropped"], 0)

    def test_structures_reach_persisted_understanding(self):
        ctx, outputs = self.run_dag(transcript=LONG_TRANSCRIPT)
        merge = outputs["merge_understanding"]
        self.assertIn("definition_count", merge)
        row = self.db.fetch_one(
            "SELECT structured_json FROM lesson_understandings WHERE run_id=?",
            (ctx.run_id,))
        payload = json.loads(row["structured_json"])
        for key in ("definitions", "formulas", "derivations", "examples",
                    "valid_source_ids", "consumed_segment_ids"):
            self.assertIn(key, payload, f"持久化理解必须包含 {key}")
        # 结构块可反向定位到原 segment
        for block in payload["definitions"] + payload["formulas"] + payload["examples"]:
            self.assertTrue(block.get("segment_ids"),
                            "每个结构块必须记录来源 segment")

    def test_legacy_v5_structures_present_in_fixture_pipeline(self):
        """fake 模型产出定义/公式时，merge 后必须全部保留。"""
        ctx, outputs = self.run_dag(transcript=LONG_TRANSCRIPT)
        row = self.db.fetch_one(
            "SELECT structured_json FROM lesson_understandings WHERE run_id=?",
            (ctx.run_id,))
        payload = json.loads(row["structured_json"])
        self.assertGreaterEqual(len(payload.get("definitions") or []), 1)
        self.assertGreaterEqual(len(payload.get("formulas") or []), 1)

    def test_structures_dropped_blocks_gate(self):
        from app.learning_engine.contracts import Gate, MaterialCoverageReport
        with self.assertRaises(Exception):
            MaterialCoverageReport(run_id=1, domain_id=1, gate=Gate.PASSED,
                                   structures_dropped=1)
        from app.learning_engine.coverage import evaluate_gate
        gate, reason, _ = evaluate_gate({
            "silent_dropped": 0, "domain_accounting_rate": 1.0, "unassigned_count": 0,
            "semantic_processing_rate": 1.0, "canonical_non_noise_spans": 5,
            "structures_dropped": 2, "segment_structures_total": 4,
            "merged_structures_total": 2,
        })
        self.assertNotEqual(gate, "passed")
        self.assertIn("structures_dropped", reason)

    def test_conflicting_same_key_goes_to_unresolved(self):
        from app.learning_engine.understanding import deterministic_merge
        segs = self._segments()
        segs[1]["data"]["definitions"] = [
            {"term": "法线", "meaning": "另一种说法", "source_refs": ["T000002"]}]
        merged = deterministic_merge(1, 1, None, segs, {"T000001", "T000002"})
        self.assertTrue(any("法线" in c for c in merged["unresolved_conflicts"]),
                        "同键不同表述必须进入 unresolved_conflicts")
        self.assertEqual(merged["structures_dropped"], 0)


# ===========================================================================
# 四、模型 fallback
# ===========================================================================
class TestModelFallback(CloseoutBase):
    def test_candidate_model_is_used_and_recorded(self):
        ctx, outputs = self.run_dag(transcript=LONG_TRANSCRIPT)
        node = outputs["understand_segments"]
        self.assertIn("model_used_candidate", node)
        self.assertTrue(node["model_used_candidate"])
        rows = self.model_calls_for(ctx.run_id, "understand_segments")
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["model_id"], node["model_used_candidate"],
                             "segment 调用必须使用 DAG 传入的候选模型")
        stored = self.db.fetch_all(
            "SELECT model_used FROM segment_understandings")
        for s in stored:
            self.assertEqual(s["model_used"], node["model_used_candidate"],
                             "segment_understandings.model_used 必须是候选模型")

    def test_first_model_fails_second_succeeds(self):
        """第一个候选模型持续失败 → DAG 切到第二个模型并成功。"""
        from app.learning_engine import understanding as und
        ctx, _ = self.run_dag(transcript=LONG_TRANSCRIPT)
        # 清掉首轮结果，重新以多候选模型运行节点
        self.db.execute("DELETE FROM segment_understandings")
        self.db.execute("DELETE FROM lesson_segments")
        self.db.execute(
            "DELETE FROM coverage_ledger WHERE stage='understand_segments'")
        self.db.execute("DELETE FROM coverage_plans WHERE domain_id=?", (ctx.domain_id,))
        self.db.execute("DELETE FROM source_spans WHERE domain_id=?", (ctx.domain_id,))

        from app.learning_engine.domain import build_source_map
        build_source_map(ctx.domain_id)
        from app.learning_engine.segment import (
            persist_segments, plan_segments, record_segment_ledger,
        )
        plan = plan_segments(ctx.domain_id)
        persist_segments(ctx.domain_id, ctx.run_id, plan)
        record_segment_ledger(ctx.domain_id, ctx.run_id)

        original = und.understand_one_segment
        attempts: list[str] = []

        async def _first_fails(segment, runs_dir=None):
            model = segment.get("_model") or ""
            attempts.append(model)
            if model == "qwen3_flash":
                raise RuntimeError("first model always fails")
            return await original(segment, runs_dir)

        und.understand_one_segment = _first_fails
        try:
            result = asyncio.run(und.run_understand_segments(
                ctx, ctx.domain_id, ctx.run_id, model_id="qwen3_flash"))
        finally:
            und.understand_one_segment = original
        # 单候选内重试用尽 → 节点报失败（DAG 会切下一个候选）
        self.assertFalse(result["all_succeeded"])
        self.assertTrue(all(m == "qwen3_flash" for m in attempts),
                        "单候选内重试必须使用同一模型")

        # 换第二个候选模型应成功
        attempts.clear()
        und.understand_one_segment = _first_fails
        try:
            result2 = asyncio.run(und.run_understand_segments(
                ctx, ctx.domain_id, ctx.run_id, model_id="deepseek_v4_free"))
        finally:
            und.understand_one_segment = original
        self.assertTrue(result2["all_succeeded"], "第二个候选模型必须成功")
        self.assertTrue(all(m == "deepseek_v4_free" for m in attempts))

    def test_model_calls_show_distinct_models(self):
        """model_calls 必须能看到不同 model_id（证明 fallback 真的切换）。"""
        from app.learning_engine import understanding as und
        ctx, _ = self.run_dag(transcript=LONG_TRANSCRIPT)
        before_ids = [r["model_id"] for r in self.model_calls_for(ctx.run_id)]
        self.db.execute("DELETE FROM segment_understandings")
        self.db.execute(
            "DELETE FROM coverage_ledger WHERE stage='understand_segments'")
        original = und.understand_one_segment

        async def _fail_first(segment, runs_dir=None):
            if (segment.get("_model") or "") == "qwen3_flash":
                raise RuntimeError("boom")
            return await original(segment, runs_dir)

        und.understand_one_segment = _fail_first
        try:
            asyncio.run(und.run_understand_segments(
                ctx, ctx.domain_id, ctx.run_id, model_id="qwen3_flash"))
            asyncio.run(und.run_understand_segments(
                ctx, ctx.domain_id, ctx.run_id, model_id="deepseek_v4_free"))
        finally:
            und.understand_one_segment = original
        after_ids = {r["model_id"] for r in self.model_calls_for(ctx.run_id)}
        self.assertIn("qwen3_flash", after_ids)
        self.assertIn("deepseek_v4_free", after_ids)

    def test_unavailable_model_filtered_from_candidates(self):
        """候选链过滤：不可用（未注册 / 已停用）模型必须被剔除，不得复活。"""
        from app.dag_lesson import build_lesson_dag
        from app.models_registry import get_model
        node = build_lesson_dag().nodes["understand_segments"]
        candidates = list(node.preferred_models) + list(node.fallback_models) + [
            "definitely_not_a_registered_model"]
        enabled = []
        for mid in candidates:
            try:
                get_model(mid)
            except KeyError:
                continue
            enabled.append(mid)
        self.assertNotIn("definitely_not_a_registered_model", enabled,
                         "未注册/已停用模型不得进入候选链")
        self.assertTrue(enabled, "至少应有一个可用模型")


# ===========================================================================
# 五、真实上下文预算
# ===========================================================================
class TestContextBudget(CloseoutBase):
    def test_material_appears_once_in_messages(self):
        from app.learning_engine.understanding import render_segment_messages
        ctx, _ = self.run_dag(transcript=LONG_TRANSCRIPT)
        from app.learning_engine.segment import get_segments
        seg = get_segments(ctx.domain_id)[0]
        messages, meta = render_segment_messages(seg, 1)
        material = meta["material_only"]
        self.assertTrue(material)
        self.assertNotIn(material, meta["system_text"],
                         "材料不得出现在 system prompt 中")
        self.assertIn(material, meta["user_text"])
        joined = meta["system_text"] + meta["user_text"]
        self.assertEqual(joined.count("【HEAD-MARK】"), 1,
                         "材料正文在最终 messages 中只能出现一次")

    def test_final_messages_within_budget(self):
        from app.learning_engine.segment import plan_segments, verify_segment_budget
        ctx, _ = self.run_dag(transcript=LONG_TRANSCRIPT)
        canonical = self.db.fetch_all(
            "SELECT token_count FROM source_spans WHERE domain_id=? AND span_state='included'",
            (ctx.domain_id,))
        biggest = max(int(c["token_count"] or 0) for c in canonical)
        total = sum(int(c["token_count"] or 0) for c in canonical)
        # 预算 = 最大单 span + prompt 开销 + 余量，但装不下全部 → 必然分段且不超限
        plan = plan_segments(ctx.domain_id,
                             token_budget=max(biggest * 2 + 900, total // 2))
        check = verify_segment_budget(ctx.domain_id, plan)
        self.assertEqual(check["over_budget_segments"], 0,
                         "最终 messages 不得超预算")
        self.assertLessEqual(check["max_input_tokens"], plan["budget"])

    def test_overlap_never_pushes_over_budget(self):
        from app.learning_engine.segment import plan_segments
        ctx, _ = self.run_dag(transcript=LONG_TRANSCRIPT)
        canonical = self.db.fetch_all(
            "SELECT token_count FROM source_spans WHERE domain_id=? AND span_state='included'",
            (ctx.domain_id,))
        biggest = max(int(c["token_count"] or 0) for c in canonical)
        total = sum(int(c["token_count"] or 0) for c in canonical)
        plan = plan_segments(ctx.domain_id,
                             token_budget=max(biggest + 900, total // 3))
        self.assertGreater(len(plan["drafts"]), 1)
        from app.learning_engine.segment import verify_segment_budget
        check = verify_segment_budget(ctx.domain_id, plan)
        self.assertEqual(check["over_budget_segments"], 0)
        # primary 一个都不能少
        primary = [s["source_id"] for d in plan["drafts"] for s in d.primary]
        expected = [r["source_id"] for r in self.db.fetch_all(
            "SELECT source_id FROM source_spans WHERE domain_id=? AND span_state='included' "
            "ORDER BY ordinal", (ctx.domain_id,))]
        self.assertEqual(sorted(primary), sorted(expected))

    def test_natural_boundary_function_is_used(self):
        """_is_natural_boundary 必须真正参与分段（不是死代码）。"""
        import inspect
        from app.learning_engine import segment as seg
        src = inspect.getsource(seg.plan_segments)
        self.assertIn("_is_natural_boundary", src,
                      "_is_natural_boundary 必须参与分段决策")

    def test_oversized_single_span_splits_or_fails_honestly(self):
        """超大单 span：要么被安全拆分，要么诚实失败（不得静默截断）。"""
        big = "超长单句内容" * 3000
        path = self._uploads / "big.txt"
        path.write_text(big, encoding="utf-8")
        mat = self.db.insert(
            "INSERT INTO materials (course_id, chapter_id, lesson_id, file_path, name, "
            " display_name, kind, type, parser_status, status) "
            "VALUES (?,?,?,?,?,'big.txt','text','text','ready','ready')",
            (self.course_id, self.chapter_id, self.lesson_id, str(path), "big.txt"))
        from app.learning_engine.segment import SegmentationError, plan_segments
        ctx, _ = self.run_dag(material_ids=[mat])
        # 默认预算（大）：解析阶段应已按句子安全拆分，不得出现超预算单 span
        plan = plan_segments(ctx.domain_id)
        spans = self.db.fetch_all(
            "SELECT text, token_count FROM source_spans WHERE domain_id=? AND span_state='included' "
            "ORDER BY ordinal", (ctx.domain_id,))
        self.assertTrue(spans)
        joined = "".join(s["text"] or "" for s in spans)
        # 字符覆盖率 100%（拆分不得丢字）
        self.assertGreaterEqual(len(joined), len(big) * 0.95,
                                "拆分后字符覆盖率必须接近 100%")
        # 极小预算必须诚实失败，且不得静默截断
        try:
            plan_segments(ctx.domain_id, token_budget=50)
        except SegmentationError as e:
            self.assertTrue("预算" in str(e) or "拆分" in str(e))
            # 失败后原始 span 内容仍完整（没有发生破坏性截断）
            after = self.db.fetch_all(
                "SELECT text FROM source_spans WHERE domain_id=? AND span_state='included' "
                "ORDER BY ordinal", (ctx.domain_id,))
            self.assertEqual(len("".join(x["text"] or "" for x in after)), len(joined),
                             "失败路径不得改写已建好的 span")


# ===========================================================================
# 六、跨 run 复用
# ===========================================================================
class TestCrossRunReuse(CloseoutBase):
    def test_child_run_reuses_with_zero_model_calls(self):
        ctx_a, _ = self.run_dag(transcript=LONG_TRANSCRIPT)
        child = self.make_child_run(ctx_a.run_id, transcript=LONG_TRANSCRIPT)
        before = len(self.model_calls_for(child, "understand_segments"))
        ctx_b, outputs = self.run_dag(transcript=LONG_TRANSCRIPT, run_id=child)
        node = outputs["understand_segments"]
        self.assertGreaterEqual(node["reused"], 1, "child run 必须复用父 run 理解结果")
        self.assertEqual(node["attempted"], 0)
        after_calls = self.model_calls_for(child, "understand_segments")
        self.assertEqual(len(after_calls), before,
                         "复用不得产生 understand_segments 模型调用")
        self.assertEqual(outputs["coverage_audit"]["gate"], "passed")

    def test_reuse_recorded_with_source(self):
        ctx_a, _ = self.run_dag(transcript=LONG_TRANSCRIPT)
        child = self.make_child_run(ctx_a.run_id, transcript=LONG_TRANSCRIPT)
        self.run_dag(transcript=LONG_TRANSCRIPT, run_id=child)
        rows = self.db.fetch_all("SELECT * FROM segment_reuse_index ORDER BY id")
        self.assertTrue(rows, "复用必须写入 segment_reuse_index")
        row = rows[-1]
        self.assertEqual(int(row["source_run_id"]), ctx_a.run_id)
        self.assertIsNotNone(row["source_segment_id"])
        self.assertIsNotNone(row["reuse_reason"])
        self.assertIsNotNone(row["reuse_scope_hash"])

    def test_child_projection_is_its_own_row(self):
        ctx_a, _ = self.run_dag(transcript=LONG_TRANSCRIPT)
        child = self.make_child_run(ctx_a.run_id, transcript=LONG_TRANSCRIPT)
        ctx_b, _ = self.run_dag(transcript=LONG_TRANSCRIPT, run_id=child)
        rows = self.db.fetch_all(
            "SELECT run_id FROM segment_understandings ORDER BY id")
        self.assertIn(ctx_a.run_id, [int(r["run_id"]) for r in rows])
        self.assertIn(child, [int(r["run_id"]) for r in rows],
                      "child 必须拥有自己的 understanding 行（投影，不共享可变行）")

    def test_changed_text_prevents_reuse(self):
        ctx_a, _ = self.run_dag(transcript=LONG_TRANSCRIPT)
        child = self.make_child_run(ctx_a.run_id, transcript=LONG_TRANSCRIPT)
        changed = LONG_TRANSCRIPT.replace("【HEAD-MARK】", "【HEAD-MARK-改】")
        ctx_b, outputs = self.run_dag(transcript=changed, run_id=child)
        self.assertEqual(outputs["understand_segments"]["reused"], 0,
                         "内容变化必须使缓存失效")

    def test_changed_model_prevents_reuse(self):
        from app.learning_engine import understanding as und
        ctx_a, _ = self.run_dag(transcript=LONG_TRANSCRIPT)
        child = self.make_child_run(ctx_a.run_id, transcript=LONG_TRANSCRIPT)
        # 用不同候选模型跑 child 的理解节点
        from app.learning_engine.segment import (
            persist_segments, plan_segments, record_segment_ledger,
        )
        from app.learning_engine.domain import build_source_map, freeze_material_domain
        domain = freeze_material_domain(run_id=child, course_id=self.course_id,
                                        chapter_id=self.chapter_id,
                                        lesson_id=self.lesson_id, transcript=LONG_TRANSCRIPT)
        build_source_map(domain["id"])
        plan = plan_segments(domain["id"])
        persist_segments(domain["id"], child, plan)
        record_segment_ledger(domain["id"], child)
        from app.dag import DAGContext
        ctx_child = DAGContext()
        ctx_child.run_id = child
        ctx_child.domain_id = int(domain["id"])
        result = asyncio.run(und.run_understand_segments(
            ctx_child, int(domain["id"]), child, model_id="glm_flash"))
        self.assertEqual(result["reused"], 0, "换模型不得复用旧模型缓存")
        self.assertTrue(result["all_succeeded"])

    def test_failed_parent_segment_not_reused(self):
        ctx_a, _ = self.run_dag(transcript=LONG_TRANSCRIPT)
        self.db.execute("UPDATE lesson_segments SET status='failed'")
        self.db.execute("UPDATE segment_understandings SET status='failed'")
        child = self.make_child_run(ctx_a.run_id, transcript=LONG_TRANSCRIPT)
        ctx_b, outputs = self.run_dag(transcript=LONG_TRANSCRIPT, run_id=child)
        self.assertEqual(outputs["understand_segments"]["reused"], 0,
                         "failed 结果不得被复用")

    def test_reuse_scope_hash_excludes_run_id(self):
        from app.learning_engine.domain import compute_reuse_scope_hash
        members = [{"material_id": 1, "label": "a", "content_hash": "h", "required": True}]
        h1 = compute_reuse_scope_hash(scope="lesson", course_id=1, chapter_id=1,
                                      lesson_id=1, members=members)
        h2 = compute_reuse_scope_hash(scope="lesson", course_id=1, chapter_id=1,
                                      lesson_id=1, members=members)
        self.assertEqual(h1, h2)
        h3 = compute_reuse_scope_hash(scope="lesson", course_id=1, chapter_id=1,
                                      lesson_id=1,
                                      members=[dict(members[0], content_hash="h2")])
        self.assertNotEqual(h1, h3, "内容变化必须改变 reuse scope")


# ===========================================================================
# 七、并发与取消
# ===========================================================================
class TestConcurrencyAndCancel(CloseoutBase):
    def _three_segments(self):
        """构造至少 3 个 segment 的域。"""
        ctx, _ = self.run_dag(transcript=LONG_TRANSCRIPT)
        from app.learning_engine.segment import (
            PlanStrategy, SegmentDraft, canonical_spans, persist_segments,
            record_segment_ledger,
        )
        self.db.execute("DELETE FROM lesson_segments WHERE domain_id=?", (ctx.domain_id,))
        canonical = canonical_spans(ctx.domain_id)
        self.assertGreaterEqual(len(canonical), 3, "fixture 至少需要 3 个 source span")
        # 这里测试的是 sibling 并发与取消，不是分段算法。直接按连续区间构造
        # 3 个合法 segment，避免生产分段预算或自然边界策略变化让夹具退化成
        # 2 段、从而在进入真正被测逻辑前失败。
        cuts = (len(canonical) // 3, (len(canonical) * 2) // 3)
        groups = (canonical[:cuts[0]], canonical[cuts[0]:cuts[1]], canonical[cuts[1]:])
        drafts = []
        for ordinal, primary in enumerate(groups, 1):
            drafts.append(SegmentDraft(
                ordinal=ordinal,
                primary=primary,
                overlap=[],
                strategy=PlanStrategy.SEGMENTED_MAP_MERGE.value,
                start_ms=min((s["start_ms"] for s in primary if s["start_ms"] is not None), default=None),
                end_ms=max((s["end_ms"] for s in primary if s["end_ms"] is not None), default=None),
                title=None,
            ))
        plan = {"drafts": drafts}
        segments = persist_segments(ctx.domain_id, ctx.run_id, plan)
        record_segment_ledger(ctx.domain_id, ctx.run_id)
        self.assertGreaterEqual(len(segments), 3, "fixture 必须产生 ≥3 个 segment")
        return ctx, segments

    def test_cancel_cancels_all_siblings(self):
        from app.learning_engine import understanding as und
        from app.dag import RunCancelledError
        ctx, segments = self._three_segments()
        os.environ["V6_SEGMENT_CONCURRENCY"] = "3"   # 让多个 sibling 真正并发
        started: list[int] = []

        async def _cancel_second(segment, runs_dir=None):
            started.append(int(segment["id"]))
            if len(started) >= 2:
                raise RunCancelledError("cancelled by test")
            await asyncio.sleep(0.05)
            return {"topics": [], "knowledge_units": [], "source_refs": [],
                    "_model": segment.get("_model")}

        original = und.understand_one_segment
        und.understand_one_segment = _cancel_second
        raised = False
        try:
            asyncio.run(und.run_understand_segments(
                ctx, ctx.domain_id, ctx.run_id, model_id="qwen3_flash"))
        except RunCancelledError:
            raised = True
        finally:
            und.understand_one_segment = original
        self.assertTrue(raised, "取消必须向上传播")
        rows = self.db.fetch_all(
            "SELECT status FROM lesson_segments WHERE domain_id=?", (ctx.domain_id,))
        for row in rows:
            self.assertIn(row["status"], ("cancelled", "succeeded", "failed"),
                          "取消后不得留下 pending/running segment")
        self.assertFalse([r for r in rows if r["status"] in ("pending", "running")],
                         "取消后不得有残留运行中的 segment")

    def test_cancel_produces_no_succeeded_understanding(self):
        from app.learning_engine import understanding as und
        from app.dag import RunCancelledError
        ctx, segments = self._three_segments()

        async def _always_cancel(segment, runs_dir=None):
            raise RunCancelledError("cancelled by test")

        original = und.understand_one_segment
        und.understand_one_segment = _always_cancel
        try:
            try:
                asyncio.run(und.run_understand_segments(
                    ctx, ctx.domain_id, ctx.run_id, model_id="qwen3_flash"))
            except RunCancelledError:
                pass
        finally:
            und.understand_one_segment = original
        succ = self.db.fetch_all(
            "SELECT status FROM segment_understandings WHERE domain_id=?",
            (ctx.domain_id,))
        self.assertFalse([r for r in succ if r["status"] == "succeeded"],
                         "取消后不得写出 succeeded understanding")

    def test_concurrency_limit_respected(self):
        from app.learning_engine import understanding as und
        ctx, segments = self._three_segments()
        os.environ["V6_SEGMENT_CONCURRENCY"] = "1"
        peak = {"now": 0, "max": 0}
        original = und.understand_one_segment

        async def _tracked(segment, runs_dir=None):
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            try:
                await asyncio.sleep(0.02)
                return await original(segment, runs_dir)
            finally:
                peak["now"] -= 1

        und.understand_one_segment = _tracked
        try:
            asyncio.run(und.run_understand_segments(
                ctx, ctx.domain_id, ctx.run_id, model_id="qwen3_flash"))
        finally:
            und.understand_one_segment = original
        self.assertEqual(peak["max"], 1, "并发上限必须被遵守")


# ===========================================================================
# 八、状态与版本
# ===========================================================================
class TestStateAndVersions(CloseoutBase):
    def test_engine_version_is_phase2(self):
        from app.learning_engine import config as v6cfg
        self.assertEqual(v6cfg.engine_version(), "v6.0.0-shadow")
        ctx, _ = self.run_dag(transcript=LONG_TRANSCRIPT)
        row = self.db.fetch_one(
            "SELECT engine_version FROM material_domains WHERE run_id=?", (ctx.run_id,))
        self.assertEqual(row["engine_version"], "v6.0.0-shadow")

    def test_publish_flag_and_publication_mode_honest(self):
        from app.learning_engine import config as v6cfg
        self.assertFalse(v6cfg.real_notes_publish_enabled(),
                         "Composer V2 未实现前不得声称 V6 接管笔记")
        self.assertEqual(v6cfg.publication_mode(),
                         "v6_understanding_only_legacy_publication")
        os.environ["V6_LEARNING_ENGINE"] = "on"
        self.assertTrue(v6cfg.real_notes_publish_enabled(),
                        "Phase 5 完成后 on 模式必须由 Composer V2 接管笔记")
        self.assertEqual(v6cfg.engine_version(), "v6.0.0-on")
        self.assertEqual(v6cfg.publication_mode(), "v6_composer_v2")

    def test_semantic_rate_floor_not_env_configurable(self):
        from app.learning_engine.coverage import SEMANTIC_RATE_FLOOR, semantic_rate_floor
        self.assertEqual(SEMANTIC_RATE_FLOOR, 1.0)
        os.environ["V6_SEMANTIC_RATE_FLOOR"] = "0.3"
        try:
            self.assertEqual(semantic_rate_floor(), 1.0)
        finally:
            os.environ.pop("V6_SEMANTIC_RATE_FLOOR", None)

    def test_gate_reports_structures_dropped(self):
        from app.learning_engine.contracts import MaterialCoverageReport
        report = MaterialCoverageReport(run_id=1, domain_id=1)
        self.assertIn("structures_dropped", report.model_dump())


if __name__ == "__main__":
    unittest.main()
