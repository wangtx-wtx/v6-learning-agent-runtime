"""V6 材料覆盖 / 分段理解 Benchmark（只读 / 隔离）。

用途：为某次运行（或某个真实课堂 run）复算材料完整性与 Phase 2 全量理解指标，
输出与设计文档 §13.2 对齐的报告。**默认只读、默认不调用任何真实模型。**

安全约束（任务书 §十一）:

* 默认 ``--db`` 指向正式库时以 ``mode=ro`` 打开，绝不写入、绝不迁移；
* 默认不调用真实模型；调用真实模型必须显式设置
  ``V6_BENCHMARK_ALLOW_LIVE=1`` **且** 传 ``--live``，两者缺一不可；
* 不删除、不覆盖正式课程材料与笔记；
* 报告必须注明数据时间、run_id、material_id 与计算口径。

用法::

    # 只读复算某个 run 的覆盖口径（推荐）
    python -X utf8 backend/tools/v6_coverage_benchmark.py --run-id 123

    # 列出可选 run
    python -X utf8 backend/tools/v6_coverage_benchmark.py --list

    # 直接对一个 v5.db 只读复算（不经过 app.database，不迁移）
    python -X utf8 backend/tools/v6_coverage_benchmark.py --db backend/data/v5.db --run-id 123

    # 输出 JSON
    python -X utf8 backend/tools/v6_coverage_benchmark.py --run-id 123 --json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = (BACKEND_DIR / "data" / "v5.db").resolve()

#: 旧生成链当前的候选窗口（设计文档 §2.1）。报告里用于解释「为什么覆盖率低」。
LEGACY_RUN_CHUNK_LIMIT = 12
LEGACY_RAG_TOP_K = 8
LEGACY_OUTLINE_CHUNK_LIMIT = 20
LEGACY_OUTLINE_CHARS = 300
LEGACY_CANDIDATE_CHARS = 500


def _read_only_conn(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise SystemExit(f"[benchmark] 数据库不存在: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _ratio(num: int, den: int) -> float:
    if den <= 0:
        return 1.0
    return round(num / den, 6)


def _ms_to_ts(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    ms = max(int(ms), 0)
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, _ = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def list_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    if _table_exists(conn, "material_domains"):
        rows = _rows(
            conn,
            "SELECT r.id, r.workflow, r.status, r.created_at, d.id AS domain_id, "
            " d.engine_version, (SELECT gate FROM coverage_reports c WHERE c.run_id=r.id) AS gate "
            "FROM workflow_runs r LEFT JOIN material_domains d ON d.run_id=r.id "
            "ORDER BY r.id DESC LIMIT ?",
            (limit,),
        )
    else:
        rows = _rows(
            conn,
            "SELECT id, workflow, status, created_at, NULL AS domain_id, "
            " NULL AS engine_version, NULL AS gate FROM workflow_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    return rows


def compute_metrics(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    """只读复算覆盖指标（与 learning_engine.coverage 同口径，但不依赖 app 包）。"""
    domain = None
    if _table_exists(conn, "material_domains"):
        row = conn.execute("SELECT * FROM material_domains WHERE run_id=?", (run_id,)).fetchone()
        domain = dict(row) if row else None
    result: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "caliber": {
            "hash_algo": "sha256+norm-v1",
            "token_estimate": "cjk_chars + ceil(other_chars/4)",
            "domain_accounting_rate": "有终态的 domain_items / domain_items 总数",
            "semantic_processing_rate": "已进入 understand_segments 的 canonical 非噪声 span / canonical 非噪声 span 总数",
            "timeline_coverage_rate": "已处理的转写 span / canonical 非噪声转写 span 总数",
            "silent_dropped": "既无账目又无显式原因的 canonical 非噪声 span 数",
            "source_ref_validity": "processed source refs / canonical source refs",
            "note": "Phase 2 已实现全量分段理解：semantic_processing_rate 反映 canonical 非噪声 span 进入 understand_segments 的比例（门禁要求 1.0）",
        },
        "legacy_chain_limits": {
            "run_chunk_limit": LEGACY_RUN_CHUNK_LIMIT,
            "rag_top_k": LEGACY_RAG_TOP_K,
            "outline_chunk_limit": LEGACY_OUTLINE_CHUNK_LIMIT,
            "outline_chars_per_chunk": LEGACY_OUTLINE_CHARS,
            "candidate_chars_per_chunk": LEGACY_CANDIDATE_CHARS,
        },
        "run_id": run_id,
        "domain": None,
    }
    run = conn.execute("SELECT * FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise SystemExit(f"[benchmark] run {run_id} 不存在")
    result["run"] = {k: run[k] for k in run.keys() if k in
                     ("id", "workflow", "mode", "status", "course_id", "chapter_id",
                      "lesson_id", "created_at", "updated_at", "error")}

    if domain is None:
        result["coverage"] = None
        result["note"] = ("该 run 没有 Material Domain：V6_LEARNING_ENGINE=off 或运行早于 0016 迁移。"
                          "如需覆盖口径，请以 shadow 模式重跑。")
        # 仍然给出旧链可读的材料体量，便于对照
        result["legacy_material_stats"] = _legacy_material_stats(conn, run_id)
        return result

    domain_id = int(domain["id"])
    result["domain"] = {
        "domain_id": domain_id,
        "scope": domain.get("scope"),
        "version": domain.get("version"),
        "state": domain.get("state"),
        "domain_hash": domain.get("domain_hash"),
        "engine_version": domain.get("engine_version"),
        "transcript_chars": domain.get("transcript_chars"),
        "course_id": domain.get("course_id"),
        "chapter_id": domain.get("chapter_id"),
        "lesson_id": domain.get("lesson_id"),
        "frozen_at": domain.get("frozen_at"),
        "created_at": domain.get("created_at"),
    }

    items = _rows(conn, "SELECT * FROM material_domain_items WHERE domain_id=? "
                        "ORDER BY ordinal, id", (domain_id,))
    total_items = len(items)
    accounted = sum(
        1 for i in items
        if i["state"] in ("included", "duplicate") or i.get("reason_code")
    )
    unique_items = sum(1 for i in items if i["state"] != "duplicate")
    exempt = [i for i in items if i["state"] in
              ("unsupported", "failed", "excluded_with_reason")]
    raw_chars = sum(int(i.get("raw_chars") or 0) for i in items)
    raw_tokens = sum(int(i.get("raw_tokens") or 0) for i in items)

    state_counts = {r["span_state"]: int(r["n"]) for r in _rows(
        conn, "SELECT span_state, COUNT(*) AS n FROM source_spans WHERE domain_id=? "
              "GROUP BY span_state", (domain_id,))}
    total_spans = sum(state_counts.values())
    canonical = state_counts.get("included", 0)

    processed_span_ids = {
        int(r["source_span_id"]) for r in _rows(
            conn, "SELECT DISTINCT source_span_id FROM coverage_ledger "
                  "WHERE domain_id=? AND stage='understand_segments' AND outcome='processed' "
                  "AND source_span_id IS NOT NULL", (domain_id,))
    }
    ledger_span_ids = {
        int(r["source_span_id"]) for r in _rows(
            conn, "SELECT DISTINCT source_span_id FROM coverage_ledger "
                  "WHERE domain_id=? AND source_span_id IS NOT NULL", (domain_id,))
    }
    canonical_rows = _rows(
        conn, "SELECT id, source_id, source_kind, start_ms, end_ms, slide_no, page_no, "
              " token_count, char_count FROM source_spans "
              "WHERE domain_id=? AND span_state='included' ORDER BY ordinal, id", (domain_id,))
    silent_source_ids = [r["source_id"] for r in canonical_rows
                         if int(r["id"]) not in ledger_span_ids]

    tl_rows = [r for r in canonical_rows if r["source_kind"] == "transcript"]
    tl_processed = [r for r in tl_rows if int(r["id"]) in processed_span_ids]
    starts = [r["start_ms"] for r in tl_rows if r["start_ms"] is not None]
    ends = [r["end_ms"] for r in tl_rows if r["end_ms"] is not None]

    ppt_rows = [r for r in canonical_rows if r["source_kind"] == "ppt"]
    ppt_pages = {(r["slide_no"] or r["page_no"]) for r in ppt_rows
                 if (r["slide_no"] or r["page_no"]) is not None}
    ppt_done = {(r["slide_no"] or r["page_no"]) for r in ppt_rows
                if (r["slide_no"] or r["page_no"]) is not None
                and int(r["id"]) in processed_span_ids}

    plan = None
    if _table_exists(conn, "coverage_plans"):
        prow = conn.execute("SELECT * FROM coverage_plans WHERE domain_id=?", (domain_id,)).fetchone()
        plan = dict(prow) if prow else None

    report_row = None
    if _table_exists(conn, "coverage_reports"):
        rrow = conn.execute("SELECT * FROM coverage_reports WHERE run_id=?", (run_id,)).fetchone()
        report_row = dict(rrow) if rrow else None

    ledger = _rows(
        conn, "SELECT stage, outcome, reason_code, COUNT(*) AS n FROM coverage_ledger "
              "WHERE domain_id=? GROUP BY stage, outcome, reason_code "
              "ORDER BY stage, outcome", (domain_id,))

    result["domain_items"] = {
        "total": total_items,
        "unique": unique_items,
        "accounted": accounted,
        "exempt": len(exempt),
        "duplicates": sum(1 for i in items if i["state"] == "duplicate"),
        "raw_chars": raw_chars,
        "raw_tokens_estimate": raw_tokens,
        "items": [
            {"ordinal": i["ordinal"], "material_id": i["material_id"],
             "source_kind": i["source_kind"], "state": i["state"],
             "reason_code": i["reason_code"], "raw_chars": i["raw_chars"],
             "duplicate_of_item_id": i["duplicate_of_item_id"]}
            for i in items
        ],
    }
    result["source_map"] = {
        "total_spans": total_spans,
        "canonical_non_noise_spans": canonical,
        "duplicate_spans": state_counts.get("duplicate", 0),
        "noise_spans": state_counts.get("noise", 0),
        "unsupported_spans": state_counts.get("unsupported", 0),
        "failed_spans": state_counts.get("failed", 0),
        "excluded_spans": state_counts.get("excluded_with_reason", 0),
    }
    result["required_items"] = {
        "total": sum(1 for i in items if i.get("required")),
        "failed": sum(1 for i in items if i.get("required") and i["state"] == "failed"),
        "unsupported": sum(1 for i in items if i.get("required") and i["state"] == "unsupported"),
        "without_canonical_span": sum(
            1 for i in items if i.get("required") and i["state"] != "duplicate"
            and not conn.execute(
                "SELECT 1 FROM source_spans s WHERE s.domain_item_id=? AND ("
                " s.span_state='included' OR (s.span_state='duplicate' AND EXISTS ("
                "   SELECT 1 FROM source_spans c WHERE c.id=s.canonical_span_id "
                "   AND c.span_state='included'))) LIMIT 1", (int(i["id"]),)).fetchone()),
        "spans_unprocessed": 0,  # 下方用 processed 账目计算
    }
    _req_span_rows = conn.execute(
        "SELECT s.id FROM source_spans s JOIN material_domain_items i ON i.id = s.domain_item_id "
        "WHERE s.domain_id=? AND s.span_state='included' AND i.required=1",
        (domain_id,)).fetchall()
    result["required_items"]["spans_unprocessed"] = sum(
        1 for r in _req_span_rows if int(r["id"]) not in processed_span_ids)
    result["coverage"] = {
        "domain_accounting_rate": _ratio(accounted, total_items),
        "semantic_processing_rate": _ratio(len(processed_span_ids & {int(r["id"]) for r in canonical_rows}), canonical),
        "processed_spans": len(processed_span_ids & {int(r["id"]) for r in canonical_rows}),
        "timeline_coverage_rate": _ratio(len(tl_processed), len(tl_rows)),
        "timeline_span_total": len(tl_rows),
        "timeline_span_processed": len(tl_processed),
        "timeline_start": _ms_to_ts(min(starts)) if starts else None,
        "timeline_end": _ms_to_ts(max(ends)) if ends else None,
        "timeline_gaps": _timeline_gaps(sorted(tl_rows, key=lambda r: r["start_ms"] or 0)),
        "ppt_pages_total": len(ppt_pages),
        "ppt_pages_processed": len(ppt_done),
        "silent_dropped": len(silent_source_ids),
        "silent_dropped_source_ids": silent_source_ids[:50],
        "source_refs_total": canonical,
        "source_refs_valid": len(processed_span_ids & {int(r["id"]) for r in canonical_rows}),
    }
    result["plan"] = None if not plan else {
        "strategy": plan.get("strategy"),
        "model_profile_id": plan.get("model_profile_id"),
        "context_window": plan.get("context_window"),
        "input_budget_tokens": plan.get("input_budget_tokens"),
        "output_budget_tokens": plan.get("output_budget_tokens"),
        "planned_span_count": plan.get("planned_span_count"),
        "unassigned_count": plan.get("unassigned_count"),
    }
    result["ledger"] = ledger
    result["report"] = None if not report_row else {
        "gate": report_row.get("gate"),
        "silent_dropped": report_row.get("silent_dropped"),
        "degradation_reason": report_row.get("degradation_reason"),
        "engine_mode": report_row.get("engine_mode"),
        "engine_version": report_row.get("engine_version"),
        "created_at": report_row.get("created_at"),
        "updated_at": report_row.get("updated_at"),
    }
    # ---- Phase 2：segment / 理解 / merge ----
    result["segments"] = _segment_metrics(conn, domain_id, run_id)
    # ---- Phase 3：认知层 ----
    result["cognitive"] = _cognitive_metrics(conn, run_id)
    result["reuse"] = _reuse_metrics(conn, domain_id, run_id)
    result["legacy_material_stats"] = _legacy_material_stats(conn, run_id)
    result["material_utilization"] = _utilization(result)
    return result


def _cognitive_metrics(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    """Phase 3 CognitiveMap 指标（表不存在时返回 available=False）。"""
    if not _table_exists(conn, "cognitive_maps"):
        return {"available": False, "note": "该库低于 v19（无 cognitive_maps）"}
    row = conn.execute("SELECT * FROM cognitive_maps WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        return {"available": True, "present": False,
                "note": "该运行没有 CognitiveMap（Phase 3 未执行）"}
    items = _rows(conn, "SELECT item_type, severity, confidence, origin, status "
                        "FROM cognitive_items WHERE cognitive_map_id=? ORDER BY ordinal",
                  (int(row["id"]),))
    by_type: dict[str, int] = {}
    by_origin: dict[str, int] = {}
    for item in items:
        by_type[item["item_type"]] = by_type.get(item["item_type"], 0) + 1
        by_origin[item["origin"]] = by_origin.get(item["origin"], 0) + 1
    ledger = _rows(conn, "SELECT status, COUNT(*) AS n FROM cognitive_input_ledger "
                         "WHERE run_id=? GROUP BY status", (run_id,))
    return {
        "available": True, "present": True,
        "status": row["status"],
        "model_used": row["model_used"],
        "knowledge_unit_total": int(row["knowledge_unit_total"] or 0),
        "knowledge_unit_processed": int(row["knowledge_unit_processed"] or 0),
        "cognitive_input_coverage": row["cognitive_input_coverage"],
        "batch_count": int(row["batch_count"] or 0),
        "item_count": len(items),
        "by_type": by_type,
        "by_origin": by_origin,
        "input_ledger": {r["status"]: int(r["n"]) for r in ledger},
        "error": row["error"],
    }


def _reuse_metrics(conn: sqlite3.Connection, domain_id: int, run_id: int) -> dict[str, Any]:
    """跨 run 复用的 segment 数（Phase 2 Closeout）。"""
    if not _table_exists(conn, "lesson_segments"):
        return {"available": False}
    rows = _rows(conn, "SELECT COUNT(*) AS n FROM lesson_segments WHERE domain_id=? "
                       "AND status='succeeded' AND error LIKE 'reused from run%'",
                 (domain_id,))
    total = _rows(conn, "SELECT COUNT(*) AS n FROM lesson_segments WHERE domain_id=?",
                  (domain_id,))
    return {
        "available": True,
        "reused_segments": int(rows[0]["n"] or 0) if rows else 0,
        "segment_total": int(total[0]["n"] or 0) if total else 0,
        "reuse_records": len(_rows(conn, "SELECT id FROM segment_reuse_index")),
    }


def _segment_metrics(conn: sqlite3.Connection, domain_id: int, run_id: int) -> dict[str, Any]:
    """Phase 2 分段理解指标（表不存在时返回 ``available=False``）。"""
    if not _table_exists(conn, "lesson_segments"):
        return {"available": False, "note": "该库低于 v18（无 lesson_segments）"}
    segs = _rows(
        conn,
        "SELECT ls.id, ls.ordinal, ls.status, ls.strategy, ls.input_hash, ls.token_count, "
        " ls.primary_span_count, ls.overlap_span_count, ls.attempts, ls.error, "
        " (SELECT COUNT(*) FROM segment_source_spans ss WHERE ss.segment_id=ls.id "
        "  AND ss.role='primary') AS primary_linked, "
        " su.status AS understanding_status, su.model_used, su.source_ref_count "
        "FROM lesson_segments ls "
        "LEFT JOIN segment_understandings su ON su.segment_id = ls.id "
        "WHERE ls.domain_id=? ORDER BY ls.ordinal",
        (domain_id,),
    )
    statuses: dict[str, int] = {}
    for s in segs:
        statuses[s["status"]] = statuses.get(s["status"], 0) + 1
    total_primary = sum(int(s["primary_span_count"] or 0) for s in segs)
    merge = None
    if _table_exists(conn, "lesson_understandings"):
        row = conn.execute(
            "SELECT status, consumed_segment_count, segment_count, merge_levels, "
            " valid_source_count, model_used, input_hash FROM lesson_understandings "
            "WHERE run_id=?", (run_id,)).fetchone()
        merge = dict(row) if row else None
    # 首/中/尾可定位性：转写 span 的时间覆盖范围
    tl = conn.execute(
        "SELECT MIN(start_ms) AS s, MAX(end_ms) AS e, COUNT(*) AS n FROM source_spans "
        "WHERE domain_id=? AND span_state='included' AND start_ms IS NOT NULL",
        (domain_id,)).fetchone()
    return {
        "available": True,
        "segment_count": len(segs),
        "strategy": segs[0]["strategy"] if segs else None,
        "status_counts": statuses,
        "primary_span_total": total_primary,
        "merge": merge,
        "timeline": {
            "timed_span_count": int(tl["n"] or 0) if tl else 0,
            "start_ms": tl["s"] if tl else None,
            "end_ms": tl["e"] if tl else None,
            "start": _ms_to_ts(tl["s"]) if tl and tl["s"] is not None else None,
            "end": _ms_to_ts(tl["e"]) if tl and tl["e"] is not None else None,
        },
        "segments": [
            {"ordinal": s["ordinal"], "status": s["status"],
             "primary_span_count": s["primary_span_count"],
             "primary_linked": s["primary_linked"],
             "overlap_span_count": s["overlap_span_count"],
             "token_count": s["token_count"], "attempts": s["attempts"],
             "understanding_status": s["understanding_status"],
             "model_used": s["model_used"], "source_ref_count": s["source_ref_count"],
             "error": s["error"]}
            for s in segs
        ],
    }


def _timeline_gaps(tl_rows: list[dict], min_gap_ms: int = 60_000) -> list[dict]:
    """时间轴空洞（相邻 span 间隔 > min_gap_ms）。只报告，不掩饰。"""
    gaps = []
    for prev, cur in zip(tl_rows, tl_rows[1:]):
        prev_end = prev.get("end_ms")
        cur_start = cur.get("start_ms")
        if prev_end is None or cur_start is None:
            continue
        gap = int(cur_start) - int(prev_end)
        if gap > min_gap_ms:
            gaps.append({"from": _ms_to_ts(int(prev_end)), "to": _ms_to_ts(int(cur_start)),
                         "gap_ms": gap})
    return gaps


def _legacy_material_stats(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    """旧生成链本轮真实可用的材料体量（只读）。"""
    row = conn.execute("SELECT input_json FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
    material_ids: list[int] = []
    transcript_chars = 0
    if row and row["input_json"]:
        try:
            payload = json.loads(row["input_json"])
            if isinstance(payload, dict):
                material_ids = [int(m) for m in (payload.get("material_ids") or [])]
                transcript_chars = len(str(payload.get("transcript") or ""))
        except Exception:
            pass
    chunk_count = 0
    chunk_chars = 0
    if material_ids:
        q = ",".join("?" * len(material_ids))
        stats = conn.execute(
            f"SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(text)), 0) AS c FROM source_chunks "
            f"WHERE material_id IN ({q})", tuple(material_ids)).fetchone()
        chunk_count = int(stats["n"] or 0)
        chunk_chars = int(stats["c"] or 0)
    return {
        "material_ids": material_ids,
        "transcript_chars": transcript_chars,
        "source_chunks": chunk_count,
        "source_chunk_chars": chunk_chars,
        "candidate_window_chars_upper_bound": LEGACY_CANDIDATE_CHARS * (LEGACY_RUN_CHUNK_LIMIT + LEGACY_RAG_TOP_K),
    }


def _utilization(result: dict[str, Any]) -> dict[str, Any]:
    """当前材料利用率：旧链实际消费的 span / 全部 canonical span。"""
    cov = result.get("coverage") or {}
    canonical = int(cov.get("source_refs_total") or 0)
    valid = int(cov.get("source_refs_valid") or 0)
    return {
        "canonical_spans": canonical,
        "consumed_spans": valid,
        "utilization_rate": _ratio(valid, canonical),
        "semantic_processing_rate": cov.get("semantic_processing_rate"),
        "interpretation": "Phase 1 数值反映旧生成链候选窗口上限，不是 V6 目标值",
    }


def _live_authorized() -> bool:
    return (os.environ.get("V6_BENCHMARK_ALLOW_LIVE") or "").strip() == "1"


def _print_human(result: dict[str, Any]) -> None:
    print("=" * 78)
    print("V6 材料覆盖 / 分段理解 Benchmark（只读）")
    print("=" * 78)
    print(f"生成时间(UTC): {result['generated_at']}")
    run = result.get("run") or {}
    print(f"run_id={run.get('id')} workflow={run.get('workflow')} status={run.get('status')} "
          f"created_at={run.get('created_at')}")
    dom = result.get("domain")
    if not dom:
        print("\n[!] 该 run 无 Material Domain。")
        print(f"    {result.get('note','')}")
        stats = result.get("legacy_material_stats") or {}
        print(f"    材料 material_ids={stats.get('material_ids')} "
              f"source_chunks={stats.get('source_chunks')} chars={stats.get('source_chunk_chars')} "
              f"transcript_chars={stats.get('transcript_chars')}")
        return
    print(f"domain_id={dom['domain_id']} scope={dom['scope']} state={dom['state']} "
          f"engine={dom['engine_version']}")
    print(f"domain_hash={dom['domain_hash']}")
    print(f"course/chapter/lesson = {dom['course_id']}/{dom['chapter_id']}/{dom['lesson_id']}")

    di = result["domain_items"]
    print("\n--- Material Domain ---")
    print(f"  材料总数={di['total']}  唯一材料={di['unique']}  已记账={di['accounted']}  "
          f"重复={di['duplicates']}  显式排除={di['exempt']}")
    print(f"  raw chars={di['raw_chars']}  raw tokens(估算)={di['raw_tokens_estimate']}")
    for it in di["items"]:
        print(f"    #{it['ordinal']:<3} material={str(it['material_id']):>6} "
              f"{it['source_kind']:<10} {it['state']:<22} {it['reason_code'] or ''}"
              + (f" -> item {it['duplicate_of_item_id']}" if it['duplicate_of_item_id'] else ""))

    sm = result["source_map"]
    print("\n--- Source Map ---")
    print(f"  source spans={sm['total_spans']}  canonical 非噪声={sm['canonical_non_noise_spans']}  "
          f"duplicate={sm['duplicate_spans']}  noise={sm['noise_spans']}  "
          f"unsupported={sm['unsupported_spans']}  failed={sm['failed_spans']}  "
          f"excluded={sm['excluded_spans']}")

    cov = result["coverage"]
    print("\n--- Coverage（本地确定性程序计算）---")
    print(f"  domain_accounting_rate   = {cov['domain_accounting_rate']}")
    print(f"  semantic_processing_rate = {cov['semantic_processing_rate']} "
          f"({cov['processed_spans']}/{sm['canonical_non_noise_spans']})")
    print(f"  timeline_coverage_rate   = {cov['timeline_coverage_rate']} "
          f"({cov['timeline_span_processed']}/{cov['timeline_span_total']}) "
          f"range={cov['timeline_start']}~{cov['timeline_end']}")
    if cov["timeline_gaps"]:
        print(f"  时间轴空洞({len(cov['timeline_gaps'])}):")
        for g in cov["timeline_gaps"][:10]:
            print(f"    {g['from']} → {g['to']}  ({g['gap_ms']/1000:.0f}s)")
    print(f"  ppt_pages_processed      = {cov['ppt_pages_processed']}/{cov['ppt_pages_total']}")
    print(f"  source_refs_valid        = {cov['source_refs_valid']}/{cov['source_refs_total']}")
    print(f"  silent_dropped           = {cov['silent_dropped']}")
    if cov["silent_dropped_source_ids"]:
        print(f"    样例: {cov['silent_dropped_source_ids'][:10]}")
    req = result.get("required_items") or {}
    if req:
        print(f"  required 材料: 总数={req.get('total')} 失败={req.get('failed')} "
              f"不支持={req.get('unsupported')} 无 canonical span={req.get('without_canonical_span')} "
              f"未处理 span={req.get('spans_unprocessed')}")

    plan = result.get("plan")
    if plan:
        print("\n--- Coverage Plan ---")
        print(f"  strategy={plan['strategy']} planned={plan['planned_span_count']} "
              f"unassigned={plan['unassigned_count']} "
              f"input_budget={plan['input_budget_tokens']} ctx={plan['context_window']}")

    rep = result.get("report")
    if rep:
        print("\n--- Coverage Report ---")
        print(f"  gate={rep['gate']}  silent_dropped={rep['silent_dropped']}  mode={rep['engine_mode']}")
        if rep["degradation_reason"]:
            print(f"  degradation_reason={rep['degradation_reason']}")

    print("\n--- Coverage Ledger ---")
    for row in result.get("ledger") or []:
        print(f"  {row['stage']:<22} {row['outcome']:<12} {row['reason_code'] or '':<34} {row['n']}")

    util = result.get("material_utilization") or {}
    print("\n--- 材料利用率 ---")
    print(f"  canonical spans={util.get('canonical_spans')} consumed={util.get('consumed_spans')} "
          f"utilization={util.get('utilization_rate')}")

    seg = result.get("segments") or {}
    if seg.get("available"):
        print("\n--- Phase 2 分段理解 ---")
        print(f"  segment 数量={seg['segment_count']} 策略={seg['strategy']} "
              f"primary span 合计={seg['primary_span_total']}")
        print(f"  状态分布={seg['status_counts']}")
        tl = seg.get("timeline") or {}
        print(f"  有时间轴 span={tl.get('timed_span_count')} 范围={tl.get('start')}~{tl.get('end')}")
        merge = seg.get("merge")
        if merge:
            print(f"  merge: status={merge['status']} "
                  f"consumed={merge['consumed_segment_count']}/{merge['segment_count']} "
                  f"levels={merge['merge_levels']} valid_sources={merge['valid_source_count']}")
        for s in seg["segments"]:
            print(f"    #{s['ordinal']:<3} {s['status']:<10} primary={s['primary_span_count']:<4}"
                  f" linked={s['primary_linked']:<4} overlap={s['overlap_span_count']:<3}"
                  f" attempts={s['attempts']} understanding={s['understanding_status']}"
                  f" model={s['model_used']}")
            if s.get("error"):
                print(f"        error: {s['error']}")
    cog = result.get("cognitive") or {}
    if cog.get("available") and cog.get("present"):
        print("\n--- Phase 3 认知分析 ---")
        print(f"  status={cog['status']} items={cog['item_count']} "
              f"KU 覆盖={cog['knowledge_unit_processed']}/{cog['knowledge_unit_total']} "
              f"({cog['cognitive_input_coverage']}) batch={cog['batch_count']}")
        if cog["by_type"]:
            print(f"  八类分布: {cog['by_type']}")
        if cog["by_origin"]:
            print(f"  来源分布: {cog['by_origin']}")
        print(f"  输入账本: {cog['input_ledger']}")
        if cog.get("error"):
            print(f"  error: {cog['error']}")
    elif cog.get("available"):
        print("\n--- Phase 3 认知分析 ---")
        print(f"  {cog.get('note')}")

    reuse = result.get("reuse") or {}
    if reuse.get("available"):
        print("\n--- 跨 run 复用 ---")
        print(f"  复用 segment={reuse['reused_segments']}/{reuse['segment_total']} "
              f"复用审计记录={reuse['reuse_records']}")

    print("\n--- 旧链材料体量 ---")
    legacy = result.get("legacy_material_stats") or {}
    print(f"  material_ids={legacy.get('material_ids')} "
          f"chunks={legacy.get('source_chunks')} chars={legacy.get('source_chunk_chars')} "
          f"transcript_chars={legacy.get('transcript_chars')}")
    print(f"  旧链候选窗口上限: run_chunk={LEGACY_RUN_CHUNK_LIMIT} + rag_top_k={LEGACY_RAG_TOP_K} "
          f"(设计文档 §2.1)")
    print("\n计算口径:")
    for k, v in (result.get("caliber") or {}).items():
        print(f"  {k}: {v}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="V6 Phase 1 材料覆盖 Benchmark（默认只读、默认不调用真实模型）"
    )
    parser.add_argument("--db", default=str(DEFAULT_DB),
                        help=f"SQLite 路径（默认 {DEFAULT_DB}，以 mode=ro 打开）")
    parser.add_argument("--run-id", type=int, default=None, help="要复算的 run id")
    parser.add_argument("--list", action="store_true", help="列出最近的 run")
    parser.add_argument("--limit", type=int, default=20, help="--list 的条数")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    parser.add_argument("--live", action="store_true",
                        help="允许真实模型调用（还需 V6_BENCHMARK_ALLOW_LIVE=1）")
    args = parser.parse_args(argv)

    if args.live and not _live_authorized():
        print("[benchmark] 拒绝：--live 需要显式环境变量授权 "
              "V6_BENCHMARK_ALLOW_LIVE=1（默认不调用真实模型）。", file=sys.stderr)
        return 2
    if args.live:
        # Phase 1 的 benchmark 只复算覆盖口径，本身不需要模型调用。
        print("[benchmark] 注意：--live 已授权，但本脚本不发起模型调用；"
              "真实课堂 live benchmark 属于后续阶段。", file=sys.stderr)

    db_path = Path(args.db).resolve()
    conn = _read_only_conn(db_path)
    try:
        if args.list:
            rows = list_runs(conn, args.limit)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                print(f"{'run_id':>7} {'workflow':<10} {'status':<12} {'domain':>7} "
                      f"{'gate':<10} {'engine':<18} created_at")
                for r in rows:
                    print(f"{r['id']:>7} {str(r['workflow']):<10} {str(r['status']):<12} "
                          f"{str(r['domain_id'] or '-'):>7} {str(r['gate'] or '-'):<10} "
                          f"{str(r['engine_version'] or '-'):<18} {r['created_at']}")
            return 0

        run_id = args.run_id
        if run_id is None:
            row = conn.execute(
                "SELECT id FROM workflow_runs ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                print("[benchmark] 库中没有 workflow_runs 记录。", file=sys.stderr)
                return 1
            run_id = int(row["id"])
            print(f"[benchmark] 未指定 --run-id，使用最新 run {run_id}", file=sys.stderr)

        result = compute_metrics(conn, run_id)
        result["db_path_note"] = "只读打开（mode=ro），未写入、未迁移"
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            _print_human(result)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
