"""
RAG 检索：embedding + rerank (经由本地网关)。
使用 NumPy 暴力召回作为 MVP(延迟到 sqlite-vec)。

V5.6.2 FTS 生命周期重构：
- FTS 初始化只由 lifespan 启动序列触发（数据库迁移完成→FTS 探测→原子初始化→
  校验→任务恢复→Worker 启动），**业务路径（解析/检索/删除）不得执行 FTS DDL**。
- 统一状态机：uninitialized / initializing / ready / unavailable / failed。
- 进程级互斥：同一进程仅一个线程执行初始化，其他线程通过 await_fts_ready 等待/降级。
- 有界重试（100ms/250ms/500ms），确认 SQLite 不支持 FTS5 才进 unavailable，否则
  锁超限进 failed；永不静默吞掉 "no such table"。
"""
import json
import logging
import re
import sqlite3
import threading
import time
from typing import Optional

import numpy as np

from .database import query, execute
from .gateway import gateway
from . import tokenizer as _tokenizer  # V5.6.3: 统一分词组件

logger = logging.getLogger(__name__)


# ===========================================================================
# FTS 状态机（V5.6.2）
# ===========================================================================
_FTS_LOCK = threading.Lock()
_fts_state = {
    "state": "uninitialized",          # uninitialized|initializing|ready|unavailable|failed
    "sqlite_fts5": None,               # 探测结果缓存
    "table_exists": False,
    "last_error_code": None,           # 结构化错误码，不在公开 health 暴露
    "indexable_chunks": 0,
    "indexed_chunks": 0,
}

_FTS_RETRY_DELAYS = (0.1, 0.25, 0.5)   # 100ms → 250ms → 500ms

FTS_FTS5_UNSUPPORTED = "E_FTS5_UNSUPPORTED"
FTS_INIT_BUSY = "E_FTS_INIT_BUSY"
FTS_INIT_ERROR = "E_FTS_INIT_ERROR"
FTS_STRUCTURE_MISSING = "E_FTS_STRUCTURE_MISSING"


def _sqlite_fts5_supported() -> bool:
    """探测当前 SQLite 构建是否支持 FTS5（用 :memory: 连接，不碰数据/锁）。"""
    try:
        probe = sqlite3.connect(":memory:")
        try:
            probe.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
            return True
        except sqlite3.OperationalError:
            return False
        finally:
            probe.close()
    except Exception:
        return False


def _count_rows(sql: str) -> int:
    try:
        from .database import fetch_one
        r = fetch_one(sql)
        return int(r["c"]) if r else 0
    except Exception:
        return 0


def _fts_table_exists() -> bool:
    from .database import fetch_one
    try:
        r = fetch_one(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
        )
        return bool(r)
    except Exception:
        return False


def reset_fts_state_for_test() -> None:
    """仅测试使用：把 FTS 状态机复位为未初始化（避免跨测试污染）。"""
    global _fts_state, _FTS_LOCK
    with _FTS_LOCK:
        _fts_state = {
            "state": "uninitialized",
            "sqlite_fts5": None,
            "table_exists": False,
            "last_error_code": None,
            "indexable_chunks": 0,
            "indexed_chunks": 0,
        }


def get_fts_status() -> dict:
    """返回 FTS 状态（供本机 diagnostics；不含底层异常文本）。"""
    with _FTS_LOCK:
        out = {
            "status": _fts_state["state"],
            "sqlite_fts5": bool(_fts_state["sqlite_fts5"]),
            "table_exists": bool(_fts_state["table_exists"]),
            "indexable_chunks": _fts_state["indexable_chunks"],
            "indexed_chunks": _fts_state["indexed_chunks"],
            "last_error_code": _fts_state["last_error_code"],
        }
    # 实时核对表/行数（只读）
    out["table_exists"] = _fts_table_exists()
    out["indexable_chunks"] = _count_rows("SELECT COUNT(*) AS c FROM source_chunks")
    out["indexed_chunks"] = _count_rows(
        "SELECT COUNT(*) AS c FROM chunks_fts") if out["table_exists"] else 0
    return out


def init_fts() -> str:
    """V5.6.2 受控初始化（仅 lifespan 启动触发；并发安全）。

    返回终态：ready / unavailable / failed。
    - 同一进程仅一个线程执行初始化；其他线程阻塞在锁上后读到终态返回。
    - 使用 :memory: 探测 FTS5 能力；确认不支持 → unavailable（不伪装）。
    - 磁盘建表用有界重试；锁超限 → failed（不伪装成不支持）。
    """
    with _FTS_LOCK:
        if _fts_state["state"] in ("ready", "unavailable", "failed"):
            return _fts_state["state"]
        _fts_state["state"] = "initializing"

        # 1) FTS5 能力探测
        supported = _sqlite_fts5_supported()
        _fts_state["sqlite_fts5"] = supported
        if not supported:
            _fts_state["state"] = "unavailable"
            _fts_state["last_error_code"] = FTS_FTS5_UNSUPPORTED
            logger.warning("当前 SQLite 不支持 FTS5，FTS 降级为 keyword_only")
            return _fts_state["state"]

        # 2) 独立短连接 + 有界重试建表
        from .database import create_connection
        created = False
        last_locked = True
        for delay in _FTS_RETRY_DELAYS:
            conn = None
            try:
                conn = create_connection()
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, content='')"
                )
                conn.commit()
                created = True
                break
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "locked" in msg or "busy" in msg:
                    last_locked = True
                    if delay:
                        time.sleep(delay)
                    continue
                # 非锁：SQL/结构错误 → failed
                _fts_state["state"] = "failed"
                _fts_state["last_error_code"] = FTS_INIT_ERROR
                logger.warning("FTS 初始化 SQL 错误: %s", e)
                return _fts_state["state"]
            finally:
                if conn is not None:
                    conn.close()

        if not created:
            # 超过重试上限仍锁 → failed（不是 unavailable）
            _fts_state["state"] = "failed"
            _fts_state["last_error_code"] = FTS_INIT_BUSY
            logger.error("FTS 初始化持续 database is locked，超过重试上限")
            return _fts_state["state"]

        # 3) 结构校验
        if not _fts_table_exists():
            _fts_state["state"] = "failed"
            _fts_state["last_error_code"] = FTS_STRUCTURE_MISSING
            logger.error("FTS 初始化后 chunks_fts 不存在")
            return _fts_state["state"]

        _fts_state["state"] = "ready"
        _fts_state["last_error_code"] = None
        _fts_state["table_exists"] = True
        logger.info("FTS 已就绪")
        return _fts_state["state"]


def await_fts_ready(timeout: float = 6.0) -> bool:
    """业务路径门控：等待 FTS 达到终态，**不触发 DDL**。

    - ready → True（可执行 FTS 写/删/搜）
    - unavailable/failed → False（业务降级为 keyword_only / 跳过索引）
    - uninitialized/initializing → 轮询等待到 timeout，仍未就绪则 False。
    """
    deadline = time.time() + timeout
    while True:
        with _FTS_LOCK:
            s = _fts_state["state"]
        if s == "ready":
            return True
        if s in ("unavailable", "failed"):
            return False
        if time.time() >= deadline:
            with _FTS_LOCK:
                return _fts_state["state"] == "ready"
        time.sleep(0.05)


def rebuild_fts_all(conn: Optional[sqlite3.Connection] = None) -> int:
    """
    全量重建 FTS 索引（迁移/恢复后调用）。返回索引块数。
    rowid = source_chunks.id，保证删除/重解析时能精确同步。
    """
    if not await_fts_ready():
        logger.warning("FTS 状态 %s，跳过重建", get_fts_status()["status"])
        return -1
    own = conn is None
    if own:
        from .database import create_connection
        conn = create_connection()
    try:
        conn.execute("DELETE FROM chunks_fts")
        conn.execute("INSERT INTO chunks_fts(rowid, text) SELECT id, text FROM source_chunks")
        conn.commit()
        return int(conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0])
    except Exception as e:
        logger.warning(f"FTS 重建失败: {e}")
        return -1
    finally:
        if own:
            conn.close()


def fts_index_chunks(conn: Optional[sqlite3.Connection], rows: list[tuple[int, str]]) -> None:
    """把新块写入 FTS（rowid=chunk_id, text）。conn=None 时使用独立短连接提交。

    V5.6.2：业务路径不执行 DDL；FTS 未就绪则跳过（降级），不抛出 no such table。
    """
    if not rows:
        return
    if not await_fts_ready():
        logger.debug("FTS 未就绪(%s)，跳过索引写入 → keyword_only 覆盖",
                     get_fts_status()["status"])
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
    """删除指定块的 FTS 行（材料重解析/删除时调用，方案 10.1）。conn=None 用独立短连接。

    V5.6.2：FTS 未就绪则不 DDL；直接跳过删除（chunks_fts 不存在即无同步需求）。
    """
    if not chunk_ids:
        return
    if not await_fts_ready():
        logger.debug("FTS 未就绪(%s)，跳过索引删除",
                     get_fts_status()["status"])
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
    """中英文混合分词（V5.6.3：统一走 tokenizer.tokenize）。"""
    return set(_tokenizer.tokenize(text))


def _bonus_phrase_score(query_text: str, doc_text: str) -> float:
    """关键词短语命中加成:长 query 子串在 doc 中完整出现时加权"""
    qt = query_text.strip()
    if not qt:
        return 0.0
    # V5.6.3：用统一 tokenizer 生成短语候选；只保留长度≥2 的短语
    phrases = [w for w in _tokenizer.tokenize(qt) if len(w) >= 2]
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
    """分词为列表（BM25 需要词频，不能用集合）。V5.6.3：统一走 tokenizer。"""
    return _tokenizer.tokenize(text)


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
    # V5.6.3: 审计字段加入实际使用的 tokenizer 模式（复用 filters_json，无新 migration）
    filters_json = json.dumps({
        "chapter_id": chapter_id,
        "lesson_id": lesson_id,
        "_tokenizer": _tokenizer.get_tokenizer_status()["mode"],
    }, ensure_ascii=False)

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

    # ---- V5.6.2: FTS 未就绪（unavailable/failed/uninitialized）→ 强制 keyword_only ----
    final_mode = mode
    if mode == "hybrid" and get_fts_status()["status"] != "ready":
        final_mode = "keyword_only"
        logger.debug("FTS 状态 %s，检索降级 keyword_only", get_fts_status()["status"])

    # ---- BM25 归一化 ----
    bm25_raw = _bm25_scores(query_text, [c.get("text") or "" for c in chunks])
    max_bm25 = max(bm25_raw) if bm25_raw else 0.0

    # ---- 向量（hybrid 时才调用 embedding；失败自动降级） ----
    q_vec: list[float] = []
    if final_mode == "hybrid":
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