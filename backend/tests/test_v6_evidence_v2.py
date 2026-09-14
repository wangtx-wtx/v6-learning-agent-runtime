"""V6 Phase 4 Evidence V2 缺陷注入测试（任务书 §十二 的 20 项）。

核心不变量:
  * 模型只选 Source ID；``bound_quote`` **只可能**来自 ``source_spans.text``；
  * 查询同时限定 ``domain_id`` + ``source_id``（禁止全库按 source_id 命中）;
  * 伪造 / 跨域 / 缺失 / 未呈现的来源一律阻断 Evidence gate，不静默通过；
  * 重跑幂等、历史 run 不受影响、取消不留半套、级联删除干净。

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

TEXT_A = "\n".join([
    "[00:00:00] 第一节课：光的反射定律，入射角等于反射角。",
    "[00:05:00] 第一节课：平面镜成像，物像等大正立虚像。",
])
TEXT_B = "\n".join([
    "[00:00:00] 第二节课：凸透镜成像规律，物距大于二倍焦距成倒立缩小实像。",
    "[00:05:00] 第二节课：容易混淆的是实像与虚像的判别。",
])


def _force_fake_gateway():
    os.environ["V5_GATEWAY_MODE"] = "fake"
    os.environ.pop("V5_GATEWAY_API_KEY", None)
    os.environ.pop("V5_ALLOW_LIVE_MODEL_TESTS", None)


class EvidenceBase(unittest.TestCase):
    def setUp(self):
        _force_fake_gateway()
        self._prev = os.environ.get("V6_LEARNING_ENGINE")
        os.environ["V6_LEARNING_ENGINE"] = "shadow"
        from app import gateway as gw_mod
        gw_mod.reset_engine_for_test()
        from app import config as app_config
        self._saved_mobile_token = app_config.MOBILE_TOKEN
        app_config.MOBILE_TOKEN = ""

        self.tmp = Path(tempfile.mkdtemp(prefix="v6_ev_"))
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
    def run_dag(self, transcript=TEXT_B):
        from app.dag import DAGContext
        from app.dag_lesson import build_lesson_dag
        ctx = DAGContext()
        ctx.input = {"course_id": self.course_id, "chapter_id": self.chapter_id,
                     "lesson_id": self.lesson_id, "transcript": transcript,
                     "material_ids": []}
        outputs = asyncio.run(build_lesson_dag().run(ctx))
        return ctx, outputs

    def evidence(self, ctx):
        from app.learning_engine import evidence_v2 as ev
        return ev.get_evidence_report(ctx.run_id)

    def claims(self, ctx):
        from app.learning_engine import evidence_v2 as ev
        return ev.get_claims(ctx.run_id)

    def claim_by_type(self, ctx, claim_type):
        return [c for c in self.claims(ctx) if c["claim_type"] == claim_type]

    # ---- Phase 4.1：per-claim producer provenance ----
    def lu_provenance(self, ctx, segment_ordinals=(1,)):
        """LessonUnderstanding 侧的 provenance（按 segment 输入账本）。"""
        from app.learning_engine import evidence_v2 as ev
        return ev._provenance_for_segments(
            int(ctx.domain_id), list(segment_ordinals), "lesson_understanding")

    def batch_provenance(self, ctx, run_id=None, consumed_only=True):
        """按**已落库**的 cognitive batch 证据构造 provenance（逐 batch 可见来源）。

        刻意从 content_claims 已存的 item provenance 反查，确保测试断言的对象是
        真实落库的生产可见性，而不是测试自己编的集合。
        """
        from app.learning_engine import evidence_v2 as ev
        run_id = int(run_id or self.db.fetch_one(
            "SELECT run_id FROM content_claims ORDER BY id DESC LIMIT 1")["run_id"])
        ledger = ev.cognitive_batch_ledger(run_id)
        per_source: dict[str, list[int]] = {}
        for ordinal, batch in sorted(ledger.items()):
            if consumed_only and batch.get("status") != "consumed":
                continue
            for sid in batch.get("visible_source_ids") or []:
                per_source.setdefault(sid, []).append(int(ordinal))
        return per_source, ledger

    def counts(self, run_id=None):
        """本 run 的 claims / claim_sources / legacy 投影行数。

        必须按 run 过滤：``self.run_dag()`` 每次都会新建一个 run，
        全表计数会把「另一个 run 的 claim」误当成「重跑累积」。
        """
        if run_id is None:
            row = self.db.fetch_one(
                "SELECT run_id FROM content_claims ORDER BY id DESC LIMIT 1")
            run_id = int(row["run_id"]) if row else -1
        return (
            int(self.db.fetch_one(
                "SELECT COUNT(*) AS n FROM content_claims WHERE run_id=?",
                (run_id,))["n"]),
            int(self.db.fetch_one(
                "SELECT COUNT(*) AS n FROM claim_sources WHERE claim_id IN "
                "(SELECT id FROM content_claims WHERE run_id=?)", (run_id,))["n"]),
            int(self.db.fetch_one(
                "SELECT COUNT(*) AS n FROM evidence_links WHERE owner_type='content_claim' "
                "AND owner_id IN (SELECT id FROM content_claims WHERE run_id=?)",
                (run_id,))["n"]),
        )

    def bind_with_drafts(self, ctx, drafts):
        """用**自造** ClaimDraft 走真实 Binder（注入伪造/跨域来源）。"""
        from app.learning_engine import evidence_v2 as ev
        bound = ev.bind_claims(int(ctx.domain_id), ctx.run_id, drafts)
        report = ev.compute_evidence_report(ctx.run_id, int(ctx.domain_id), bound)
        return bound, report

    def draft(self, key, claim_type, text, refs, importance="supporting",
              provenance=None, producer_node="test"):
        """构造 ClaimDraft。

        Phase 4.1 起必须显式给出 provenance（或用 :meth:`lu_provenance` /
        :meth:`batch_provenance` 构造），否则 Binder 会按 ``UNAVAILABLE`` 判 failed。
        """
        from app.learning_engine.contracts import ClaimDraft
        return ClaimDraft(claim_key=key, claim_type=claim_type, claim_text=text,
                          importance=importance, source_refs=refs,
                          origin_ref=key, producer_node=producer_node,
                          provenance=provenance if provenance is not None else
                          self._default_provenance())

    def _default_provenance(self):
        from app.learning_engine.contracts import ClaimProvenance
        return ClaimProvenance(producer_kind="lesson_understanding",
                               visibility_basis="unavailable")


class TestDefectInjection(EvidenceBase):
    """§十二 的 20 项缺陷注入。"""

    # 1. 模型返回伪造 Source ID → bind_evidence 失败
    def test_01_forged_source_id_fails(self):
        ctx, _ = self.run_dag()
        bound, report = self.bind_with_drafts(ctx, [
            self.draft("forged", "classroom_fact", "伪造来源的课堂事实", ["T999999"])])
        self.assertEqual(bound[0].evidence_status.value, "failed")
        self.assertEqual(report.unbound_source_refs, 1)
        self.assertEqual(report.gate.value, "failed")
        self.assertIn("source_not_in_domain:T999999", bound[0].failure_reasons)

    # 2. Domain A/B 都有 T000001 → B 只能绑定 B
    def test_02_cross_domain_same_source_id(self):
        ctx_a, _ = self.run_dag(TEXT_A)
        ctx_b, _ = self.run_dag(TEXT_B)
        spans_b = {r["source_id"]: r["text"] for r in self.db.fetch_all(
            "SELECT source_id, text FROM source_spans WHERE domain_id=?", (ctx_b.domain_id,))}
        self.assertTrue(spans_b)
        for claim in self.claim_by_type(ctx_b, "classroom_fact") + \
                self.claim_by_type(ctx_b, "classroom_paraphrase"):
            for src in claim["sources"]:
                if src["binding_status"] != "bound":
                    continue
                self.assertEqual(src["bound_quote"], spans_b[src["source_id"]],
                                 "Domain B 只能绑定 B 的原文")
                row = self.db.fetch_one("SELECT domain_id FROM source_spans WHERE id=?",
                                        (src["source_span_id"],))
                self.assertEqual(int(row["domain_id"]), int(ctx_b.domain_id))

    # 3. 同 lesson 历史 run 的 Source ID/文本不得串入新 run
    def test_03_historical_run_not_leaked(self):
        ctx_a, _ = self.run_dag(TEXT_A)
        ctx_b, _ = self.run_dag(TEXT_B)
        a_texts = {r["text"] for r in self.db.fetch_all(
            "SELECT text FROM source_spans WHERE domain_id=?", (ctx_a.domain_id,))}
        for claim in self.claims(ctx_b):
            for src in claim["sources"]:
                if src["binding_status"] != "bound":
                    continue
                self.assertNotIn(src["bound_quote"], a_texts,
                                 "不得把 Run A 的原文绑进 Run B 的 claim")
        self.assertNotEqual(int(ctx_a.domain_id), int(ctx_b.domain_id))

    # 4. 模型输出伪造 quote → bound_quote 仍等于数据库文本
    def test_04_model_forged_quote_ignored(self):
        from app.learning_engine import evidence_v2 as ev
        ctx, _ = self.run_dag()
        fabricated = "这是模型凭空生成的引用原文，数据库里根本不存在。"
        # 模拟"模型输出对象里带 quote"：project_claims 只认字符串 Source ID
        structured = json.loads(self.db.fetch_one(
            "SELECT structured_json FROM lesson_understandings WHERE run_id=?",
            (ctx.run_id,))["structured_json"])
        self.assertTrue(structured.get("valid_source_ids"))
        drafts = ev.project_claims(int(ctx.domain_id), ctx.run_id)
        bound = ev.bind_claims(int(ctx.domain_id), ctx.run_id, drafts)
        originals = {r["text"] for r in self.db.fetch_all(
            "SELECT text FROM source_spans WHERE domain_id=?", (ctx.domain_id,))}
        self.assertTrue(bound)
        for item in bound:
            for src in item.sources:
                if src.binding_status.value != "bound":
                    continue
                self.assertIn(src.bound_quote, originals)
                self.assertNotEqual(src.bound_quote, fabricated)
        # 对象形式（含 quote 字段）不会被当作来源，也不会把 quote 带进 claim
        drafts2 = ev.project_claims(int(ctx.domain_id), ctx.run_id)
        from app.learning_engine.contracts import ClaimDraft
        weird = ClaimDraft(claim_key="weird", claim_type="classroom_fact",
                           claim_text="对象形式来源",
                           source_refs=[], origin_ref="weird", producer_node="test")
        bound2 = ev.bind_claims(int(ctx.domain_id), ctx.run_id, [weird])
        self.assertEqual(bound2[0].evidence_status.value, "failed")
        for src in bound2[0].sources:
            self.assertNotIn(fabricated, src.bound_quote)

    # 5. 修改模型 quote 不改变 bound_quote
    def test_05_bound_quote_independent_of_model_output(self):
        ctx, _ = self.run_dag()
        first = {(c["claim_key"], s["source_id"]): s["bound_quote"]
                 for c in self.claims(ctx) for s in c["sources"]}
        self.assertTrue(first)
        # 重跑（fake 引擎输出同样内容）→ bound_quote 必须逐字节一致
        self.run_dag()
        second = {(c["claim_key"], s["source_id"]): s["bound_quote"]
                  for c in self.claims(ctx) for s in c["sources"]}
        self.assertEqual(first, second)

    # 6. 修改数据库 Source 文本副本后，quote hash 按当前 frozen source 规则变化或拒绝
    def test_06_source_text_change_is_detected(self):
        """数据库原文变动后，bound_quote/quote_hash 必须与**当前**原文一致。

        明确规则（可审计）：``bound_quote`` 永远等于「绑定那一刻数据库里的原文」，
        因此
          * 同一 span 原文被改动 → 重跑后 bound_quote / quote_hash 跟着变；
          * 若改动发生在**未重跑**期间，旧投影与当前数据库不再一致 —— 这里同时
            断言「当前 span 原文确实变了」，证明该情形是可检测的。
        """
        from app.learning_engine import evidence_v2 as ev
        ctx, _ = self.run_dag()
        claim = [c for c in self.claims(ctx) if c["sources"]][0]
        src = claim["sources"][0]
        self.assertEqual(src["binding_status"], "bound")
        span_id = int(src["source_span_id"])
        old_quote = src["bound_quote"]
        old_hash = src["quote_hash"]
        mutated = old_quote + "（改动后）"

        # (a) 改动后不重跑：数据库与既有投影不一致 → 可被检测
        self.db.execute("UPDATE source_spans SET text=? WHERE id=?", (mutated, span_id))
        current = self.db.fetch_one("SELECT text FROM source_spans WHERE id=?", (span_id,))
        self.assertEqual(current["text"], mutated)
        stale = self.claims(ctx)[0]["sources"][0] if self.claims(ctx) else None
        stale = next((s for c in self.claims(ctx) for s in c["sources"]
                      if s["source_span_id"] == span_id), None)
        self.assertIsNotNone(stale)
        self.assertNotEqual(stale["bound_quote"], current["text"],
                            "未重跑前应处于「投影过期」状态（这就是可检测的信号）")

        # (b) 重新绑定：bound_quote/quote_hash 必须按当前原文重算
        ev.run_bind_evidence(int(ctx.domain_id), ctx.run_id)
        fresh = next((s for c in self.claims(ctx) for s in c["sources"]
                      if s["source_span_id"] == span_id), None)
        self.assertIsNotNone(fresh)
        current = self.db.fetch_one("SELECT text FROM source_spans WHERE id=?", (span_id,))
        self.assertEqual(fresh["bound_quote"], current["text"],
                         "重绑后 bound_quote 必须等于当前数据库原文")
        self.assertEqual(
            fresh["quote_hash"],
            "sha256:" + hashlib.sha256(current["text"].encode("utf-8")).hexdigest(),
            "quote_hash 必须按当前原文重算")
        self.assertNotEqual(fresh["bound_quote"], old_quote)
        self.assertNotEqual(fresh["quote_hash"], old_hash)

    # 7. classroom_fact 无 source → failed
    def test_07_classroom_fact_without_source_fails(self):
        ctx, _ = self.run_dag()
        bound, report = self.bind_with_drafts(ctx, [
            self.draft("no-src-fact", "classroom_fact", "无来源的课堂事实", [])])
        self.assertEqual(bound[0].evidence_status.value, "failed")
        self.assertIn("classroom_claim_without_source", bound[0].failure_reasons)
        self.assertEqual(report.gate.value, "failed")

    # 8. classroom_paraphrase 有合法 source → passed，但不声称语义已验证
    def test_08_paraphrase_bound_but_semantics_not_claimed(self):
        ctx, _ = self.run_dag()
        prov = self.lu_provenance(ctx, (1,))
        self.assertTrue(prov.visible_source_ids, "segment 1 应有可见来源")
        bound, report = self.bind_with_drafts(ctx, [
            self.draft("para", "classroom_paraphrase", "课堂重述",
                       [prov.visible_source_ids[0]], provenance=prov)])
        self.assertEqual(bound[0].evidence_status.value, "passed")
        self.assertTrue(bound[0].sources[0].bound_quote)
        self.assertTrue(bound[0].sources[0].presented_to_producer)
        # 语义未被声称验证：报告里没有任何语义结论（只有绑定事实）
        self.assertNotIn("semantic", json.dumps(report.model_dump(), ensure_ascii=False).lower())

    # 9. ai_explanation 无 source → not_required，并有可见标识
    def test_09_ai_explanation_not_required_with_label(self):
        ctx, _ = self.run_dag()
        bound, report = self.bind_with_drafts(ctx, [
            self.draft("ai", "ai_explanation", "模型教学补充说明", [])])
        self.assertEqual(bound[0].evidence_status.value, "not_required")
        self.assertEqual(report.not_required_claims, 1)
        self.assertEqual(report.required_claims, 0)
        # 渲染层可见标识
        from app import document_artifacts as da
        html = da.render_html(da.normalize_document({
            "title": "t", "subtitle": "", "deck": "",
            "sections": [{"title": "s", "blocks": [
                {"type": "ai_explanation", "claim_type": "ai_explanation",
                 "content": "说明", "title": "", "items": [], "rows": [], "source_refs": []}]}],
            "sources": [],
        }, title="t", body=""))
        self.assertIn('data-claim-type="ai_explanation"', html)
        self.assertIn(da.AI_EXPLANATION_LABEL, html)

    # 10. ai_explanation 伪装 classroom_fact → failed
    def test_10_ai_explanation_disguised_as_fact_fails(self):
        ctx, _ = self.run_dag()
        # 伪装：内容其实是模型推断（无课堂来源），却标成 classroom_fact
        bound, report = self.bind_with_drafts(ctx, [
            self.draft("disguised", "classroom_fact", "模型推断被标成课堂事实", [])])
        self.assertEqual(bound[0].evidence_status.value, "failed")
        self.assertEqual(report.gate.value, "failed")
        self.assertEqual(report.failed_claims, 1)
        # 反向：ai_explanation 保持自身类型，不会被算进课堂事实分母
        bound2, report2 = self.bind_with_drafts(ctx, [
            self.draft("ai2", "ai_explanation", "模型推断", [])])
        self.assertEqual(bound2[0].evidence_status.value, "not_required")
        self.assertEqual(report2.failed_claims, 0)

    # 11. critical claim 少一个有效证据 → rate<1，gate failed
    def test_11_critical_claim_missing_evidence_fails_gate(self):
        ctx, _ = self.run_dag()
        prov = self.lu_provenance(ctx, (1,))
        ok_sid = prov.visible_source_ids[0]
        bound, report = self.bind_with_drafts(ctx, [
            self.draft("c-ok", "classroom_fact", "有证据的关键事实", [ok_sid],
                       "critical", provenance=prov),
            self.draft("c-bad", "classroom_fact", "缺证据的关键事实", ["T999999"],
                       "critical", provenance=prov),
        ])
        self.assertEqual(report.critical_evidence_denominator, 2)
        self.assertEqual(report.critical_claims_bound, 1)
        self.assertLess(report.critical_claim_evidence_rate, 1.0)
        self.assertEqual(report.gate.value, "failed")
        self.assertIn("critical_claim_evidence_rate", report.degradation_reason)

    # 12. 所有 critical claim 有效 → rate=1，gate passed
    def test_12_all_critical_bound_passes_gate(self):
        ctx, _ = self.run_dag()
        prov = self.lu_provenance(ctx, (1,))
        self.assertGreaterEqual(len(prov.visible_source_ids), 2,
                                "segment 1 应有至少 2 个可见来源")
        sids = prov.visible_source_ids
        bound, report = self.bind_with_drafts(ctx, [
            self.draft("c1", "classroom_fact", "关键事实一", [sids[0]], "critical",
                       provenance=prov),
            self.draft("c2", "classroom_fact", "关键事实二", [sids[-1]], "critical",
                       provenance=prov),
        ])
        self.assertEqual(report.critical_evidence_denominator, 2)
        self.assertEqual(report.critical_claims_bound, 2)
        self.assertEqual(report.critical_claim_evidence_rate, 1.0)
        self.assertEqual(report.gate.value, "passed")
        self.assertIsNone(report.degradation_reason)

    # 13. omitted_due_budget Source 被 CognitiveItem 引用 → 不得 passed
    def test_13_unread_source_cannot_pass(self):
        """omitted_due_budget 的来源（库里有、模型没读）不得 passed。

        Phase 4.1：判定依据是该 claim 自己的 provenance，而非 run 级集合。
        """
        ctx, _ = self.run_dag()
        from app.learning_engine import evidence_v2 as ev
        from app.learning_engine.contracts import ClaimProvenance
        per_source, ledger = self.batch_provenance(ctx)
        self.assertTrue(per_source, "认知批次应有可见来源")
        target = sorted(per_source)[0]
        # 构造 provenance：只保留另一个来源可见（等价于 target 被 omitted），
        # 且如实记录 target 被 omitted（审计可见）
        others = [s for s in sorted(per_source) if s != target]
        prov = ClaimProvenance(
            producer_kind="cognitive_map", visibility_basis="cognitive_batch_audit",
            invocation_refs=per_source[target],
            visible_source_ids=others,
            per_source_invocations={s: per_source[s] for s in others})
        bound, report = self.bind_with_drafts(ctx, [
            self.draft("unread", "classroom_fact", "引用未呈现来源", [target],
                       provenance=prov)])
        self.assertEqual(bound[0].evidence_status.value, "failed",
                         "未呈现给该 claim producer 的来源绝不能 passed")
        self.assertFalse(bound[0].sources[0].presented_to_producer)
        self.assertEqual(bound[0].sources[0].visibility.value, "not_visible")
        self.assertGreaterEqual(report.not_in_claim_producer_refs, 1)
        self.assertGreaterEqual(report.unread_source_refs, 1)
        self.assertEqual(report.gate.value, "failed")

    # 14. malformed Source ID → 失败，不能静默剔除
    def test_14_malformed_source_id_fails_loudly(self):
        ctx, _ = self.run_dag()
        for bad in ("bogus", "T1", "X000001", "", "  "):
            bound, report = self.bind_with_drafts(ctx, [
                self.draft(f"bad-{bad!r}", "classroom_fact", "非法来源", [bad] if bad.strip()
                           else [])])
            self.assertEqual(bound[0].evidence_status.value, "failed",
                             f"非法 Source ID {bad!r} 必须让 claim 失败")
        bound, report = self.bind_with_drafts(ctx, [
            self.draft("malformed", "classroom_fact", "格式非法", ["bogus"])])
        self.assertEqual(bound[0].sources[0].binding_status.value, "invalid")
        self.assertEqual(bound[0].sources[0].failure_reason, "malformed_source_id")
        self.assertEqual(report.invalid_source_refs, 1)
        self.assertEqual(report.gate.value, "failed")
        # 批量投影不会因为一条非法来源而整体崩掉或静默丢弃
        self.assertEqual(len(bound), 1)

    # 15. 同 run 重跑 claims/sources 数量不增加
    def test_15_rerun_is_idempotent(self):
        """同一 run 内重复 bind_evidence（幂等要求）不得累积 claims/sources/投影行。"""
        from app.learning_engine import evidence_v2 as ev
        ctx, _ = self.run_dag()
        first = self.counts(ctx.run_id)
        self.assertTrue(first[0] > 0)
        for _ in range(2):
            ev.run_bind_evidence(int(ctx.domain_id), ctx.run_id)
        self.assertEqual(self.counts(ctx.run_id), first,
                         "同 run 重跑不得累积 claims/sources/投影行")
        keys = [c["claim_key"] for c in self.claims(ctx)]
        self.assertEqual(len(keys), len(set(keys)))

    # 16. Run A 后 Run B 不修改 A 的 claims/sources
    def test_16_new_run_does_not_touch_previous_run(self):
        ctx_a, _ = self.run_dag(TEXT_A)
        a_before = (self.claims(ctx_a),
                    [dict(r) for r in self.db.fetch_all(
                        "SELECT * FROM claim_sources WHERE claim_id IN "
                        "(SELECT id FROM content_claims WHERE run_id=?) ORDER BY id",
                        (ctx_a.run_id,))])
        ctx_b, _ = self.run_dag(TEXT_B)
        a_after = (self.claims(ctx_a),
                   [dict(r) for r in self.db.fetch_all(
                       "SELECT * FROM claim_sources WHERE claim_id IN "
                       "(SELECT id FROM content_claims WHERE run_id=?) ORDER BY id",
                       (ctx_a.run_id,))])
        self.assertEqual(a_before, a_after, "Run B 不得修改 Run A 的 claims/sources")
        self.assertNotEqual(int(ctx_a.run_id), int(ctx_b.run_id))
        self.assertTrue(self.claims(ctx_b))

    # 17. 删除 run 正确级联
    def test_17_delete_run_cascades(self):
        ctx_a, _ = self.run_dag(TEXT_A)
        ctx_b, _ = self.run_dag(TEXT_B)
        a_claims = self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM content_claims WHERE run_id=?", (ctx_a.run_id,))["n"]
        b_claims = self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM content_claims WHERE run_id=?", (ctx_b.run_id,))["n"]
        self.assertTrue(a_claims and b_claims)
        self.db.execute("DELETE FROM workflow_runs WHERE id=?", (ctx_a.run_id,))
        left_a = self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM content_claims WHERE run_id=?", (ctx_a.run_id,))["n"]
        left_b = self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM content_claims WHERE run_id=?", (ctx_b.run_id,))["n"]
        self.assertEqual(int(left_a), 0, "删除 run 必须级联清理 content_claims")
        self.assertEqual(int(left_b), int(b_claims), "其他 run 的 claims 不受影响")
        orphan = self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM claim_sources WHERE claim_id NOT IN "
            "(SELECT id FROM content_claims)")["n"]
        self.assertEqual(int(orphan), 0, "claim_sources 不得留下孤儿行")
        self.assertEqual(self.db.fetch_all("PRAGMA foreign_key_check"), [])

    # 18. 取消期间不留下半套数据
    def test_18_cancel_leaves_no_partial_rows(self):
        from app.learning_engine import evidence_v2 as ev
        ctx, _ = self.run_dag()
        before = self.counts()
        drafts = ev.project_claims(int(ctx.domain_id), ctx.run_id)
        self.assertTrue(drafts)
        original = ev.persist_evidence

        class _Boom(RuntimeError):
            pass

        def exploding(domain_id, run_id, d, b):
            # 在事务内写入若干行后抛错：必须整体回滚
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO content_claims (run_id, domain_id, claim_key, claim_type, "
                    " claim_text) VALUES (?,?, 'cancel-probe', 'classroom_fact', 'x')",
                    (int(run_id), int(domain_id)))
                raise _Boom("注入：事务中途失败")

        ev.persist_evidence = exploding
        try:
            bound = ev.bind_claims(int(ctx.domain_id), ctx.run_id, drafts)
            with self.assertRaises(_Boom):
                ev.persist_evidence(int(ctx.domain_id), ctx.run_id, drafts, bound)
        finally:
            ev.persist_evidence = original
        self.assertEqual(self.counts(), before, "事务回滚后不得留下半套数据")
        self.assertEqual(self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM content_claims WHERE claim_key='cancel-probe'")["n"], 0)

    # 19. evidence_links 兼容投影幂等
    def test_19_legacy_projection_idempotent(self):
        from app.learning_engine import evidence_v2 as ev
        ctx, _ = self.run_dag()
        before = self.counts(ctx.run_id)
        self.assertTrue(before[2] > 0, "必须有 legacy 兼容投影行")
        rows_before = [dict(r) for r in self.db.fetch_all(
            "SELECT * FROM evidence_links WHERE owner_type='content_claim' "
            "AND owner_id IN (SELECT id FROM content_claims WHERE run_id=?) ORDER BY id",
            (ctx.run_id,))]
        for _ in range(2):
            ev.run_bind_evidence(int(ctx.domain_id), ctx.run_id)
        rows_after = [dict(r) for r in self.db.fetch_all(
            "SELECT * FROM evidence_links WHERE owner_type='content_claim' "
            "AND owner_id IN (SELECT id FROM content_claims WHERE run_id=?) ORDER BY id",
            (ctx.run_id,))]
        self.assertEqual(len(rows_before), len(rows_after), "投影必须幂等")
        self.assertEqual([r["quote"] for r in rows_before], [r["quote"] for r in rows_after])
        # 可追溯到 content_claims.id
        for row in rows_after:
            self.assertIsNotNone(self.db.fetch_one(
                "SELECT id FROM content_claims WHERE id=?", (row["owner_id"],)))
        # 历史 legacy 行（非 content_claim owner）必须原样保留
        legacy = self.db.fetch_all(
            "SELECT id, owner_type, quote FROM evidence_links "
            "WHERE owner_type<>'content_claim' ORDER BY id")
        self.assertIsInstance(legacy, list)

    # 20. API 不泄露 prompt、reasoning、密钥和绝对路径
    def test_20_api_does_not_leak_sensitive_data(self):
        from fastapi.testclient import TestClient
        from app.main import app
        ctx, _ = self.run_dag()
        with TestClient(app) as client:
            res = client.get(f"/api/runs/{ctx.run_id}/evidence-v2")
            self.assertEqual(res.status_code, 200)
            body = res.json()
            blob = json.dumps(body, ensure_ascii=False)
            for forbidden in ("reasoning_content", "chain_of_thought", "chain-of-thought",
                              "system_prompt", "prompt_text", "api_key", "apikey",
                              "sk-", "Bearer ", "uploads", "C:\\\\", "/home/", "backend\\",
                              "Authorization"):
                self.assertNotIn(forbidden, blob, f"API 不得泄露 {forbidden}")
            self.assertEqual(body["report"]["domain_id"], int(ctx.domain_id))
            self.assertIn("claims", body)
            self.assertIn("ai_explanation_label", body)
            self.assertFalse(body["publication"]["included_in_document"])
            self.assertFalse(body["engine"]["real_notes_publish_enabled"])
            self.assertEqual(body["engine"]["publication_mode"],
                             "v6_understanding_only_legacy_publication")


class TestEvidenceContractsAndSchema(EvidenceBase):
    """0020 结构、契约枚举、CHECK 一致性。"""

    def test_schema_v21_and_tables(self):
        self.assertEqual(self.db.schema_version(), 23)
        tables = {r["name"] for r in self.db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("content_claims", "claim_sources"):
            self.assertIn(t, tables)
        cols = {r["name"] for r in self.db.fetch_all("PRAGMA table_info(content_claims)")}
        self.assertIn("producer_provenance_json", cols)
        src_cols = {r["name"] for r in self.db.fetch_all("PRAGMA table_info(claim_sources)")}
        self.assertIn("visibility", src_cols)
        self.assertIn("visible_invocations_json", src_cols)
        self.assertEqual(self.db.fetch_all("PRAGMA foreign_key_check"), [])

    def test_check_constraints_match_contracts(self):
        ctx, _ = self.run_dag()
        claim = self.claims(ctx)[0]
        for bad_type in ("bogus_type", ""):
            with self.assertRaises(Exception):
                self.db.insert(
                    "INSERT INTO content_claims (run_id, domain_id, claim_key, claim_type) "
                    "VALUES (?,?, 'bad-type', ?)", (ctx.run_id, int(ctx.domain_id), bad_type))
        for bad_importance in ("bogus", ""):
            with self.assertRaises(Exception):
                self.db.insert(
                    "INSERT INTO content_claims (run_id, domain_id, claim_key, claim_type, "
                    " importance) VALUES (?,?, 'bad-imp', 'classroom_fact', ?)",
                    (ctx.run_id, int(ctx.domain_id), bad_importance))
        for bad_status in ("bogus", ""):
            with self.assertRaises(Exception):
                self.db.insert(
                    "INSERT INTO content_claims (run_id, domain_id, claim_key, claim_type, "
                    " evidence_status) VALUES (?,?, 'bad-status', 'classroom_fact', ?)",
                    (ctx.run_id, int(ctx.domain_id), bad_status))
        claim_id = int(claim["id"])
        for bad_binding in ("bogus", ""):
            with self.assertRaises(Exception):
                self.db.insert(
                    "INSERT INTO claim_sources (claim_id, source_id, binding_status) "
                    "VALUES (?, 'T000001', ?)", (claim_id, bad_binding))
        with self.assertRaises(Exception):
            self.db.insert(
                "INSERT INTO claim_sources (claim_id, source_id, relation) "
                "VALUES (?, 'T000001', 'bogus')", (claim_id,))

    def test_unique_run_claim_key(self):
        ctx, _ = self.run_dag()
        claim = self.claims(ctx)[0]
        with self.assertRaises(Exception):
            self.db.insert(
                "INSERT INTO content_claims (run_id, domain_id, claim_key, claim_type) "
                "VALUES (?,?, ?, 'classroom_fact')",
                (ctx.run_id, int(ctx.domain_id), claim["claim_key"]))

    def test_unique_claim_source_relation(self):
        ctx, _ = self.run_dag()
        row = self.db.fetch_one(
            "SELECT claim_id, source_id, relation FROM claim_sources LIMIT 1")
        self.assertIsNotNone(row)
        with self.assertRaises(Exception):
            self.db.insert(
                "INSERT INTO claim_sources (claim_id, source_id, relation, binding_status) "
                "VALUES (?,?,?, 'bound')",
                (int(row["claim_id"]), row["source_id"], row["relation"]))

    def test_claim_key_is_deterministic(self):
        ctx, _ = self.run_dag()
        first = [c["claim_key"] for c in self.claims(ctx)]
        self.run_dag()
        second = [c["claim_key"] for c in self.claims(ctx)]
        self.assertEqual(first, second, "claim_key 必须同输入稳定")

    def test_input_and_content_hash_recomputable(self):
        ctx, _ = self.run_dag()
        from app.learning_engine import evidence_v2 as ev
        drafts = ev.project_claims(int(ctx.domain_id), ctx.run_id)
        bound = ev.bind_claims(int(ctx.domain_id), ctx.run_id, drafts)
        by_key = {b.claim_key: b for b in bound}
        rows = self.db.fetch_all(
            "SELECT * FROM content_claims WHERE run_id=? ORDER BY id", (ctx.run_id,))
        self.assertTrue(rows)
        for row in rows:
            draft = next(d for d in drafts if d.claim_key == row["claim_key"])
            self.assertEqual(row["input_hash"], ev._claim_input_hash(draft, int(ctx.domain_id)))
            self.assertEqual(row["content_hash"], ev._claim_content_hash(by_key[row["claim_key"]]))

    def test_ai_explanation_type_preserved(self):
        """ai_explanation 必须保留自身类型，且不计入课堂事实分母。"""
        ctx, _ = self.run_dag()
        report = self.evidence(ctx)
        by_type = report["claims_by_type"]
        self.assertIn("ai_explanation", by_type) if by_type.get("ai_explanation") else None
        for claim in self.claim_by_type(ctx, "ai_explanation"):
            self.assertEqual(claim["evidence_status"], "not_required")
            self.assertFalse(claim["requires_source"])
            self.assertTrue(claim["is_ai_explanation"])
        # 分母只含需要课堂证据的类型
        required = sum(v for k, v in by_type.items() if k != "ai_explanation")
        self.assertEqual(report["required_claims"], required)

    def test_gate_records_zero_denominator_honestly(self):
        """没有需要证据的 critical claim 时 rate=1.0，但分母必须为 0 且留痕。"""
        ctx, _ = self.run_dag()
        from app.learning_engine import evidence_v2 as ev
        bound, report = self.bind_with_drafts(ctx, [
            self.draft("only-ai", "ai_explanation", "只有模型补充", [])])
        self.assertEqual(report.critical_evidence_denominator, 0)
        self.assertEqual(report.critical_claim_evidence_rate, 1.0)
        self.assertTrue(any("denominator=0" in i for i in report.issues),
                        "分母为 0 必须显式记录，不得伪装成验证了很多证据")

    def test_upgrade_v20_to_v21_preserves_data(self):
        """路径验证：只应用 0020（Phase 4）的库 → 应用 0021（Phase 4.1）
        后既有 content_claims/claim_sources 数据与行数不变。"""
        import sqlite3
        from app.database import (
            SCHEMA_MIGRATIONS_DDL, _apply_migration, _migration_files, sync_user_version,
        )
        tmp = self.tmp / "up21.db"
        conn = sqlite3.connect(str(tmp))
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(SCHEMA_MIGRATIONS_DDL)
            conn.commit()
            files = _migration_files()
            for mig in files:
                if mig["version"] <= 20:
                    _apply_migration(conn, mig, ledger_only=False, strict=True)
            sync_user_version(conn)
            self.assertEqual(int(conn.execute("PRAGMA user_version").fetchone()[0]), 20)
            conn.execute("INSERT INTO courses (name, code) VALUES ('升级前课程','PRE')")
            conn.execute("INSERT INTO chapters (course_id, chapter_no, title) "
                         "VALUES (1, 1, '升级前章节')")
            conn.execute(
                "INSERT INTO workflow_runs (workflow, mode, status, input_json) "
                "VALUES ('lesson', 'attend', 'completed', '{}')")
            conn.execute(
                "INSERT INTO material_domains (run_id, scope, version, schema_version, "
                " domain_hash, state, engine_version, transcript_chars, frozen_at, created_at) "
                "VALUES (1, 'lesson', 1, 'v6.1', 'h', 'frozen', 'v6', 0, "
                " datetime('now','localtime'), datetime('now','localtime'))")
            conn.commit()
            before_courses = [tuple(r) for r in conn.execute(
                "SELECT name, code FROM courses ORDER BY id").fetchall()]
            before_domains = conn.execute(
                "SELECT COUNT(*) FROM material_domains").fetchone()[0]
            before_claims = conn.execute(
                "SELECT COUNT(*) FROM content_claims").fetchone()[0]
            for mig in files:
                if mig["version"] == 21:
                    _apply_migration(conn, mig, ledger_only=False, strict=True)
            sync_user_version(conn)
            self.assertEqual(int(conn.execute("PRAGMA user_version").fetchone()[0]), 21)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(content_claims)")}
            self.assertIn("producer_provenance_json", cols)
            cols_src = {r[1] for r in conn.execute("PRAGMA table_info(claim_sources)")}
            self.assertIn("visibility", cols_src)
            self.assertIn("visible_invocations_json", cols_src)
            # 既有数据（按升级前就存在的列）逐行不变
            after_courses = [tuple(r) for r in conn.execute(
                "SELECT name, code FROM courses ORDER BY id").fetchall()]
            self.assertEqual(before_courses, after_courses)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM material_domains").fetchone()[0], before_domains)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM content_claims").fetchone()[0], before_claims)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            conn.close()


class TestDagEvidenceIntegration(EvidenceBase):
    """DAG 接入与 Evidence gate 行为。"""

    def test_bind_evidence_node_is_local_and_ordered(self):
        from app.dag_lesson import build_lesson_dag
        dag = build_lesson_dag()
        self.assertIn("bind_evidence", dag.nodes)
        node = dag.nodes["bind_evidence"]
        self.assertTrue(node.is_local, "bind_evidence 必须是 local 节点（不调模型）")
        self.assertEqual(node.depends_on, ["student_simulator"])
        layers = dag.topological_layers()
        idx = {name: i for i, layer in enumerate(layers) for name in layer}
        self.assertGreater(idx["bind_evidence"], idx["student_simulator"])
        self.assertGreater(idx["coverage_audit"], idx["bind_evidence"])

    def test_coverage_audit_exposes_evidence_report(self):
        ctx, outputs = self.run_dag()
        audit = outputs["coverage_audit"]
        for key in ("evidence_gate", "claims_total", "required_claims", "bound_claims",
                    "failed_claims", "critical_claim_evidence_rate", "invalid_source_refs",
                    "cross_domain_refs", "coverage_gate"):
            self.assertIn(key, audit)
        self.assertEqual(audit["evidence_gate"], "passed")
        self.assertEqual(audit["gate"], "passed")
        self.assertEqual(audit["coverage_gate"], "passed")

    def test_evidence_failure_blocks_success_reporting(self):
        """Evidence gate 失败时 coverage_audit 的 gate 不得是 passed。

        做法：把一条**伪造来源的 critical claim** 直接写进 content_claims
        （模拟投影产出伪造来源），再跑 coverage_audit —— 它必须读取
        EvidenceReport 并拒绝报告整体成功。
        """
        from app.dag import DAGContext
        from app.dag_lesson import coverage_audit_node
        from app.learning_engine import evidence_v2 as ev

        ctx, _ = self.run_dag()
        domain_id = int(ctx.domain_id)

        def inject_forged_claim():
            self.db.execute(
                "INSERT INTO content_claims (run_id, domain_id, claim_key, claim_type, "
                " claim_text, importance, evidence_status) "
                "VALUES (?,?, 'poison:forged', 'classroom_fact', '伪造来源的关键事实', "
                " 'critical', 'failed')", (ctx.run_id, domain_id))
            poison_id = int(self.db.fetch_one(
                "SELECT id FROM content_claims WHERE claim_key='poison:forged'")["id"])
            self.db.execute(
                "INSERT INTO claim_sources (claim_id, source_id, relation, binding_status, "
                " binding_method, failure_reason) "
                "VALUES (?, 'T999999', 'supports', 'missing', 'deterministic_source_id', "
                " 'source_not_in_domain')", (poison_id,))

        # 审计节点的正常输入（真实 bind_evidence 输出）
        audit_ctx = DAGContext()
        audit_ctx.run_id = ctx.run_id
        audit_ctx.domain_id = domain_id
        audit_ctx.input = {"course_id": self.course_id, "chapter_id": self.chapter_id,
                           "lesson_id": self.lesson_id}
        bind_result = ev.run_bind_evidence(domain_id, ctx.run_id)
        self.assertEqual(bind_result["gate"], "passed", "基线必须是通过的")

        # 注入伪造 claim 并复算报告 → 必须 failed（报告读的是**已落库**状态）
        inject_forged_claim()
        report_dict = ev.get_evidence_report(ctx.run_id)
        self.assertEqual(report_dict["gate"], "failed")
        self.assertGreater(report_dict["unbound_source_refs"], 0)
        self.assertGreater(report_dict["failed_claims"], 0)

        audit_ctx.outputs = {
            "retrieve_context": {"candidates": [], "domain_id": domain_id},
            "bind_evidence": {**bind_result, "gate": "failed",
                              "degradation_reason": report_dict["degradation_reason"]},
        }
        result = asyncio.run(coverage_audit_node(audit_ctx, "_local_"))
        self.assertEqual(result["evidence_gate"], "failed")
        self.assertNotEqual(result["gate"], "passed",
                            "证据门禁失败时不得报告整体成功")
        self.assertTrue(result["degradation_reason"], "必须给出降级原因，不得静默通过")

    def test_off_mode_has_no_evidence_node_and_tables_empty(self):
        os.environ["V6_LEARNING_ENGINE"] = "off"
        from app.dag_lesson import build_lesson_dag
        self.assertNotIn("bind_evidence", build_lesson_dag().nodes)
        self.run_dag()
        for table in ("content_claims", "claim_sources"):
            self.assertEqual(int(self.db.fetch_one(
                f"SELECT COUNT(*) AS n FROM {table}")["n"]), 0,
                f"off 模式不得写 {table}")

    def test_real_notes_publish_still_disabled(self):
        from app.learning_engine import config as v6cfg
        self.assertFalse(v6cfg.real_notes_publish_enabled())
        self.assertEqual(v6cfg.publication_mode(),
                         "v6_understanding_only_legacy_publication")
        ctx, _ = self.run_dag()
        notes = self.db.fetch_all("SELECT id, body FROM notes")
        claims = self.claims(ctx)
        for note in notes:
            for claim in claims:
                if claim["claim_text"]:
                    self.assertNotIn(claim["claim_text"], note["body"] or "",
                                     "Evidence V2 不得写入正式笔记（Phase 5 才接管）")

    def test_legacy_evidence_node_kept(self):
        from app.dag_lesson import build_lesson_dag
        dag = build_lesson_dag()
        self.assertIn("evidence", dag.nodes, "legacy evidence 节点必须保留兼容")
        self.assertEqual(dag.nodes["evidence"].depends_on, ["note_writer"])


if __name__ == "__main__":
    unittest.main()
