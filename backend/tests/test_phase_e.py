"""V5.5 阶段E 测试：Obsidian 路径/命名、证据空不自动确认、错题事件、作业状态机与先做后看。"""
import json
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

from app import database as db  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        db.configure_db(self.tmp / "t.db")
        db.init_db()
        from app import config as app_config
        self._saved_token = app_config.MOBILE_TOKEN
        app_config.MOBILE_TOKEN = ""

    def tearDown(self):
        db.reset_connections()
        from app import config as app_config
        app_config.MOBILE_TOKEN = self._saved_token


class TestObsidianNames(Base):
    def test_windows_unsafe_chars(self):
        from app.obsidian import sanitize_windows_name
        self.assertEqual(sanitize_windows_name('a<b>c:d"e/f\\g|h|i?j*k'), "a_b_c_d_e_f_g_h_i_j_k")
        self.assertEqual(sanitize_windows_name("标题."), "标题")
        self.assertEqual(sanitize_windows_name("CON"), "_CON")
        self.assertEqual(sanitize_windows_name(""), "未命名")

    def test_stable_id_naming(self):
        from app.obsidian import note_vault_path
        p = note_vault_path("光学", '第1章 折射', "note-42", "光的折射/定律")
        self.assertEqual(p.name, "[note-42] 光的折射_定律.md")
        self.assertIn("01 Courses", str(p))

    def test_resolve_paths_from_db(self):
        db.insert("INSERT INTO courses (name) VALUES ('真实课程A')")
        db.insert("INSERT INTO chapters (course_id, chapter_no, title) VALUES (1, 2, '光的折射')")
        db.insert("INSERT INTO lessons (chapter_id, course_id, lesson_no, title) VALUES (1, 1, '3', '折射定律')")
        from app.obsidian import resolve_note_paths
        paths = resolve_note_paths(1, 1, 1)
        self.assertEqual(paths["course"], "真实课程A")
        self.assertEqual(paths["chapter"], "第2章 光的折射")
        self.assertIn("L3 折射定律", paths["lesson"])


class TestEvidenceEmptyRule(Base):
    def test_empty_evidence_not_auto_confirmed(self):
        """方案 10.3：证据为空时 all_verified 必须为 False（不得自动确认）。"""
        import asyncio
        from app.dag import DAGContext
        from app.dag_lesson import evidence_verifier_node
        ctx = DAGContext()
        ctx.set_output("note_writer", {"note": {"title": "t", "body": "b", "evidence": []}})
        out = asyncio.run(evidence_verifier_node(ctx, "_local_"))
        self.assertFalse(out["all_verified"])
        self.assertTrue(out["evidence_empty"])


class TestErrorEvents(Base):
    def setUp(self):
        super().setUp()
        db.insert("INSERT INTO courses (name) VALUES ('C')")

    def test_confirm_then_reject_conflict(self):
        from fastapi.testclient import TestClient
        from app.main import app
        eid = db.insert(
            "INSERT INTO errors (course_id, question_text, status) VALUES (1, '题', 'provisional')")
        c = TestClient(app)
        r = c.post(f"/api/errors/{eid}/confirm")
        self.assertEqual(r.status_code, 200)
        # 终态后再操作 → 409
        self.assertEqual(c.post(f"/api/errors/{eid}/confirm").status_code, 409)
        self.assertEqual(c.post(f"/api/errors/{eid}/reject").status_code, 409)
        # 事件留痕
        events = db.fetch_all("SELECT * FROM error_events WHERE error_id=?", (eid,))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "confirmed")
        self.assertEqual(events[0]["old_status"], "provisional")

    def test_reject_flow(self):
        from fastapi.testclient import TestClient
        from app.main import app
        eid = db.insert(
            "INSERT INTO errors (course_id, question_text, status) VALUES (1, '题', 'provisional')")
        r = TestClient(app).post(f"/api/errors/{eid}/reject")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(db.fetch_one("SELECT status FROM errors WHERE id=?", (eid,))["status"], "rejected")

    def test_structured_cause_json(self):
        from app.dag_error import error_ingest
        from app.dag import DAGContext
        import asyncio
        ctx = DAGContext({"course_id": 1})
        ctx.set_output("cross_check", {"candidates": [{
            "question_text": "题", "student_answer": "答",
            "candidate_causes": ["计算失误"], "uncertain": "低",
            "knowledge_points": ["折射定律"], "status": "provisional"}]})
        ctx.set_output("analyst", {"analysis": [{
            "question_text": "题",
            "ai_error_analysis": {"phenomenon": "P", "direct_cause": "D",
                                  "root_cause": "R", "knowledge_gaps": ["G"],
                                  "possible_causes": ["C"]}}]})
        out = asyncio.run(error_ingest(ctx, "_local_"))
        row = db.fetch_one("SELECT ai_error_json FROM errors WHERE id=?", (out["ids"][0],))
        data = json.loads(row["ai_error_json"])
        self.assertEqual(data["phenomenon"], "P")
        self.assertEqual(data["root_cause"], "R")
        self.assertEqual(data["confirmed_causes"], ["计算失误"])


class TestHomeworkStateMachine(Base):
    def setUp(self):
        super().setUp()
        db.insert("INSERT INTO courses (name) VALUES ('C')")

    def _mk_hw(self, status="awaiting_confirmation"):
        hw = db.insert("INSERT INTO homeworks (title, status, course_id) VALUES ('t', ?, 1)", (status,))
        qid = db.insert("INSERT INTO questions (homework_id, question_no, text) VALUES (?,?, '题？')", (hw, 1))
        return hw, qid

    def test_images_only_routes_to_ocr_pipeline(self):
        from fastapi.testclient import TestClient
        from app.main import app
        # 不进入 lifespan：无 worker 执行，仅验证路由与入库
        c = TestClient(app)
        r = c.post("/api/workflows/homework",
                   json={"images": ["data:image/png;base64,iVBORw0KGgo="], "course_id": 1})
        self.assertEqual(r.status_code, 202)
        run = db.fetch_one("SELECT workflow FROM workflow_runs WHERE id=?", (r.json()["run_id"],))
        self.assertEqual(run["workflow"], "homework_ocr")

    def test_confirm_triggers_solve_run(self):
        from fastapi.testclient import TestClient
        from app.main import app
        hw, qid = self._mk_hw()
        c = TestClient(app)
        r = c.post(f"/api/homeworks/{hw}/confirm",
                   json={"questions": [{"question_id": qid, "text": "修正后的题"}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "confirmed")
        self.assertIsNotNone(r.json().get("run_id"))
        self.assertEqual(db.fetch_one("SELECT text FROM questions WHERE id=?", (qid,))["text"], "修正后的题")
        run = db.fetch_one("SELECT workflow FROM workflow_runs WHERE id=?", (r.json()["run_id"],))
        self.assertEqual(run["workflow"], "homework")
        # 重复确认 → 409
        self.assertEqual(c.post(f"/api/homeworks/{hw}/confirm", json={"questions": []}).status_code, 409)

    def test_answer_gating(self):
        from fastapi.testclient import TestClient
        from app.main import app
        hw, qid = self._mk_hw("solved")
        aid = db.insert(
            "INSERT INTO answer_items (question_id, final_answer, detailed_solution) VALUES (?,?, '解析')",
            (qid, "42"))
        c = TestClient(app)
        # 未提交学生答案 → 解答隐藏
        d = c.get(f"/api/homeworks/{hw}").json()
        self.assertIsNone(d["questions"][0].get("answer"))
        # 提交学生答案 → 揭示
        r = c.post(f"/api/homeworks/{hw}/answer", json={"question_id": qid, "student_answer": "我的答案"})
        self.assertEqual(r.status_code, 200)
        d2 = c.get(f"/api/homeworks/{hw}").json()
        self.assertEqual(d2["questions"][0]["answer"]["final_answer"], "42")
        # 重复提交 → 409
        r2 = c.post(f"/api/homeworks/{hw}/answer", json={"question_id": qid, "student_answer": "再答"})
        self.assertEqual(r2.status_code, 409)


if __name__ == "__main__":
    unittest.main()
