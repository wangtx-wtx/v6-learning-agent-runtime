"""
RAG 检索：embedding + rerank (经由本地网关)。
使用 NumPy 暴力召回作为 MVP(延迟到 sqlite-vec)。

V5.2:中文分词改用 jieba(细粒度模式),Jaccard 之外附加关键词命中加成。
"""
import json
import logging
import re
import sqlite3
from typing import Optional

import numpy as np

from .database import query, execute
from .gateway import gateway

logger = logging.getLogger(__name__)

try:
    import jieba  # type: ignore
    _HAS_JIEBA = True
except ImportError:
    _HAS_JIEBA = False
    logger.warning("jieba 未安装,中文分词降级为字符切分。pip install jieba 启用。")


_FTS_INIT_DONE = False


def init_fts() -> None:
    """创建 FTS5 块索引表（幂等）。"""
    global _FTS_INIT_DONE
    if _FTS_INIT_DONE:
        return
    from .database import execute
    try:
        execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, content='')"
        )
        _FTS_INIT_DONE = True
    except Exception as e:
        logger.warning(f"FTS5 初始化失败(可能当前 sqlite 无 fts5): {e}")


def rebuild_fts_all(conn: Optional[sqlite3.Connection] = None) -> int:
    """
    全量重建 FTS 索引（迁移/恢复后调用）。返回索引块数。
    rowid = source_chunks.id，保证删除/重解析时能精确同步。
    """
    own = conn is None
    if own:
        from .database import get_connection
        conn = get_connection()
    init_fts()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM chunks_fts")
        cur.execute("INSERT INTO chunks_fts(rowid, text) SELECT id, text FROM source_chunks")
        conn.commit()
        return int(cur.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0])
    except Exception as e:
        logger.warning(f"FTS 重建失败: {e}")
        return -1
    finally:
        if own:
            conn.close()


def fts_index_chunks(conn: Optional[sqlite3.Connection], rows: list[tuple[int, str]]) -> None:
    """把新块写入 FTS（rowid=chunk_id, text）。conn=None 时使用独立短连接提交。"""
    init_fts()
    if not rows:
        return
    own = conn is None
    if own:
        from .database import create_connection
        conn = create_connection()
    try:
        conn.executemany("INSERT INTO chunks_fts(rowid, text) VALUES (?,?)", rows)
        if own:
            conn.commit()
    except Exception as e:
        logger.warning(f"FTS 索引写入失败: {e}")
    finally:
        if own:
            conn.close()


def fts_delete_chunk_ids(conn: Optional[sqlite3.Connection], chunk_ids: list[int]) -> None:
    """删除指定块的 FTS 行（材料重解析/删除时调用，方案 10.1）。conn=None 用独立短连接。"""
    init_fts()
    if not chunk_ids:
        return
    own = conn is None
    if own:
        from .database import create_connection
        conn = create_connection()
    try:
        conn.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(i,) for i in chunk_ids])
        if own:
            conn.commit()
    except Exception as e:
        logger.warning(f"FTS 删除失败: {e}")
    finally:
        if own:
            conn.close()


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


def _bm25_scores(query_text: str, docs: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    """轻量 BM25：在候选集内计算，返回每篇的原始分（不做全局 IDF 归一）。"""
    import math
    from collections import Counter
    q_tokens = [t for t in _tokenize_list(query_text)]
    if not q_tokens or not docs:
        return [0.0] * len(docs)
    doc_tokens = [_tokenize_list(d) for d in docs]
    avgdl = sum(len(t) for t in doc_tokens) / max(len(doc_tokens), 1)
    df: Counter = Counter()
    for toks in doc_tokens:
        for term in set(toks):
            df[term] += 1
    n = len(docs)
    scores = []
    for toks in doc_tokens:
        tf = Counter(toks)
        score = 0.0
        for term in q_tokens:
            if term not in tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * (tf[term] * (k1 + 1)) / (tf[term] + k1 * (1 - b + b * len(toks) / max(avgdl, 1)))
        scores.append(score)
    return scores


def _tokenize_list(text: str) -> list[str]:
    """分词为列表（BM25 需要词频，不能用集合）。"""
    import re as _re
    tokens: list[str] = []
    s = str(text)
    if _HAS_JIEBA and _re.search(r"[一-鿿]", s):
        for word in jieba.cut_for_search(s):
            clean = _re.sub(r"[^\w一-鿿]", "", word).strip()
            if clean:
                tokens.append(clean.lower())
    else:
        for word in s.split():
            clean = _re.sub(r"[^\w一-鿿]", "", word).strip()
            if clean:
                tokens.append(clean.lower())
    return tokens


def _cosine(a: list[float], b: list[float]) -> float:
    import math
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / max(na * nb, 1e-9)


async def retrieve_chunks(
    query_text: str,
    chapter_id: Optional[int] = None,
    lesson_id: Optional[int] = None,
    top_k: int = 5,
    run_id: Optional[int] = None,
    mode: str = "hybrid",
) -> list[dict]:
    """混合检索（方案 10.5）：
    score = 0.45*BM25 + 0.40*向量余弦 + 0.10*层级匹配 + 0.05*短语命中
    - embedding 不可用时自动降级 keyword_only（BM25+层级+短语）；
    - 每次检索写一行 retrieval_runs（可归因：输出差是检索问题还是生成问题）。"""
    sql = "SELECT id, type, locator, text, chapter_id, lesson_id, embedding FROM source_chunks WHERE 1=1"
    params: list = []
    if chapter_id:
        sql += " AND chapter_id=?"
        params.append(chapter_id)
    if lesson_id:
        sql += " AND lesson_id=?"
        params.append(lesson_id)
    chunks = query(sql, tuple(params))
    filters_json = json.dumps({"chapter_id": chapter_id, "lesson_id": lesson_id}, ensure_ascii=False)

    def _persist(selected: list[dict], final_mode: str) -> None:
        try:
            execute(
                "INSERT INTO retrieval_runs (workflow_run_id, query_text, filters_json, retrieval_mode, "
                " candidate_count, selected_chunk_ids) VALUES (?,?,?,?,?,?)",
                (run_id, query_text[:500], filters_json, final_mode, len(chunks),
                 json.dumps([c.get("chunk_id") for c in selected], ensure_ascii=False)),
            )
        except Exception as e:
            logger.warning(f"retrieval_runs 写入失败: {e}")

    if not chunks:
        return []

    # ---- BM25 归一化 ----
    bm25_raw = _bm25_scores(query_text, [c.get("text") or "" for c in chunks])
    max_bm25 = max(bm25_raw) if bm25_raw else 0.0

    # ---- 向量（hybrid 时才调用 embedding；失败自动降级） ----
    q_vec: list[float] = []
    final_mode = mode
    if mode == "hybrid":
        q_vec = await embed_text(query_text)
        if not q_vec:
            final_mode = "keyword_only"   # 降级：embedding 不可用

    def _hierarchy(c: dict) -> float:
        if lesson_id and c.get("lesson_id") == lesson_id:
            return 1.0
        if chapter_id and c.get("chapter_id") == chapter_id:
            return 0.6
        return 0.2

    scored: list[dict] = []
    for i, c in enumerate(chunks):
        doc_text = c.get("text") or ""
        bm25_n = (bm25_raw[i] / max_bm25) if max_bm25 > 0 else 0.0
        vec_score = 0.0
        if q_vec:
            emb = c.get("embedding")
            doc_vec = None
            if emb:
                try:
                    doc_vec = json.loads(emb) if isinstance(emb, str) else emb
                except Exception:
                    doc_vec = None
            if doc_vec:
                vec_score = max(_cosine(q_vec, doc_vec), 0.0)
            elif final_mode == "hybrid":
                # 无块向量：hybrid 下该分量缺失，自动按 keyword_only 权重处理
                pass
        hier_n = _hierarchy(c)          # 已归一 0..1
        phrase_n = min(_bonus_phrase_score(query_text, doc_text) / 0.5, 1.0)
        if final_mode == "hybrid":
            score = 0.45 * bm25_n + 0.40 * vec_score + 0.10 * hier_n + 0.05 * phrase_n
        else:
            score = 0.60 * bm25_n + 0.30 * hier_n + 0.10 * phrase_n
        if score > 0:
            scored.append({
                "chunk_id": c.get("id"),
                "text": doc_text,
                "locator": c.get("locator", ""),
                "type": c.get("type", ""),
                "score": float(score),
            })

    scored.sort(key=lambda x: x["score"], reverse=True)
    selected = scored[:top_k]
    _persist(selected, final_mode)
    return selected