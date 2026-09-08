"""
Reasoning 输出解析器：
- 清洗 MiniMax 的 <thinking> 标记
- 分离 reasoning_content 和 content
- 提取 JSON、markdown、引文
"""
import json
import re
from typing import Any, Optional


def clean_minimax_thinking(text: str) -> str:
    """去掉 MiniMax 可能包裹的 <thinking>...</thinking>"""
    # 头部 thinking
    text = re.sub(r"^\s*<thinking>.*?</thinking>\s*", "", text, flags=re.DOTALL)
    # 任意位置
    text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL)
    text = re.sub(r"<\s*thinking\s*>", "", text)
    text = re.sub(r"</\s*thinking\s*>", "", text)
    return text.strip()


def extract_json(text: str) -> Optional[Any]:
    """尝试从字符串中提取 JSON 对象/数组"""
    text = clean_minimax_thinking(text).strip()
    # 直接尝试
    try:
        return json.loads(text)
    except Exception:
        pass
    # 从 ```json ... ``` 块提取
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            pass
    # 从花括号/方括号匹配提取
    for open_ch, close_ch in [("{", "}"), ("[", "]")]:
        start = text.find(open_ch)
        if start == -1:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except Exception:
                        break
    return None


def extract_structured(text: str) -> dict:
    """返回 {json: Any|None, cleaned_text: str, quotes: list[str]}"""
    obj = extract_json(text)
    quotes = re.findall(r"「(.+?)」|『(.+?)』|\"(.{10,})\"", text)
    quotes = [q for grp in quotes for q in grp if q]
    return {
        "json": obj,
        "cleaned_text": clean_minimax_thinking(text),
        "quotes": quotes,
    }


def repair_json_blob(text: str) -> Optional[Any]:
    """对 LLM 常见低错误做修复：去除代码围栏、修尾逗号、补完整截断括号。"""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    s = (s or "").strip()
    if not s:
        return None
    # 先原样试
    try:
        return json.loads(s)
    except Exception:
        pass
    # 去除尾逗号（对象/数组末尾 , 后跟 } 或 ]）
    s = re.sub(r",\s*([}\]])", r"\1", s)
    try:
        return json.loads(s)
    except Exception:
        pass
    # 尝试补全（括号/引号计数）- 简单策略：逐层截断到最近平衡点
    for idx in range(len(s), 0, -1):
        cand = s[:idx]
        if cand.count("{") == cand.count("}") and cand.count("[") == cand.count("]"):
            try:
                return json.loads(cand)
            except Exception:
                continue
    return None
