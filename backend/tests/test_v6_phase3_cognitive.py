"""V6 Phase 3 Cognitive Layer 验收测试（student_simulator / CognitiveMap 八类 / 输入覆盖）。

运行：python -X utf8 backend/tools/run_tests_isolated.py
"""
from __future__ import annotations

import asyncio
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

#: 明确含认知线索的课堂材料（注意 / 容易混淆 / 省略 / 记住）
CUE_TRANSCRIPT = "\n".join([
    "[00:00:00] 开场：注意这里容易混淆，入射角与折射角要分清",
    "[00:10:00] 中段：这里省略了一步推导，请自己补上",
    "[00:20:00] 结尾：记住法线是垂直于镜面的",
])
#: 完全中性的材料（不含任何认知线索关键词）
NEUTRAL_TRANSCRIPT = "\n".join([
    "[00:00:00] 太阳系有八大行星。",
    "[00:10:00] 地球是第三颗行星。",
])
CUE_WORDS = ("容易混淆", "易混淆", "易错", "注意", "重点", "省略", "跳过", "难点", "记住")


def _force_fake_gateway():
    os.environ["V5_GATEWAY_MODE"] = "fake"
    os.environ.pop("V5_GATEWAY_API_KEY", None)
    os.environ.pop("V5_ALLOW_LIVE_MODEL_TESTS", None)


class CognitiveBase(unittest.TestCase):
    def setUp(self):
        _force_fake_gateway()
        self._prev = os.environ.get("V6_LEARNING_ENGINE")
        os.environ["V6_LEARNING_ENGINE"] = "shadow"
        from app import gateway as gw_mod
        gw_mod.reset_engine_for_test()
        from app import config as app_config
        self._saved_mobile_token = app_config.MOBILE_TOKEN
        app_config.MOBILE_TOKEN = ""

        self.tmp = Path(tempfile.mkdtemp(prefix="v6_p3_"))
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
        if self._prev is None:
            os.environ.pop("V6_LEARNING_ENGINE", None)
        else:
            os.environ["V6_LEARNING_ENGINE"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_material(self, name: str, text: str) -> int:
        import hashlib
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

    def cue_run(self):
        mat = self.make_material("cue.txt", CUE_TRANSCRIPT)
        return self.run_dag(material_ids=[mat])

    def neutral_run(self):
        mat = self.make_material("neutral.txt", NEUTRAL_TRANSCRIPT)
        return self.run_dag(material_ids=[mat])

    def items_for(self, run_id):
        return self.db.fetch_all(
            "SELECT ci.* FROM cognitive_items ci JOIN cognitive_maps cm "
            "ON cm.id = ci.cognitive_map_id WHERE cm.run_id=? ORDER BY ci.ordinal", (run_id,))


class TestCognitiveContracts(CognitiveBase):
    """契约层：八类枚举、severity/confidence、KU 与 Source 强制。"""

    def test_eight_types_defined(self):
        from app.learning_engine.contracts import CognitiveItemType
        self.assertEqual({t.value for t in CognitiveItemType}, {
            "confusion_point", "prerequisite_gap", "pitfall", "emphasis",
            "concept_relation", "memory_anchor", "missing_step", "difficulty"})

    def test_item_requires_knowledge_unit(self):
        from app.learning_engine.contracts import CognitiveItem
        with self.assertRaises(Exception):
            CognitiveItem(stable_key="k1", item_type="difficulty")

    def test_classroom_fact_types_require_source(self):
        from app.learning_engine.contracts import CognitiveItem
        for item_type in ("pitfall", "emphasis", "missing_step", "confusion_point"):
            with self.assertRaises(Exception):
                CognitiveItem(stable_key=f"k-{item_type}", item_type=item_type,
                              knowledge_unit_refs=["KU-1"])

    def test_severity_confidence_range_enforced(self):
        from app.learning_engine.contracts import CognitiveItem
        for value in (-0.1, 1.5):
            with self.assertRaises(Exception):
                CognitiveItem(stable_key="k", item_type="difficulty",
                              knowledge_unit_refs=["KU-1"], severity=value)
            with self.assertRaises(Exception):
                CognitiveItem(stable_key="k", item_type="difficulty",
                              knowledge_unit_refs=["KU-1"], confidence=value)

    def test_extra_fields_forbidden(self):
        from app.learning_engine.contracts import CognitiveItem
        with self.assertRaises(Exception):
            CognitiveItem(stable_key="k", item_type="difficulty",
                          knowledge_unit_refs=["KU-1"], surprise=1)

    def test_empty_status_cannot_carry_items(self):
        from app.learning_engine.contracts import CognitiveItem, CognitiveMap
        item = CognitiveItem(stable_key="k", item_type="difficulty",
                             knowledge_unit_refs=["KU-1"])
        with self.assertRaises(Exception):
            CognitiveMap(run_id=1, domain_id=1, status="empty", items=[item])

    def test_processed_not_greater_than_total(self):
        from app.learning_engine.contracts import CognitiveMap
        with self.assertRaises(Exception):
            CognitiveMap(run_id=1, domain_id=1, knowledge_unit_total=1,
                         knowledge_unit_processed=2)

    def test_origin_enum_covers_required_sources(self):
        from app.learning_engine.contracts import CognitiveOrigin
        values = {o.value for o in CognitiveOrigin}
        for expected in ("classroom_evidence", "confirmed_error",
                         "model_cognitive_inference"):
            self.assertIn(expected, values)

    def test_db_check_constraint_on_item_type(self):
        ctx, _ = self.cue_run()
        map_id = self.db.fetch_one(
            "SELECT id FROM cognitive_maps WHERE run_id=?", (ctx.run_id,))["id"]
        with self.assertRaises(Exception):
            self.db.insert(
                "INSERT INTO cognitive_items (cognitive_map_id, run_id, stable_key, item_type) "
                "VALUES (?,?, 'bad', 'not_a_type')", (map_id, ctx.run_id))

    def test_db_check_constraint_on_severity(self):
        ctx, _ = self.cue_run()
        map_id = self.db.fetch_one(
            "SELECT id FROM cognitive_maps WHERE run_id=?", (ctx.run_id,))["id"]
        with self.assertRaises(Exception):
            self.db.insert(
                "INSERT INTO cognitive_items (cognitive_map_id, run_id, stable_key, item_type, "
                " severity) VALUES (?,?, 'bad2', 'difficulty', 2.0)", (map_id, ctx.run_id))


class TestCognitivePipeline(CognitiveBase):
    """节点 09 端到端行为。"""

    def test_node_runs_in_dag(self):
        from app.dag_lesson import build_lesson_dag
        nodes = set(build_lesson_dag().nodes)
        self.assertIn("student_simulator", nodes)
        node = build_lesson_dag().nodes["student_simulator"]
        self.assertEqual(node.depends_on, ["merge_understanding"])

    def test_cue_material_produces_matching_items(self):
        ctx, outputs = self.cue_run()
        node = outputs["student_simulator"]
        self.assertIn(node["status"], ("succeeded", "empty"))
        types = {i["item_type"] for i in self.items_for(ctx.run_id)}
        self.assertIn("pitfall", types, "「容易混淆」必须产生 pitfall")
        self.assertIn("missing_step", types, "「省略了一步」必须产生 missing_step")
        self.assertIn("emphasis", types, "「注意」必须产生 emphasis")

    def test_every_item_has_ku_and_valid_source(self):
        ctx, _ = self.cue_run()
        items = self.items_for(ctx.run_id)
        self.assertTrue(items)
        valid_sources = {r["source_id"] for r in self.db.fetch_all(
            "SELECT source_id FROM source_spans WHERE domain_id=? AND span_state='included'",
            (ctx.domain_id,))}
        for item in items:
            kus = self.db.fetch_all(
                "SELECT knowledge_unit_key FROM cognitive_item_knowledge_units "
                "WHERE cognitive_item_id=?", (int(item["id"]),))
            self.assertTrue(kus, f"{item['stable_key']} 必须关联至少一个 KU")
            srcs = self.db.fetch_all(
                "SELECT source_id FROM cognitive_item_sources WHERE cognitive_item_id=?",
                (int(item["id"]),))
            if item["item_type"] in ("pitfall", "emphasis", "missing_step", "confusion_point"):
                self.assertTrue(srcs, f"{item['stable_key']} 必须绑定课堂 Source ID")
            for s in srcs:
                self.assertIn(s["source_id"], valid_sources,
                              "认知项不得引用不存在的 Source ID")

    def test_source_span_fk_linked(self):
        ctx, _ = self.cue_run()
        rows = self.db.fetch_all(
            "SELECT s.source_span_id, s.source_id FROM cognitive_item_sources s "
            "JOIN cognitive_items ci ON ci.id = s.cognitive_item_id "
            "JOIN cognitive_maps cm ON cm.id = ci.cognitive_map_id WHERE cm.run_id=?",
            (ctx.run_id,))
        self.assertTrue(rows)
        for row in rows:
            self.assertIsNotNone(row["source_span_id"],
                                 "source_id 必须能解析到 source_spans 行")
        self.assertEqual(self.db.fetch_all("PRAGMA foreign_key_check"), [])

    def test_neutral_material_returns_empty_map(self):
        """无认知风险线索 → 空 items，绝不编造 pitfall。"""
        self.assertFalse(any(w in NEUTRAL_TRANSCRIPT for w in CUE_WORDS))
        ctx, outputs = self.neutral_run()
        node = outputs["student_simulator"]
        self.assertEqual(node["status"], "empty")
        self.assertEqual(node["item_count"], 0)
        self.assertEqual(self.items_for(ctx.run_id), [])
        row = self.db.fetch_one("SELECT status, structured_json FROM cognitive_maps "
                                "WHERE run_id=?", (ctx.run_id,))
        payload = json.loads(row["structured_json"])
        self.assertEqual(row["status"], "empty")
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["by_type"], {})
        self.assertEqual(payload["cognitive_input_coverage"], 1.0)

    def test_empty_map_reported_honestly_in_output(self):
        ctx, outputs = self.neutral_run()
        node = outputs["student_simulator"]
        self.assertEqual(node["item_count"], 0)
        # 空结果不得显示为 failed（它不是失败）
        self.assertNotEqual(node["status"], "failed")

    def test_cognitive_does_not_modify_lesson_understanding(self):
        ctx, _ = self.cue_run()
        before = self.db.fetch_one(
            "SELECT content_hash, structured_json FROM lesson_understandings WHERE run_id=?",
            (ctx.run_id,))
        # 再跑一次认知节点
        from app.dag import DAGContext
        from app.learning_engine.cognition import run_student_simulator
        ctx2 = DAGContext()
        ctx2.run_id = ctx.run_id
        ctx2.domain_id = ctx.domain_id
        asyncio.run(run_student_simulator(ctx2, ctx.domain_id, ctx.run_id, self.lesson_id,
                                          model_id="qwen3_flash"))
        after = self.db.fetch_one(
            "SELECT content_hash, structured_json FROM lesson_understandings WHERE run_id=?",
            (ctx.run_id,))
        self.assertEqual(before["content_hash"], after["content_hash"],
                         "认知层不得修改 LessonUnderstanding")
        self.assertEqual(before["structured_json"], after["structured_json"])

    def test_cognitive_does_not_write_notes(self):
        ctx, _ = self.cue_run()
        notes = self.db.fetch_all("SELECT id, body FROM notes")
        # 认知项内容不得出现在笔记正文里
        items = self.items_for(ctx.run_id)
        for note in notes:
            for item in items:
                if item["title"]:
                    self.assertNotIn(item["title"], note["body"] or "",
                                     "认知分析不得写入最终笔记")

    def test_cognitive_map_unique_per_run(self):
        ctx, _ = self.cue_run()
        from app.dag import DAGContext
        from app.learning_engine.cognition import run_student_simulator
        ctx2 = DAGContext()
        ctx2.run_id = ctx.run_id
        ctx2.domain_id = ctx.domain_id
        for _ in range(2):
            asyncio.run(run_student_simulator(ctx2, ctx.domain_id, ctx.run_id,
                                              self.lesson_id, model_id="qwen3_flash"))
        count = self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM cognitive_maps WHERE run_id=?", (ctx.run_id,))
        self.assertEqual(count["n"], 1, "每个 run 只有一份 CognitiveMap")

    def test_items_idempotent_on_rerun(self):
        ctx, _ = self.cue_run()
        first = self.items_for(ctx.run_id)
        from app.dag import DAGContext
        from app.learning_engine.cognition import run_student_simulator
        ctx2 = DAGContext()
        ctx2.run_id = ctx.run_id
        ctx2.domain_id = ctx.domain_id
        asyncio.run(run_student_simulator(ctx2, ctx.domain_id, ctx.run_id,
                                          self.lesson_id, model_id="qwen3_flash"))
        second = self.items_for(ctx.run_id)
        self.assertEqual([i["stable_key"] for i in first],
                         [i["stable_key"] for i in second],
                         "重跑不得重复累积认知项")

    def test_custom_note_and_origin_are_attributed(self):
        """无课堂来源时不得标 classroom_evidence（必须降级为模型推断）。"""
        from app.learning_engine.cognition import _normalize_items
        raw = [{"type": "difficulty", "title": "推断难度",
                "explanation": "e", "severity": 0.5, "confidence": 0.4,
                "origin": "classroom_evidence",   # 无有效 source → 必须降级
                "knowledge_unit_refs": ["KU-0001"], "source_refs": ["T999999"]}]
        items = _normalize_items(raw, {"T000001"}, ["KU-0001"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].origin.value, "model_cognitive_inference",
                         "无课堂来源不得标为 classroom_evidence")

    def test_pitfall_without_evidence_is_dropped(self):
        from app.learning_engine.cognition import _normalize_items
        raw = [{"type": "pitfall", "title": "凭空的易错点", "explanation": "e",
                "knowledge_unit_refs": ["KU-0001"], "source_refs": []}]
        items = _normalize_items(raw, set(), ["KU-0001"])
        self.assertEqual(items, [], "无依据的 pitfall 必须被丢弃")

    def test_invalid_source_id_is_dropped_not_fabricated(self):
        """非法 Source ID 被剔除；同时保留合法的那一个（不整体丢弃）。"""
        from app.learning_engine.cognition import _normalize_items
        raw = [{"type": "emphasis", "title": "强调", "explanation": "e",
                "knowledge_unit_refs": ["KU-0001"],
                "source_refs": ["T999999", "T000001"]}]
        items = _normalize_items(raw, {"T000001"}, ["KU-0001"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source_refs, ["T000001"],
                         "非法 Source ID 必须被剔除，合法保留")
        self.assertNotIn("T999999", items[0].source_refs)

    def test_unknown_item_type_dropped(self):
        from app.learning_engine.cognition import _normalize_items
        raw = [{"type": "made_up_type", "title": "x",
                "knowledge_unit_refs": ["KU-0001"], "source_refs": ["T000001"]}]
        self.assertEqual(_normalize_items(raw, {"T000001"}, ["KU-0001"]), [])


class TestCognitiveInputCoverage(CognitiveBase):
    """输入覆盖率必须为 1.0；每个 KU 必须进入某个 batch。"""

    def test_coverage_is_one(self):
        ctx, outputs = self.cue_run()
        node = outputs["student_simulator"]
        self.assertEqual(node["cognitive_input_coverage"], 1.0)
        self.assertEqual(node["knowledge_unit_processed"], node["knowledge_unit_total"])
        row = self.db.fetch_one(
            "SELECT cognitive_input_coverage, knowledge_unit_total, knowledge_unit_processed "
            "FROM cognitive_maps WHERE run_id=?", (ctx.run_id,))
        self.assertEqual(row["cognitive_input_coverage"], 1.0)

    def test_ledger_covers_every_ku(self):
        ctx, _ = self.cue_run()
        unit_count = self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM cognitive_input_ledger WHERE run_id=? AND status='consumed'",
            (ctx.run_id,))["n"]
        total = self.db.fetch_one(
            "SELECT knowledge_unit_total FROM cognitive_maps WHERE run_id=?",
            (ctx.run_id,))["knowledge_unit_total"]
        self.assertEqual(int(unit_count), int(total))

    def test_coverage_recomputed_deterministically(self):
        ctx, _ = self.cue_run()
        from app.learning_engine.cognition import cognitive_input_coverage
        cov = cognitive_input_coverage(ctx.run_id)
        self.assertEqual(cov["coverage"], 1.0)
        self.assertEqual(cov["failed"], 0)
        self.assertEqual(cov["skipped"], 0)

    def test_batching_is_order_preserving_and_complete(self):
        from app.learning_engine.cognition import plan_ku_batches
        units = [{"id": f"KU-{i:04d}", "topic": f"t{i}", "summary": "s" * 50}
                 for i in range(1, 18)]
        batches = plan_ku_batches(units, batch_size=5)
        flattened = [u["id"] for b in batches for u in b]
        self.assertEqual(flattened, [u["id"] for u in units], "分批必须保序且不丢")
        self.assertGreaterEqual(len(batches), 4)

    def test_no_ku_means_empty_map_with_full_coverage(self):
        """没有 Knowledge Unit 时 → 空 map，覆盖率按「无成员=完备」记 1.0。"""
        from app.dag import DAGContext
        from app.learning_engine.cognition import run_student_simulator
        from app.learning_engine.domain import build_source_map, freeze_material_domain
        run_id = self.db.insert(
            "INSERT INTO workflow_runs (workflow, mode, course_id, chapter_id, lesson_id, "
            " status, input_json) VALUES ('lesson','attend',?,?,?,'running','{}')",
            (self.course_id, self.chapter_id, self.lesson_id))
        domain = freeze_material_domain(run_id=run_id, course_id=self.course_id,
                                        chapter_id=self.chapter_id,
                                        lesson_id=self.lesson_id, transcript="占位内容")
        build_source_map(domain["id"])
        ctx = DAGContext()
        ctx.run_id = run_id
        ctx.domain_id = int(domain["id"])
        result = asyncio.run(run_student_simulator(
            ctx, int(domain["id"]), run_id, self.lesson_id, model_id="qwen3_flash"))
        self.assertEqual(result["item_count"], 0)
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["cognitive_input_coverage"], 1.0)


class TestCognitiveApi(CognitiveBase):
    def test_cognitive_map_endpoint(self):
        from fastapi.testclient import TestClient
        from app.main import app
        ctx, _ = self.cue_run()
        with TestClient(app) as client:
            res = client.get(f"/api/runs/{ctx.run_id}/cognitive-map")
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertIn("cognitive_map", body)
            self.assertEqual(body["cognitive_map"]["cognitive_input_coverage"], 1.0)
            self.assertIn("by_type", body)
            self.assertFalse(body["publication"]["included_in_document"],
                             "Composer V2 未实施，认知分析不得进入文档")
            blob = json.dumps(body, ensure_ascii=False)
            for forbidden in ("reasoning_content", "chain_of_thought", "prompt_text", "uploads"):
                self.assertNotIn(forbidden, blob)

    def test_cognitive_map_empty_is_200_not_404(self):
        from fastapi.testclient import TestClient
        from app.main import app
        ctx, _ = self.neutral_run()
        with TestClient(app) as client:
            res = client.get(f"/api/runs/{ctx.run_id}/cognitive-map")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["items"], [])

    def test_cognitive_map_404_for_unknown_run(self):
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/runs/999999/cognitive-map").status_code, 404)

    def test_engine_info_reports_publication_honestly(self):
        from fastapi.testclient import TestClient
        from app.main import app
        ctx, _ = self.cue_run()
        with TestClient(app) as client:
            body = client.get(f"/api/runs/{ctx.run_id}/cognitive-map").json()
            engine = body["engine"]
            self.assertFalse(engine["real_notes_publish_enabled"])
            self.assertEqual(engine["publication_mode"],
                             "v6_understanding_only_legacy_publication")


class TestCognitiveMigrations(CognitiveBase):
    def test_phase3_tables_exist(self):
        self.assertEqual(self.db.schema_version(), 25)
        tables = {r["name"] for r in self.db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("cognitive_maps", "cognitive_items", "cognitive_item_sources",
                  "cognitive_item_knowledge_units", "cognitive_input_ledger"):
            self.assertIn(t, tables)

    def test_source_chunks_provenance_columns(self):
        cols = {r["name"] for r in self.db.fetch_all("PRAGMA table_info(source_chunks)")}
        self.assertIn("origin_run_id", cols)
        self.assertIn("inline_text_hash", cols)

    def test_material_domains_reuse_scope_hash(self):
        cols = {r["name"] for r in self.db.fetch_all("PRAGMA table_info(material_domains)")}
        self.assertIn("reuse_scope_hash", cols)

    def test_foreign_keys_clean_after_cognitive_run(self):
        ctx, _ = self.cue_run()
        self.assertEqual(self.db.fetch_all("PRAGMA foreign_key_check"), [])

    def test_deleting_run_cascades_cognitive_rows(self):
        ctx, _ = self.cue_run()
        self.assertTrue(self.db.fetch_all(
            "SELECT id FROM cognitive_items"))
        self.db.execute("DELETE FROM workflow_runs WHERE id=?", (ctx.run_id,))
        left = self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM cognitive_maps WHERE run_id=?", (ctx.run_id,))
        self.assertEqual(left["n"], 0, "删除 run 必须级联清理认知产物")
        self.assertEqual(self.db.fetch_all("PRAGMA foreign_key_check"), [])


class TestOffModeCognitive(CognitiveBase):
    def test_off_mode_has_no_cognitive_node(self):
        os.environ["V6_LEARNING_ENGINE"] = "off"
        from app.dag_lesson import build_lesson_dag
        nodes = set(build_lesson_dag().nodes)
        self.assertNotIn("student_simulator", nodes)

    def test_off_mode_writes_no_cognitive_tables(self):
        os.environ["V6_LEARNING_ENGINE"] = "off"
        mat = self.make_material("cue.txt", CUE_TRANSCRIPT)
        self.run_dag(material_ids=[mat])
        for table in ("cognitive_maps", "cognitive_items", "cognitive_input_ledger"):
            count = self.db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
            self.assertEqual(count, 0, f"off 模式不得写 {table}")


if __name__ == "__main__":
    unittest.main()
