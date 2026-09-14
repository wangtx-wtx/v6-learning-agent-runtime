"""学期、固定课表和调课调休的确定性计算服务。"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .database import execute, insert, query, query_one, transaction


LEGACY_CALENDAR_PATH = Path(
    r"C:\Users\28595\Desktop\新建文件夹 (4)\自动化学习框架\data\courses\calendar.json"
)

ADJUSTMENT_TYPES = {"holiday", "day_off", "workday"}


def _json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _iso_date(value: str, field: str = "date") -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        raise ValueError(f"{field} 必须是 YYYY-MM-DD")


def parse_week_spec(spec: Any, default_end: int = 16) -> tuple[int, int, str]:
    """把 1-16 / 1-15单 / 2-16双 / 3 转成起止周和单双周。"""
    text = str(spec or "").strip()
    parity = "all"
    if text.endswith("单"):
        parity, text = "odd", text[:-1]
    elif text.endswith("双"):
        parity, text = "even", text[:-1]
    match = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", text)
    if not match:
        return 1, max(1, int(default_end or 16)), parity
    start = int(match.group(1))
    end = int(match.group(2) or start)
    return max(1, start), max(start, end), parity


def bootstrap_schedule_data(calendar_path: Optional[Path] = None) -> dict:
    """仅在空表时，把现有校历和 courses.schedule_json 规范化导入。"""
    stats = {"term_created": 0, "rules_created": 0, "adjustments_created": 0}
    cal_path = calendar_path or LEGACY_CALENDAR_PATH
    cal: dict[str, Any] = {}
    if cal_path.exists():
        try:
            cal = json.loads(cal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            cal = {}

    if not query_one("SELECT id FROM academic_terms LIMIT 1") and cal.get("firstWeekMonday"):
        first_monday = _iso_date(cal["firstWeekMonday"], "firstWeekMonday")
        teaching_weeks = max(1, int(cal.get("teachingWeeks") or 16))
        start_date = cal.get("registration") or first_monday
        end_date = cal.get("teachingEnd") or (
            date.fromisoformat(first_monday) + timedelta(days=teaching_weeks * 7 - 1)
        ).isoformat()
        exam = cal.get("examPeriod") or {}
        insert(
            "INSERT INTO academic_terms "
            "(name, school_year, semester, start_date, end_date, first_week_monday, "
            "teaching_weeks, exam_start, exam_end, source, active) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
            (
                cal.get("label") or cal.get("termName") or cal.get("semester") or "当前学期",
                str(cal.get("semester") or "").rsplit("-", 1)[0] or None,
                cal.get("semester"), _iso_date(start_date, "start_date"), _iso_date(end_date, "end_date"),
                first_monday, teaching_weeks, exam.get("start"), exam.get("end"), str(cal_path),
            ),
        )
        stats["term_created"] = 1

    if not query_one("SELECT id FROM course_schedule_rules LIMIT 1"):
        default_end = int((query_one(
            "SELECT teaching_weeks FROM academic_terms WHERE active=1 ORDER BY id DESC LIMIT 1"
        ) or {}).get("teaching_weeks") or 16)
        for course in query("SELECT id, teacher, schedule_json FROM courses ORDER BY id"):
            for item in _json_list(course.get("schedule_json")):
                if not isinstance(item, dict):
                    continue
                weekday = int(item.get("day") or 0)
                if weekday < 1 or weekday > 7:
                    continue
                start_week, end_week, parity = parse_week_spec(item.get("weeks"), default_end)
                insert(
                    "INSERT INTO course_schedule_rules "
                    "(course_id, weekday, start_week, end_week, week_parity, periods_json, "
                    "start_time, end_time, location, teacher, note, source) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,'legacy')",
                    (
                        course["id"], weekday, start_week, end_week, parity,
                        json.dumps(_json_list(item.get("periods")), ensure_ascii=False),
                        item.get("start"), item.get("end"), item.get("location"),
                        item.get("teacher") or course.get("teacher"), item.get("note"),
                    ),
                )
                stats["rules_created"] += 1

    if cal:
        desired_adjustments: list[dict[str, Any]] = []
        for holiday in cal.get("holidays") or []:
            if not isinstance(holiday, dict) or not holiday.get("date"):
                continue
            desired_adjustments.append({
                "adjustment_type": "holiday", "date": _iso_date(holiday["date"]),
                "periods": [], "title": holiday.get("name") or "节假日",
                "detail": "校历原始数据", "source_date": None,
            })
        for event in cal.get("specialEvents") or []:
            if not isinstance(event, dict) or not event.get("date"):
                continue
            note = str(event.get("note") or "")
            periods = list(range(1, 11)) if "晚上不停课" in note else []
            desired_adjustments.append({
                "adjustment_type": "day_off", "date": _iso_date(event["date"]),
                "periods": periods, "title": event.get("name") or "校历停课",
                "detail": note or "校历原始数据", "source_date": None,
            })
        for workday in cal.get("workdays") or []:
            if not isinstance(workday, dict) or not workday.get("date") or not workday.get("sourceDate"):
                continue
            desired_adjustments.append({
                "adjustment_type": "workday", "date": _iso_date(workday["date"]),
                "periods": [], "title": workday.get("name") or "补班",
                "detail": workday.get("note") or "校历原始数据",
                "source_date": _iso_date(workday["sourceDate"], "sourceDate"),
            })
        for item in desired_adjustments:
            existing = query_one(
                "SELECT id FROM calendar_adjustments WHERE source='legacy_calendar' "
                "AND adjustment_type=? AND date=? AND title=? LIMIT 1",
                (item["adjustment_type"], item["date"], item["title"]),
            )
            values = (
                json.dumps(item["periods"], ensure_ascii=False), item["detail"],
                item["source_date"], item["adjustment_type"], item["date"], item["title"],
            )
            if existing:
                execute(
                    "UPDATE calendar_adjustments SET periods_json=?, detail=?, source_date=?, "
                    "updated_at=datetime('now','localtime') WHERE adjustment_type=? AND date=? "
                    "AND title=? AND source='legacy_calendar'",
                    values,
                )
            else:
                insert(
                    "INSERT INTO calendar_adjustments "
                    "(adjustment_type, date, periods_json, title, detail, source_date, source) "
                    "VALUES (?,?,?,?,?,?,'legacy_calendar')",
                    (
                        item["adjustment_type"], item["date"],
                        json.dumps(item["periods"], ensure_ascii=False), item["title"],
                        item["detail"], item["source_date"],
                    ),
                )
                stats["adjustments_created"] += 1
    return stats


def list_terms() -> list[dict]:
    return query("SELECT * FROM academic_terms ORDER BY active DESC, start_date DESC, id DESC")


def save_term(data: dict, term_id: Optional[int] = None) -> dict:
    first = _iso_date(data.get("first_week_monday"), "first_week_monday")
    start = _iso_date(data.get("start_date"), "start_date")
    end = _iso_date(data.get("end_date"), "end_date")
    if end < start:
        raise ValueError("end_date 不能早于 start_date")
    teaching_weeks = int(data.get("teaching_weeks") or 16)
    if teaching_weeks < 1 or teaching_weeks > 30:
        raise ValueError("teaching_weeks 必须在 1 到 30 之间")
    active = 1 if data.get("active", True) else 0
    values = (
        str(data.get("name") or "当前学期").strip(), data.get("school_year"), data.get("semester"),
        start, end, first, teaching_weeks, data.get("exam_start") or None,
        data.get("exam_end") or None, data.get("source") or "manual", active,
    )
    with transaction() as conn:
        if active:
            conn.execute("UPDATE academic_terms SET active=0 WHERE active=1")
        if term_id is None:
            cur = conn.execute(
                "INSERT INTO academic_terms (name, school_year, semester, start_date, end_date, "
                "first_week_monday, teaching_weeks, exam_start, exam_end, source, active) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)", values,
            )
            term_id = int(cur.lastrowid)
        else:
            cur = conn.execute(
                "UPDATE academic_terms SET name=?, school_year=?, semester=?, start_date=?, end_date=?, "
                "first_week_monday=?, teaching_weeks=?, exam_start=?, exam_end=?, source=?, active=?, "
                "updated_at=datetime('now','localtime') WHERE id=?", values + (term_id,),
            )
            if not cur.rowcount:
                raise LookupError("学期不存在")
    return query_one("SELECT * FROM academic_terms WHERE id=?", (term_id,)) or {}


def list_rules(course_id: Optional[int] = None) -> list[dict]:
    sql = (
        "SELECT r.*, c.name AS course_name, c.code AS course_code FROM course_schedule_rules r "
        "JOIN courses c ON c.id=r.course_id WHERE 1=1"
    )
    params: list[Any] = []
    if course_id is not None:
        sql += " AND r.course_id=?"
        params.append(course_id)
    sql += " ORDER BY r.weekday, json_extract(r.periods_json, '$[0]'), r.id"
    rows = query(sql, tuple(params))
    for row in rows:
        row["periods"] = _json_list(row.pop("periods_json", "[]"))
    return rows


def save_rule(data: dict, rule_id: Optional[int] = None) -> dict:
    course_id = int(data.get("course_id") or 0)
    if not query_one("SELECT id FROM courses WHERE id=?", (course_id,)):
        raise ValueError("请选择有效课程")
    weekday = int(data.get("weekday") or 0)
    start_week = int(data.get("start_week") or 1)
    end_week = int(data.get("end_week") or start_week)
    parity = str(data.get("week_parity") or "all")
    if weekday not in range(1, 8):
        raise ValueError("weekday 必须在 1 到 7 之间")
    if start_week < 1 or end_week < start_week:
        raise ValueError("周次范围无效")
    if parity not in {"all", "odd", "even"}:
        raise ValueError("week_parity 必须是 all、odd 或 even")
    periods = [int(p) for p in _json_list(data.get("periods")) if str(p).isdigit()]
    values = (
        course_id, weekday, start_week, end_week, parity,
        json.dumps(periods, ensure_ascii=False), data.get("start_time") or None,
        data.get("end_time") or None, data.get("location") or None,
        data.get("teacher") or None, data.get("note") or None,
        data.get("source") or "manual", 1 if data.get("enabled", True) else 0,
    )
    if rule_id is None:
        rule_id = insert(
            "INSERT INTO course_schedule_rules (course_id, weekday, start_week, end_week, week_parity, "
            "periods_json, start_time, end_time, location, teacher, note, source, enabled) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", values,
        )
    else:
        count = execute(
            "UPDATE course_schedule_rules SET course_id=?, weekday=?, start_week=?, end_week=?, "
            "week_parity=?, periods_json=?, start_time=?, end_time=?, location=?, teacher=?, note=?, "
            "source=?, enabled=?, updated_at=datetime('now','localtime') WHERE id=?", values + (rule_id,),
        )
        if not count:
            raise LookupError("固定课表规则不存在")
    return next((r for r in list_rules() if r["id"] == rule_id), {})


def list_adjustments(start: Optional[str] = None, end: Optional[str] = None) -> list[dict]:
    sql = (
        "SELECT a.*, c.name AS course_name FROM calendar_adjustments a "
        "LEFT JOIN courses c ON c.id=a.course_id WHERE 1=1"
    )
    params: list[Any] = []
    if start:
        sql += " AND a.date>=?"
        params.append(_iso_date(start, "start"))
    if end:
        sql += " AND a.date<=?"
        params.append(_iso_date(end, "end"))
    sql += " ORDER BY a.date DESC, a.id DESC"
    rows = query(sql, tuple(params))
    for row in rows:
        row["periods"] = _json_list(row.pop("periods_json", "[]"))
        row["target_periods"] = _json_list(row.pop("target_periods_json", "[]"))
    return rows


def save_adjustment(data: dict, adjustment_id: Optional[int] = None) -> dict:
    kind = str(data.get("adjustment_type") or "")
    if kind not in ADJUSTMENT_TYPES:
        raise ValueError("不支持的调整类型")
    source_date = _iso_date(data.get("date"), "date")
    source_calendar_date = data.get("source_date") or None
    if kind == "workday" and not source_calendar_date:
        raise ValueError("补班必须指定要补哪一天的课程")
    if source_calendar_date:
        source_calendar_date = _iso_date(source_calendar_date, "source_date")
    values = (
        kind, None, source_date, None, None,
        json.dumps(_json_list(data.get("periods")), ensure_ascii=False),
        "[]", None, None, None, None, None, None,
        data.get("title") or None, data.get("detail") or None,
        source_calendar_date, data.get("source") or "manual",
    )
    if adjustment_id is None:
        adjustment_id = insert(
            "INSERT INTO calendar_adjustments (adjustment_type, course_id, date, target_date, "
            "source_weekday, periods_json, target_periods_json, start_time, end_time, "
            "target_start_time, target_end_time, location, teacher, title, detail, source_date, source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values,
        )
    else:
        count = execute(
            "UPDATE calendar_adjustments SET adjustment_type=?, course_id=?, date=?, target_date=?, "
            "source_weekday=?, periods_json=?, target_periods_json=?, start_time=?, end_time=?, "
            "target_start_time=?, target_end_time=?, location=?, teacher=?, title=?, detail=?, "
            "source_date=?, source=?, updated_at=datetime('now','localtime') WHERE id=?",
            values + (adjustment_id,),
        )
        if not count:
            raise LookupError("临时调整不存在")
    return next((a for a in list_adjustments() if a["id"] == adjustment_id), {})


def _week_number(day: date, first_monday: Optional[str]) -> Optional[int]:
    if not first_monday:
        return None
    delta = (day - date.fromisoformat(first_monday)).days
    return delta // 7 + 1 if delta >= 0 else None


def _rule_applies(rule: dict, week_no: Optional[int]) -> bool:
    if week_no is None or week_no < int(rule["start_week"]) or week_no > int(rule["end_week"]):
        return False
    parity = rule.get("week_parity") or "all"
    return parity == "all" or (parity == "odd" and week_no % 2 == 1) or (
        parity == "even" and week_no % 2 == 0
    )


def _period_match(occurrence: dict, adjustment: dict) -> bool:
    wanted = _json_list(adjustment.get("periods_json"))
    return not wanted or bool(set(wanted) & set(occurrence.get("periods") or []))


def _rule_occurrence(rule: dict, day: date, week_no: Optional[int], status: str = "scheduled") -> dict:
    periods = _json_list(rule.get("periods_json"))
    return {
        "id": f"rule-{rule['id']}-{day.isoformat()}", "rule_id": rule["id"],
        "course_id": rule["course_id"], "course_name": rule.get("course_name"),
        "course_code": rule.get("course_code"), "date": day.isoformat(),
        "weekday": day.isoweekday(), "week_number": week_no, "periods": periods,
        "start_time": rule.get("start_time"), "end_time": rule.get("end_time"),
        "location": rule.get("location"), "teacher": rule.get("teacher"),
        "note": rule.get("note"), "week_parity": rule.get("week_parity"), "status": status,
    }


def effective_schedule(start: Optional[str] = None, days: int = 7) -> dict:
    days = max(1, min(int(days), 31))
    start_day = date.fromisoformat(_iso_date(start, "start")) if start else date.today()
    end_day = start_day + timedelta(days=days - 1)
    term = query_one(
        "SELECT * FROM academic_terms WHERE active=1 ORDER BY start_date DESC, id DESC LIMIT 1"
    )
    rules = query(
        "SELECT r.*, c.name AS course_name, c.code AS course_code FROM course_schedule_rules r "
        "JOIN courses c ON c.id=r.course_id WHERE r.enabled=1 ORDER BY r.id"
    )
    adjustments = query(
        "SELECT a.*, c.name AS course_name, c.code AS course_code FROM calendar_adjustments a "
        "LEFT JOIN courses c ON c.id=a.course_id "
        "WHERE a.date BETWEEN ? AND ? AND a.adjustment_type IN ('holiday','day_off','workday') "
        "ORDER BY a.id",
        (start_day.isoformat(), end_day.isoformat()),
    )
    legacy_events = query(
        "SELECT id, event_type, title, date, detail FROM academic_calendar "
        "WHERE date BETWEEN ? AND ? ORDER BY date, id",
        (start_day.isoformat(), end_day.isoformat()),
    )
    result_days: list[dict] = []
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        day_text = day.isoformat()
        week_no = _week_number(day, term.get("first_week_monday") if term else None)
        originals = [a for a in adjustments if a.get("date") == day_text]
        adjustment_titles = {str(a.get("title") or "") for a in originals}
        day_legacy_events = [
            event for event in legacy_events
            if event.get("date") == day_text and str(event.get("title") or "") not in adjustment_titles
        ]
        adjustment_full_day_off = next((
            a for a in originals
            if a["adjustment_type"] == "holiday"
            or (a["adjustment_type"] == "day_off" and not _json_list(a.get("periods_json")))
        ), None)
        legacy_full_day_off = next((
            event for event in day_legacy_events
            if event.get("event_type") == "holiday" or (
                any(marker in f"{event.get('title') or ''}{event.get('detail') or ''}"
                    for marker in ("全天停课", "全校停课"))
                and "晚上不停课" not in f"{event.get('title') or ''}{event.get('detail') or ''}"
            )
        ), None)
        full_day_off = adjustment_full_day_off or legacy_full_day_off
        partial_days_off = [
            a for a in originals
            if a["adjustment_type"] == "day_off" and _json_list(a.get("periods_json"))
        ]
        workday = next((a for a in originals if a["adjustment_type"] == "workday"), None)
        source_day = date.fromisoformat(workday["source_date"]) if workday else day
        source_week_no = _week_number(source_day, term.get("first_week_monday") if term else None)
        effective_weekday = source_day.isoweekday()
        occurrences: list[dict] = []
        if not full_day_off:
            for rule in rules:
                if int(rule["weekday"]) != effective_weekday or not _rule_applies(rule, source_week_no):
                    continue
                occurrence = _rule_occurrence(
                    rule, day, source_week_no, "workday" if workday else "scheduled"
                )
                if workday:
                    occurrence["source_date"] = source_day.isoformat()
                if not any(_period_match(occurrence, off) for off in partial_days_off):
                    occurrences.append(occurrence)
        occurrences.sort(key=lambda item: (
            (item.get("periods") or [99])[0], item.get("start_time") or "99:99", item.get("course_name") or ""
        ))
        events = [
            {k: a.get(k) for k in ("id", "adjustment_type", "title", "detail", "source_date")}
            for a in originals
        ]
        events.extend({
            "id": f"legacy-{event['id']}",
            "adjustment_type": "holiday" if event.get("event_type") == "holiday" else "day_off",
            "title": event.get("title"), "detail": event.get("detail"), "source_date": None,
        } for event in day_legacy_events)
        result_days.append({
            "date": day_text, "weekday": day.isoweekday(), "week_number": week_no,
            "is_today": day == date.today(), "is_day_off": bool(full_day_off),
            "effective_weekday": effective_weekday, "events": events, "courses": occurrences,
        })
    return {"start": start_day.isoformat(), "end": end_day.isoformat(), "term": term, "days": result_days}
