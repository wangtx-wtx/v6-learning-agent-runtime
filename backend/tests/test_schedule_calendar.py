"""课表与校历：隔离数据库下验证规则、例外优先级和 API。"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from tests.test_v564_gateway_mode import Base


class TestScheduleCalendar(Base):
    def _seed(self):
        from app.database import insert
        c1 = insert(
            "INSERT INTO courses (name, code, teacher, schedule_json) VALUES (?,?,?,?)",
            (
                "程序设计", "CS101", "齐老师",
                json.dumps([{
                    "weeks": "1-16", "day": 1, "periods": [1, 2],
                    "location": "闵二教202", "teacher": "齐老师",
                }], ensure_ascii=False),
            ),
        )
        c2 = insert(
            "INSERT INTO courses (name, code, schedule_json) VALUES (?,?,?)",
            (
                "光学", "PHYS2509",
                json.dumps([{
                    "weeks": "1-15单", "day": 2, "periods": [3, 4], "location": "闵二教307",
                }], ensure_ascii=False),
            ),
        )
        calendar = self.tmp / "calendar.json"
        calendar.write_text(json.dumps({
            "semester": "2026-2027-1", "label": "测试学期", "registration": "2026-09-13",
            "firstWeekMonday": "2026-09-14", "teachingWeeks": 16,
            "teachingEnd": "2027-01-01", "examPeriod": {"start": "2027-01-04", "end": "2027-01-15"},
            "holidays": [{"name": "国庆节", "date": "2026-10-01"}],
            "specialEvents": [
                {"name": "运动会", "date": "2026-10-27", "note": "全天停课"},
                {"name": "主题活动日", "date": "2026-10-28", "note": "上下午停课，晚上不停课"},
            ],
            "workdays": [
                {"name": "国庆补班", "date": "2026-10-10", "sourceDate": "2026-10-05"},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        from app.schedule_calendar import bootstrap_schedule_data
        stats = bootstrap_schedule_data(calendar)
        self.assertEqual(stats, {"term_created": 1, "rules_created": 2, "adjustments_created": 4})
        return c1, c2

    def test_parse_week_spec(self):
        from app.schedule_calendar import parse_week_spec
        self.assertEqual(parse_week_spec("1-15单"), (1, 15, "odd"))
        self.assertEqual(parse_week_spec("2-16双"), (2, 16, "even"))
        self.assertEqual(parse_week_spec("3"), (3, 3, "all"))

    def test_bootstrap_and_odd_week_schedule(self):
        self._seed()
        from app.schedule_calendar import effective_schedule
        result = effective_schedule("2026-09-14", 2)
        self.assertEqual(result["term"]["name"], "测试学期")
        self.assertEqual([c["course_name"] for c in result["days"][0]["courses"]], ["程序设计"])
        self.assertEqual([c["course_name"] for c in result["days"][1]["courses"]], ["光学"])
        # 第二教学周为双周，单周光学不出现。
        week_two = effective_schedule("2026-09-22", 1)
        self.assertEqual(week_two["days"][0]["courses"], [])
        imported_workday = effective_schedule("2026-10-10", 1)["days"][0]
        self.assertEqual(imported_workday["effective_weekday"], 1)
        self.assertTrue(all(c["source_date"] == "2026-10-05" for c in imported_workday["courses"]))

    def test_holiday_and_workday_precedence(self):
        self._seed()
        from app.schedule_calendar import effective_schedule, save_adjustment
        save_adjustment({"adjustment_type": "holiday", "date": "2026-09-14", "title": "测试放假"})
        monday = effective_schedule("2026-09-14", 1)["days"][0]
        self.assertTrue(monday["is_day_off"])
        self.assertEqual(monday["courses"], [])
        save_adjustment({
            "adjustment_type": "workday", "date": "2026-09-19", "source_date": "2026-09-14",
            "title": "补周一课程",
        })
        saturday = effective_schedule("2026-09-19", 1)["days"][0]
        self.assertEqual(saturday["effective_weekday"], 1)
        self.assertEqual([c["course_name"] for c in saturday["courses"]], ["程序设计"])
        self.assertEqual(saturday["courses"][0]["status"], "workday")
        self.assertEqual(saturday["courses"][0]["source_date"], "2026-09-14")

    def test_workday_uses_source_date_week_and_parity(self):
        self._seed()
        from app.schedule_calendar import effective_schedule, save_adjustment
        # 9 月 22 日为第 2 周周二，单周课程“光学”不应被复制到补班日。
        save_adjustment({
            "adjustment_type": "workday", "date": "2026-09-26", "source_date": "2026-09-22",
            "title": "补第 2 周周二课程",
        })
        saturday = effective_schedule("2026-09-26", 1)["days"][0]
        self.assertEqual(saturday["effective_weekday"], 2)
        self.assertEqual(saturday["courses"], [])

    def test_existing_academic_calendar_holiday_is_effective(self):
        self._seed()
        from app.database import insert
        from app.schedule_calendar import effective_schedule
        insert(
            "INSERT INTO academic_calendar (event_type, title, date, detail) VALUES (?,?,?,?)",
            ("holiday", "国庆节", "2026-09-14", "学校既有校历事件"),
        )
        monday = effective_schedule("2026-09-14", 1)["days"][0]
        self.assertTrue(monday["is_day_off"])
        self.assertEqual(monday["courses"], [])
        self.assertEqual(monday["events"][0]["title"], "国庆节")

    def test_partial_day_off_preserves_evening_courses(self):
        self._seed()
        from app.database import insert
        from app.schedule_calendar import effective_schedule, save_adjustment
        insert(
            "INSERT INTO course_schedule_rules "
            "(course_id, weekday, start_week, end_week, week_parity, periods_json) "
            "VALUES (1,3,1,16,'all','[11,12]')"
        )
        save_adjustment({
            "adjustment_type": "day_off", "date": "2026-09-16", "periods": list(range(1, 11)),
            "title": "上下午停课",
        })
        day = effective_schedule("2026-09-16", 1)["days"][0]
        self.assertFalse(day["is_day_off"])
        self.assertEqual([course["periods"] for course in day["courses"]], [[11, 12]])

    def test_api_crud(self):
        self._seed()
        from app.main import app
        client = TestClient(app)
        self.assertEqual(client.get("/api/schedule/effective?start=2026-09-14&days=7").status_code, 200)
        created = client.post("/api/calendar-adjustments", json={
            "adjustment_type": "day_off", "date": "2026-09-14", "title": "临时调休",
        })
        self.assertEqual(created.status_code, 200)
        adjustment_id = created.json()["id"]
        rows = client.get("/api/calendar-adjustments").json()
        self.assertTrue(any(row["id"] == adjustment_id for row in rows))
        self.assertEqual(client.delete(f"/api/calendar-adjustments/{adjustment_id}").status_code, 200)

    def test_invalid_adjustments_are_rejected(self):
        self._seed()
        from app.main import app
        client = TestClient(app)
        response = client.post("/api/calendar-adjustments", json={
            "adjustment_type": "workday", "date": "2026-09-19",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("补哪一天", response.json()["detail"])
