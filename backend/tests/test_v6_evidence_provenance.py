"""V6 Phase 4.1 —— Evidence V2 逐 claim、逐生产者可见性（P1 修复回归）。

已独立复现的缺陷（修复前为真）:

    ``presented_source_ids()`` 把该 run 内**所有**认知批次的已呈现来源合并为一个
    集合，于是 batch 1 产出的 claim 只要被篡改为引用「batch 2 读过」的来源，就会
    被误判成「模型已读过」；LessonUnderstanding 的 claim 也被认知层集合判定。

本文件覆盖任务书 §6 的 A–F 六类：

=========================  ==================================================
A                         两个成功 batch：batch1 的 claim 篡改为引用 batch2 来源
B                         LessonUnderstanding claim 的来源未进入其 segment 输入
C                         正常同批来源 → passed
D                         同 stable_key 跨批合并：逐来源按各自 batch 判定
E                         provenance 缺失/损坏 → failed，不得 fallback
F                         Phase 4 全部既有保障仍有效
=========================  ==================================================

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

TEXT = "\n".join([
    "[00:00:00] 凸透镜成像规律，物距大于二倍焦距成倒立缩小实像。",
    "[00:05:00] 容易混淆的是实像与虚像的判别。",
])


def _force_fake_gateway():
    os.environ["V5_GATEWAY_MODE"] = "fake"
    os.environ.pop("V5_GATEWAY_API_KEY", None)
    os.environ.pop("V5_ALLOW_LIVE_MODEL_TESTS", None)


class ProvenanceBase(unittest.TestCase):
    def setUp(self):
        _force_fake_gateway()
        self._prev = os.environ.get("V6_LEARNING_ENGINE")
        os.environ["V6_LEARNING_ENGINE"] = "shadow"
        from app import gateway as gw_mod
        gw_mod.reset_engine_for_test()
        from app import config as app_config
        self._saved_mobile_token = app_config.MOBILE_TOKEN
        app_config.MOBILE_TOKEN = ""

        self.tmp = Path(tempfile.mkdtemp(prefix="v6_p41_"))
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
    def run_dag(self, transcript=TEXT):
        from app.dag import DAGContext
        from app.dag_lesson import build_lesson_dag
        ctx = DAGContext()
        ctx.input = {"course_id": self.course_id, "chapter_id": self.chapter_id,
                     "lesson_id": self.lesson_id, "transcript": transcript,
                     "material_ids": []}
        outputs = asyncio.run(build_lesson_dag().run(ctx))
        return ctx, outputs

    def claims(self, ctx):
        from app.learning_engine import evidence_v2 as ev
        return ev.get_claims(ctx.run_id)

    def bind(self, ctx, drafts):
        from app.learning_engine import evidence_v2 as ev
        bound = ev.bind_claims(int(ctx.domain_id), ctx.run_id, drafts)
        report = ev.compute_evidence_report(ctx.run_id, int(ctx.domain_id), bound)
        return bound, report

    def draft(self, key, claim_type, refs, provenance, importance="supporting"):
        from app.learning_engine.contracts import ClaimDraft
        return ClaimDraft(claim_key=key, claim_type=claim_type,
                          claim_text=f"{key} 文本", importance=importance,
                          source_refs=refs, origin_ref=key,
                          producer_node="test", provenance=provenance)

    def prov(self, visible, invocations, kind="cognitive_map",
             basis="cognitive_batch_audit"):
        from app.learning_engine.contracts import ClaimProvenance
        return ClaimProvenance(
            producer_kind=kind, visibility_basis=basis,
            visible_source_ids=list(visible), invocation_refs=list(invocations),
            per_source_invocations={s: list(invocations) for s in visible})

    def batch_visible(self, ctx):
        """真实落库的逐 batch 可见来源：``{ordinal: [source_id]}``。"""
        from app.learning_engine import evidence_v2 as ev
        ledger = ev.cognitive_batch_ledger(ctx.run_id)
        return {int(o): list(b["visible_source_ids"]) for o, b in ledger.items()
                if b.get("status") == "consumed"}

    def segment_visible(self, domain_id):
        from app.learning_engine import evidence_v2 as ev
        return ev.segment_input_ledger(int(domain_id))


class TestA_CrossBatchBorrow(ProvenanceBase):
    """A. batch1 的 claim 引用 batch2 的来源 → 必须 failed。"""

    def test_a1_batch1_claim_cannot_borrow_batch2_source(self):
        ctx, _ = self.run_dag()
        visible = self.batch_visible(ctx)
        self.assertGreaterEqual(len(visible), 2, "需要至少两个成功认知批次")
        b1, b2 = sorted(visible)[0], sorted(visible)[1]
        only_b2 = [s for s in visible[b2] if s not in visible[b1]]
        self.assertTrue(only_b2, "构造失败：batch2 应有 batch1 未见的来源")
        target = only_b2[0]

        # batch1 的 provenance 只看到 batch1 的来源
        batch1_prov = self.prov(visible[b1], [b1])
        bound, report = self.bind(ctx, [
            self.draft("A-borrow", "classroom_fact", [target], batch1_prov)])
        self.assertEqual(bound[0].evidence_status.value, "failed")
        src = bound[0].sources[0]
        self.assertFalse(src.presented_to_producer,
                         "其他 batch 读过不等于本批读过")
        self.assertEqual(src.visibility.value, "not_visible")
        self.assertEqual(src.failure_reason,
                         "source_not_presented_to_this_claim_producer")
        self.assertEqual(report.not_in_claim_producer_refs, 1)
        self.assertEqual(report.gate.value, "failed")

    def test_a2_same_source_in_both_batches_is_visible(self):
        """同一来源若真的也进入了本批调用，则应可见（不因跨批而一刀切）。"""
        ctx, _ = self.run_dag()
        visible = self.batch_visible(ctx)
        common = set.intersection(*[set(v) for v in visible.values()]) if visible else set()
        for sid in sorted(common):
            bound, _ = self.bind(ctx, [
                self.draft(f"A-common-{sid}", "classroom_fact", [sid],
                           self.prov(sorted(visible[sorted(visible)[0]]),
                                     [sorted(visible)[0]]))])
            self.assertEqual(bound[0].evidence_status.value, "passed")

    def test_a3_run_level_union_is_not_used_as_pass_basis(self):
        """回归护栏：run 级并集不得成为通过依据。"""
        from app.learning_engine import evidence_v2 as ev
        ctx, _ = self.run_dag()
        union = ev.presented_source_ids(int(ctx.domain_id), ctx.run_id)
        visible = self.batch_visible(ctx)
        b1 = sorted(visible)[0]
        leaked = sorted(union - set(visible[b1]))
        if not leaked:
            self.skipTest("本 fixture 的 run 级并集与 batch1 相同")
        bound, _ = self.bind(ctx, [
            self.draft("A-union", "classroom_fact", [leaked[0]],
                       self.prov(visible[b1], [b1]))])
        self.assertEqual(bound[0].evidence_status.value, "failed",
                         "run 级并集里的来源不得让 batch1 的 claim 通过")


class TestB_LessonUnderstandingVisibility(ProvenanceBase):
    """B. LessonUnderstanding 的可见性必须来自自己的 segment 输入。"""

    def test_b1_lu_claim_ignores_cognitive_visibility(self):
        from app.learning_engine import evidence_v2 as ev
        ctx, _ = self.run_dag()
        did = int(ctx.domain_id)
        seg_visible = self.segment_visible(did)
        self.assertTrue(seg_visible)
        seg1 = sorted(seg_visible)[0]
        sids = seg_visible[seg1]
        self.assertGreaterEqual(len(sids), 2, "segment 1 应有至少 2 个来源")
        target = sids[-1]

        # 从 segment 1 的**输入账本**里移除该来源（认知层仍读过它）
        seg = self.db.fetch_one(
            "SELECT id FROM lesson_segments WHERE domain_id=? AND ordinal=?",
            (did, seg1))
        self.db.execute("DELETE FROM segment_source_spans WHERE segment_id=? AND source_id=?",
                        (int(seg["id"]), target))
        self.assertIn(target, ev.presented_source_ids(did, ctx.run_id),
                      "认知层诊断集合仍包含该来源（这正是旧实现的误判来源）")

        lu_prov = ev._provenance_for_segments(did, [seg1], "lesson_understanding")
        self.assertNotIn(target, lu_prov.visible_source_ids)
        bound, report = self.bind(ctx, [
            self.draft("B-lu", "classroom_fact", [target], lu_prov)])
        self.assertEqual(bound[0].evidence_status.value, "failed",
                         "LU claim 不得借用认知层的可见性")
        self.assertFalse(bound[0].sources[0].presented_to_producer)
        self.assertEqual(bound[0].sources[0].failure_reason,
                         "source_not_presented_to_this_claim_producer")
        self.assertEqual(report.gate.value, "failed")

    def test_b2_lu_provenance_basis_is_segment_ledger(self):
        from app.learning_engine import evidence_v2 as ev
        ctx, _ = self.run_dag()
        prov = ev._provenance_for_segments(int(ctx.domain_id), [1], "lesson_understanding")
        self.assertEqual(prov.visibility_basis.value, "segment_input_ledger")
        self.assertEqual(prov.producer_kind.value, "lesson_understanding")
        self.assertEqual(prov.source_scope if hasattr(prov, "source_scope") else
                         "claim_producer_invocations", "claim_producer_invocations")
        # 落库的 LU claim 同样以 segment 账本为依据
        lu_claims = [c for c in self.claims(ctx)
                     if c["provenance"]["producer_kind"] == "lesson_understanding"]
        self.assertTrue(lu_claims)
        for claim in lu_claims:
            self.assertEqual(claim["provenance"]["visibility_basis"],
                             "segment_input_ledger")
            self.assertTrue(claim["provenance"]["available"])

    def test_b3_lu_claim_with_segment_visible_source_passes(self):
        from app.learning_engine import evidence_v2 as ev
        ctx, _ = self.run_dag()
        prov = ev._provenance_for_segments(int(ctx.domain_id), [1], "lesson_understanding")
        self.assertTrue(prov.visible_source_ids)
        bound, _ = self.bind(ctx, [
            self.draft("B-ok", "classroom_paraphrase", [prov.visible_source_ids[0]], prov)])
        self.assertEqual(bound[0].evidence_status.value, "passed")


class TestC_SameBatchVisible(ProvenanceBase):
    """C. 同批真实出现过的来源必须 passed。"""

    def test_c1_same_batch_source_passes(self):
        ctx, _ = self.run_dag()
        visible = self.batch_visible(ctx)
        ordinal = sorted(visible)[0]
        sid = visible[ordinal][0]
        bound, report = self.bind(ctx, [
            self.draft("C-ok", "classroom_fact", [sid], self.prov(visible[ordinal],
                                                                  [ordinal]))])
        self.assertEqual(bound[0].evidence_status.value, "passed")
        src = bound[0].sources[0]
        self.assertTrue(src.presented_to_producer)
        self.assertEqual(src.visibility.value, "visible")
        self.assertEqual(src.visible_invocations, [ordinal])
        self.assertIsNone(src.failure_reason)
        self.assertEqual(report.gate.value, "passed")

    def test_c2_real_pipeline_claims_pass(self):
        """真实 DAG 产出的 claim 必须全部通过（修复不得误伤正常路径）。"""
        ctx, _ = self.run_dag()
        report = __import__("app.learning_engine.evidence_v2", fromlist=["x"]) \
            .get_evidence_report(ctx.run_id)
        self.assertEqual(report["gate"], "passed", report["degradation_reason"])
        self.assertEqual(report["failed_claims"], 0)
        self.assertEqual(report["not_in_claim_producer_refs"], 0)
        self.assertEqual(report["claims_without_provenance"], 0)
        for claim in self.claims(ctx):
            self.assertTrue(claim["provenance"]["available"], claim["claim_key"])


class TestD_MergedProvenance(ProvenanceBase):
    """D. 同 stable_key 跨批合并：逐来源按各自 invocation 判定。"""

    def test_d1_merged_item_keeps_per_source_invocations(self):
        from app.learning_engine.contracts import ClaimProvenance
        ctx, _ = self.run_dag()
        visible = self.batch_visible(ctx)
        ordinals = sorted(visible)
        self.assertGreaterEqual(len(ordinals), 2)
        o1, o2 = ordinals[0], ordinals[1]
        s1, s2 = visible[o1][0], visible[o2][-1]
        merged = ClaimProvenance(
            producer_kind="cognitive_map", visibility_basis="cognitive_batch_audit",
            invocation_refs=[o1, o2], visible_source_ids=sorted({s1, s2}),
            per_source_invocations={s1: [o1], s2: [o2]})
        bound, _ = self.bind(ctx, [
            self.draft("D-merged", "classroom_fact", [s1, s2], merged)])
        self.assertEqual(bound[0].evidence_status.value, "passed")
        by_sid = {s.source_id: s for s in bound[0].sources}
        self.assertEqual(by_sid[s1].visible_invocations, [o1])
        self.assertEqual(by_sid[s2].visible_invocations, [o2])
        self.assertTrue(all(s.presented_to_producer for s in bound[0].sources))

    def test_d2_merge_does_not_widen_visibility(self):
        """合并后的 provenance 不得让「只被别的 invocation 读过」的来源通过。"""
        ctx, _ = self.run_dag()
        visible = self.batch_visible(ctx)
        ordinals = sorted(visible)
        o1, o2 = ordinals[0], ordinals[1]
        only_o2 = [s for s in visible[o2] if s not in visible[o1]]
        self.assertTrue(only_o2, "构造失败：batch2 应有 batch1 未见的来源")
        # provenance 只含 invocation o1
        bound, report = self.bind(ctx, [
            self.draft("D-narrow", "classroom_fact", [only_o2[0]],
                       self.prov(visible[o1], [o1]))])
        self.assertEqual(bound[0].evidence_status.value, "failed")
        self.assertFalse(bound[0].sources[0].presented_to_producer)
        self.assertEqual(report.gate.value, "failed")

    def test_d3_pipeline_merged_item_provenance_is_per_batch(self):
        """真实链路上同 stable_key 合并后的 item 必须带逐来源 invocation。"""
        from app.learning_engine import evidence_v2 as ev
        ctx, _ = self.run_dag()
        payload = json.loads(self.db.fetch_one(
            "SELECT structured_json FROM cognitive_maps WHERE run_id=?",
            (ctx.run_id,))["structured_json"])
        items = payload.get("items") or []
        self.assertTrue(items)
        for item in items:
            prov = item.get("provenance") or {}
            self.assertEqual(prov.get("visibility_basis"), "cognitive_batch_audit")
            self.assertEqual(prov.get("source_scope"), "claim_producer_invocations")
            self.assertTrue(prov.get("per_source_invocations") is not None)
            for sid, invocations in (prov.get("per_source_invocations") or {}).items():
                self.assertTrue(invocations, f"{sid} 必须记录真实 invocation")
                for o in invocations:
                    self.assertIn(sid, ev.cognitive_batch_ledger(ctx.run_id)[int(o)][
                        "visible_source_ids"],
                        "per_source_invocations 必须与 batch 审计一致")

    def test_d4_merged_item_citing_unread_source_fails_in_pipeline(self):
        """篡改**已落库** item 的来源 → 重绑后必须 failed（真实链路）。

        这是本 P1 的核心回归：落库后的 claim 借用「同 run 其他 batch 读过的来源」
        必须失败，而不是被 run 级全局集合放行。
        """
        from app.learning_engine import evidence_v2 as ev
        ctx, _ = self.run_dag()
        visible = self.batch_visible(ctx)
        ordinals = sorted(visible)
        self.assertGreaterEqual(len(ordinals), 2)

        row = self.db.fetch_one(
            "SELECT id, structured_json FROM cognitive_maps WHERE run_id=?", (ctx.run_id,))
        payload = json.loads(row["structured_json"])
        item = (payload.get("items") or [None])[0]
        self.assertIsNotNone(item, "本 fixture 至少应有 1 个认知项")
        item_visible = set((item.get("provenance") or {}).get(
            "per_source_invocations") or {})
        self.assertTrue(item_visible, "该 item 必须带逐来源 provenance")

        # 目标来源：本 run 内某个 batch 读过，但**没有**被这个 item 读过
        elsewhere = sorted({s for o in ordinals for s in visible[o]} - item_visible)
        self.assertTrue(elsewhere, "构造失败：找不到该 item 未读过的来源")
        target_sid = elsewhere[0]
        # 篡改：让这个 item 引用它其实没读过的来源
        item["source_refs"] = [target_sid]
        item["out_of_batch_source_refs"] = [target_sid]
        self.db.execute("UPDATE cognitive_maps SET structured_json=? WHERE id=?",
                        (json.dumps(payload, ensure_ascii=False, sort_keys=True), row["id"]))
        ev.run_bind_evidence(int(ctx.domain_id), ctx.run_id)
        report = ev.get_evidence_report(ctx.run_id)
        self.assertEqual(report["gate"], "failed",
                         "落库的 claim 借用其他批次来源必须失败")
        self.assertGreater(report["not_in_claim_producer_refs"], 0)
        # 篡改后的 claim：来源仍被绑定（审计可见），但标记为未进入本 claim 的 producer 输入
        tampered_claim = next(
            (c for c in self.claims(ctx)
             if c["provenance"]["producer_kind"] == "cognitive_map"
             and c["evidence_status"] == "failed"
             and target_sid in [s["source_id"] for s in c["sources"]]), None)
        self.assertIsNotNone(tampered_claim, "必须存在引用该来源且失败的 cognitive claim")
        src = next(s for s in tampered_claim["sources"] if s["source_id"] == target_sid)
        self.assertFalse(src["presented_to_producer"])
        self.assertEqual(src["visibility"], "not_visible")
        self.assertEqual(src["failure_reason"],
                         "source_not_presented_to_this_claim_producer")
        # 同一来源在**真正读过它的** claim 里必须仍被如实标为可见（不得一刀切）
        other = next((s for c in self.claims(ctx)
                      if c["provenance"]["producer_kind"] == "cognitive_map"
                      and c["evidence_status"] == "passed"
                      for s in c["sources"] if s["source_id"] == target_sid), None)
        if other is not None:
            self.assertTrue(other["presented_to_producer"])
            self.assertEqual(other["visibility"], "visible")


class TestE_MissingOrCorruptProvenance(ProvenanceBase):
    """E. provenance 缺失/损坏 → failed，绝不 fallback 到全局集合。"""

    def test_e1_unavailable_provenance_fails(self):
        from app.learning_engine.contracts import ClaimProvenance
        ctx, _ = self.run_dag()
        visible = self.batch_visible(ctx)
        sid = visible[sorted(visible)[0]][0]
        for label, prov in (
                ("unavailable", ClaimProvenance(producer_kind="cognitive_map",
                                                visibility_basis="unavailable")),
                ("empty-visible", ClaimProvenance(producer_kind="cognitive_map",
                                                  visibility_basis="unavailable",
                                                  visible_source_ids=[]))):
            bound, report = self.bind(ctx, [
                self.draft(f"E-{label}", "classroom_fact", [sid], prov)])
            self.assertEqual(bound[0].evidence_status.value, "failed", label)
            self.assertIn("producer_visibility_missing", bound[0].failure_reasons)
            self.assertGreaterEqual(report.claims_without_provenance, 1)
            self.assertEqual(report.gate.value, "failed")

    def test_e2_corrupt_stored_provenance_fails(self):
        from app.learning_engine import evidence_v2 as ev
        ctx, _ = self.run_dag()
        row = self.db.fetch_one("SELECT id FROM content_claims WHERE run_id=? LIMIT 1",
                                (ctx.run_id,))
        self.db.execute(
            "UPDATE content_claims SET producer_provenance_json='{not-json' WHERE id=?",
            (int(row["id"]),))
        claims = {int(c["id"]): c for c in self.claims(ctx)}
        broken = claims[int(row["id"])]
        self.assertFalse(broken["provenance"]["available"],
                         "损坏的 provenance 必须按不可用处理")
        report = ev.get_evidence_report(ctx.run_id)
        self.assertGreaterEqual(report["claims_without_provenance"], 1,
                                "必须统计缺少 provenance 的 claim 数")
        self.assertEqual(report["gate"], "failed")

    def test_e3_unknown_schema_provenance_fails(self):
        from app.learning_engine import evidence_v2 as ev
        ctx, _ = self.run_dag()
        row = self.db.fetch_one("SELECT id FROM content_claims WHERE run_id=? LIMIT 1",
                                (ctx.run_id,))
        self.db.execute(
            "UPDATE content_claims SET producer_provenance_json=? WHERE id=?",
            (json.dumps({"producer_kind": "not_a_kind", "visibility_basis": "bogus"}),
             int(row["id"])))
        claims = {int(c["id"]): c for c in self.claims(ctx)}
        self.assertFalse(claims[int(row["id"])]["provenance"]["available"])
        self.assertEqual(ev.get_evidence_report(ctx.run_id)["gate"], "failed")

    def test_e4_missing_provenance_does_not_use_run_union(self):
        """明确反例：即使 run 级集合里存在该来源，缺 provenance 也必须 failed。"""
        from app.learning_engine import evidence_v2 as ev
        from app.learning_engine.contracts import ClaimProvenance
        ctx, _ = self.run_dag()
        sid = sorted(ev.presented_source_ids(int(ctx.domain_id), ctx.run_id))[0]
        bound, _ = self.bind(ctx, [
            self.draft("E-no-prov", "classroom_fact", [sid],
                       ClaimProvenance(producer_kind="cognitive_map",
                                       visibility_basis="unavailable"))])
        self.assertEqual(bound[0].evidence_status.value, "failed",
                         "缺 provenance 时不得回退到 run 级并集")
        self.assertFalse(bound[0].sources[0].presented_to_producer)


class TestF_Phase4GuaranteesStillHold(ProvenanceBase):
    """F. Phase 4 全部既有保障在 Phase 4.1 之后仍有效。"""

    def test_f1_forged_and_malformed_and_cross_domain(self):
        ctx, _ = self.run_dag()
        visible = self.batch_visible(ctx)
        prov = self.prov(visible[sorted(visible)[0]], [sorted(visible)[0]])
        # 伪造（本 domain 不存在）
        bound, report = self.bind(ctx, [self.draft("F-forged", "classroom_fact",
                                                   ["T999999"], prov)])
        self.assertEqual(bound[0].evidence_status.value, "failed")
        self.assertEqual(report.unbound_source_refs, 1)
        # 格式非法
        bound, report = self.bind(ctx, [self.draft("F-malformed", "classroom_fact",
                                                   ["bogus"], prov)])
        self.assertEqual(bound[0].sources[0].binding_status.value, "invalid")
        self.assertEqual(report.invalid_source_refs, 1)
        # 跨 domain 同名 Source ID
        ctx_a, _ = self.run_dag("[00:00:00] 另一堂课：光的反射定律。")
        a_ids = {r["source_id"] for r in self.db.fetch_all(
            "SELECT source_id FROM source_spans WHERE domain_id=?", (ctx_a.domain_id,))}
        b_ids = {r["source_id"] for r in self.db.fetch_all(
            "SELECT source_id FROM source_spans WHERE domain_id=?", (ctx.domain_id,))}
        only_a = sorted(a_ids - b_ids) or sorted(a_ids)
        if only_a:
            cross_sid = only_a[0]
            in_b = self.db.fetch_one(
                "SELECT id FROM source_spans WHERE domain_id=? AND source_id=?",
                (int(ctx.domain_id), cross_sid))
            if in_b is None:
                # B 里不存在该 ID：按 missing / cross-domain 处理，不得当作 visible
                bound, report = self.bind(ctx, [self.draft("F-cross", "classroom_fact",
                                                           [cross_sid], prov)])
                self.assertEqual(bound[0].sources[0].visibility.value, "unknown")
                self.assertIn(bound[0].sources[0].binding_status.value,
                              ("invalid", "missing"))
                self.assertGreaterEqual(
                    report.invalid_source_refs + report.unbound_source_refs, 1)
                self.assertEqual(report.gate.value, "failed")
            else:
                # B 里存在同名 ID：必须绑定 B 自己的 span
                row = self.db.fetch_one("SELECT text FROM source_spans WHERE id=?",
                                        (int(in_b["id"]),))
                bound, _ = self.bind(ctx, [self.draft("F-cross2", "classroom_fact",
                                                      [cross_sid], prov)])
                self.assertEqual(bound[0].sources[0].bound_quote, row["text"])

    def test_f2_bound_quote_only_from_database(self):
        ctx, _ = self.run_dag()
        originals = {r["text"] for r in self.db.fetch_all(
            "SELECT text FROM source_spans WHERE domain_id=?", (ctx.domain_id,))}
        checked = 0
        for claim in self.claims(ctx):
            for src in claim["sources"]:
                if src["binding_status"] != "bound":
                    continue
                checked += 1
                self.assertIn(src["bound_quote"], originals)
                self.assertEqual(
                    src["quote_hash"],
                    "sha256:" + hashlib.sha256(src["bound_quote"].encode("utf-8")).hexdigest())
                self.assertEqual(src["binding_method"], "deterministic_source_id")
        self.assertGreater(checked, 0)

    def test_f3_critical_rate_and_gate(self):
        ctx, _ = self.run_dag()
        from app.learning_engine import evidence_v2 as ev
        report = ev.get_evidence_report(ctx.run_id)
        self.assertEqual(report["critical_evidence_denominator"], report["critical_claims"])
        if report["critical_evidence_denominator"]:
            self.assertEqual(report["critical_claim_evidence_rate"], 1.0)
            self.assertEqual(report["gate"], "passed")
        else:
            self.assertEqual(report["critical_claim_evidence_rate"], 1.0)
            self.assertTrue(any("denominator=0" in i for i in report["issues"]))

    def test_f4_rerun_idempotent_and_history_isolated(self):
        from app.learning_engine import evidence_v2 as ev
        ctx, _ = self.run_dag()

        def counts(run_id):
            return (
                int(self.db.fetch_one("SELECT COUNT(*) AS n FROM content_claims WHERE run_id=?",
                                      (run_id,))["n"]),
                int(self.db.fetch_one(
                    "SELECT COUNT(*) AS n FROM claim_sources WHERE claim_id IN "
                    "(SELECT id FROM content_claims WHERE run_id=?)", (run_id,))["n"]),
                int(self.db.fetch_one(
                    "SELECT COUNT(*) AS n FROM evidence_links WHERE owner_type='content_claim' "
                    "AND owner_id IN (SELECT id FROM content_claims WHERE run_id=?)",
                    (run_id,))["n"]),
            )

        first = counts(ctx.run_id)
        for _ in range(2):
            ev.run_bind_evidence(int(ctx.domain_id), ctx.run_id)
        self.assertEqual(counts(ctx.run_id), first, "同 run 重跑不得累积")

        a_claims = self.claims(ctx)
        ctx_b, _ = self.run_dag("[00:00:00] 第三节课：光的折射与全反射。")
        self.assertEqual(self.claims(ctx), a_claims, "新 run 不得改动历史 run 的 claims")
        self.assertNotEqual(int(ctx.run_id), int(ctx_b.run_id))

    def test_f5_ai_explanation_and_omitted_budget(self):
        """ai_explanation 仍 not_required；声明来源时可见性如实标注。"""
        ctx, _ = self.run_dag()
        from app.learning_engine.contracts import ClaimProvenance
        visible = self.batch_visible(ctx)
        sid = visible[sorted(visible)[0]][0]
        bound, report = self.bind(ctx, [
            self.draft("F-ai", "ai_explanation", [], ClaimProvenance(
                producer_kind="cognitive_map", visibility_basis="unavailable"))])
        self.assertEqual(bound[0].evidence_status.value, "not_required")
        self.assertEqual(report.failed_claims, 0)
        # 声明了课堂来源的 ai_explanation：绑定/可见性仍如实记录
        per_source, _ = self.batch_visible(ctx), None
        prov = self.prov(visible[sorted(visible)[0]], [sorted(visible)[0]])
        bound2, _ = self.bind(ctx, [self.draft("F-ai-src", "ai_explanation", [sid], prov)])
        self.assertEqual(bound2[0].evidence_status.value, "not_required")
        self.assertEqual(bound2[0].sources[0].binding_status.value, "bound")
        self.assertTrue(bound2[0].sources[0].presented_to_producer)

    def test_f6_legacy_projection_and_api_safety(self):
        from fastapi.testclient import TestClient
        from app.main import app
        ctx, _ = self.run_dag()
        rows = self.db.fetch_all(
            "SELECT owner_type, owner_id FROM evidence_links "
            "WHERE owner_type='content_claim'")
        self.assertTrue(rows)
        for row in rows:
            self.assertIsNotNone(self.db.fetch_one(
                "SELECT id FROM content_claims WHERE id=?", (int(row["owner_id"]),)))
        with TestClient(app) as client:
            res = client.get(f"/api/runs/{ctx.run_id}/evidence-v2")
            self.assertEqual(res.status_code, 200)
            body = res.json()
            blob = json.dumps(body, ensure_ascii=False)
            for forbidden in ("reasoning_content", "chain_of_thought", "system_prompt",
                              "prompt_text", "api_key", "sk-", "Bearer ", "uploads",
                              "C:\\\\", "backend\\", "Authorization"):
                self.assertNotIn(forbidden, blob, f"不得泄露 {forbidden}")
            claim = next(c for c in body["claims"] if c["sources"])
            self.assertIn("provenance", claim)
            prov = claim["provenance"]
            for key in ("producer_node", "invocation_refs", "visible_source_ids",
                        "visibility_basis", "available"):
                self.assertIn(key, prov)
            self.assertIn("visibility", claim["sources"][0])
            self.assertIn("visible_invocations", claim["sources"][0])
            self.assertEqual(body["evidence_semantics"]["visibility_scope"],
                             "claim_producer_invocations（逐 claim、逐生产者）")

    def test_f7_off_mode_writes_nothing(self):
        os.environ["V6_LEARNING_ENGINE"] = "off"
        from app.dag_lesson import build_lesson_dag
        self.assertNotIn("bind_evidence", build_lesson_dag().nodes)
        self.run_dag()
        for table in ("content_claims", "claim_sources"):
            self.assertEqual(int(self.db.fetch_one(
                f"SELECT COUNT(*) AS n FROM {table}")["n"]), 0)


if __name__ == "__main__":
    unittest.main()
