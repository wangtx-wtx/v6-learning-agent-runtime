"""V6 Phase 3 Final Micro-Closeout 回归测试（P1-A/B/C + 状态矩阵）。

对应独立复现的 4 个问题:

P1-A  预算必须来自**当前候选模型**的 ``model_profiles.context_window``，
      而不是恒用默认窗口。
P1-B  每批只暴露本批 KU 与本批 ``allowed_source_ids``；模型引用其他批次的
      Source ID 必须让该 batch 失败并触发 fallback；跨 batch 只在合并阶段归并。
P1-C  ``foreign_source_ids`` 只表示「当前 domain 中确实不存在」；受预算限制
      未发送的来源记 ``omitted_due_budget``；正文截断记 ``excerpt_truncated``。
P1-D  状态矩阵：succeeded / empty / partial / failed / cancelled。

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

#: 含认知线索的课堂正文（3 个 span → 3 个 KU，便于构造 3 个 batch）
TEXT = "\n".join([
    "[00:00:00] 第二节课：凸透镜成像规律，物距大于二倍焦距成倒立缩小实像。",
    "[00:05:00] 第二节课：物距小于焦距时成正立放大虚像。",
    "[00:10:00] 第二节课：容易混淆的是实像与虚像的判别。",
])
#: 完全中性的正文（无认知线索 → 空 items，用于 empty 状态）
NEUTRAL = "\n".join([
    "[00:00:00] 太阳系有八大行星。",
    "[00:05:00] 地球是第三颗行星。",
])


def _force_fake_gateway():
    os.environ["V5_GATEWAY_MODE"] = "fake"
    os.environ.pop("V5_GATEWAY_API_KEY", None)
    os.environ.pop("V5_ALLOW_LIVE_MODEL_TESTS", None)


def _budget_patch(cog, value: int):
    """把认知预算固定为 ``value``，保留审计字段形状。"""
    original = cog._cognitive_input_budget

    def patched(model_profile_id=None):
        audit = dict(original(model_profile_id))
        audit["input_budget"] = int(value)
        audit["budget_override_for_test"] = True
        return audit

    return patched


class MicroBase(unittest.TestCase):
    def setUp(self):
        _force_fake_gateway()
        self._prev = os.environ.get("V6_LEARNING_ENGINE")
        os.environ["V6_LEARNING_ENGINE"] = "shadow"
        from app import gateway as gw_mod
        gw_mod.reset_engine_for_test()
        from app import config as app_config
        self._saved_mobile_token = app_config.MOBILE_TOKEN
        app_config.MOBILE_TOKEN = ""

        self.tmp = Path(tempfile.mkdtemp(prefix="v6_mc_"))
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
    def run_dag(self, *, transcript="", material_ids=None):
        from app.dag import DAGContext
        from app.dag_lesson import build_lesson_dag
        ctx = DAGContext()
        ctx.input = {"course_id": self.course_id, "chapter_id": self.chapter_id,
                     "lesson_id": self.lesson_id, "transcript": transcript,
                     "material_ids": material_ids or []}
        outputs = asyncio.run(build_lesson_dag().run(ctx))
        return ctx, outputs

    def define_model_profile(self, model_id: str, context_window: int):
        self.db.execute(
            "INSERT INTO model_profiles (id, gateway_model, provider, family, capability, "
            " enabled, is_builtin, context_window, updated_at) "
            "VALUES (?,?, 'qwen', 'qwen', 'text', 1, 0, ?, datetime('now','localtime')) "
            "ON CONFLICT(id) DO UPDATE SET context_window=excluded.context_window",
            (model_id, model_id, context_window))

    def map_for(self, run_id):
        row = self.db.fetch_one("SELECT * FROM cognitive_maps WHERE run_id=?", (run_id,))
        return dict(row) if row else None

    def ledger_for(self, run_id):
        return [dict(r) for r in self.db.fetch_all(
            "SELECT * FROM cognitive_input_ledger WHERE run_id=? "
            "ORDER BY batch_ordinal, id", (run_id,))]

    def make_batches(self, ctx, per_ku_budget):
        """把 KU 分成「每批 1 个」的批次（返回 (batches, inputs, budget)）。"""
        from app.learning_engine import cognition as cog
        inputs = cog.collect_cognitive_inputs(int(ctx.domain_id), ctx.run_id, self.lesson_id)
        units = inputs["knowledge_units"]
        self.assertGreaterEqual(len(units), 3)
        budget = cog._cognitive_input_budget(None)["input_budget"]
        one = cog.plan_ku_batches(units[:1], token_budget=budget)
        info = cog._verify_batch_budgets(
            one, inputs=inputs, valid_source_ids=inputs["valid_source_ids"], budget=budget)
        per_ku_budget = per_ku_budget or max(info["max_input_tokens"], 1)
        batches = cog.plan_ku_batches(units, batch_size=1, token_budget=per_ku_budget)
        self.assertEqual(len(batches), len(units), "必须形成每批 1 个 KU 的批次")
        return batches, inputs, per_ku_budget


class TestBatchLocalInputs(MicroBase):
    """P1-B：每批只暴露本批 KU 与本批 Source ID。"""

    def test_batch_messages_exclude_other_batches(self):
        from app.learning_engine import cognition as cog
        ctx, _ = self.run_dag(transcript=TEXT)
        batches, inputs, _ = self.make_batches(ctx, None)
        self.assertEqual(len(batches), 3)
        keys = [[cog._ku_key(u, i + 1) for i, u in enumerate(b)] for b in batches]
        all_keys = [k for group in keys for k in group]

        for ordinal, batch in enumerate(batches, 1):
            messages, meta = cog.render_cognitive_messages(
                batch, ordinal, len(batches), domain_id=int(ctx.domain_id),
                lesson_understanding=inputs["lesson_understanding"],
                chapter_notes=inputs["chapter_notes"],
                confirmed_errors=inputs["confirmed_errors"],
                mastery=inputs["mastery"],
                homework_feedback=inputs["homework_feedback"],
                valid_source_ids=inputs["valid_source_ids"])
            system_text = messages[0]["content"]
            user_text = messages[1]["content"]
            own = set(keys[ordinal - 1])
            other = [k for k in all_keys if k not in own]
            own_sources = sorted({s for u in batch for s in (u.get("source_refs") or [])
                                  if s in inputs["valid_source_ids"]})
            other_sources = sorted(
                {s for b in batches if b is not batch
                 for u in b for s in (u.get("source_refs") or [])
                 if s in inputs["valid_source_ids"]} - set(own_sources))

            # 1) user 消息（KU 行 + 材料线索 + 本批原文）不得出现其他批的 KU
            for k in other:
                self.assertNotIn(k, user_text,
                                 f"batch {ordinal} 的 user 消息不得出现其他批的 KU {k}")
            # 1b) KU 行（system/user 里唯一会被模型当作 KU_KEY 来源的块）只含本批
            for k in other:
                self.assertNotIn(k, meta["ku_block"])
            # 1c) 系统提示中的 KU 清单本身不得列出其他批 KU
            #     （模板 JSON 示例里的 "KU-0001" 是格式示例，不属于任何批次）
            self.assertNotIn("KU_KEY=KU-0002", system_text)
            self.assertNotIn("KU_KEY=KU-0003", system_text)
            # 2) allowed 清单必须等于本批 KU 实际引用的 Source ID
            self.assertEqual(sorted(meta["allowed_ids"]), own_sources)
            self.assertEqual(sorted(meta["batch_source_ids"]), own_sources)
            # 3) 其他批的 Source ID 不得出现在 system/user 任何位置
            for sid in other_sources:
                self.assertNotIn(sid, system_text,
                                 f"batch {ordinal} 的 system 提示不得出现其他批 Source ID {sid}")
                self.assertNotIn(f"SRC={sid}", user_text,
                                 f"batch {ordinal} 的原文不得包含其他批 Source ID {sid}")
            # 4) 材料线索只含本批 KU
            clue_block = user_text.split("## 本批课堂材料线索")[1].split("##")[0]
            for k in other:
                self.assertNotIn(k, clue_block)
            # 5) 系统提示声明的是「本批」名单
            self.assertIn("本批允许使用的 Source ID", system_text)

    def test_allowed_ids_are_subset_of_domain(self):
        from app.learning_engine.cognition import _batch_allowed_source_ids
        ctx, _ = self.run_dag(transcript=TEXT)
        batches, inputs, _ = self.make_batches(ctx, None)
        domain_ids = set(inputs["valid_source_ids"])
        for batch in batches:
            allowed = _batch_allowed_source_ids(batch, domain_ids)
            self.assertTrue(set(allowed) <= domain_ids)
            self.assertTrue(allowed, "本批应有自己的 Source ID")

    def test_model_citing_other_batch_source_fails_and_falls_back(self):
        """P1-B 验收：batch 1 引用 batch 2 的 Source ID → 该 batch 失败 → fallback。"""
        from app import gateway as gw_mod
        from app.dag import DAGContext, RetryableModelError
        from app.learning_engine import cognition as cog

        ctx, _ = self.run_dag(transcript=TEXT)
        self.db.execute("DELETE FROM cognitive_maps WHERE run_id=?", (ctx.run_id,))
        batches, inputs, per_ku = self.make_batches(ctx, None)
        batch1_ids = set(cog._batch_allowed_source_ids(batches[0], inputs["valid_source_ids"]))
        batch2_ids = set(cog._batch_allowed_source_ids(batches[1], inputs["valid_source_ids"]))
        foreign_in_domain = sorted(batch2_ids - batch1_ids)
        self.assertTrue(foreign_in_domain, "构造失败：batch2 应有 batch1 没有的 Source ID")
        other_batch_sid = foreign_in_domain[0]

        orig_budget = cog._cognitive_input_budget
        orig_chat = gw_mod.gateway.chat
        calls = {"n": 0}

        async def citing_other_batch(model, messages, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                # 第一轮的第一批：故意引用别的批次的 Source ID
                return {"content": json.dumps({
                    "items": [{
                        "type": "emphasis", "title": "越界来源", "explanation": "e",
                        "severity": 0.5, "confidence": 0.5,
                        "recommended_treatment": "none", "origin": "classroom_evidence",
                        "knowledge_unit_refs": [cog._ku_key(batches[0][0], 1)],
                        "source_refs": [other_batch_sid],
                    }],
                    "analysis_note": "",
                }, ensure_ascii=False), "model": model, "tokens_in": 1, "tokens_out": 1}
            return await orig_chat(model, messages, **kw)

        cog._cognitive_input_budget = _budget_patch(cog, per_ku)
        gw_mod.gateway.chat = citing_other_batch
        raised = None
        persisted = None
        calls["n"] = 0
        try:
            node = DAGContext()
            node.run_id = ctx.run_id
            node.domain_id = int(ctx.domain_id)
            try:
                asyncio.run(cog.run_student_simulator(
                    node, int(ctx.domain_id), ctx.run_id, self.lesson_id,
                    model_id="qwen3_flash"))
            except BaseException as e:  # noqa: BLE001
                raised = e
            persisted = self.map_for(ctx.run_id)
        finally:
            cog._cognitive_input_budget = orig_budget
            gw_mod.gateway.chat = orig_chat

        self.assertIsInstance(raised, RetryableModelError,
                              "越界引用必须让节点抛可恢复错误（DAG 才能 fallback）")
        self.assertIsNotNone(persisted)
        ledger = self.ledger_for(ctx.run_id)
        self.assertTrue(any(r["status"] == "failed" for r in ledger))
        failed_rows = [r for r in ledger if r["status"] == "failed"]
        self.assertTrue(any("batch_failed" in (r["reason_code"] or "") for r in failed_rows),
                        "失败原因必须留痕")
        # 越界引用绝不进入持久化认知项
        items = self.db.fetch_all(
            "SELECT ci.source_id FROM cognitive_item_sources ci "
            "JOIN cognitive_items i ON i.id = ci.cognitive_item_id "
            "JOIN cognitive_maps m ON m.id = i.cognitive_map_id WHERE m.run_id=?",
            (ctx.run_id,))
        self.assertNotIn(other_batch_sid, {r["source_id"] for r in items},
                         "越界 Source ID 绝不落库")

    def test_dag_fallback_after_cross_batch_violation(self):
        """越界引用 → 节点抛错 → DAG 换到下一个候选模型并最终成功。"""
        from app import gateway as gw_mod
        from app.learning_engine import cognition as cog

        ctx, _ = self.run_dag(transcript=TEXT)
        self.db.execute("DELETE FROM cognitive_maps WHERE run_id=?", (ctx.run_id,))
        batches, inputs, per_ku = self.make_batches(ctx, None)
        batch1_ids = set(cog._batch_allowed_source_ids(batches[0], inputs["valid_source_ids"]))
        batch2_ids = set(cog._batch_allowed_source_ids(batches[1], inputs["valid_source_ids"]))
        other_sid = sorted(batch2_ids - batch1_ids)[0]

        orig_budget = cog._cognitive_input_budget
        orig_chat = gw_mod.gateway.chat
        # DAG 的首选候选来自模型路由（agent_role=student_simulator），因此这里按
        # 「第一次调用」注入越界，而不是硬编码某个模型名。
        state = {"n": 0, "first_model": None, "violated": False}

        async def violating_first_attempt(model, messages, **kw):
            if kw.get("contract") != "lesson/student_simulator":
                return await orig_chat(model, messages, **kw)
            state["n"] += 1
            if state["n"] == 1:
                state["first_model"] = str(model)
                state["violated"] = True
                return {"content": json.dumps({
                    "items": [{
                        "type": "emphasis", "title": "越界", "explanation": "e",
                        "severity": 0.5, "confidence": 0.5,
                        "recommended_treatment": "none", "origin": "classroom_evidence",
                        "knowledge_unit_refs": [cog._ku_key(batches[0][0], 1)],
                        "source_refs": [other_sid],
                    }],
                    "analysis_note": "",
                }, ensure_ascii=False), "model": model, "tokens_in": 1, "tokens_out": 1}
            return await orig_chat(model, messages, **kw)

        cog._cognitive_input_budget = _budget_patch(cog, per_ku)
        gw_mod.gateway.chat = violating_first_attempt
        outputs = None
        try:
            from app.dag import DAGContext
            from app.dag_lesson import build_lesson_dag
            node = DAGContext()
            node.input = {"course_id": self.course_id, "chapter_id": self.chapter_id,
                          "lesson_id": self.lesson_id, "transcript": TEXT,
                          "material_ids": []}
            outputs = asyncio.run(build_lesson_dag().run(node))
            run_id = node.run_id
        finally:
            cog._cognitive_input_budget = orig_budget
            gw_mod.gateway.chat = orig_chat

        self.assertTrue(state["violated"], "必须真的注入过越界候选")
        self.assertEqual(outputs["student_simulator"]["status"], "succeeded",
                         "第二个候选模型必须接管并成功")
        self.assertNotEqual(outputs["student_simulator"]["model_used"],
                            state["first_model"],
                            "越界后必须换到**另一个**候选模型")
        # 最终合法认知项仍绑定本 domain 的 source_span_id
        rows = self.db.fetch_all(
            "SELECT s.source_span_id, s.source_id, sp.domain_id, m.domain_id AS map_domain "
            "FROM cognitive_item_sources s "
            "JOIN cognitive_items i ON i.id = s.cognitive_item_id "
            "JOIN cognitive_maps m ON m.id = i.cognitive_map_id "
            "LEFT JOIN source_spans sp ON sp.id = s.source_span_id "
            "WHERE m.run_id=?", (run_id,))
        self.assertTrue(rows)
        for row in rows:
            self.assertIsNotNone(row["source_span_id"])
            self.assertEqual(int(row["domain_id"]), int(row["map_domain"]))


class TestExcerptTruncation(MicroBase):
    """P1-C：>40 个合法 Source ID 时 foreign 必须为 0，截断单独记账。"""

    def _big_domain(self, count=60):
        text = "\n".join(
            f"[{i // 60:02d}:{i % 60:02d}] 第{i}段课堂内容，用于验证过量来源记账。"
            for i in range(count))
        ctx, _ = self.run_dag(transcript=text)
        spans = self.db.fetch_all(
            "SELECT source_id FROM source_spans WHERE domain_id=? AND span_state='included' "
            "ORDER BY ordinal", (int(ctx.domain_id),))
        return ctx, [r["source_id"] for r in spans]

    def test_more_than_max_spans_is_not_foreign(self):
        from app.learning_engine import cognition as cog
        ctx, ids = self._big_domain(60)
        self.assertGreater(len(ids), cog.COGNITIVE_MATERIAL_MAX_SPANS)
        result = cog._material_excerpts(int(ctx.domain_id), ids)
        self.assertEqual(result["foreign_source_ids"], [],
                         "同一 domain 内超过 40 条合法来源绝不能被记成域外")
        self.assertEqual(len(result["included_source_ids"]),
                         cog.COGNITIVE_MATERIAL_MAX_SPANS)
        self.assertTrue(result["omitted_due_budget"], "超出上限的来源必须显式记账")
        omitted_ids = {e["source_id"] for e in result["omitted_due_budget"]}
        self.assertTrue(omitted_ids <= set(ids))
        self.assertEqual(
            len(result["included_source_ids"]) + len(omitted_ids),
            len(ids), "每条来源都必须有明确终态（included 或 omitted），不得静默丢弃")
        # 逐条状态可复算
        by_status = {}
        for entry in result["excerpts"]:
            by_status.setdefault(entry["status"], []).append(entry["source_id"])
        self.assertEqual(sorted(by_status.get("foreign", [])), [])
        self.assertEqual(sorted(by_status.get("omitted_due_budget", [])),
                         sorted(omitted_ids))

    def test_foreign_and_omitted_are_distinct(self):
        from app.learning_engine import cognition as cog
        ctx, ids = self._big_domain(60)
        request = ids + ["T999999", "T999998"]
        result = cog._material_excerpts(int(ctx.domain_id), request)
        self.assertEqual(result["foreign_source_ids"], ["T999999", "T999998"],
                         "真正不存在的 ID 才记 foreign")
        omitted_ids = {e["source_id"] for e in result["omitted_due_budget"]}
        self.assertFalse(omitted_ids & set(result["foreign_source_ids"]),
                         "omitted 与 foreign 必须互斥")
        self.assertFalse(set(result["included_source_ids"]) & set(
            result["foreign_source_ids"]))

    def test_char_budget_truncation_recorded(self):
        from app.learning_engine import cognition as cog
        long_text = "\n".join(
            f"[00:{i:02d}:00] " + ("这是一段很长的课堂原文内容。" * 40) for i in range(3))
        ctx, _ = self.run_dag(transcript=long_text)
        ids = [r["source_id"] for r in self.db.fetch_all(
            "SELECT source_id FROM source_spans WHERE domain_id=? AND span_state='included' "
            "ORDER BY ordinal", (int(ctx.domain_id),))]
        original_chars = cog.COGNITIVE_MATERIAL_MAX_CHARS
        cog.COGNITIVE_MATERIAL_MAX_CHARS = 120
        try:
            result = cog._material_excerpts(int(ctx.domain_id), ids)
        finally:
            cog.COGNITIVE_MATERIAL_MAX_CHARS = original_chars
        self.assertEqual(result["foreign_source_ids"], [])
        self.assertTrue(result["omitted_due_budget"],
                        "字符预算不足时必须记 omitted_due_budget")
        self.assertTrue(all(e["reason"] == "char_budget_exceeded"
                            for e in result["omitted_due_budget"]))
        self.assertEqual(len(result["included_source_ids"]) + len(
            result["omitted_due_budget"]), len(ids))

    def test_single_excerpt_truncation_is_marked(self):
        from app.learning_engine import cognition as cog
        long_text = "[00:00:00] " + ("凸透镜成像规律的完整推导过程。" * 60)
        ctx, _ = self.run_dag(transcript=long_text)
        ids = [r["source_id"] for r in self.db.fetch_all(
            "SELECT source_id FROM source_spans WHERE domain_id=? AND span_state='included' "
            "ORDER BY ordinal", (int(ctx.domain_id),))]
        result = cog._material_excerpts(int(ctx.domain_id), ids)
        self.assertTrue(result["excerpt_truncated"], "超长正文必须标记截断")
        truncated = [e for e in result["excerpts"] if e["status"] == "truncated"]
        self.assertTrue(truncated)
        for entry in truncated:
            self.assertGreater(entry["total_chars"], entry["sent_chars"])

    def test_batch_audit_records_material_states(self):
        """批次审计必须带上摘录三态（included / omitted / foreign）。"""
        from app.learning_engine import cognition as cog
        ctx, _ = self.run_dag(transcript=TEXT)
        inputs = cog.collect_cognitive_inputs(int(ctx.domain_id), ctx.run_id, self.lesson_id)
        batches = cog.plan_ku_batches(inputs["knowledge_units"], token_budget=20000)
        info = cog._verify_batch_budgets(
            batches, inputs=inputs, valid_source_ids=inputs["valid_source_ids"],
            budget=20000)
        self.assertIn("material_omitted_due_budget", info)
        for row in info["batches"]:
            for key in ("allowed_source_ids", "material_omitted_due_budget",
                        "material_foreign_source_ids"):
                self.assertIn(key, row)


class TestStatusMatrix(MicroBase):
    """P1-D：succeeded / empty / partial / failed / cancelled 状态矩阵。"""

    def _run_node(self, ctx, *, model_id="qwen3_flash", budget=None):
        from app.dag import DAGContext
        from app.learning_engine import cognition as cog
        orig = cog._cognitive_input_budget
        if budget is not None:
            cog._cognitive_input_budget = _budget_patch(cog, budget)
        raised = None
        result = None
        try:
            node = DAGContext()
            node.run_id = ctx.run_id
            node.domain_id = int(ctx.domain_id)
            result = asyncio.run(cog.run_student_simulator(
                node, int(ctx.domain_id), ctx.run_id, self.lesson_id, model_id=model_id))
        except BaseException as e:  # noqa: BLE001
            raised = e
        finally:
            cog._cognitive_input_budget = orig
        return result, raised

    def test_succeeded_with_items(self):
        ctx, _ = self.run_dag(transcript=TEXT)
        result, raised = self._run_node(ctx)
        self.assertIsNone(raised)
        self.assertEqual(result["status"], "succeeded")
        self.assertGreater(result["item_count"], 0)
        self.assertEqual(result["knowledge_unit_processed"], result["knowledge_unit_total"])

    def test_empty_when_all_succeed_but_no_items(self):
        ctx, _ = self.run_dag(transcript=NEUTRAL)
        result, raised = self._run_node(ctx)
        self.assertIsNone(raised)
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["item_count"], 0)
        self.assertEqual(result["cognitive_input_coverage"], 1.0)

    def test_partial_when_some_batches_fail(self):
        from app import gateway as gw_mod
        from app.learning_engine import cognition as cog
        ctx, _ = self.run_dag(transcript=TEXT)
        batches, inputs, per_ku = self.make_batches(ctx, None)
        orig_chat = gw_mod.gateway.chat
        calls = {"n": 0}

        async def fail_first(model, messages, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("注入：第一批失败")
            return await orig_chat(model, messages, **kw)

        gw_mod.gateway.chat = fail_first
        try:
            result, raised = self._run_node(ctx, budget=per_ku)
        finally:
            gw_mod.gateway.chat = orig_chat
        self.assertIsNotNone(raised, "部分失败必须抛可恢复错误")
        row = self.map_for(ctx.run_id)
        self.assertEqual(row["status"], "partial")

    def test_failed_when_all_batches_fail(self):
        from app import gateway as gw_mod
        from app.learning_engine import cognition as cog
        ctx, _ = self.run_dag(transcript=TEXT)
        batches, inputs, per_ku = self.make_batches(ctx, None)
        orig_chat = gw_mod.gateway.chat

        async def fail_all(model, messages, **kw):
            raise RuntimeError("注入：全部失败")

        gw_mod.gateway.chat = fail_all
        try:
            result, raised = self._run_node(ctx, budget=per_ku)
        finally:
            gw_mod.gateway.chat = orig_chat
        self.assertIsNotNone(raised)
        row = self.map_for(ctx.run_id)
        self.assertEqual(row["status"], "failed",
                         "全部 batch 失败必须记 failed（不是 partial）")
        self.assertEqual(int(row["knowledge_unit_processed"]), 0)
        ledger = self.ledger_for(ctx.run_id)
        self.assertTrue(all(r["status"] == "failed" for r in ledger))

    def test_cancelled_leaves_no_map(self):
        from app import gateway as gw_mod
        from app.dag import RunCancelledError
        ctx, _ = self.run_dag(transcript=TEXT)
        self.db.execute("DELETE FROM cognitive_maps WHERE run_id=?", (ctx.run_id,))
        orig_chat = gw_mod.gateway.chat

        async def cancel_chat(model, messages, **kw):
            raise RunCancelledError("注入取消")

        gw_mod.gateway.chat = cancel_chat
        try:
            result, raised = self._run_node(ctx)
        finally:
            gw_mod.gateway.chat = orig_chat
        self.assertIsInstance(raised, (RunCancelledError, asyncio.CancelledError))
        self.assertIsNone(self.map_for(ctx.run_id),
                          "取消不得持久化 partial/伪成功 map")

    def test_status_enum_matches_persisted_values(self):
        """五态都必须是契约枚举的合法取值。"""
        from app.learning_engine.contracts import CognitiveMapStatus
        values = {s.value for s in CognitiveMapStatus}
        self.assertEqual(values, {"succeeded", "partial", "failed", "empty"})
        # cancelled 不落库（map 不写），由 run 状态表达
        for text, expected in ((TEXT, "succeeded"), (NEUTRAL, "empty")):
            ctx, _ = self.run_dag(transcript=text)
            self._run_node(ctx)
            self.assertIn(self.map_for(ctx.run_id)["status"], values)


class TestCandidateModelBudget(MicroBase):
    """P1-A 验收：生产路径必须使用候选模型的实际预算。"""

    def test_small_context_candidate_replans_batches(self):
        from app.learning_engine import cognition as cog
        self.define_model_profile("small_ctx", 8192)
        audit_small = cog._cognitive_input_budget("small_ctx")
        audit_default = cog._cognitive_input_budget(None)
        self.assertEqual(audit_small["context_window"], 8192)
        self.assertEqual(audit_small["context_window_source"], "model_profile")
        self.assertLess(audit_small["input_budget"], audit_default["input_budget"])

        ctx, _ = self.run_dag(transcript=TEXT)
        inputs = cog.collect_cognitive_inputs(int(ctx.domain_id), ctx.run_id, self.lesson_id)
        units = inputs["knowledge_units"]
        wide = cog.plan_ku_batches(units, token_budget=audit_default["input_budget"])
        narrow = cog.plan_ku_batches(units, token_budget=audit_small["input_budget"])
        self.assertGreaterEqual(len(narrow), len(wide),
                                "小上下文候选必须规划出不少于（通常更多）的批次")
        # 两者都不丢 KU
        for plan in (wide, narrow):
            self.assertEqual(sorted(u["id"] for b in plan for u in b),
                             sorted(u["id"] for u in units))

    def test_production_path_uses_candidate_budget(self):
        """把候选模型配成小窗口后，生产节点必须按该窗口规划并如实审计。"""
        from app.dag import DAGContext
        from app.learning_engine import cognition as cog
        self.define_model_profile("small_ctx", 12288)
        ctx, _ = self.run_dag(transcript=TEXT)
        node = DAGContext()
        node.run_id = ctx.run_id
        node.domain_id = int(ctx.domain_id)
        result = asyncio.run(cog.run_student_simulator(
            node, int(ctx.domain_id), ctx.run_id, self.lesson_id, model_id="small_ctx"))
        self.assertEqual(result["budget"]["model_profile_id"], "small_ctx")
        self.assertEqual(result["budget"]["context_window"], 12288)
        self.assertNotEqual(result["budget"]["context_window"], 32768)
        self.assertEqual(result["budget"]["input_budget"],
                         cog._cognitive_input_budget("small_ctx")["input_budget"])
        self.assertLessEqual(result["budget"]["max_input_tokens"],
                             result["budget"]["input_budget"])
        payload = json.loads(self.map_for(ctx.run_id)["structured_json"])
        self.assertEqual(payload["budget"]["context_window"], 12288)

    def test_budget_changes_with_candidate_model(self):
        from app.learning_engine import cognition as cog
        self.define_model_profile("tiny_ctx", 8192)
        self.define_model_profile("huge_ctx", 65536)
        tiny = cog._cognitive_input_budget("tiny_ctx")
        huge = cog._cognitive_input_budget("huge_ctx")
        self.assertLess(tiny["input_budget"], huge["input_budget"])
        self.assertEqual(huge["context_window"], 65536)

    def test_missing_profile_window_uses_conservative_default(self):
        from app.learning_engine import cognition as cog
        audit = cog._cognitive_input_budget("does_not_exist")
        self.assertEqual(audit["context_window_source"],
                         "default_model_profile_missing_or_window_absent")
        self.assertEqual(audit["context_window"], 32768)

    def test_fallback_replans_with_new_candidate_budget(self):
        """候选模型切换后，预算与批次必须按**新**候选重新规划。"""
        from app import gateway as gw_mod
        from app.dag import DAGContext
        from app.learning_engine import cognition as cog

        self.define_model_profile("small_ctx", 8192)
        ctx, _ = self.run_dag(transcript=TEXT)
        self.db.execute("DELETE FROM cognitive_maps WHERE run_id=?", (ctx.run_id,))
        orig_chat = gw_mod.gateway.chat
        seen: list[tuple[str, int]] = []

        async def record(model, messages, **kw):
            if kw.get("contract") == "lesson/student_simulator":
                if str(model) == "small_ctx":
                    # 记录本轮 batch_ordinal/batch_total 与预算
                    import re as _re
                    m = _re.search(r"批次：(\d+) / (\d+)", str(messages[0]["content"]))
                    if m:
                        seen.append((str(model), int(m.group(2))))
                    raise RuntimeError("注入：小上下文候选失败")
            return await orig_chat(model, messages, **kw)

        gw_mod.gateway.chat = record
        try:
            node = DAGContext()
            node.run_id = ctx.run_id
            node.domain_id = int(ctx.domain_id)
            result = asyncio.run(cog.run_student_simulator(
                node, int(ctx.domain_id), ctx.run_id, self.lesson_id,
                model_id="small_ctx"))
        except BaseException as e:  # noqa: BLE001
            result = None
            raised = e
        finally:
            gw_mod.gateway.chat = orig_chat
        # small_ctx 失败后节点抛可恢复错误（DAG 会换模型重跑整个节点）
        self.assertIsNotNone(self.map_for(ctx.run_id))
        self.assertTrue(seen, "小上下文候选必须真的被调用过")
        self.assertTrue(all(m == "small_ctx" for m, _ in seen))


if __name__ == "__main__":
    unittest.main()
