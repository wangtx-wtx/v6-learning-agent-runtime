"""材料规范化（V6 Phase 1）: 文本、locator、时间轴、页码、噪声识别。

设计约束（任务书 §六）:

* 噪声识别只做**保守**规则：空文本、纯分隔符、明确的解析失败标记、
  明确的占位符。不确定的一律按有效内容处理。
* 被判为噪声的内容必须写 reason_code，不允许静默丢弃。
* 不为了提高覆盖率而把空文本、解析错误或占位信息伪装成有效内容。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional

from .contracts import ItemReason, SourceKind, SpanReason

# ---------------------------------------------------------------------------
# 文本规范化
# ---------------------------------------------------------------------------
#: 零宽 / 方向控制字符：只影响哈希稳定性，不承载内容。
_ZERO_WIDTH = re.compile("[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
#: 制表、换页、回车等统一折叠。
_WS_RUN = re.compile(r"[ \t\r\f\v\u00a0\u3000]+")
_BLANK_RUN = re.compile(r"\n{3,}")
#: 英文软连字符换行（PDF 抽取常见）。
_SOFT_HYPHEN = re.compile(r"[A-Za-z]-\n[A-Za-z]")
#: 中文标点与字符之间的空白属于抽取噪声（「光 的 反 射」→「光的反射」）。
_CJK_GAP = re.compile(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])")


def normalize_text(text: Optional[str]) -> str:
    """规范化文本：去零宽、统一换行、折叠空白、修 PDF 软连字符。

    只做「形态」归一，**不删减任何实义内容**，也不补造内容。
    """
    if not text:
        return ""
    out = str(text).replace("\r\n", "\n").replace("\r", "\n")
    out = _ZERO_WIDTH.sub("", out)
    out = _SOFT_HYPHEN.sub(lambda m: m.group(0)[0] + m.group(0)[2], out)
    out = _WS_RUN.sub(" ", out)
    out = "\n".join(line.strip() for line in out.split("\n"))
    out = _CJK_GAP.sub("", out)
    out = _BLANK_RUN.sub("\n\n", out)
    return out.strip()


def hash_text(normalized: str) -> str:
    """规范化文本哈希（用于 duplicates 判定，域内稳定）。"""
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def hash_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# token 估算
# ---------------------------------------------------------------------------
_CJK = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_WORD = re.compile(r"[A-Za-z0-9]+")


def estimate_tokens(text: Optional[str], normalized: Optional[str] = None) -> int:
    """确定性 token 估算（无外部 tokenizer，保证可比与可复现）。

    公式：``cjk_chars + ceil(other_chars / 4)``。中文一字约一 token；
    ASCII 约 4 字符一 token。不用模型统计，避免网络依赖与非确定性。
    """
    src = normalized if normalized is not None else normalize_text(text)
    if not src:
        return 0
    cjk = len(_CJK.findall(src))
    other = len(src) - cjk
    return int(cjk + -(-other // 4))


def char_count(text: Optional[str]) -> int:
    """规范化后的字符数（与 token 估算口径一致）。"""
    return len(normalize_text(text))


def raw_char_count(text: Optional[str]) -> int:
    """原始字符数（未规范化），用于报告材料体量。"""
    return len(str(text)) if text else 0


# ---------------------------------------------------------------------------
# source_kind 推断
# ---------------------------------------------------------------------------
_SUFFIX_KIND = {
    ".pdf": SourceKind.PDF,
    ".ppt": SourceKind.PPT,
    ".pptx": SourceKind.PPT,
    ".doc": SourceKind.DOC,
    ".docx": SourceKind.DOC,
    ".md": SourceKind.TEXT,
    ".txt": SourceKind.TEXT,
    ".srt": SourceKind.TRANSCRIPT,
    ".vtt": SourceKind.TRANSCRIPT,
    ".json": SourceKind.TRANSCRIPT,
    ".mp3": SourceKind.TRANSCRIPT,
    ".m4a": SourceKind.TRANSCRIPT,
    ".wav": SourceKind.TRANSCRIPT,
    ".png": SourceKind.OCR,
    ".jpg": SourceKind.OCR,
    ".jpeg": SourceKind.OCR,
    ".webp": SourceKind.OCR,
    ".gif": SourceKind.OCR,
}

_KIND_ALIASES = {
    "transcript": SourceKind.TRANSCRIPT,
    "audio": SourceKind.TRANSCRIPT,
    "srt": SourceKind.TRANSCRIPT,
    "ppt": SourceKind.PPT,
    "pptx": SourceKind.PPT,
    "slides": SourceKind.PPT,
    "pdf": SourceKind.PDF,
    "handout": SourceKind.DOC,
    "doc": SourceKind.DOC,
    "docx": SourceKind.DOC,
    "text": SourceKind.TEXT,
    "markdown": SourceKind.TEXT,
    "md": SourceKind.TEXT,
    "image": SourceKind.OCR,
    "ocr": SourceKind.OCR,
}


def infer_source_kind(kind: Optional[str] = None, mime: Optional[str] = None,
                      name: Optional[str] = None) -> SourceKind:
    """从材料 kind / mime / 文件名推断 SourceKind（保守：无法判定 → unknown）。"""
    for raw in (kind, mime):
        if not raw:
            continue
        value = str(raw).strip().lower()
        if value in _KIND_ALIASES:
            return _KIND_ALIASES[value]
        if "/" in value:  # mime
            subtype = value.split("/", 1)[1].split(";")[0].strip()
            if subtype in _KIND_ALIASES:
                return _KIND_ALIASES[subtype]
    if name:
        lowered = str(name).strip().lower()
        for suffix, sk in _SUFFIX_KIND.items():
            if lowered.endswith(suffix):
                return sk
    return SourceKind.UNKNOWN


def is_supported_kind(source_kind: SourceKind) -> tuple[bool, Optional[ItemReason]]:
    """类型支持矩阵（与 material_parser 一致，不伪装 ready）。"""
    if source_kind in (SourceKind.OCR,):
        return False, ItemReason.UNSUPPORTED_PARSE_STATE
    if source_kind == SourceKind.TRANSCRIPT:
        # 音频转写由 material_parser 负责；未转写完成的音频不是音频内容本身。
        return True, None
    if source_kind == SourceKind.UNKNOWN:
        return False, ItemReason.UNSUPPORTED_FILE_TYPE
    return True, None


# ---------------------------------------------------------------------------
# locator / 时间轴 / 页码
# ---------------------------------------------------------------------------
_TRANSCRIPT_TS = re.compile(
    r"\[?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?(?:[.,](\d{1,3}))?\s*\]?"
)


@dataclass(frozen=True)
class LocatorInfo:
    """解析后的 locator 结构。未识别的形态保持 ``kind='unknown'`` 且不改写。"""

    kind: str                       # transcript | page | slide | body | segment | unknown
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None
    page_no: Optional[int] = None
    slide_no: Optional[int] = None
    normalized: Optional[str] = None


def _ts_to_ms(h: str, m: str, s: Optional[str], frac: Optional[str]) -> int:
    hours = int(h)
    minutes = int(m)
    seconds = int(s) if s is not None else 0
    millis = 0
    if frac:
        millis = int((frac + "000")[:3])
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def _ms_to_ts(ms: int) -> str:
    ms = max(int(ms), 0)
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    if millis:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def parse_transcript_locator(locator: Optional[str]) -> Optional[tuple[int, int]]:
    """解析转写时间范围 locator，返回 ``(start_ms, end_ms)``。

    支持 ``00:31:18-00:31:51`` / ``00:31:18.500--00:31:51`` / ``[00:31:18 - 00:31:51]``。
    解析不出唯一区间时返回 ``None``（不猜测）。
    """
    if not locator:
        return None
    text = str(locator).strip()
    if not text:
        return None
    hits = list(_TRANSCRIPT_TS.finditer(text))
    if not hits:
        return None
    values = [_ts_to_ms(m.group(1), m.group(2), m.group(3), m.group(4)) for m in hits]
    if len(values) == 1:
        return values[0], values[0]
    start, end = values[0], values[1]
    if end < start:
        return None
    return start, end


def parse_page_locator(locator: Optional[str]) -> tuple[Optional[int], Optional[int], Optional[str]]:
    """解析 ``page:N`` / ``slide:N`` locator。

    返回 ``(page_no, slide_no, kind)``；``kind`` 为 ``page`` / ``slide`` / ``None``。
    """
    if not locator:
        return None, None, None
    text = str(locator).strip().lower()
    m = re.fullmatch(r"page[:# ]?\s*(\d+)", text)
    if m:
        return int(m.group(1)), None, "page"
    m = re.fullmatch(r"slide[:# ]?\s*(\d+)", text)
    if m:
        return None, int(m.group(1)), "slide"
    return None, None, None


def parse_locator(locator: Optional[str], source_kind: SourceKind) -> LocatorInfo:
    """按来源类型解析 locator，并把 locator 规范化到稳定文本形态。

    规范化只**收敛同义写法**（如 ``page:7`` / ``Page 7`` → ``page:7``），
    不改变 locator 的语义，也不凭空补造页码或时间轴。
    """
    raw = (str(locator).strip() if locator else "")
    page_no, slide_no, kind = parse_page_locator(raw)
    if kind == "page":
        return LocatorInfo(kind="page", page_no=page_no, normalized=f"page:{page_no}")
    if kind == "slide":
        return LocatorInfo(kind="slide", slide_no=slide_no, normalized=f"slide:{slide_no}")
    if raw.lower() in ("body", "full", "text"):
        return LocatorInfo(kind="body", normalized="body")
    m = re.fullmatch(r"seg[:# ]?\s*(\d+)", raw, re.IGNORECASE)
    if m:
        return LocatorInfo(kind="segment", normalized=f"seg:{int(m.group(1))}")
    m = re.fullmatch(r"cue[:# ]?\s*(\d+)", raw, re.IGNORECASE)
    if m:
        return LocatorInfo(kind="cue", normalized=f"cue:{int(m.group(1))}")
    rng = parse_transcript_locator(raw)
    if rng is not None:
        start, end = rng
        # 同一 cue 切出的片段带 `#n` 后缀：时间范围共享，locator 保持唯一
        part = ""
        m_part = re.search(r"#(\d+)\s*$", raw)
        if m_part:
            part = f"#{int(m_part.group(1)):02d}"
        return LocatorInfo(kind="transcript", start_ms=start, end_ms=end,
                           normalized=f"{_ms_to_ts(start)}-{_ms_to_ts(end)}{part}")
    if source_kind == SourceKind.TRANSCRIPT:
        return LocatorInfo(kind="unknown", normalized=raw or None)
    return LocatorInfo(kind="unknown", normalized=raw or None)


# ---------------------------------------------------------------------------
# 噪声识别（保守规则）
# ---------------------------------------------------------------------------
_PLACEHOLDER_PATTERNS = (
    re.compile(r"^[\.\-_=*~#·—–\s]+$"),
    re.compile(r"^[\[\(（【]\s*(空|无|略|图片|image|figure|table|图表|公式|equation|待补充|todo|tbd)\s*[\]\)）】]$",
               re.IGNORECASE),
    re.compile(r"^(无内容|没有内容|此页无文字|no content|empty|n/?a)$", re.IGNORECASE),
)
_PARSE_MARKER_PATTERNS = (
    re.compile(r"^\[[^\]\n]{0,40}解析失败[^\]\n]{0,120}\]$"),
    re.compile(r"^\[[^\]\n]{0,40}parse (failed|error)[^\]\n]{0,120}\]$", re.IGNORECASE),
    re.compile(r"^error:\s*.{0,120}$", re.IGNORECASE),
)


@dataclass(frozen=True)
class NoiseVerdict:
    is_noise: bool
    reason_code: Optional[SpanReason] = None
    detail: Optional[str] = None


_NO_NOISE = NoiseVerdict(False)


def classify_noise(text: Optional[str], normalized: Optional[str] = None) -> NoiseVerdict:
    """保守噪声识别。

    只判定三类明确无内容的情形，且**必须**给出受控 reason_code：

    1. 空 / 纯空白（``noise_empty_text``）
    2. 纯分隔符或明确占位符（``noise_placeholder``）
    3. 明确的解析失败标记（``noise_parse_marker``）

    任何含实义文字的内容一律不是噪声 —— 宁可少判噪声，不可把内容判成噪声。
    """
    raw = "" if text is None else str(text)
    norm = normalized if normalized is not None else normalize_text(raw)
    if not norm.strip():
        return NoiseVerdict(True, SpanReason.NOISE_EMPTY_TEXT, "规范化后为空")
    if len(norm) > 200:
        # 长文本不可能是占位符；避免正则回溯，也避免误判。
        return _NO_NOISE
    for pat in _PLACEHOLDER_PATTERNS:
        if pat.match(norm):
            return NoiseVerdict(True, SpanReason.NOISE_PLACEHOLDER, f"占位内容: {norm[:40]}")
    for pat in _PARSE_MARKER_PATTERNS:
        if pat.match(norm):
            return NoiseVerdict(True, SpanReason.NOISE_PARSE_MARKER, f"解析标记: {norm[:40]}")
    return _NO_NOISE


def looks_like_parse_marker(text: Optional[str]) -> bool:
    """材料解析阶段写入的伪内容标记（``[docx 解析失败: ...]``）。"""
    if not text:
        return False
    value = str(text).strip()
    if len(value) > 300:
        return False
    for pat in _PARSE_MARKER_PATTERNS:
        if pat.match(value):
            return True
    return bool(re.match(r"^\[(docx|pdf|pptx?)\s*解析失败[:：]", value))


# ---------------------------------------------------------------------------
# 转写 cue 解析（Phase 2：稳定 start_ms/end_ms）
# ---------------------------------------------------------------------------
#: 独立的 cue 时间戳行：`[00:12:34]` / `00:12:34` / `[00:12:34 - 00:13:10]` / `12:34`。
#: 允许前导 `[`/`(`，允许 `-->` 或 `-` 或 `–` 作为区间分隔。
_CUE_LINE = re.compile(
    r"^\s*[\[\(（]?\s*"
    r"(?P<a>\d{1,3}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)"
    r"(?:\s*(?:-{1,2}>|[-–—~])\s*"
    r"(?P<b>\d{1,3}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?))?"
    r"\s*[\]\)）]?\s*$"
)
#: cue 时间戳前缀 + 同行正文：`[00:12:34] 老师开始讲……`
_CUE_INLINE = re.compile(
    r"^\s*[\[\(（]?\s*"
    r"(?P<a>\d{1,3}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)"
    r"(?:\s*(?:-{1,2}>|[-–—~])\s*"
    r"(?P<b>\d{1,3}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?))?"
    r"\s*[\]\)）]?\s*(?P<text>\S.*)$"
)


def _hms_to_ms(value: str) -> Optional[int]:
    """``HH:MM:SS`` / ``MM:SS`` / ``SS`` → 毫秒。不合规返回 ``None``。"""
    if not value:
        return None
    raw = value.replace(",", ".")
    frac_ms = 0
    if "." in raw:
        raw, frac = raw.split(".", 1)
        frac_ms = int((frac + "000")[:3])
    parts = raw.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        hours, minutes, seconds = 0, nums[0], nums[1]
    elif len(nums) == 3:
        hours, minutes, seconds = nums
    else:
        return None
    if minutes > 59 or seconds > 59:
        return None
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + frac_ms


def parse_cue_timestamp(value: Optional[str]) -> Optional[int]:
    """把单个 cue 时间戳文本解析为毫秒。无法解析返回 ``None``（不伪造时间）。"""
    if not value:
        return None
    m = re.fullmatch(r"\s*[\[\(（]?\s*(\d{1,3}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)\s*[\]\)）]?\s*",
                     str(value))
    if not m:
        return None
    return _hms_to_ms(m.group(1))


@dataclass(frozen=True)
class TranscriptCue:
    """一条转写 cue：文本 + 稳定时间范围（解析不出时间戳时为 ``None``）。"""

    text: str
    start_ms: Optional[int]
    end_ms: Optional[int]
    ordinal: int

    @property
    def has_time(self) -> bool:
        return self.start_ms is not None


def parse_transcript_cues(text: Optional[str],
                          default_gap_ms: int = 30_000) -> list[TranscriptCue]:
    """把转写文本切成带时间范围的 cue 序列。

    支持格式（混排亦可）::

        [00:12:34] 老师开始讲……
        00:12:34
        [00:12:34-00:13:10]
        12:34
        [00:12:34 --> 00:13:10] 正文

    规则:

    * 有起止时间的 cue 直接使用；只有起始时间的 cue 结束时间 = 下一 cue 起始时间
      （最后由 ``default_gap_ms`` 兜底），**不伪造**不存在的时间。
    * 完全没有时间戳的行归属到"当前 cue"；若整份文本都没有时间戳，则返回
      **一个** ``start_ms=None`` 的 cue（调用方据此走无时间轴路径）。
    * 时间戳单调性由调用方负责校验（乱序会被标记为需要人工确认）。
    """
    if not text or not str(text).strip():
        return []
    lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")

    raw_cues: list[tuple[Optional[int], Optional[int], str]] = []
    pending_time: Optional[tuple[Optional[int], Optional[int]]] = None
    buffer: list[str] = []
    saw_time = False

    def flush() -> None:
        nonlocal buffer, pending_time
        body = normalize_text("\n".join(buffer))
        if body or pending_time is not None:
            start, end = pending_time if pending_time is not None else (None, None)
            raw_cues.append((start, end, body))
        buffer = []
        pending_time = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        m_time_only = _CUE_LINE.match(stripped)
        if m_time_only:
            start = _hms_to_ms(m_time_only.group("a"))
            end = _hms_to_ms(m_time_only.group("b")) if m_time_only.group("b") else None
            if start is None:
                buffer.append(stripped)
                continue
            flush()
            saw_time = True
            pending_time = (start, end)
            continue
        m_inline = _CUE_INLINE.match(stripped)
        if m_inline and _hms_to_ms(m_inline.group("a")) is not None:
            start = _hms_to_ms(m_inline.group("a"))
            end = _hms_to_ms(m_inline.group("b")) if m_inline.group("b") else None
            flush()
            saw_time = True
            pending_time = (start, end)
            buffer.append(m_inline.group("text"))
            continue
        buffer.append(stripped)
    flush()

    if not saw_time:
        whole = normalize_text("\n".join(lines))
        return [TranscriptCue(text=whole, start_ms=None, end_ms=None, ordinal=1)] if whole else []

    # 补齐缺失的 end_ms 并强制时间单调不回退：
    # 现实中转写常出现「下一 cue 起点 == 上一 cue 终点」甚至轻微重叠。不做
    # 规范化会产出零长度或倒退的时间轴，时间轴覆盖率与空洞统计都不可信。
    cues: list[TranscriptCue] = []
    for i, (start, end, body) in enumerate(raw_cues):
        if start is None:
            # 时间戳之前的引导文字：不伪造时间，保留为无时间 cue
            cues.append(TranscriptCue(text=body, start_ms=None, end_ms=None,
                                      ordinal=len(cues) + 1))
            continue
        resolved_end = end
        if resolved_end is None:
            nxt = next((raw_cues[j][0] for j in range(i + 1, len(raw_cues))
                        if raw_cues[j][0] is not None), None)
            resolved_end = nxt if nxt is not None and nxt >= start else start + default_gap_ms
        if resolved_end < start:
            resolved_end = start
        cues.append(TranscriptCue(text=body, start_ms=start, end_ms=resolved_end,
                                  ordinal=len(cues) + 1))

    ordered: list[TranscriptCue] = []
    cursor = 0
    for cue in cues:
        if cue.start_ms is None:
            ordered.append(cue)
            continue
        start = max(cue.start_ms, cursor)
        end = cue.end_ms if cue.end_ms is not None else start
        end = max(end, start)
        cursor = end
        ordered.append(TranscriptCue(text=cue.text, start_ms=start, end_ms=end,
                                     ordinal=len(ordered) + 1))
    return ordered


def cues_to_locator(start_ms: Optional[int], end_ms: Optional[int]) -> Optional[str]:
    """毫秒范围 → 稳定 locator 文本（无时间则返回 ``None``）。"""
    if start_ms is None:
        return None
    end = start_ms if end_ms is None else max(end_ms, start_ms)
    return f"{_ms_to_ts(start_ms)}-{_ms_to_ts(end)}"


#: 单条转写 cue 的最大字符数（超出按句边界切开，仍共享同一时间范围）。
TRANSCRIPT_CUE_MAX_CHARS = 1200
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*")


def split_transcript_by_time(text: str, max_len: int = TRANSCRIPT_CUE_MAX_CHARS) -> list[str]:
    """把一条 cue 的正文按句边界切成不超过 ``max_len`` 的片段。

    切分只按标点与长度，**不改动文字**；时间范围由调用方在片段间共享
    （片段 locator 追加 ``#n`` 后缀以保持唯一）。
    """
    body = normalize_text(text)
    if not body or len(body) <= max_len:
        return [body] if body else []
    pieces: list[str] = []
    buf = ""
    for part in _SENTENCE_SPLIT.split(body):
        if not part:
            continue
        if len(buf) + len(part) > max_len:
            if buf:
                pieces.append(buf.strip())
                buf = ""
            # 无标点的超长连续文本也必须切开；旧逻辑仅在 buf 非空时进入，
            # 导致整份文本成为一个数万 token 的 Source Span。
            while len(part) > max_len:
                pieces.append(part[:max_len].strip())
                part = part[max_len:]
        buf += part
    if buf.strip():
        pieces.append(buf.strip())
    return pieces


def transcript_to_located_texts(text: Optional[str],
                               max_len: int = TRANSCRIPT_CUE_MAX_CHARS) -> list[tuple[str, str]]:
    """转写文本 → ``[(locator, text)]``，locator 为 ``HH:MM:SS-HH:MM:SS``。

    ``material_parser``（写 source_chunks）与 ``learning_engine.domain``
    （inline transcript → Source Span）共用此函数，保证两条路径的
    locator / 时间轴 / 切片口径**完全一致**。

    解析不出时间时该 cue 的 locator 退回 ``cue:N``（绝不伪造时间）。
    """
    cues = parse_transcript_cues(text)
    if not cues:
        return []
    out: list[tuple[str, str]] = []
    for i, cue in enumerate(cues, 1):
        for part_no, piece in enumerate(split_transcript_by_time(cue.text, max_len), 1):
            if not piece.strip():
                continue
            locator = cues_to_locator(cue.start_ms, cue.end_ms)
            if locator is None:
                locator = f"cue:{i}"
            elif part_no > 1:
                locator = f"{locator}#{part_no}"
            out.append((locator, piece))
    return out


def timeline_stats(starts: list[Optional[int]], ends: list[Optional[int]],
                   min_gap_ms: int = 60_000) -> dict:
    """时间轴汇总（range + 空洞）。只统计有时间戳的 span，不推测缺失时间。"""
    pairs = [(s, e) for s, e in zip(starts, ends) if s is not None]
    if not pairs:
        return {"start_ms": None, "end_ms": None, "gap_count": 0, "gap_ms": 0, "gaps": []}
    pairs.sort(key=lambda p: p[0])
    gaps: list[dict] = []
    end_max = pairs[0][0]
    for start, end in pairs:
        if start > end_max:
            gap = start - end_max
            if gap > min_gap_ms:
                gaps.append({"from_ms": end_max, "to_ms": start, "gap_ms": gap})
        end_max = max(end_max, end if end is not None else start)
    return {
        "start_ms": pairs[0][0],
        "end_ms": end_max,
        "gap_count": len(gaps),
        "gap_ms": sum(g["gap_ms"] for g in gaps),
        "gaps": gaps,
    }
