"""V6 Source Map：为 domain 内 source span 分配**稳定** Source ID。

Source ID 规则（设计文档 §6.2）::

    转写     T + 六位顺序号        T000001
    PPT      P + 四位页码 + .NN    P0017.01
    PDF/讲义 D + 四位页码 + .NN    D0017.01
    图片 OCR I + 六位顺序号        I000001

稳定性要求:

* ID 在 Material Domain 冻结后不可重排；同一 domain version 重跑必须得到
  完全相同的 ID 集合。
* 因此分配只依赖**域内确定顺序**（domain_item ordinal → span ordinal），
  不依赖数据库自增 id、时间戳或并发顺序。
* Source ID 冲突必须**明确失败**（``SourceIdCollisionError``），绝不静默改写。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from .contracts import SourceKind

SEQ_PREFIX = {
    SourceKind.TRANSCRIPT: "T",
    SourceKind.OCR: "I",
    # 讲义正文段落 / 其它文本：没有页码可言，按域内顺序编号。
    # 设计文档 §6.2 只规定了 T/P/D/I 四种；TEXT/DOC 段落归入顺序编号是
    # Phase 1 的保守扩展，避免出现「无法定位」的 span 没有稳定 ID。
    SourceKind.TEXT: "T",
    SourceKind.DOC: "T",
    SourceKind.UNKNOWN: "T",
}

PAGE_PREFIX = {
    SourceKind.PPT: "P",
    SourceKind.PDF: "D",
    # 讲义在有页码元数据时按页编号（D0017.01）；无页码时回退顺序编号（T000001）。
    SourceKind.DOC: "D",
}

#: 保留给「既非顺序也非分页」的未来来源类型；当前无来源命中（UNKNOWN 已归 T）。
FALLBACK_PREFIX = "S"

SEQ_WIDTH = 6
PAGE_WIDTH = 4

SOURCE_ID_PATTERN = re.compile(
    r"^(?P<prefix>[TPDIS])(?P<body>\d{4,6})(?:\.(?P<seg>\d{1,2}))?$"
)


class SourceIdCollisionError(RuntimeError):
    """同一 domain 内两个不同 span 被分配到同一 Source ID（必须失败）。"""


class SourceIdFormatError(ValueError):
    """Source ID 不符合规则（解析 source_id 时使用）。"""


@dataclass(frozen=True)
class SourceIdParts:
    prefix: str
    body: str
    segment: Optional[int]

    @property
    def kind(self) -> str:
        return "sequence" if self.prefix in ("T", "I", "S") else "paged"


def format_sequence_source_id(prefix: str, seq: int) -> str:
    if seq < 1:
        raise SourceIdFormatError(f"顺序号必须 >= 1: {seq}")
    if seq >= 10 ** SEQ_WIDTH:
        raise SourceIdFormatError(f"顺序号超出 {SEQ_WIDTH} 位上限: {seq}")
    return f"{prefix}{seq:0{SEQ_WIDTH}d}"


def format_paged_source_id(prefix: str, page_no: int, segment: int = 1) -> str:
    if page_no < 1:
        raise SourceIdFormatError(f"页码必须 >= 1: {page_no}")
    if page_no >= 10 ** PAGE_WIDTH:
        raise SourceIdFormatError(f"页码超出 {PAGE_WIDTH} 位上限: {page_no}")
    if segment < 1:
        raise SourceIdFormatError(f"页内段号必须 >= 1: {segment}")
    if segment > 99:
        raise SourceIdFormatError(f"页内段号超出两位上限: {segment}")
    return f"{prefix}{page_no:0{PAGE_WIDTH}d}.{segment:02d}"


def parse_source_id(source_id: str) -> SourceIdParts:
    """解析 Source ID。格式非法时抛 ``SourceIdFormatError``（不猜测）。"""
    if not source_id:
        raise SourceIdFormatError("source_id 为空")
    m = SOURCE_ID_PATTERN.match(str(source_id).strip())
    if not m:
        raise SourceIdFormatError(f"source_id 不符合规则: {source_id!r}")
    seg = m.group("seg")
    return SourceIdParts(m.group("prefix"), m.group("body"), int(seg) if seg else None)


def prefix_for(
    source_kind: SourceKind,
    page_no: Optional[int] = None,
    slide_no: Optional[int] = None,
) -> tuple[str, bool]:
    """返回 ``(prefix, is_paged)``。

    有页码/幻灯片号 → 按页编号（``P0017.01`` / ``D0017.01``）；
    否则按来源顺序编号（``T000001`` / ``I000001``）。
    """
    if source_kind in PAGE_PREFIX:
        page = page_no if page_no is not None else slide_no
        if page is not None:
            return PAGE_PREFIX[source_kind], True
    if source_kind in SEQ_PREFIX:
        return SEQ_PREFIX[source_kind], False
    return FALLBACK_PREFIX, False


@dataclass(frozen=True)
class SourceIdRequest:
    """分配请求。``ordinal`` 越小越先分配；分配只依赖该顺序。"""

    ordinal: int
    source_kind: SourceKind
    page_no: Optional[int] = None
    slide_no: Optional[int] = None
    label: str = ""


def assign_source_ids(requests: Iterable[SourceIdRequest]) -> dict[int, str]:
    """按确定顺序为每个 ``ordinal`` 分配 Source ID。

    返回 ``{ordinal: source_id}``。同一 ``(prefix, page)`` 下的多个 span 依次
    获得 ``.01`` / ``.02``；无页码 span 按来源类型独立顺序编号。

    冲突（同一 ordinal 重复请求，或同页段号溢出）→ ``SourceIdCollisionError``。
    """
    ordered = sorted(requests, key=lambda r: r.ordinal)
    out: dict[int, str] = {}
    used: dict[str, int] = {}
    seq_counters: dict[str, int] = {}
    page_counters: dict[tuple[str, int], int] = {}

    for req in ordered:
        if req.ordinal in out:
            raise SourceIdCollisionError(f"ordinal={req.ordinal} 被重复请求")
        prefix, is_paged = prefix_for(req.source_kind, req.page_no, req.slide_no)
        if is_paged:
            page = req.page_no if req.page_no is not None else req.slide_no
            # 同页多个 span 使用 .01/.02...，第一段也带后缀（规则 P0017.01）。
            key = (prefix, int(page))
            seg = page_counters.get(key, 0) + 1
            page_counters[key] = seg
            source_id = format_paged_source_id(prefix, int(page), seg)
        else:
            seq = seq_counters.get(prefix, 0) + 1
            seq_counters[prefix] = seq
            source_id = format_sequence_source_id(prefix, seq)
        if source_id in used:
            raise SourceIdCollisionError(
                f"Source ID 冲突: {source_id} 同时被 ordinal={used[source_id]} 与 "
                f"ordinal={req.ordinal} 使用"
            )
        used[source_id] = req.ordinal
        out[req.ordinal] = source_id
    return out


def allocate_terminal_source_id(source_kind: SourceKind, used: set[str]) -> str:
    """为「材料级终态占位 span」分配一个不与既有 ID 冲突的顺序 Source ID。

    这类 span 没有页码/时间轴，但仍必须拥有**稳定且带来源前缀**的 Source ID
    （如失败 PDF 为 ``D000001``），否则覆盖报告里会混入无来源语义的 ``S…`` 编号。
    ``used`` 为本次已分配集合，函数会就地登记新 ID。
    """
    prefix, _ = prefix_for(source_kind)
    seq = 1
    while True:
        candidate = format_sequence_source_id(prefix, seq)
        if candidate not in used:
            used.add(candidate)
            return candidate
        seq += 1


def source_id_sort_key(source_id: str) -> tuple:
    """稳定排序键（报告与账本输出用，避免字典序把 T000010 排在 T000009 前）。"""
    parts = parse_source_id(source_id)
    return (parts.prefix, int(parts.body), parts.segment or 0)
