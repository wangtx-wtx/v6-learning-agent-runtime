"""Material Domain 冻结、归属校验与 Source Map 构建（V6 Phase 1）。

职责（任务书 §六）:

* 根据 run/course/chapter/lesson/material_ids/transcript 冻结 Material Domain
* 材料归属校验（不同课程材料混入必须拒绝）
* ``domain_hash``（确定性、可复现）
* 幂等重入（同一 run 重入返回同一 frozen domain；范围变化 → 新 version）
* frozen 后禁止静默移除成员
* transcript_only 输入建立可追踪的 domain item

所有写入都落在 0016 / 0017 新表，**不修改** materials / source_chunks / notes。
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Optional

from .. import database as db
from . import config as engine_config
from .contracts import (
    SCHEMA_VERSION,
    DomainItemState,
    DomainScope,
    DomainState,
    ItemReason,
    MaterialDomainSnapshot,
    SourceKind,
    SourceSpan,
    SpanReason,
    SpanState,
)
from .deduplicate import (
    detect_item_duplicate,
    item_duplicate_key,
    register_span_canonical,
    span_duplicate_verdict,
)
from .normalize import (
    classify_noise,
    hash_text,
    infer_source_kind,
    is_supported_kind,
    looks_like_parse_marker,
    normalize_text,
    parse_locator,
    parse_page_locator,
    transcript_to_located_texts,
)
from .source_map import (
    SourceIdRequest,
    allocate_terminal_source_id,
    assign_source_ids,
)

logger = logging.getLogger(__name__)

#: 域内哈希口径版本：变更哈希输入时必须同步递增，避免历史域被误判。
DOMAIN_HASH_ALGO = "sha256+domain-v1"

#: 跨 run 复用 scope 哈希口径版本（不含 run_id）。
REUSE_SCOPE_HASH_ALGO = "sha256+reuse-scope-v1"


class DomainError(RuntimeError):
    """Material Domain 业务错误（归属错误、冻结违规等）。"""


class DomainScopeMismatchError(DomainError):
    """材料归属与本次课堂任务范围不一致 —— 直接失败，不做静默剔除。"""


class FrozenDomainMutationError(DomainError):
    """试图修改已冻结 domain 的成员集合。"""


class DomainHashConflictError(DomainError):
    """同一 run 的 domain 已存在且材料集合与本次请求不一致。"""


class SourceMapConflictError(DomainError):
    """Source Map 构建冲突（重复 span 行等）。"""


# ---------------------------------------------------------------------------
# 归属校验
# ---------------------------------------------------------------------------
def _material_rows(material_ids: list[int]) -> list[dict]:
    if not material_ids:
        return []
    placeholders = ",".join("?" * len(material_ids))
    rows = db.fetch_all(
        f"SELECT id, course_id, chapter_id, lesson_id, file_path, type, kind, name, "
        f"display_name, mime, sha256, size_bytes, parser_status, status, parse_error "
        f"FROM materials WHERE id IN ({placeholders})",
        tuple(material_ids),
    )
    found = {int(r["id"]): r for r in rows}
    missing = [mid for mid in material_ids if mid not in found]
    if missing:
        raise DomainError(f"material_ids 不存在: {missing}")
    # 保持调用方给出的顺序（那是用户的选择顺序，也是 ordinal 的来源）
    return [found[mid] for mid in material_ids]


def assert_material_in_scope(row: dict, *, course_id: Optional[int], chapter_id: Optional[int],
                             lesson_id: Optional[int]) -> None:
    """材料归属校验。

    只有调用方**显式声明**了某个范围维度时才校验该维度；未声明（``None``）表示
    该维度不受限。任一已声明维度不匹配即 ``DomainScopeMismatchError``：
    不做静默剔除，也不偷偷缩小 domain。
    """
    mid = row.get("id")
    if course_id is not None and row.get("course_id") is not None \
            and int(row["course_id"]) != int(course_id):
        raise DomainScopeMismatchError(
            f"材料 {mid} 属于课程 {row['course_id']}，不属于课程 {course_id}"
        )
    if chapter_id is not None and row.get("chapter_id") is not None \
            and int(row["chapter_id"]) != int(chapter_id):
        raise DomainScopeMismatchError(
            f"材料 {mid} 属于章节 {row['chapter_id']}，不属于章节 {chapter_id}"
        )
    if lesson_id is not None and row.get("lesson_id") is not None \
            and int(row["lesson_id"]) != int(lesson_id):
        raise DomainScopeMismatchError(
            f"材料 {mid} 属于课时 {row['lesson_id']}，不属于课时 {lesson_id}"
        )


# ---------------------------------------------------------------------------
# domain_hash
# ---------------------------------------------------------------------------
def compute_domain_hash(*, run_id: int, scope: str, course_id: Optional[int],
                        chapter_id: Optional[int], lesson_id: Optional[int],
                        version: int, members: list[dict]) -> str:
    """确定性 domain 哈希。

    输入只包含「范围 + 版本 + 成员身份与内容哈希」，按 ``(material_id, label)``
    排序后序列化，因此与调用顺序、并发、时间无关。
    """
    normalized_members = sorted(
        (
            {
                "material_id": m.get("material_id"),
                "label": m.get("label") or "",
                "content_hash": m.get("content_hash") or "",
                "required": bool(m.get("required", True)),
            }
            for m in members
        ),
        key=lambda m: (m["material_id"] if m["material_id"] is not None else -1, m["label"]),
    )
    payload = {
        "algo": DOMAIN_HASH_ALGO,
        "run_id": int(run_id),
        "scope": scope,
        "course_id": course_id,
        "chapter_id": chapter_id,
        "lesson_id": lesson_id,
        "version": int(version),
        "members": normalized_members,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def compute_reuse_scope_hash(*, scope: str, course_id: Optional[int],
                             chapter_id: Optional[int], lesson_id: Optional[int],
                             members: list[dict]) -> str:
    """**不含 run_id** 的内容域哈希，专供跨 run 复用判定。

    ``domain_hash`` 含 run_id，两个 run 即使材料完全相同也不会相等，因此不能
    作为跨 run 内容相等依据。本函数只包含「内容范围 + 成员内容哈希」，因此
    child run「输入完全一致」时能与父 run 命中同一 reuse scope。

    刻意**不含** version：显式重试产生 child run 时 version 可能递增，但材料
    内容未变仍应允许复用。材料变化会改变 ``content_hash``，从而改变本哈希。
    """
    normalized_members = sorted(
        (
            {
                "material_id": m.get("material_id"),
                "label": m.get("label") or "",
                "content_hash": m.get("content_hash") or "",
                "required": bool(m.get("required", True)),
            }
            for m in members
        ),
        key=lambda m: (m["material_id"] if m["material_id"] is not None else -1, m["label"]),
    )
    payload = {
        "algo": REUSE_SCOPE_HASH_ALGO,
        "scope": scope,
        "course_id": course_id,
        "chapter_id": chapter_id,
        "lesson_id": lesson_id,
        "members": normalized_members,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 域构建
# ---------------------------------------------------------------------------
def _content_hash_of(row: dict) -> Optional[str]:
    """材料**规范化内容**哈希（域内去重键）。

    刻意不使用 ``materials.sha256``：那是 blob 字节哈希，同一份内容被复制成
    两个文件（文件名/路径不同）时字节哈希相同、但反过来「同一份讲义改名重新
    上传」也可能字节相同 —— 真正要判定的是**内容是否等价**，因此统一按
    规范化文本哈希对齐 chunk 级判定口径。

    文件不可读时不返回伪造哈希（``None`` 表示"未知"，不参与去重）。
    """
    path = row.get("file_path")
    if not path:
        return None
    p = Path(str(path))
    if not p.exists() or not p.is_file():
        return None
    try:
        return hash_text(normalize_text(p.read_text(encoding="utf-8", errors="ignore")))
    except Exception as e:  # 二进制 / 权限：不伪造哈希
        logger.debug("材料 %s 内容哈希失败: %s", row.get("id"), e)
        return None


def _label_of(row: dict) -> str:
    return str(row.get("display_name") or row.get("name") or f"material-{row.get('id')}")


def _item_state_for(row: dict, source_kind: SourceKind) -> tuple[DomainItemState, Optional[ItemReason], Optional[str]]:
    """按材料当前状态给出显式终态。禁止「解析失败但当作 included」。"""
    parser_status = str(row.get("parser_status") or row.get("status") or "").lower()
    if looks_like_parse_marker(row.get("parse_error")):
        return DomainItemState.FAILED, ItemReason.PARSE_FAILED, str(row.get("parse_error"))[:200]
    if parser_status in ("failed",):
        return DomainItemState.FAILED, ItemReason.PARSE_FAILED, str(row.get("parse_error") or "parser_status=failed")[:200]
    if parser_status in ("needs_ocr", "transcribing"):
        return (DomainItemState.UNSUPPORTED, ItemReason.UNSUPPORTED_PARSE_STATE,
                f"parser_status={parser_status}（Phase 1 尚无 OCR / 转写产物）")
    supported, reason = is_supported_kind(source_kind)
    if not supported:
        return DomainItemState.UNSUPPORTED, reason, f"source_kind={source_kind.value}"
    if parser_status == "parsing":
        return DomainItemState.INCLUDED, None, None  # 解析中进行中，由 parse 节点产生 span
    return DomainItemState.INCLUDED, None, None


def _build_members(rows: list[dict], transcript_text: str) -> list[dict]:
    """构造 domain 成员集合（含 item 级去重）。

    抽成独立函数，使 ``freeze_material_domain`` 能在**落库之前**先算出预期
    ``domain_hash`` 并与既有冻结域比较（缺陷 2 的幂等重入校验）。
    """
    members: list[dict] = []
    seen_items: dict[str, int] = {}
    seen_material_ids: set[int] = set()
    ordinal = 0
    for row in rows:
        material_id = int(row["id"])
        if material_id in seen_material_ids:
            # 调用方把同一 material_id 传了多次：域内唯一，直接跳过重复引用
            # （数据库层还有 UNIQUE(domain_id, material_id) 兜底）。
            logger.info("material %s 在 material_ids 中重复出现，域内只保留一次", material_id)
            continue
        seen_material_ids.add(material_id)
        ordinal += 1
        source_kind = infer_source_kind(row.get("kind"), row.get("mime"),
                                        row.get("name") or row.get("file_path"))
        state, reason, detail = _item_state_for(row, source_kind)
        content_hash = _content_hash_of(row)
        raw_path = row.get("file_path")
        raw_chars = 0
        if raw_path and Path(str(raw_path)).exists():
            try:
                raw_chars = len(Path(str(raw_path)).read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                raw_chars = 0
        item = {
            "material_id": material_id,
            "label": _label_of(row),
            "source_kind": source_kind.value,
            "locator": row.get("file_path"),
            "ordinal": ordinal,
            "required": True,
            "raw_chars": raw_chars,
            "content_hash": content_hash,
            "state": state,
            "reason_code": reason.value if reason else None,
            "reason_detail": detail,
            "canonical_ordinal": None,
        }
        if state == DomainItemState.INCLUDED:
            verdict, canonical_ordinal = detect_item_duplicate(
                content_hash, content_hash, source_kind.value, seen_items
            )
            if verdict.is_duplicate:
                item["state"] = DomainItemState.DUPLICATE
                item["reason_code"] = verdict.reason_code
                item["reason_detail"] = verdict.detail
                # 此阶段 seen_items 的值是 canonical **ordinal**；落库时映射为 item_id。
                item["canonical_ordinal"] = canonical_ordinal
            else:
                # 关键：必须在**循环内**登记 canonical，否则同一循环后续成员
                # 永远命中不到去重索引（早期实现把登记放到落库阶段，导致域内
                # 重复材料从未被判为 duplicate）。
                key = item_duplicate_key(content_hash, content_hash, source_kind.value)
                if key is not None:
                    seen_items[key] = ordinal
        members.append(item)

    if transcript_text.strip():
        ordinal += 1
        t_hash = hash_text(normalize_text(transcript_text))
        t_item = {
            "material_id": None,
            "label": "inline-transcript",
            "source_kind": SourceKind.TRANSCRIPT.value,
            "locator": "transcript:inline",
            "ordinal": ordinal,
            "required": True,
            "raw_chars": len(transcript_text),
            "content_hash": t_hash,
            "state": DomainItemState.INCLUDED,
            "reason_code": None,
            "reason_detail": None,
            "canonical_ordinal": None,
        }
        # inline 转写与某个材料内容完全相同时（常见：转写既上传成文件又粘贴），
        # 域内只保留一个 canonical 表示，另一个记为 duplicate 但仍保留账目。
        verdict, canonical_ordinal = detect_item_duplicate(
            t_hash, t_hash, SourceKind.TRANSCRIPT.value, seen_items)
        if verdict.is_duplicate:
            t_item["state"] = DomainItemState.DUPLICATE
            t_item["reason_code"] = verdict.reason_code
            t_item["reason_detail"] = verdict.detail
            t_item["canonical_ordinal"] = canonical_ordinal
        else:
            key = item_duplicate_key(t_hash, t_hash, SourceKind.TRANSCRIPT.value)
            if key is not None:
                seen_items[key] = ordinal
        members.append(t_item)
    return members


# ---------------------------------------------------------------------------
# ItemReason → SpanReason 显式映射（缺陷 3）
# ---------------------------------------------------------------------------
#: 材料级终态原因 → span 级受控原因。
#: **不得**再用 ``SpanReason(item_reason_string)`` 直接转换：两个枚举取值集不同
#: （``parse_failed`` / ``unsupported_parse_state`` / ``material_missing`` 等都不是
#: 合法 SpanReason），直接转换会抛 ValueError，导致解析失败的材料在
#: build_source_map 阶段崩溃、连覆盖报告都写不出来。
ITEM_REASON_TO_SPAN_REASON: dict[str, SpanReason] = {
    ItemReason.DUPLICATE_CONTENT_HASH.value: SpanReason.DUPLICATE_CONTENT_HASH,
    ItemReason.UNSUPPORTED_FILE_TYPE.value: SpanReason.UNSUPPORTED_FILE_TYPE,
    ItemReason.UNSUPPORTED_PARSE_STATE.value: SpanReason.UNSUPPORTED_FILE_TYPE,
    ItemReason.MATERIAL_MISSING.value: SpanReason.FAILED_PARSE,
    ItemReason.MATERIAL_FILE_MISSING.value: SpanReason.FAILED_PARSE,
    ItemReason.MATERIAL_NOT_IN_SCOPE.value: SpanReason.EXCLUDED_UNASSIGNED,
    ItemReason.PARSE_FAILED.value: SpanReason.FAILED_PARSE,
    ItemReason.PARSE_EMPTY.value: SpanReason.FAILED_PARSE,
    ItemReason.TRANSCRIPT_EMPTY.value: SpanReason.NOISE_EMPTY_TEXT,
    ItemReason.OPTIONAL_NOT_SELECTED.value: SpanReason.EXCLUDED_UNASSIGNED,
    ItemReason.REQUIRED_BUT_EXCLUDED.value: SpanReason.EXCLUDED_UNASSIGNED,
}

#: span 级终态 → span 级受控原因（item 状态非 included/duplicate 时使用）。
ITEM_STATE_TO_SPAN_STATE: dict[str, SpanState] = {
    DomainItemState.UNSUPPORTED.value: SpanState.UNSUPPORTED,
    DomainItemState.FAILED.value: SpanState.FAILED,
    DomainItemState.EXCLUDED_WITH_REASON.value: SpanState.EXCLUDED_WITH_REASON,
}


def _as_span_reason(value: Any) -> Optional[SpanReason]:
    """把候选 span 的 ``reason`` 统一收敛为 ``SpanReason``。

    候选里的值可能是 ``SpanReason``（来自映射表）或字符串（来自去重判定）。
    字符串一律通过显式名称查找，失败则回退 ``failed_parse`` —— 绝不因枚举
    转换失败让整张 Source Map 崩掉。
    """
    if value is None:
        return None
    if isinstance(value, SpanReason):
        return value
    try:
        return SpanReason(str(value))
    except ValueError:
        return map_item_reason_to_span_reason(str(value))


def map_item_reason_to_span_reason(reason_code: Optional[str]) -> Optional[SpanReason]:
    """把材料级 reason_code 映射为 span 级受控原因。

    未知值**不猜测、不崩溃**：回退到 ``failed_parse``，保证「解析失败也必须
    留下可审计 span」这一不变量不被枚举差异破坏。
    """
    if not reason_code:
        return None
    mapped = ITEM_REASON_TO_SPAN_REASON.get(str(reason_code))
    if mapped is not None:
        return mapped
    logger.warning("未知 item reason_code=%r，按 failed_parse 记账", reason_code)
    return SpanReason.FAILED_PARSE


def freeze_material_domain(
    *,
    run_id: int,
    course_id: Optional[int] = None,
    chapter_id: Optional[int] = None,
    lesson_id: Optional[int] = None,
    material_ids: Optional[list[int]] = None,
    transcript: Optional[str] = None,
    scope: str = DomainScope.LESSON.value,
    version: int = 1,
) -> dict[str, Any]:
    """冻结并持久化 Material Domain。

    幂等：同一 run 重入时，若成员集合哈希一致则直接返回既有 frozen domain；
    若不一致则 ``DomainHashConflictError``（范围变化必须显式新建 version/新 run）。
    """
    if scope not in tuple(s.value for s in DomainScope):
        raise DomainError(f"非法 scope: {scope}")
    material_ids = [int(m) for m in (material_ids or [])]
    transcript_text = transcript or ""
    if not material_ids and not transcript_text.strip():
        raise DomainError("材料域为空：需要至少一个 material_ids 或 transcript")

    existing = get_domain_for_run(run_id)
    rows = _material_rows(material_ids)
    for row in rows:
        assert_material_in_scope(row, course_id=course_id, chapter_id=chapter_id,
                                 lesson_id=lesson_id)

    members = _build_members(rows, transcript_text)
    domain_hash = compute_domain_hash(
        run_id=run_id, scope=scope, course_id=course_id, chapter_id=chapter_id,
        lesson_id=lesson_id, version=version, members=members,
    )
    reuse_scope_hash = compute_reuse_scope_hash(
        scope=scope, course_id=course_id, chapter_id=chapter_id,
        lesson_id=lesson_id, members=members,
    )

    if existing is not None:
        # 幂等重入校验（缺陷 2）：必须比较**重新计算的预期哈希**，而不是只看
        # "已有 domain 就返回"。否则同一 run 换材料/换转写/换范围时会静默沿用
        # 旧域 —— 那正是「材料被静默替换」这一类 silent drop 的温床。
        if existing["domain_hash"] != domain_hash:
            raise DomainHashConflictError(
                f"run {run_id} 已冻结的 Material Domain 与本次请求不一致：\n"
                f"  已冻结 domain_id={existing['id']} hash={existing['domain_hash']}\n"
                f"  本次请求             hash={domain_hash}\n"
                "材料集合 / 转写 / scope / course-chapter-lesson / version 任一变化都必须"
                "显式新建 run 或新建 version，不得原地替换冻结域成员。"
            )
        return existing

    # ---- 原子落库 ----
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO material_domains (run_id, scope, course_id, chapter_id, lesson_id, "
            " version, schema_version, domain_hash, state, engine_version, transcript_chars, "
            " frozen_at, created_at, reuse_scope_hash) "
            "VALUES (?,?,?,?,?,?,?,?, 'frozen', ?, ?, datetime('now','localtime'), "
            " datetime('now','localtime'), ?)",
            (run_id, scope, course_id, chapter_id, lesson_id, version, SCHEMA_VERSION,
             domain_hash, engine_config.engine_version(), len(transcript_text),
             reuse_scope_hash),
        )
        domain_id = int(cur.lastrowid)
        ordinal_to_item_id: dict[int, int] = {}
        for member in members:
            # duplicate 的 duplicate_of_item_id 需要 canonical 的 item_id，而它在
            # 同一批插入里才产生。因此先按 included 落库，再在下面原子地改为
            # duplicate + duplicate_of_item_id —— 满足 CHECK
            # (state <> 'duplicate' OR duplicate_of_item_id IS NOT NULL)，避免
            # 出现"短暂非法中间态"。
            insert_state = ("included" if member["state"] == DomainItemState.DUPLICATE.value
                            else member["state"])
            insert_reason = (None if member["state"] == DomainItemState.DUPLICATE.value
                             else member["reason_code"])
            cur = conn.execute(
                "INSERT INTO material_domain_items (domain_id, material_id, source_kind, locator, "
                " ordinal, required, raw_chars, raw_tokens, content_hash, state, reason_code, "
                " reason_detail, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, datetime('now','localtime'))",
                (domain_id, member["material_id"], member["source_kind"], member["locator"],
                 member["ordinal"], 1 if member["required"] else 0, member["raw_chars"],
                 member["raw_chars"], member["content_hash"], insert_state,
                 insert_reason, member["reason_detail"]),
            )
            ordinal_to_item_id[member["ordinal"]] = int(cur.lastrowid)
        # 回填 duplicate（state + duplicate_of_item_id 同一条 UPDATE 写入）
        for member in members:
            if member["state"] != DomainItemState.DUPLICATE.value:
                continue
            canonical_item_id = ordinal_to_item_id.get(member.get("canonical_ordinal"))
            if canonical_item_id is None:
                raise DomainError(
                    f"duplicate item (ordinal={member['ordinal']}) 找不到 canonical item"
                )
            conn.execute(
                "UPDATE material_domain_items SET state='duplicate', reason_code=?, "
                " duplicate_of_item_id=? WHERE id=?",
                (member["reason_code"], canonical_item_id,
                 ordinal_to_item_id[member["ordinal"]]),
            )

    snapshot = get_domain_for_run(run_id)
    if snapshot is None:  # pragma: no cover - 事务成功即应可读
        raise DomainError(f"domain 写入后不可读: run_id={run_id}")
    if snapshot["domain_hash"] != domain_hash:
        raise DomainHashConflictError(
            f"run {run_id} 的 domain 哈希与本次请求不一致："
            f"已有 {snapshot['domain_hash']}，请求 {domain_hash}"
        )
    return snapshot


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
def get_domain_for_run(run_id: int) -> Optional[dict]:
    """读取 run 的 domain 快照（含 items）。不存在返回 ``None``。"""
    domain = db.fetch_one("SELECT * FROM material_domains WHERE run_id=?", (run_id,))
    if not domain:
        return None
    domain = dict(domain)
    domain["items"] = get_domain_items(int(domain["id"]))
    return domain


def get_domain(domain_id: int) -> Optional[dict]:
    domain = db.fetch_one("SELECT * FROM material_domains WHERE id=?", (domain_id,))
    if not domain:
        return None
    domain = dict(domain)
    domain["items"] = get_domain_items(domain_id)
    return domain


def get_domain_items(domain_id: int) -> list[dict]:
    rows = db.fetch_all(
        "SELECT * FROM material_domain_items WHERE domain_id=? ORDER BY ordinal, id",
        (domain_id,),
    )
    return [dict(r) for r in rows]


def get_domain_item(domain_item_id: int) -> Optional[dict]:
    row = db.fetch_one("SELECT * FROM material_domain_items WHERE id=?", (domain_item_id,))
    return dict(row) if row else None


def domain_is_frozen(domain_id: int) -> bool:
    row = db.fetch_one("SELECT state FROM material_domains WHERE id=?", (domain_id,))
    return bool(row and row.get("state") == DomainState.FROZEN.value)


def remove_domain_item(domain_item_id: int) -> None:
    """冻结保护：frozen domain 的成员不允许移除。

    Phase 1 不提供任何合法移除路径 —— 范围变化必须新建 version（新 domain 行）。
    该函数存在的意义是让「静默移除」成为一个显式失败点，而不是一句注释。
    """
    item = get_domain_item(domain_item_id)
    if not item:
        raise DomainError(f"domain item {domain_item_id} 不存在")
    if domain_is_frozen(int(item["domain_id"])):
        raise FrozenDomainMutationError(
            f"domain {item['domain_id']} 已冻结，禁止移除成员 {domain_item_id}；"
            "范围变化必须新建 version"
        )
    db.execute("DELETE FROM material_domain_items WHERE id=?", (domain_item_id,))


# ---------------------------------------------------------------------------
# Source Map 构建
# ---------------------------------------------------------------------------
def _chunks_for_material(material_id: int) -> list[dict]:
    """读取某材料的 source_chunks（附带 materials.parser_status 供诊断）。"""
    return [dict(r) for r in db.fetch_all(
        "SELECT c.id, c.material_id, c.lesson_id, c.chapter_id, c.course_id, "
        " c.type, c.locator, c.text, m.parser_status "
        "FROM source_chunks c LEFT JOIN materials m ON m.id = c.material_id "
        "WHERE c.material_id=? ORDER BY c.id",
        (material_id,),
    )]


def _insert_span(conn, span: SourceSpan) -> int:
    cur = conn.execute(
        "INSERT INTO source_spans (source_id, domain_id, domain_item_id, source_chunk_id, "
        " material_id, source_kind, locator, ordinal, start_ms, end_ms, page_no, slide_no, "
        " text, normalized_text_hash, token_count, char_count, canonical_span_id, span_state, "
        " reason_code, reason_detail, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now','localtime'))",
        (span.source_id, span.domain_id, span.domain_item_id, span.source_chunk_id,
         span.material_id, span.source_kind.value, span.locator, span.ordinal, span.start_ms,
         span.end_ms, span.page_no, span.slide_no, span.text, span.normalized_text_hash,
         span.token_count, span.char_count, span.canonical_span_id, span.span_state.value,
         span.reason_code.value if span.reason_code else None, span.reason_detail),
    )
    return int(cur.lastrowid)


def build_source_map(domain_id: int, *, covered_span_ids: Optional[set[int]] = None) -> dict[str, Any]:
    """为 domain 建立稳定 Source Map。

    ``covered_span_ids`` 为 ``None`` 时全部 canonical span 视为已建图。显式传入
    集合时，落在集合外的 canonical span 记为 ``excluded_with_reason`` /
    ``excluded_unassigned`` —— 这是**唯一**允许制造「未建图 span」的入口，
    用于证伪测试与故障注入，正常流程不得传参。

    幂等：同一 domain 已有 span 且数量与本次一致时直接返回既有结果。
    """
    from .normalize import estimate_tokens

    domain = get_domain(domain_id)
    if not domain:
        raise DomainError(f"domain {domain_id} 不存在")
    if domain.get("state") != DomainState.FROZEN.value:
        raise DomainError(f"domain {domain_id} 尚未冻结，拒绝建图")

    existing = db.fetch_all("SELECT id FROM source_spans WHERE domain_id=?", (domain_id,))
    if existing:
        return _source_map_summary(domain_id, already_present=True)

    items = domain["items"]

    # ---- 1. 规划候选 span（在写库前先完成 Source ID 分配，保证稳定性） ----
    candidates: list[dict] = []
    ordinal = 0
    for item in items:
        item_id = int(item["id"])
        item_state = item["state"]
        source_kind = SourceKind(item["source_kind"])
        material_id = item.get("material_id")
        if item_state in (DomainItemState.UNSUPPORTED.value, DomainItemState.FAILED.value,
                          DomainItemState.EXCLUDED_WITH_REASON.value):
            # 材料级终态：记一条占位 span，保证账目在 span 维度也可见。
            # 缺陷 3 修复：reason_code 必须经**显式映射**转换，不能直接构造
            # SpanReason（枚举取值集不同会抛 ValueError 并让整张 Source Map 崩掉）。
            ordinal += 1
            candidates.append({
                "domain_item_id": item_id, "material_id": material_id,
                "source_kind": source_kind, "source_chunk_id": None,
                "locator": item.get("locator"), "ordinal": ordinal,
                "text": "", "raw_text": "", "page_no": None, "slide_no": None,
                "state": ITEM_STATE_TO_SPAN_STATE.get(item_state, SpanState.EXCLUDED_WITH_REASON),
                "reason": map_item_reason_to_span_reason(item.get("reason_code")),
                "detail": item.get("reason_detail"),
            })
            continue

        if material_id is None:
            # transcript_only：inline 转写按 cue 建立**多个**有时间的 span（缺陷 6）。
            # 早期实现把整段 inline 转写压成一个无法对应真实消费情况的大 span，
            # 且没有 source_chunk，导致 required_items_without_canonical_span>0。
            # 现在把 inline 转写也物化成 source_chunks（与文件转写同一函数、
            # 同一 locator 口径），span 与 chunk 一一对应。
            text = _inline_transcript_text(int(domain["run_id"]))
            chunk_rows = _materialize_inline_transcript(domain, text)
            if not chunk_rows:
                ordinal += 1
                candidates.append({
                    "domain_item_id": item_id, "material_id": None, "source_kind": source_kind,
                    "source_chunk_id": None, "locator": "transcript:inline",
                    "ordinal": ordinal, "text": text, "raw_text": text,
                    "page_no": None, "slide_no": None, "state": None, "reason": None,
                    "detail": None, "_start_ms": None, "_end_ms": None,
                })
                continue
            for chunk in chunk_rows:
                ordinal += 1
                info = parse_locator(chunk.get("locator"), source_kind)
                candidates.append({
                    "domain_item_id": item_id, "material_id": None, "source_kind": source_kind,
                    "source_chunk_id": int(chunk["id"]),
                    "locator": info.normalized or chunk.get("locator"),
                    "ordinal": ordinal, "text": chunk.get("text") or "",
                    "raw_text": chunk.get("text") or "",
                    "page_no": None, "slide_no": None, "state": None, "reason": None,
                    "detail": None, "_start_ms": info.start_ms, "_end_ms": info.end_ms,
                })
            continue

        chunks = _chunks_for_material(int(material_id))
        if not chunks:
            state = SpanState.EXCLUDED_WITH_REASON
            reason = SpanReason.FAILED_PARSE
            detail = "材料无可用的 source_chunks（解析未产出内容）"
            mrow = db.fetch_one("SELECT parser_status, parse_error FROM materials WHERE id=?",
                                (int(material_id),))
            if mrow:
                pstatus = str(mrow.get("parser_status") or "").lower()
                if pstatus == "parsing":
                    detail = "材料仍在解析中，无可用 chunk"
                elif pstatus == "failed":
                    state = SpanState.FAILED
                    reason = map_item_reason_to_span_reason(
                        ItemReason.PARSE_FAILED.value) or SpanReason.FAILED_PARSE
                    detail = f"材料解析失败：{str(mrow.get('parse_error') or '')[:200]}"
                elif pstatus in ("needs_ocr", "transcribing"):
                    state = SpanState.UNSUPPORTED
                    reason = SpanReason.UNSUPPORTED_FILE_TYPE
                    detail = f"parser_status={pstatus}（无可用文本产物）"
                elif pstatus == "ready":
                    state = SpanState.EXCLUDED_WITH_REASON
                    reason = map_item_reason_to_span_reason(
                        ItemReason.PARSE_EMPTY.value) or SpanReason.FAILED_PARSE
                    detail = "材料标记 ready 但没有任何 source_chunk（空材料）"
            ordinal += 1
            candidates.append({
                "domain_item_id": item_id, "material_id": int(material_id),
                "source_kind": source_kind, "source_chunk_id": None,
                "locator": item.get("locator"), "ordinal": ordinal, "text": "",
                "raw_text": "", "page_no": None, "slide_no": None,
                "state": state, "reason": reason, "detail": detail,
            })
            continue

        for chunk in chunks:
            ordinal += 1
            info = parse_locator(chunk.get("locator"), source_kind)
            page_no, slide_no = info.page_no, info.slide_no
            # source_chunks.locator 未必是 page:/slide: 形态（material_parser 的
            # PPT 写 slide:N，PDF 写 page:N）。可解析出页码时按页编号得到
            # P0017.01 / D0017.01；解析不出则退回来源顺序号（绝不伪造页码）。
            if page_no is None and slide_no is None:
                fallback_page, fallback_slide, _kind = parse_page_locator(chunk.get("locator"))
                page_no, slide_no = fallback_page, fallback_slide
            candidates.append({
                "domain_item_id": item_id, "material_id": int(material_id),
                "source_kind": source_kind, "source_chunk_id": int(chunk["id"]),
                "locator": info.normalized or chunk.get("locator"),
                "ordinal": ordinal, "text": chunk.get("text") or "",
                "raw_text": chunk.get("text") or "",
                "page_no": page_no, "slide_no": slide_no,
                "state": None, "reason": None, "detail": None,
                "_start_ms": info.start_ms, "_end_ms": info.end_ms,
            })

    requests = [
        SourceIdRequest(ordinal=c["ordinal"], source_kind=c["source_kind"],
                        page_no=c.get("page_no"), slide_no=c.get("slide_no"))
        for c in candidates
        if c.get("state") is None
    ]
    source_ids = assign_source_ids(requests)
    # 材料级终态占位 span 没有页码/时间轴：单独按来源前缀顺序编号，
    # 避免落到无来源语义的兜底前缀（失败 PDF 应为 D000001 而不是 S000001）。
    used_ids = set(source_ids.values())
    for cand in candidates:
        if cand.get("state") is None:
            continue
        source_ids[cand["ordinal"]] = allocate_terminal_source_id(cand["source_kind"], used_ids)

    # ---- 2. 计算噪声 / 重复 / 未建图 终态 ----
    seen_spans: dict[str, int] = {}
    for cand in candidates:
        source_id = source_ids[cand["ordinal"]]
        cand["source_id"] = source_id
        if cand["state"] is not None:
            continue
        normalized = normalize_text(cand["text"])
        if looks_like_parse_marker(normalized):
            cand["state"] = SpanState.NOISE
            cand["reason"] = SpanReason.NOISE_PARSE_MARKER.value
            cand["detail"] = "材料解析失败标记，非真实内容"
            continue
        verdict = classify_noise(cand["text"], normalized)
        if verdict.is_noise:
            cand["state"] = SpanState.NOISE
            cand["reason"] = verdict.reason_code.value if verdict.reason_code else SpanReason.NOISE_EMPTY_TEXT.value
            cand["detail"] = verdict.detail
            continue
        if covered_span_ids is not None and cand["ordinal"] not in covered_span_ids:
            # 故障注入 / 异常路径：显式记账为未建图，绝不静默丢弃。
            cand["state"] = SpanState.EXCLUDED_WITH_REASON
            cand["reason"] = SpanReason.EXCLUDED_UNASSIGNED.value
            cand["detail"] = "未纳入 source map（excluded_span_ids 注入）"
            continue
        cand["state"] = SpanState.INCLUDED

    # 重复 span 判定（同类来源、保守）：只对 included 且非噪声的 span 生效。
    # 必须在**同一循环内**先判定再登记 canonical；登记的值此阶段是 span ordinal，
    # 落库后再统一映射为真实 span_id。
    seen_spans: dict[str, int] = {}
    for cand in candidates:
        if cand["state"] != SpanState.INCLUDED:
            continue
        normalized = normalize_text(cand["text"])
        # 同一句话在课堂不同时间出现是两个独立证据位置，不是可合并的重复
        # chunk。整份重复上传已由 item 级内容哈希处理；span 级去重只用于
        # PPT/PDF 等解析重叠，不能吞掉转写时间轴或字符覆盖。
        if cand["source_kind"] == SourceKind.TRANSCRIPT:
            cand["canonical_ordinal"] = None
            continue
        verdict, canonical_ordinal = span_duplicate_verdict(
            cand["text"], normalized, cand["source_kind"].value, seen_spans
        )
        if verdict.is_duplicate:
            cand["state"] = SpanState.DUPLICATE
            cand["reason"] = verdict.reason_code
            cand["detail"] = verdict.detail
            cand["canonical_ordinal"] = canonical_ordinal
        else:
            cand["canonical_ordinal"] = None
            seen_spans = register_span_canonical(seen_spans, normalized,
                                                 cand["source_kind"].value, cand["ordinal"])

    # ---- 3. 落库（先插入全部 span，再回填 duplicate 的 canonical_span_id） ----
    with db.transaction() as conn:
        # 第一遍：所有 span 先以 canonical_span_id=NULL 写入（含 duplicate）
        for cand in candidates:
            normalized = normalize_text(cand["text"])
            # 与 domain item 同理：duplicate 的 canonical_span_id 要等插入后才能
            # 确定，因此先按 included 落库，第二遍再原子改为 duplicate + canonical。
            pending_duplicate = cand["state"] == SpanState.DUPLICATE
            span = SourceSpan(
                source_id=cand["source_id"],
                domain_id=domain_id,
                domain_item_id=cand["domain_item_id"],
                source_chunk_id=cand["source_chunk_id"],
                material_id=cand["material_id"],
                source_kind=cand["source_kind"],
                locator=cand["locator"],
                ordinal=cand["ordinal"],
                start_ms=cand.get("_start_ms"),
                end_ms=cand.get("_end_ms"),
                page_no=cand["page_no"],
                slide_no=cand["slide_no"],
                text=normalized,
                normalized_text_hash=hash_text(normalized),
                token_count=estimate_tokens(normalized, normalized),
                char_count=len(normalized),
                span_state=(SpanState.INCLUDED if pending_duplicate else cand["state"]),
                reason_code=(None if pending_duplicate else _as_span_reason(cand["reason"])),
                reason_detail=cand["detail"],
            )
            span_id = _insert_span(conn, span)
            cand["span_id"] = span_id
        # 第二遍：把构建阶段记录的 canonical **ordinal** 映射为真实 span_id 并回填。
        ordinal_to_span_id = {c["ordinal"]: c["span_id"] for c in candidates}
        for cand in candidates:
            if cand["state"] != SpanState.DUPLICATE:
                continue
            canonical_ordinal = cand.get("canonical_ordinal")
            canonical_id = ordinal_to_span_id.get(canonical_ordinal)
            if canonical_id is None:
                raise SourceMapConflictError(
                    f"duplicate span {cand['source_id']} 缺少 canonical span，拒绝写入"
                )
            if canonical_id == cand["span_id"]:
                raise SourceMapConflictError(
                    f"duplicate span {cand['source_id']} 的 canonical 指向自身，拒绝写入"
                )
            conn.execute(
                "UPDATE source_spans SET span_state='duplicate', reason_code=?, "
                " canonical_span_id=? WHERE id=?",
                (cand["reason"], canonical_id, cand["span_id"]),
            )

    return _source_map_summary(domain_id, already_present=False)


def _materialize_inline_transcript(domain: dict, text: str) -> list[dict]:
    """把 inline 转写物化为**属于当前 run** 的 source_chunks。

    修复两个 P0 数据问题（Phase 2 Closeout）:

    1. **绝不删除其他 run 的材料**。早期实现执行
       ``DELETE FROM source_chunks WHERE material_id IS NULL AND lesson_id = ?``，
       会把同一课时**历史课堂**的原始转写 chunk 全部抹掉 —— 破坏原始材料证据链。
       现在只操作 ``origin_run_id = 当前 run`` 的 chunk。
    2. **同 locator 不同正文不得串课**。早期实现只比较 locator 判幂等，导致
       新课堂使用相同时间戳时直接复用旧 run 的正文。现在幂等判定同时比较
       locator 顺序**与规范化文本哈希**，任一不同即重写本 run 的 chunk。

    归属方式：``source_chunks.origin_run_id``（0019 新增，可空，向后兼容）。
    文件材料 chunk 的该列为 NULL，不受影响。
    """
    if not text.strip():
        return []
    located = transcript_to_located_texts(text)
    if not located:
        return []
    run_id = int(domain["run_id"])
    lesson_id = domain.get("lesson_id")
    hashes = [hash_text(piece) for _loc, piece in located]
    locators = [loc for loc, _piece in located]

    def _current_run_chunks() -> list[dict]:
        # 只取本 run 明确归属的 inline chunk；绝不按 lesson_id 全量捞历史。
        return [dict(r) for r in db.fetch_all(
            "SELECT id, locator, text, inline_text_hash FROM source_chunks "
            "WHERE material_id IS NULL AND origin_run_id = ? "
            "ORDER BY id", (run_id,))]

    current = _current_run_chunks()
    # 幂等：locator 顺序与文本哈希都一致才复用
    if current and [r["locator"] for r in current] == locators \
            and [r.get("inline_text_hash") for r in current] == hashes:
        return current

    with db.transaction() as conn:
        # 只清掉**本 run** 的旧 inline chunk（重入且内容变化时）
        conn.execute(
            "DELETE FROM source_chunks WHERE material_id IS NULL AND origin_run_id = ?",
            (run_id,),
        )
        for (locator, piece), digest in zip(located, hashes):
            conn.execute(
                "INSERT INTO source_chunks (material_id, lesson_id, chapter_id, course_id, "
                " type, locator, text, origin_run_id, inline_text_hash, created_at) "
                "VALUES (NULL, ?, ?, ?, 'transcript', ?, ?, ?, ?, datetime('now','localtime'))",
                (lesson_id, domain.get("chapter_id"), domain.get("course_id"),
                 locator, piece, run_id, digest),
            )
    return _current_run_chunks()


def _inline_transcript_text(run_id: int) -> str:
    """从 run 输入读取 inline transcript（transcript_only 的持久化来源）。"""
    row = db.fetch_one("SELECT input_json FROM workflow_runs WHERE id=?", (run_id,))
    if not row:
        return ""
    try:
        payload = json.loads(row.get("input_json") or "{}")
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("transcript") or "")


def _source_map_summary(domain_id: int, *, already_present: bool) -> dict[str, Any]:
    rows = db.fetch_all(
        "SELECT span_state, COUNT(*) AS n FROM source_spans WHERE domain_id=? GROUP BY span_state",
        (domain_id,),
    )
    counts = {r["span_state"]: int(r["n"]) for r in rows}
    total = sum(counts.values())
    return {
        "domain_id": domain_id,
        "total_spans": total,
        "included_spans": counts.get(SpanState.INCLUDED.value, 0),
        "duplicate_spans": counts.get(SpanState.DUPLICATE.value, 0),
        "noise_spans": counts.get(SpanState.NOISE.value, 0),
        "unsupported_spans": counts.get(SpanState.UNSUPPORTED.value, 0),
        "failed_spans": counts.get(SpanState.FAILED.value, 0),
        "excluded_spans": counts.get(SpanState.EXCLUDED_WITH_REASON.value, 0),
        "already_present": already_present,
    }


# ---------------------------------------------------------------------------
# 空 domain 的 MaterialDomainSnapshot（给 API / 契约层复用）
# ---------------------------------------------------------------------------
def domain_snapshot_model(domain: dict) -> MaterialDomainSnapshot:
    from .contracts import DomainItem

    items = []
    for row in domain.get("items") or []:
        items.append(DomainItem(
            domain_item_id=int(row["id"]),
            material_id=row.get("material_id"),
            source_kind=SourceKind(row["source_kind"]),
            locator=row.get("locator"),
            ordinal=int(row["ordinal"]),
            required=bool(row.get("required")),
            raw_chars=int(row.get("raw_chars") or 0),
            raw_tokens=int(row.get("raw_tokens") or 0),
            content_hash=row.get("content_hash"),
            state=DomainItemState(row["state"]),
            reason_code=row.get("reason_code"),
            reason_detail=row.get("reason_detail"),
            duplicate_of_item_id=row.get("duplicate_of_item_id"),
        ))
    return MaterialDomainSnapshot(
        run_id=int(domain["run_id"]),
        domain_id=int(domain["id"]),
        scope=DomainScope(domain.get("scope") or DomainScope.LESSON.value),
        course_id=domain.get("course_id"),
        chapter_id=domain.get("chapter_id"),
        lesson_id=domain.get("lesson_id"),
        version=int(domain.get("version") or 1),
        domain_hash=domain["domain_hash"],
        state=DomainState(domain.get("state") or DomainState.FROZEN.value),
        engine_version=domain.get("engine_version") or "v6-phase1",
        transcript_chars=int(domain.get("transcript_chars") or 0),
        frozen_at=domain.get("frozen_at"),
        items=items,
    )
