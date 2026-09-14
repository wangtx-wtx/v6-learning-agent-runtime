"""V5.6.4 4 类 DAG 在 fake 模式下端到端闭环验收。"""
from __future__ import annotations

import asyncio
import os
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


def _force_fake():
    os.environ["V5_GATEWAY_MODE"] = "fake"
    os.environ.pop("V5_GATEWAY_API_KEY", None)
    os.environ.pop("V5_ALLOW_LIVE_MODEL_TESTS", None)


class Base(unittest.TestCase):
    def setUp(self):
        _force_fake()
        from app import gateway as gw_mod
        gw_mod.reset_engine_for_test()
        self.tmp = Path(tempfile.mkdtemp(prefix="v564_dag_"))
        from app import database as db
        db.configure_db(self.tmp / "t.db")
        db.init_db()
        self.course_id = db.insert(
            "INSERT INTO courses (name, code) VALUES ('Fake Course', 'FAKE101')"
        )
        self.chapter_id = db.insert(
            "INSERT INTO chapters (course_id, chapter_no, title) VALUES (?, 1, 'Chapter 1')",
            (self.course_id,),
        )
        self.lesson_id = db.insert(
            "INSERT INTO lessons (chapter_id, course_id, lesson_no, title) VALUES (?, ?, 'L01', 'Lesson 1')",
            (self.chapter_id, self.course_id),
        )

    def tearDown(self):
        from app import database as db
        db.reset_connections()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        from app import gateway as gw_mod
        gw_mod.reset_engine_for_test()


# ===========================================================================
# 11) 4 个 DAG 端到端
# ===========================================================================
class TestLessonDAGFake(Base):
    def test_fake_lesson_dag_e2e(self):
        """完整 lesson DAG：resolve → parse → retrieve → outline → note → evidence → critic → persist → sync。
        所有 LLM 节点走 fake，业务结果非空。"""
        from app.workers import build_flow
        from app.dag import DAGContext

        dag = build_flow("lesson")
        ctx = DAGContext()
        ctx.input = {
            "course_id": self.course_id,
            "transcript": "本节讲折射定律 sin i / sin r = n。",
            "chapter_id": self.chapter_id,
        }
        # 跑 DAG
        outputs = asyncio.run(dag.run(ctx))
        # 至少 LLM 节点产出
        self.assertIn("note_writer", outputs)
        self.assertIn("lesson_outline", outputs)
        # note 应有 title / body / evidence
        note = outputs["note_writer"].get("note", {})
        self.assertIn("title", note)
        self.assertIn("body", note)
        # fake fixture 不包含可核验证据，安全规则要求保持 draft，不能自动确认
        self.assertIn("persist_note", outputs)
        self.assertEqual(outputs["persist_note"].get("status"),
                         "draft")


class TestHomeworkDAGFake(Base):
    def test_fake_homework_dag_e2e(self):
        """完整 homework DAG：resolve_input → persist → risk → solver → parallel → adjudicator → teaching → persist_answers。"""
        from app.workers import build_flow
        from app.dag import DAGContext

        dag = build_flow("homework")
        ctx = DAGContext()
        ctx.input = {
            "course_id": self.course_id,
            "chapter_id": self.chapter_id,
            "lesson_id": self.lesson_id,
            "homework_text": "1. 1+1=?\n2. 2+2=?",
        }
        outputs = asyncio.run(dag.run(ctx))
        # 至少 solver 应返回答案
        self.assertIn("solver", outputs)
        answers = outputs["solver"].get("answers", [])
        self.assertGreaterEqual(len(answers), 2)
        # parallel_solver 仅 high-risk 题；本题不长，应为空
        # adjudicator / teaching 也应跑通
        self.assertIn("adjudicator", outputs)
        self.assertIn("teaching_explainer", outputs)


class TestErrorDAGFake(Base):
    def test_fake_error_dag_e2e(self):
        """错题流：analyst → cross_check → ingest → schedule。"""
        from app.workers import build_flow
        from app.dag import DAGContext

        dag = build_flow("error")
        ctx = DAGContext()
        ctx.input = {
            "course_id": self.course_id,
            "chapter_id": self.chapter_id,
            "lesson_id": self.lesson_id,
            "question_text": "1+1=?",
            "student_answer": "11",
            "correct_answer": "2",
        }
        outputs = asyncio.run(dag.run(ctx))
        # analyst 应有结构化错因
        self.assertIn("analyst", outputs)
        analysis = outputs["analyst"].get("analysis", [])
        self.assertGreaterEqual(len(analysis), 1)
        # cross_check 应有候选确认原因
        self.assertIn("cross_check", outputs)
        # ingest 应已写错题
        self.assertIn("ingest", outputs)
        ids = outputs["ingest"].get("ids", [])
        self.assertGreaterEqual(len(ids), 1)


class TestReviewDAGFake(Base):
    def test_fake_review_dag_e2e(self):
        """复习流：aggregator → writer → self_test → persist_review。"""
        from app.workers import build_flow
        from app.dag import DAGContext
        from app import database as db

        # 先建一个 confirmed error 与一个 note，让 aggregator 不空
        cid = self.chapter_id
        lesson_id = self.lesson_id
        note_id = db.insert(
            "INSERT INTO notes (lesson_id, chapter_id, course_id, title, body, "
            " status, version, evidence_json, model_used, created_at) "
            "VALUES (?, ?, ?, 'T', 'B', 'confirmed', 1, '[]', "
            " 'fake', datetime('now','localtime'))",
            (lesson_id, cid, self.course_id),
        )
        error_id = db.insert(
            "INSERT INTO errors (course_id, chapter_id, lesson_id, "
            " question_text, student_answer, correct_answer, status, "
            " next_review_at, review_stage, mastery) "
            "VALUES (?, ?, ?, 'Q', 'S', 'C', 'confirmed', "
            " datetime('now','+1 day'), 0, 0.5)",
            (self.course_id, cid, lesson_id),
        )

        dag = build_flow("review")
        ctx = DAGContext()
        ctx.input = {
            "course_id": self.course_id,
            "chapter_id": cid,
            "kind": "chapter",
        }
        outputs = asyncio.run(dag.run(ctx))
        self.assertIn("writer", outputs)
        self.assertIn("self_test", outputs)
        # persist_review 应写一行
        self.assertIn("persist_review", outputs)
        rid = outputs["persist_review"].get("review_id")
        self.assertIsNotNone(rid)


if __name__ == "__main__":
    unittest.main()
