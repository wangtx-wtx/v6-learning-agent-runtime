"""
文件类型支持矩阵（V5.5 方案 4.4）。

关键原则：
- 音频不得伪装 ready（旧实现写入 "[音频材料待转写]" 占位 chunk 并标 ready）；
- 图片不得伪装 ready（"[待视觉识别]" 占位）；
- 两类材料分别进入 transcribing / needs_ocr 状态，等待真实识别管线（方案 6.x/7.x）；
- 不支持的类型在上传层即被 ALLOWED_EXTS 拒绝。
"""
from __future__ import annotations

# 解析管线可直接产出 chunks 的类型（kind 别名一并收录：_kind_of 的 fallback 是 "text"）
PARSEABLE_KINDS = {"pdf", "ppt", "doc", "text", "txt", "md"}

# kind → (parser_status, status)
STATE_MATRIX: dict[str, tuple[str, str]] = {
    "pdf":          ("queued", "queued"),
    "ppt":          ("queued", "queued"),
    "doc":          ("queued", "queued"),
    "text":         ("queued", "queued"),
    "txt":          ("queued", "queued"),
    "md":           ("queued", "queued"),
    "image":        ("needs_ocr", "needs_ocr"),        # 等待视觉识别（方案 7.x）
    "audio":        ("transcribing", "transcribing"),  # 等待转写（方案 6.x）
    "unsupported":  ("unsupported", "unsupported"),
}


def initial_state(kind: str) -> tuple[str, str]:
    """上传入库时的初始 (parser_status, status)。未知类型按 unsupported 处理。"""
    return STATE_MATRIX.get(kind, STATE_MATRIX["unsupported"])


def is_parseable(kind: str) -> bool:
    return kind in PARSEABLE_KINDS
