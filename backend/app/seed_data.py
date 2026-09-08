"""Seed academic_calendar from legacy calendar.json/courses.json."""
import json
import datetime
from pathlib import Path
from typing import Optional

from .database import query, execute, insert

LEGACY_CALENDAR_PATH = Path(r"C:\Users\28595\Desktop\新建文件夹 (4)\自动化学习框架\data\courses\calendar.json")
LEGACY_COURSES_PATH = Path(r"C:\Users\28595\Desktop\新建文件夹 (4)\自动化学习框架\data\courses\courses.json")


def parse_week_range(weeks_str):
    odd_only = str(weeks_str.strip()).endswith("单")
    s = str(weeks_str.strip()).rstrip("单")
    if "-" in s:
        a, b = s.split("-", 1)
        return int(a), int(b), odd_only
    return int(s), int(s), odd_only


def _week_to_date(first_monday: str, week_no: int, weekday: int) -> str:
    d = datetime.date.fromisoformat(first_monday)
    return (d + datetime.timedelta(days=(week_no - 1) * 7 + (weekday - 1))).isoformat()


def _expand_schedule_dates(schedule, first_monday: str):
    dates = []
    if not isinstance(schedule, list):
        schedule = [schedule]
    for item in schedule:
        if not isinstance(item, dict):
            continue
        weeks_str = item.get("weeks", "1-16")
        day = int(item.get("day", 1))
        try:
            start, end, odd_only = parse_week_range(weeks_str)
        except Exception:
            start, end, odd_only = 1, 16, False
        for wk in range(start, end + 1):
            if odd_only and wk % 2 == 0:
                continue
            dates.append(_week_to_date(first_monday, wk, day))
    return sorted(set(dates))


def _safe_dump(schedule):
    try:
        return json.dumps(schedule or [], ensure_ascii=False)
    except Exception:
        return "[]"


def seed_calendar(calendar_path: Optional[Path] = None, courses_path: Optional[Path] = None, force: bool = False) -> dict:
    """导入旧项目校历 + 课程表，生成校历事件与期末考试（期末周自动排，其余手动输入）。"""
    cal_path = calendar_path or LEGACY_CALENDAR_PATH
    c_path = courses_path or LEGACY_COURSES_PATH

    if not cal_path.exists() or not c_path.exists():
        return {"status": "skipped", "reason": f"legacy data missing: cal={cal_path.exists()} courses={c_path.exists()}"}

    cal = json.loads(cal_path.read_text(encoding="utf-8"))
    legacy_courses = json.loads(c_path.read_text(encoding="utf-8"))["courses"]

    existing = query("SELECT COUNT(*) AS c FROM academic_calendar")[0]["c"]
    if existing and not force:
        return {"status": "already_seeded", "count": existing}

    first_monday = cal.get("firstWeekMonday", "2026-09-14")
    exam_start = cal.get("examPeriod", {}).get("start", "2027-01-04")
    exam_end = cal.get("examPeriod", {}).get("end", "2027-01-15")

    stats = {"calendar_events": 0, "exam_dates": 0, "lesson_dates": 0, "teachers_updated": 0}

    # ---------- 1. 全局校历事件 ----------
    events = []
    events.append(("global", "报到注册", cal.get("registration"), "2026-2027 学年第一学期注册"))
    events.append(("global", "开学（第1周）", first_monday, "正式上课"))
    events.append(("global", "教学周结束", cal.get("teachingEnd"), f"共 {cal.get('teachingWeeks','?')} 周教学"))
    events.append(("global", "期末考试周", f"{exam_start}~{exam_end}", "期末集中考试"))
    if cal.get("winterVacation"):
        events.append(("global", "寒假开始", cal["winterVacation"].get("start"), "寒假"))
    if cal.get("makeupExam"):
        events.append(("global", "补考", "", cal.get("makeupExam")))
    for h_ in cal.get("holidays", []):
        events.append(("holiday", f"{h_.get('name','')}放假", h_.get("date", ""), h_.get("note", "")))
    for s_ in cal.get("specialEvents", []):
        events.append(("special", s_.get("name", ""), s_.get("date", ""), s_.get("note", "")))

    for typ, title, dt, detail in events:
        insert(
            "INSERT INTO academic_calendar (course_id, event_type, title, date, detail) VALUES (NULL,?,?,?,?)",
            (typ, title, dt or None, detail or None),
        )
        stats["calendar_events"] += 1

    # ---------- 2. 课程进修：更新 teacher / schedule_json 并排期末 ----------
    rows = query("SELECT id, code, name, teacher, schedule_json FROM courses")
    all_courses = list(rows)
    by_code = {r["code"]: r for r in all_courses if r.get("code")}
    by_name = {r["name"]: r for r in all_courses}

    exam_dates_added = []
    for lc in legacy_courses:
        code = lc.get("code") or ""
        name = lc.get("name") or ""
        rec = by_code.get(code) or by_name.get(name)
        if not rec:
            teacher = lc.get("schedule", [{}])[0].get("teacher") if lc.get("schedule") else None
            sid = insert(
                "INSERT INTO courses (name, code, semester, teacher, schedule_json) VALUES (?,?,?,?,?)",
                (lc.get("name"), code, cal.get("semester"), teacher, _safe_dump(lc.get("schedule", []))),

)
            rec = {"id": sid, "teacher": teacher, "schedule_json": _safe_dump(lc.get("schedule", []))}
            stats["teachers_updated"] += 1

        # Update teacher on db course
        t_list = [x.get("teacher") for x in lc.get("schedule", []) if x.get("teacher")]
        teacher = ";".join(sorted(set(t for t in t_list if t))) or rec.get("teacher")
        if rec.get("teacher") != teacher:
            execute("UPDATE courses SET teacher=? WHERE id=?", (teacher, rec["id"]))
            stats["teachers_updated"] += 1

        # Update schedule_json if empty
        sched_json = rec.get("schedule_json") or ""
        if not sched_json.strip() or sched_json.strip() == "{}":
            execute(
                "UPDATE courses SET schedule_json=? WHERE id=?",
                (_safe_dump(lc.get("schedule", [])), rec["id"]),
            )

        # Lesson dates 占位：每个课程第一讲日期
        sched = lc.get("schedule", [])
        if sched:
            dates = _expand_schedule_dates(sched, first_monday)
            if dates:
                lesson_date = dates[0]
                has_lesson = query("SELECT COUNT(*) AS c FROM lessons WHERE course_id=?", (rec["id"],))[0]["c"]
                if not has_lesson:
                    chid = None
                    ch_rows = query("SELECT id FROM chapters WHERE course_id=? ORDER BY chapter_no LIMIT 1", (rec["id"],))
                    if ch_rows:
                        chid = ch_rows[0]["id"]
                    else:
                        chid = insert(
                            "INSERT INTO chapters (course_id, chapter_no, title, status) VALUES (?,?,?,?)",
                            (rec["id"], 1, "第1章（未定）", "not_started"),

)
                    insert(
                        "INSERT INTO lessons (chapter_id, course_id, lesson_no, title, date, status) VALUES (?,?,?,?,?,?)",
                        (chid, rec["id"], "L01", "第1讲", lesson_date, "not_started"),
                    )
                    stats["lesson_dates"] += 1

        # 期末：给每个课程分配一个考试周工作日
        if rec.get("id"):
            idx = (rec["id"] - 1) % max(1, 10)
            d = datetime.date.fromisoformat(exam_start) + datetime.timedelta(days=idx)
            insert(
                "INSERT INTO academic_calendar (course_id, event_type, title, date, detail) VALUES (?,?,?,?,?)",
                (rec["id"], "exam", f"期末考试：{lc.get('name','')}", d.isoformat(),
                 f"{code} 期末建议日期（请根据教务处通知手动核正）"),
            )
            stats["exam_dates"] += 1
            exam_dates_added.append(d.isoformat())

    return {"status": "ok",
            "calendar_events": stats["calendar_events"],
            "exam_dates": stats["exam_dates"],
            "lesson_dates": stats["lesson_dates"],
            "teachers_updated": stats["teachers_updated"],
            "exam_span": f"{exam_start}~{exam_end}", "exam_dates_added": exam_dates_added}
