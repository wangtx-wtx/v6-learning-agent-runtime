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
from .database import init_db, query, query_one, execute, fetch_one, insert
from .lifecycle import lifespan
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

app = FastAPI(
    title="v5.5 学习 Agent Runtime",
    description="以课程章节为核心、以证据链为约束的本地学习 Agent Runtime。",
    version="5.5.0",
    lifespan=lifespan,
)

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
    return {"status": "ok", "version": "5.5.0"}


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


@app.post("/api/chapters")
async def create_chapter(ch: ChapterCreate):
    rid = insert(
        "INSERT INTO chapters (course_id, chapter_no, title, syllabus_ref, status) VALUES (?,?,?,?,'not_started')",
        (ch.course_id, ch.chapter_no, ch.title, ch.syllabus_ref),

)
    return {"id": rid, "status": "created"}


@app.get("/api/chapters")
async def list_chapters(course_id: Optional[int] = None):
    if course_id:
        return query("SELECT * FROM chapters WHERE course_id=? ORDER BY chapter_no", (course_id,))
    return query("SELECT * FROM chapters ORDER BY course_id, chapter_no")


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
    row = query_one("SELECT status FROM errors WHERE id=?", (error_id,))
    if not row:
        raise HTTPException(404, "错题不存在")
    old = row["status"]
    if old in ("confirmed", "rejected"):
        raise HTTPException(409, f"错题已为终态（{old}），不能再次确认")
    n = execute("UPDATE errors SET status='confirmed' WHERE id=? AND status=?", (error_id, old))
    if n == 0:
        raise HTTPException(409, "状态已变化，请刷新后重试")
    _error_event(error_id, "confirmed", old, "confirmed")
    return {"id": error_id, "status": "confirmed"}


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
    return {"id": error_id, "status": "rejected"}


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

    return {"attempt_id": attempt_id, "question_no": body.question_no,
            "is_correct": is_correct, "expected_answer": expected,
            "mastery_before": mastery_before, "mastery_after": mastery_after,
            "interval_after_days": interval_after, "next_review_at": next_review_at}


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
    if status in ("completed", "failed", "cancelled"):
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
    await worker_manager.cancel(run_id)
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
    nodes = query(
        "SELECT node_name, status, output_json FROM run_nodes WHERE run_id=? ORDER BY id",
        (run_id,),
    )
    outputs = {}
    for n in nodes:
        try:
            outputs[n["node_name"]] = json.loads(n["output_json"]) if n["output_json"] else {}
        except Exception:
            outputs[n["node_name"]] = {}
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
        course_id = insert(
            "INSERT INTO courses (name, code, semester) VALUES (?,?,?)",
            (req.course_name or req.course_code or "未命名课程", req.course_code, None),

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
                ch_id = insert(
                    "INSERT INTO chapters (course_id, chapter_no, title, status) VALUES (?,?,?,'not_started')",
                    (course["id"], ch_no, ch_title),

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
            insert(
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
        if not full_path or full_path.startswith("api/") or full_path == "api":
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

