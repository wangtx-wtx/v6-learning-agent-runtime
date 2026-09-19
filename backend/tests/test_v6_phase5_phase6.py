"""V6 Phase 5/6 acceptance tests (isolated fake gateway only)."""
from __future__ import annotations

import json
import os

from tests.test_v6_evidence_v2 import EvidenceBase, TEXT_B


class TestComposerV2(EvidenceBase):
    def setUp(self):
        super().setUp()
        os.environ["V6_LEARNING_ENGINE"] = "on"

    def test_on_mode_uses_only_composer_publication(self):
        from app.dag_lesson import build_lesson_dag
        from app.learning_engine import config
        dag = build_lesson_dag()
        self.assertTrue(config.real_notes_publish_enabled())
        self.assertIn("compose_learning_content", dag.nodes)
        self.assertIn("critic_v2", dag.nodes)
        self.assertNotIn("note_writer", dag.nodes)
        self.assertNotIn("evidence", dag.nodes)

    def test_full_v6_note_revision_quality_and_artifact(self):
        ctx, outputs = self.run_dag(TEXT_B)
        note_id = outputs["persist_note"]["note_id"]
        note = self.db.fetch_one("SELECT * FROM notes WHERE id=?", (note_id,))
        revision = self.db.fetch_one("SELECT * FROM note_revisions WHERE note_id=?", (note_id,))
        quality = self.db.fetch_one("SELECT * FROM learning_quality_states WHERE run_id=?",
                                    (ctx.run_id,))
        artifact = self.db.fetch_one("SELECT * FROM document_artifacts WHERE note_id=?", (note_id,))
        self.assertEqual(note["model_used"], "v6_composer_v2")
        self.assertEqual(note["status"], "confirmed")
        self.assertEqual(revision["content_hash"], artifact["content_hash"])
        self.assertEqual(quality["processing_status"], "completed")
        self.assertEqual(quality["coverage_status"], "passed")
        self.assertEqual(quality["evidence_status"], "passed")
        self.assertEqual(quality["review_status"], "passed")
        self.assertEqual(quality["publication_status"], "rendered")
        self.assertEqual(quality["sync_status"], "synced")
        # Obsidian sync must not overwrite the content status.
        self.assertNotEqual(note["status"], "synced")
        doc = json.loads(revision["structured_json"])
        blocks = [b for s in doc["sections"] for b in s["blocks"]]
        for block in blocks:
            self.assertTrue(block.get("selection_reason"))
        claims = self.db.fetch_all("SELECT note_id,block_id FROM content_claims WHERE run_id=?",
                                   (ctx.run_id,))
        self.assertTrue(claims)
        self.assertTrue(all(c["note_id"] == note_id and c["block_id"] for c in claims))

    def test_optional_blocks_are_analysis_driven(self):
        ctx, outputs = self.run_dag(TEXT_B)
        blocks = [b for s in outputs["compose_learning_content"]["document"]["sections"]
                  for b in s["blocks"]]
        for block in blocks:
            if block["type"] in {"pitfall", "notice", "key_point", "derivation"}:
                self.assertIn(block["claim_key"].split(":", 1)[0],
                              {"lesson_understanding", "cognitive_map"})
            if block["claim_type"] == "ai_explanation":
                self.assertEqual(block["type"], "ai_explanation")


class TestLearningLoop(EvidenceBase):
    def setUp(self):
        super().setUp()
        os.environ["V6_LEARNING_ENGINE"] = "on"

    def test_confirmed_error_traces_to_ku_and_source_and_changes_mastery(self):
        ctx, _ = self.run_dag(TEXT_B)
        ku = self.db.fetch_one("SELECT * FROM knowledge_units WHERE lesson_id=? ORDER BY id LIMIT 1",
                               (self.lesson_id,))
        self.assertIsNotNone(ku)
        eid = self.db.insert(
            "INSERT INTO errors(course_id,chapter_id,lesson_id,question_text,status) "
            "VALUES (?,?,?,?, 'provisional')",
            (self.course_id, self.chapter_id, self.lesson_id, ku["topic"]))
        from app.learning_engine.learning_loop import record_feedback, source_trace_for_error
        updates = record_feedback(source_type="error", source_id=eid, relation="mistake_on",
                                  weight=-.20, text=ku["topic"], course_id=self.course_id,
                                  chapter_id=self.chapter_id, lesson_id=self.lesson_id)
        self.assertTrue(updates)
        trace = source_trace_for_error(eid)
        self.assertTrue(trace["knowledge_units"])
        self.assertTrue(trace["knowledge_units"][0]["classroom_sources"])
        mastery = self.db.fetch_one("SELECT * FROM knowledge_mastery WHERE knowledge_unit_id=?",
                                    (ku["id"],))
        self.assertLess(float(mastery["mastery"]), .5)

    def test_deactivation_recomputes_without_orphan_weight(self):
        self.run_dag(TEXT_B)
        ku = self.db.fetch_one("SELECT * FROM knowledge_units ORDER BY id LIMIT 1")
        from app.learning_engine.learning_loop import deactivate_feedback, record_feedback
        record_feedback(source_type="error", source_id=91, relation="mistake_on", weight=-.2,
                        text=ku["topic"], course_id=self.course_id,
                        chapter_id=self.chapter_id, lesson_id=self.lesson_id)
        deactivate_feedback("error", 91)
        mastery = self.db.fetch_one("SELECT * FROM knowledge_mastery WHERE knowledge_unit_id=?",
                                    (ku["id"],))
        self.assertEqual(float(mastery["mastery"]), .5)
        active = self.db.fetch_one("SELECT COUNT(*) AS n FROM learning_feedback_links "
                                   "WHERE source_type='error' AND source_id=91 AND active=1")
        self.assertEqual(int(active["n"]), 0)

    def test_mastery_changes_review_priority(self):
        self.run_dag(TEXT_B)
        units = self.db.fetch_all("SELECT * FROM knowledge_units ORDER BY id")
        self.assertTrue(units)
        from app.learning_engine.learning_loop import mastery_snapshot, record_feedback
        target = units[-1]
        record_feedback(source_type="manual", source_id=1, relation="manual", weight=-.3,
                        text=target["topic"], course_id=self.course_id,
                        chapter_id=self.chapter_id, lesson_id=self.lesson_id,
                        knowledge_unit_id=target["id"])
        snapshot = mastery_snapshot(chapter_id=self.chapter_id)
        self.assertEqual(snapshot[0]["knowledge_unit_id"], target["id"])


class TestMigrations(EvidenceBase):
    def test_schema_23_and_tables(self):
        self.assertEqual(self.db.schema_version(), 25)
        tables = {r["name"] for r in self.db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"learning_quality_states", "note_revisions", "knowledge_units",
                         "knowledge_unit_sources", "knowledge_mastery",
                         "learning_feedback_links", "mastery_events"} <= tables)
        self.assertEqual(self.db.fetch_all("PRAGMA foreign_key_check"), [])
