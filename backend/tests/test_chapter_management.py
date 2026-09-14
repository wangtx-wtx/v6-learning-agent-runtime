"""章节管理：隔离数据库验证备注、编辑、状态和防误删。"""
from fastapi.testclient import TestClient

from tests.test_v564_gateway_mode import Base


class TestChapterManagement(Base):
    def test_crud_notes_and_exact_title_delete(self):
        from app.database import insert, query_one
        from app.main import app

        course_id = insert("INSERT INTO courses (name) VALUES (?)", ("章节管理测试课",))
        with TestClient(app) as client:
            created = client.post("/api/chapters", json={
                "course_id": course_id, "chapter_no": 1,
                "title": "绪论", "notes": "第一条备注",
            })
            self.assertEqual(created.status_code, 200)
            chapter_id = created.json()["id"]

            updated = client.put(f"/api/chapters/{chapter_id}", json={
                "chapter_no": 2, "title": "力学基础",
                "notes": "教师强调矢量分解", "status": "in_progress",
            })
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(updated.json()["notes"], "教师强调矢量分解")
            self.assertEqual(updated.json()["status"], "in_progress")

            rejected = client.delete(
                f"/api/chapters/{chapter_id}", params={"confirm_title": "错误名称"}
            )
            self.assertEqual(rejected.status_code, 409)
            self.assertIsNotNone(query_one("SELECT id FROM chapters WHERE id=?", (chapter_id,)))

            deleted = client.delete(
                f"/api/chapters/{chapter_id}", params={"confirm_title": "力学基础"}
            )
            self.assertEqual(deleted.status_code, 200)
            self.assertIsNone(query_one("SELECT id FROM chapters WHERE id=?", (chapter_id,)))
