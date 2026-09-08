"""V5.5 阶段D 测试：复习作答判分 + 简化 SM-2 闭环（方案 5.3/5.4）。"""
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
from app.review_service import (  # noqa: E402
    grade_answer, next_interval_days, next_mastery, consecutive_wrong_count,
    normalize_answer, MIN_INTERVAL, MAX_INTERVAL,
)


class TestSm2Math(unittest.TestCase):
    def test_rating_interval_mapping(self):
        self.assertEqual(next_interval_days(10, 0, 0), 1.0)
        self.assertEqual(next_interval_days(10, 1, 0), 1.0)
        self.assertEqual(next_interval_days(10, 2, 0), 3.0)
        self.assertEqual(next_interval_days(10, 3, 0), 18.0)   # ×1.8
        self.assertEqual(next_interval_days(10, 4, 0), 25.0)   # ×2.5
        self.assertEqual(next_interval_days(10, 5, 0), 32.0)   # ×3.2

    def test_interval_clamped(self):
        self.assertEqual(next_interval_days(200, 5, 0), MAX_INTERVAL)
        self.assertEqual(next_interval_days(0, 3, 0), 1.8)

    def test_consecutive_wrong_resets(self):
        self.assertEqual(next_interval_days(50, 5, 2), MIN_INTERVAL)
        self.assertEqual(next_interval_days(50, 5, 1), 50 * 3.2, "仅1次错误不重置")

    def test_mastery_driven_by_correctness_only(self):
        self.assertEqual(next_mastery(0.5, True), 0.65)
        self.assertEqual(next_mastery(0.5, False), 0.25)
        self.assertEqual(next_mastery(0.99, True), 1.0)
        self.assertEqual(next_mastery(0.1, False), 0.0)

    def test_rating_out_of_range_clamped(self):
        self.assertEqual(next_interval_days(10, 99, 0), 32.0)
        self.assertEqual(next_interval_days(10, -3, 0), 1.0)

    def test_consecutive_wrong_count(self):
        hist = [{"is_correct": 1}, {"is_correct": 0}, {"is_correct": 0}]
        self.assertEqual(consecutive_wrong_count(hist), 2)
        self.assertEqual(consecutive_wrong_count([{"is_correct": 1}]), 0)
        self.assertEqual(consecutive_wrong_count([]), 0)


class TestGrading(unittest.TestCase):
    def test_exact_and_normalized(self):
        self.assertTrue(grade_answer("牛顿第二定律", "牛顿第二定律"))
        self.assertTrue(grade_answer("F = ma", "f=ma"))
        self.assertTrue(grade_answer("能量守恒。", "能量守恒"))

    def test_containment_for_long_answers(self):
        self.assertTrue(grade_answer("动量守恒", "根据条件，系统满足动量守恒定律"))
        self.assertFalse(grade_answer("动量守恒定律的内容", "能量守恒"))

    def test_wrong_answer_rejected(self):
        self.assertFalse(grade_answer("折射角小于入射角", "折射角大于入射角"))
        self.assertFalse(grade_answer("正确答案", ""))


class TestReviewFlow(unittest.TestCase):
    """接口级：建复习包 → 作答 → 完成（测试环境不跑真实模型，直接插库）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        db.configure_db(self.tmp / "t.db")
        db.init_db()
        from app import config as app_config
        self._saved_token = app_config.MOBILE_TOKEN
        app_config.MOBILE_TOKEN = ""
        db.insert("INSERT INTO courses (name, semester) VALUES ('测试课程', '2026春')")
        db.insert("INSERT INTO chapters (course_id, chapter_no, title) VALUES (1, 1, '第一章')")
        self.review_id = self._mk_review()
        self.error_id = db.insert(
            "INSERT INTO errors (course_id, question_text, correct_answer, mastery, status) "
            "VALUES (1, '什么是光的折射？', '光进入另一介质时传播方向发生偏折', 0.5, 'confirmed')",
        )

    def _mk_review(self):
        import json
        questions = [
            {"question_no": "1", "q": "什么是光的折射？", "answer": "光进入另一介质时传播方向发生偏折", "error_id": None},
            {"question_no": "2", "q": "折射定律内容？", "answer": "sin i / sin r = 常数", "error_id": None},
        ]
        return db.insert(
            "INSERT INTO reviews (course_id, chapter_id, kind, outline, self_test, status, score) "
            "VALUES (1, 1, 'chapter', ?, ?, 'generated', 0)",
            (json.dumps(["大纲1"], ensure_ascii=False),
             json.dumps(questions, ensure_ascii=False)),
        )

    def tearDown(self):
        db.reset_connections()
        from app import config as app_config
        app_config.MOBILE_TOKEN = self._saved_token

    def test_attempt_grades_and_updates_sm2(self):
        from fastapi.testclient import TestClient
        from app.main import app
        # 关联 error_id 到题1
        import json as _json
        row = db.fetch_one("SELECT self_test FROM reviews WHERE id=?", (self.review_id,))
        qs = _json.loads(row["self_test"])
        qs[0]["error_id"] = self.error_id
        db.execute("UPDATE reviews SET self_test=? WHERE id=?",
                   (_json.dumps(qs, ensure_ascii=False), self.review_id))

        with TestClient(app) as client:
            # 默认隐藏答案
            r = client.get(f"/api/reviews/{self.review_id}")
            self.assertEqual(r.status_code, 200)
            self.assertNotIn("answer", r.json()["questions"][0])
            # 答对（自评 4）→ mastery +0.15，间隔 ×2.5
            r = client.post(f"/api/reviews/{self.review_id}/attempts",
                            json={"question_no": "1", "user_answer": "光进入另一介质时传播方向发生偏折",
                                  "self_rating": 4})
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertTrue(body["is_correct"])
            self.assertEqual(body["mastery_after"], 0.65)
            self.assertEqual(body["interval_after_days"], 2.5)
            # 作答后该题答案可见
            r = client.get(f"/api/reviews/{self.review_id}")
            q1 = next(q for q in r.json()["questions"] if q["question_no"] == "1")
            self.assertIn("answer", q1)
            # 答错（自评 1）→ 间隔 1 天，mastery -0.25
            r = client.post(f"/api/reviews/{self.review_id}/attempts",
                            json={"question_no": "1", "user_answer": "完全不对", "self_rating": 1})
            body2 = r.json()
            self.assertFalse(body2["is_correct"])
            self.assertEqual(body2["mastery_after"], 0.4)
            self.assertEqual(body2["interval_after_days"], 1.0)
            # errors 表同步
            err = db.fetch_one("SELECT mastery, next_review_at FROM errors WHERE id=?", (self.error_id,))
            self.assertEqual(err["mastery"], 0.4)
            self.assertIsNotNone(err["next_review_at"])

    def test_complete_review(self):
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as client:
            client.post(f"/api/reviews/{self.review_id}/attempts",
                        json={"question_no": "1", "user_answer": "光进入另一介质时传播方向发生偏折",
                              "self_rating": 5})
            r = client.post(f"/api/reviews/{self.review_id}/complete")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body["attempts"], 1)
            self.assertEqual(body["correct"], 1)
            self.assertEqual(body["score"], 1.0)
            # 重复完成 → 409
            r2 = client.post(f"/api/reviews/{self.review_id}/complete")
            self.assertEqual(r2.status_code, 409)

    def test_unknown_question_404(self):
        from fastapi.testclient import TestClient
        from app.main import app
        r = TestClient(app).post(f"/api/reviews/{self.review_id}/attempts",
                                 json={"question_no": "99", "user_answer": "x"})
        self.assertEqual(r.status_code, 404)

    def test_attempt_history_preserved(self):
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as client:
            for i in range(3):
                client.post(f"/api/reviews/{self.review_id}/attempts",
                            json={"question_no": "2", "user_answer": f"答{i}", "self_rating": 2})
        rows = db.fetch_all("SELECT * FROM review_attempts WHERE review_id=? ORDER BY id",
                            (self.review_id,))
        self.assertEqual(len(rows), 3, "作答历史不允许被覆盖")
        self.assertEqual(rows[0]["interval_after"], 3.0)   # rating=2 → 3d
        self.assertEqual(rows[2]["interval_before"], 3.0)  # 上次间隔成为本次基准


if __name__ == "__main__":
    unittest.main()
