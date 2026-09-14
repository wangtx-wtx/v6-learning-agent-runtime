"""V5.6.4 三个 Engine：Fake / Replay / Live。

设计原则：
- Fake / Replay **不允许** ``import httpx``；Live 独占 httpx。
- 13 个 contract 必须由调用方显式传入；未知 contract 立即 RuntimeError。
- fixture 启动时按对应 Pydantic Schema 校验；漂移即 fail。
- replay key 是完整 64-hex SHA-256，包含 messages / temperature / max_tokens /
  response_format / schema_version 等所有参数。
- LiveEngine 每次 HTTP 尝试（含重试）调用 ``note_attempt()``。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Optional

from .gateway_mode import (
    ReplayMissError,
    live_required_env,
    note_attempt,
    reserve_budget,
    resolve_mode,
    settle,
)

logger = logging.getLogger(__name__)


class GatewayReadTimeoutError(RuntimeError):
    """网关长请求在读取期限内没有完成；上层应立即切换候选模型。"""

RESOURCES_DIR = Path(__file__).resolve().parent / "resources"
FAKE_DIR = RESOURCES_DIR / "gateway_fake"

EMBED_DIM = 1024


def _load_schema_classes():
    """延迟导入 integrations.schemas（避免循环）。"""
    from .integrations.schemas import (
        LessonOutlineOut, NoteWriterOut, CriticOut,
        SolverOut, ParallelSolverOut, AdjudicatorOut, TeachingOut,
        OcrOut, VisionOut, AnalystOut, CrossCheckOut,
        ReviewWriterOut, SelfTestOut,
        SegmentUnderstandingOut, MergeUnderstandingOut, StudentSimulatorOut,
    )
    return {
        "lesson/lesson_outline": ("lesson_outline", LessonOutlineOut),
        "lesson/note_writer":    ("note_writer",    NoteWriterOut),
        "lesson/critic":         ("critic",         CriticOut),
        "homework/ocr":          ("ocr",            OcrOut),
        "homework/solver":       ("solver",         SolverOut),
        "homework/parallel_solver": ("parallel_solver", ParallelSolverOut),
        "homework/adjudicator":  ("adjudicator",    AdjudicatorOut),
        "homework/teaching":     ("teaching",       TeachingOut),
        "error/vision":          ("vision",         VisionOut),
        "error/analyst":         ("analyst",        AnalystOut),
        "error/cross_check":     ("cross_check",    CrossCheckOut),
        "review/writer":         ("review_writer",  ReviewWriterOut),
        "review/self_test":      ("self_test",      SelfTestOut),
        # V6 Phase 2：fake fixture 需按输入动态生成（否则无法引用真实 Source ID）
        "lesson/segment_understanding": ("lesson_segment_understanding",
                                        SegmentUnderstandingOut, "segment_understanding"),
        "lesson/merge_understanding":   ("lesson_merge_understanding",
                                        MergeUnderstandingOut, "merge_understanding"),
        # V6 Phase 3：认知分析同样按输入生成（无认知风险材料必须返回空 items）
        "lesson/student_simulator":     ("lesson_student_simulator",
                                        StudentSimulatorOut, "student_simulator"),
    }


CONTRACT_TABLE: dict[str, tuple] = _load_schema_classes()

#: 需要按输入动态构造 fixture 的 contract → 生成器名。
DYNAMIC_FIXTURES: dict[str, str] = {
    contract: spec[2] for contract, spec in CONTRACT_TABLE.items() if len(spec) > 2
}


def known_contracts() -> list[str]:
    """返回所有合法 contract（公开给诊断和前端）。"""
    return list(CONTRACT_TABLE.keys())


# ---------------------------------------------------------------------------
# Deterministic Fake Embedding（1024 维）
# ---------------------------------------------------------------------------
def deterministic_embed(text: str, dim: int = EMBED_DIM) -> list[float]:
    """SHA-256 → HMAC 派生 1024 维确定性向量；L2 归一化。

    性质：
      - 相同 text → 完全相同向量
      - 不同 text → 不同向量
      - 无 NaN / 无全零
      - 跨平台 / 跨进程一致
    """
    if text is None:
        text = ""
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    out: list[float] = []
    # 128 次迭代 × 8 floats = 1024
    for i in range(dim // 8):
        block = hmac.new(seed, i.to_bytes(2, "big"), hashlib.sha256).digest()
        for j in range(8):
            v = (block[2 * j] * 256 + block[2 * j + 1]) / 65535.0 - 0.5
            out.append(v)
    norm = math.sqrt(sum(x * x for x in out)) or 1.0
    return [x / norm for x in out]


# ---------------------------------------------------------------------------
# Fixture 加载（启动期 Pydantic 校验）
# ---------------------------------------------------------------------------
#: prompt 中「允许使用的 Source ID」清单（模板里显式列出，是唯一权威来源）。
#: 必须**同行**匹配：跨行会把 JSON Schema 示例里的示例 ID 也吞进来。
_ALLOWED_IDS_LINE = re.compile(
    r"(?:允许使用的 Source ID|allowed_source_ids)[^\S\n]*[:：][^\S\n]*([^\n]+)"
)


def _extract_allowed_source_ids(messages: list[dict]) -> list[str]:
    """从 prompt 中抽取**权威**的允许 Source ID 清单。

    必须优先读模板里显式列出的 `allowed_source_ids` 行，而不是全文正则扫描 ——
    因为 prompt 的 JSON Schema 示例里本身就含 ``T000012`` / ``P0017.01`` 这类
    示例 ID。早期实现扫全文，导致 fake fixture 引用了输入里并不存在的 ID，
    把一个本应正常的假通过变成 SourceRefViolationError。
    """
    sid_pattern = re.compile(r"\b([TPDI])(\d{4,6})(?:\.(\d{1,2}))?\b")
    for msg in messages or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, str):
            continue
        m = _ALLOWED_IDS_LINE.search(content)
        if m:
            ids = [x.group(0) for x in sid_pattern.finditer(m.group(1))]
            if ids:
                return list(dict.fromkeys(ids))
    # 回退：全文扫描（模板未显式列出清单时）
    return _extract_source_ids(messages)
    """从 prompt 中抽取出现的 Source ID（P0017.01 / T000128 / D0002.01 / I000001）。

    fake 引擎据此生成**只引用真实输入**的理解结果 —— 这也是
    ``test_illegal_source_id_fails_segment`` 之外的正常路径保障。
    """
    pattern = re.compile(r"\b([TPDI])(\d{4,6})(?:\.(\d{1,2}))?\b")
    seen: list[str] = []
    for msg in messages or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, str):
            continue
        for m in pattern.finditer(content):
            sid = m.group(0)
            if sid not in seen:
                seen.append(sid)
    return seen


def _extract_allowed_source_ids(messages: list[dict]) -> list[str]:
    """从 prompt 中抽取**权威**的允许 Source ID 清单。

    必须优先读模板里显式列出的 ``allowed_source_ids`` 行，而不是全文正则扫描 ——
    因为 prompt 的 JSON Schema 示例里本身就含 ``T000012`` / ``P0017.01`` 这类
    示例 ID。早期实现扫全文，导致 fake fixture 引用了输入里并不存在的 ID，
    把一个本应正常的假通过变成 SourceRefViolationError。
    """
    sid_pattern = re.compile(r"\b([TPDI])(\d{4,6})(?:\.(\d{1,2}))?\b")
    for msg in messages or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, str):
            continue
        m = _ALLOWED_IDS_LINE.search(content)
        if m:
            ids = [x.group(0) for x in sid_pattern.finditer(m.group(1))]
            if ids:
                return list(dict.fromkeys(ids))
    # 回退：全文扫描（模板未显式列出清单时）
    seen: list[str] = []
    for msg in messages or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, str):
            continue
        for m in sid_pattern.finditer(content):
            if m.group(0) not in seen:
                seen.append(m.group(0))
    return seen


def _dynamic_segment_understanding(messages: list[dict]) -> dict:
    """按输入 Source ID 生成单段理解 fixture（确定性、可复现）。

    **关键**：必须覆盖该 segment 的**全部** primary Source ID。若只随手挑几个，
    fake 模式下会产生「某些 span 没进入理解」的假象，让覆盖门禁的语义处理率
    与埋点断言失去意义（真实模型也应覆盖整段材料）。
    """
    refs = _extract_allowed_source_ids(messages)
    if not refs:
        return {
            "topics": [], "knowledge_units": [], "definitions": [], "formulas": [],
            "derivations": [], "examples": [], "teacher_emphasis": 0.0,
            "unresolved_points": [], "source_refs": [],
        }
    units = []
    for i, sid in enumerate(refs, 1):
        units.append({
            "temp_id": f"SU-{i:02d}",
            "topic": f"主题 {i}",
            "kind": "definition" if i == 1 else "concept",
            "summary": f"来自 {sid} 的课堂要点",
            "source_refs": [sid],
            "teacher_emphasis": round(max(0.5, 0.95 - 0.05 * i), 2),
            "relations": [],
        })
    return {
        "topics": [f"主题 {i}" for i in range(1, len(refs) + 1)],
        "knowledge_units": units,
        "definitions": ([{"term": "反射定律", "meaning": "入射角等于反射角",
                          "source_refs": [refs[0]]}] if refs else []),
        "formulas": ([{"expression": "i = r", "meaning": "反射角等于入射角",
                       "source_refs": [refs[0]]}] if refs else []),
        "derivations": [],
        "examples": [],
        "teacher_emphasis": 0.85,
        "unresolved_points": [],
        "source_refs": list(refs),
    }


def _dynamic_student_simulator(messages: list[dict]) -> dict:
    """按输入生成认知分析 fixture（确定性）。

    行为必须与真实模型语义一致：
      * 材料里**没有**明确认知风险线索（注意 / 容易混淆 / 省略一步 / 易错 /
        难点 / 记住）时返回**空 items** —— 不得为了模板完整编造 pitfall；
      * 出现明确线索时返回对应类型的 item，并只引用输入中的真实 Source ID 与
        KU_KEY。
    """
    import re as _re  # noqa: F401  (保留以免移除该 import 影响其它分支)

    user_text = ""
    for msg in reversed(messages or []):
        content = str(msg.get("content") or "")
        if "KU_KEY=" in content:
            user_text = content
            break
    ku_keys = list(dict.fromkeys(re.findall(r"KU_KEY=([^\s|]+)", user_text)))

    # Source ID 只认**权威清单行**（prompt 的 JSON 示例里含 T000012 这类示例 ID，
    # 全文扫描会把它当成真实来源，导致引用不存在的 ID 被剔除）。
    system_text = "\n".join(str(m.get("content") or "") for m in messages or []
                            if str(m.get("role")) == "system")
    m = _ALLOWED_IDS_LINE.search(system_text)
    if not m:
        m = _ALLOWED_IDS_LINE.search(
            "\n".join(str(x.get("content") or "") for x in messages or []))
    allowed: list[str] = []
    if m:
        allowed = list(dict.fromkeys(
            x.group(0) for x in re.finditer(
                r"\b([TPDI])(\d{4,6})(?:\.(\d{1,2}))?\b", m.group(1))))
    if not allowed or allowed == ["（无）"]:
        return {"items": [], "analysis_note": "没有可用的课堂 Source ID"}
    if not ku_keys:
        return {"items": [], "analysis_note": "没有可分析的 Knowledge Unit"}

    # 线索只从**真实输入数据**里找：本批课堂原文（`- [SRC=...]`）与 KU 行。
    # 绝不能拿 prompt 模板全文匹配：模板本身写着「容易混淆」「省略」等说明
    # 文字，会让「无认知风险」的材料被误判为有易错点 —— 等于凭模板编造 pitfall。
    data_lines = [ln for ln in user_text.splitlines()
                  if "KU_KEY=" in ln or ln.strip().startswith("- ")]
    evidence_text = "\n".join(data_lines)

    def _first() -> str:
        return allowed[0]

    # 明确线索 → 对应认知项类型（确定性关键词，仅用于 fake fixture）
    rules = [
        ("容易混淆", "pitfall", "容易混淆的点", "contrast", 0.8),
        ("易混淆", "pitfall", "容易混淆的点", "contrast", 0.8),
        ("易错", "pitfall", "容易混淆的点", "contrast", 0.75),
        ("注意", "emphasis", "老师明确强调", "none", 0.7),
        ("重点", "emphasis", "老师明确强调", "none", 0.7),
        ("省略", "missing_step", "讲解中省略的步骤", "explanation", 0.75),
        ("跳过", "missing_step", "讲解中省略的步骤", "explanation", 0.75),
        ("难点", "difficulty", "理解难度较大", "explanation", 0.6),
        ("记住", "memory_anchor", "记忆锚点", "memory_hook", 0.5),
    ]
    items: list[dict] = []
    seen: set[str] = set()
    for keyword, item_type, title, treatment, severity in rules:
        if keyword in evidence_text and item_type not in seen:
            seen.add(item_type)
            items.append({
                "type": item_type,
                "title": title,
                "explanation": f"输入数据中出现「{keyword}」，据此识别为 {item_type}",
                "severity": severity,
                "confidence": 0.7,
                "recommended_treatment": treatment,
                "origin": "classroom_evidence",
                "knowledge_unit_refs": ku_keys[:1],
                "source_refs": [_first()],
            })
    return {"items": items, "analysis_note": "基于输入线索的确定性 fake 认知分析"}


def _dynamic_merge_understanding(messages: list[dict]) -> dict:
    """按输入生成合并 fixture（保留全部 Source ID，不新增事实）。"""
    refs = _extract_allowed_source_ids(messages)
    units = []
    for i, sid in enumerate(refs[:4], 1):
        units.append({
            "temp_id": f"KU-{i:03d}",
            "topic": f"合并主题 {i}",
            "kind": "concept",
            "summary": f"由 {sid} 支持",
            "source_refs": [sid],
            "teacher_emphasis": 0.8,
            "relations": [],
        })
    return {
        "topics": [f"合并主题 {i}" for i in range(1, min(len(refs), 4) + 1)] or ["合并主题 1"],
        "knowledge_units": units,
        "unresolved_conflicts": [],
        "teacher_emphasis": 0.85,
    }


_DYNAMIC_FIXTURE_BUILDERS = {
    "segment_understanding": _dynamic_segment_understanding,
    "merge_understanding": _dynamic_merge_understanding,
    "student_simulator": _dynamic_student_simulator,
}


def _load_fixture(contract: str, messages: Optional[list[dict]] = None) -> dict:
    if contract not in CONTRACT_TABLE:
        raise RuntimeError(
            f"未知 contract: {contract!r}（合法: {sorted(CONTRACT_TABLE)}）"
        )
    spec = CONTRACT_TABLE[contract]
    name, schema_cls = spec[0], spec[1]
    builder_name = DYNAMIC_FIXTURES.get(contract)
    if builder_name:
        # 动态 fixture：必须由输入推导，否则无法保证只引用真实 Source ID
        data = _DYNAMIC_FIXTURE_BUILDERS[builder_name](messages or [])
        schema_cls.model_validate(data)
        return data
    p = FAKE_DIR / f"{name}.json"
    if not p.exists():
        raise FileNotFoundError(f"fake fixture 缺失: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    # 启动期校验；fixture 漂移即 fail，不允许静默降级
    schema_cls.model_validate(data)
    return data


def validate_all_fixtures_at_import() -> None:
    """模块导入时一次性校验全部静态 fixture；任一失败即抛。

    动态 fixture（Phase 2）在无输入时也必须能生成合法结果，因此一并校验。
    """
    for c in CONTRACT_TABLE:
        if c in DYNAMIC_FIXTURES:
            continue
        try:
            _load_fixture(c)
        except Exception as e:
            raise RuntimeError(
                f"fake fixture 校验失败: contract={c} error={e}"
            ) from e
    for c in DYNAMIC_FIXTURES:
        try:
            _load_fixture(c, [{"role": "user", "content": "T000001 P0001.01"}])
        except Exception as e:
            raise RuntimeError(
                f"动态 fake fixture 校验失败: contract={c} error={e}"
            ) from e


# ---------------------------------------------------------------------------
# FakeEngine
# ---------------------------------------------------------------------------
class FakeEngine:
    """零网络、零 Token。返回的 JSON 必须通过对应 Pydantic Schema。"""

    def __init__(self):
        # 启动时一次性校验全部 fixture
        validate_all_fixtures_at_import()

    async def chat(self, contract, model_id, messages, temperature, max_tokens,
                   response_format) -> dict:
        data = _load_fixture(contract, messages)
        return {
            "content": json.dumps(data, ensure_ascii=False),
            "reasoning_content": "",
            "tokens_in": 0,
            "tokens_out": 0,
            "model": f"fake::{model_id}",
            "elapsed_ms": 0,
        }

    async def embedding_single(self, text: str) -> list[float]:
        return deterministic_embed(text)

    async def rerank(self, query: str, documents: list[str], top_n: int | None = None) -> list[dict]:
        ranked = sorted(enumerate(documents), key=lambda pair: len(set(query) & set(pair[1])), reverse=True)
        return [{"index": i, "relevance_score": 1.0 / (rank + 1)} for rank, (i, _) in enumerate(ranked[:top_n or len(ranked)])]

    async def ocr_document(self, file_value: str) -> dict:
        return {"text": "", "model": "fake::glm-ocr"}

    async def get_usage(self) -> dict:
        # fake 永远 0 用量；不触发 routing 保守模式
        return {
            "available": True,
            "data": {"used_5h": 0, "limit_5h": 1200},
        }

    async def aclose(self) -> None:
        pass


# ---------------------------------------------------------------------------
# ReplayEngine
# ---------------------------------------------------------------------------
def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)


def compute_replay_key(operation, model_id, contract, prompt_name,
                       prompt_checksum, messages, temperature, max_tokens,
                       response_format, schema_name, schema_version) -> str:
    """完整 64-hex SHA-256，包含所有影响结果的字段。

    任一字段变化 → key 变化 → miss → 自动重新录制（用户显式授权）。
    """
    payload = {
        "op": operation,
        "model": model_id,
        "contract": contract,
        "pn": prompt_name,
        "pc": prompt_checksum,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": response_format,
        "schema": schema_name,
        "sv": schema_version,
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


class ReplayEngine:
    """命中夹具 → 返回；miss → 抛 ReplayMissError（绝不回退 live）。"""

    def __init__(self, replay_dir: Path):
        self.dir = Path(replay_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _key_path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    async def chat(self, contract, model_id, messages, temperature, max_tokens,
                   response_format) -> dict:
        from .gateway import _call_context  # 避免循环

        ctx = _call_context.get() or {}
        key = compute_replay_key(
            "chat", model_id, contract,
            ctx.get("prompt_name") or "",
            ctx.get("prompt_checksum") or "",
            messages, temperature, max_tokens, response_format,
            ctx.get("schema_name") or "",
            ctx.get("schema_version") or "v1",
        )
        p = self._key_path(key)
        if not p.exists():
            raise ReplayMissError(
                f"replay miss: contract={contract} key={key[:12]}"
            )
        env = json.loads(p.read_text(encoding="utf-8"))
        # 二次校验 digest 与 schema
        if env.get("request_digest") != key:
            raise ReplayMissError(
                f"replay 夹具 digest 不一致: {p.name}"
            )
        if contract not in CONTRACT_TABLE:
            raise ReplayMissError(
                f"replay 夹具 contract 不在注册表: {contract}"
            )
        _, schema_cls = CONTRACT_TABLE[contract]
        try:
            schema_cls.model_validate(env["response"])
        except Exception as e:
            raise ReplayMissError(
                f"replay 夹具 schema 校验失败: {e}"
            ) from e
        return {
            "content": json.dumps(env["response"], ensure_ascii=False),
            "reasoning_content": "",
            "tokens_in": 0,
            "tokens_out": 0,
            "model": f"replay::{model_id}",
            "elapsed_ms": 0,
            "replay_key": key,
            "replay_hit": True,
        }

    async def embedding_single(self, text: str) -> list[float]:
        return deterministic_embed(text)

    async def rerank(self, query: str, documents: list[str], top_n: int | None = None) -> list[dict]:
        ranked = sorted(enumerate(documents), key=lambda pair: len(set(query) & set(pair[1])), reverse=True)
        return [{"index": i, "relevance_score": 1.0 / (rank + 1)} for rank, (i, _) in enumerate(ranked[:top_n or len(ranked)])]

    async def ocr_document(self, file_value: str) -> dict:
        raise ReplayMissError("OCR 专用端点尚无 replay 夹具")

    async def get_usage(self) -> dict:
        return {
            "available": True,
            "data": {"used_5h": 0, "limit_5h": 1200},
        }

    async def aclose(self) -> None:
        pass


# ---------------------------------------------------------------------------
# LiveEngine —— 唯一允许 httpx
# ---------------------------------------------------------------------------
class LiveEngine:
    """保留 V5.5.1 httpx 链 + 熔断 + 重试语义；每次 HTTP 尝试计 attempt。"""

    def __init__(self, base_url, api_key, timeout):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._client = None  # 懒加载；只有真要发请求时才 new httpx

    def _ensure_client(self):
        # 注意：这是 LiveEngine 内部唯一可以 new httpx.AsyncClient 的地方
        if self._client is None:
            try:
                import httpx  # noqa: WPS433
            except ImportError as e:
                raise RuntimeError("live 模式需要 httpx") from e
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=15.0, read=self.timeout,
                                      write=30.0, pool=10.0),
                limits=httpx.Limits(max_connections=20,
                                    max_keepalive_connections=10),
            )
        return self._client

    def _headers(self, trace: str = "") -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        if trace:
            h["X-Trace-Id"] = trace
        return h

    @staticmethod
    def _trace_id() -> str:
        import uuid
        return "tr-" + uuid.uuid4().hex[:12]

    async def _post_with_retry(self, path, payload, *, read_timeout: Optional[float] = None):
        # 简易重试：429/5xx 指数退避；每次都 note_attempt
        try:
            import httpx  # noqa: WPS433
        except ImportError as e:
            raise RuntimeError("live 模式需要 httpx") from e

        client = self._ensure_client()
        last_err: Optional[BaseException] = None
        for attempt in range(3):
            note_attempt()
            try:
                timeout_s = float(read_timeout or self.timeout)
                resp = await client.post(
                    self.base_url + path,
                    json=payload,
                    headers=self._headers(self._trace_id()),
                    timeout=httpx.Timeout(connect=15.0, read=timeout_s,
                                          write=30.0, pool=10.0),
                )
            except httpx.TimeoutException as e:
                # 超时请求通常已在上游执行；原样重放既浪费额度又把等待扩大三倍。
                raise GatewayReadTimeoutError(
                    f"网关读取超时（{timeout_s:.0f}s），应缩小分段或切换候选模型"
                ) from e
            except httpx.TransportError as e:
                last_err = e
                await asyncio.sleep(min(0.6 * (2 ** attempt), 6.0))
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                # 必须记录 last_err：否则重试耗尽后错误消息为空，无法诊断。
                last_err = RuntimeError(
                    f"网关返回 HTTP {resp.status_code}: {resp.text[:200]}"
                )
                await asyncio.sleep(min(0.6 * (2 ** attempt), 6.0))
                continue
            return resp
        # httpx 超时异常的 str() 常为空，补上异常类型名便于诊断。
        raise RuntimeError(
            f"live 网关重试 3 次仍失败: {type(last_err).__name__}: {last_err}"
        )

    async def chat(self, contract, model_id, messages, temperature, max_tokens,
                   response_format) -> dict:
        from .dag import (  # 延迟导入；统一错误基类
            AuthError, BadRequestError, RetryableModelError,
        )
        from .models_registry import get_model

        # 1) 预算预检（必须发生在网络前）
        msgs_text = json.dumps(messages, ensure_ascii=False, default=str)
        reserve_budget(len(msgs_text), int(max_tokens or 4096),
                       label=f"chat:{contract}")
        spec = get_model(model_id)
        payload = {
            "model": spec.gateway_model, "messages": messages,
            "temperature": temperature, "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if response_format:
            payload["response_format"] = response_format
        started = time.time()
        read_timeout = self.timeout
        if contract == "lesson/segment_understanding":
            raw_timeout = (os.environ.get("V6_SEGMENT_GATEWAY_TIMEOUT") or "600").strip()
            try:
                read_timeout = max(30.0, min(float(raw_timeout), 600.0))
            except ValueError:
                read_timeout = 600.0
        resp = await self._post_with_retry(
            "/v1/chat/completions", payload, read_timeout=read_timeout)
        if resp.status_code in (401, 403):
            raise AuthError(f"live 鉴权失败: {resp.status_code}")
        if resp.status_code == 400:
            raise BadRequestError(f"live 400 非法: {resp.text[:200]}")
        if resp.status_code >= 500 or resp.status_code == 429:
            raise RetryableModelError(f"live 不可恢复: {resp.status_code}")
        data = resp.json()
        usage = data.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens", 0) or 0)
        tokens_out = int(usage.get("completion_tokens", 0) or 0)
        settle(tokens_in, tokens_out)
        content = (
            (data.get("choices") or [{}])[0]
            .get("message", {})
            .get("content", "")
            or ""
        )
        return {
            "content": content,
            "reasoning_content": "",
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "model": spec.gateway_model,
            "elapsed_ms": int((time.time() - started) * 1000),
        }

    async def embedding_single(self, text: str) -> list[float]:
        from .models_registry import get_model
        reserve_budget(len(text or ""), 0, label="embedding")
        spec = get_model("embedding")
        resp = await self._post_with_retry(
            "/v1/embeddings",
            {"model": spec.gateway_model, "input": text},
        )
        note_attempt()
        return (resp.json().get("data") or [{}])[0].get("embedding", [])

    async def rerank(self, query: str, documents: list[str], top_n: int | None = None) -> list[dict]:
        from .models_registry import get_model
        reserve_budget(len(query) + sum(len(x) for x in documents), 0, label="rerank")
        spec = get_model("rerank")
        payload = {"model": spec.gateway_model, "query": query, "documents": documents}
        if top_n:
            payload["top_n"] = top_n
        resp = await self._post_with_retry("/v1/rerank", payload)
        note_attempt()
        data = resp.json()
        return data.get("results") or []

    async def ocr_document(self, file_value: str) -> dict:
        reserve_budget(len(file_value), 4096, label="ocr")
        resp = await self._post_with_retry("/v1/ocr", {"model": "glm-ocr", "file": file_value})
        note_attempt()
        return resp.json()

    async def get_usage(self) -> dict:
        try:
            import httpx  # noqa: WPS433
        except ImportError as e:
            raise RuntimeError("live 模式需要 httpx") from e
        note_attempt()
        client = self._ensure_client()
        resp = await client.get(
            self.base_url + "/admin/api/usage",
            headers=self._headers(self._trace_id()),
        )
        note_attempt()
        if resp.status_code == 404:
            return {"available": False, "detail": "端点不存在"}
        if resp.status_code >= 500 or resp.status_code == 429:
            return {"available": False, "error": f"http_{resp.status_code}"}
        try:
            data = resp.json()
        except Exception:
            return {"available": False, "error": "invalid_json"}
        data.setdefault("available", True)
        return data

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as e:
                logger.warning("live httpx 关闭失败: %s", e)
            self._client = None
