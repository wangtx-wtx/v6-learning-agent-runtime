"""
Obsidian 同步 MVP：
- 建立 vault 目录结构
- 笔记 YAML frontmatter
- 同步 job 记录
"""
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Optional

from .config import OBSIDIAN_VAULT_ROOT
from .database import execute, query

VAULT_STRUCTURE = [
    "00 Inbox",
    "01 Courses",
    "02 Homework",
    "03 Errors",
    "04 Reviews",
    "05 Exams",
]


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


def _course_dir(course: str) -> Path:
    root = ensure_default_vault_structure()
    safe = course.replace("/", "_").replace("\\", "_")
    return root / "01 Courses" / safe


def _chapter_dir(course_dir: Path, chapter_title: str) -> Path:
    safe = chapter_title.replace("/", "_").replace("\\", "_")
    return course_dir / safe


def _lesson_note_path(course: str, chapter: str, lesson_label: str, title: str) -> Path:
    safe_title = title.replace("/", "_").replace("\\", "_").replace(":", "_")
    return _chapter_dir(_course_dir(course), chapter) / f"{lesson_label} {safe_title}.md"


def write_note_vault(course: str, chapter: str, lesson_label: str, title: str,
                     markdown: str, meta: dict) -> str:
    """写笔记到 Obsidian vault，返回相对 vault 的路径"""
    root = ensure_default_vault_structure()
    path = _lesson_note_path(course, chapter, lesson_label, title)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    rel = path.relative_to(root)
    # 记录 sync job
    h = _hash(markdown)
    execute(
        "INSERT INTO sync_jobs (target, asset_id, asset_path, content_hash, status, retries, synced_at) "
        "VALUES ('obsidian', 0, ?, ?, 'synced', 0, datetime('now','localtime'))",
        (str(rel), h),
    )
    return str(rel)


def list_sync_status():
    return query("SELECT * FROM sync_jobs ORDER BY id DESC LIMIT 100")

def _ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p

def ensure_default_vault_structure() -> Path:
    return ensure_vault_structure()
