"""课程删除：隔离数据库验证影响预览、名称确认和级联清理。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_v564_gateway_mode import Base


class TestCourseDelete(Base):
    def _seed_course(self) -> int:
        from app.database import insert

        course_id = insert(
            "INSERT INTO courses (name, code, semester, teacher, schedule_json) VALUES (?,?,?,?,?)",
            ("防误删测试课", "SAFE101", "测试学期", "测试教师", "{}"),
        )
        chapter_id = insert(
            "INSERT INTO chapters (course_id, chapter_no, title, status) VALUES (?,?,?,'not_started')",
            (course_id, 1, "测试章节"),
        )
        insert(
            "INSERT INTO lessons (chapter_id, course_id, lesson_no, title) VALUES (?,?,?,?)",
            (chapter_id, course_id, 1, "测试课时"),
        )
        insert(
            "INSERT INTO course_schedule_rules "
            "(course_id, weekday, start_week, end_week, week_parity, periods_json) "
            "VALUES (?,?,?,?,?,?)",
            (course_id, 1, 1, 16, "all", "[1,2]"),
        )
        insert(
            "INSERT INTO academic_calendar (course_id, event_type, title, date) VALUES (?,?,?,?)",
            (course_id, "exam", "测试考试", "2026-12-01"),
        )
        return course_id

    def test_delete_requires_exact_name_and_reports_impact(self):
        course_id = self._seed_course()
        from app.database import query_one
        from app.main import app

        with TestClient(app) as client:
            impact = client.get(f"/api/courses/{course_id}/delete-impact")
            self.assertEqual(impact.status_code, 200)
            counts = impact.json()["counts"]
            self.assertEqual(counts["chapters"], 1)
            self.assertEqual(counts["lessons"], 1)
            self.assertEqual(counts["course_schedule_rules"], 1)
            self.assertEqual(counts["academic_calendar"], 1)

            rejected = client.delete(
                f"/api/courses/{course_id}", params={"confirm_name": "错误名称"}
            )
            self.assertEqual(rejected.status_code, 409)
            self.assertIsNotNone(query_one("SELECT id FROM courses WHERE id=?", (course_id,)))

            deleted = client.delete(
                f"/api/courses/{course_id}", params={"confirm_name": "防误删测试课"}
            )
            self.assertEqual(deleted.status_code, 200)
            self.assertEqual(deleted.json()["counts"]["lessons"], 1)

        self.assertIsNone(query_one("SELECT id FROM courses WHERE id=?", (course_id,)))
        self.assertIsNone(query_one("SELECT id FROM chapters WHERE course_id=?", (course_id,)))
        self.assertIsNone(query_one("SELECT id FROM lessons WHERE course_id=?", (course_id,)))
        self.assertIsNone(query_one("SELECT id FROM course_schedule_rules WHERE course_id=?", (course_id,)))
        # 考试记录保留，但解除课程关联，避免误删独立校历信息。
        event = query_one("SELECT course_id FROM academic_calendar WHERE title='测试考试'")
        self.assertIsNotNone(event)
        self.assertIsNone(event["course_id"])

