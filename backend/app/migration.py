"""
旧数据迁移：从 v2 项目迁移 courses.json, calendar.json, error-bank.json,
knowledge-graph.json 到 v5 SQLite。

同时包含 V5.2 引入的 schema 演进(run_migrations):
- 唯一索引去重(课程、章节、文件 hash)
- ON DELETE CASCADE 重建外键表(若数据库是新建或用户确认重建)
"""
import json
import logging
from pathlib import Path
from typing import Optional

from .database import execute, query, query_one, init_db, insert

logger = logging.getLogger(__name__)


def migrate_from_path(base_path: str) -> dict:
    """
    迁移指定目录下的旧项目数据。
    base_path: 旧项目根目录（含 data/courses/courses.json 等）
    Returns: 统计 dict
    """
    base = Path(base_path)
    stats = {"courses": 0, "chapters": 0, "lessons": 0, "errors": 0, "graph_nodes": 0, "graph_edges": 0}

    # courses
    courses_file = base / "data" / "courses" / "courses.json"
    if courses_file.exists():
        try:
            data = json.loads(courses_file.read_text(encoding="utf-8"))
            items = data if isinstance(data, list) else data.get("courses", [])
            for item in items:
                if isinstance(item, dict):
                    course_name = item.get("name") or item.get("title") or ""
                    if not course_name:
                        continue
                    insert(
                        "INSERT INTO courses (name, code, semester, teacher, schedule_json) VALUES (?,?,?,?,?)",
                        (course_name, item.get("code", ""), item.get("semester", ""),
                         item.get("teacher", ""), json.dumps(item.get("schedule", {}), ensure_ascii=False)),
                    )
                    stats["courses"] += 1
        except Exception as e:
            print(f"courses.json 迁移失败: {e}")

    # calendar
    cal_file = base / "data" / "courses" / "calendar.json"
    if cal_file.exists():
        try:
            data = json.loads(cal_file.read_text(encoding="utf-8"))
            events = data if isinstance(data, list) else data.get("events", [])
            for ev in events:
                if isinstance(ev, list):
                    continue
                insert(
                    "INSERT INTO academic_calendar (course_id, event_type, title, date, detail) VALUES (?,?,?,?,?)",
                    (ev.get("course_id"), ev.get("type", ""), ev.get("title", ""),
                     ev.get("date", ""), json.dumps(ev, ensure_ascii=False)),
                )
        except Exception as e:
            print(f"calendar.json 迁移失败: {e}")

    # error bank
    err_file = base / "data" / "error-bank.json"
    if err_file.exists():
        try:
            data = json.loads(err_file.read_text(encoding="utf-8"))
            items = data if isinstance(data, list) else data.get("errors", [])
            for item in items:
                if isinstance(item, list):
                    continue
                insert(
                    "INSERT INTO errors (course_id, chapter_id, lesson_id, question_text, image_file, "
                    "student_answer, correct_answer, user_explanation, status) VALUES (?,?,?,?,?,?,?,?, 'confirmed')",
                    (item.get("course_id"), item.get("chapter_id"), item.get("lesson_id"),
                     item.get("question_text", ""), item.get("image_file", ""),
                     item.get("student_answer", ""), item.get("correct_answer", ""), item.get("user_explanation")),
                )
                stats["errors"] += 1
        except Exception as e:
            print(f"error-bank.json 迁移失败: {e}")

    # knowledge graph
    kg_file = base / "data" / "knowledge-graph.json"
    if kg_file.exists():
        try:
            data = json.loads(kg_file.read_text(encoding="utf-8"))
            nodes = data.get("nodes", []) if isinstance(data, dict) else []
            edges = data.get("edges", []) if isinstance(data, dict) else []
            node_map = {}
            for node in nodes:
                if isinstance(node, list):
                    continue
                nid = insert(
                    "INSERT INTO graph_nodes (course_id, title, node_type, meta_json) VALUES (?,?,?,?)",
                    (node.get("course_id"), node.get("title", ""), node.get("node_type", "topic"),
                     json.dumps(node, ensure_ascii=False)),

)
                node_map[node.get("id")] = nid
                stats["graph_nodes"] += 1
            for edge in edges:
                src = node_map.get(edge.get("source"))
                tgt = node_map.get(edge.get("target"))
                if src and tgt:
                    insert(
                        "INSERT INTO graph_edges (source_id, target_id, relation) VALUES (?,?,?)",
                        (src, tgt, edge.get("relation", "")),
                    )
                    stats["graph_edges"] += 1
        except Exception as e:
            print(f"knowledge-graph.json 迁移失败: {e}")

    # archive markdown files
    archive_dir = base / "data" / "archive"
    if archive_dir.exists():
        for md_file in sorted(archive_dir.rglob("*.md")):
            try:
                content = md_file.read_text(encoding="utf-8", errors="ignore")
                insert(
                    "INSERT INTO source_chunks (type, locator, text) VALUES ('archive_md', ?, ?)",
                    (str(md_file.relative_to(base)), content[:5000]),
                )
            except Exception as e:
                print(f"archive 迁移失败 {md_file}: {e}")

    return stats


def migrate_from_legacy() -> dict:
    """从当前目录的旧项目自动迁移"""
    import os
    candidates = ["..", "../自动化学习框架", "../data"]
    for c in candidates:
        p = Path(os.path.abspath(os.path.join(os.getcwd(), c)))
        if (p / "data" / "courses" / "courses.json").exists():
            print(f"发现旧项目: {p}")
            return migrate_from_path(str(p))
    print("未找到旧项目数据")
    return {}


# ============== V5.2 Schema 演进 ==============

def run_migrations() -> dict:
    """
    V5.2 引入的数据库演进:创建唯一索引、清理重复数据。
    幂等:可重复运行。
    返回迁移报告 dict。
    """
    init_db()  # 确保基础表存在
    report = {
        "courses_unique": False,
        "materials_hash_unique": False,
        "chapters_no_unique": False,
        "cleaned_duplicates": {},
    }

    # 1) 课程去重:按 (name, semester) 保留最早 id
    dups = query(
        "SELECT name, COALESCE(semester,'') AS sem, COUNT(*) AS c "
        "FROM courses GROUP BY name, COALESCE(semester,'') HAVING c > 1"
    )
    if dups:
        for row in dups:
            keep_id = query_one(
                "SELECT MIN(id) AS id FROM courses "
                "WHERE name=? AND COALESCE(semester,'')=?",
                (row["name"], row["sem"]),
            )
            keep_id = keep_id["id"] if keep_id else None
            if keep_id:
                deleted = execute(
                    "DELETE FROM courses WHERE name=? AND COALESCE(semester,'')=? AND id<>?",
                    (row["name"], row["sem"], keep_id),
                )
                report["cleaned_duplicates"].setdefault("courses", 0)
                report["cleaned_duplicates"]["courses"] += deleted if isinstance(deleted, int) else 0

    # 2) 创建唯一索引
    try:
        execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_courses_name_semester "
                "ON courses(name, COALESCE(semester, ''))")
        report["courses_unique"] = True
    except Exception as e:
        logger.warning("courses 唯一索引创建失败: %s", e)

    try:
        execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_materials_hash "
                "ON materials(file_hash) WHERE file_hash IS NOT NULL AND file_hash != ''")
        report["materials_hash_unique"] = True
    except Exception as e:
        logger.warning("materials 唯一索引创建失败: %s", e)

    try:
        execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_chapters_course_no "
                "ON chapters(course_id, chapter_no) WHERE chapter_no IS NOT NULL")
        report["chapters_no_unique"] = True
    except Exception as e:
        logger.warning("chapters 唯一索引创建失败: %s", e)

    return report

