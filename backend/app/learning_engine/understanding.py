"""节点 07 understand_segments / 节点 08 merge_lesson_understanding（V6 Phase 2）。

节点 07（LLM map）:

* 每个 segment 把该段**全部** Source Span 提供给模型（primary + overlap）。
* 严格 Pydantic schema（``extra="forbid"``）。
* 模型只能引用输入中存在的 Source ID；非法 Source ID 立即让该段失败。
* 不要求模型复制 exact quote。
* 单段可独立重试；重试用尽后整个理解状态不得 completed。
* 不得用「跳过失败 segment」换取成功。
* 并发受 Worker/Gateway 上限控制，并响应取消。

节点 08（reduce）:

* 必须消费**全部**成功的 primary segment（分批/分层，不允许只合并前 N 段）。
* 冲突进入 ``unresolved_conflicts``，禁止静默覆盖。
* 不得新增不存在于单段结果或 Source Map 的课堂事实。

实现说明：Phase 2 的数值**汇总**（source_refs 并集、segment 计数、Source ID
校验）由确定性程序完成；模型只负责归纳 ``topics`` / ``summary`` / 冲突识别。
这样「不得新增课堂事实」由程序可验证地保证。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from typing import Any, Optional

from .. import database as db
from ..dag import RunCancelledError
from . import config as engine_config
from .contracts import (
    SCHEMA_VERSION,
    SegmentStatus,
)
from .segment import SEGMENT_PROMPT_VERSION, get_segments

logger = logging.getLogger(__name__)

MERGE_PROMPT_VERSION = "v1"

#: 每段最大重试次数（含首次）。
SEGMENT_MAX_ATTEMPTS = 3

#: 单段输出应是紧凑的结构化理解，不应沿用模型档案里数万 token 的理论输出上限。
DEFAULT_SEGMENT_MAX_OUTPUT_TOKENS = 4_096

#: merge 每批最多合并多少段片段结果（超过则分层合并）。
MERGE_BATCH_SIZE = 12

#: 并发上限（不引入第二套队列：仅限制本节点内部并发）。
DEFAULT_SEGMENT_CONCURRENCY = 2

_SOURCE_ID_RE = re.compile(r"^[TPDI]\d{4,6}(?:\.\d{1,2})?$")


class SourceRefViolationError(RuntimeError):
    """模型引用了不存在的 Source ID —— 该 segment 必须失败。"""


class SegmentUnderstandingError(RuntimeError):
    """单段理解在重试用尽后仍失败。"""


def _segment_concurrency() -> int:
    raw = (os.environ.get("V6_SEGMENT_CONCURRENCY") or "").strip()
    if raw:
        try:
            return max(1, min(int(raw), 8))
        except ValueError:
            pass
    return DEFAULT_SEGMENT_CONCURRENCY


def _segment_max_output_tokens() -> int:
    raw = (os.environ.get("V6_SEGMENT_MAX_OUTPUT_TOKENS") or "").strip()
    if not raw:
        return DEFAULT_SEGMENT_MAX_OUTPUT_TOKENS
    try:
        return max(1_024, min(int(raw), 16_384))
    except ValueError:
        logger.warning("无效 V6_SEGMENT_MAX_OUTPUT_TOKENS=%r，使用默认值", raw)
        return DEFAULT_SEGMENT_MAX_OUTPUT_TOKENS


def _valid_source_id(value: str) -> bool:
    return bool(_SOURCE_ID_RE.match(str(value or "").strip()))


# ---------------------------------------------------------------------------
# 节点 07：单段理解
# ---------------------------------------------------------------------------
def _render_material_blocks(rows: list[dict]) -> str:
    """把段内 span 渲染成 prompt 材料块（带 SRC= 标注，便于模型只引用真实 ID）。"""
    lines = []
    for span in rows:
        locator = span.get("locator") or ""
        lines.append(f"[SRC={span['source_id']}] ({locator})")
        lines.append(str(span.get("text") or ""))
        lines.append("")
    return "\n".join(lines).strip() or "（无材料）"


async def understand_one_segment(segment: dict, runs_dir: Optional[dict] = None) -> dict:
    """对单个 segment 调模型做理解（一次尝试）。

    失败原因（schema 错误 / 非法 Source ID / 网关错误）由调用方决定是否重试。
    """
    from ..gateway import gateway
    from ..integrations.prompts import render_prompt
    from ..integrations.schemas import SegmentUnderstandingOut, parse_model_output

    sources = segment.get("sources") or []
    span_rows = _load_spans([int(s["source_span_id"]) for s in sources])
    allowed_set = {r["source_id"] for r in span_rows}

    segment_total = int((runs_dir or {}).get("segment_total") or 1)
    messages, meta = render_segment_messages(segment, segment_total)
    # 断言材料只出现一次（防止日后把 material_blocks 又塞回 system prompt）
    material_only = _render_material_blocks(span_rows)
    if material_only and material_only in meta["system_text"]:
        raise RuntimeError("segment prompt 中材料同时出现在 system 与 user（重复计费与超预算）")

    resp = await gateway.chat(
        segment.get("_model") or "", messages,
        contract="lesson/segment_understanding", temperature=0.25,
        max_tokens=_segment_max_output_tokens(),
        response_format={"type": "json_object"},
    )

    data = parse_model_output(SegmentUnderstandingOut, resp.get("content", ""),
                             f"segment_understanding#{segment['ordinal']}")
    # ---- 非法 Source ID 立即失败（任务书 §五）----
    bad = sorted({sid for sid in _collect_source_refs(data) if sid not in allowed_set})
    if bad:
        raise SourceRefViolationError(
            f"segment {segment['ordinal']} 引用了不存在的 Source ID: {bad}"
        )
    data["_tokens_in"] = resp.get("tokens_in", 0)
    data["_tokens_out"] = resp.get("tokens_out", 0)
    # _model 统一为**候选模型名**（复用键口径）；gateway 实际返回的模型名另存
    data["_model"] = segment.get("_model") or ""
    data["_gateway_model"] = resp.get("model") or ""
    data["_prompt_checksum"] = meta["prompt_checksum"]
    data["_input_tokens_estimate"] = estimate_messages_tokens(messages)
    return data


def _collect_source_refs(data: dict) -> list[str]:
    refs: list[str] = []
    for sid in data.get("source_refs") or []:
        refs.append(str(sid).strip())
    for key in ("knowledge_units", "definitions", "formulas", "derivations", "examples"):
        for item in data.get(key) or []:
            if isinstance(item, dict):
                for sid in item.get("source_refs") or []:
                    refs.append(str(sid).strip())
    return [r for r in refs if r]


def _load_spans(span_ids: list[int]) -> list[dict]:
    if not span_ids:
        return []
    q = ",".join("?" * len(span_ids))
    rows = db.fetch_all(
        f"SELECT id, source_id, locator, text, start_ms, end_ms, source_kind, ordinal "
        f"FROM source_spans WHERE id IN ({q}) ORDER BY ordinal, id",
        tuple(span_ids),
    )
    return [dict(r) for r in rows]


def render_segment_messages(segment: dict, segment_total: int,
                            extra_spans: Optional[list[dict]] = None) -> tuple[list[dict], dict]:
    """构造该 segment 的最终 messages（**材料只出现一次**）。

    返回 ``(messages, meta)``。``extra_spans`` 允许调用方先做预算试算（不查库）。

    修复（Phase 2 Closeout）：早期实现把材料同时放进 system（prompt 变量
    ``material_blocks``）与 user 消息，等于每个 span 发送两次，使真实输入量翻倍、
    预算计算失去意义。现在材料**只在 user 消息中出现**。
    """
    from ..integrations.prompts import render_prompt

    if extra_spans is not None:
        span_rows = extra_spans
    else:
        sources = segment.get("sources") or []
        span_rows = _load_spans([int(s["source_span_id"]) for s in sources])
    allowed_ids = [r["source_id"] for r in span_rows]
    first, last = (allowed_ids[0], allowed_ids[-1]) if allowed_ids else ("-", "-")
    p = render_prompt(
        "lesson/segment_understanding", SEGMENT_PROMPT_VERSION,
        segment_ordinal=str(segment["ordinal"]),
        segment_total=str(segment_total),
        segment_range=f"{first} … {last}",
        segment_tokens=str(segment.get("token_count") or 0),
        allowed_source_ids=", ".join(allowed_ids) or "（无）",
    )
    material_only = _render_material_blocks(span_rows)
    user_text = (f"片段 {segment['ordinal']} / {segment_total} 材料如下"
                 f"（每块以 SRC=<Source ID> 标注）：\n\n{material_only}")
    messages = [
        {"role": "system", "content": p["text"]},
        {"role": "user", "content": user_text},
    ]
    meta = {"prompt_checksum": p["checksum"], "allowed_ids": allowed_ids,
            "system_text": p["text"], "user_text": user_text,
            "material_only": material_only}
    return messages, meta


def estimate_messages_tokens(messages: list[dict]) -> int:
    """按**最终实际 messages** 估算输入 token（含 role 结构开销）。"""
    from .normalize import estimate_tokens
    return sum(estimate_tokens(m.get("content") or "") for m in messages) + len(messages) * 4


async def run_understand_segments(ctx, domain_id: int, run_id: int,
                                  model_id: Optional[str] = None) -> dict:
    """节点 07 主体：并发理解全部 pending/failed segment，写账本与结果。

    ``model_id`` 为 DAG 当前的**候选模型**（handler 的 ``model`` 参数）。
    必须真实使用：早期实现忽略它、内部重新 ``ctx.resolve_model(role)`` 取路由
    首选模型，导致「第一个模型失败后 DAG 切到第二个模型」时，节点内重试仍然
    反复调用第一个模型，fallback 形同虚设。

    取消：任一任务取消时显式 cancel 其余未完成 sibling task 并等待清理，
    避免留下继续跑的后台任务。
    """
    segments = get_segments(domain_id)
    if not segments:
        return {"segment_count": 0, "succeeded": 0, "failed": 0,
                "note": "没有 segment（canonical span 为空）"}

    effective_model = model_id or ctx.resolve_model("segment_understanding") or ""

    # 跨 run 复用（Closeout）：命中则零模型调用
    reused = 0
    todo: list[dict] = []
    for seg in segments:
        if _try_reuse(seg, ctx, domain_id, run_id, effective_model):
            reused += 1
            continue
        todo.append(seg)

    total = len(segments)
    semaphore = asyncio.Semaphore(_segment_concurrency())

    tasks: list[asyncio.Task] = []

    async def _work(seg: dict) -> dict:
        async with semaphore:
            return await _understand_with_retries(
                seg, ctx, domain_id, run_id, total, effective_model)

    cancelled = False
    try:
        for seg in todo:
            tasks.append(asyncio.create_task(_work(seg)))
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except BaseException:
        # 取消 / 异常：显式取消所有未完成 sibling 并等待它们真正结束
        cancelled = True
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        _mark_unfinished_cancelled(domain_id, [int(s["id"]) for s in todo])
        raise

    succeeded = reused
    failed = 0
    cancel_error: Optional[BaseException] = None
    for res in results:
        if isinstance(res, BaseException):
            if isinstance(res, (asyncio.CancelledError, RunCancelledError)):
                cancelled = True
                cancel_error = cancel_error or res
                continue
            failed += 1
            continue
        if res.get("status") == SegmentStatus.SUCCEEDED.value:
            succeeded += 1
        else:
            failed += 1
    if cancelled:
        _mark_unfinished_cancelled(domain_id, [int(s["id"]) for s in todo])
    if cancel_error is not None:
        # 取消必须向上传播：``gather(return_exceptions=True)`` 会把异常收进结果
        # 列表，若不显式重抛，调用方会以为理解"正常结束"，取消语义丢失。
        raise cancel_error

    return {
        "segment_count": total,
        "succeeded": succeeded,
        "failed": failed,
        "reused": reused,
        "attempted": len(todo),
        "model_used_candidate": effective_model,
        "cancelled": cancelled,
        "all_succeeded": failed == 0 and not cancelled and succeeded == total,
    }


def _try_reuse(seg: dict, ctx, domain_id: int, run_id: int,
               model_id: str) -> bool:
    """尝试复用**同 run 或跨 run** 已成功且输入完全一致的 SegmentUnderstanding。

    复用条件（全部必须一致）:
      schema_version / prompt_version / 候选模型 / 分段算法策略 /
      primary Source ID 集合与其规范化文本哈希 / reuse_scope_hash

    重要约束:
    * 只复用 ``succeeded`` 结果；failed / cancelled / partial 一律不复用。
    * ``domain_hash`` 含 run_id，**不能**作为跨 run 内容相等依据 —— 因此使用
      不含 run_id 的 ``reuse_scope_hash``（见 domain.compute_reuse_scope_hash）。
    * 复用是**投影**：把结果复制成本 run 自己的 segment_understandings 行，
      child run 不会共享父 run 的可变记录。
    * 记录来源 run_id / segment_id / reuse_reason 到 segment_reuse_index。

    候选模型口径：``segment_understandings.model_used`` 存的是 **DAG 传入的候选
    模型**（不是 gateway 返回的 ``fake::x``），否则 fake/live 两种模式下复用键
    口径不一致，会出现「同模型却命中不到」或「换模型却错误复用」。
    """
    my_hash = seg.get("input_hash") or ""
    if not my_hash:
        return False
    candidate = _candidate_model_key(model_id)

    # 1) 同 domain 内已成功（重入 / 断点复用）
    #    版本必须参与判定：早期实现只比 input_hash + model_used，一旦 prompt /
    #    schema / 分段算法版本升级，旧结果会被当成本次结果复用 —— 复用键口径
    #    与跨 run 分支不一致（跨 run 分支本来就比版本）。分段算法策略与版本已
    #    进入 segment.input_hash（SegmentDraft.input_hash），因此这里只需补齐
    #    prompt / schema 两个版本维度。
    same = db.fetch_one(
        "SELECT segment_id, model_used, prompt_version, schema_version "
        "FROM segment_understandings "
        "WHERE segment_id=? AND status='succeeded' AND input_hash=?",
        (int(seg["id"]), my_hash),
    )
    if same and (same.get("model_used") or "") == candidate \
            and (same.get("prompt_version") or "") == SEGMENT_PROMPT_VERSION \
            and (same.get("schema_version") or "") == SCHEMA_VERSION:
        return True

    # 2) 跨 run：reuse_scope_hash + input_hash + 版本 + 候选模型 全等
    scope = _reuse_scope_hash(domain_id)
    if not scope:
        return False
    src = db.fetch_one(
        "SELECT su.segment_id, su.run_id FROM segment_understandings su "
        "JOIN lesson_segments ls ON ls.id = su.segment_id "
        "JOIN material_domains md ON md.id = su.domain_id "
        "WHERE su.status='succeeded' AND su.input_hash=? AND su.prompt_version=? "
        "  AND su.schema_version=? AND su.model_used=? "
        "  AND md.reuse_scope_hash=? AND su.run_id <> ? "
        "ORDER BY su.id LIMIT 1",
        (my_hash, SEGMENT_PROMPT_VERSION, SCHEMA_VERSION, candidate, scope, run_id),
    )
    if not src:
        return False
    return _project_reuse(seg, domain_id, run_id, src, scope, candidate)


def _project_reuse(seg: dict, domain_id: int, run_id: int, src: dict,
                   scope: str, candidate: str) -> bool:
    """把来源 run 的成功理解**投影**为本 run 的记录（不共享可变行）。"""
    from .coverage import record_ledger_entry

    full = db.fetch_one(
        "SELECT structured_json FROM segment_understandings WHERE segment_id=? AND run_id=?",
        (int(src["segment_id"]), int(src["run_id"])),
    )
    if not full:
        return False
    try:
        data = json.loads(full["structured_json"])
    except Exception:
        return False
    data["_model"] = candidate          # 保持本 run 的候选模型口径
    data["_reused_from_run"] = int(src["run_id"])
    data["_reused_from_segment"] = int(src["segment_id"])
    _persist_understanding(seg, domain_id, run_id, data)
    # 复用等价于「本 run 已理解该段」：必须同样写 processed 账目，
    # 否则覆盖审计会把已复用的 span 记成 0% 语义处理率（假降级）。
    for source in seg.get("sources") or []:
        if source["role"] != "primary":
            continue
        record_ledger_entry(
            run_id=run_id, domain_id=domain_id,
            source_span_id=int(source["source_span_id"]), source_id=source["source_id"],
            stage="understand_segments", outcome="processed",
            reason_code="reused_from_previous_run",
            reason_detail=f"复用 run {src['run_id']} segment {src['segment_id']} 的理解结果",
            segment_id=int(seg["id"]),
        )
    _mark_segment(int(seg["id"]), SegmentStatus.SUCCEEDED.value, attempt=0,
                  error=f"reused from run {src['run_id']} segment {src['segment_id']}")
    _record_reuse_index(domain_id, run_id, seg, scope, src, candidate)
    return True


def _candidate_model_key(model_id: str) -> str:
    """复用键里的候选模型标识（DAG 传入的逻辑模型名）。"""
    return str(model_id or "")


def _reuse_scope_hash(domain_id: int) -> Optional[str]:
    row = db.fetch_one("SELECT reuse_scope_hash FROM material_domains WHERE id=?",
                       (domain_id,))
    return (row or {}).get("reuse_scope_hash")


def _record_reuse_index(domain_id: int, run_id: int, seg: dict, scope: str,
                        src: dict, model_id: str) -> None:
    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO segment_reuse_index (domain_hash, input_hash, prompt_version, "
                " schema_version, model_used, segment_id, reuse_scope_hash, segment_ordinal, "
                " source_run_id, source_segment_id, reuse_reason, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?, datetime('now','localtime')) "
                "ON CONFLICT(domain_hash, input_hash, prompt_version, schema_version) "
                "DO UPDATE SET segment_id=excluded.segment_id, "
                " reuse_scope_hash=excluded.reuse_scope_hash, "
                " source_run_id=excluded.source_run_id, "
                " source_segment_id=excluded.source_segment_id, "
                " reuse_reason=excluded.reuse_reason",
                (scope, seg.get("input_hash") or "", SEGMENT_PROMPT_VERSION, SCHEMA_VERSION,
                 _candidate_model_key(model_id), int(seg["id"]), scope, int(seg["ordinal"]),
                 int(src["run_id"]), int(src["segment_id"]),
                 "identical input_hash + reuse_scope_hash + prompt/schema/model"),
            )
    except Exception as e:  # pragma: no cover - 审计写入失败不应阻断理解
        logger.warning("segment_reuse_index 写入失败: %s", e)


def get_reuse_records(domain_hash: Optional[str] = None) -> list[dict]:
    """复用审计记录（供测试与 API 展示）。"""
    if domain_hash:
        rows = db.fetch_all(
            "SELECT * FROM segment_reuse_index WHERE reuse_scope_hash=? ORDER BY id",
            (domain_hash,))
    else:
        rows = db.fetch_all("SELECT * FROM segment_reuse_index ORDER BY id")
    return [dict(r) for r in rows]


def _mark_unfinished_cancelled(domain_id: int, segment_ids: list[int]) -> None:
    """把仍处于 pending/running 的 segment 标为 cancelled（终态确定性）。"""
    if not segment_ids:
        return
    q = ",".join("?" * len(segment_ids))
    db.execute(
        f"UPDATE lesson_segments SET status='cancelled', error='run cancelled', "
        f" updated_at=datetime('now','localtime') "
        f"WHERE id IN ({q}) AND status IN ('pending','running')",
        tuple(segment_ids),
    )


async def _understand_with_retries(seg: dict, ctx, domain_id: int, run_id: int,
                                   segment_total: int,
                                   model_id: Optional[str] = None) -> dict:
    """单段理解：最多 ``SEGMENT_MAX_ATTEMPTS`` 次；失败即显式记账，绝不跳过。

    ``model_id`` 必须由调用方（DAG 候选模型）提供：模型 fallback 的语义是
    「同一候选模型内重试 N 次 → 整体失败 → DAG 切下一个候选模型」，
    因此这里**只使用传入的模型**，不再自行查路由首选模型。
    """
    from .coverage import REASON_SEGMENT_NOT_REACHED, record_ledger_entry

    segment_id = int(seg["id"])
    last_err: Optional[BaseException] = None
    effective_model = model_id or ctx.resolve_model("segment_understanding") or ""
    if not effective_model:
        raise SegmentUnderstandingError("segment_understanding 没有可用模型")

    for attempt in range(1, SEGMENT_MAX_ATTEMPTS + 1):
        await ctx.raise_if_cancelled()
        seg_probe = dict(seg)
        seg_probe["_model"] = effective_model
        _mark_segment(segment_id, SegmentStatus.RUNNING.value, attempt=attempt)
        try:
            data = await understand_one_segment(
                seg_probe, {"segment_total": segment_total})
        except BaseException as exc:  # noqa: BLE001
            # 必须捕获 BaseException：``RunCancelledError`` 继承 BaseException
            # （设计上要穿透节点的 ``except Exception``），若只写
            # ``except (asyncio.CancelledError, Exception)``，取消会直接冒泡而不落
            # segment 终态，该段会永远停在 running。
            from ..dag import RunCancelledError
            if isinstance(exc, (asyncio.CancelledError, RunCancelledError)):
                _mark_segment(segment_id, SegmentStatus.CANCELLED.value,
                              attempt=attempt, error="run cancelled")
                raise
            if not isinstance(exc, Exception):
                raise  # 其他 BaseException（KeyboardInterrupt 等）不吞
            last_err = exc
            logger.warning("segment %s 第 %s 次理解失败: %s", seg["ordinal"], attempt, exc)
            _mark_segment(segment_id, SegmentStatus.FAILED.value, attempt=attempt,
                          error=str(exc)[:500])
            # HTTP read timeout 再对同一请求重复 2 次，只会把 180s 放大成 9 分钟。
            # 立即结束当前候选，让 DAG 使用下一候选模型；schema/source-ref 失败
            # 仍保留原有段级重试语义。
            from ..gateway_engines import GatewayReadTimeoutError
            if isinstance(exc, GatewayReadTimeoutError):
                break
            continue

        _persist_understanding(seg, domain_id, run_id, data)
        _mark_segment(segment_id, SegmentStatus.SUCCEEDED.value, attempt=attempt, error="")
        # 该段全部 primary span 记为 processed（覆盖率只按 primary 计算）
        for src in seg.get("sources") or []:
            if src["role"] != "primary":
                continue
            record_ledger_entry(
                run_id=run_id, domain_id=domain_id,
                source_span_id=int(src["source_span_id"]), source_id=src["source_id"],
                stage="understand_segments", outcome="processed",
                segment_id=segment_id,
            )
        return {"segment_id": segment_id, "status": SegmentStatus.SUCCEEDED.value,
                "tokens_in": data.get("_tokens_in", 0),
                "tokens_out": data.get("_tokens_out", 0)}

    # 重试用尽：该段保持 failed；primary span 保持 not_used(segment_not_reached)
    for src in seg.get("sources") or []:
        if src["role"] != "primary":
            continue
        record_ledger_entry(
            run_id=run_id, domain_id=domain_id,
            source_span_id=int(src["source_span_id"]), source_id=src["source_id"],
            stage="understand_segments", outcome="not_used",
            reason_code=REASON_SEGMENT_NOT_REACHED,
            reason_detail=f"segment {seg['ordinal']} 理解失败（重试 {SEGMENT_MAX_ATTEMPTS} 次）",
            segment_id=segment_id,
        )
    return {"segment_id": segment_id, "status": SegmentStatus.FAILED.value,
            "error": str(last_err)[:300]}


def _mark_segment(segment_id: int, status: str, *, attempt: int, error: str = "") -> None:
    db.execute(
        "UPDATE lesson_segments SET status=?, attempts=?, error=?, "
        " updated_at=datetime('now','localtime') WHERE id=?",
        (status, attempt, (error or "")[:1000], segment_id),
    )


def _persist_understanding(seg: dict, domain_id: int, run_id: int, data: dict) -> None:
    payload = {k: v for k, v in data.items() if not k.startswith("_")}
    # 跨 run 复用的「元信息」也要保留在结构体里，便于审计反查来源
    for meta_key in ("_reused_from_run", "_reused_from_segment"):
        if meta_key in data:
            payload[meta_key.lstrip("_")] = data[meta_key]
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    content_hash = "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
    refs = sorted(set(_collect_source_refs(payload)))
    # model_used 存 **DAG 候选模型**（data['_model'] 由 _understand_with_retries /
    # _project_reuse 统一写入候选模型名），保证复用键口径稳定。
    candidate = str(data.get("_model") or "")
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO segment_understandings (segment_id, run_id, domain_id, schema_version, "
            " prompt_version, model_used, input_hash, structured_json, content_hash, "
            " source_ref_count, status, error, attempts, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?, 'succeeded', '', 1, datetime('now','localtime'), "
            " datetime('now','localtime')) "
            "ON CONFLICT(segment_id) DO UPDATE SET model_used=excluded.model_used, "
            " run_id=excluded.run_id, domain_id=excluded.domain_id, "
            " input_hash=excluded.input_hash, structured_json=excluded.structured_json, "
            " content_hash=excluded.content_hash, source_ref_count=excluded.source_ref_count, "
            " status='succeeded', error='', attempts=attempts+1, "
            " updated_at=datetime('now','localtime')",
            (int(seg["id"]), run_id, domain_id, SCHEMA_VERSION, SEGMENT_PROMPT_VERSION,
             candidate, seg.get("input_hash") or "", blob, content_hash, len(refs)),
        )


# ---------------------------------------------------------------------------
# 节点 08：全局合并
# ---------------------------------------------------------------------------
def _load_succeeded_understandings(domain_id: int) -> list[dict]:
    rows = db.fetch_all(
        "SELECT su.structured_json, su.segment_id, ls.ordinal, ls.input_hash "
        "FROM segment_understandings su JOIN lesson_segments ls ON ls.id = su.segment_id "
        "WHERE su.domain_id=? AND su.status='succeeded' ORDER BY ls.ordinal",
        (domain_id,),
    )
    out = []
    for row in rows:
        try:
            payload = json.loads(row["structured_json"])
        except Exception:
            payload = {}
        out.append({"segment_id": int(row["segment_id"]), "ordinal": int(row["ordinal"]),
                    "input_hash": row["input_hash"], "data": payload})
    return out


def _structure_key(kind: str, item: dict) -> str:
    """结构块的稳定合并键（同类对象按此键 + Source ID 归并）。"""
    if kind == "definitions":
        return f"def:{str(item.get('term') or '').strip()}"
    if kind == "formulas":
        return f"formula:{str(item.get('expression') or '').strip()}"
    if kind == "derivations":
        return f"derive:{str(item.get('goal') or '').strip()}"
    if kind == "examples":
        return f"example:{str(item.get('prompt') or '').strip()}"
    return f"{kind}:{json.dumps(item, ensure_ascii=False, sort_keys=True)}"


#: 必须在 merge 中完整保留的结构块（缺一即视为内容丢失）。
MERGE_STRUCTURE_KINDS = ("definitions", "formulas", "derivations", "examples")


def _merge_structures(segments: list[dict], kind: str, valid_source_ids: set[str],
                      conflicts: list[str]) -> list[dict]:
    """合并同类结构块（定义 / 公式 / 推导 / 例题）。

    * 按稳定键归并，`source_refs` 取并集；
    * 键相同但**内容不同**时不静默覆盖：保留先出现者，并把差异写入
      ``unresolved_conflicts``（同时记录 ``variants`` 供人工确认）；
    * 每个块记录 ``segment_ids``，保证可反向定位到原 SegmentUnderstanding；
    * **绝不因合并删除任何 segment 独有的结构块**。
    """
    merged: dict[str, dict] = {}
    for seg in segments:
        for raw in seg["data"].get(kind) or []:
            if not isinstance(raw, dict):
                continue
            key = _structure_key(kind, raw)
            refs = [r for r in (raw.get("source_refs") or []) if r in valid_source_ids]
            payload = {k: v for k, v in raw.items() if k != "source_refs"}
            if key in merged:
                existing = merged[key]
                existing["source_refs"] = list(dict.fromkeys(existing["source_refs"] + refs))
                existing["segment_ids"] = list(dict.fromkeys(
                    existing["segment_ids"] + [seg["ordinal"]]))
                prev = {k: v for k, v in existing.items()
                        if k not in ("source_refs", "segment_ids", "variants")}
                if prev != payload:
                    # 同一键、两种表述：不覆盖，进冲突列表
                    conflicts.append(
                        f"{kind}「{key.split(':', 1)[-1]}」在片段 {existing['segment_ids']} "
                        f"与 [{seg['ordinal']}] 中内容不一致"
                    )
                    variants = existing.setdefault("variants", [])
                    if payload not in variants:
                        variants.append(payload)
            else:
                merged[key] = {
                    **payload,
                    "source_refs": refs,
                    "segment_ids": [seg["ordinal"]],
                }
    return list(merged.values())


def deterministic_merge(domain_id: int, run_id: int, lesson_id: Optional[int],
                        segments: list[dict], valid_source_ids: set[str]) -> dict:
    """确定性合并（数值汇总 + 结构归并 + 冲突检测）。

    只做**并集与归并**，不生成新事实：模型无法凭空造出无 Source ID 支持的内容。

    修复：早期实现只输出 ``topics`` / ``knowledge_units`` / ``unresolved_conflicts``，
    把单段里的 **definitions / formulas / derivations / examples 全部丢弃**。
    现在这些结构块全部保留并可反向定位到原 segment，且统计
    ``structures_dropped`` 供覆盖门禁阻断。
    """
    topics: list[str] = []
    units: dict[str, dict] = {}
    conflicts: list[str] = []
    unresolved_points: list[str] = []
    emphasis = 0.0

    for seg in segments:
        data = seg["data"]
        for t in data.get("topics") or []:
            if t not in topics:
                topics.append(t)
        emphasis = max(emphasis, float(data.get("teacher_emphasis") or 0.0))
        for unit in data.get("knowledge_units") or []:
            topic = str(unit.get("topic") or "").strip()
            key = topic or str(unit.get("temp_id") or "")
            refs = [r for r in (unit.get("source_refs") or []) if r in valid_source_ids]
            if not key:
                continue
            if key in units:
                existing = units[key]
                merged_refs = list(dict.fromkeys(existing["source_refs"] + refs))
                relations = list(dict.fromkeys(existing["relations"] +
                                                [str(r) for r in unit.get("relations") or []]))
                if existing["summary"] and unit.get("summary") \
                        and existing["summary"] != unit.get("summary"):
                    # 同一主题两种表述：不静默覆盖，进入 unresolved_conflicts
                    conflicts.append(
                        f"知识点「{topic}」在片段 {existing['segment_ids']} 与 "
                        f"[{seg['ordinal']}] 中表述不一致"
                    )
                    if existing["summary"] != unit.get("summary"):
                        existing["summaries"] = list(dict.fromkeys(
                            existing.get("summaries", [existing["summary"]])
                            + [str(unit.get("summary"))]))
                existing["source_refs"] = merged_refs
                existing["relations"] = relations
                existing["teacher_emphasis"] = max(
                    existing["teacher_emphasis"], float(unit.get("teacher_emphasis") or 0.0))
                existing["segment_ids"] = list(dict.fromkeys(
                    existing["segment_ids"] + [seg["ordinal"]]))
            else:
                units[key] = {
                    "id": f"KU-{len(units) + 1:04d}",
                    "topic": topic or key,
                    "kind": str(unit.get("kind") or "concept"),
                    "summary": str(unit.get("summary") or ""),
                    "source_refs": refs,
                    "teacher_emphasis": float(unit.get("teacher_emphasis") or 0.0),
                    "relations": [str(r) for r in unit.get("relations") or []],
                    "segment_ids": [seg["ordinal"]],
                }
        for point in data.get("unresolved_points") or []:
            text = str(point).strip()
            if text and text not in unresolved_points:
                unresolved_points.append(text)
            tagged = f"[片段 {seg['ordinal']}] {text}" if text else ""
            if tagged and tagged not in conflicts:
                conflicts.append(tagged)

    # ---- 结构块完整归并（修复内容丢失） ----
    structures: dict[str, list[dict]] = {}
    for kind in MERGE_STRUCTURE_KINDS:
        structures[kind] = _merge_structures(segments, kind, valid_source_ids, conflicts)

    # ---- 结构级 coverage：证明没有任何结构块被 merge 丢弃 ----
    per_segment_total = sum(
        len([x for x in (seg["data"].get(kind) or []) if isinstance(x, dict)])
        for seg in segments for kind in MERGE_STRUCTURE_KINDS)
    merged_total = sum(len(v) for v in structures.values())
    # 归并只会「减少条数」（同键合并），不会减少**结构块实例数**：
    # 用「被归并消费的实例数」作为守恒量，差额即真正丢失。
    consumed_instances = 0
    for kind in MERGE_STRUCTURE_KINDS:
        for item in structures[kind]:
            consumed_instances += len(item.get("segment_ids") or []) or 1
    structures_dropped = max(per_segment_total - consumed_instances, 0)

    consumed_ids = [seg["segment_id"] for seg in segments]
    input_hash = _merge_input_hash(domain_id, segments)
    return {
        "topics": topics,
        "knowledge_units": list(units.values()),
        "definitions": structures["definitions"],
        "formulas": structures["formulas"],
        "derivations": structures["derivations"],
        "examples": structures["examples"],
        "unresolved_conflicts": conflicts,
        "unresolved_points": unresolved_points,
        "teacher_emphasis": emphasis,
        "consumed_segment_ids": consumed_ids,
        "consumed_segment_count": len(consumed_ids),
        "input_hash": input_hash,
        "segment_ordinals": [seg["ordinal"] for seg in segments],
        "valid_source_ids": sorted(valid_source_ids),
        "segment_structures_total": per_segment_total,
        "merged_structures_total": merged_total,
        "structures_dropped": structures_dropped,
    }


def _merge_input_hash(domain_id: int, segments: list[dict]) -> str:
    payload = {
        "schema": SCHEMA_VERSION,
        "prompt": MERGE_PROMPT_VERSION,
        "domain_id": domain_id,
        "segments": [[s["segment_id"], s["input_hash"]] for s in segments],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def run_merge_lesson_understanding(ctx, domain_id: int, run_id: int,
                                         lesson_id: Optional[int]) -> dict:
    """节点 08 主体。返回 ``(status, payload)`` 语义的 dict。

    只有全部 primary segment 都成功时才写 succeeded；否则写 partial 并保留
    已消费计数 —— 让覆盖门禁可以看到「还有段没进理解」。
    """
    from .coverage import REASON_SEGMENT_NOT_REACHED, record_ledger_entry

    segments_all = get_segments(domain_id)
    succeeded = _load_succeeded_understandings(domain_id)
    total = len(segments_all)
    valid_source_ids = {r["source_id"] for r in db.fetch_all(
        "SELECT source_id FROM source_spans WHERE domain_id=? AND span_state='included'",
        (domain_id,))}

    merged = deterministic_merge(domain_id, run_id, lesson_id, succeeded, valid_source_ids)
    # status 只看**实际成功的 segment 数**，不能因为「存在一行 understanding」就写
    # succeeded —— 否则一个 segment 都没成功时也会被当成成功。
    succeeded_segments = sum(
        1 for seg in segments_all
        if seg["status"] == SegmentStatus.SUCCEEDED.value)
    status = ("succeeded"
              if (total > 0 and succeeded_segments == total and len(succeeded) == total)
              else "partial")
    model_used = "deterministic_union_v1"

    blob = json.dumps({
        "topics": merged["topics"],
        "knowledge_units": [{k: v for k, v in u.items() if k != "summaries"}
                            for u in merged["knowledge_units"]],
        # 结构块必须与 knowledge_units 一样被持久化 —— 否则 definitions /
        # formulas / derivations / examples 会「合并了但没保存」。
        "definitions": merged["definitions"],
        "formulas": merged["formulas"],
        "derivations": merged["derivations"],
        "examples": merged["examples"],
        "unresolved_conflicts": merged["unresolved_conflicts"],
        "unresolved_points": merged["unresolved_points"],
        "teacher_emphasis": merged["teacher_emphasis"],
        # 下游（Phase 3 认知层 / Phase 4 证据绑定）需要有效的 Source ID 全集与
        # 消费段清单；必须随结构化结果一起持久化，否则下游只能重新查询或拿到空集。
        "valid_source_ids": merged["valid_source_ids"],
        "consumed_segment_ids": merged["consumed_segment_ids"],
        "segment_ordinals": merged["segment_ordinals"],
        "segment_structures_total": merged["segment_structures_total"],
        "merged_structures_total": merged["merged_structures_total"],
        "structures_dropped": merged["structures_dropped"],
    }, ensure_ascii=False, sort_keys=True)
    content_hash = "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()

    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO lesson_understandings (run_id, domain_id, lesson_id, schema_version, "
            " prompt_version, model_used, input_hash, structured_json, content_hash, "
            " consumed_segment_count, segment_count, merge_levels, valid_source_count, status, "
            " error, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now','localtime'), "
            " datetime('now','localtime')) "
            "ON CONFLICT(run_id) DO UPDATE SET lesson_id=excluded.lesson_id, "
            " model_used=excluded.model_used, input_hash=excluded.input_hash, "
            " structured_json=excluded.structured_json, content_hash=excluded.content_hash, "
            " consumed_segment_count=excluded.consumed_segment_count, "
            " segment_count=excluded.segment_count, merge_levels=excluded.merge_levels, "
            " valid_source_count=excluded.valid_source_count, status=excluded.status, "
            " error=excluded.error, updated_at=datetime('now','localtime')",
            (run_id, domain_id, lesson_id, SCHEMA_VERSION, MERGE_PROMPT_VERSION, model_used,
             merged["input_hash"], blob, content_hash, merged["consumed_segment_count"],
             total, 1, len(valid_source_ids), status,
             "" if status == "succeeded" else
             f"仍有 {total - len(succeeded)}/{total} 个 segment 未成功理解"),
        )

    if status != "succeeded":
        # 未成功的段其 primary span 保持 not_used —— 覆盖门禁据此阻断 completed
        for seg in segments_all:
            if seg["status"] == SegmentStatus.SUCCEEDED.value:
                continue
            for src in seg.get("sources") or []:
                if src["role"] != "primary":
                    continue
                record_ledger_entry(
                    run_id=run_id, domain_id=domain_id,
                    source_span_id=int(src["source_span_id"]), source_id=src["source_id"],
                    stage="understand_segments", outcome="not_used",
                    reason_code=REASON_SEGMENT_NOT_REACHED,
                    reason_detail=f"segment {seg['ordinal']} 状态={seg['status']}",
                    segment_id=int(seg["id"]),
                )

    return {
        "status": status,
        "segment_count": total,
        "consumed_segment_count": merged["consumed_segment_count"],
        "consumed_segment_ids": merged["consumed_segment_ids"],
        "knowledge_unit_count": len(merged["knowledge_units"]),
        "topic_count": len(merged["topics"]),
        "definition_count": len(merged["definitions"]),
        "formula_count": len(merged["formulas"]),
        "derivation_count": len(merged["derivations"]),
        "example_count": len(merged["examples"]),
        "segment_structures_total": merged["segment_structures_total"],
        "merged_structures_total": merged["merged_structures_total"],
        "structures_dropped": merged["structures_dropped"],
        "unresolved_conflicts": merged["unresolved_conflicts"],
        "unresolved_points": merged["unresolved_points"],
        "valid_source_count": len(valid_source_ids),
        "merge_levels": 1,
        "input_hash": merged["input_hash"],
        "model_used": model_used,
    }


def record_empty_understanding(domain_id: int, run_id: int,
                               lesson_id: Optional[int]) -> None:
    """写入一份「无 canonical span」的空理解（status=partial，绝不 succeeded）。

    这样 ``/understanding`` 与覆盖审计对「没有内容可理解」的运行给出**一致**的
    显式结论，而不是 404 或伪装成功。
    """
    blob = json.dumps({"topics": [], "knowledge_units": [], "unresolved_conflicts": [],
                       "note": "no canonical span"}, ensure_ascii=False, sort_keys=True)
    content_hash = "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO lesson_understandings (run_id, domain_id, lesson_id, schema_version, "
            " prompt_version, model_used, input_hash, structured_json, content_hash, "
            " consumed_segment_count, segment_count, merge_levels, valid_source_count, status, "
            " error, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,0,0,1,0,'partial',?, "
            " datetime('now','localtime'), datetime('now','localtime')) "
            "ON CONFLICT(run_id) DO UPDATE SET status='partial', error=excluded.error, "
            " updated_at=datetime('now','localtime')",
            (run_id, domain_id, lesson_id, SCHEMA_VERSION, MERGE_PROMPT_VERSION,
             "deterministic_union_v1", f"sha256:empty:{domain_id}", blob, content_hash,
             "无 canonical 非噪声 span，无可理解内容"),
        )


def get_lesson_understanding(run_id: int) -> Optional[dict]:
    row = db.fetch_one("SELECT * FROM lesson_understandings WHERE run_id=?", (run_id,))
    if not row:
        return None
    out = dict(row)
    try:
        out["structured"] = json.loads(out.get("structured_json") or "{}")
    except Exception:
        out["structured"] = {}
    return out


def get_segment_understandings(domain_id: int) -> list[dict]:
    rows = db.fetch_all(
        "SELECT su.*, ls.ordinal FROM segment_understandings su "
        "JOIN lesson_segments ls ON ls.id = su.segment_id "
        "WHERE su.domain_id=? ORDER BY ls.ordinal", (domain_id,))
    out = []
    for row in rows:
        item = dict(row)
        try:
            item["structured"] = json.loads(item.get("structured_json") or "{}")
        except Exception:
            item["structured"] = {}
        out.append(item)
    return out
