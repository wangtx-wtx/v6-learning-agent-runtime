"""V5.6.3 统一中文分词组件（tokenizer）。

- 启动阶段只初始化一次；多线程安全。
- jieba 是核心检索依赖，缺失时降级 char_fallback，且**只记录一次**降级日志。
- 业务请求不得反复 import/初始化。
- 状态机：uninitialized / initializing / ready / unavailable / failed
  模式：jieba / char_fallback
"""
from __future__ import annotations

import logging
import re
import threading
from typing import List

logger = logging.getLogger(__name__)

# 中文字符区间（用于判定是否触发 jieba 路径）
_CJK_RE = re.compile(r"[一-鿿㐀-䶿]")

# 状态常量
_STATE_UNINITIALIZED = "uninitialized"
_STATE_INITIALIZING = "initializing"
_STATE_READY = "ready"
_STATE_UNAVAILABLE = "unavailable"
_STATE_FAILED = "failed"

_MODE_JIEBA = "jieba"
_MODE_CHAR = "char_fallback"

# 错误码
_ERR_MODULE_MISSING = "E_TOKENIZER_MODULE_MISSING"
_ERR_INIT_FAILED = "E_TOKENIZER_INIT_FAILED"

_state_lock = threading.Lock()
_state: dict = {
    "state": _STATE_UNINITIALIZED,
    "mode": None,
    "initialized": False,
    "version": None,
    "last_error_code": None,
    "_degraded_logged": False,
}
# 已加载的 jieba 模块引用（tokenize 用它，避免请求时反复 import）
_jieba_mod = None


def reset_tokenizer_state_for_test() -> None:
    """仅测试使用：复位状态机（避免跨测试污染）。"""
    global _state, _jieba_mod
    with _state_lock:
        _state = {
            "state": _STATE_UNINITIALIZED,
            "mode": None,
            "initialized": False,
            "version": None,
            "last_error_code": None,
            "_degraded_logged": False,
        }
        _jieba_mod = None


def get_tokenizer_status() -> dict:
    """返回 tokenizer 状态（供本机 diagnostics，不含异常堆栈）。"""
    with _state_lock:
        return {
            "status": _state["state"],
            "mode": _state["mode"],
            "version": _state["version"],
            "initialized": _state["initialized"],
            "last_error_code": _state["last_error_code"],
        }


def _import_jieba():
    """导入 jieba 并返回模块；缺失抛 ImportError。"""
    import jieba  # type: ignore  # noqa: F401
    return jieba


def initialize_tokenizer() -> str:
    """启动阶段单次初始化 tokenizer。

    返回终态 ready / unavailable / failed。并发安全：同一进程仅一个线程实际
    执行，其余线程读到终态返回。
    """
    with _state_lock:
        if _state["state"] in (_STATE_READY, _STATE_UNAVAILABLE, _STATE_FAILED):
            return _state["state"]
        if _state["state"] == _STATE_INITIALIZING:
            _state_lock.release()
            try:
                # 其他线程正初始化：轮询到终态
                deadline = _deadline(5.0)
                while True:
                    with _state_lock:
                        s = _state["state"]
                    if s in (_STATE_READY, _STATE_UNAVAILABLE, _STATE_FAILED):
                        return s
                    _sleep(0.05)
                    if __import__("time").time() > deadline:
                        with _state_lock:
                            return _state["state"]
            finally:
                _state_lock.acquire()
        _state["state"] = _STATE_INITIALIZING

        # 1) 尝试导入 jieba
        try:
            jieba_mod = _import_jieba()
        except ImportError:
            _state["state"] = _STATE_UNAVAILABLE
            _state["mode"] = _MODE_CHAR
            _state["initialized"] = True
            _state["version"] = None
            _state["last_error_code"] = _ERR_MODULE_MISSING
            _maybe_log_degraded_once()
            return _state["state"]
        except Exception as e:  # pragma: no cover - 非常规导入错误
            _state["state"] = _STATE_FAILED
            _state["mode"] = _MODE_CHAR
            _state["initialized"] = True
            _state["last_error_code"] = _ERR_INIT_FAILED
            logger.error("jieba 初始化异常: %s", e)
            return _state["state"]

        # 2) 实际初始化（触发词典加载，可失败/耗时）
        try:
            jieba_mod.initialize()
        except Exception as e:
            # 初始化失败 → failed，但降级 char_fallback 保证后端可启动
            _state["state"] = _STATE_FAILED
            _state["mode"] = _MODE_CHAR
            _state["initialized"] = True
            _state["last_error_code"] = _ERR_INIT_FAILED
            logger.error("jieba initialize() 失败: %s", e)
            _maybe_log_degraded_once()
            return _state["state"]

        global _jieba_mod
        _jieba_mod = jieba_mod
        _state["state"] = _STATE_READY
        _state["mode"] = _MODE_JIEBA
        _state["initialized"] = True
        _state["version"] = getattr(jieba_mod, "__version__", None) or _version_from_pkg()
        _state["last_error_code"] = None
        logger.info("tokenizer 就绪: mode=jieba version=%s", _state["version"])
        return _state["state"]


def _maybe_log_degraded_once() -> None:
    """jieba 缺失/失败时仅记录一次降级日志（模块级标记）。"""
    if _state.get("_degraded_logged"):
        return
    _state["_degraded_logged"] = True
    logger.warning("jieba 不可用，中文检索降级为字符级（char_fallback）。"
                   "标准安装应通过 requirements.txt 安装 jieba。")


def _version_from_pkg() -> str | None:
    try:
        import importlib.metadata as _md
        return _md.version("jieba")
    except Exception:
        return None


def tokenize(text: str | None) -> List[str]:
    """统一分词接口（过滤纯空白 token）。

    - jieba 可用：中文走 jieba.cut_for_search，英文/数字按空白切。
    - jieba 不可用：字符级降级（保留中文单字 + 英文/数字词）。
    - None / 空文本 / 纯标点 / 空白输入返回空列表。
    """
    if text is None:
        return []
    s = str(text)
    if not s or not s.strip():
        return []

    # 读取当前模式（懒初始化：_ensure_ready 只初始化一次）
    mode = _current_mode()

    if mode == _MODE_CHAR:
        return _char_tokenize(s)

    # 用初始化时缓存的 jieba 模块引用（不在请求路径 import）
    jieba_mod = _jieba_mod
    if jieba_mod is None:
        try:
            jieba_mod = _import_jieba()
        except Exception:
            return _char_tokenize(s)
    try:
        toks: List[str] = []
        for word in jieba_mod.cut_for_search(s):
            clean = re.sub(r"[^\w一-鿿]", "", word).strip()
            if clean:
                toks.append(clean.lower())
        return toks
    except Exception:
        # 运行时异常（不应该发生，防御）：回退字符级
        return _char_tokenize(s)


def _current_mode() -> str:
    with _state_lock:
        s = _state["state"]
    if s in (_STATE_READY, _STATE_UNAVAILABLE, _STATE_FAILED):
        return _state["mode"] or _MODE_CHAR
    # 未初始化：尝试初始化（幂等；仅首次实际初始化）
    initialize_tokenizer()
    with _state_lock:
        return _state["mode"] or _MODE_CHAR


def _char_tokenize(text: str) -> List[str]:
    """字符级降级：中文单字 + 英文/数字连续词。"""
    s = str(text)
    toks: List[str] = []
    # 英文/数字连续块
    ascii_chunks = re.findall(r"[A-Za-z0-9]+", s)
    toks.extend(c.lower() for c in ascii_chunks if c.strip())
    # 中文单字
    for ch in s:
        if _CJK_RE.match(ch):
            toks.append(ch)
    return toks


def _deadline(sec: float) -> float:
    import time
    return time.time() + sec


def _sleep(sec: float) -> None:
    import time
    time.sleep(sec)


# 分词别名（供业务代码迁移，语义同 tokenize）
tokenize_text = tokenize