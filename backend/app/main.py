"""
FastAPI 主应用 + API 路由。
"""
import asyncio
import httpx
import json
import logging
import os
import socket
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Any, Optional
import re
import secrets
import string
from urllib.parse import quote

from . import config
from .config import OBSIDIAN_VAULT_ROOT, FRONTEND_PORT
from .database import init_db, query, query_one, execute, fetch_one, insert, transaction
from .lifecycle import lifespan
from .gateway import gateway
from .dag import DAGContext, is_terminal_run_status
from .dag_lesson import build_lesson_dag
from .dag_homework import build_homework_dag
from .dag_error import build_error_dag
from .dag_review import build_review_dag
from .routing import load_usage_from_gateway, make_route_for_workflow
from .obsidian import ensure_vault_structure, list_sync_status
from .tailscale import get_tailscale_info
from .env_file import upsert_env_key
from .schedule_calendar import (
    bootstrap_schedule_data,
    effective_schedule,
    list_adjustments,
    list_rules,
    list_terms,
    save_adjustment,
    save_rule,
    save_term,
)
from .document_artifacts import artifact_public, render_artifact, safe_artifact_file

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


# ---------- 上传安全常量 ----------
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB；上传过程流式落盘，不整文件驻留内存
MAX_INLINE_TRANSCRIPT_CHARS = 2_000_000
TRANSCRIPT_WARNING_CHARS = 200_000
ALLOWED_EXTS = {
    ".pdf", ".ppt", ".pptx", ".doc", ".docx",
    ".md", ".txt", ".srt", ".vtt", ".json", ".png", ".jpg", ".jpeg", ".webp",
    ".gif", ".mp3", ".m4a", ".wav",
}
# 简易 magic bytes 校验表:type → 前 4 字节 (hex)
_MAGIC_BYTES: dict[str, bytes] = {
    "pdf":  b"%PDF",
    "png":  b"\x89PNG",
    "jpg":  b"\xff\xd8\xff",
    "gif":  b"GIF8",
    "zip":  b"PK\x03\x04",  # .pptx / .docx / .xlsx 都是 zip 容器
    "ogg":  b"OggS",
}


def _safe_filename(name: str) -> str:
    """去除路径分隔符与控制字符,只保留文件名本体。"""
    base = name.replace("\\", "/").split("/")[-1]
    return "".join(c for c in base if c.isprintable() and c not in '\x00\r\n')


# ---------- V6 Learning Engine Phase 1 运行标识 ----------
def _v6_engine_info() -> dict:
    """当前引擎模式。V5 / V6 运行必须可区分（设计文档 §14）。"""
    from .learning_engine import config as v6cfg
    return {"mode": v6cfg.engine_mode(), "engine_version": v6cfg.engine_version(),
            "enabled": v6cfg.engine_enabled(),
            # 出版层语义必须如实展示：shadow 不接管，on 使用 Composer V2。
            "publication_mode": v6cfg.publication_mode(),
            "real_notes_publish_enabled": v6cfg.real_notes_publish_enabled()}


def _check_magic(data_head: bytes, ext: str) -> bool:
    """根据扩展名核对文件头几个字节。文本类直接放行。"""
    ext = ext.lower().lstrip(".")
    if ext in ("md", "txt", "srt", "vtt", "json"):
        return True  # 纯文本不做 magic 校验
    expected = _MAGIC_BYTES.get(ext)
    if not expected:
        return True  # 未登记的扩展名(已被 ALLOWED_EXTS 控制)
    return data_head.startswith(expected)

app = FastAPI(
    title="V6.0 学习 Agent Runtime",
    description="以课程章节为核心、以证据链为约束的本地学习 Agent Runtime。",
    version="6.0.0",
    lifespan=lifespan,
)

# 集中版本号常量；schema 版本仍从 migrations 动态读取，避免双版本源漂移。
PRODUCT_VERSION = "6.0.0"


def _detect_schema_version() -> int:
    """从 backend/migrations 目录动态读最大版本号。

    单文件实现，懒加载（lifespan 启动后第一次调用时计算）。
    """
    try:
        from pathlib import Path
        from .config import BASE_DIR
        mig_dir = BASE_DIR / "migrations"
        if not mig_dir.exists():
            return 0
        max_v = 0
        for p in mig_dir.glob("*.sql"):
            try:
                v = int(p.stem.split("_", 1)[0])
                if v > max_v:
                    max_v = v
            except (ValueError, OSError):
                continue
        return max_v
    except Exception:
        return 0


SCHEMA_VERSION = _detect_schema_version()

# ---------- CORS:收紧到明确白名单(避免任意 Origin 携带 Token) ----------
ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8800",
    "http://127.0.0.1:8800",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["Content-Type", "x-app-token", "Accept"],
)


# ---------- 移动端 Token 鉴权(Funnel / 远程模式) ----------
# 默认白名单策略:只有显式列出的路径允许匿名访问;其余所有 /api/* 路径
# 在启用 V5_MOBILE_TOKEN 时强制校验 token。本机 127.0.0.1 / ::1 永免校验。

_PUBLIC_PATHS = {
    "/api/mobile/server-info",
    "/api/health",
}


@app.middleware("http")
async def mobile_token_check(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/"):
        # P0 安全:远程(Funnel/广域网)情况下关闭调试文档,避免暴露 API 全貌
        if path in ("/docs", "/redoc", "/openapi.json", "/api/openapi.json") and config.MOBILE_TOKEN:
            client_host = request.client.host if request.client else ""
            if client_host not in ("127.0.0.1", "::1"):
                return JSONResponse(status_code=403, content={"detail": "调试文档仅在本地开放"})
        return await call_next(request)

    # admin token 管理路径有独立 localhost 校验
    if path.startswith("/api/admin/mobile-token"):
        return await call_next(request)

    # server-info / health 始终匿名
    if path in _PUBLIC_PATHS:
        return await call_next(request)

    # 本机请求永免校验
    client_host = request.client.host if request.client else ""
    is_local = client_host in ("127.0.0.1", "::1")

    if config.MOBILE_TOKEN and not is_local:
        provided = (
            request.headers.get("x-app-token", "")
            or request.headers.get("X-App-Token", "")
            or request.query_params.get("token", "")
        )
        if provided != config.MOBILE_TOKEN:
            return JSONResponse(
                status_code=403,
                content={"detail": "凭证无效,请在 URL 后添加 ?token=xxx 或设置 x-app-token header"},
            )
    return await call_next(request)


# 启动/关闭全部交给 lifespan（方案 3.1）：见 app/lifecycle.py。
# V5.5：schema 版本校验在 init_db() 内完成；遗留库（无 schema_migrations）会抛
# MigrationRequiredError 拒绝启动，必须先运行 `python -m tools.migrate_database`。


class CourseCreate(BaseModel):
    name: str
    code: Optional[str] = None
    semester: Optional[str] = None
    teacher: Optional[str] = None
    schedule: Optional[Any] = None


class ChapterCreate(BaseModel):
    course_id: int
    chapter_no: Optional[int] = None
    title: str
    syllabus_ref: Optional[str] = None
    notes: Optional[str] = None


class ChapterUpdate(BaseModel):
    chapter_no: Optional[int] = None
    title: str
    notes: Optional[str] = None
    status: str = "not_started"


class LessonCreate(BaseModel):
    chapter_id: Optional[int] = None
    course_id: int
    lesson_no: Optional[str] = None
    title: Optional[str] = None
    date: Optional[str] = None


class LessonRunRequest(BaseModel):
    course_id: Optional[int] = None
    chapter_id: Optional[int] = None
    lesson_id: Optional[int] = None
    course: Optional[str] = None
    chapter: Optional[str] = None
    lesson_no: Optional[str] = None
    date: Optional[str] = None
    transcript: Optional[str] = None
    materials: Optional[list] = None
    material_ids: Optional[list] = None  # P0: 移动端用它把真实上传文件带入工作流
    images: Optional[list] = None
    material_summary: Optional[str] = None


class HomeworkRunRequest(BaseModel):
    course_id: Optional[int] = None
    lesson_id: Optional[int] = None
    chapter_id: Optional[int] = None
    homework_id: Optional[int] = None
    homework_text: Optional[str] = None
    images: Optional[list] = None
    material_ids: Optional[list] = None  # P0: 移动端上传图片后回传的已入库材料 id


class ErrorRunRequest(BaseModel):
    course_id: Optional[int] = None
    lesson_id: Optional[int] = None
    chapter_id: Optional[int] = None
    question_text: Optional[str] = None
    student_answer: Optional[str] = None
    correct_answer: Optional[str] = None
    images: Optional[list] = None


class ReviewRunRequest(BaseModel):
    course_id: Optional[int] = None
    chapter_id: Optional[int] = None
    kind: str = "chapter"
    scope: Optional[dict] = None
    exam_date: Optional[str] = None

class AcademicCalendarCreate(BaseModel):
    course_id: Optional[int] = None
    event_type: str
    title: str
    date: Optional[str] = None
    detail: Optional[str] = None


class AcademicTermPayload(BaseModel):
    name: str
    school_year: Optional[str] = None
    semester: Optional[str] = None
    start_date: str
    end_date: str
    first_week_monday: str
    teaching_weeks: int = 16
    exam_start: Optional[str] = None
    exam_end: Optional[str] = None
    source: Optional[str] = "manual"
    active: bool = True


class ScheduleRulePayload(BaseModel):
    course_id: int
    weekday: int
    start_week: int = 1
    end_week: int = 16
    week_parity: str = "all"
    periods: list[int] = Field(default_factory=list)
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    location: Optional[str] = None
    teacher: Optional[str] = None
    note: Optional[str] = None
    source: Optional[str] = "manual"
    enabled: bool = True


class CalendarAdjustmentPayload(BaseModel):
    adjustment_type: str
    date: str
    source_date: Optional[str] = None
    periods: list[int] = Field(default_factory=list)
    title: Optional[str] = None
    detail: Optional[str] = None
    source: Optional[str] = "manual"



# ---------- 工具 ----------

def _extract_quota_pct(usage: dict) -> float:
    try:
        if not isinstance(usage, dict):
            return 0.0
        data = usage.get("data") if isinstance(usage.get("data"), dict) else usage
        used = data.get("used_5h", 0) or 0
        limit = data.get("limit_5h", 1200) or 1200
        return min(100.0, float(used) / max(float(limit), 1.0) * 100)
    except Exception:
        return 0.0


async def enqueue_workflow(workflow_name: str, mode: str, input_data: dict, response_body: Optional[dict] = None) -> dict:
    """创建 run(queued) 并交给后台 Worker 执行，接口立即返回 202。"""
    from .dag import DAGContext
    from .workers import enqueue_workflow as q_workflow
    ctx = DAGContext(input_data)
    course_id = ctx.input.get("course_id")
    lesson_id = ctx.input.get("lesson_id")
    chapter_id = ctx.input.get("chapter_id")
    run_id = ctx.create_run(workflow_name, mode, course_id=course_id, lesson_id=lesson_id, chapter_id=chapter_id)
    q_workflow(run_id)
    body = dict(response_body or {})
    body.update({"run_id": run_id, "status": "queued"})
    return body


def _sha256_hex(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


# ---------- 健康 ----------

@app.get("/api/health")
async def health():
    """公开健康端点 (V5.5.1 收尾 C.2/C.6)。

    仅返回非敏感信息：产品版本、API 版本、UTC 时间。
    详细诊断路 /api/admin/diagnostics，且**无条件**仅本机可访问。
    """
    from .time_utils import now_utc_iso
    degraded_reasons: list[str] = []
    try:
        from .database import fetch_one
        fetch_one("SELECT 1")
    except Exception:
        degraded_reasons.append("database_unavailable")
    try:
        from .workers import worker_manager
        if not (worker_manager._supervisor and not worker_manager._supervisor.done()):
            degraded_reasons.append("worker_supervisor_dead")
    except Exception:
        degraded_reasons.append("worker_check_failed")
    # V5.6.2: FTS 简化状态（ready/unavailable/failed/uninitialized），不泄露底层异常
    try:
        from .rag import get_fts_status
        info_fts = get_fts_status()
        fts_flag = info_fts.get("status", "uninitialized")
        if fts_flag == "failed":
            degraded_reasons.append("fts_failed")
    except Exception:
        fts_flag = "unavailable"

    # V5.6.3: tokenizer 简化状态（degraded 含 unavailable/failed；不泄露异常）
    try:
        from . import tokenizer as _tok
        _tok_status = _tok.get_tokenizer_status()["status"]
        tokenizer_flag = "ready" if _tok_status == "ready" else "degraded"
        if _tok_status == "failed":
            degraded_reasons.append("tokenizer_failed")   # 初始化异常 → 503
        # unavailable（jieba 缺失）是预期降级：显示 degraded，但不 503
    except Exception:
        tokenizer_flag = "degraded"

    info = {
        "status": "degraded" if degraded_reasons else "ok",
        "version": PRODUCT_VERSION,
        "api_version": "v1",
        "now_utc": now_utc_iso(),
        "fts": fts_flag,
        "tokenizer": tokenizer_flag,
    }
    # V5.6.4: 网关模式 + 公开 model_gateway 别名（前端徽标读这里；不泄露路径/Key）
    try:
        from .gateway_mode import resolve_mode
        gw_mode = resolve_mode()
        info["gateway_mode"] = gw_mode
        info["model_gateway"] = {
            "fake": "simulated",
            "replay": "replay",
            "live": "live",
        }.get(gw_mode, "unavailable")
    except Exception:
        info["gateway_mode"] = "unavailable"
        info["model_gateway"] = "unavailable"
    if degraded_reasons:
        info["degraded_reasons"] = degraded_reasons
        return JSONResponse(status_code=503, content=info)
    return info


@app.get("/api/capabilities/limits")
async def capability_limits():
    """前端预检使用的稳定、非敏感限制，避免传完整文件后才收到 413。"""
    return {
        "max_upload_bytes": MAX_FILE_SIZE,
        "max_inline_transcript_chars": MAX_INLINE_TRANSCRIPT_CHARS,
        "transcript_warning_chars": TRANSCRIPT_WARNING_CHARS,
        "allowed_extensions": sorted(ALLOWED_EXTS),
        "large_input_strategy": "stream_upload_then_segment",
    }


@app.get("/api/admin/diagnostics")
async def admin_diagnostics(request: Request):
    """受保护诊断端点 (V5.5.1 收尾 C.2)。**无条件**调用 _require_local_admin。"""
    _require_local_admin(request)
    import os
    from .database import schema_version, _active_db_path, is_override
    from .time_utils import now_utc_iso
    info: dict[str, Any] = {
        "status": "ok",
        "version": PRODUCT_VERSION,
        "api_version": "v1",
        "now_utc": now_utc_iso(),
        "pid": os.getpid(),
        "instance": {},
        "database": {},
        "schema": {},
        "worker": {},
        "gateway": {},
    }
    try:
        info["database"]["path"] = str(_active_db_path())
    except Exception:
        info["database"]["error"] = "read_failed"
    try:
        sv = schema_version()
        info["schema"]["schema_migrations_max"] = sv
        from .database import fetch_one
        uv = fetch_one("PRAGMA user_version")
        info["schema"]["user_version"] = (
            (uv or {}).get("user_version", 0) if isinstance(uv, dict)
            else int(uv[0] if uv else 0)
        )
        info["schema"]["consistent"] = (info["schema"]["user_version"] == sv)
    except Exception:
        info["schema"]["error"] = "read_failed"
    try:
        from .instance_lock import read_instance_lock
        info["instance"]["lock"] = read_instance_lock()
    except Exception:
        info["instance"]["error"] = "read_failed"
    try:
        from .workers import worker_manager
        info["worker"]["running_tasks"] = len(worker_manager.running_tasks)
        info["worker"]["running_parses"] = len(worker_manager.running_parses)
        info["worker"]["supervisor_alive"] = bool(
            worker_manager._supervisor and not worker_manager._supervisor.done()
        )
    except Exception:
        info["worker"]["error"] = "read_failed"
    # V5.6.2: 完整 FTS 诊断（本机 diagnostics；不隐含底层异常文本，只给错误码）
    try:
        from .rag import get_fts_status
        info["fts"] = get_fts_status()
    except Exception:
        info["fts"] = {"status": "unavailable", "error": "read_failed"}
    # V5.6.3: 完整 tokenizer 诊断（本机 diagnostics）
    try:
        from . import tokenizer as _tok
        info["tokenizer"] = _tok.get_tokenizer_status()
    except Exception:
        info["tokenizer"] = {"status": "unavailable", "error": "read_failed"}
    # V5.6.4: 网关模式 + 计数（仅本机；不暴露路径/Key）
    try:
        from .gateway_mode import snapshot as _gw_snapshot
        info["gateway"] = _gw_snapshot()
    except Exception as e:
        info["gateway"] = {"error": f"snapshot_failed: {type(e).__name__}"}
    degraded = (
        not info["schema"].get("consistent", True)
        or not info["worker"].get("supervisor_alive", True)
        or bool(info["database"].get("error"))
        or info.get("fts", {}).get("status") == "failed"
        or info.get("tokenizer", {}).get("status") == "failed"
    )
    if degraded:
        info["status"] = "degraded"
        return JSONResponse(status_code=503, content=info)
    return info


# ---------- 课程 ----------

@app.post("/api/courses")
async def create_course(course: CourseCreate):
    rid = insert(
        "INSERT INTO courses (name, code, semester, teacher, schedule_json) VALUES (?,?,?,?,?)",
        (course.name, course.code, course.semester, course.teacher,
         json.dumps(course.schedule or {}, ensure_ascii=False)),

)
    return {"id": rid, "status": "created"}


@app.get("/api/courses")
async def list_courses():
    return query("SELECT * FROM courses ORDER BY id")


COURSE_DELETE_IMPACT_TABLES = (
    "chapters", "lessons", "materials", "source_chunks", "notes", "homeworks",
    "errors", "reviews", "workflow_runs", "graph_nodes", "academic_calendar",
    "course_schedule_rules",
)


def _course_delete_impact(course_id: int, conn=None) -> dict[str, int]:
    db = conn
    counts: dict[str, int] = {}
    for table in COURSE_DELETE_IMPACT_TABLES:
        if db is None:
            row = query_one(f"SELECT COUNT(*) AS n FROM {table} WHERE course_id=?", (course_id,))
        else:
            raw = db.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE course_id=?", (course_id,)).fetchone()
            row = dict(raw) if raw else None
        counts[table] = int((row or {}).get("n") or 0)
    return counts


@app.get("/api/courses/{course_id}/delete-impact")
async def course_delete_impact(course_id: int):
    course = query_one("SELECT id, name, code FROM courses WHERE id=?", (course_id,))
    if not course:
        raise HTTPException(404, "课程不存在")
    return {"course": course, "counts": _course_delete_impact(course_id)}


@app.delete("/api/courses/{course_id}")
async def delete_course(course_id: int, confirm_name: str):
    """删除课程及级联数据；必须精确输入课程名称，防止误删和绕过前端确认。"""
    with transaction() as conn:
        raw = conn.execute("SELECT id, name, code FROM courses WHERE id=?", (course_id,)).fetchone()
        course = dict(raw) if raw else None
        if not course:
            raise HTTPException(404, "课程不存在")
        if confirm_name != course["name"]:
            raise HTTPException(409, "课程名称不匹配，已拒绝删除")
        counts = _course_delete_impact(course_id, conn)
        conn.execute("DELETE FROM courses WHERE id=?", (course_id,))
    return {"id": course_id, "name": course["name"], "status": "deleted", "counts": counts}


@app.post("/api/chapters")
async def create_chapter(ch: ChapterCreate):
    title = ch.title.strip()
    if not title:
        raise HTTPException(422, "章节名称不能为空")
    if not query_one("SELECT id FROM courses WHERE id=?", (ch.course_id,)):
        raise HTTPException(404, "课程不存在")
    rid = insert(
        "INSERT INTO chapters (course_id, chapter_no, title, syllabus_ref, notes, status) VALUES (?,?,?,?,?,'not_started')",
        (ch.course_id, ch.chapter_no, title, ch.syllabus_ref, (ch.notes or "").strip() or None),

)
    return {"id": rid, "status": "created"}


@app.get("/api/chapters")
async def list_chapters(course_id: Optional[int] = None):
    if course_id:
        return query("SELECT * FROM chapters WHERE course_id=? ORDER BY chapter_no", (course_id,))
    return query("SELECT * FROM chapters ORDER BY course_id, chapter_no")


CHAPTER_STATUSES = {"not_started", "in_progress", "completed", "reviewed"}


@app.put("/api/chapters/{chapter_id}")
async def update_chapter(chapter_id: int, body: ChapterUpdate):
    current = query_one("SELECT id FROM chapters WHERE id=?", (chapter_id,))
    if not current:
        raise HTTPException(404, "章节不存在")
    title = body.title.strip()
    if not title:
        raise HTTPException(422, "章节名称不能为空")
    if body.status not in CHAPTER_STATUSES:
        raise HTTPException(422, "章节状态无效")
    notes = (body.notes or "").strip() or None
    execute(
        "UPDATE chapters SET chapter_no=?, title=?, notes=?, status=?, "
        "completed_at=CASE WHEN ? IN ('completed','reviewed') THEN COALESCE(completed_at,datetime('now','localtime')) ELSE NULL END, "
        "reviewed_at=CASE WHEN ?='reviewed' THEN COALESCE(reviewed_at,datetime('now','localtime')) ELSE NULL END, "
        "review_status=CASE WHEN ?='reviewed' THEN 'reviewed' ELSE review_status END WHERE id=?",
        (body.chapter_no, title, notes, body.status, body.status, body.status, body.status, chapter_id),
    )
    return query_one("SELECT * FROM chapters WHERE id=?", (chapter_id,))


@app.delete("/api/chapters/{chapter_id}")
async def delete_chapter(chapter_id: int, confirm_title: str):
    current = query_one("SELECT id, title FROM chapters WHERE id=?", (chapter_id,))
    if not current:
        raise HTTPException(404, "章节不存在")
    if confirm_title != current["title"]:
        raise HTTPException(409, "章节名称不匹配，已拒绝删除")
    execute("DELETE FROM chapters WHERE id=?", (chapter_id,))
    return {"id": chapter_id, "title": current["title"], "status": "deleted"}


@app.post("/api/lessons")
async def create_lesson(ls: LessonCreate):
    rid = insert(
        "INSERT INTO lessons (chapter_id, course_id, lesson_no, title, date) VALUES (?,?,?,?,?)",
        (ls.chapter_id, ls.course_id, ls.lesson_no, ls.title, ls.date),

)
    return {"id": rid, "status": "created"}


@app.get("/api/lessons")
async def list_lessons(chapter_id: Optional[int] = None, course_id: Optional[int] = None):
    if chapter_id:
        return query("SELECT * FROM lessons WHERE chapter_id=? ORDER BY id", (chapter_id,))
    if course_id:
        return query("SELECT * FROM lessons WHERE course_id=? ORDER BY id", (course_id,))
    return query("SELECT * FROM lessons ORDER BY id")


# ---------- 模型与路由 ----------

class ModelProfileUpsert(BaseModel):
    id: str
    gateway_model: str
    provider: str
    family: str = "other"
    capability: str = "text"
    enabled: bool = True
    context_window: Optional[int] = None
    embedding_dimensions: Optional[int] = None
    input_cost: Optional[float] = None
    output_cost: Optional[float] = None
    cost_unit: str = "unknown"
    notes: str = ""


class ModelEnabledUpdate(BaseModel):
    enabled: bool


class ModelRoleRouteUpdate(BaseModel):
    model_ids: list[str]

@app.get("/api/models")
async def list_models_api():
    from .models_registry import list_models
    return list_models()


@app.get("/api/models/gateway-services")
async def list_gateway_service_models_api():
    """Return native embedding/rerank/OCR metadata without exposing secrets."""
    base_url = config.GATEWAY_BASE_URL.rstrip("/")
    admin_url = base_url + "/admin/specialists.html"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(base_url + "/api/admin/service-models")
        if response.status_code != 200:
            return {
                "available": False, "models": [],
                "detail": f"网关返回 HTTP {response.status_code}",
                "admin_url": admin_url,
            }
        payload = response.json()
        return {
            "available": True, "models": payload.get("models", []),
            "detail": "", "admin_url": admin_url,
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "available": False, "models": [], "detail": str(exc),
            "admin_url": admin_url,
        }


@app.put("/api/models/{model_id}")
async def save_model_api(model_id: str, body: ModelProfileUpsert):
    from .models_registry import save_model
    if model_id != body.id:
        raise HTTPException(status_code=400, detail="路径 ID 与表单 ID 不一致")
    try:
        return save_model(body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.patch("/api/models/{model_id}/enabled")
async def set_model_enabled_api(model_id: str, body: ModelEnabledUpdate):
    from .models_registry import set_model_enabled
    try:
        set_model_enabled(model_id, body.enabled)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="模型不存在") from e
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"id": model_id, "enabled": body.enabled}


@app.get("/api/model-routes")
async def list_model_routes_api():
    from .models_registry import list_role_routes
    return list_role_routes()


@app.put("/api/model-routes/{role}")
async def save_model_route_api(role: str, body: ModelRoleRouteUpdate):
    from .models_registry import set_role_models
    try:
        return {"role": role, "model_ids": set_role_models(role, body.model_ids)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/usage")
async def get_usage():
    usage = await load_usage_from_gateway(gateway)
    return usage


@app.get("/api/routes/{workflow}")
async def get_routes(workflow: str):
    usage = await load_usage_from_gateway(gateway)
    pct = _extract_quota_pct(usage)
    routes = make_route_for_workflow(workflow, pct)
    return {"quota_pct_5h": pct, "routes": routes}


# ---------- 听课流 ----------

@app.post("/api/workflows/lesson", status_code=202)
async def run_lesson(req: LessonRunRequest):
    payload = req.model_dump(exclude_none=True)
    transcript = payload.get("transcript") or ""
    if len(transcript) > MAX_INLINE_TRANSCRIPT_CHARS:
        raise HTTPException(
            status_code=413,
            detail=(f"粘贴文本共 {len(transcript):,} 字，超过 {MAX_INLINE_TRANSCRIPT_CHARS:,} 字的单次请求上限。"
                    "请保存为 UTF-8 TXT/SRT/VTT 后从材料收件箱上传；文件会流式落盘并自动分段，内容不会被截断。"),
        )
    return await enqueue_workflow("lesson", "attend", payload)


@app.get("/api/workflows/lesson/{run_id}")
async def get_lesson_run(run_id: int, include_payloads: bool = False):
    run = query_one(
        "SELECT id, workflow, mode, course_id, lesson_id, chapter_id, status, error, "
        "created_at, updated_at, parent_run_id, output_json "
        "FROM workflow_runs WHERE id=? AND workflow='lesson'", (run_id,))
    if not run:
        raise HTTPException(404, "run 不存在")
    if not include_payloads:
        run.pop("output_json", None)
    nodes = query(
        "SELECT rn.id, rn.run_id, rn.node_name, rn.status, rn.agent_role, rn.model, rn.attempt, "
        "rn.input_ref, COALESCE(rn.output_ref, substr(rn.output_json,1,4000)) AS output_ref, "
        "rn.started_at, rn.finished_at, rn.tokens_in, rn.tokens_out, rn.latency_ms, rn.error, "
        "COALESCE(mp.gateway_model, rn.model) AS model_display "
        "FROM run_nodes rn LEFT JOIN model_profiles mp ON mp.id=rn.model "
        "WHERE rn.run_id=? ORDER BY rn.id",
        (run_id,),
    )
    domain = query_one("SELECT id, engine_version FROM material_domains WHERE run_id=?", (run_id,))
    return {"run": run, "nodes": nodes,
            "engine": _v6_engine_info(),
            "engine_version": (domain or {}).get("engine_version") or _v6_engine_info()["engine_version"],
            "domain_id": (domain or {}).get("id")}


# ---------- V6 Learning Engine Phase 1：Material Domain / Coverage / Source Span ----------
#
# 安全约束（任务书 §九）:
#   * 不返回文件系统路径（materials.file_path / 上传目录 / vault 路径）。
#   * 不返回任何 API Key / Token。
#   * SourceSpan 原文只允许读取属于该 run/domain 的有效来源；跨 run 越权 → 404。


def _public_domain_item(row: dict) -> dict:
    """domain item 的公开投影：保留判定依据，剔除文件系统路径。"""
    return {
        "domain_item_id": row.get("id"),
        "material_id": row.get("material_id"),
        "source_kind": row.get("source_kind"),
        "ordinal": row.get("ordinal"),
        "required": bool(row.get("required")),
        "raw_chars": row.get("raw_chars"),
        "raw_tokens": row.get("raw_tokens"),
        "content_hash": row.get("content_hash"),
        "state": row.get("state"),
        "reason_code": row.get("reason_code"),
        "reason_detail": row.get("reason_detail"),
        "duplicate_of_item_id": row.get("duplicate_of_item_id"),
    }


#: 每个 domain item 最多回传的 Source ID 数（避免超大材料把响应撑爆）。
MAX_SPAN_IDS_PER_ITEM = 20


@app.get("/api/runs/{run_id}/material-domain")
async def get_run_material_domain(run_id: int):
    """Material Domain 快照（材料总数 / 唯一材料 / 逐项终态与原因）。

    * run 不存在 → 404
    * flag=off 或该 run 未建立 domain → 404（语义：没有材料域可查）
    """
    run = query_one("SELECT id, workflow, status FROM workflow_runs WHERE id=?", (run_id,))
    if not run:
        raise HTTPException(404, "run 不存在")
    domain = query_one("SELECT * FROM material_domains WHERE run_id=?", (run_id,))
    if not domain:
        raise HTTPException(404, "该运行未建立 Material Domain（V6_LEARNING_ENGINE=off 或运行早于 V6）")
    items = query(
        "SELECT * FROM material_domain_items WHERE domain_id=? ORDER BY ordinal, id",
        (domain["id"],),
    )
    unique_items = [i for i in items if i.get("state") != "duplicate"]
    exempt = [i for i in items if i.get("state") in ("unsupported", "failed", "excluded_with_reason")]
    # 每个 item 的真实 Source ID 列表（供前端按 ID 取原文；不猜测、不拼接）
    span_rows = query(
        "SELECT domain_item_id, source_id, span_state FROM source_spans WHERE domain_id=? "
        "ORDER BY ordinal, id",
        (domain["id"],),
    )
    spans_by_item: dict[int, dict] = {}
    for s in span_rows:
        item_id = s.get("domain_item_id")
        if item_id is None:
            continue
        bucket = spans_by_item.setdefault(int(item_id), {"span_count": 0, "source_ids": []})
        bucket["span_count"] += 1
        if len(bucket["source_ids"]) < MAX_SPAN_IDS_PER_ITEM:
            bucket["source_ids"].append(s["source_id"])
    public_items = []
    for row in items:
        public = _public_domain_item(row)
        bucket = spans_by_item.get(int(row["id"]))
        public["span_count"] = bucket["span_count"] if bucket else 0
        public["source_ids"] = bucket["source_ids"] if bucket else []
        public_items.append(public)
    return {
        "engine": _v6_engine_info(),
        "domain": {
            "domain_id": domain["id"],
            "run_id": domain["run_id"],
            "scope": domain.get("scope"),
            "course_id": domain.get("course_id"),
            "chapter_id": domain.get("chapter_id"),
            "lesson_id": domain.get("lesson_id"),
            "version": domain.get("version"),
            "schema_version": domain.get("schema_version"),
            "domain_hash": domain.get("domain_hash"),
            "state": domain.get("state"),
            "engine_version": domain.get("engine_version"),
            "transcript_chars": domain.get("transcript_chars"),
            "frozen_at": domain.get("frozen_at"),
            "created_at": domain.get("created_at"),
        },
        "counts": {
            "total_items": len(items),
            "unique_items": len(unique_items),
            "duplicate_items": len(items) - len(unique_items),
            "exempt_items": len(exempt),
            "included_items": sum(1 for i in items if i.get("state") == "included"),
        },
        "items": public_items,
    }


@app.get("/api/runs/{run_id}/coverage")
async def get_run_coverage(run_id: int):
    """覆盖报告 + 账本汇总 + 未处理原因。覆盖率由本地确定性程序计算。"""
    run = query_one("SELECT id, workflow, status FROM workflow_runs WHERE id=?", (run_id,))
    if not run:
        raise HTTPException(404, "run 不存在")
    report = query_one("SELECT * FROM coverage_reports WHERE run_id=?", (run_id,))
    if not report:
        raise HTTPException(404, "该运行暂无覆盖报告（V6_LEARNING_ENGINE=off 或覆盖审计未执行）")
    metrics: dict = {}
    try:
        metrics = json.loads(report.get("metrics_json") or "{}")
    except Exception:
        metrics = {}
    plan = query_one("SELECT * FROM coverage_plans WHERE domain_id=?", (report["domain_id"],))
    ledger_rows = query(
        "SELECT stage, outcome, reason_code, COUNT(*) AS n FROM coverage_ledger "
        "WHERE run_id=? GROUP BY stage, outcome, reason_code ORDER BY stage, outcome",
        (run_id,),
    )
    reasons = query(
        "SELECT reason_code, stage, COUNT(*) AS n FROM coverage_ledger "
        "WHERE run_id=? AND outcome IN ('not_used','noise','duplicate','unsupported','failed','excluded') "
        "GROUP BY reason_code, stage ORDER BY n DESC, reason_code",
        (run_id,),
    )
    return {
        "engine": _v6_engine_info(),
        "run": {"run_id": run["id"], "workflow": run.get("workflow"), "status": run.get("status"),
                "degraded": run.get("status") == "degraded"},
        "report": {
            "domain_id": report["domain_id"],
            "engine_version": report.get("engine_version"),
            "engine_mode": report.get("engine_mode"),
            "gate": report.get("gate"),
            "silent_dropped": report.get("silent_dropped"),
            "degradation_reason": report.get("degradation_reason"),
            "metrics": metrics,
            "created_at": report.get("created_at"),
            "updated_at": report.get("updated_at"),
        },
        "plan": None if not plan else {
            "strategy": plan.get("strategy"),
            "model_profile_id": plan.get("model_profile_id"),
            "context_window": plan.get("context_window"),
            "input_budget_tokens": plan.get("input_budget_tokens"),
            "output_budget_tokens": plan.get("output_budget_tokens"),
            "planned_span_count": plan.get("planned_span_count"),
            "unassigned_count": plan.get("unassigned_count"),
        },
        "ledger": ledger_rows,
        "unprocessed_reasons": reasons,
    }


@app.get("/api/runs/{run_id}/segments")
async def get_run_segments(run_id: int):
    """Phase 2：segment 清单（顺序 / 每段 Source ID / 状态 / 模型 / 重试 / 耗时）。"""
    run = query_one("SELECT id, workflow FROM workflow_runs WHERE id=?", (run_id,))
    if not run:
        raise HTTPException(404, "run 不存在")
    domain = query_one("SELECT id FROM material_domains WHERE run_id=?", (run_id,))
    if not domain:
        raise HTTPException(404, "该运行未建立 Material Domain")
    from .learning_engine.segment import segment_plan_summary
    summary = segment_plan_summary(int(domain["id"]))
    return {"engine": _v6_engine_info(), "run_id": run_id,
            "domain_id": int(domain["id"]), **summary}


@app.get("/api/runs/{run_id}/understanding")
async def get_run_understanding(run_id: int):
    """Phase 2：全局理解 + 逐段理解。

    只返回**结构化理解结果**；不返回模型思维链、prompt 文本或任何路径。
    """
    run = query_one("SELECT id, workflow FROM workflow_runs WHERE id=?", (run_id,))
    if not run:
        raise HTTPException(404, "run 不存在")
    domain = query_one("SELECT id FROM material_domains WHERE run_id=?", (run_id,))
    if not domain:
        raise HTTPException(404, "该运行未建立 Material Domain")
    from .learning_engine.understanding import (
        get_lesson_understanding, get_segment_understandings,
    )
    lesson = get_lesson_understanding(run_id)
    segments = get_segment_understandings(int(domain["id"]))
    if lesson is None and not segments:
        raise HTTPException(404, "该运行尚无 LessonUnderstanding（Phase 2 理解未执行）")
    public_segments = [{
        "segment_id": int(s["segment_id"]),
        "segment_ordinal": int(s.get("ordinal") or 0),
        "status": s.get("status"),
        "model_used": s.get("model_used"),
        "prompt_version": s.get("prompt_version"),
        "schema_version": s.get("schema_version"),
        "input_hash": s.get("input_hash"),
        "content_hash": s.get("content_hash"),
        "source_ref_count": s.get("source_ref_count"),
        "attempts": s.get("attempts"),
        "error": s.get("error"),
        "understanding": s.get("structured") or {},
    } for s in segments]
    lesson_public = None
    if lesson:
        structured = lesson.get("structured") or {}
        lesson_public = {
            "status": lesson.get("status"),
            "model_used": lesson.get("model_used"),
            "prompt_version": lesson.get("prompt_version"),
            "schema_version": lesson.get("schema_version"),
            "input_hash": lesson.get("input_hash"),
            "content_hash": lesson.get("content_hash"),
            "consumed_segment_count": lesson.get("consumed_segment_count"),
            "segment_count": lesson.get("segment_count"),
            "valid_source_count": lesson.get("valid_source_count"),
            "merge_levels": lesson.get("merge_levels"),
            "error": lesson.get("error"),
            "understanding": structured,
        }
    return {"engine": _v6_engine_info(), "run_id": run_id,
            "domain_id": int(domain["id"]),
            "lesson_understanding": lesson_public,
            "segment_understandings": public_segments}


@app.get("/api/runs/{run_id}/cognitive-map")
async def get_run_cognitive_map(run_id: int):
    """Phase 3：认知分析审计（CognitiveMap + 八类认知项 + 输入覆盖率）。

    只返回结构化认知项；不返回模型思维链、prompt 文本或任何路径。
    """
    run = query_one("SELECT id, workflow FROM workflow_runs WHERE id=?", (run_id,))
    if not run:
        raise HTTPException(404, "run 不存在")
    domain = query_one("SELECT id FROM material_domains WHERE run_id=?", (run_id,))
    if not domain:
        raise HTTPException(404, "该运行未建立 Material Domain")
    from .learning_engine.cognition import cognitive_input_coverage, get_cognitive_map
    cmap = get_cognitive_map(run_id)
    if cmap is None:
        raise HTTPException(404, "该运行尚无 CognitiveMap（Phase 3 认知分析未执行）")
    coverage = cognitive_input_coverage(run_id)
    structured = cmap.get("structured") or {}
    items = []
    for row in cmap.get("items") or []:
        items.append({
            "item_id": row.get("id"),
            "stable_key": row.get("stable_key"),
            "item_type": row.get("item_type"),
            "title": row.get("title"),
            "explanation": row.get("explanation"),
            "severity": row.get("severity"),
            "confidence": row.get("confidence"),
            "recommended_treatment": row.get("recommended_treatment"),
            "origin": row.get("origin"),
            "status": row.get("status"),
            "source_refs": [s for s in (row.get("source_ids") or "").split(",") if s],
            "knowledge_unit_refs": [k for k in (row.get("knowledge_unit_keys") or "").split(",") if k],
        })
    by_type: dict[str, int] = {}
    for item in items:
        key = str(item["item_type"])
        by_type[key] = by_type.get(key, 0) + 1
    return {
        "engine": _v6_engine_info(),
        "run_id": run_id,
        "domain_id": int(domain["id"]),
        "cognitive_map": {
            "status": cmap.get("status"),
            "model_used": cmap.get("model_used"),
            "prompt_version": cmap.get("prompt_version"),
            "schema_version": cmap.get("schema_version"),
            "input_hash": cmap.get("input_hash"),
            "content_hash": cmap.get("content_hash"),
            "knowledge_unit_total": cmap.get("knowledge_unit_total"),
            "knowledge_unit_processed": cmap.get("knowledge_unit_processed"),
            "cognitive_input_coverage": cmap.get("cognitive_input_coverage"),
            "batch_count": cmap.get("batch_count"),
            "error": cmap.get("error"),
            "availability": structured.get("availability") or {},
            "note": structured.get("note"),
        },
        "input_coverage": coverage,
        "by_type": by_type,
        "items": items,
        "publication": {
            "included_in_document": False,
            "reason": "Composer V2（Phase 5）尚未实施：认知分析仅用于审计，不进入 HTML/PDF",
        },
    }


@app.get("/api/runs/{run_id}/evidence-v2")
async def get_run_evidence_v2(run_id: int):
    """Phase 4：Evidence V2 只读审计（claims + 逐来源绑定 + 门禁）。

    安全边界（与 cognitive-map 同口径）:
    * 只返回结构化 claim / 绑定 / locator，**不返回**模型思维链、完整 system
      prompt、API key、uploads 绝对路径或任何数据库内部路径；
    * ``bound_quote`` 是**数据库原文**（不是模型生成的 quote）；
    * ``ai_explanation`` 明确标注 ``is_ai_explanation``，供前端显示「模型教学补充
      （非课堂原话）」。

    该端点**不依赖** legacy ``evidence_links``。
    """
    run = query_one("SELECT id, workflow FROM workflow_runs WHERE id=?", (run_id,))
    if not run:
        raise HTTPException(404, "run 不存在")
    domain = query_one("SELECT id FROM material_domains WHERE run_id=?", (run_id,))
    if not domain:
        raise HTTPException(404, "该运行未建立 Material Domain")
    from .learning_engine.evidence_v2 import get_claims, get_evidence_report

    report = get_evidence_report(run_id)
    if report is None:
        raise HTTPException(404, "该运行尚无 Evidence V2（Phase 4 绑定未执行）")
    claims = get_claims(run_id)

    light_claims = []
    for claim in claims:
        sources = []
        for src in claim.get("sources") or []:
            sources.append({
                "source_id": src.get("source_id"),
                "source_span_id": src.get("source_span_id"),
                "relation": src.get("relation"),
                "binding_status": src.get("binding_status"),
                "binding_method": src.get("binding_method"),
                "bound_quote": src.get("bound_quote"),
                "quote_hash": src.get("quote_hash"),
                "source_text_hash": src.get("source_text_hash"),
                "locator": src.get("locator"),
                "start_ms": src.get("start_ms"),
                "end_ms": src.get("end_ms"),
                "page_no": src.get("page_no"),
                "slide_no": src.get("slide_no"),
                "source_kind": src.get("source_kind"),
                "presented_to_producer": bool(src.get("presented_to_producer")),
                "visibility": src.get("visibility"),
                "visible_invocations": src.get("visible_invocations") or [],
                "failure_reason": src.get("failure_reason"),
            })
        light_claims.append({
            "claim_id": claim.get("id"),
            "claim_key": claim.get("claim_key"),
            "claim_type": claim.get("claim_type"),
            "claim_text": claim.get("claim_text"),
            "importance": claim.get("importance"),
            "producer_node": claim.get("producer_node"),
            "evidence_status": claim.get("evidence_status"),
            "requires_source": bool(claim.get("requires_source")),
            "is_ai_explanation": bool(claim.get("is_ai_explanation")),
            "input_hash": claim.get("input_hash"),
            "content_hash": claim.get("content_hash"),
            # Phase 4.1：只暴露**安全摘要**（节点/调用引用/可见性/依据），
            # 不含 prompt、模型上下文、路径或 reasoning。
            "provenance": claim.get("provenance") or {},
            "sources": sources,
        })
    grouped: dict[str, list[dict]] = {}
    for claim in light_claims:
        grouped.setdefault(str(claim["claim_type"]), []).append(claim)

    return {
        "engine": _v6_engine_info(),
        "run_id": run_id,
        "domain_id": int(domain["id"]),
        "report": report,
        "by_type": grouped,
        "claims": light_claims,
        "ai_explanation_label": "模型教学补充（非课堂原话）",
        "evidence_semantics": {
            "verified_by_binder": ["引用存在", "归属正确", "quote 来自数据库",
                                   "来源进入产出该 claim 的模型调用", "类型与证据要求一致"],
            "not_verified_by_binder": ["语义等价 / 语义真实性（属 Phase 5 Critic）"],
            "visibility_scope": "claim_producer_invocations（逐 claim、逐生产者）",
            "visibility_note": "「数据库里存在」≠「该 claim 的 producer 读过」；"
                              "其他 batch 读过也不算已读。",
        },
        "publication": {
            "included_in_document": _v6_engine_info().get("mode") == "on",
            "reason": ("Composer V2 已使用 Evidence V2 组装正式笔记"
                       if _v6_engine_info().get("mode") == "on"
                       else "shadow/off 模式仅审计，不覆盖正式笔记"),
            "real_notes_publish_enabled": _v6_engine_info().get("mode") == "on",
        },
    }


@app.get("/api/runs/{run_id}/quality")
async def get_run_quality(run_id: int):
    """V6 Phase 5 independent quality/publication/sync dimensions."""
    if not query_one("SELECT id FROM workflow_runs WHERE id=?", (run_id,)):
        raise HTTPException(404, "运行不存在")
    from .learning_engine.quality import get_quality_state
    state = get_quality_state(run_id)
    if not state:
        raise HTTPException(404, "该运行尚无 V6 质量状态")
    revisions = query(
        "SELECT id,note_id,run_id,revision,schema_version,content_hash,evidence_status,"
        "coverage_status,review_status,created_at FROM note_revisions WHERE run_id=? ORDER BY revision",
        (run_id,))
    state.pop("metrics_json", None)
    return {"run_id": run_id, "quality": state, "revisions": revisions}


@app.get("/api/learning/mastery")
async def get_learning_mastery(course_id: Optional[int] = None,
                               chapter_id: Optional[int] = None):
    from .learning_engine.learning_loop import mastery_snapshot
    return {"items": mastery_snapshot(course_id=course_id, chapter_id=chapter_id)}


@app.get("/api/errors/{error_id}/learning-trace")
async def get_error_learning_trace(error_id: int):
    if not query_one("SELECT id FROM errors WHERE id=?", (error_id,)):
        raise HTTPException(404, "错题不存在")
    from .learning_engine.learning_loop import source_trace_for_error
    return source_trace_for_error(error_id)


@app.get("/api/source-spans/{source_id}")
async def get_source_span(source_id: str, run_id: int):
    """按 Source ID 读取 span 原文与定位。

    ``run_id`` 是**必填查询参数**：Source ID 只在所属 Material Domain 内唯一，
    缺少 ``run_id`` 会被 FastAPI 直接判为 422，从而不会退化成「跨课程全库
    Source ID 读取入口」。跨 run / domain → 404。
    """
    run = query_one("SELECT id FROM workflow_runs WHERE id=?", (run_id,))
    if not run:
        raise HTTPException(404, "run 不存在")
    domain = query_one("SELECT id FROM material_domains WHERE run_id=?", (run_id,))
    if not domain:
        raise HTTPException(404, "该运行未建立 Material Domain")
    span = query_one(
        "SELECT * FROM source_spans WHERE domain_id=? AND source_id=?",
        (domain["id"], source_id),
    )
    if not span:
        raise HTTPException(404, f"Source ID {source_id} 不属于 run {run_id}")
    canonical = None
    if span.get("canonical_span_id"):
        canonical = query_one(
            "SELECT source_id, locator FROM source_spans WHERE id=? AND domain_id=?",
            (span["canonical_span_id"], domain["id"]),
        )
    return {
        "engine": _v6_engine_info(),
        "source_id": span["source_id"],
        "domain_id": span["domain_id"],
        "run_id": run_id,
        "material_id": span.get("material_id"),
        "domain_item_id": span.get("domain_item_id"),
        "source_chunk_id": span.get("source_chunk_id"),
        "source_kind": span.get("source_kind"),
        "locator": span.get("locator"),
        "ordinal": span.get("ordinal"),
        "start_ms": span.get("start_ms"),
        "end_ms": span.get("end_ms"),
        "page_no": span.get("page_no"),
        "slide_no": span.get("slide_no"),
        "text": span.get("text"),
        "normalized_text_hash": span.get("normalized_text_hash"),
        "token_count": span.get("token_count"),
        "char_count": span.get("char_count"),
        "span_state": span.get("span_state"),
        "reason_code": span.get("reason_code"),
        "reason_detail": span.get("reason_detail"),
        "canonical_source_id": canonical.get("source_id") if canonical else None,
    }


# ---------- 作业流 ----------

@app.post("/api/workflows/homework", status_code=202)
async def run_homework(req: HomeworkRunRequest):
    """作业流（方案 7.2 状态机）：
    - 仅图片输入且未确认 → 走 OCR 管线（homework_ocr），停在 awaiting_confirmation；
    - 已确认 homework_id 或有文本 → 直接求解。"""
    payload = req.model_dump(exclude_none=True)
    homework_id = payload.get("homework_id")
    if homework_id:
        hw = query_one("SELECT status FROM homeworks WHERE id=?", (homework_id,))
        if not hw:
            raise HTTPException(404, "homework 不存在")
        if hw["status"] == "awaiting_confirmation":
            raise HTTPException(409, "作业等待 OCR 确认，请先调用 confirm 接口")
    if (payload.get("images") and not payload.get("homework_text")
            and not homework_id):
        return await enqueue_workflow("homework_ocr", "solve", payload)
    return await enqueue_workflow("homework", "solve", payload)


class HomeworkConfirmRequest(BaseModel):
    questions: list[dict] = []   # [{question_id, text}] — 可修正 OCR 文本


@app.post("/api/homeworks/{homework_id}/confirm")
async def confirm_homework_ocr(homework_id: int, body: HomeworkConfirmRequest):
    """确认 OCR 题目（awaiting_confirmation→confirmed），确认后自动排队求解。"""
    hw = query_one("SELECT status FROM homeworks WHERE id=?", (homework_id,))
    if not hw:
        raise HTTPException(404, "homework 不存在")
    if hw["status"] != "awaiting_confirmation":
        raise HTTPException(409, f"作业状态为 {hw['status']}，不能确认 OCR")
    for q in body.questions:
        execute("UPDATE questions SET text=? WHERE id=? AND homework_id=?",
                (q.get("text", ""), q.get("question_id"), homework_id))
    execute("UPDATE homeworks SET status='confirmed' WHERE id=?", (homework_id,))
    payload = {"homework_id": homework_id}
    run = await enqueue_workflow("homework", "solve", payload)
    return {"homework_id": homework_id, "status": "confirmed",
            "run_id": run.get("run_id")}


@app.post("/api/homeworks/{homework_id}/answer")
async def submit_homework_answer(homework_id: int, body: dict):
    """先做后看（方案 7.4）：学生提交自己的答案后才允许看解答。"""
    qid = body.get("question_id")
    student_answer = (body.get("student_answer") or "").strip()
    if not qid or not student_answer:
        raise HTTPException(400, "question_id 与 student_answer 必填")
    q = query_one("SELECT id, homework_id FROM questions WHERE id=? AND homework_id=?",
                  (qid, homework_id))
    if not q:
        raise HTTPException(404, "题目不存在")
    n = execute(
        "UPDATE questions SET student_answer=?, submitted_at=datetime('now','localtime'), "
        " reveal_allowed=1 WHERE id=? AND (submitted_at IS NULL OR submitted_at='')",
        (student_answer, qid),
    )
    if n == 0:
        raise HTTPException(409, "该题已提交过答案，不能重复提交")
    return {"question_id": qid, "submitted": True, "reveal_allowed": 1}


@app.get("/api/homeworks/{homework_id}")
async def homework_detail(homework_id: int):
    """作业详情（先做后看）：未提交答案的题目不返回解答内容。"""
    hw = query_one("SELECT * FROM homeworks WHERE id=?", (homework_id,))
    if not hw:
        raise HTTPException(404, "homework 不存在")
    questions = query(
        "SELECT id, question_no, text, student_answer, submitted_at, reveal_allowed "
        "FROM questions WHERE homework_id=? ORDER BY question_no", (homework_id,))
    # 只有已提交作答（reveal_allowed=1）的题目才返回解答
    answers = {a["question_id"]: a for a in query(
        "SELECT a.* FROM answer_items a JOIN questions q ON q.id=a.question_id "
        "WHERE q.homework_id=? AND q.reveal_allowed=1", (homework_id,))}
    items = []
    for q in questions:
        item = {k: q[k] for k in ("id", "question_no", "text", "student_answer",
                                  "submitted_at", "reveal_allowed")}
        a = answers.get(q["id"])
        if a:
            item["answer"] = {k: a[k] for k in ("final_answer", "solution_plan",
                                                "detailed_solution", "teaching", "conflict")}
        items.append(item)
    return {"homework": hw, "questions": items}


@app.get("/api/workflows/homework/{run_id}")
async def get_homework_run(run_id: int):
    run = query_one("SELECT * FROM workflow_runs WHERE id=? AND workflow='homework'", (run_id,))
    if not run:
        raise HTTPException(404, "run 不存在")
    nodes = query(
        "SELECT rn.*, COALESCE(mp.gateway_model, rn.model) AS model_display "
        "FROM run_nodes rn LEFT JOIN model_profiles mp ON mp.id=rn.model "
        "WHERE rn.run_id=? ORDER BY rn.id",
        (run_id,),
    )
    return {"run": run, "nodes": nodes}


# ---------- 错题流 ----------

@app.post("/api/workflows/error", status_code=202)
async def run_error(req: ErrorRunRequest):
    payload = req.model_dump(exclude_none=True)
    return await enqueue_workflow("error", "collect", payload)


@app.get("/api/errors")
async def list_errors(status: Optional[str] = None, chapter_id: Optional[int] = None):
    sql = "SELECT * FROM errors WHERE 1=1"
    params = []
    if status:
        sql += " AND status=?"
        params.append(status)
    if chapter_id:
        sql += " AND chapter_id=?"
        params.append(chapter_id)
    sql += " ORDER BY id DESC"
    return query(sql, tuple(params))


def _error_event(error_id: int, event_type: str, old: str, new: str, payload: dict | None = None):
    insert(
        "INSERT INTO error_events (error_id, event_type, old_status, new_status, payload_json) "
        "VALUES (?,?,?,?,?)",
        (error_id, event_type, old, new,
         json.dumps(payload or {}, ensure_ascii=False)),
    )


@app.post("/api/errors/{error_id}/confirm")
async def confirm_error(error_id: int):
    """确认错题（方案 8.3）：rowcount 判定 + 终态 409 + error_events 留痕。"""
    row = query_one("SELECT * FROM errors WHERE id=?", (error_id,))
    if not row:
        raise HTTPException(404, "错题不存在")
    old = row["status"]
    if old in ("confirmed", "rejected"):
        raise HTTPException(409, f"错题已为终态（{old}），不能再次确认")
    n = execute("UPDATE errors SET status='confirmed' WHERE id=? AND status=?", (error_id, old))
    if n == 0:
        raise HTTPException(409, "状态已变化，请刷新后重试")
    _error_event(error_id, "confirmed", old, "confirmed")
    from .learning_engine.learning_loop import record_feedback, source_trace_for_error
    final = {}
    try:
        final = json.loads(row.get("final_error_json") or "{}")
    except Exception:
        final = {}
    links = record_feedback(
        source_type="error", source_id=error_id, relation="mistake_on", weight=-0.20,
        text=" ".join(str(x or "") for x in
                      (row.get("question_text"), row.get("user_explanation"), final.get("cause"))),
        course_id=row.get("course_id"), chapter_id=row.get("chapter_id"),
        lesson_id=row.get("lesson_id"), explicit_points=final.get("knowledge_points") or [])
    return {"id": error_id, "status": "confirmed", "mastery_updates": links,
            "learning_trace": source_trace_for_error(error_id)}


@app.post("/api/errors/{error_id}/reject")
async def reject_error(error_id: int):
    """驳回错题：同 confirm 语义（rowcount + 409 + 事件）。"""
    row = query_one("SELECT status FROM errors WHERE id=?", (error_id,))
    if not row:
        raise HTTPException(404, "错题不存在")
    old = row["status"]
    if old in ("confirmed", "rejected"):
        raise HTTPException(409, f"错题已为终态（{old}），不能驳回")
    n = execute("UPDATE errors SET status='rejected' WHERE id=? AND status=?", (error_id, old))
    if n == 0:
        raise HTTPException(409, "状态已变化，请刷新后重试")
    _error_event(error_id, "rejected", old, "rejected")
    from .learning_engine.learning_loop import deactivate_feedback
    return {"id": error_id, "status": "rejected",
            "mastery_updates": deactivate_feedback("error", error_id)}


# ---------- 复习流 ----------

@app.post("/api/workflows/review", status_code=202)
async def run_review(req: ReviewRunRequest):
    payload = req.model_dump(exclude_none=True)
    return await enqueue_workflow("review", "review", payload)


@app.get("/api/reviews")
async def list_reviews():
    return query("SELECT * FROM reviews ORDER BY id DESC LIMIT 100")


# ---------- 复习作答与 SM-2 闭环（方案 5.3/5.4） ----------

def _self_test_questions(review: dict) -> list[dict]:
    try:
        data = json.loads(review.get("self_test") or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []


@app.get("/api/reviews/{review_id}")
async def get_review(review_id: int, reveal: bool = False):
    """复习包详情。自测题答案默认隐藏：该题已作答或显式 reveal=1 才返回。"""
    review = query_one("SELECT * FROM reviews WHERE id=?", (review_id,))
    if not review:
        raise HTTPException(404, "复习包不存在")
    attempts = query(
        "SELECT * FROM review_attempts WHERE review_id=? ORDER BY id", (review_id,))
    answered_nos = {a["question_no"] for a in attempts if a["question_no"]}
    questions = []
    for i, q in enumerate(_self_test_questions(review), 1):
        qno = str(q.get("question_no") or i)
        item = {"q": q.get("q", ""), "question_no": qno}
        if reveal or qno in answered_nos:
            item["answer"] = q.get("answer", "")
        item["answered"] = qno in answered_nos
        questions.append(item)
    return {"review": {k: review[k] for k in (
        "id", "chapter_id", "course_id", "kind", "status", "score", "outline",
        "review_materials", "created_at") if k in review},
            "questions": questions,
            "attempts": [{"question_no": a["question_no"], "user_answer": a["user_answer"],
                          "is_correct": a["is_correct"], "self_rating": a["self_rating"],
                          "mastery_after": a["mastery_after"]} for a in attempts]}


class ReviewAttemptRequest(BaseModel):
    question_no: str
    user_answer: str
    self_rating: int = 3


@app.post("/api/reviews/{review_id}/attempts")
async def submit_review_attempt(review_id: int, body: ReviewAttemptRequest):
    """提交自测作答：确定性判分 + 简化 SM-2 更新（方案 5.3/5.4）。"""
    import datetime as _dt
    from .database import fetch_all
    from .review_service import (grade_answer, next_interval_days, next_mastery,
                                 consecutive_wrong_count, current_interval_days)
    review = query_one("SELECT * FROM reviews WHERE id=?", (review_id,))
    if not review:
        raise HTTPException(404, "复习包不存在")
    if review.get("status") not in ("generated", "in_progress"):
        raise HTTPException(409, f"复习包状态为 {review.get('status')}，不能作答")

    all_questions = _self_test_questions(review)
    q = None
    for i, x in enumerate(all_questions, 1):
        if str(x.get("question_no") or i) == str(body.question_no):
            q = x
            break
    if not q:
        raise HTTPException(404, f"题目 {body.question_no} 不存在")
    expected = q.get("answer", "")
    is_correct = grade_answer(expected, body.user_answer)

    error_id = q.get("error_id")
    err = fetch_one("SELECT * FROM errors WHERE id=?", (error_id,)) if error_id else None
    # 基准（before）优先级：错题表现值 → 本题上次作答的 after 快照 → 默认首次
    last = fetch_one(
        "SELECT interval_after, mastery_after FROM review_attempts "
        "WHERE review_id=? AND question_no=? ORDER BY id DESC LIMIT 1",
        (review_id, str(body.question_no)),
    )
    if err:
        mastery_before = float(err["mastery"])
        interval_before = current_interval_days(err["next_review_at"])
    elif last:
        mastery_before = float(last["mastery_after"] or 0.5)
        interval_before = float(last["interval_after"] or 1.0)
    else:
        mastery_before = 0.5
        interval_before = 1.0

    prior = fetch_all(
        "SELECT is_correct FROM review_attempts WHERE error_id=? ORDER BY id", (error_id,))
    consec_wrong = consecutive_wrong_count(prior) + (0 if is_correct else 1)
    rating = max(0, min(5, int(body.self_rating)))
    interval_after = next_interval_days(interval_before, rating, consec_wrong)
    mastery_after = next_mastery(mastery_before, is_correct)
    now = _dt.datetime.now()
    next_review_at = (now + _dt.timedelta(days=interval_after)).isoformat(timespec="seconds")

    attempt_id = insert(
        "INSERT INTO review_attempts (review_id, question_no, question_text, user_answer, "
        " is_correct, mastery_after, error_id, expected_answer, self_rating, score, feedback, "
        " mastery_before, interval_before, interval_after, reviewed_at, next_review_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now','localtime'))",
        (review_id, str(body.question_no), q.get("q", ""), body.user_answer,
         1 if is_correct else 0, mastery_after, error_id, expected, rating,
         1.0 if is_correct else 0.0,
         ("正确" if is_correct else f"期望答案：{expected}"),
         mastery_before, interval_before, interval_after,
         now.isoformat(timespec="seconds"), next_review_at),
    )

    if err:
        execute(
            "UPDATE errors SET mastery=?, next_review_at=?, review_stage=review_stage+1 "
            "WHERE id=?",
            (mastery_after, next_review_at, error_id),
        )
    execute("UPDATE reviews SET status='in_progress' WHERE id=? AND status='generated'", (review_id,))

    from .learning_engine.learning_loop import record_feedback
    mastery_updates = record_feedback(
        source_type="review_attempt", source_id=attempt_id, relation="reviewed",
        weight=0.15 if is_correct else -0.25,
        text=f"{q.get('q','')} {expected} {body.user_answer}",
        course_id=review.get("course_id"), chapter_id=review.get("chapter_id"),
        explicit_points=q.get("knowledge_points") or [],
        evidence={"review_id": review_id, "question_no": str(body.question_no),
                  "is_correct": bool(is_correct)})

    return {"attempt_id": attempt_id, "question_no": body.question_no,
            "is_correct": is_correct, "expected_answer": expected,
            "mastery_before": mastery_before, "mastery_after": mastery_after,
            "interval_after_days": interval_after, "next_review_at": next_review_at,
            "knowledge_mastery_updates": mastery_updates}


@app.post("/api/reviews/{review_id}/complete")
async def complete_review(review_id: int):
    """结束本次复习：汇总作答表现，更新复习包状态与得分。"""
    review = query_one("SELECT status FROM reviews WHERE id=?", (review_id,))
    if not review:
        raise HTTPException(404, "复习包不存在")
    if review.get("status") in ("completed",):
        raise HTTPException(409, "复习已完成")
    attempts = query("SELECT * FROM review_attempts WHERE review_id=?", (review_id,))
    total = len(attempts)
    correct = sum(1 for a in attempts if int(a["is_correct"] or 0) == 1)
    score = round(correct / total, 4) if total else 0.0
    execute(
        "UPDATE reviews SET status='completed', score=? WHERE id=?",
        (score, review_id),
    )
    return {"review_id": review_id, "attempts": total, "correct": correct,
            "score": score, "status": "completed"}


# ---------- 材料上传 ----------

@app.post("/api/materials/upload")
async def upload_material(file: UploadFile = File(...), lesson_id: Optional[int] = Form(None),
                          chapter_id: Optional[int] = Form(None), course_id: Optional[int] = Form(None)):
    """
    安全上传（V5.4 整改）:
    1. 扩展名白名单 (ALLOWED_EXTS)
    2. 流式写入临时文件（避免 50MB 级多份内存副本）→ 校验后原子移动到 UPLOAD_DIR
    3. 单文件 ≤ MAX_FILE_SIZE(默认 50MB)
    4. Magic bytes 校验（文本类除外）+ SHA-256 / size
    5. 相同 SHA-256 去重：返回已有材料或建立引用
    6. 基于配置的 UPLOAD_DIR 绝对路径，不回传绝对路径
    7. 初始状态 uploaded（进入材料状态机）
    """
    from pathlib import Path
    import uuid as _uuid
    from tempfile import NamedTemporaryFile
    from .config import UPLOAD_DIR

    raw_name = _safe_filename(file.filename or "") or "unnamed"
    suf = Path(raw_name).suffix.lower()
    if suf not in ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型:{suf or '(无)'}")

    # 流式写临时文件 + 实时 SHA-256 / 大小统计
    tmp_path = None
    final_path = None
    try:
        tmp = NamedTemporaryFile(prefix="up_", suffix=suf, delete=False, dir=str(UPLOAD_DIR))
        tmp_path = tmp.name
        total = 0
        import hashlib as _hl
        dig = _hl.sha256()
        try:
            while True:
                chunk = await file.read(1 * 1024 * 1024)  # 1 MB
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FILE_SIZE:
                    raise HTTPException(status_code=413, detail=f"文件超过 {MAX_FILE_SIZE // (1024*1024)} MB 限制")
                tmp.write(chunk)
                dig.update(chunk)
        finally:
            tmp.close()
        sha256 = dig.hexdigest()
        if total == 0:
            raise HTTPException(status_code=400, detail="空文件")

        # Magic bytes 校验
        head = b""
        with open(tmp_path, "rb") as fh:
            head = fh.read(8)
        if not _check_magic(head, suf):
            raise HTTPException(status_code=400, detail=f"文件内容与扩展名 {suf} 不匹配")

        kind = _kind_of(suf)

        # Blob 去重（方案 4.1）：统一入口处理 live 复用 / 死 blob 复活 / 新落位；
        # 去重命中 = 新建 material 引用同一 blob，绝不修改旧材料归属。
        from .services.blob import acquire_blob
        from .services.file_types import initial_state
        got = acquire_blob(sha256, tmp_path, suf, file.content_type, total, UPLOAD_DIR)
        tmp_path = None
        blob_id = got["blob_id"]
        final_path = got["storage_path"]

        rid = insert(
            "INSERT INTO materials (lesson_id, chapter_id, course_id, file_path, name, display_name, "
            " file_hash, sha256, type, kind, mime, size_bytes, blob_id, parser_status, status, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now','localtime'))",
            (lesson_id, chapter_id, course_id, final_path, raw_name, raw_name,
             sha256[:16], sha256, kind, kind, (file.content_type or ""), total,
             blob_id, *initial_state(kind)),
        )

        _enqueue_parse_if_needed(rid, kind)
        final_path = None  # 成功:不再需要清理
        return {"id": rid, "status": "uploaded", "kind": kind, "name": raw_name,
                "deduped": bool(got["reused"])}
    except HTTPException:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
        if final_path and os.path.exists(final_path):
            os.remove(final_path)
        raise
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
        if final_path and os.path.exists(final_path):
            os.remove(final_path)
        raise


def _kind_of(suf: str) -> str:
    suf = suf.lower()
    if suf in (".pptx", ".ppt"):
        return "ppt"
    if suf == ".pdf":
        return "pdf"
    if suf in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        return "image"
    if suf in (".doc", ".docx"):
        return "doc"
    if suf in (".mp3", ".m4a", ".wav"):
        return "audio"
    return "text"


def _enqueue_parse_if_needed(material_id: int, kind: str) -> None:
    """只有可解析类型进解析队列；image/audio 进入 needs_ocr/transcribing 等待真实识别管线。"""
    from .services.file_types import is_parseable
    if not is_parseable(kind):
        return
    from .workers import enqueue_parse
    enqueue_parse(material_id)


# ---------- 材料库 CRUD ----------


# ---------- 材料库 CRUD ----------

@app.get("/api/materials")
async def list_materials(status: Optional[str] = None, course_id: Optional[int] = None,
                         chapter_id: Optional[int] = None, lesson_id: Optional[int] = None):
    sql = "SELECT * FROM materials WHERE 1=1"
    params = []
    if status:
        sql += " AND status=?"
        params.append(status)
    if course_id is not None:
        sql += " AND course_id=?"
        params.append(course_id)
    if chapter_id is not None:
        sql += " AND chapter_id=?"
        params.append(chapter_id)
    if lesson_id is not None:
        sql += " AND lesson_id=?"
        params.append(lesson_id)
    sql += " ORDER BY id DESC"
    # 不回传绝对路径
    rows = query(sql, tuple(params))
    for r in rows:
        r.pop("file_path", None)
    return rows


@app.get("/api/materials/{material_id}")
async def get_material(material_id: int):
    row = query_one("SELECT * FROM materials WHERE id=?", (material_id,))
    if not row:
        raise HTTPException(404, "材料不存在")
    row.pop("file_path", None)
    return row


class MaterialPatch(BaseModel):
    course_id: Optional[int] = None
    chapter_id: Optional[int] = None
    lesson_id: Optional[int] = None
    name: Optional[str] = None
    status: Optional[str] = None


@app.patch("/api/materials/{material_id}")
async def patch_material(material_id: int, body: MaterialPatch):
    sets = ["updated_at=datetime('now','localtime')"]
    params = []
    for field in ("course_id", "chapter_id", "lesson_id", "name", "status"):
        val = getattr(body, field)
        if val is not None:
            sets.append(f"{field}=?")
            params.append(val)
    if len(sets) == 1:
        raise HTTPException(400, "没有可更新的字段")
    params.append(material_id)
    execute(f"UPDATE materials SET {', '.join(sets)} WHERE id=?", tuple(params))
    return {"id": material_id, "status": "patched"}


@app.delete("/api/materials/{material_id}")
async def delete_material(material_id: int):
    row = query_one("SELECT id FROM materials WHERE id=?", (material_id,))
    if not row:
        raise HTTPException(404, "材料不存在")
    # 解析进行中不可删除（避免与 worker 竞态写 source_chunks）
    running = query_one(
        "SELECT id FROM parse_tasks WHERE material_id=? AND status='running'", (material_id,)
    )
    if running:
        raise HTTPException(409, "材料正在解析中，请等待完成后再删除")
    # 排队中的解析任务直接作废
    execute(
        "UPDATE parse_tasks SET status='cancelled', error='material deleted', "
        " finished_at=datetime('now','localtime'), updated_at=datetime('now','localtime') "
        "WHERE material_id=? AND status='queued'",
        (material_id,),
    )
    # 按依赖顺序清理（evidence→FTS→chunks→parse_tasks→materials→blob 引用减一）
    from .services.blob_gc import delete_material_record, gc_blob_now
    summary = delete_material_record(material_id)
    # 归零的 blob 立即尝试物理删除（失败自动落 GC 任务队列）
    if summary.get("pending_delete"):
        summary["gc"] = gc_blob_now(summary["blob_id"])
    return {"id": material_id, "status": "deleted", **summary}


@app.post("/api/materials/{material_id}/retry")
async def retry_material(material_id: int):
    from .services.file_types import is_parseable
    row = query_one("SELECT id, kind FROM materials WHERE id=?", (material_id,))
    if not row:
        raise HTTPException(404, "材料不存在")
    if not is_parseable(row["kind"] or "text"):
        raise HTTPException(400, f"类型 {row['kind']} 不支持自动解析（等待识别管线）")
    execute(
        "UPDATE materials SET parser_status='queued', status='queued', parse_error=NULL, updated_at=datetime('now','localtime') WHERE id=?",
        (material_id,),
    )
    from .workers import enqueue_parse
    enqueue_parse(material_id)
    return {"id": material_id, "status": "queued"}


# ---------- 存储审计与 GC（方案 4.3） ----------

@app.get("/api/admin/storage/audit")
async def admin_storage_audit():
    from .services.blob_gc import storage_audit
    return storage_audit()


@app.post("/api/admin/storage/gc")
async def admin_storage_gc():
    from .services.blob_gc import run_pending_gc, cleanup_tmp_files
    report = run_pending_gc()
    report["tmp_files_removed"] = cleanup_tmp_files(max_age_hours=0)  # GC 手动触发时清理全部残留
    return report


@app.get("/api/materials/{material_id}/chunks")
async def material_chunks(material_id: int):
    rows = query("SELECT id, type, locator, text, ocr_confidence FROM source_chunks WHERE material_id=? ORDER BY id", (material_id,))
    return rows


# ---------- 同步 ----------

@app.get("/api/sync")
async def sync_status():
    return list_sync_status()


@app.get("/api/sync/vault")
async def vault_info():
    return {"vault_root": str(OBSIDIAN_VAULT_ROOT)}


# ---------- 运行日志 ----------

@app.get("/api/runs")
async def list_runs(limit: int = 50):
    """运行列表。附带 V6 引擎版本与覆盖门禁摘要（V5 运行为 NULL）。

    用 LEFT JOIN 而非逐行查询，避免 N+1；不返回任何文件系统路径或 Key。
    """
    return query(
        "SELECT r.id, r.workflow, r.mode, r.course_id, r.lesson_id, r.chapter_id, "
        "r.status, r.error, r.created_at, r.updated_at, r.parent_run_id, "
        "d.id AS domain_id, d.engine_version AS engine_version, "
        " c.gate AS coverage_gate, c.silent_dropped AS silent_dropped "
        "FROM workflow_runs r "
        "LEFT JOIN material_domains d ON d.run_id = r.id "
        "LEFT JOIN coverage_reports c ON c.run_id = r.id "
        "ORDER BY r.id DESC LIMIT ?",
        (limit,),
    )


@app.get("/api/runs/{run_id}")
async def get_run(run_id: int):
    run = query_one(
        "SELECT id, workflow, mode, course_id, lesson_id, chapter_id, status, error, "
        "created_at, updated_at, parent_run_id FROM workflow_runs WHERE id=?", (run_id,))
    if not run:
        raise HTTPException(404, "run 不存在")
    nodes = query(
        "SELECT rn.id, rn.run_id, rn.node_name, rn.status, rn.agent_role, rn.model, rn.attempt, "
        "rn.input_ref, COALESCE(rn.output_ref, substr(rn.output_json,1,4000)) AS output_ref, "
        "rn.started_at, rn.finished_at, rn.tokens_in, rn.tokens_out, rn.latency_ms, rn.error, "
        "COALESCE(mp.gateway_model, rn.model) AS model_display "
        "FROM run_nodes rn LEFT JOIN model_profiles mp ON mp.id=rn.model "
        "WHERE rn.run_id=? ORDER BY rn.id",
        (run_id,),
    )
    # V5/V6 运行必须可区分（设计文档 §14）；degraded 原因来自覆盖门禁。
    domain = query_one("SELECT id, engine_version, domain_hash FROM material_domains WHERE run_id=?",
                       (run_id,))
    report = query_one("SELECT gate, degradation_reason, silent_dropped FROM coverage_reports WHERE run_id=?",
                       (run_id,))
    return {"run": run, "nodes": nodes,
            "engine": _v6_engine_info(),
            "engine_version": (domain or {}).get("engine_version") or _v6_engine_info()["engine_version"],
            "domain_id": (domain or {}).get("id"),
            "domain_hash": (domain or {}).get("domain_hash"),
            "coverage": None if not report else {
                "gate": report.get("gate"),
                "silent_dropped": report.get("silent_dropped"),
                "degradation_reason": report.get("degradation_reason"),
            }}


@app.get("/api/runs/{run_id}/events")
async def run_events(request: Request, run_id: int):
    """SSE 实时进度：仅当 SSE 断线时前端降级轮询。"""
    from fastapi.responses import StreamingResponse

    async def gen():
        last = None
        try:
            while True:
                run = query_one("SELECT status, error FROM workflow_runs WHERE id=?", (run_id,))
                nodes = query(
                    "SELECT rn.node_name, rn.status, rn.model, "
                    "COALESCE(mp.gateway_model, rn.model) AS model_display "
                    "FROM run_nodes rn LEFT JOIN model_profiles mp ON mp.id=rn.model "
                    "WHERE rn.run_id=? ORDER BY rn.id",
                    (run_id,),
                )
                status = run.get("status") if run else "missing"
                payload = {
                    "run_id": run_id,
                    "status": status,
                    "error": run.get("error", "") if run else None,
                    "nodes": [{"node_name": n["node_name"], "status": n["status"],
                               "model": n["model"], "model_display": n["model_display"]} for n in nodes],
                }
                if payload != last:
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    last = payload
                if is_terminal_run_status(status) or status == "missing":
                    yield "event: done\n\n"
                    break
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: int):
    """取消任务（方案 3.4）。

    queued → 直接 cancelled；running → 打 cancel_requested 标记并中断 asyncio 任务；
    终态 → 409。已完成的节点记录保留，不删除任何材料或用户数据。
    """
    from .workers import worker_manager
    run = query_one("SELECT status FROM workflow_runs WHERE id=?", (run_id,))
    if not run:
        raise HTTPException(404, "run 不存在")
    status = run.get("status")
    if is_terminal_run_status(status):
        raise HTTPException(409, f"运行已结束（{status}），不能取消")
    if status == "queued":
        execute(
            "UPDATE workflow_runs SET status='cancelled', updated_at=datetime('now','localtime') "
            "WHERE id=? AND status IN ('queued','running')",
            (run_id,),
        )
        execute(
            "UPDATE run_tasks SET status='cancelled', error='cancelled by user', "
            " finished_at=datetime('now','localtime'), updated_at=datetime('now','localtime') "
            "WHERE run_id=? AND status IN ('queued','running')",
            (run_id,),
        )
        worker_manager.notify()
        return {"run_id": run_id, "status": "cancelled"}
    # running：请求取消（节点边界 / 模型重试边界 / 硬中断三层保障）
    execute("UPDATE workflow_runs SET cancel_requested=1, updated_at=datetime('now','localtime') WHERE id=?", (run_id,))
    execute("UPDATE run_tasks SET cancel_requested=1, updated_at=datetime('now','localtime') WHERE run_id=?", (run_id,))
    cancelled_live_task = await worker_manager.cancel(run_id)
    if not cancelled_live_task:
        # 重启/崩溃后可能遗留 workflow_runs=running，但新进程没有对应的
        # asyncio task。仅设置 cancel_requested 会产生永久“幽灵运行”；此时
        # 当前进程已确认没有可中断任务，可安全直接收敛为 cancelled。
        execute(
            "UPDATE workflow_runs SET status='cancelled', updated_at=datetime('now','localtime') "
            "WHERE id=? AND status='running'",
            (run_id,),
        )
        execute(
            "UPDATE run_tasks SET status='cancelled', error='cancelled orphaned run', "
            "finished_at=datetime('now','localtime'), updated_at=datetime('now','localtime') "
            "WHERE run_id=? AND status IN ('queued','running','interrupted')",
            (run_id,),
        )
        return {"run_id": run_id, "status": "cancelled"}
    return {"run_id": run_id, "status": "cancellation_requested"}


class RetryRunRequest(BaseModel):
    from_node: Optional[str] = None
    reuse_successful_dependencies: bool = True


@app.post("/api/runs/{run_id}/retry")
async def retry_run(run_id: int, body: RetryRunRequest):
    """不可变重试（方案 3.5）：创建新 run（parent_run_id 指向原运行），
    复制输入；可选复用已成功依赖产物，从指定节点重新排队。原运行不修改。"""
    run = query_one("SELECT * FROM workflow_runs WHERE id=?", (run_id,))
    if not run:
        raise HTTPException(404, "run 不存在")
    from .workers import build_flow, enqueue_workflow
    workflow = run.get("workflow") or ""
    dag = build_flow(workflow)  # 顺便校验 from_node 合法性
    from_node = body.from_node
    if from_node and from_node not in dag.nodes:
        raise HTTPException(400, f"节点 {from_node} 不存在于 {workflow} 工作流")

    new_id = insert(
        "INSERT INTO workflow_runs (workflow, mode, course_id, lesson_id, chapter_id, status, "
        " input_json, parent_run_id, created_at, updated_at) "
        "VALUES (?,?,?,?,?, 'queued', ?, ?, datetime('now','localtime'), datetime('now','localtime'))",
        (workflow, run.get("mode"), run.get("course_id"), run.get("lesson_id"),
         run.get("chapter_id"),
         json.dumps({**_run_input(run), "_retry": {
             "parent_run_id": run_id,
             "from_node": from_node,
             "reuse": bool(body.reuse_successful_dependencies and from_node),
         }}, ensure_ascii=False),
         run_id),
    )
    enqueue_workflow(new_id)
    return {
        "run_id": new_id,
        "parent_run_id": run_id,
        "from_node": from_node,
        "reuse_successful_dependencies": bool(body.reuse_successful_dependencies and from_node),
        "status": "queued",
    }


@app.get("/api/runs/{run_id}/result")
async def run_result(run_id: int):
    """稳定业务 DTO（方案 12.4）：按工作流类型聚合最终 result，前端不解释 DAG 内部输出。"""
    run = query_one("SELECT * FROM workflow_runs WHERE id=?", (run_id,))
    if not run:
        raise HTTPException(404, "run 不存在")
    workflow = run.get("workflow") or ""
    result: dict = {"workflow": workflow, "status": run.get("status"),
                    "error": run.get("error")}
    if workflow == "lesson":
        note = query_one(
            "SELECT id, title, status, body, markdown_path FROM notes WHERE lesson_id=? "
            "ORDER BY id DESC LIMIT 1",
            (run.get("lesson_id"),),
        )
        result["note"] = note
        result["evidence"] = _evidence_for_owner("note", note["id"]) if note else []
    elif workflow == "homework":
        hw_id = _homework_id_of_run(run)
        result["questions"] = query(
            "SELECT id, question_no, text, student_answer, submitted_at, reveal_allowed FROM questions "
            "WHERE homework_id=? ORDER BY question_no",
            (hw_id,),
        )
        # 先做后看（方案 7.4）：只返回已提交作答题目的解答
        result["solutions"] = query(
            "SELECT a.* FROM answer_items a JOIN questions q ON q.id=a.question_id "
            "WHERE q.homework_id=? AND q.reveal_allowed=1 ORDER BY q.question_no",
            (hw_id,),
        )
        result["conflicts"] = [s for s in result["solutions"]
                               if (s.get("conflict") or "").lower() in ("1", "true", "conflict")]
    elif workflow == "error":
        result["error"] = query_one(
            "SELECT * FROM errors ORDER BY id DESC LIMIT 1"
        ) or None
    elif workflow == "review":
        result["review"] = query_one(
            "SELECT id, kind, status, outline, self_test, score FROM reviews ORDER BY id DESC LIMIT 1"
        ) or None
    return {"run_id": run_id, **result}


def _homework_id_of_run(run: dict) -> int:
    return int(_run_input(run).get("homework_id") or 0)


def _run_input(run: dict) -> dict:
    try:
        inp = json.loads(run.get("input_json") or "{}")
    except Exception:
        inp = {}
    return inp if isinstance(inp, dict) else {}


def _evidence_for_owner(owner_type: str, owner_id: int) -> list[dict]:
    return query(
        "SELECT e.*, c.locator AS chunk_locator FROM evidence_links e "
        "LEFT JOIN source_chunks c ON c.id=e.chunk_id "
        "WHERE e.owner_type=? AND e.owner_id=?",
        (owner_type, owner_id),
    )


@app.post("/api/migrate")
async def run_migration_route(base_path: Optional[str] = None):
    from .migration import migrate_from_path, migrate_from_legacy
    if base_path:
        stats = migrate_from_path(base_path)
    else:
        stats = migrate_from_legacy()
    return {"status": "done", "stats": stats}

# ---------- RAG ----------

@app.get("/api/rag/search")
async def rag_search(q: str, chapter_id: Optional[int] = None, lesson_id: Optional[int] = None, top_k: int = 5):
    from .rag import retrieve_chunks
    results = await retrieve_chunks(q, chapter_id=chapter_id, lesson_id=lesson_id, top_k=top_k)
    return {"results": results}



# ---------- 知识图谱 ----------

@app.get("/api/graph/nodes")
async def list_graph_nodes(course_id: Optional[int] = None):
    if course_id:
        return query("SELECT * FROM graph_nodes WHERE course_id=? ORDER BY id", (course_id,))
    return query("SELECT * FROM graph_nodes ORDER BY id")


@app.get("/api/graph/edges")
async def list_graph_edges():
    return query("SELECT * FROM graph_edges ORDER BY id")


@app.get("/api/graph")
async def get_graph_data():
    nodes = query("SELECT * FROM graph_nodes ORDER BY id")
    edges = query("SELECT * FROM graph_edges ORDER BY id")
    return {"nodes": nodes, "edges": edges}


# ---------- 作业与题目 ----------

@app.get("/api/homeworks")
async def list_homeworks(chapter_id: Optional[int] = None, lesson_id: Optional[int] = None):
    sql = "SELECT * FROM homeworks WHERE 1=1"
    params = []
    if chapter_id:
        sql += " AND chapter_id=?"
        params.append(chapter_id)
    if lesson_id:
        sql += " AND lesson_id=?"
        params.append(lesson_id)
    sql += " ORDER BY id DESC"
    return query(sql, tuple(params))


@app.post("/api/homeworks")
async def create_homework():
    rid = insert(
        "INSERT INTO homeworks (title, status) VALUES (?, ?)",
        ("新作业", "pending"),

)
    return {"id": rid, "status": "pending"}


@app.get("/api/questions")
async def list_questions(homework_id: Optional[int] = None, limit: int = 50):
    if homework_id:
        return query("SELECT * FROM questions WHERE homework_id=? ORDER BY id", (homework_id,))
    return query("SELECT * FROM questions ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/answer_items")
async def list_answer_items(question_id: Optional[int] = None):
    if question_id:
        return query("SELECT * FROM answer_items WHERE question_id=? ORDER BY id", (question_id,))
    return query("SELECT * FROM answer_items ORDER BY id DESC LIMIT 200")


# ---------- 学习笔记 ----------

@app.get("/api/notes")
async def list_notes(lesson_id: Optional[int] = None, chapter_id: Optional[int] = None):
    sql = "SELECT * FROM notes WHERE 1=1"
    params = []
    if lesson_id:
        sql += " AND lesson_id=?"
        params.append(lesson_id)
    if chapter_id:
        sql += " AND chapter_id=?"
        params.append(chapter_id)
    sql += " ORDER BY id"
    return query(sql, tuple(params))


# ---------- HTML / PDF 学习产出 ----------

@app.get("/api/artifacts")
async def list_artifacts(owner_type: Optional[str] = None, owner_id: Optional[int] = None):
    sql, params = "SELECT * FROM document_artifacts WHERE 1=1", []
    if owner_type:
        if owner_type not in ("note", "review") or owner_id is None:
            raise HTTPException(400, "owner_type 必须是 note/review，并提供 owner_id")
        sql += f" AND {'note_id' if owner_type == 'note' else 'review_id'}=?"
        params.append(owner_id)
    return [artifact_public(r) for r in query(sql + " ORDER BY id DESC", tuple(params))]


@app.get("/api/artifacts/{artifact_id}")
async def get_artifact(artifact_id: int):
    row = fetch_one("SELECT * FROM document_artifacts WHERE id=?", (artifact_id,))
    if not row:
        raise HTTPException(404, "产出不存在")
    return artifact_public(row)


@app.get("/api/artifacts/{artifact_id}/content")
async def preview_artifact(artifact_id: int):
    row = fetch_one("SELECT * FROM document_artifacts WHERE id=?", (artifact_id,))
    if not row:
        raise HTTPException(404, "产出不存在")
    try:
        return FileResponse(safe_artifact_file(row, "html"), media_type="text/html; charset=utf-8")
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "HTML 尚未生成")


@app.get("/api/artifacts/{artifact_id}/download")
async def download_artifact(artifact_id: int, format: str = "pdf"):
    if format not in ("html", "pdf"):
        raise HTTPException(400, "format 必须是 html 或 pdf")
    row = fetch_one("SELECT * FROM document_artifacts WHERE id=?", (artifact_id,))
    if not row:
        raise HTTPException(404, "产出不存在")
    try:
        path = safe_artifact_file(row, format)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, f"{format.upper()} 尚未生成")
    return FileResponse(path, media_type="application/pdf" if format == "pdf" else "text/html; charset=utf-8",
                        filename=f"v5-{row['document_type']}-{artifact_id}.{format}")


@app.post("/api/artifacts/{artifact_id}/regenerate")
async def regenerate_artifact(artifact_id: int):
    row = fetch_one("SELECT * FROM document_artifacts WHERE id=?", (artifact_id,))
    if not row:
        raise HTTPException(404, "产出不存在")
    owner_type = "note" if row.get("note_id") else "review"
    try:
        result = render_artifact(owner_type=owner_type, owner_id=row.get("note_id") or row.get("review_id"),
                                 document=json.loads(row["structured_json"]))
    except Exception as exc:
        raise HTTPException(500, f"重新生成失败: {exc}")
    return artifact_public(result)


# ---------- 章节 ----------

@app.post("/api/chapters/{chapter_id}/status")
async def update_chapter_status(chapter_id: int, status: str):
    execute("UPDATE chapters SET status=? WHERE id=?", (status, chapter_id))
    return {"id": chapter_id, "status": status}


@app.post("/api/chapters/{chapter_id}/review")
async def mark_chapter_review(chapter_id: int):
    execute("UPDATE chapters SET review_status='generated', reviewed_at=datetime('now','localtime') WHERE id=?", (chapter_id,))
    return {"id": chapter_id, "review_status": "generated"}

# ---------- 章节状态机 ----------

CHAPTER_STATES = ["not_started", "in_progress", "material_ready", "homework_ready", "review_pending", "review_generated", "reviewed"]

@app.get("/api/chapters/{chapter_id}/state")
async def get_chapter_state(chapter_id: int):
    ch = query_one("SELECT * FROM chapters WHERE id=?", (chapter_id,))
    if not ch:
        raise HTTPException(404, "章节不存在")
    # 计算可推进的状态列表
    idx = CHAPTER_STATES.index(ch.get("status", "not_started")) if ch.get("status") in CHAPTER_STATES else 0
    return {
        "chapter": ch,
        "current": ch.get("status", "not_started"),
        "next_states": CHAPTER_STATES[idx+1:],
        "all_states": CHAPTER_STATES,
    }

@app.post("/api/chapters/{chapter_id}/advance")
async def advance_chapter(chapter_id: int):
    ch = query_one("SELECT * FROM chapters WHERE id=?", (chapter_id,))
    if not ch:
        raise HTTPException(404, "章节不存在")
    current = ch.get("status", "not_started")
    if current not in CHAPTER_STATES:
        current = "not_started"
    idx = CHAPTER_STATES.index(current)
    if idx >= len(CHAPTER_STATES) - 1:
        return {"id": chapter_id, "status": current, "note": "已经是最终状态"}
    new_status = CHAPTER_STATES[idx + 1]
    if new_status == "reviewed":
        execute("UPDATE chapters SET status=?, reviewed_at=datetime('now','localtime'), review_status='reviewed' WHERE id=?",
                (new_status, chapter_id))
    else:
        execute("UPDATE chapters SET status=? WHERE id=?", (new_status, chapter_id))
    return {"id": chapter_id, "status": new_status}

class SetStatusBody(BaseModel):
    status: str


@app.post("/api/chapters/{chapter_id}/set_status")
async def set_chapter_status_api(chapter_id: int, body: Optional[SetStatusBody] = None, status: Optional[str] = None):
    # P0: 前端发送 JSON body，后端必须优先读 body；query 参数仅作向后兼容
    value = (body.status if body is not None else None) or status
    if not value:
        raise HTTPException(400, "缺少 status")
    if value not in CHAPTER_STATES:
        raise HTTPException(400, f"无效状态: {value}. 允许值: {CHAPTER_STATES}")
    execute("UPDATE chapters SET status=? WHERE id=?", (value, chapter_id))
    return {"id": chapter_id, "status": value}


# ---------- 数据备份 ----------

@app.get("/api/backups")
async def list_backups_api():
    from .backup import list_backups
    return list_backups()

@app.post("/api/backups")
async def create_backup_api():
    from .backup import backup_database
    result = backup_database()
    if not result:
        raise HTTPException(500, "备份失败")
    return {"status": "ok", "path": str(result)}


# ---------- 校历 & 课程表 ----------

@app.get("/api/academic-terms")
async def get_academic_terms():
    return list_terms()


@app.post("/api/academic-terms")
async def create_academic_term(body: AcademicTermPayload):
    try:
        return save_term(body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/academic-terms/{term_id}")
async def update_academic_term(term_id: int, body: AcademicTermPayload):
    try:
        return save_term(body.model_dump(), term_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/academic-terms/{term_id}")
async def delete_academic_term(term_id: int):
    if not execute("DELETE FROM academic_terms WHERE id=?", (term_id,)):
        raise HTTPException(404, "学期不存在")
    return {"id": term_id, "status": "deleted"}


@app.get("/api/schedule-rules")
async def get_schedule_rules(course_id: Optional[int] = None):
    return list_rules(course_id)


@app.post("/api/schedule-rules")
async def create_schedule_rule(body: ScheduleRulePayload):
    try:
        return save_rule(body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/schedule-rules/{rule_id}")
async def update_schedule_rule(rule_id: int, body: ScheduleRulePayload):
    try:
        return save_rule(body.model_dump(), rule_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/schedule-rules/{rule_id}")
async def delete_schedule_rule(rule_id: int):
    if not execute("DELETE FROM course_schedule_rules WHERE id=?", (rule_id,)):
        raise HTTPException(404, "固定课表规则不存在")
    return {"id": rule_id, "status": "deleted"}


@app.get("/api/calendar-adjustments")
async def get_calendar_adjustments(start: Optional[str] = None, end: Optional[str] = None):
    try:
        return list_adjustments(start, end)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/calendar-adjustments")
async def create_calendar_adjustment(body: CalendarAdjustmentPayload):
    try:
        return save_adjustment(body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/calendar-adjustments/{adjustment_id}")
async def update_calendar_adjustment(adjustment_id: int, body: CalendarAdjustmentPayload):
    try:
        return save_adjustment(body.model_dump(), adjustment_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/calendar-adjustments/{adjustment_id}")
async def delete_calendar_adjustment(adjustment_id: int):
    if not execute("DELETE FROM calendar_adjustments WHERE id=?", (adjustment_id,)):
        raise HTTPException(404, "临时调整不存在")
    return {"id": adjustment_id, "status": "deleted"}


@app.get("/api/schedule/effective")
async def get_effective_schedule(start: Optional[str] = None, days: int = 7):
    try:
        return effective_schedule(start, days)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/schedule/bootstrap")
async def bootstrap_schedule():
    return bootstrap_schedule_data()

@app.get("/api/calendar")
async def list_calendar(course_id: Optional[int] = None, event_type: Optional[str] = None):
    sql = "SELECT * FROM academic_calendar WHERE 1=1"
    params = []
    if course_id:
        sql += " AND course_id=?"
        params.append(course_id)
    if event_type:
        sql += " AND event_type=?"
        params.append(event_type)
    sql += " ORDER BY date, id"
    return query(sql, tuple(params))


@app.post("/api/calendar")
async def create_calendar_event(ev: AcademicCalendarCreate):
    rid = insert(
        "INSERT INTO academic_calendar (course_id, event_type, title, date, detail) VALUES (?,?,?,?,?)",
        (ev.course_id, ev.event_type, ev.title, ev.date, ev.detail),

)
    return {"id": rid, "status": "created"}


@app.put("/api/calendar/{event_id}")
async def update_calendar_event(event_id: int, ev: AcademicCalendarCreate):
    execute(
        "UPDATE academic_calendar SET course_id=?, event_type=?, title=?, date=?, detail=? WHERE id=?",
        (ev.course_id, ev.event_type, ev.title, ev.date, ev.detail, event_id),
    )
    return {"id": event_id, "status": "updated"}


@app.delete("/api/calendar/{event_id}")
async def delete_calendar_event(event_id: int):
    execute("DELETE FROM academic_calendar WHERE id=?", (event_id,))
    return {"id": event_id, "status": "deleted"}


@app.get("/api/exams")
async def list_exams():
    return query(
        "SELECT e.*, c.name AS course_name, c.code AS course_code "
        "FROM academic_calendar e LEFT JOIN courses c ON c.id = e.course_id "
        "WHERE e.event_type='exam' ORDER BY e.date"
    )


# ---------- 移动端 / 局域网发现 ----------


def _collect_ipv4_addresses() -> list[str]:
    """枚举本机所有非回环 IPv4 地址。"""
    ips: list[str] = []
    seen: set[str] = set()
    # 1) 优先用 hostname 解析,覆盖大多数单网卡机器
    try:
        for fam, *_rest, sockaddr in socket.getaddrinfo(socket.gethostname(), None):
            if fam != socket.AF_INET:
                continue
            ip = sockaddr[0]
            if not ip.startswith("127.") and ip not in seen:
                seen.add(ip)
                ips.append(ip)
    except Exception:
        pass
    # 2) 用 UDP socket 探测出口网卡对应的 IP,避免 hostname 在某些环境下解析为空
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127.") and ip not in seen:
                seen.add(ip)
                ips.append(ip)
        finally:
            s.close()
    except Exception:
        pass
    return ips


@app.get("/api/mobile/server-info")
async def mobile_server_info():
    """返回供手机页面显示的连接信息:
    - 局域网 IP (同 Wi-Fi 可直连)
    - Tailscale 状态、IP、DNS 名、serve/funnel URL
    - recommended_url: 优先级 serve > funnel > lan
    - remote_enabled: Tailscale 是否在线
    - mobile_token_required: 后端是否启用了 Token 鉴权
    """
    ts = get_tailscale_info()
    ips = _collect_ipv4_addresses()
    lan_ip = next(
        (ip for ip in ips
         if not ip.startswith("127.")
         and not ip.startswith("169.254.")
         and not ip.startswith("100.")),
        None,
    )
    # fallback recommended_url
    if not ts.get("recommended_url") and lan_ip:
        ts["recommended_url"] = f"http://{lan_ip}:{FRONTEND_PORT}/#/m-upload"

    return {
        "port": int(os.environ.get("V5_PORT", "8800")),
        "lan_ip": lan_ip,
        "tailscale_ip": ts.get("ip"),
        "frontend_port": FRONTEND_PORT,
        "all_ips": ips,
        "tailscale": ts,
        "remote_enabled": ts.get("available", False),
        "mobile_token_required": bool(config.MOBILE_TOKEN),
        "recommended_url": ts.get("recommended_url"),
    }


# ---------- 移动端 Token 管理(仅本机) ----------

_TOKEN_VALUE_RE = re.compile(r"^[A-Za-z0-9_\-.@]{4,64}$")


def _require_local_admin(request: Request) -> None:
    """
    Token 管理 API 只允许本机浏览器直接访问。

    禁止：
    - 局域网 IP 访问
    - Tailscale Serve 访问
    - Tailscale Funnel 访问
    - 携带代理转发头的访问
    """
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="Token 管理功能仅允许在本机使用")

    host = (request.headers.get("host") or "").lower()
    host_name = host.split(":", 1)[0]
    if host_name not in {"127.0.0.1", "localhost"}:
        raise HTTPException(status_code=403, detail="Token 管理功能仅允许本机地址访问")

    forwarded_headers = (
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-real-ip",
    )
    if any(request.headers.get(h) for h in forwarded_headers):
        raise HTTPException(status_code=403, detail="Token 管理功能禁止通过代理访问")


def _mask_token(token: str) -> str | None:
    if not token:
        return None
    if len(token) <= 6:
        return f"{token[0]}***"
    return f"{token[:3]}***"


def _append_token(base_url: str | None, token: str | None) -> str | None:
    if not base_url:
        return None
    if not token:
        return base_url
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}token={quote(token, safe='')}"


def _mobile_token_share_urls(token: str | None = None) -> dict:
    """根据当前 Tailscale / 局域网状态生成分享链接。"""
    ts = get_tailscale_info()

    ips = _collect_ipv4_addresses()
    lan_ip = next(
        (
            ip for ip in ips
            if not ip.startswith("127.")
            and not ip.startswith("169.254.")
            and not ip.startswith("100.")
        ),
        None,
    )

    lan_url = f"http://{lan_ip}:{FRONTEND_PORT}/#/m-upload" if lan_ip else None
    serve_url = ts.get("serve_url")
    funnel_url = ts.get("funnel_url")
    recommended_url = ts.get("recommended_url") or serve_url or funnel_url or lan_url

    return {
        "recommended": _append_token(recommended_url, token),
        "tailscale": _append_token(serve_url, token),
        "funnel": _append_token(funnel_url, token),
        "lan": _append_token(lan_url, token),
    }


class MobileTokenSetRequest(BaseModel):
    token: str


class MobileTokenRotateRequest(BaseModel):
    length: Optional[int] = 32


@app.get("/api/admin/mobile-token/status")
async def admin_mobile_token_status(request: Request):
    """查询移动端 Token 状态。不返回完整 Token。"""
    _require_local_admin(request)

    return {
        "enabled": bool(config.MOBILE_TOKEN),
        "token_hint": _mask_token(config.MOBILE_TOKEN),
        "share_base_urls": _mobile_token_share_urls(None),
    }


@app.post("/api/admin/mobile-token/rotate")
async def admin_mobile_token_rotate(
    body: MobileTokenRotateRequest,
    request: Request,
):
    """一键生成新的移动端 Token。"""
    _require_local_admin(request)

    length = int(body.length or 32)
    length = max(16, min(64, length))

    alphabet = string.ascii_letters + string.digits
    random_part = "".join(secrets.choice(alphabet) for _ in range(length))
    token = f"v5_{random_part}"

    upsert_env_key("V5_MOBILE_TOKEN", token)
    config.set_mobile_token(token)

    return {
        "enabled": True,
        "token": token,
        "share_urls": _mobile_token_share_urls(token),
    }


@app.post("/api/admin/mobile-token/set")
async def admin_mobile_token_set(
    body: MobileTokenSetRequest,
    request: Request,
):
    """设置自定义移动端 Token。"""
    _require_local_admin(request)

    token = body.token.strip()

    if not token:
        raise HTTPException(status_code=400, detail="Token 不能为空")

    if not _TOKEN_VALUE_RE.fullmatch(token):
        raise HTTPException(
            status_code=400,
            detail="Token 只能包含 A-Z a-z 0-9 _ - . @，长度 4-64",
        )

    upsert_env_key("V5_MOBILE_TOKEN", token)
    config.set_mobile_token(token)

    return {
        "enabled": True,
        "token": token,
        "share_urls": _mobile_token_share_urls(token),
    }


@app.post("/api/admin/mobile-token/reset")
async def admin_mobile_token_reset(request: Request):
    """重置为无鉴权模式。Funnel 模式下不建议使用。"""
    _require_local_admin(request)

    upsert_env_key("V5_MOBILE_TOKEN", "")
    config.set_mobile_token("")

    return {
        "enabled": False,
        "token": None,
        "share_urls": _mobile_token_share_urls(None),
    }


# ---------- SPA 静态托管(可选,生产部署) ----------

_FRONTEND_DIST = (
    Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
)
if _FRONTEND_DIST.exists():
    assets_dir = _FRONTEND_DIST / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """兜底路由:返回前端 SPA 的 index.html,交由前端 hash 路由处理。"""
        # API 路径已被上面的 @app.get 捕获;此处只处理静态文件 + SPA 兜底
        if full_path.startswith("api/") or full_path == "api":
            raise HTTPException(404, "API 不存在")
        # P0 安全:拒绝路径穿越,并强制将路径锚定到 dist 根目录内
        if ".." in full_path or "\\" in full_path:
            raise HTTPException(400, "非法路径")
        dist_root = _FRONTEND_DIST.resolve()
        candidate = (dist_root / full_path).resolve()
        try:
            candidate.relative_to(dist_root)
        except ValueError:
            raise HTTPException(400, "非法路径")
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(_FRONTEND_DIST / "index.html"))

