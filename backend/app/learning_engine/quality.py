"""Independent V6 quality, publication and sync state handling."""
from __future__ import annotations

import json
from typing import Any, Optional

from .. import database as db


def ensure_quality_state(run_id: int, **values: Any) -> dict:
    allowed = {
        "note_id", "processing_status", "coverage_status", "evidence_status",
        "review_status", "publication_status", "sync_status", "degradation_reason",
        "metrics_json",
    }
    payload = {k: v for k, v in values.items() if k in allowed}
    if isinstance(payload.get("metrics_json"), (dict, list)):
        payload["metrics_json"] = json.dumps(payload["metrics_json"], ensure_ascii=False)
    with db.transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO learning_quality_states (run_id) VALUES (?)",
                     (int(run_id),))
        if payload:
            cols = list(payload)
            conn.execute(
                "UPDATE learning_quality_states SET "
                + ",".join(f"{c}=?" for c in cols)
                + ",updated_at=datetime('now','localtime') WHERE run_id=?",
                tuple(payload[c] for c in cols) + (int(run_id),),
            )
    return get_quality_state(run_id) or {}


def get_quality_state(run_id: int) -> Optional[dict]:
    row = db.fetch_one("SELECT * FROM learning_quality_states WHERE run_id=?", (int(run_id),))
    if not row:
        return None
    out = dict(row)
    try:
        out["metrics"] = json.loads(out.get("metrics_json") or "{}")
    except Exception:
        out["metrics"] = {}
    return out


def degradation_reason(run_id: int) -> Optional[str]:
    state = get_quality_state(run_id)
    if not state:
        return "V6 quality state missing"
    failures = []
    expected = {
        "processing_status": "completed",
        "coverage_status": "passed",
        "evidence_status": "passed",
        "review_status": "passed",
        "publication_status": "rendered",
    }
    for key, value in expected.items():
        if state.get(key) != value:
            failures.append(f"{key}={state.get(key)}")
    if failures:
        return state.get("degradation_reason") or "; ".join(failures)
    return None
