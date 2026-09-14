"""节点 09 student_simulator：Cognitive Layer（V6 Phase 3）。

职责与边界（任务书 §十–§十三）:

* 只分析**学生可能如何理解、混淆或记忆已有课堂内容**。
* **不得**新增课堂事实、不得直接写最终笔记、不得修改 LessonUnderstanding。
* 每个认知项必须关联至少一个 Knowledge Unit；课堂事实型 item 必须绑定真实
  Source ID（契约层强制）。
* ``pitfall`` 只有在材料 / 教师提醒 / 错题 / 明确认知分析支持时才生成；
  没有易错信息时返回**空** CognitiveMap，绝不为了模板完整而编造。
* 模型教学推断标 ``origin=model_cognitive_inference``，教师明确提醒标
  ``classroom_evidence``，学生错题标 ``confirmed_error``。
* 输入超上下文时按 Knowledge Unit **保序分批**；每个 KU 必须进入某个 batch，
  ``cognitive_input_coverage`` 必须为 1.0。

实现说明：Phase 3 的认知项**归并与去重**由确定性程序完成（稳定键 + Source ID
并集），模型只负责在给定 KU 与材料范围内提出候选认知项。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, Optional

from .. import database as db
from ..dag import RetryableModelError, RunCancelledError
from . import config as engine_config
from .contracts import (
    COGNITIVE_TYPES_REQUIRING_SOURCE,
    SCHEMA_VERSION,
    CognitiveItem,
    CognitiveItemType,
    CognitiveMap,
    CognitiveMapStatus,
    CognitiveOrigin,
    CognitiveStatus,
)
from .segment import _model_input_budget
from .understanding import (
    MERGE_PROMPT_VERSION,
    estimate_messages_tokens,
    get_lesson_understanding,
)

logger = logging.getLogger(__name__)

COGNITIVE_PROMPT_VERSION = "v1"

#: Phase 4.1 认知项 provenance 口径版本（变更即让旧 provenance 失效）。
COGNITIVE_PROVENANCE_VERSION = "v1"

#: 每个 batch 的最大 KU 数（保序切分；超上下文时继续细分）。
#: 预算才是主约束（见 ``plan_ku_batches`` / ``_verify_batch_budgets``）；本值只是
#: 「一批塞太多 KU 会让模型注意力涣散」的经验上限。
DEFAULT_KU_BATCH_SIZE = 8

#: 认知输入账本的终态取值（与 0019 的 CHECK 约束一致）。
LEDGER_CONSUMED = "consumed"
LEDGER_FAILED = "failed"
LEDGER_SKIPPED = "skipped"

#: 单个 KU 独占一批仍超预算时的原因码（诚实失败，不静默截断）。
REASON_KU_OVER_BUDGET = "ku_exceeds_input_budget"


class CognitiveInputError(RuntimeError):
    """认知输入缺失或不可用（不伪造空输入）。"""


class CognitiveBudgetError(CognitiveInputError):
    """认知输入预算不足（单个 KU 独占一批仍放不下）—— 必须诚实失败。"""


class CognitiveSourceScopeError(CognitiveInputError):
    """模型引用了**不属于本批** allowed_source_ids 的 Source ID（P1-B 硬校验）。"""


#: 单个 Source Span 提供给认知分析的原文上限（有界，避免 prompt 失控）。
COGNITIVE_MATERIAL_EXCERPT_CHARS = 400
#: 一批最多提供多少条课堂原文。
COGNITIVE_MATERIAL_MAX_SPANS = 40
#: 一批课堂原文正文的总字符上限（跨 span 的二级约束）。
#: 超过 ``COGNITIVE_MATERIAL_MAX_SPANS`` 或本上限的来源会进入
#: ``omitted_due_budget``，**绝不**被记成 ``foreign``。
COGNITIVE_MATERIAL_MAX_CHARS = 4_000

#: 摘录终态（逐条来源，可审计）。
EXCERPT_INCLUDED = "included"
EXCERPT_TRUNCATED = "truncated"
EXCERPT_OMITTED_BUDGET = "omitted_due_budget"
EXCERPT_FOREIGN = "foreign"


def _budget_from_window(window: int, reserved_output: int) -> int:
    """与分段层同一口径：窗口 × (1-安全边际) − 预留 system − 预留输出。"""
    from .coverage import DEFAULT_RESERVED_SYSTEM, SAFETY_MARGIN

    return max(int(window * (1.0 - SAFETY_MARGIN)) - DEFAULT_RESERVED_SYSTEM
               - int(reserved_output), 0)


def _model_profile_context_window(model_profile_id: Optional[str]) -> Optional[int]:
    """读取模型档案的 ``context_window``；查不到或为空返回 ``None``。

    ``segment._model_input_budget`` 在档案缺失时会回落到保守默认值，而**真实**的
    真实档案却可能配置了小得多的窗口（前端可改）。这里用独立的探测函数区分
    「档案存在且带窗口」与「档案不可用」，预算审计据此如实标注。
    """
    if not model_profile_id:
        return None
    row = db.fetch_one(
        "SELECT context_window FROM model_profiles WHERE id=?", (model_profile_id,))
    if not row or row.get("context_window") in (None, "", 0):
        return None
    try:
        value = int(row["context_window"])
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _cognitive_input_budget(model_profile_id: Optional[str]) -> dict[str, Any]:
    """认知层输入预算：**按传入的候选模型**派生（缺陷 P1-A）。

    早期实现生产路径恒传 ``None``，于是「qwen3_flash 配成 8192 上下文」时认知层
    仍按默认 32768 规划批次，真实请求会直接溢出。现在：

    * 档案存在且带 ``context_window`` → 用该窗口派生预算；
    * 档案缺失或窗口为空 → 回落到保守默认（``degraded`` 语义由
      ``context_window_source`` 如实标注，不猜测放大）。

    返回审计用明细（``model_profile_id`` / ``context_window`` /
    ``reserved_output`` / ``input_budget`` / ``context_window_source``）。
    """
    from .coverage import DEFAULT_CONTEXT_WINDOW, DEFAULT_RESERVED_OUTPUT
    from .segment import _model_input_budget

    budget, window, reserved_output = _model_input_budget(model_profile_id)
    configured = _model_profile_context_window(model_profile_id)
    if configured is not None:
        # 与分段层保持完全一致的口径（覆盖 segment 里的默认回落）
        window = configured
        budget = _budget_from_window(window, reserved_output)
        source = "model_profile"
    elif model_profile_id:
        source = "default_model_profile_missing_or_window_absent"
        window, budget = DEFAULT_CONTEXT_WINDOW, _budget_from_window(
            DEFAULT_CONTEXT_WINDOW, DEFAULT_RESERVED_OUTPUT)
    else:
        source = "default_no_model_profile"
        window, budget = DEFAULT_CONTEXT_WINDOW, _budget_from_window(
            DEFAULT_CONTEXT_WINDOW, DEFAULT_RESERVED_OUTPUT)
    return {
        "model_profile_id": model_profile_id,
        "context_window": int(window),
        "reserved_output": int(reserved_output),
        "input_budget": int(budget),
        "context_window_source": source,
    }


# ---------------------------------------------------------------------------
# 输入装配（缺失即显式为空，不伪造）
# ---------------------------------------------------------------------------
def collect_cognitive_inputs(domain_id: int, run_id: int,
                             lesson_id: Optional[int]) -> dict[str, Any]:
    """装配 student_simulator 的全部输入。

    缺失的数据源一律返回**空列表**并在 ``availability`` 里显式标注为
    ``absent``（附 ``*_note`` 说明原因），绝不伪造内容。

    作用域（缺陷 5）：章节备注 / 已确认错题 / mastery / 作业反馈**必须**限定在
    本堂课所属的 course+chapter+lesson 内。早期实现把 ``knowledge_mastery`` 全表
    前 200 行直接塞进 prompt（不限课程/章节/KU），会把完全无关课程的掌握度数据
    当成这堂课的学情依据 —— 那既误导模型也污染审计。
    """
    lesson = get_lesson_understanding(run_id) or {}
    structured = lesson.get("structured") or {}
    knowledge_units = structured.get("knowledge_units") or []

    # 作用域：优先取 domain 上的 course/chapter/lesson（域即真相源），
    # 其次回落到 lessons 表；两者都缺失时作用域为「未知」。
    course_id: Optional[int] = None
    chapter_id: Optional[int] = None
    dom = db.fetch_one(
        "SELECT course_id, chapter_id, lesson_id FROM material_domains WHERE id=?",
        (int(domain_id),)) or {}
    if dom.get("course_id") is not None:
        course_id = int(dom["course_id"])
    if dom.get("chapter_id") is not None:
        chapter_id = int(dom["chapter_id"])
    if dom.get("lesson_id") is not None:
        lesson_id = int(dom["lesson_id"])
    if lesson_id is not None and (course_id is None or chapter_id is None):
        row = db.fetch_one("SELECT course_id, chapter_id FROM lessons WHERE id=?",
                           (lesson_id,))
        if row:
            course_id = course_id if course_id is not None else row.get("course_id")
            chapter_id = chapter_id if chapter_id is not None else row.get("chapter_id")

    notes: dict[str, Any] = {"chapter_notes": [], "chapter_notes_note": None}
    if chapter_id:
        row = db.fetch_one("SELECT notes, title FROM chapters WHERE id=?", (chapter_id,))
        if row and (row.get("notes") or "").strip():
            notes["chapter_notes"] = [str(row["notes"]).strip()]
    else:
        notes["chapter_notes_note"] = "作用域未知（无 chapter_id），章节备注不参与分析"

    # ---- 已确认错题：lesson 优先，其次本 chapter（都在作用域内）----
    confirmed_errors: list[dict] = []
    errors_scope = "absent"
    if lesson_id:
        confirmed_errors = [dict(r) for r in db.fetch_all(
            "SELECT id, lesson_id, chapter_id, question_text, student_answer, correct_answer "
            "FROM errors WHERE lesson_id=? AND status='confirmed' ORDER BY id LIMIT 50",
            (lesson_id,))]
        errors_scope = f"lesson:{lesson_id}"
    elif chapter_id:
        confirmed_errors = [dict(r) for r in db.fetch_all(
            "SELECT id, lesson_id, chapter_id, question_text, student_answer, correct_answer "
            "FROM errors WHERE chapter_id=? AND status='confirmed' ORDER BY id LIMIT 50",
            (chapter_id,))]
        errors_scope = f"chapter:{chapter_id}"

    # ---- mastery：Phase 6 才建表；表存在时必须按 course/chapter 过滤 ----
    mastery: list[dict] = []
    mastery_note: Optional[str] = None
    if not _table_exists("knowledge_mastery"):
        mastery_note = "Phase 6 才建 knowledge_mastery 表，本轮无 mastery 数据"
    elif chapter_id is None:
        mastery_note = "作用域未知（无 chapter_id），拒绝读取全局 mastery 数据"
    else:
        where = ["chapter_id = ?"]
        params: list[Any] = [chapter_id]
        if course_id is not None:
            where.append("course_id = ?")
            params.append(course_id)
        try:
            mastery = [dict(r) for r in db.fetch_all(
                "SELECT km.id, km.course_id, km.chapter_id, ku.stable_key AS knowledge_unit_key, "
                "km.mastery, km.confidence, km.updated_from FROM knowledge_mastery km "
                "JOIN knowledge_units ku ON ku.id=km.knowledge_unit_id WHERE "
                + " AND ".join("km." + part for part in where) +
                " ORDER BY km.id LIMIT 200", tuple(params))]
        except Exception as e:  # noqa: BLE001 - 表结构差异不阻断认知分析
            logger.info("mastery 读取失败（作用域 chapter=%s）: %s", chapter_id, e)
            mastery = []
            mastery_note = f"mastery 读取不可用：{e}"

    # ---- 作业反馈：作用域在 homeworks 上（questions 表没有 lesson 关联列） ----
    # 修复：早期实现把 lesson 过滤写在 questions 上，而该表并没有 lesson 列 ——
    # 查询每次都抛 ``no such column`` 并被 ``except`` 吞掉，于是「作业反馈」永远
    # 是空的（既是静默失效，也谈不上作用域）。现在按 ``homeworks.lesson_id``
    # 过滤，并把作业自身的 course/chapter/lesson 一并取出以便审计。
    homework_feedback: list[dict] = []
    homework_note: Optional[str] = None
    if lesson_id:
        try:
            homework_feedback = [dict(r) for r in db.fetch_all(
                "SELECT q.id, h.id AS homework_id, h.lesson_id, h.chapter_id, h.course_id, "
                " q.text, a.final_answer, a.conflict "
                "FROM answer_items a JOIN questions q ON q.id = a.question_id "
                "JOIN homeworks h ON h.id = q.homework_id "
                "WHERE h.lesson_id=? ORDER BY q.id LIMIT 50", (lesson_id,))]
        except Exception as e:  # noqa: BLE001 - 表结构差异不阻断认知分析
            logger.warning("作业反馈读取失败（lesson=%s）: %s", lesson_id, e)
            homework_feedback = []
            homework_note = f"作业反馈读取不可用：{e}"
    else:
        homework_note = "作用域未知（无 lesson_id），作业反馈不参与分析"

    availability = {
        "lesson_understanding": "present" if structured else "absent",
        "knowledge_units": f"{len(knowledge_units)}",
        "chapter_notes": f"{len(notes['chapter_notes'])}",
        "confirmed_errors": f"{len(confirmed_errors)}",
        "mastery": f"{len(mastery)}",
        "homework_feedback": f"{len(homework_feedback)}",
        "scope": {"course_id": course_id, "chapter_id": chapter_id, "lesson_id": lesson_id},
        "confirmed_errors_scope": errors_scope,
        "homework_feedback_scope": (f"lesson:{lesson_id}" if lesson_id else "unavailable"),
        "mastery_scope": (f"course={course_id},chapter={chapter_id}"
                          if chapter_id is not None else "unavailable"),
    }
    if notes["chapter_notes_note"]:
        availability["chapter_notes_note"] = notes["chapter_notes_note"]
    if homework_note:
        availability["homework_feedback_note"] = homework_note
    if mastery_note:
        # 明确区分「表不存在」与「有表但作用域不可用」——不得含糊成「无数据」。
        availability["mastery_state"] = ("absent" if not _table_exists("knowledge_mastery")
                                        else "deferred")
        availability["mastery_note"] = mastery_note
    elif _table_exists("knowledge_mastery"):
        availability["mastery_state"] = "ready"
    return {
        "lesson_understanding": structured,
        "knowledge_units": knowledge_units,
        "valid_source_ids": set(structured.get("valid_source_ids") or []),
        "chapter_notes": notes["chapter_notes"],
        "confirmed_errors": confirmed_errors,
        "mastery": mastery,
        "homework_feedback": homework_feedback,
        "availability": availability,
        "course_id": course_id,
        "lesson_id": lesson_id,
        "chapter_id": chapter_id,
        "domain_id": int(domain_id),
        "schema_version": lesson.get("schema_version") or SCHEMA_VERSION,
        "prompt_version": lesson.get("prompt_version") or MERGE_PROMPT_VERSION,
        "understanding_input_hash": lesson.get("input_hash") or "",
    }


def _table_exists(name: str) -> bool:
    row = db.fetch_one(
        "SELECT 1 AS ok FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return bool(row)


def _ku_key(unit: dict, index: int) -> str:
    return str(unit.get("id") or unit.get("topic") or f"KU-{index:04d}")


def _material_excerpts(domain_id: int, source_ids: list[str]) -> dict[str, Any]:
    """取**本 domain 内**的真实课堂原文摘录，并逐条给出终态。

    修复（Phase 3 Final Micro-Closeout 缺陷 P1-C）：早期实现先把请求列表截到
    ``COGNITIVE_MATERIAL_MAX_SPANS`` 条再查询，然后用**截断后**的结果反推
    ``foreign`` —— 于是同一 domain 中第 41 条以后的**合法** Source ID 会被错报成
    「域外」。那既污染审计，也掩盖真实的预算截断。

    现在的口径（三者互不混淆）:

    * ``foreign_source_ids``    —— 在**当前 domain 中确实不存在**（含属于别的
      domain）的 ID。校验针对**全部**请求 ID，不受任何截断影响。
    * ``omitted_due_budget``    —— 合法但受 span 数 / 正文字符上限限制**未发送**
      的来源；每条都带原因，绝不静默丢弃。
    * ``excerpts``              —— 逐条状态 ``included`` / ``truncated``，正文被
      截断时给出 ``sent_chars`` / ``total_chars``。

    返回 dict（不再是裸字符串），使调用方与审计都能复算。
    """
    requested = [str(s).strip() for s in (source_ids or []) if str(s).strip()]
    requested = list(dict.fromkeys(requested))
    result: dict[str, Any] = {
        "text": "（无课堂原文）",
        "requested_source_ids": list(requested),
        "included_source_ids": [],
        "excerpts": [],
        "foreign_source_ids": [],
        "omitted_due_budget": [],
        "excerpt_truncated": False,
    }
    if not requested:
        return result

    # 1) 对**全部**请求 ID 做域内校验（不受 MAX_SPANS / MAX_CHARS 影响）
    q = ",".join("?" * len(requested))
    all_rows = {str(r["source_id"]): r for r in db.fetch_all(
        f"SELECT source_id, locator, text, ordinal FROM source_spans "
        f"WHERE domain_id=? AND source_id IN ({q}) ORDER BY ordinal, id",
        tuple([int(domain_id)] + requested))}
    foreign = [sid for sid in requested if sid not in all_rows]
    if foreign:
        logger.warning("认知 prompt 忽略不属于 domain %s 的 Source ID: %s",
                       domain_id, foreign[:10])

    # 2) 按域内 ordinal 排序后按 span 数 + 正文字符预算装填
    ordered = sorted(all_rows.values(), key=lambda r: (int(r["ordinal"]), str(r["source_id"])))
    lines: list[str] = []
    used_chars = 0
    for row in ordered:
        sid = str(row["source_id"])
        if len(lines) >= COGNITIVE_MATERIAL_MAX_SPANS:
            result["omitted_due_budget"].append(
                {"source_id": sid, "reason": "max_spans_exceeded",
                 "limit": COGNITIVE_MATERIAL_MAX_SPANS})
            continue
        body = (row.get("text") or "").strip().replace("\n", " ")
        total_chars = len(body)
        if used_chars + total_chars > COGNITIVE_MATERIAL_MAX_CHARS:
            result["omitted_due_budget"].append(
                {"source_id": sid, "reason": "char_budget_exceeded",
                 "limit": COGNITIVE_MATERIAL_MAX_CHARS, "total_chars": total_chars})
            continue
        truncated = total_chars > COGNITIVE_MATERIAL_EXCERPT_CHARS
        sent = body[:COGNITIVE_MATERIAL_EXCERPT_CHARS] + "…" if truncated else body
        used_chars += total_chars
        lines.append(f"- [SRC={sid}] ({row.get('locator') or ''}) {sent}")
        result["included_source_ids"].append(sid)
        result["excerpts"].append({
            "source_id": sid, "status": EXCERPT_TRUNCATED if truncated else EXCERPT_INCLUDED,
            "total_chars": total_chars, "sent_chars": len(sent),
        })
        if truncated:
            result["excerpt_truncated"] = True
    for entry in result["omitted_due_budget"]:
        result["excerpts"].append({"source_id": entry["source_id"],
                                   "status": EXCERPT_OMITTED_BUDGET,
                                   "reason": entry["reason"]})
    for sid in foreign:
        result["excerpts"].append({"source_id": sid, "status": EXCERPT_FOREIGN})
    result["foreign_source_ids"] = foreign
    if lines:
        result["text"] = "\n".join(lines)
    return result


def plan_ku_batches(knowledge_units: list[dict], *, batch_size: int = DEFAULT_KU_BATCH_SIZE,
                    token_budget: Optional[int] = None) -> list[list[dict]]:
    """按 Knowledge Unit **保序**分批。

    保序是硬要求：不得按相关性重排或淘汰 KU。若给定 token 预算，则按预算进一步
    细分批次，但**每个 KU 仍然恰好进入一个 batch**（不丢、不重）。
    """
    if not knowledge_units:
        return []
    if batch_size < 1:
        batch_size = 1
    from .normalize import estimate_tokens

    batches: list[list[dict]] = []
    current: list[dict] = []
    used = 0
    for unit in knowledge_units:
        cost = estimate_tokens(json.dumps(unit, ensure_ascii=False))
        if current and (len(current) >= batch_size
                        or (token_budget is not None and used + cost > token_budget)):
            batches.append(current)
            current, used = [], 0
        current.append(unit)
        used += cost
    if current:
        batches.append(current)
    return batches


def _batch_allowed_source_ids(batch: list[dict], valid_source_ids: set[str]) -> list[str]:
    """本批**允许引用**的 Source ID = 本批 KU 实际引用且属于本 domain 的集合。

    这是 batch-local 约束的核心（缺陷 P1-B）：早期实现把整个 domain 的
    ``valid_source_ids`` 作为 ``allowed_source_ids`` 发给每一批，模型因此可以引用
    本批根本没有读取的来源，也白白重复消耗 token。
    """
    return sorted({
        str(sid) for u in batch for sid in (u.get("source_refs") or [])
        if sid in valid_source_ids
    })


def _verify_batch_budgets(batches: list[list[dict]], *, inputs: dict[str, Any],
                          valid_source_ids: set[str], budget: int) -> dict[str, Any]:
    """用**最终实际 messages** 复算每个 batch 的输入 token，验证不超预算。

    与分段层同一口径（``segment.verify_segment_budget``）：预算是相对最终
    messages 计算的，而不是相对 KU 序列化后的 token 之和。任何 batch 超预算都
    必须被显式报告（节点据此失败），绝不静默截断 KU 或材料。
    """
    rows: list[dict] = []
    worst = 0
    over = 0
    omitted_total = 0
    for ordinal, batch in enumerate(batches, 1):
        messages, meta = render_cognitive_messages(
            batch, ordinal, len(batches), domain_id=int(inputs["domain_id"]),
            lesson_understanding=inputs["lesson_understanding"],
            chapter_notes=inputs["chapter_notes"],
            confirmed_errors=inputs["confirmed_errors"],
            mastery=inputs["mastery"],
            homework_feedback=inputs["homework_feedback"],
            valid_source_ids=valid_source_ids)
        used = estimate_messages_tokens(messages)
        omitted_total += len(meta["material"]["omitted_due_budget"])
        worst = max(worst, used)
        if used > budget:
            over += 1
        rows.append({"ordinal": ordinal, "input_tokens": used, "budget": budget,
                     "ku_count": len(batch),
                     "allowed_source_ids": meta["allowed_ids"],
                     "material_omitted_due_budget": len(meta["material"]["omitted_due_budget"]),
                     "material_foreign_source_ids": meta["material"]["foreign_source_ids"]})
    return {"budget": budget, "max_input_tokens": worst,
            "over_budget_batches": over,
            "material_omitted_due_budget": omitted_total,
            "batches": rows}


# ---------------------------------------------------------------------------
# 模型调用
# ---------------------------------------------------------------------------
def render_cognitive_messages(batch: list[dict], batch_ordinal: int, batch_total: int,
                              *, domain_id: int,
                              lesson_understanding: dict,
                              chapter_notes: list[str],
                              confirmed_errors: list[dict],
                              mastery: list[dict],
                              homework_feedback: list[dict],
                              valid_source_ids: set[str]) -> tuple[list[dict], dict]:
    """构造认知分析 messages（材料只出现一次，KU 与证据分离标注）。

    **batch-local 约束**（缺陷 P1-B）：本批只暴露
    * 本批 KU（``ku_block`` 与 ``material_clues`` 都只用本批 KU，不再重复全部 KU）；
    * 本批 KU 实际引用的 Source ID（作为 ``allowed_source_ids`` 写进 system prompt）。

    **模型可见证据边界**（Phase 4 §五）：``allowed_source_ids`` **等于**
    ``included_source_ids`` —— 受摘录预算限制被 ``omitted_due_budget`` 截掉的来源
    虽然「数据库里存在」，但模型根本没读到，因此既不出现在 allowed 清单里，也不
    允许被引用。这样「数据库里有」与「生成该 claim 时模型可见」才是两件事。

    作用域：课堂原文只取当前 domain（``_material_excerpts(domain_id, ...)``，返回
    逐条终态）；章节备注 / 错题 / mastery / 作业反馈由 ``collect_cognitive_inputs``
    完成 course-chapter-lesson 过滤后才传进来。
    """
    from ..integrations.prompts import render_prompt

    referenced = _batch_allowed_source_ids(batch, valid_source_ids)
    # 先取摘录（含逐条终态），再把 allowed 收紧为**真正发送给模型**的那些来源。
    material = _material_excerpts(int(domain_id), referenced)
    included = {str(s) for s in material["included_source_ids"]}
    allowed = [sid for sid in referenced if sid in included]
    omitted_not_allowed = [sid for sid in referenced if sid not in included]
    if omitted_not_allowed:
        # 显式记账：合法但未随本批发送的来源不得被模型引用（也不得算已阅读证据）。
        logger.warning(
            "batch %s: %s 条来源因摘录预算未发送，已从 allowed_source_ids 移除: %s",
            batch_ordinal, len(omitted_not_allowed), omitted_not_allowed[:5])
    ku_block = "\n".join(
        f"- KU_KEY={_ku_key(u, i + 1)} | topic={u.get('topic') or ''} | "
        f"kind={u.get('kind') or ''} | summary={u.get('summary') or ''} | "
        f"source_refs={','.join(u.get('source_refs') or [])}"
        for i, u in enumerate(batch))
    notes_block = "\n".join(f"- {n}" for n in chapter_notes) or "（无章节备注）"
    errors_block = "\n".join(
        f"- 题目：{e.get('question_text') or ''} | 学生答案：{e.get('student_answer') or ''} | "
        f"正确答案：{e.get('correct_answer') or ''}" for e in confirmed_errors) or "（无已确认错题）"
    # mastery 行必须显示其自身作用域，便于人工核对「这些数据真的属于这堂课」。
    mastery_block = "\n".join(
        f"- mastery 记录：id={m.get('id')} course={m.get('course_id')} "
        f"chapter={m.get('chapter_id')} "
        f"ku={m.get('knowledge_unit_key') or '-'} mastery={m.get('mastery')} "
        f"confidence={m.get('confidence')}" for m in mastery) or "（无 mastery 数据）"
    hw_block = "\n".join(
        f"- 作业题：{h.get('text') or ''} | 结论：{h.get('final_answer') or ''} | "
        f"冲突：{h.get('conflict') or ''}" for h in homework_feedback) or "（无作业反馈）"

    p = render_prompt(
        "lesson/student_simulator", COGNITIVE_PROMPT_VERSION,
        batch_ordinal=str(batch_ordinal),
        batch_total=str(batch_total),
        allowed_source_ids=", ".join(allowed) or "（无）",
    )
    # 课堂材料线索：**只列本批 KU** 的 topic/summary（早期实现重复全部 KU，既让
    # 模型看到别的批次内容，也重复消耗 token）。
    lu = lesson_understanding or {}
    material_clues = "\n".join(
        f"- [{u.get('id') or ''}] {u.get('topic') or ''}：{u.get('summary') or ''}"
        for u in batch) or "（无知识点）"
    # 全局主题只作为「本堂课在讲什么」的背景，不含任何 KU 细节。
    topics_block = "、".join(lu.get("topics") or []) or "（无主题）"
    user_text = "\n\n".join([
        f"## 本批 Knowledge Unit（{batch_ordinal}/{batch_total}）",
        ku_block or "（无）",
        "## 本堂课全局主题（背景，不含其他批次内容）",
        topics_block,
        "## 本批课堂材料线索（仅本批 Knowledge Unit）",
        material_clues,
        "## 本批课堂原文（按 Source ID，供判断学生可能在哪里出错）",
        material["text"],
        "## 章节手动备注",
        notes_block,
        "## 已确认错题",
        errors_block,
        "## 已有 mastery 数据",
        mastery_block,
        "## 作业反馈",
        hw_block,
    ])
    messages = [
        {"role": "system", "content": p["text"]},
        {"role": "user", "content": user_text},
    ]
    meta = {"prompt_checksum": p["checksum"], "allowed_ids": allowed,
            "system_text": p["text"], "user_text": user_text, "ku_block": ku_block,
            "batch_source_ids": allowed, "material": material,
            # allowed 已收紧为 included；referenced 保留原请求集合以便审计差额
            "referenced_source_ids": referenced,
            "omitted_not_allowed": omitted_not_allowed,
            "foreign_source_ids": material["foreign_source_ids"],
            "omitted_due_budget": material["omitted_due_budget"],
            "presented_source_ids": list(material["included_source_ids"]),
            "batch_ku_keys": [_ku_key(u, i + 1) for i, u in enumerate(batch)],
            "input_tokens": estimate_messages_tokens(messages)}
    return messages, meta


async def run_student_simulator(ctx, domain_id: int, run_id: int,
                                lesson_id: Optional[int],
                                model_id: Optional[str] = None) -> dict:
    """节点 09 主体。返回 CognitiveMap 的持久化结果摘要。

    预算（缺陷 4 + P1-A）：批次数由**当前候选模型** ``effective_model`` 的
    ``model_profiles.context_window`` 派生，并用最终实际 messages 复算校验；任何
    batch 超预算即整体失败（不静默截断 KU / 材料）。模型 fallback 后 DAG 会以新
    候选重新执行本节点，因此批次按新候选预算重新规划，不沿用上一候选的预算。

    batch-local（P1-B）：每批只暴露本批 KU 与本批 KU 引用的 Source ID；模型输出
    的 ``source_refs`` 必须是**本批** ``allowed_source_ids`` 的子集，跨 batch 仅在
    合并阶段做 domain 级归并。

    失败（缺陷 3 + P1-D）：
    * 部分 batch 失败 → ``status=partial``、账本记 ``failed``，然后重抛
      ``RetryableModelError``（DAG 据此切换下一个候选模型）；
    * **全部** batch 失败 → ``status=failed``；
    * 全部成功且无 item → ``status=empty``；全部成功且有 item → ``succeeded``；
    * 全部候选耗尽时 run 必须 failed 而不是 completed。

    取消：不写半成品 CognitiveMap（``cognitive_maps`` 是 run 级幂等产物，重试会
    整体重建），只把取消向上传播；调用方据此把 run 标为 cancelled。这与
    ``understand_segments`` 的取消语义一致（不落 partial 冒充结果）。
    """
    from ..gateway import gateway
    from ..integrations.schemas import StudentSimulatorOut, parse_model_output

    inputs = collect_cognitive_inputs(domain_id, run_id, lesson_id)
    units = inputs["knowledge_units"]
    valid_source_ids = inputs["valid_source_ids"]
    all_keys = [_ku_key(u, i + 1) for i, u in enumerate(units)]

    effective_model = model_id or ctx.resolve_model("student_simulator") or ""

    if not units:
        # 没有 Knowledge Unit ⇒ 没有可分析的课堂内容。返回空 map 并如实记录，
        # **不虚构 pitfall**，也不把「无输入」伪装成「分析成功」。
        return _persist_cognitive_map(
            domain_id, run_id, lesson_id,
            items=[], batch_lines=[], ku_total=0, ku_processed=0,
            coverage=1.0,
            status=CognitiveMapStatus.EMPTY,
            model_id=effective_model, inputs=inputs, error=None,
            budget_info={**_cognitive_input_budget(effective_model),
                         "max_input_tokens": 0, "over_budget_batches": 0,
                         "material_omitted_due_budget": 0},
            note="无 Knowledge Unit，未产生认知项")

    if not effective_model:
        raise CognitiveInputError("student_simulator 没有可用模型")

    # P1-A：预算必须来自**当前候选模型**的档案，而不是默认窗口。
    budget_audit = _cognitive_input_budget(effective_model)
    budget = int(budget_audit["input_budget"])
    # 先用「每个 KU 独占一批」的粒度探测，再按批量上限合并：这样既保证
    # 「预算才是主约束」，也保证「不因 batch_size 先合批而误判超预算」。
    batches = plan_ku_batches(units, batch_size=1, token_budget=budget)
    if len(batches) < len(units):
        batches = plan_ku_batches(units, token_budget=budget)
    budget_info = {**budget_audit, **_verify_batch_budgets(
        batches, inputs=inputs, valid_source_ids=valid_source_ids, budget=budget)}
    if budget_info["over_budget_batches"]:
        # 单个 KU 独占一批仍超预算：诚实失败（不得截断 KU，也不得跳过）。
        over = [b for b in budget_info["batches"] if b["input_tokens"] > budget]
        raise CognitiveBudgetError(
            f"[{REASON_KU_OVER_BUDGET}] {len(over)} 个认知 batch 超过输入预算 {budget} tokens"
            f"（候选模型 {effective_model}，context_window="
            f"{budget_audit['context_window']}，max_input_tokens="
            f"{budget_info['max_input_tokens']}）："
            + ", ".join(f"batch{b['ordinal']}({b['input_tokens']})" for b in over[:5])
        )

    collected: list[dict] = []
    batch_lines: list[dict] = []
    failed_batches = 0
    for ordinal, batch in enumerate(batches, 1):
        await ctx.raise_if_cancelled()
        batch_keys = [_ku_key(u, i + 1) for i, u in enumerate(batch)]
        messages, meta = render_cognitive_messages(
            batch, ordinal, len(batches), domain_id=int(domain_id),
            lesson_understanding=inputs["lesson_understanding"],
            chapter_notes=inputs["chapter_notes"],
            confirmed_errors=inputs["confirmed_errors"],
            mastery=inputs["mastery"],
            homework_feedback=inputs["homework_feedback"],
            valid_source_ids=valid_source_ids)
        line = {"batch_ordinal": ordinal, "knowledge_unit_keys": batch_keys,
                "model": effective_model,
                "input_hash": _batch_input_hash(domain_id, ordinal, batch, meta),
                "input_tokens": meta.get("input_tokens", 0),
                "allowed_source_ids": meta["allowed_ids"],
                "material_included_source_ids": meta["material"]["included_source_ids"],
                "material_referenced_source_ids": meta["referenced_source_ids"],
                "material_omitted_not_allowed": meta["omitted_not_allowed"],
                "material_omitted_due_budget": meta["omitted_due_budget"],
                "material_foreign_source_ids": meta["foreign_source_ids"],
                "material_excerpt_truncated": meta["material"]["excerpt_truncated"]}
        try:
            resp = await gateway.chat(effective_model, messages,
                                      contract="lesson/student_simulator",
                                      temperature=0.3)
            data = parse_model_output(StudentSimulatorOut, resp.get("content", ""),
                                     f"student_simulator#{ordinal}")
            # P1-B 硬校验：模型输出的 source_refs 必须是**本批**
            # allowed_source_ids 的子集。越界（包括引用了同 domain 其他批次的
            # Source ID）→ 该 batch 失败 → 节点重抛 → DAG 切换候选模型。
            out_of_batch = _out_of_batch_source_refs(data, meta["allowed_ids"])
            if out_of_batch:
                raise CognitiveSourceScopeError(
                    f"batch {ordinal} 引用了不属于本批 allowed_source_ids 的 Source ID: "
                    f"{out_of_batch[:5]}（本批允许: {meta['allowed_ids'] or '（无）'}）"
                )
        except (RunCancelledError, asyncio.CancelledError):
            line.update({"status": LEDGER_FAILED, "reason_code": "run_cancelled"})
            batch_lines.append(line)
            raise
        except BaseException as e:  # noqa: BLE001
            if not isinstance(e, Exception):
                line.update({"status": LEDGER_FAILED, "reason_code": "non_retryable"})
                batch_lines.append(line)
                raise
            logger.warning("cognitive batch %s 失败: %s", ordinal, e)
            failed_batches += 1
            line.update({"status": LEDGER_FAILED,
                         "reason_code": f"batch_failed:{type(e).__name__}",
                         "error": str(e)[:200]})
            batch_lines.append(line)
            continue
        for item in data.get("items") or []:
            item = dict(item)
            item["_batch_keys"] = batch_keys
            # P1-B：本批只承认本批的 allowed_source_ids
            item["_batch_allowed_ids"] = set(meta["allowed_ids"])
            # Phase 4.1：逐 item 记录**产出它的那次模型调用**的可见来源，
            # 否则落库后无法区分「本批读过」与「其他批次读过」。
            item["_provenance"] = [{
                "invocation": ordinal,
                "input_hash": line["input_hash"],
                "allowed_source_ids": list(meta["allowed_ids"]),
                "visible_source_ids": list(meta["material"]["included_source_ids"]),
                # 呈现给模型但未在 allowed 清单里的来源（理论为空；留痕以便审计）
                "extra_presented_source_ids": [
                    s for s in meta["material"]["included_source_ids"]
                    if s not in set(meta["allowed_ids"])],
            }]
            collected.append(item)
        line.update({"status": LEDGER_CONSUMED, "reason_code": None})
        batch_lines.append(line)

    consumed_keys = [k for line in batch_lines if line["status"] == LEDGER_CONSUMED
                     for k in line["knowledge_unit_keys"]]
    processed = len(set(consumed_keys))
    coverage = round(processed / len(all_keys), 6) if all_keys else 1.0

    items = _normalize_items(collected, valid_source_ids, all_keys)
    # 状态矩阵（P1-D）：全部失败 → failed；部分成功 → partial；
    # 全部成功且有 item → succeeded；全部成功且无 item → empty。
    if failed_batches and failed_batches == len(batches):
        status = CognitiveMapStatus.FAILED
    elif failed_batches:
        status = CognitiveMapStatus.PARTIAL
    elif items:
        status = CognitiveMapStatus.SUCCEEDED
    else:
        status = CognitiveMapStatus.EMPTY

    result = _persist_cognitive_map(
        domain_id, run_id, lesson_id, items=items, batch_lines=batch_lines,
        ku_total=len(all_keys), ku_processed=processed, coverage=coverage,
        status=status, model_id=effective_model, inputs=inputs,
        error=(f"{failed_batches}/{len(batches)} 个 batch 失败" if failed_batches else None),
        budget_info=budget_info, note=None)
    if failed_batches:
        # 重抛可恢复错误：DAG 据此切换下一个候选模型；全部候选耗尽时 run 必须
        # failed（绝不是 completed）。账本与 map 已经如实落库，审计不受影响。
        raise RetryableModelError(
            f"cognitive batch 失败 {failed_batches}/{len(batches)}（候选模型 {effective_model}）；"
            f"认知输入覆盖率 {coverage}"
        )
    return result


def _normalize_items(raw_items: list[dict], valid_source_ids: set[str],
                     all_ku_keys: list[str]) -> list[CognitiveItem]:
    """把模型候选收敛为合法认知项。

    * 稳定键 = type + 归一化标题（同键归并，source_refs / KU 取并集）；
    * 引用了本批 ``allowed_source_ids`` 之外、但**本 domain 内合法**的 Source ID：
      保留在 ``source_refs`` 里但记入 ``out_of_batch_source_refs`` —— 它是越界
      证据，必须可审计（Evidence V2 会把它判为「未进入该 producer 的输入」）。
      彻底不存在于本 domain 的 ID 才剔除；
    * 无 KU 关联时回退到该 batch 的 KU（模型漏填不改变「每个 item 至少一个 KU」）；
    * ``pitfall`` 无任何支持证据（Source ID / 错题 / 章节备注）时被丢弃 ——
      绝不为了模板完整而编造易错点。

    ``valid_source_ids`` 仍是整个 domain 的合法集合，用于判定「是跨批越界还是
    彻底不存在的 ID」；真正的准入判定用每条的 ``_batch_allowed_ids``。
    **Phase 4.1**：每个 item 的 ``provenance`` 记录产出它的那次模型调用实际发送
    过的来源；同稳定键合并时逐来源保留 invocation，绝不扩大为所有批次的并集。
    """
    merged: dict[str, dict] = {}
    for raw in raw_items:
        item_type = str(raw.get("type") or raw.get("item_type") or "").strip()
        if item_type not in {t.value for t in CognitiveItemType}:
            logger.warning("忽略非法认知项类型: %r", item_type)
            continue
        title = str(raw.get("title") or "").strip()
        key = f"{item_type}:{title.lower()[:80] or 'untitled'}"
        # batch-local 校验（P1-B）+ 越界留痕（Phase 4.1）
        # ``_batch_allowed_ids`` 由节点逐 batch 注入；缺失时（直接单元调用）
        # 回落到 domain 级集合，保持函数可独立测试。
        local_allowed = raw.get("_batch_allowed_ids")
        if local_allowed is None:
            local_allowed = valid_source_ids
        raw_refs = [str(r) for r in (raw.get("source_refs") or [])]
        refs = [r for r in raw_refs if r in local_allowed]
        out_of_batch = [r for r in raw_refs if r not in local_allowed and r in valid_source_ids]
        dropped = [r for r in raw_refs if r not in valid_source_ids]
        if out_of_batch:
            # 本 domain 内合法、但不属于本批 allowed：模型越过了本批证据边界。
            # 保留引用以便 Evidence V2 判为「未进入该 producer 输入」并阻断门禁，
            # **绝不**因为"别的批次读过"就放行。
            logger.warning("认知项 %s 跨批次引用了其他批的 Source ID（保留以便审计）: %s",
                           key, out_of_batch)
            refs = list(dict.fromkeys(refs + out_of_batch))
        if dropped:
            logger.warning("认知项 %s 引用了本 domain 不存在的 Source ID，已剔除: %s",
                           key, dropped)
        ku_refs = [k for k in (raw.get("knowledge_unit_refs") or []) if k in all_ku_keys] \
            or list(raw.get("_batch_keys") or [])
        if not ku_refs:
            continue
        origin = str(raw.get("origin") or CognitiveOrigin.MODEL_COGNITIVE_INFERENCE.value)
        if origin not in {o.value for o in CognitiveOrigin}:
            origin = CognitiveOrigin.MODEL_COGNITIVE_INFERENCE.value
        # 「课堂证据」必须真的有课堂来源；没有有效 source 时一律降级为模型推断，
        # 不得让任何类型的 item 伪装成课堂事实。
        if origin == CognitiveOrigin.CLASSROOM_EVIDENCE.value and not refs:
            origin = CognitiveOrigin.MODEL_COGNITIVE_INFERENCE.value
        # 课堂事实型 item 必须绑定真实来源；pitfall 无任何支持证据时必须丢弃，
        # 绝不为了模板完整而编造易错点。
        if item_type in {t.value for t in COGNITIVE_TYPES_REQUIRING_SOURCE} and not refs:
            if item_type == CognitiveItemType.PITFALL.value:
                logger.info("丢弃无来源支持的 pitfall 候选: %s", key)
                continue
        treatment = str(raw.get("recommended_treatment") or "explanation")
        if treatment not in {"explanation", "example", "contrast", "drill",
                             "memory_hook", "prerequisite_review", "none"}:
            treatment = "explanation"
        candidate = {
            "stable_key": key,
            "item_type": item_type,
            "title": title,
            "explanation": str(raw.get("explanation") or ""),
            "severity": _clamp(raw.get("severity")),
            "confidence": _clamp(raw.get("confidence")),
            "recommended_treatment": treatment,
            "origin": origin,
            "status": CognitiveStatus.PROPOSED.value,
            "knowledge_unit_refs": ku_refs,
            "source_refs": refs,
            "out_of_batch_source_refs": out_of_batch,
            "provenance": _merge_item_provenance(None, raw.get("_provenance")),
        }
        if key in merged:
            existing = merged[key]
            existing["source_refs"] = list(dict.fromkeys(existing["source_refs"] + refs))
            existing["knowledge_unit_refs"] = list(dict.fromkeys(
                existing["knowledge_unit_refs"] + ku_refs))
            existing["out_of_batch_source_refs"] = list(dict.fromkeys(
                existing["out_of_batch_source_refs"] + out_of_batch))
            existing["provenance"] = _merge_item_provenance(
                existing.get("provenance"), raw.get("_provenance"))
            existing["severity"] = max(existing["severity"], candidate["severity"])
            existing["confidence"] = max(existing["confidence"], candidate["confidence"])
        else:
            merged[key] = candidate

    out: list[CognitiveItem] = []
    for candidate in merged.values():
        try:
            out.append(CognitiveItem(**candidate))
        except Exception as e:  # noqa: BLE001
            logger.warning("认知项未通过契约校验，已丢弃 %s: %s", candidate.get("stable_key"), e)
    out.sort(key=lambda i: (-i.severity, i.item_type.value, i.stable_key))
    return out


def _out_of_batch_source_refs(data: dict, allowed_ids: list[str]) -> list[str]:
    """返回模型输出中**不属于本批 allowed_source_ids** 的 Source ID（保序去重）。

    只统计形如 ``T000001`` 的引用（与 understanding 层同一正则口径）；空引用不算
    越界（那是「无来源」，由契约层处理）。
    """
    from .understanding import _valid_source_id

    allowed = {str(a) for a in allowed_ids}
    bad: list[str] = []
    for sid in _collect_source_refs(data):
        if sid in allowed or sid in bad:
            continue
        if _valid_source_id(sid):
            bad.append(sid)
    return bad


def _collect_source_refs(data: dict) -> list[str]:
    """收集模型输出里所有 Source ID 引用。

    必须覆盖 ``lesson/student_simulator`` 的真实输出形状（``items[].source_refs``）
    —— 早期实现只扫 ``knowledge_units/definitions/formulas/derivations/examples``
    （那是 segment understanding 的形状），于是认知项的 source_refs **从未被校验**，
    越界引用只会被静默剔除、不会让 batch 失败。
    """
    refs: list[str] = []
    for sid in data.get("source_refs") or []:
        refs.append(str(sid).strip())
    nested_keys = ("items", "knowledge_units", "definitions", "formulas",
                   "derivations", "examples")
    for key in nested_keys:
        for item in data.get(key) or []:
            if isinstance(item, dict):
                for sid in item.get("source_refs") or []:
                    refs.append(str(sid).strip())
    return [r for r in refs if r]


def _merge_item_provenance(existing: Optional[dict],
                           incoming: Any) -> dict[str, Any]:
    """把「产出该认知项的那次模型调用」的可见来源并入 provenance（Phase 4.1）。

    结构::

        {
          "producer_kind": "cognitive_map",
          "visibility_basis": "cognitive_batch_audit",
          "provenance_version": "v1",
          "invocations": [
            {"invocation": 1, "input_hash": "...",
             "allowed_source_ids": [...], "visible_source_ids": [...]},
            ...
          ],
          "visible_source_ids": [...],          # 逐 invocation 的并集
          "per_source_invocations": {"T000001": [1], "T000002": [2]}
        }

    关键：``per_source_invocations`` 逐来源记录「哪些 invocation 真的发过它」。
    只有出现在至少一个**产出该 item 的** invocation 里的来源，才能被 Binder 判为
    「模型已读」。它绝不等于「本 run 所有批次读过的来源并集」。
    """
    out = dict(existing or {})
    out.setdefault("producer_kind", "cognitive_map")
    out.setdefault("visibility_basis", "cognitive_batch_audit")
    out.setdefault("provenance_version", COGNITIVE_PROVENANCE_VERSION)
    invocations: list[dict] = list(out.get("invocations") or [])
    per_source: dict[str, list[int]] = {
        str(k): [int(x) for x in (v or [])]
        for k, v in (out.get("per_source_invocations") or {}).items()}
    seen_invocations = {int(i.get("invocation")) for i in invocations}
    for entry in (incoming or []):
        if not isinstance(entry, dict):
            continue
        ordinal = int(entry.get("invocation") or 0)
        if ordinal <= 0 or ordinal in seen_invocations:
            continue
        seen_invocations.add(ordinal)
        visible = sorted({str(s) for s in (entry.get("visible_source_ids") or [])})
        invocations.append({
            "invocation": ordinal,
            "input_hash": entry.get("input_hash") or "",
            "allowed_source_ids": sorted({str(s) for s in (entry.get("allowed_source_ids") or [])}),
            "visible_source_ids": visible,
            "extra_presented_source_ids": sorted({
                str(s) for s in (entry.get("extra_presented_source_ids") or [])}),
        })
        for sid in visible:
            per_source.setdefault(sid, [])
            if ordinal not in per_source[sid]:
                per_source[sid].append(ordinal)
    invocations.sort(key=lambda i: int(i["invocation"]))
    out["invocations"] = invocations
    out["per_source_invocations"] = {k: sorted(v) for k, v in sorted(per_source.items())}
    out["visible_source_ids"] = sorted(per_source.keys())
    out["source_scope"] = "claim_producer_invocations"
    return out


def _clamp(value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, v))


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------
def _batch_input_hash(domain_id: int, ordinal: int, batch: list[dict],
                      meta: dict) -> str:
    """单个 batch 的稳定输入哈希（不含 run_id，可复算）。"""
    payload = {
        "schema": SCHEMA_VERSION,
        "prompt": COGNITIVE_PROMPT_VERSION,
        "domain_id": int(domain_id),
        "batch_ordinal": int(ordinal),
        "ku_keys": [_ku_key(u, i + 1) for i, u in enumerate(batch)],
        "ku_content_hashes": [_content_hash(u) for u in batch],
        "batch_source_ids": list(meta.get("batch_source_ids") or []),
        "prompt_checksum": meta.get("prompt_checksum") or "",
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _content_hash(value: Any) -> str:
    """任意 JSON 可序列化内容的稳定短哈希。"""
    blob = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _cognitive_input_hash(domain_id: int, *, inputs: dict[str, Any], batch_lines: list[dict],
                          model_id: str, ku_total: int, coverage: float) -> str:
    """CognitiveMap 的输入哈希：由**真实输入**派生，而不是输出 item 的稳定键。

    修复（缺陷 P1-b）：早期实现用 ``[i.stable_key for i in items]`` 作为输入指纹，
    等于「用输出算输入」—— 模型换一个措辞、item 少一个，输入哈希就变；反过来
    输入（材料 / KU / 备注 / 错题 / 作业反馈）变化却可能算出同一个哈希。现在
    包含：LessonUnderstanding 的 input_hash 与 content_hash + KU 内容 + 各 batch
    引用的 Source ID + Source 正文哈希 + 章节备注 + 错题 + 作业反馈 + mastery +
    prompt/schema 版本 + 候选模型 + 预算。
    """
    lu = inputs.get("lesson_understanding") or {}
    # 本 run 实际用到的 Source ID（由 KU 汇总）
    used_source_ids = sorted({
        sid for u in (inputs.get("knowledge_units") or [])
        for sid in (u.get("source_refs") or [])
        if sid in (inputs.get("valid_source_ids") or set())
    })
    source_hashes = {}
    if used_source_ids:
        q = ",".join("?" * len(used_source_ids))
        for r in db.fetch_all(
                f"SELECT source_id, normalized_text_hash FROM source_spans "
                f"WHERE domain_id=? AND source_id IN ({q})",
                tuple([int(domain_id)] + used_source_ids)):
            source_hashes[str(r["source_id"])] = r.get("normalized_text_hash") or ""
    payload = {
        "algo": "sha256+cognitive-input-v1",
        "schema": SCHEMA_VERSION,
        "prompt": COGNITIVE_PROMPT_VERSION,
        "domain_id": int(domain_id),
        "model": str(model_id or ""),
        "lesson_understanding_input_hash": inputs.get("understanding_input_hash") or "",
        "lesson_understanding_content_hash": lu.get("content_hash") or "",
        "knowledge_units": {_ku_key(u, i + 1): _content_hash(u)
                            for i, u in enumerate(inputs.get("knowledge_units") or [])},
        "source_text_hashes": source_hashes,
        "chapter_notes": [_content_hash(n) for n in (inputs.get("chapter_notes") or [])],
        "confirmed_errors": [_content_hash(e) for e in (inputs.get("confirmed_errors") or [])],
        "homework_feedback": [_content_hash(h) for h in (inputs.get("homework_feedback") or [])],
        "mastery": [_content_hash(m) for m in (inputs.get("mastery") or [])],
        "batch_plan": [[line["batch_ordinal"], line["knowledge_unit_keys"]]
                       for line in batch_lines],
        "ku_total": int(ku_total),
        "coverage": round(float(coverage), 6),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _persist_cognitive_map(domain_id: int, run_id: int, lesson_id: Optional[int],
                           *, items: list[CognitiveItem], batch_lines: list[dict],
                           ku_total: int, ku_processed: int, coverage: float,
                           status: CognitiveMapStatus, model_id: str,
                           inputs: dict[str, Any], error: Optional[str],
                           budget_info: dict[str, Any],
                           note: Optional[str]) -> dict:
    by_type: dict[str, int] = {}
    for item in items:
        by_type[item.item_type.value] = by_type.get(item.item_type.value, 0) + 1
    payload = {
        "items": [json.loads(i.model_dump_json()) for i in items],
        "by_type": by_type,
        "knowledge_unit_total": ku_total,
        "knowledge_unit_processed": ku_processed,
        "cognitive_input_coverage": coverage,
        "batch_count": len(batch_lines),
        "availability": inputs.get("availability") or {},
        "budget": {k: v for k, v in (budget_info or {}).items() if k != "batches"},
        # 每个 batch 的真实结果（序号 / KU / 模型 / 输入哈希 / 终态）必须随产物
        # 一起持久化，便于复算覆盖率与定位失败批次。
        "batches": [dict(line) for line in batch_lines],
    }
    if note:
        payload["note"] = note
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    content_hash = "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
    input_hash = _cognitive_input_hash(
        domain_id, inputs=inputs, batch_lines=batch_lines, model_id=model_id,
        ku_total=ku_total, coverage=coverage)

    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO cognitive_maps (run_id, domain_id, lesson_id, schema_version, "
            " prompt_version, model_used, input_hash, structured_json, content_hash, "
            " knowledge_unit_total, knowledge_unit_processed, cognitive_input_coverage, "
            " batch_count, status, error, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now','localtime'), "
            " datetime('now','localtime')) "
            "ON CONFLICT(run_id) DO UPDATE SET model_used=excluded.model_used, "
            " input_hash=excluded.input_hash, structured_json=excluded.structured_json, "
            " content_hash=excluded.content_hash, "
            " knowledge_unit_total=excluded.knowledge_unit_total, "
            " knowledge_unit_processed=excluded.knowledge_unit_processed, "
            " cognitive_input_coverage=excluded.cognitive_input_coverage, "
            " batch_count=excluded.batch_count, status=excluded.status, error=excluded.error, "
            " updated_at=datetime('now','localtime')",
            (run_id, domain_id, lesson_id, SCHEMA_VERSION, COGNITIVE_PROMPT_VERSION,
             model_id, input_hash, blob, content_hash, ku_total, ku_processed, coverage,
             len(batch_lines), status.value, (error or "")[:1000]),
        )
        map_row = conn.execute(
            "SELECT id FROM cognitive_maps WHERE run_id=?", (run_id,)).fetchone()
        map_id = int(map_row["id"])
        _write_cognitive_children(conn, map_id, run_id, domain_id, items)
        _write_input_ledger(conn, run_id, map_id, domain_id, batch_lines)
    return {
        "enabled": True,
        "domain_id": domain_id,
        "status": status.value,
        "item_count": len(items),
        "by_type": by_type,
        "knowledge_unit_total": ku_total,
        "knowledge_unit_processed": ku_processed,
        "cognitive_input_coverage": coverage,
        "batch_count": len(batch_lines),
        "model_used": model_id,
        "error": error,
        "note": note,
        "availability": inputs.get("availability") or {},
        "budget": {k: v for k, v in (budget_info or {}).items() if k != "batches"},
        "ledger": {"consumed": sum(1 for b in batch_lines if b["status"] == LEDGER_CONSUMED),
                   "failed": sum(1 for b in batch_lines if b["status"] == LEDGER_FAILED),
                   "skipped": sum(1 for b in batch_lines if b["status"] == LEDGER_SKIPPED)},
    }


def _write_cognitive_children(conn, map_id: int, run_id: int, domain_id: int,
                              items: list[CognitiveItem]) -> None:
    """写认知项与来源/KU 关联（幂等：同 map 全量重写）。

    ``source_span_id`` 只在**本 domain**内解析，保证持久化的关联不会指向别的
    课堂的 span（缺陷 2 的持久化侧）。
    """
    conn.execute("DELETE FROM cognitive_items WHERE cognitive_map_id=?", (map_id,))
    span_by_source = {r["source_id"]: int(r["id"]) for r in db.fetch_all(
        "SELECT id, source_id FROM source_spans WHERE domain_id=?", (domain_id,))}
    for ordinal, item in enumerate(items, 1):
        cur = conn.execute(
            "INSERT INTO cognitive_items (cognitive_map_id, run_id, stable_key, item_type, "
            " title, explanation, severity, confidence, recommended_treatment, origin, "
            " status, ordinal, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, datetime('now','localtime'))",
            (map_id, run_id, item.stable_key, item.item_type.value, item.title,
             item.explanation, item.severity, item.confidence,
             item.recommended_treatment.value, item.origin.value, item.status.value,
             ordinal),
        )
        item_id = int(cur.lastrowid)
        for sid in item.source_refs:
            span_id = span_by_source.get(sid)
            if span_id is None:
                # 不属于本 domain 的 Source ID 绝不写入关联行（宁可缺失也不串域）。
                logger.warning("认知项 %s 的 Source ID %s 不属于 domain %s，跳过关联",
                               item.stable_key, sid, domain_id)
                continue
            conn.execute(
                "INSERT OR IGNORE INTO cognitive_item_sources (cognitive_item_id, "
                " source_span_id, source_id, relation, created_at) "
                "VALUES (?,?,?, 'supports', datetime('now','localtime'))",
                (item_id, span_id, sid),
            )
        for ku in item.knowledge_unit_refs:
            conn.execute(
                "INSERT OR IGNORE INTO cognitive_item_knowledge_units (cognitive_item_id, "
                " knowledge_unit_key, relation, created_at) "
                "VALUES (?,?, 'about', datetime('now','localtime'))",
                (item_id, ku),
            )


def _write_input_ledger(conn, run_id: int, map_id: int, domain_id: int,
                        batch_lines: list[dict]) -> None:
    """按**真实 batch 结果**写认知输入账本（缺陷 3）。

    早期实现为每个 KU 无条件写 ``consumed`` + ``batch_ordinal=1``，即使对应
    batch 已经失败 —— 于是同一 run 里 CognitiveMap 写着
    ``status=partial, coverage=0.0``，账本复算却是 ``consumed=N, coverage=1.0``。
    现在逐 batch 落真实序号、KU、模型、输入哈希与终态（consumed/failed），
    覆盖率只能由账本复算，不能与 map 冲突。
    """
    conn.execute("DELETE FROM cognitive_input_ledger WHERE run_id=?", (run_id,))
    for line in batch_lines:
        status = line.get("status") or LEDGER_FAILED
        reason = line.get("reason_code")
        if status == LEDGER_SKIPPED and not reason:
            # CHECK(status <> 'skipped' OR reason_code IS NOT NULL)：不得写非法行
            status = LEDGER_FAILED
            reason = reason or "skipped_without_reason"
        for ku_key in line.get("knowledge_unit_keys") or []:
            conn.execute(
                "INSERT OR IGNORE INTO cognitive_input_ledger (run_id, cognitive_map_id, "
                " knowledge_unit_key, batch_ordinal, input_hash, origin, status, reason_code, "
                " created_at) "
                "VALUES (?,?,?,?,?,?,?,?, datetime('now','localtime'))",
                (run_id, map_id, ku_key, int(line.get("batch_ordinal") or 1),
                 line.get("input_hash"), "lesson_understanding", status,
                 None if reason is None else str(reason)[:200]),
            )


def get_cognitive_map(run_id: int) -> Optional[dict]:
    row = db.fetch_one("SELECT * FROM cognitive_maps WHERE run_id=?", (run_id,))
    if not row:
        return None
    out = dict(row)
    try:
        out["structured"] = json.loads(out.get("structured_json") or "{}")
    except Exception:
        out["structured"] = {}
    out["items"] = [dict(r) for r in db.fetch_all(
        "SELECT ci.*, "
        " (SELECT GROUP_CONCAT(source_id) FROM cognitive_item_sources s "
        "  WHERE s.cognitive_item_id = ci.id) AS source_ids, "
        " (SELECT GROUP_CONCAT(knowledge_unit_key) FROM cognitive_item_knowledge_units k "
        "  WHERE k.cognitive_item_id = ci.id) AS knowledge_unit_keys "
        "FROM cognitive_items ci WHERE ci.cognitive_map_id=? ORDER BY ci.ordinal",
        (int(row["id"]),))]
    return out


def cognitive_input_coverage(run_id: int) -> dict[str, Any]:
    """复算认知输入覆盖率（确定性程序，不依赖模型自述）。"""
    rows = db.fetch_all(
        "SELECT status, COUNT(*) AS n FROM cognitive_input_ledger WHERE run_id=? "
        "GROUP BY status", (run_id,))
    counts = {r["status"]: int(r["n"]) for r in rows}
    consumed = counts.get("consumed", 0)
    skipped = counts.get("skipped", 0)
    failed = counts.get("failed", 0)
    total = consumed + skipped + failed
    return {"total": total, "consumed": consumed, "skipped": skipped, "failed": failed,
            "coverage": round(consumed / total, 6) if total else 1.0}
