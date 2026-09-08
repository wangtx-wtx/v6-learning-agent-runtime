"""
FastAPI 主应用 + API 路由。
"""
import asyncio
import json
import logging
import os
import socket
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Any, Optional
import re
import secrets
import string
from urllib.parse import quote

from . import config
from .config import OBSIDIAN_VAULT_ROOT, FRONTEND_PORT
from .database import init_db, query, query_one, execute, fetch_one
from .gateway import gateway
from .dag import DAGContext
from .dag_lesson import build_lesson_dag
from .dag_homework import build_homework_dag
from .dag_error import build_error_dag
from .dag_review import build_review_dag
from .routing import load_usage_from_gateway, make_route_for_workflow
from .obsidian import ensure_vault_structure, list_sync_status
from .tailscale import get_tailscale_info
from .env_file import upsert_env_key

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


# ---------- 上传安全常量 ----------
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
ALLOWED_EXTS = {
    ".pdf", ".ppt", ".pptx", ".doc", ".docx",
    ".md", ".txt", ".png", ".jpg", ".jpeg", ".webp",
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


def _check_magic(data_head: bytes, ext: str) -> bool:
    """根据扩展名核对文件头几个字节。文本类直接放行。"""
    ext = ext.lower().lstrip(".")
    if ext in ("md", "txt"):
        return True  # 纯文本不做 magic 校验
    expected = _MAGIC_BYTES.get(ext)
    if not expected:
        return True  # 未登记的扩展名(已被 ALLOWED_EXTS 控制)
    return data_head.startswith(expected)

app = FastAPI(title="v5.1 学习 Agent Runtime", description="以课程章节为核心、以证据链为约束的本地学习 Agent Runtime。", version="5.1.0")

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


@app.on_event("startup")
async def startup():
    init_db()
    # 存量库增量迁移：补齐方案要求而旧库缺失的字段（非破坏性）
    try:
        from .database import migrate_existing_db
        _mr = migrate_existing_db()
        if _mr.get("migrated_columns"):
            logger.info("数据库增量迁移: %s", ", ".join(_mr["migrated_columns"]))
    except Exception as e:
        logger.warning(f"migrate_existing_db skipped: {e}")
    # V5.2:运行 schema 演进(唯一索引等)
    try:
        from .migration import run_migrations
        run_migrations()
    except Exception as e:
        logger.warning(f"run_migrations skipped: {e}")
    ensure_vault_structure()
    # 自动导入校历数据（幂等：已有数据则不重复导入）
    try:
        from .seed_data import seed_calendar
        seed_calendar(force=False)
    except Exception as e:
        logger.warning(f"seed_calendar skipped: {e}")
    from .backup import backup_database
    backup_database()
    logger.info("v5 后端启动完成，数据库已初始化")


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
    return {"status": "ok", "version": "5.1.0"}


# ---------- 课程 ----------

@app.post("/api/courses")
async def create_course(course: CourseCreate):
    rid = execute(
        "INSERT INTO courses (name, code, semester, teacher, schedule_json) VALUES (?,?,?,?,?)",
        (course.name, course.code, course.semester, course.teacher,
         json.dumps(course.schedule or {}, ensure_ascii=False)),
        returning_lastrowid=True,
    )
    return {"id": rid, "status": "created"}


@app.get("/api/courses")
async def list_courses():
    return query("SELECT * FROM courses ORDER BY id")


@app.post("/api/chapters")
async def create_chapter(ch: ChapterCreate):
    rid = execute(
        "INSERT INTO chapters (course_id, chapter_no, title, syllabus_ref, status) VALUES (?,?,?,?,'not_started')",
        (ch.course_id, ch.chapter_no, ch.title, ch.syllabus_ref),
        returning_lastrowid=True,
    )
    return {"id": rid, "status": "created"}


@app.get("/api/chapters")
async def list_chapters(course_id: Optional[int] = None):
    if course_id:
        return query("SELECT * FROM chapters WHERE course_id=? ORDER BY chapter_no", (course_id,))
    return query("SELECT * FROM chapters ORDER BY course_id, chapter_no")


@app.post("/api/lessons")
async def create_lesson(ls: LessonCreate):
    rid = execute(
        "INSERT INTO lessons (chapter_id, course_id, lesson_no, title, date) VALUES (?,?,?,?,?)",
        (ls.chapter_id, ls.course_id, ls.lesson_no, ls.title, ls.date),
        returning_lastrowid=True,
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

@app.get("/api/models")
async def list_models_api():
    from .models_registry import list_models
    return list_models()


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
    return await enqueue_workflow("lesson", "attend", payload)


@app.get("/api/workflows/lesson/{run_id}")
async def get_lesson_run(run_id: int):
    run = query_one("SELECT * FROM workflow_runs WHERE id=? AND workflow='lesson'", (run_id,))
    if not run:
        raise HTTPException(404, "run 不存在")
    nodes = query("SELECT * FROM run_nodes WHERE run_id=? ORDER BY id", (run_id,))
    return {"run": run, "nodes": nodes}


# ---------- 作业流 ----------

@app.post("/api/workflows/homework", status_code=202)
async def run_homework(req: HomeworkRunRequest):
    payload = req.model_dump(exclude_none=True)
    return await enqueue_workflow("homework", "solve", payload)


@app.get("/api/workflows/homework/{run_id}")
async def get_homework_run(run_id: int):
    run = query_one("SELECT * FROM workflow_runs WHERE id=? AND workflow='homework'", (run_id,))
    if not run:
        raise HTTPException(404, "run 不存在")
    nodes = query("SELECT * FROM run_nodes WHERE run_id=? ORDER BY id", (run_id,))
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


@app.post("/api/errors/{error_id}/confirm")
async def confirm_error(error_id: int):
    execute("UPDATE errors SET status='confirmed' WHERE id=?", (error_id,))
    return {"status": "confirmed"}


@app.post("/api/errors/{error_id}/reject")
async def reject_error(error_id: int):
    execute("UPDATE errors SET status='rejected' WHERE id=?", (error_id,))
    return {"status": "rejected"}


# ---------- 复习流 ----------

@app.post("/api/workflows/review", status_code=202)
async def run_review(req: ReviewRunRequest):
    payload = req.model_dump(exclude_none=True)
    return await enqueue_workflow("review", "review", payload)


@app.get("/api/reviews")
async def list_reviews():
    return query("SELECT * FROM reviews ORDER BY id DESC LIMIT 100")


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

        # SHA-256 去重：相同内容返回已有材料（只做逻辑引用，不重复落盘）
        existing = query_one("SELECT id FROM materials WHERE sha256=?", (sha256,))
        if existing:
            os.remove(tmp_path)
            tmp_path = None
            # 建立逻辑引用（更新归属绑定）
            execute(
                "UPDATE materials SET lesson_id=COALESCE(?,lesson_id), chapter_id=COALESCE(?,chapter_id), "
                "course_id=COALESCE(?,course_id), updated_at=datetime('now','localtime') WHERE id=?",
                (lesson_id, chapter_id, course_id, existing["id"]),
            )
            return {"id": existing["id"], "status": "uploaded", "kind": _kind_of(suf),
                    "name": raw_name, "deduped": True}

        # 原子移动到最终文件名
        safe_name = f"{_uuid.uuid4().hex}{suf}"
        target = UPLOAD_DIR / safe_name
        os.replace(tmp_path, str(target))
        tmp_path = None

        kind = _kind_of(suf)
        rid = execute(
            "INSERT INTO materials (lesson_id, chapter_id, course_id, file_path, name, display_name, "
            " file_hash, sha256, type, kind, mime, size_bytes, parser_status, status, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'uploaded', 'uploaded', datetime('now','localtime'))",
            (lesson_id, chapter_id, course_id, str(target), raw_name, raw_name,
             sha256[:16], sha256, kind, kind, (file.content_type or ""), total),
            returning_lastrowid=True,
        )
        # 入库即进解析队列（后台线程读取→切分→建索引）
        from .workers import enqueue_parse
        enqueue_parse(rid)
        return {"id": rid, "status": "uploaded", "kind": kind, "name": raw_name}
    except HTTPException:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
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
    execute("DELETE FROM materials WHERE id=?", (material_id,))
    # 物理文件清理交给后台 GC；此处仅删除记录
    return {"id": material_id, "status": "deleted"}


@app.post("/api/materials/{material_id}/retry")
async def retry_material(material_id: int):
    from .workers import enqueue_parse
    row = query_one("SELECT id FROM materials WHERE id=?", (material_id,))
    if not row:
        raise HTTPException(404, "材料不存在")
    execute(
        "UPDATE materials SET parser_status='queued', status='queued', parse_error=NULL, updated_at=datetime('now','localtime') WHERE id=?",
        (material_id,),
    )
    enqueue_parse(material_id)
    return {"id": material_id, "status": "queued"}


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
    return query("SELECT * FROM workflow_runs ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/runs/{run_id}")
async def get_run(run_id: int):
    run = query_one("SELECT * FROM workflow_runs WHERE id=?", (run_id,))
    if not run:
        raise HTTPException(404, "run 不存在")
    nodes = query("SELECT * FROM run_nodes WHERE run_id=? ORDER BY id", (run_id,))
    return {"run": run, "nodes": nodes}


@app.get("/api/runs/{run_id}/events")
async def run_events(request: Request, run_id: int):
    """SSE 实时进度：仅当 SSE 断线时前端降级轮询。"""
    from fastapi.responses import StreamingResponse

    async def gen():
        last = None
        try:
            while True:
                run = query_one("SELECT status, error FROM workflow_runs WHERE id=?", (run_id,))
                nodes = query("SELECT node_name, status, model FROM run_nodes WHERE run_id=? ORDER BY id", (run_id,))
                status = run.get("status") if run else "missing"
                payload = {
                    "run_id": run_id,
                    "status": status,
                    "error": run.get("error", "") if run else None,
                    "nodes": [{"node_name": n["node_name"], "status": n["status"], "model": n["model"]} for n in nodes],
                }
                if payload != last:
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    last = payload
                if status in ("completed", "failed", "cancelled", "missing"):
                    yield "event: done\n\n"
                    break
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/runs/{run_id}/rerun/{node_name}")
async def rerun_node(run_id: int, node_name: str):
    """从失败节点重跑：立即在前台执行（同步），更新该节点 output。"""
    from .dag import DAGContext
    from .workers import build_flow
    run = query_one("SELECT * FROM workflow_runs WHERE id=?", (run_id,))
    if not run:
        raise HTTPException(404, "run 不存在")
    dag = build_flow(run.get("workflow") or "")
    if node_name not in dag.nodes:
        raise HTTPException(404, f"节点 {node_name} 不存在")
    ctx = DAGContext()
    ctx.run_id = run_id
    try:
        ctx.input = json.loads(run.get("input_json") or "{}") if isinstance(run.get("input_json"), str) else (run.get("input_json") or {})
    except Exception:
        ctx.input = {}
    result = await dag.rerun_node(ctx, node_name)
    return {"run_id": run_id, "node": node_name, "result": result}


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
    rid = execute(
        "INSERT INTO homeworks (title, status) VALUES (?, ?)",
        ("新作业", "pending"),
        returning_lastrowid=True,
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
    rid = execute(
        "INSERT INTO academic_calendar (course_id, event_type, title, date, detail) VALUES (?,?,?,?,?)",
        (ev.course_id, ev.event_type, ev.title, ev.date, ev.detail),
        returning_lastrowid=True,
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


@app.post("/api/calendar/seed")
async def seed_calendar_api(force: bool = False):
    from .seed_data import seed_calendar
    return seed_calendar(force=force)


@app.get("/api/exams")
async def list_exams():
    return query(
        "SELECT e.*, c.name AS course_name, c.code AS course_code "
        "FROM academic_calendar e LEFT JOIN courses c ON c.id = e.course_id "
        "WHERE e.event_type='exam' ORDER BY e.date"
    )


# ---------- 教学大纲导入（支持手动粘贴/后续从ECNU抓取） ----------

class SyllabusChaptersItem(BaseModel):
    chapter_no: Optional[int] = None
    title: Optional[str] = None
    lessons: Optional[list] = None


class SyllabusImportReq(BaseModel):
    course_code: Optional[str] = None
    course_name: Optional[str] = None
    course_id: Optional[int] = None
    chapters: list[SyllabusChaptersItem] = []


@app.post("/api/syllabus/import")
async def import_syllabus(req: SyllabusImportReq):
    """导入课程教学大纲 JSON，自动创建 chapter + lesson 节点。

    示例:
    {
      "course_code": "PHYS2509",
      "course_name": "光学",
      "chapters": [
        {"chapter_no": 1, "title": "几何光学", "lessons": ["L01 ...", "L02 ..."]}
      ]
    }
    """
    if not (req.course_code or req.course_name or req.course_id):
        raise HTTPException(400, "需要 course_code / course_name / course_id 中的至少一个")
    # locate course
    course = None
    if req.course_id:
        course = query_one("SELECT * FROM courses WHERE id=?", (req.course_id,))
    if not course and req.course_code:
        course = query_one("SELECT * FROM courses WHERE code=?", (req.course_code,))
    if not course and req.course_name:
        course = query_one("SELECT * FROM courses WHERE name=?", (req.course_name,))
    if not course:
        # 自动创建课程
        course_id = execute(
            "INSERT INTO courses (name, code, semester) VALUES (?,?,?)",
            (req.course_name or req.course_code or "未命名课程", req.course_code, None),
            returning_lastrowid=True,
        )
        course = {"id": course_id}
    stats = {"chapters": 0, "lessons": 0}
    chapters_payload = [
        item.model_dump() if hasattr(item, "model_dump") else item
        for item in req.chapters or []
    ]
    for ch in chapters_payload:
        ch_no = ch.get("chapter_no")
        ch_title = ch.get("title") or f"第{ch_no}章"
        existing = query_one("SELECT id FROM chapters WHERE course_id=? AND title=?", (course["id"], ch_title))
        if existing:
            ch_id = existing["id"]
        else:
            placeholder = query_one(
                "SELECT id FROM chapters WHERE course_id=? AND title LIKE '第1章（未定）' ORDER BY id LIMIT 1",
                (course["id"],),
            )
            if placeholder and ch_no in (1, None):
                ch_id = placeholder["id"]
                execute("UPDATE chapters SET chapter_no=?, title=? WHERE id=?", (ch_no, ch_title, ch_id))
            else:
                ch_id = execute(
                    "INSERT INTO chapters (course_id, chapter_no, title, status) VALUES (?,?,?,'not_started')",
                    (course["id"], ch_no, ch_title),
                    returning_lastrowid=True,
                )
            stats["chapters"] += 1
        for ls in ch.get("lessons") or []:
            if isinstance(ls, str):
                lesson_title = ls
                lesson_no = None
            else:
                lesson_no = ls.get("lesson_no")
                lesson_title = ls.get("title") or ls.get("lesson_no") or "未命名课时"
            date = ls.get("date") if isinstance(ls, dict) else None
            execute(
                "INSERT INTO lessons (chapter_id, course_id, lesson_no, title, date, status) VALUES (?,?,?,?,?,'not_started')",
                (ch_id, course["id"], lesson_no, lesson_title, date),
            )
            stats["lessons"] += 1
    return {"status": "ok", "course_id": course["id"], "stats": stats}


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
        if full_path.startswith("api/"):
            raise HTTPException(404, "API 不存在")
        candidate = _FRONTEND_DIST / full_path
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(_FRONTEND_DIST / "index.html"))

