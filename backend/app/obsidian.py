"""
Obsidian 同步（V5.5 重写，方案 9.1/9.2）：
- 真实课程/章节/课时路径查询（不再硬编码「默认课程/未分类」）；
- Windows 非法文件名清洗（<>:"/\\|?* 与控制字符、结尾点/空格）；
- `[note-42]` 稳定 ID 命名：重命名标题不会破坏双链与同步幂等；
- 同步 job 记录。
"""
import hashlib
import re
from pathlib import Path
from typing import Optional

from .config import OBSIDIAN_VAULT_ROOT
from .database import query, fetch_one, insert

VAULT_STRUCTURE = [
    "00 Inbox",
    "01 Courses",
    "02 Homework",
    "03 Errors",
    "04 Reviews",
    "05 Exams",
]

# Windows 非法字符 + 保留名
_WINDOWS_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL",
                     *(f"COM{i}" for i in range(1, 10)),
                     *(f"LPT{i}" for i in range(1, 10))}


def sanitize_windows_name(name: str, fallback: str = "未命名") -> str:
    """清洗 Windows 非法文件名片段：替换非法字符、保留名、结尾点/空格。"""
    cleaned = _WINDOWS_UNSAFE.sub("_", str(name or "")).strip()
    stem = cleaned.split(".")[0].upper()
    if stem in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    cleaned = cleaned.rstrip(". ")
    return cleaned or fallback


def ensure_vault_structure(vault_root: Optional[Path] = None) -> Path:
    root = Path(vault_root or OBSIDIAN_VAULT_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    for folder in VAULT_STRUCTURE:
        (root / folder).mkdir(parents=True, exist_ok=True)
    return root


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _yaml_quote(value: str) -> str:
    if any(c in value for c in "[]{}:#-,"):
        return f'"{value}"'
    return value


def build_note_markdown(title: str, body: str, meta: dict) -> str:
    """生成带 YAML frontmatter 的 Obsidian Markdown"""
    lines = ["---"]
    if meta.get("course"):
        lines.append(f"course: {_yaml_quote(meta['course'])}")
    if meta.get("chapter"):
        lines.append(f"chapter: {_yaml_quote(meta['chapter'])}")
    if meta.get("lesson"):
        lines.append(f"lesson: {_yaml_quote(meta['lesson'])}")
    if meta.get("type"):
        lines.append(f"type: {meta['type']}")
    if meta.get("date"):
        lines.append(f"date: {meta['date']}")
    tags = meta.get("tags") or []
    if tags:
        lines.append("tags:")
        for t in tags:
            lines.append(f"  - {t}")
    status = meta.get("status", "draft")
    lines.append(f"status: {status}")
    for key, val in meta.items():
        if key not in {"course", "chapter", "lesson", "type", "date", "tags", "status"}:
            if isinstance(val, str):
                lines.append(f"{key}: {_yaml_quote(val)}")
            else:
                lines.append(f"{key}: {val}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


def resolve_note_paths(course_id=None, chapter_id=None, lesson_id=None) -> dict:
    """按真实数据查询课程/章节/课时标签（方案 9.1：路径必须来自数据库）。
    只给 lesson_id 时沿 lesson→chapter→course 级联补齐。"""
    if lesson_id and not (chapter_id and course_id):
        row = fetch_one(
            "SELECT l.chapter_id, l.course_id, c.chapter_no, c.title AS chapter_title, "
            "       l.lesson_no, l.title AS lesson_title "
            "FROM lessons l LEFT JOIN chapters c ON c.id=l.chapter_id WHERE l.id=?",
            (lesson_id,),
        )
        if row:
            chapter_id = chapter_id or row["chapter_id"]
            course_id = course_id or row["course_id"]
            lesson_label = f"L{row['lesson_no'] or ''} {row['lesson_title'] or ''}".strip()
            chapter_label = (f"第{row['chapter_no']}章 {row['chapter_title'] or ''}".strip()
                             if chapter_id else "未分类章节")
            course_label = "未分类课程"
            if course_id:
                c = fetch_one("SELECT name FROM courses WHERE id=?", (course_id,))
                course_label = c["name"] if c else course_label
            return {"course": course_label, "chapter": chapter_label,
                    "lesson": lesson_label or ""}

    course_label, chapter_label, lesson_label = "未分类课程", "未分类章节", ""
    if course_id:
        row = fetch_one("SELECT name FROM courses WHERE id=?", (course_id,))
        if row:
            course_label = row["name"]
    if chapter_id:
        row = fetch_one("SELECT chapter_no, title FROM chapters WHERE id=?", (chapter_id,))
        if row:
            chapter_label = f"第{row['chapter_no']}章 {row['title'] or ''}".strip()
    if lesson_id:
        row = fetch_one("SELECT lesson_no, title FROM lessons WHERE id=?", (lesson_id,))
        if row:
            lesson_label = f"L{row['lesson_no'] or ''} {row['title'] or ''}".strip()
    return {"course": course_label, "chapter": chapter_label, "lesson": lesson_label}


def note_vault_path(course: str, chapter: str, stable_id: str, title: str) -> Path:
    """`01 Courses/<课程>/<章节>/[note-42] 标题.md`（稳定 ID 命名）。"""
    safe_course = sanitize_windows_name(course)
    safe_chapter = sanitize_windows_name(chapter)
    safe_title = sanitize_windows_name(title)
    root = ensure_vault_structure()
    return root / "01 Courses" / safe_course / safe_chapter / f"[{stable_id}] {safe_title}.md"


def write_note_vault(course: str, chapter: str, lesson_label: str, title: str,
                     markdown: str, meta: dict, stable_id: str = "") -> str:
    """写笔记到 Obsidian vault，返回相对 vault 的路径。

    stable_id 形如 note-42：以数据库 ID 命名文件，标题改动不影响同步幂等。
    """
    root = ensure_vault_structure()
    sid = stable_id or _hash(markdown)[:12]
    course = meta.get("course") or course
    chapter = meta.get("chapter") or chapter
    path = note_vault_path(course, chapter, sid, title)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    rel = path.relative_to(root)
    h = _hash(markdown)
    insert(
        "INSERT INTO sync_jobs (target, asset_id, asset_path, content_hash, status, retries, synced_at) "
        "VALUES ('obsidian', 0, ?, ?, 'synced', 0, datetime('now','localtime'))",
        (str(rel), h),
    )
    return str(rel)


def list_sync_status():
    return query("SELECT * FROM sync_jobs ORDER BY id DESC LIMIT 100")


def ensure_default_vault_structure() -> Path:
    return ensure_vault_structure()
