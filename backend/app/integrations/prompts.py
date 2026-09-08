"""
Prompt 外置加载器（V5.5 方案 11.1/11.2）。

- 提示词全部外置到 backend/prompts/<workflow>/<name>.<version>.md；
- 加载带 sha256 校验和（审计可追溯每次调用用的提示词版本）；
- 进程内缓存；
- 测试夹具覆盖：环境变量 V5_PROMPT_FIXTURE_DIR 指向的目录优先（测试可注入固定提示词）；
- 变量语法：{{var_name}}，render_prompt 安全替换（缺变量保持原样并告警）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
_VAR_RE = re.compile(r"\{\{(\w+)\}\}")

_cache: dict[tuple[str, str], dict] = {}


def _fixture_dir() -> Path | None:
    d = os.environ.get("V5_PROMPT_FIXTURE_DIR")
    return Path(d) if d else None


def load_prompt(name: str, version: str = "v1") -> dict:
    """加载提示词：优先测试夹具，其次外置目录。返回 {text, checksum, source}。"""
    key = (name, version)
    if key in _cache:
        return _cache[key]

    fixture = _fixture_dir()
    if fixture:
        # 兼容两种夹具命名：lesson/critic.v1.md（子目录）与 lesson__critic.v1.md（扁平）
        for fname in (f"{name}.{version}.md", f"{name.replace('/', '__')}.{version}.md"):
            fp = fixture / fname
            if fp.exists():
                text = fp.read_text(encoding="utf-8")
                entry = {"text": text, "checksum": hashlib.sha256(text.encode()).hexdigest()[:16],
                         "source": f"fixture:{fp.name}"}
                _cache[key] = entry
                return entry

    # name 形如 "lesson/note_writer" 或 "note_writer"（此时在所有目录中查找）
    candidates: list[Path] = []
    if "/" in name:
        candidates.append(PROMPTS_DIR / f"{name}.{version}.md")
    else:
        for sub in sorted(p for p in PROMPTS_DIR.iterdir() if p.is_dir()):
            candidates.append(sub / f"{name}.{version}.md")
    for p in candidates:
        if p.exists():
            text = p.read_text(encoding="utf-8")
            entry = {"text": text, "checksum": hashlib.sha256(text.encode()).hexdigest()[:16],
                     "source": str(p.relative_to(PROMPTS_DIR))}
            _cache[key] = entry
            return entry
    raise FileNotFoundError(f"提示词不存在: {name}.{version}（查找于 {PROMPTS_DIR}）")


def render_prompt(name: str, version: str = "v1", **variables) -> dict:
    """加载并渲染提示词。返回 {text, checksum, version, name, source}。"""
    entry = load_prompt(name, version)
    text = entry["text"]

    def _sub(m: re.Match) -> str:
        key = m.group(1)
        if key in variables:
            return str(variables[key])
        logger.warning("提示词 %s.%s 缺少变量 %s", name, version, key)
        return m.group(0)

    rendered = _VAR_RE.sub(_sub, text)
    missing = set(_VAR_RE.findall(rendered))
    if missing:
        logger.warning("提示词 %s.%s 渲染后仍有未替换变量: %s", name, version, missing)

    # 若处于 DAG 节点上下文（gateway 审计 contextvar 存在），自动注入
    # prompt_name/prompt_version，随 model_calls 审计落库（方案 11.3）。
    try:
        from ..gateway import _call_context
        cur = _call_context.get()
        if cur is not None:
            merged = dict(cur)
            merged.setdefault("prompt_name", name)
            merged["prompt_version"] = version
            _call_context.set(merged)
    except Exception:
        pass

    return {"text": rendered, "checksum": entry["checksum"],
            "name": name, "version": version, "source": entry["source"]}


def clear_cache() -> None:
    """测试用：清空缓存（夹具目录变化后调用）。"""
    _cache.clear()
