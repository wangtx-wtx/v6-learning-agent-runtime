"""V6 Phase 2/3 Final Closeout 回归测试（缺陷 1–5 + 复用版本校验）。

覆盖（每条都对应一个已复现的真实缺陷）:

1. Run A → Run B 同课时同时间戳不同正文：Run B 必须使用**自己的**内容，旧链候选
   不得包含 Run A 的正文，且禁止 lesson 级全局 RAG。
2. 文件材料 + inline 转写混合时，候选仍然只来自当前 domain。
3. 两个 domain 共用 ``T000001`` 且正文不同：Domain A 的正文不得出现在 Domain B 的
   认知 prompt；持久化的 ``source_span_id`` 必须属于 ``cognitive_map.domain_id``。
4. 三个 batch、第二个失败：processed/coverage 必须为 2/3，账本序号为 1/2/3 且
   失败 batch 记 ``failed``；节点必须抛 ``RetryableModelError``（DAG 才能换模型）。
5. 首个候选模型失败 → DAG 真实切换到下一个候选（``model_calls`` 可见）。
6. 全部候选模型失败 → map 与账本都为 failed，run 不得 completed。
7. 认知输入预算：按上下文窗口派生 + 最终 messages 复算，超预算必须失败。
8. 同 run 复用必须比对 prompt/schema 版本。
9. ``cognitive_maps.input_hash`` 由真实输入派生（输入变则变，输出变不变）。
10. mastery 必须按 course/chapter 作用域过滤，绝不做全局读取。

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

#: Run A 的课堂正文（与 Run B 使用完全相同的 3 个时间戳，正文不同）
TEXT_A = "\n".join([
    "[00:00:00] 第一节课：光的反射定律，入射角等于反射角。",
    "[00:05:00] 第一节课：平面镜成像，物像等大正立虚像。",
    "[00:10:00] 第一节课：注意镜面反射与漫反射的区别。",
])
#: Run B 的课堂正文（不同课堂、相同时间戳）
TEXT_B = "\n".join([
    "[00:00:00] 第二节课：凸透镜成像规律，物距大于二倍焦距成倒立缩小实像。",
    "[00:05:00] 第二节课：物距小于焦距时成正立放大虚像。",
    "[00:10:00] 第二节课：容易混淆的是实像与虚像的判别。",
])


def _force_fake_gateway():
    os.environ["V5_GATEWAY_MODE"] = "fake"
    os.environ.pop("V5_GATEWAY_API_KEY", None)
    os.environ.pop("V5_ALLOW_LIVE_MODEL_TESTS", None)


def _budget_patch(cog, value: int):
    """把认知预算固定为 ``value``，保留审计字段形状。"""
    original = cog._cognitive_input_budget

    def patched(model_profile_id=None):
        audit = original(model_profile_id)
        audit = dict(audit)
        audit["input_budget"] = int(value)
        audit["budget_override_for_test"] = True
        return audit

    return patched


class CloseoutBase(unittest.TestCase):
    def setUp(self):
        _force_fake_gateway()
        self._prev = os.environ.get("V6_LEARNING_ENGINE")
        os.environ["V6_LEARNING_ENGINE"] = "shadow"
        from app import gateway as gw_mod
        gw_mod.reset_engine_for_test()
        from app import config as app_config
        self._saved_mobile_token = app_config.MOBILE_TOKEN
        app_config.MOBILE_TOKEN = ""

        self.tmp = Path(tempfile.mkdtemp(prefix="v6_fc_"))
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
            "VALUES (?, ?, 'L01', '光学')", (self.chapter_id, self.course_id))
        self._uploads = Path(app_config.UPLOAD_DIR)
        self._uploads.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        from app import database as db
        db.reset_connections()
        from app import gateway as gw_mod
        gw_mod.reset_engine_for_test()
        from app import config as app_config
        app_config.MOBILE_TOKEN = self._saved_mobile_token
        if self._prev is None:
            os.environ.pop("V6_LEARNING_ENGINE", None)
        else:
            os.environ["V6_LEARNING_ENGINE"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 基础设施 ----
    def make_material(self, name: str, text: str) -> int:
        path = self._uploads / name
        path.write_text(text, encoding="utf-8")
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return self.db.insert(
            "INSERT INTO materials (course_id, chapter_id, lesson_id, file_path, name, "
            " display_name, kind, type, sha256, parser_status, status) "
            "VALUES (?,?,?,?,?,?, 'transcript','transcript', ?, 'uploaded','uploaded')",
            (self.course_id, self.chapter_id, self.lesson_id, str(path), name, name, sha))

    def run_dag(self, *, transcript="", material_ids=None):
        from app.dag import DAGContext
        from app.dag_lesson import build_lesson_dag
        ctx = DAGContext()
        ctx.input = {"course_id": self.course_id, "chapter_id": self.chapter_id,
                     "lesson_id": self.lesson_id, "transcript": transcript,
                     "material_ids": material_ids or []}
        outputs = asyncio.run(build_lesson_dag().run(ctx))
        return ctx, outputs

    def span_texts(self, domain_id, source_id):
        return {r["text"] for r in self.db.fetch_all(
            "SELECT text FROM source_spans WHERE domain_id=? AND source_id=?",
            (domain_id, source_id))}

    def map_for(self, run_id):
        row = self.db.fetch_one("SELECT * FROM cognitive_maps WHERE run_id=?", (run_id,))
        return dict(row) if row else None

    def ledger_for(self, run_id):
        return [dict(r) for r in self.db.fetch_all(
            "SELECT * FROM cognitive_input_ledger WHERE run_id=? ORDER BY batch_ordinal, id",
            (run_id,))]


class TestLegacyCandidateScope(CloseoutBase):
    """缺陷 1：旧链候选必须限定在当前 Material Domain。"""

    def test_run_b_uses_own_content_not_run_a(self):
        ctx_a, out_a = self.run_dag(transcript=TEXT_A)
        ctx_b, out_b = self.run_dag(transcript=TEXT_B)
        self.assertNotEqual(ctx_a.domain_id, ctx_b.domain_id)

        cands_b = (out_b.get("retrieve_context") or {}).get("candidates") or []
        self.assertTrue(cands_b, "Run B 必须有候选（V6 已把域内内容建成 Source Map）")
        texts_b = {c["text"] for c in cands_b}
        a_texts = {r["text"] for r in self.db.fetch_all(
            "SELECT text FROM source_spans WHERE domain_id=?", (ctx_a.domain_id,))}
        self.assertFalse(texts_b & a_texts, "Run B 的候选不得包含 Run A 的课堂正文")
        joined = "\n".join(texts_b)
        self.assertIn("凸透镜", joined, "Run B 必须真的使用 B 的内容")
        self.assertNotIn("反射定律", joined, "Run B 不得拿到 Run A 的内容")

    def test_global_rag_disabled_in_v6(self):
        ctx, out = self.run_dag(transcript=TEXT_A)
        rc = out["retrieve_context"]
        self.assertFalse(rc.get("global_rag_used"), "V6 不得使用 lesson 级全局 RAG")
        self.assertEqual(rc.get("domain_id"), int(ctx.domain_id))
        # 所有候选都必须能在本 domain 找到对应 span
        source_ids = {r["source_id"] for r in self.db.fetch_all(
            "SELECT source_id FROM source_spans WHERE domain_id=?", (ctx.domain_id,))}
        chunk_ids = {r["id"] for r in self.db.fetch_all(
            "SELECT id FROM source_spans WHERE domain_id=?", (ctx.domain_id,))}
        self.assertTrue(rc["candidates"])
        for cand in rc["candidates"]:
            self.assertTrue(cand.get("source_id") in source_ids or cand.get("chunk_id") in chunk_ids,
                            f"候选 {cand} 不属于当前 domain")

    def test_domain_chunk_accounting_is_explicit(self):
        ctx, out = self.run_dag(transcript=TEXT_A)
        rc = out["retrieve_context"]
        selected = len(rc["candidates"])
        self.assertEqual(rc["domain_chunk_used"], selected)
        self.assertEqual(int(rc["domain_chunk_total"]),
                         selected + len(rc["domain_chunk_not_selected"]),
                         "候选窗口之外的内容必须显式记账，不得静默丢弃")

    def test_retrieve_context_runs_after_source_map(self):
        """旧链候选依赖 Source Map：必须晚于 build_source_map 执行。"""
        from app.dag_lesson import build_lesson_dag
        dag = build_lesson_dag()
        self.assertEqual(dag.nodes["retrieve_context"].depends_on, ["build_source_map"])
        layers = dag.topological_layers()
        idx = {name: i for i, layer in enumerate(layers) for name in layer}
        self.assertGreater(idx["retrieve_context"], idx["build_source_map"])

    def test_off_mode_still_uses_global_rag(self):
        os.environ["V6_LEARNING_ENGINE"] = "off"
        mat = self.make_material("a.txt", TEXT_A)
        ctx, out = self.run_dag(transcript="", material_ids=[mat])
        rc = out["retrieve_context"]
        self.assertTrue(rc.get("global_rag_used"), "V5 路径行为不得改变")
        self.assertNotIn("domain_chunk_total", rc, "V5 路径不得走 V6 作用域逻辑")

    def test_file_and_inline_material_stay_domain_scoped(self):
        mat_a = self.make_material("history.txt", TEXT_A)
        ctx_a, _ = self.run_dag(transcript="", material_ids=[mat_a])
        ctx_b, out_b = self.run_dag(transcript=TEXT_B, material_ids=[])
        cands_b = (out_b.get("retrieve_context") or {}).get("candidates") or []
        self.assertTrue(cands_b)
        a_texts = {r["text"] for r in self.db.fetch_all(
            "SELECT text FROM source_spans WHERE domain_id=?", (ctx_a.domain_id,))}
        for cand in cands_b:
            self.assertNotIn(cand["text"], a_texts, "混合场景下仍然不得串域")


class TestCrossDomainLeak(CloseoutBase):
    """缺陷 2：Source ID 按 domain 分配，认知材料必须按 domain 限定。"""

    def _two_domains(self):
        ctx_a, _ = self.run_dag(transcript=TEXT_A)
        ctx_b, _ = self.run_dag(transcript=TEXT_B)
        # 两个 domain 的首个转写 span 都是 T000001，但正文不同
        self.assertEqual(
            self.db.fetch_one(
                "SELECT COUNT(*) AS n FROM source_spans WHERE source_id='T000001'")["n"], 2)
        return ctx_a, ctx_b

    def test_material_excerpts_rejects_foreign_domain(self):
        from app.learning_engine.cognition import _material_excerpts
        ctx_a, ctx_b = self._two_domains()
        result = _material_excerpts(int(ctx_b.domain_id), ["T000001"])
        self.assertIn("凸透镜", result["text"])
        self.assertNotIn("反射定律", result["text"], "不得泄漏 Domain A 的正文")
        self.assertEqual(result["foreign_source_ids"], [])
        self.assertEqual(result["included_source_ids"], ["T000001"])

    def test_foreign_source_id_is_reported_not_used(self):
        """Domain A 独有的 Source ID 出现在 Domain B 的请求里时必须被剔除并回报。"""
        from app.learning_engine.cognition import _material_excerpts
        # 让 Domain A 拥有比 Domain B 更多的 span，从而存在「只有 A 有」的 Source ID
        longer_a = TEXT_A + "\n[00:15:00] 第一节课：球面镜与平面镜的成像差异。"
        ctx_a, _ = self.run_dag(transcript=longer_a)
        ctx_b, _ = self.run_dag(transcript=TEXT_B)
        domain_a_sources = {r["source_id"] for r in self.db.fetch_all(
            "SELECT source_id FROM source_spans WHERE domain_id=?", (ctx_a.domain_id,))}
        domain_b_sources = {r["source_id"] for r in self.db.fetch_all(
            "SELECT source_id FROM source_spans WHERE domain_id=?", (ctx_b.domain_id,))}
        only_a = sorted(domain_a_sources - domain_b_sources)
        self.assertTrue(only_a, "构造失败：Domain A 应有 B 没有的 Source ID")

        # 把 A 独有的 ID 混进 B 的请求：必须全部被剔除并回报，且不带入 A 的正文
        request = only_a + sorted(domain_b_sources)
        result = _material_excerpts(int(ctx_b.domain_id), request)
        self.assertEqual(sorted(result["foreign_source_ids"]), only_a,
                         "域外 Source ID 必须显式回报")
        self.assertNotIn("反射定律", result["text"])
        self.assertNotIn("平面镜", result["text"])
        for sid in only_a:
            self.assertNotIn(f"SRC={sid}", result["text"],
                             "域外 Source ID 不得出现在正文摘录里")

    def test_cognitive_prompt_has_no_cross_domain_text(self):
        from app.learning_engine.cognition import (
            collect_cognitive_inputs, render_cognitive_messages, plan_ku_batches,
        )
        ctx_a, ctx_b = self._two_domains()
        inputs = collect_cognitive_inputs(int(ctx_b.domain_id), ctx_b.run_id, self.lesson_id)
        units = inputs["knowledge_units"]
        self.assertTrue(units)
        batches = plan_ku_batches(units, token_budget=20000)
        for ordinal, batch in enumerate(batches, 1):
            messages, meta = render_cognitive_messages(
                batch, ordinal, len(batches), domain_id=int(ctx_b.domain_id),
                lesson_understanding=inputs["lesson_understanding"],
                chapter_notes=inputs["chapter_notes"],
                confirmed_errors=inputs["confirmed_errors"],
                mastery=inputs["mastery"],
                homework_feedback=inputs["homework_feedback"],
                valid_source_ids=inputs["valid_source_ids"])
            user_text = messages[1]["content"]
            self.assertNotIn("反射定律", user_text, "Domain A 的正文不得进入 Domain B 的 prompt")
            self.assertNotIn("平面镜", user_text)

    def test_persisted_source_span_belongs_to_map_domain(self):
        ctx_a, ctx_b = self._two_domains()
        rows = self.db.fetch_all(
            "SELECT s.source_span_id, s.source_id, sp.domain_id, cm.domain_id AS map_domain "
            "FROM cognitive_item_sources s "
            "JOIN cognitive_items ci ON ci.id = s.cognitive_item_id "
            "JOIN cognitive_maps cm ON cm.id = ci.cognitive_map_id "
            "LEFT JOIN source_spans sp ON sp.id = s.source_span_id "
            "WHERE cm.run_id=?", (ctx_b.run_id,))
        self.assertTrue(rows, "应至少有一个认知项来源关联")
        for row in rows:
            self.assertIsNotNone(row["source_span_id"])
            self.assertEqual(int(row["domain_id"]), int(row["map_domain"]),
                             "source_span_id 必须属于本 map 的 domain")


class TestCognitiveLedgerHonesty(CloseoutBase):
    """缺陷 3：账本必须记录真实 batch 终态，不得伪造成 consumed。"""

    def _three_batch_run(self, fail_batches=()):
        """跑一次认知节点，强制每个 KU 独占一批（3 个 batch）。

        ``fail_batches`` 中的序号对应 batch_ordinal（1 起）。
        """
        from app import gateway as gw_mod
        from app.learning_engine import cognition as cog
        from app.dag import DAGContext

        ctx, _ = self.run_dag(transcript=TEXT_B)
        budget = cog._cognitive_input_budget(None)["input_budget"]
        units = cog.collect_cognitive_inputs(int(ctx.domain_id), ctx.run_id,
                                             self.lesson_id)["knowledge_units"]
        self.assertGreaterEqual(len(units), 3, "需要至少 3 个 KU 才能形成 3 个 batch")
        # 让每个 KU 独占一批：预算刚好够 1 个 KU
        one = cog.plan_ku_batches(units[:1], token_budget=budget)
        self.assertEqual(len(one), 1)
        probe_inputs = cog.collect_cognitive_inputs(int(ctx.domain_id), ctx.run_id,
                                                    self.lesson_id)
        info = cog._verify_batch_budgets(one, inputs=probe_inputs,
                                         valid_source_ids=probe_inputs["valid_source_ids"],
                                         budget=budget)
        per_ku = info["max_input_tokens"]

        orig_budget = cog._cognitive_input_budget
        orig_chat = gw_mod.gateway.chat
        calls = {"n": 0}

        async def failing_chat(model, messages, **kw):
            calls["n"] += 1
            if calls["n"] in fail_batches:
                raise RuntimeError("注入 batch 失败")
            return await orig_chat(model, messages, **kw)

        cog._cognitive_input_budget = _budget_patch(cog, max(per_ku, 1))
        gw_mod.gateway.chat = failing_chat
        raised = None
        try:
            node = DAGContext()
            node.run_id = ctx.run_id
            node.domain_id = int(ctx.domain_id)
            result = asyncio.run(cog.run_student_simulator(
                node, int(ctx.domain_id), ctx.run_id, self.lesson_id,
                model_id="qwen3_flash"))
        except BaseException as e:  # noqa: BLE001
            result, raised = None, e
        finally:
            cog._cognitive_input_budget = orig_budget
            gw_mod.gateway.chat = orig_chat
        return ctx, result, raised, calls, len(units)

    def test_partial_batch_records_real_status_and_half_coverage(self):
        from app.learning_engine.cognition import cognitive_input_coverage
        ctx, result, raised, calls, ku_total = self._three_batch_run(fail_batches=(1,))
        self.assertIsInstance(raised, Exception, "失败 batch 必须让节点抛错（DAG 才能换模型）")
        from app.dag import RetryableModelError
        self.assertIsInstance(raised, RetryableModelError)

        row = self.map_for(ctx.run_id)
        self.assertEqual(row["status"], "partial")
        self.assertEqual(int(row["knowledge_unit_total"]), ku_total)
        self.assertEqual(int(row["knowledge_unit_processed"]), ku_total - 1)
        expected = round((ku_total - 1) / ku_total, 6)
        self.assertAlmostEqual(float(row["cognitive_input_coverage"]), expected, places=6)

        ledger = self.ledger_for(ctx.run_id)
        self.assertEqual(len(ledger), ku_total)
        self.assertEqual({int(r["batch_ordinal"]) for r in ledger},
                         {1, 2, 3}, "必须记录真实 batch 序号")
        self.assertEqual([r["status"] for r in ledger], ["failed"] + ["consumed"] * (ku_total - 1))
        self.assertTrue(ledger[0]["reason_code"], "failed 必须有 reason_code")
        self.assertTrue(all(r["input_hash"] for r in ledger), "每行必须带输入哈希")
        for r in ledger:
            self.assertNotEqual(r["status"], "skipped")

        recomputed = cognitive_input_coverage(ctx.run_id)
        self.assertEqual(recomputed["failed"], 1)
        self.assertAlmostEqual(recomputed["coverage"],
                               float(row["cognitive_input_coverage"]), places=6,
                               msg="账本复算覆盖率必须与 map 一致（不得自相矛盾）")

    def test_three_batch_second_failure_gives_two_thirds(self):
        ctx, result, raised, calls, ku_total = self._three_batch_run(fail_batches=(2,))
        row = self.map_for(ctx.run_id)
        self.assertEqual(int(row["knowledge_unit_processed"]), 2)
        self.assertAlmostEqual(float(row["cognitive_input_coverage"]),
                               round(2 / 3, 6), places=6)
        ledger = self.ledger_for(ctx.run_id)
        self.assertEqual(sorted(int(r["batch_ordinal"]) for r in ledger), [1, 2, 3])
        by_ordinal = {int(r["batch_ordinal"]): r["status"] for r in ledger}
        self.assertEqual(by_ordinal[1], "consumed")
        self.assertEqual(by_ordinal[2], "failed")
        self.assertEqual(by_ordinal[3], "consumed")

    def test_all_batches_succeed_then_no_items_is_empty(self):
        """全成功且无 item 才是 empty（不能把失败伪装成 empty）。"""
        from app.learning_engine.contracts import CognitiveMapStatus
        ctx, result, raised, calls, ku_total = self._three_batch_run()
        self.assertIsNone(raised)
        self.assertIn(result["status"],
                      (CognitiveMapStatus.SUCCEEDED.value, CognitiveMapStatus.EMPTY.value))
        ledger = self.ledger_for(ctx.run_id)
        self.assertTrue(all(r["status"] == "consumed" for r in ledger))
        self.assertEqual(len(ledger), ku_total)

    def test_ledger_db_check_rejects_skipped_without_reason(self):
        ctx, _ = self.run_dag(transcript=TEXT_B)
        map_row = self.map_for(ctx.run_id)
        with self.assertRaises(Exception):
            self.db.insert(
                "INSERT INTO cognitive_input_ledger (run_id, cognitive_map_id, "
                " knowledge_unit_key, batch_ordinal, status, reason_code) "
                "VALUES (?,?, 'KU-X', 1, 'skipped', NULL)",
                (ctx.run_id, int(map_row["id"])))


class TestCognitiveBudget(CloseoutBase):
    """缺陷 4 + P1-A：预算必须由**当前候选模型**的上下文窗口派生，并对最终
    messages 复算。"""

    def test_budget_derived_from_context_window(self):
        from app.learning_engine import cognition as cog
        from app.learning_engine.segment import _model_input_budget
        audit = cog._cognitive_input_budget(None)
        self.assertGreater(audit["input_budget"], 0)
        self.assertEqual(audit["input_budget"], _model_input_budget(None)[0])
        for key in ("model_profile_id", "context_window", "reserved_output",
                    "input_budget", "context_window_source"):
            self.assertIn(key, audit)

    def test_budget_follows_candidate_model_profile(self):
        """P1-A：候选模型档案的 context_window 必须真正决定预算。"""
        from app.learning_engine import cognition as cog
        from app.learning_engine.coverage import DEFAULT_CONTEXT_WINDOW
        self.db.execute(
            "INSERT INTO model_profiles (id, gateway_model, provider, family, capability, "
            " enabled, is_builtin, context_window, updated_at) "
            "VALUES ('small_ctx', 'small', 'qwen', 'qwen', 'text', 1, 0, 8192, "
            " datetime('now','localtime')) "
            "ON CONFLICT(id) DO UPDATE SET context_window=excluded.context_window")
        small = cog._cognitive_input_budget("small_ctx")
        default = cog._cognitive_input_budget(None)
        self.assertEqual(small["context_window"], 8192)
        self.assertEqual(small["context_window_source"], "model_profile")
        self.assertLess(small["input_budget"], default["input_budget"],
                        "小上下文候选模型的预算必须小于默认预算")
        self.assertEqual(default["context_window"], DEFAULT_CONTEXT_WINDOW)

    def test_budget_audit_is_persisted(self):
        from app.learning_engine import cognition as cog
        self.db.execute(
            "INSERT INTO model_profiles (id, gateway_model, provider, family, capability, "
            " enabled, is_builtin, context_window, updated_at) "
            "VALUES ('small_ctx2', 'small2', 'qwen', 'qwen', 'text', 1, 0, 12288, "
            " datetime('now','localtime')) "
            "ON CONFLICT(id) DO UPDATE SET context_window=excluded.context_window")
        ctx, _ = self.run_dag(transcript=TEXT_B)
        # 直接调用节点，显式传小上下文候选模型
        from app.dag import DAGContext
        node = DAGContext()
        node.run_id = ctx.run_id
        node.domain_id = int(ctx.domain_id)
        result = asyncio.run(cog.run_student_simulator(
            node, int(ctx.domain_id), ctx.run_id, self.lesson_id,
            model_id="small_ctx2"))
        self.assertEqual(result["budget"]["model_profile_id"], "small_ctx2")
        self.assertEqual(result["budget"]["context_window"], 12288)
        self.assertEqual(result["budget"]["context_window_source"], "model_profile")
        self.assertEqual(result["budget"]["input_budget"],
                         cog._cognitive_input_budget("small_ctx2")["input_budget"])
        row = self.map_for(ctx.run_id)
        payload = json.loads(row["structured_json"])
        self.assertEqual(payload["budget"]["model_profile_id"], "small_ctx2")
        self.assertEqual(payload["budget"]["context_window"], 12288)

    def test_verify_budget_uses_final_messages(self):
        from app.learning_engine import cognition as cog
        ctx, _ = self.run_dag(transcript=TEXT_B)
        inputs = cog.collect_cognitive_inputs(int(ctx.domain_id), ctx.run_id, self.lesson_id)
        batches = cog.plan_ku_batches(inputs["knowledge_units"], token_budget=20000)
        info = cog._verify_batch_budgets(
            batches, inputs=inputs, valid_source_ids=inputs["valid_source_ids"], budget=20000)
        self.assertEqual(info["over_budget_batches"], 0)
        self.assertGreater(info["max_input_tokens"], 0)
        # 相同批次在极小预算下必须被判超预算
        tight = cog._verify_batch_budgets(
            batches, inputs=inputs, valid_source_ids=inputs["valid_source_ids"], budget=10)
        self.assertGreater(tight["over_budget_batches"], 0)

    def test_oversized_ku_fails_honestly_and_is_recorded(self):
        from app.learning_engine import cognition as cog
        from app.dag import DAGContext
        ctx, _ = self.run_dag(transcript=TEXT_B)
        # 清掉 DAG 运行留下的 map，确保断言只针对本次调用
        self.db.execute("DELETE FROM cognitive_maps WHERE run_id=?", (ctx.run_id,))
        orig = cog._cognitive_input_budget
        cog._cognitive_input_budget = _budget_patch(cog, 5)
        raised = None
        try:
            node = DAGContext()
            node.run_id = ctx.run_id
            node.domain_id = int(ctx.domain_id)
            asyncio.run(cog.run_student_simulator(
                node, int(ctx.domain_id), ctx.run_id, self.lesson_id,
                model_id="qwen3_flash"))
        except BaseException as e:  # noqa: BLE001
            raised = e
        finally:
            cog._cognitive_input_budget = orig
        self.assertIsInstance(raised, cog.CognitiveBudgetError)
        # 诚实失败：不得落一份 succeeded/empty 的 map 掩盖问题
        self.assertIsNone(self.map_for(ctx.run_id))

    def test_batching_is_order_preserving_and_losless(self):
        from app.learning_engine import cognition as cog
        units = [{"id": f"KU-{i:04d}", "topic": f"t{i}", "summary": "s" * 30}
                 for i in range(1, 12)]
        batches = cog.plan_ku_batches(units, token_budget=40)
        flat = [u["id"] for b in batches for u in b]
        self.assertEqual(flat, [u["id"] for u in units], "分批必须保序且不丢")
        self.assertGreater(len(batches), 1)


class TestDagModelFallback(CloseoutBase):
    """缺陷 3 附加：partial/failed 必须真的触发 DAG 模型切换。"""

    def test_first_model_failure_switches_candidate(self):
        from app import gateway as gw_mod
        from app.learning_engine import cognition as cog

        orig_chat = gw_mod.gateway.chat
        seen_models: list[str] = []

        async def flaky(model, messages, **kw):
            seen_models.append(str(model))
            if str(model) == "qwen3_flash":
                raise RuntimeError("注入：首个候选模型失败")
            return await orig_chat(model, messages, **kw)

        gw_mod.gateway.chat = flaky
        try:
            ctx, outputs = self.run_dag(transcript=TEXT_B)
        finally:
            gw_mod.gateway.chat = orig_chat

        node = outputs["student_simulator"]
        self.assertEqual(node["status"], "succeeded",
                         "第二个候选模型必须真正接管")
        self.assertNotEqual(node["model_used"], "qwen3_flash")
        self.assertIn("qwen3_flash", seen_models)
        self.assertIn(node["model_used"], seen_models)

        # 审计可见：成功候选的真实网关调用落在 model_calls 上
        calls = self.db.fetch_all(
            "SELECT model_id, status FROM model_calls WHERE run_id=? ORDER BY id", (ctx.run_id,))
        self.assertTrue(calls, "model_calls 必须有审计记录")
        self.assertIn(node["model_used"], {c["model_id"] for c in calls},
                      "成功候选模型必须在 model_calls 中留痕")
        # 路由配置也能证明候选顺序里 qwen3_flash 优先、失败后才轮到下一个
        from app.dag_lesson import build_lesson_dag
        preferred = build_lesson_dag().nodes["student_simulator"].preferred_models
        self.assertEqual(preferred[0], "qwen3_flash")
        self.assertIn(node["model_used"], preferred)
        # 最终 map 记录的模型 = 成功的候选模型
        row = self.map_for(ctx.run_id)
        self.assertEqual(row["model_used"], node["model_used"])
        self.assertEqual(row["status"], "succeeded")

    def test_all_models_failed_marks_failed_and_run_not_completed(self):
        """全部 batch 失败 → map.status 必须是 failed（不是 partial），
        账本无一条 consumed，覆盖率 0，且 run 不得 completed。"""
        from app import gateway as gw_mod
        from app.dag import DAGContext
        from app.dag_lesson import build_lesson_dag
        from app.learning_engine import cognition as cog
        from app.learning_engine.contracts import CognitiveMapStatus

        ctx, _ = self.run_dag(transcript=TEXT_B)
        # 清掉 DAG 运行留下的认知产物，断言只针对本次注入调用
        self.db.execute("DELETE FROM cognitive_maps WHERE run_id=?", (ctx.run_id,))
        orig_chat = gw_mod.gateway.chat

        async def always_fail(model, messages, **kw):
            if kw.get("contract") == "lesson/student_simulator":
                raise RuntimeError("注入：全部认知模型失败")
            return await orig_chat(model, messages, **kw)

        gw_mod.gateway.chat = always_fail
        raised = None
        try:
            node = DAGContext()
            node.run_id = ctx.run_id
            node.domain_id = int(ctx.domain_id)
            asyncio.run(cog.run_student_simulator(
                node, int(ctx.domain_id), ctx.run_id, self.lesson_id,
                model_id="qwen3_flash"))
        except BaseException as e:  # noqa: BLE001
            raised = e
        finally:
            gw_mod.gateway.chat = orig_chat

        self.assertIsNotNone(raised)
        row = self.map_for(ctx.run_id)
        self.assertEqual(row["status"], CognitiveMapStatus.FAILED.value,
                         "全部 batch 失败必须记 failed，不得记 partial")
        ledger = self.ledger_for(ctx.run_id)
        self.assertTrue(ledger)
        self.assertTrue(all(r["status"] == "failed" for r in ledger),
                        "全部失败时账本不得有一条 consumed")
        self.assertEqual(int(row["knowledge_unit_processed"]), 0)
        self.assertEqual(float(row["cognitive_input_coverage"]), 0.0)

        # DAG 层面：所有候选耗尽后 run 不得 completed
        gw_mod.gateway.chat = always_fail
        run2_failed = False
        try:
            ctx2 = DAGContext()
            ctx2.input = {"course_id": self.course_id, "chapter_id": self.chapter_id,
                          "lesson_id": self.lesson_id, "transcript": TEXT_B,
                          "material_ids": []}
            asyncio.run(build_lesson_dag().run(ctx2))
        except BaseException:  # noqa: BLE001
            run2_failed = True
        finally:
            gw_mod.gateway.chat = orig_chat
        self.assertTrue(run2_failed, "认知全部失败时 DAG 必须失败而不是 completed")
        leftover = self.db.fetch_one(
            "SELECT status FROM workflow_runs WHERE id=?", (ctx2.run_id,))
        self.assertNotEqual((leftover or {}).get("status"), "completed")

    def test_cancel_propagates_and_is_not_reported_as_partial(self):
        """取消必须向上传播，且不得被伪造成 partial/empty/succeeded。"""
        from app import gateway as gw_mod
        from app.dag import DAGContext, RunCancelledError
        from app.learning_engine import cognition as cog

        ctx, _ = self.run_dag(transcript=TEXT_B)
        # 清掉 DAG 运行留下的认知产物，断言只针对本次注入调用
        self.db.execute("DELETE FROM cognitive_maps WHERE run_id=?", (ctx.run_id,))
        orig_chat = gw_mod.gateway.chat

        async def cancel_chat(model, messages, **kw):
            raise RunCancelledError("注入取消")

        gw_mod.gateway.chat = cancel_chat
        raised = None
        try:
            node = DAGContext()
            node.run_id = ctx.run_id
            node.domain_id = int(ctx.domain_id)
            asyncio.run(cog.run_student_simulator(
                node, int(ctx.domain_id), ctx.run_id, self.lesson_id,
                model_id="qwen3_flash"))
        except BaseException as e:  # noqa: BLE001
            raised = e
        finally:
            gw_mod.gateway.chat = orig_chat

        self.assertIsInstance(raised, (RunCancelledError, asyncio.CancelledError))
        # 取消不会被写成任何「成功/部分」产物 —— 认知产物按 run 幂等重建，
        # 半成品不得进入表里冒充结果。
        self.assertIsNone(self.map_for(ctx.run_id))

    def test_cancel_after_first_batch_leaves_no_fake_success(self):
        """第一个 batch 成功、第二个 batch 取消：不得落 partial/succeeded。"""
        from app import gateway as gw_mod
        from app.dag import DAGContext, RunCancelledError
        from app.learning_engine import cognition as cog

        ctx, _ = self.run_dag(transcript=TEXT_B)
        self.db.execute("DELETE FROM cognitive_maps WHERE run_id=?", (ctx.run_id,))
        budget = cog._cognitive_input_budget(None)["input_budget"]
        inputs = cog.collect_cognitive_inputs(int(ctx.domain_id), ctx.run_id, self.lesson_id)
        self.assertGreaterEqual(len(inputs["knowledge_units"]), 3)
        one = cog.plan_ku_batches(inputs["knowledge_units"][:1], token_budget=budget)
        per_ku = cog._verify_batch_budgets(
            one, inputs=inputs, valid_source_ids=inputs["valid_source_ids"],
            budget=budget)["max_input_tokens"]

        orig_budget = cog._cognitive_input_budget
        orig_chat = gw_mod.gateway.chat
        calls = {"n": 0}

        async def cancel_second(model, messages, **kw):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RunCancelledError("注入取消（第 2 批）")
            return await orig_chat(model, messages, **kw)

        cog._cognitive_input_budget = _budget_patch(cog, max(per_ku, 1))
        gw_mod.gateway.chat = cancel_second
        raised = None
        try:
            node = DAGContext()
            node.run_id = ctx.run_id
            node.domain_id = int(ctx.domain_id)
            asyncio.run(cog.run_student_simulator(
                node, int(ctx.domain_id), ctx.run_id, self.lesson_id,
                model_id="qwen3_flash"))
        except BaseException as e:  # noqa: BLE001
            raised = e
        finally:
            cog._cognitive_input_budget = orig_budget
            gw_mod.gateway.chat = orig_chat

        self.assertIsInstance(raised, (RunCancelledError, asyncio.CancelledError))
        self.assertGreaterEqual(calls["n"], 2, "必须真的跑过第 2 批")
        self.assertIsNone(self.map_for(ctx.run_id),
                          "取消不得落一份 partial/succeeded 的 CognitiveMap")


class TestInputHashHonesty(CloseoutBase):
    """缺陷 P1-b：input_hash 必须由真实输入派生。"""

    def test_input_hash_derives_from_inputs(self):
        """input_hash 必须能由真实输入复算出来（不含输出 item 的稳定键）。"""
        from app.learning_engine import cognition as cog
        ctx, _ = self.run_dag(transcript=TEXT_B)
        row = self.map_for(ctx.run_id)
        self.assertTrue(row["input_hash"])
        payload = json.loads(row["structured_json"])
        stored_batches = payload.get("batches") or []
        self.assertTrue(stored_batches, "批次计划必须随产物持久化")
        self.assertTrue(all(b.get("input_hash") for b in stored_batches))

        # 用「持久化的批次计划」+ 当前输入复算：必须与落库值一致
        batch_lines = [{"batch_ordinal": b["batch_ordinal"],
                        "knowledge_unit_keys": b["knowledge_unit_keys"],
                        "model": b.get("model"),
                        "input_hash": b["input_hash"]} for b in stored_batches]
        inputs = cog.collect_cognitive_inputs(int(ctx.domain_id), ctx.run_id, self.lesson_id)
        recomputed = cog._cognitive_input_hash(
            int(ctx.domain_id), inputs=inputs, batch_lines=batch_lines,
            model_id=row["model_used"], ku_total=int(row["knowledge_unit_total"]),
            coverage=float(row["cognitive_input_coverage"]))
        self.assertEqual(row["input_hash"], recomputed)

        # 输入变化 → 哈希必须变化
        changed = dict(inputs)
        changed["chapter_notes"] = list(inputs["chapter_notes"]) + ["新增章节备注"]
        self.assertNotEqual(
            row["input_hash"],
            cog._cognitive_input_hash(
                int(ctx.domain_id), inputs=changed, batch_lines=batch_lines,
                model_id=row["model_used"], ku_total=int(row["knowledge_unit_total"]),
                coverage=float(row["cognitive_input_coverage"])),
            "章节备注变化必须改变 input_hash")

    def test_input_hash_does_not_depend_on_output_items(self):
        """哈希只由输入派生：重算两次（输出侧不变）必须一致，
        且与输出 item 集合无关。"""
        from app.learning_engine import cognition as cog
        ctx, _ = self.run_dag(transcript=TEXT_B)
        row = self.map_for(ctx.run_id)
        payload = json.loads(row["structured_json"])
        batch_lines = [{"batch_ordinal": b["batch_ordinal"],
                        "knowledge_unit_keys": b["knowledge_unit_keys"],
                        "model": b.get("model"), "input_hash": b["input_hash"]}
                       for b in (payload.get("batches") or [])]
        inputs = cog.collect_cognitive_inputs(int(ctx.domain_id), ctx.run_id, self.lesson_id)
        first = cog._cognitive_input_hash(
            int(ctx.domain_id), inputs=inputs, batch_lines=batch_lines,
            model_id=row["model_used"], ku_total=int(row["knowledge_unit_total"]),
            coverage=float(row["cognitive_input_coverage"]))
        second = cog._cognitive_input_hash(
            int(ctx.domain_id), inputs=inputs, batch_lines=batch_lines,
            model_id=row["model_used"], ku_total=int(row["knowledge_unit_total"]),
            coverage=float(row["cognitive_input_coverage"]))
        self.assertEqual(first, second)
        # 输出 item 的稳定键不得出现在哈希输入里
        item_keys = [i["stable_key"] for i in payload.get("items") or []]
        if item_keys:
            probe = cog._cognitive_input_hash(
                int(ctx.domain_id), inputs=inputs, batch_lines=batch_lines,
                model_id=row["model_used"], ku_total=int(row["knowledge_unit_total"]),
                coverage=float(row["cognitive_input_coverage"]))
            self.assertEqual(row["input_hash"], probe)

    def test_input_hash_changes_with_input_not_with_output(self):
        from app.learning_engine import cognition as cog
        ctx, _ = self.run_dag(transcript=TEXT_B)
        inputs = cog.collect_cognitive_inputs(int(ctx.domain_id), ctx.run_id, self.lesson_id)
        lines = [{"batch_ordinal": 1, "knowledge_unit_keys": ["KU-0001"],
                  "model": "m", "input_hash": "sha256:x"}]
        base = cog._cognitive_input_hash(
            int(ctx.domain_id), inputs=inputs, batch_lines=lines, model_id="m",
            ku_total=1, coverage=1.0)
        # 输入变化 → 哈希变化
        changed = dict(inputs)
        changed["chapter_notes"] = list(inputs["chapter_notes"]) + ["新增章节备注"]
        self.assertNotEqual(
            base, cog._cognitive_input_hash(
                int(ctx.domain_id), inputs=changed, batch_lines=lines, model_id="m",
                ku_total=1, coverage=1.0), "输入变化必须改变 input_hash")
        # 只有 coverage（输出侧派生量）变化时也应变（它是账本口径的一部分）
        self.assertNotEqual(
            base, cog._cognitive_input_hash(
                int(ctx.domain_id), inputs=inputs, batch_lines=lines, model_id="m",
                ku_total=1, coverage=0.5))


class TestMasteryScope(CloseoutBase):
    """缺陷 5：mastery / 错题 / 作业反馈必须按作用域过滤。"""

    def _create_mastery_table(self):
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS knowledge_mastery ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, course_id INTEGER, chapter_id INTEGER, "
            " knowledge_unit_key TEXT, mastery REAL, confidence REAL, updated_from TEXT)")

    def test_mastery_is_absent_without_phase6_table(self):
        from app.learning_engine import cognition as cog
        ctx, _ = self.run_dag(transcript=TEXT_B)
        inputs = cog.collect_cognitive_inputs(int(ctx.domain_id), ctx.run_id, self.lesson_id)
        self.assertEqual(inputs["mastery"], [])
        self.assertEqual(inputs["availability"]["mastery_state"], "ready")

    def test_mastery_is_scoped_to_course_and_chapter(self):
        from app.learning_engine import cognition as cog
        ctx, _ = self.run_dag(transcript=TEXT_B)
        other_course = self.db.insert("INSERT INTO courses (name, code) VALUES ('化学','CHEM')")
        other_chapter = self.db.insert(
            "INSERT INTO chapters (course_id, chapter_no, title) VALUES (?, 9, '第九章')",
            (other_course,))
        other_ku = self.db.insert("INSERT INTO knowledge_units (course_id,chapter_id,stable_key,topic) VALUES (?,?,?,?)",
                                  (other_course, other_chapter, 'KU-OTHER', 'other'))
        own_ku = self.db.insert("INSERT INTO knowledge_units (course_id,chapter_id,lesson_id,stable_key,topic) VALUES (?,?,?,?,?)",
                                (self.course_id, self.chapter_id, self.lesson_id, 'KU-OWN', 'own'))
        self.db.execute("INSERT INTO knowledge_mastery (knowledge_unit_id,course_id,chapter_id,mastery,confidence,updated_from) VALUES (?,?,?,?,?,?)",
                        (other_ku, other_course, other_chapter, 0.11, 0.9, 'other'))
        self.db.execute("INSERT INTO knowledge_mastery (knowledge_unit_id,course_id,chapter_id,mastery,confidence,updated_from) VALUES (?,?,?,?,?,?)",
                        (own_ku, self.course_id, self.chapter_id, 0.77, 0.8, 'own'))

        inputs = cog.collect_cognitive_inputs(int(ctx.domain_id), ctx.run_id, self.lesson_id)
        masteries = [float(m["mastery"]) for m in inputs["mastery"]]
        self.assertEqual(masteries, [0.77], "只允许本课程+本章节的 mastery")
        self.assertNotIn(0.11, masteries, "其他课程的 mastery 绝不能被读入")
        self.assertEqual(inputs["availability"]["mastery_scope"],
                         f"course={self.course_id},chapter={self.chapter_id}")

    def test_mastery_unknown_scope_is_deferred_not_global(self):
        from app.learning_engine import cognition as cog
        # 无 domain / 无 chapter 的场景：直接调用装配函数
        inputs = cog.collect_cognitive_inputs(0, 999999, None)
        self.assertEqual(inputs["mastery"], [], "作用域未知时必须拒绝读取全局 mastery")
        self.assertEqual(inputs["availability"]["mastery_state"], "deferred")

    def test_confirmed_errors_scoped_to_lesson(self):
        from app.learning_engine import cognition as cog
        ctx, _ = self.run_dag(transcript=TEXT_B)
        other_lesson = self.db.insert(
            "INSERT INTO lessons (chapter_id, course_id, lesson_no, title) "
            "VALUES (?, ?, 'L99', '别的课')", (self.chapter_id, self.course_id))
        self.db.execute(
            "INSERT INTO errors (lesson_id, chapter_id, course_id, question_text, status) "
            "VALUES (?,?,?, '别的课的错题', 'confirmed')",
            (other_lesson, self.chapter_id, self.course_id))
        self.db.execute(
            "INSERT INTO errors (lesson_id, chapter_id, course_id, question_text, status) "
            "VALUES (?,?,?, '本课错题', 'confirmed')",
            (self.lesson_id, self.chapter_id, self.course_id))

        inputs = cog.collect_cognitive_inputs(int(ctx.domain_id), ctx.run_id, self.lesson_id)
        texts = [e["question_text"] for e in inputs["confirmed_errors"]]
        self.assertIn("本课错题", texts)
        self.assertNotIn("别的课的错题", texts, "错题必须限定在本堂课")
        self.assertEqual(inputs["availability"]["confirmed_errors_scope"],
                         f"lesson:{self.lesson_id}")

    def test_homework_feedback_scoped_to_lesson(self):
        from app.learning_engine import cognition as cog
        ctx, _ = self.run_dag(transcript=TEXT_B)
        other_lesson = self.db.insert(
            "INSERT INTO lessons (chapter_id, course_id, lesson_no, title) "
            "VALUES (?, ?, 'L98', '另一节课')", (self.chapter_id, self.course_id))
        # 作用域在 homeworks.lesson_id（questions 没有 lesson_id 列）
        hw_mine = self.db.insert(
            "INSERT INTO homeworks (course_id, chapter_id, lesson_id, title) "
            "VALUES (?,?,?, '本课作业')", (self.course_id, self.chapter_id, self.lesson_id))
        hw_other = self.db.insert(
            "INSERT INTO homeworks (course_id, chapter_id, lesson_id, title) "
            "VALUES (?,?,?, '别课作业')", (self.course_id, self.chapter_id, other_lesson))
        q_mine = self.db.insert(
            "INSERT INTO questions (homework_id, text) VALUES (?, '本课作业题')", (hw_mine,))
        q_other = self.db.insert(
            "INSERT INTO questions (homework_id, text) VALUES (?, '别课作业题')", (hw_other,))
        self.db.execute("INSERT INTO answer_items (question_id, final_answer) VALUES (?, 'A')",
                        (q_mine,))
        self.db.execute("INSERT INTO answer_items (question_id, final_answer) VALUES (?, 'B')",
                        (q_other,))

        inputs = cog.collect_cognitive_inputs(int(ctx.domain_id), ctx.run_id, self.lesson_id)
        texts = [h["text"] for h in inputs["homework_feedback"]]
        self.assertIn("本课作业题", texts)
        self.assertNotIn("别课作业题", texts, "作业反馈必须限定在本堂课")
        self.assertEqual(inputs["availability"]["homework_feedback_scope"],
                         f"lesson:{self.lesson_id}")
        for row in inputs["homework_feedback"]:
            self.assertEqual(int(row["lesson_id"]), int(self.lesson_id))

    def test_homework_feedback_query_matches_schema(self):
        """回归：questions 表没有 lesson_id 列，查询必须走 homeworks 关联。"""
        import inspect
        from app.learning_engine import cognition as cog
        src = inspect.getsource(cog.collect_cognitive_inputs)
        self.assertNotIn("q.lesson_id", src, "不得引用不存在的 questions.lesson_id")
        self.assertIn("h.lesson_id", src)
        # 表结构自证：questions 无 lesson_id
        cols = {r["name"] for r in self.db.fetch_all("PRAGMA table_info(questions)")}
        self.assertNotIn("lesson_id", cols)
        hw_cols = {r["name"] for r in self.db.fetch_all("PRAGMA table_info(homeworks)")}
        self.assertIn("lesson_id", hw_cols)


class TestReuseVersionGuard(CloseoutBase):
    """附加：同 run 复用必须比对 prompt / schema 版本。"""

    def test_same_domain_reuse_requires_matching_versions(self):
        from app.learning_engine import understanding as un
        ctx, _ = self.run_dag(transcript=TEXT_B)
        segs = self.db.fetch_all(
            "SELECT id, ordinal, input_hash, status FROM lesson_segments WHERE domain_id=? "
            "ORDER BY ordinal", (ctx.domain_id,))
        self.assertTrue(segs)
        seg_id = int(segs[0]["id"])
        self.assertEqual(segs[0]["status"], "succeeded")
        su = self.db.fetch_one("SELECT * FROM segment_understandings WHERE segment_id=?",
                               (seg_id,))
        self.assertIsNotNone(su)

        seg = {"id": seg_id, "input_hash": su["input_hash"], "ordinal": int(segs[0]["ordinal"])}
        # 版本一致 → 命中同 run 复用
        self.assertTrue(un._try_reuse(seg, ctx, int(ctx.domain_id), ctx.run_id,
                                      su["model_used"]))
        # prompt 版本被伪造为旧值 → 不得复用
        self.db.execute("UPDATE segment_understandings SET prompt_version='v0' WHERE segment_id=?",
                        (seg_id,))
        self.assertFalse(un._try_reuse(seg, ctx, int(ctx.domain_id), ctx.run_id,
                                       su["model_used"]),
                         "prompt 版本不一致时必须放弃复用")
        # schema 版本被伪造 → 不得复用
        self.db.execute(
            "UPDATE segment_understandings SET prompt_version=?, schema_version='v0' "
            "WHERE segment_id=?", (un.SEGMENT_PROMPT_VERSION, seg_id))
        self.assertFalse(un._try_reuse(seg, ctx, int(ctx.domain_id), ctx.run_id,
                                       su["model_used"]),
                         "schema 版本不一致时必须放弃复用")


if __name__ == "__main__":
    unittest.main()
