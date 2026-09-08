"""
RAG 检索：embedding + rerank (经由本地网关)。
使用 NumPy 暴力召回作为 MVP(延迟到 sqlite-vec)。

V5.2:中文分词改用 jieba(细粒度模式),Jaccard 之外附加关键词命中加成。
"""
import json
import logging
import re
from typing import Optional

import numpy as np

from .database import query
from .gateway import gateway

logger = logging.getLogger(__name__)

try:
    import jieba  # type: ignore
    _HAS_JIEBA = True
except ImportError:
    _HAS_JIEBA = False
    logger.warning("jieba 未安装,中文分词降级为字符切分。pip install jieba 启用。")


async def embed_text(text: str) -> list[float]:
    """调用 embedding 模型获取向量"""
    try:
        return await gateway.embedding_single(text)
    except Exception as e:
        logger.warning(f"embedding 调用失败: {e}")
        return []


def jaccard(query_tokens: set[str], doc_tokens: set[str]) -> float:
    if not query_tokens or not doc_tokens:
        return 0.0
    inter = len(query_tokens & doc_tokens)
    union = len(query_tokens | doc_tokens)
    return inter / max(union, 1.0)


def _tokenize(text: str) -> set[str]:
    """中英文混合分词:中文走 jieba.cut_for_search,英文/数字走空白切分"""
    tokens: set[str] = set()
    s = str(text)
    if _HAS_JIEBA and re.search(r"[一-鿿]", s):
        for word in jieba.cut_for_search(s):
            clean = re.sub(r"[^\w一-鿿]", "", word).strip()
            if len(clean) >= 1:
                tokens.add(clean.lower())
    else:
        for word in s.split():
            clean = re.sub(r"[^\w一-鿿]", "", word).strip()
            if clean:
                tokens.add(clean.lower())
    return tokens


def _bonus_phrase_score(query_text: str, doc_text: str) -> float:
    """关键词短语命中加成:长 query 子串在 doc 中完整出现时加权"""
    qt = query_text.strip()
    if not qt:
        return 0.0
    phrases: list[str] = []
    if _HAS_JIEBA and re.search(r"[一-鿿]", qt):
        for w in jieba.cut_for_search(qt):
            if len(w) >= 2:
                phrases.append(w)
    else:
        for w in qt.split():
            if len(w) >= 3:
                phrases.append(w)
    if not phrases:
        return 0.0
    hits = sum(1 for p in phrases if p in doc_text)
    return min(hits * 0.1, 0.5)


async def retrieve_chunks(
    query_text: str,
    chapter_id: Optional[int] = None,
    lesson_id: Optional[int] = None,
    top_k: int = 5,
) -> list[dict]:
    """从 source_chunks 检索最相关块(BM25-like + 关键词加成 + 可扩充向量)"""
    sql = "SELECT id, type, locator, text FROM source_chunks WHERE 1=1"
    params: list = []
    if chapter_id:
        sql += " AND chapter_id=?"
        params.append(chapter_id)
    if lesson_id:
        sql += " AND lesson_id=?"
        params.append(lesson_id)
    chunks = query(sql, tuple(params))

    if not chunks:
        return []

    query_tokens = _tokenize(query_text)
    scored: list[dict] = []
    for c in chunks:
        doc_text = c.get("text") or ""
        doc_tokens = _tokenize(doc_text)
        jac = jaccard(query_tokens, doc_tokens)
        bonus = _bonus_phrase_score(query_text, doc_text)
        score = jac + bonus
        if score > 0:
            scored.append({
                "chunk_id": c.get("id"),
                "text": doc_text,
                "locator": c.get("locator", ""),
                "type": c.get("type", ""),
                "score": float(score),
            })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]