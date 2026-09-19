"""可由前端管理的模型注册表与角色路由。"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Optional

from .database import fetch_all, fetch_one, transaction

CAPABILITIES = {"text", "vision", "multimodal", "ocr", "embedding", "rerank"}
MODEL_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def _capability_allowed_for_role(role: str, capability: str) -> bool:
    if role == "embedding":
        return capability == "embedding"
    if role == "rerank":
        return capability == "rerank"
    if role == "ocr":
        return capability in {"ocr", "vision", "multimodal"}
    if role == "vision_reader":
        return capability in {"vision", "multimodal"}
    return capability in {"text", "vision", "multimodal"}


@dataclass(frozen=True)
class ModelSpec:
    id: str
    gateway_model: str
    provider: str
    family: str
    capability: str = "text"
    enabled: bool = True
    is_builtin: bool = False
    context_window: Optional[int] = None
    embedding_dimensions: Optional[int] = None
    input_cost: Optional[float] = None
    output_cost: Optional[float] = None
    cost_unit: str = "unknown"
    notes: str = ""
    is_default_solver: bool = False
    is_default_reviewer: bool = False
    is_default_vision: bool = False
    is_default_light: bool = False


# 数据库尚未初始化时的安全回退；正常运行时以数据库配置为准。
MODELS = {
    "deepseek_v4_free": ModelSpec("deepseek_v4_free", "deepseek-flash", "deepseek", "deepseek", "multimodal", is_builtin=True, is_default_solver=True),
    "deepseek_v4_official": ModelSpec("deepseek_v4_official", "deepseek-v4-pro", "deepseek", "deepseek", enabled=False, is_builtin=True),
    "qwen3_8_27b": ModelSpec("qwen3_8_27b", "qwen3.8-27b", "qwen", "qwen", "multimodal", enabled=False, is_builtin=True),
    "qwen3_flash": ModelSpec("qwen3_flash", "qwen3.8-flash", "qwen", "qwen", "multimodal", is_builtin=True, is_default_light=True),
    "qwen3_vl": ModelSpec("qwen3_vl", "qwen3-vl-flash", "qwen", "qwen", "vision", enabled=False, is_builtin=True),
    "ocr": ModelSpec("ocr", "glm-ocr", "zhipu", "glm", "ocr", is_builtin=True),
    "minimax_m3": ModelSpec("minimax_m3", "MiniMax-M3", "minimax", "minimax", "multimodal", enabled=False, is_builtin=True),
    "glm_flash": ModelSpec("glm_flash", "glm-5.3-flash", "zhipu", "glm", "multimodal", is_builtin=True, is_default_reviewer=True),
    "embedding": ModelSpec("embedding", "text-embedding-v4", "qwen", "qwen", "embedding", is_builtin=True, context_window=8192, embedding_dimensions=1024),
    "rerank": ModelSpec("rerank", "qwen3-rerank", "qwen", "qwen", "rerank", is_builtin=True, context_window=4000),
}

ROLE_MODELS: dict[str, list[str]] = {
    "vision_reader": ["deepseek_v4_free", "qwen3_flash", "glm_flash"],
    "transcriber_splitter": ["qwen3_flash", "deepseek_v4_free"],
    "lesson_structurer": ["qwen3_flash", "deepseek_v4_free"],
    # V6 Phase 2：全量分段理解（map）。用长上下文模型承载整段材料。
    "segment_understanding": ["qwen3_flash", "deepseek_v4_free", "glm_flash"],
    # V6 Phase 2 增强：分段边界规划（节点 05.5）。只需长上下文 + 稳定 JSON，
    # 输出只有 ordinal 区间，体量极小。
    "segment_boundary_planner": ["qwen3_flash", "deepseek_v4_free", "glm_flash"],
    "student_simulator": ["deepseek_v4_free", "qwen3_flash", "glm_flash"],
    "note_writer": ["deepseek_v4_free", "qwen3_flash", "glm_flash"],
    "critic": ["glm_flash", "qwen3_flash"],
    "evidence_auditor": ["glm_flash", "qwen3_flash"],
    "scope_auditor": ["glm_flash", "qwen3_flash"],
    "solver": ["deepseek_v4_free", "glm_flash"],
    "parallel_solver": ["glm_flash", "qwen3_flash"],
    "solution_explainer": ["glm_flash", "deepseek_v4_free"],
    "error_analyst": ["deepseek_v4_free", "glm_flash"],
    "review_writer": ["deepseek_v4_free", "glm_flash"],
    "self_test_writer": ["qwen3_flash", "deepseek_v4_free"],
    "ocr": ["ocr", "deepseek_v4_free"],
    "embedding": ["embedding"],
    "rerank": ["rerank"],
    "adjudicator": ["glm_flash", "qwen3_flash"],
}


def _row_spec(row: dict) -> ModelSpec:
    return ModelSpec(
        id=row["id"], gateway_model=row["gateway_model"], provider=row["provider"],
        family=row["family"], capability=row["capability"], enabled=bool(row["enabled"]),
        is_builtin=bool(row["is_builtin"]), context_window=row.get("context_window"),
        embedding_dimensions=row.get("embedding_dimensions"),
        input_cost=row.get("input_cost"), output_cost=row.get("output_cost"),
        cost_unit=row.get("cost_unit") or "unknown", notes=row.get("notes") or "",
    )


def _db_models(enabled_only: bool = False) -> list[dict]:
    try:
        where = " WHERE enabled=1" if enabled_only else ""
        return fetch_all("SELECT * FROM model_profiles" + where + " ORDER BY family,id")
    except Exception:
        return []


def get_model(model_id: str) -> ModelSpec:
    try:
        row = fetch_one("SELECT * FROM model_profiles WHERE id=?", (model_id,))
    except Exception:
        row = None
    if row:
        if not row["enabled"]:
            raise KeyError(f"模型已停用: {model_id}")
        return _row_spec(row)
    fallback = MODELS.get(model_id)
    if fallback and fallback.enabled:
        return fallback
    raise KeyError(f"未知或已停用模型 id: {model_id}")


def get_role_models(role: str, *, enabled_only: bool = True) -> list[str]:
    try:
        rows = fetch_all(
            "SELECT r.model_id FROM model_role_routes r JOIN model_profiles m ON m.id=r.model_id "
            "WHERE r.role=?" + (" AND m.enabled=1" if enabled_only else "") + " ORDER BY r.position",
            (role,),
        )
    except Exception:
        rows = []
    return [r["model_id"] for r in rows] or list(ROLE_MODELS.get(role, []))


def list_role_routes() -> dict[str, list[str]]:
    roles = set(ROLE_MODELS)
    try:
        roles.update(r["role"] for r in fetch_all("SELECT DISTINCT role FROM model_role_routes"))
    except Exception:
        pass
    return {role: get_role_models(role, enabled_only=False) for role in sorted(roles)}


def list_models() -> list[dict]:
    rows = _db_models()
    specs = [_row_spec(r) for r in rows] if rows else list(MODELS.values())
    routes = list_role_routes()
    primary = {role: ids[0] for role, ids in routes.items() if ids}
    result = []
    for spec in specs:
        item = asdict(spec)
        item["is_default_solver"] = primary.get("solver") == spec.id
        item["is_default_reviewer"] = primary.get("critic") == spec.id
        item["is_default_vision"] = primary.get("vision_reader") == spec.id
        item["is_default_light"] = primary.get("self_test_writer") == spec.id
        result.append(item)
    return result


def save_model(data: dict) -> dict:
    model_id = str(data.get("id") or "").strip()
    capability = str(data.get("capability") or "text").strip().lower()
    if not MODEL_ID_RE.fullmatch(model_id):
        raise ValueError("内部 ID 只能使用小写字母、数字和下划线，且必须以字母开头")
    if capability not in CAPABILITIES:
        raise ValueError(f"不支持的能力类型: {capability}")
    gateway_model = str(data.get("gateway_model") or "").strip()
    provider = str(data.get("provider") or "").strip()
    family = str(data.get("family") or provider or "other").strip().lower()
    if not gateway_model or not provider:
        raise ValueError("网关模型名和供应商不能为空")
    if max(len(model_id), len(gateway_model), len(provider), len(family)) > 128:
        raise ValueError("模型字段过长")
    values = (
        model_id, gateway_model, provider, family, capability,
        1 if data.get("enabled", True) else 0,
        int(data["context_window"]) if data.get("context_window") else None,
        int(data["embedding_dimensions"]) if data.get("embedding_dimensions") else None,
        float(data["input_cost"]) if data.get("input_cost") is not None else None,
        float(data["output_cost"]) if data.get("output_cost") is not None else None,
        str(data.get("cost_unit") or "unknown")[:80], str(data.get("notes") or "")[:500],
    )
    with transaction() as conn:
        existing = conn.execute("SELECT * FROM model_profiles WHERE id=?", (model_id,)).fetchone()
        if existing and existing["capability"] == "embedding":
            old_dims = existing["embedding_dimensions"]
            new_dims = values[7]
            if old_dims and new_dims and old_dims != new_dims:
                indexed = conn.execute(
                    "SELECT COUNT(*) FROM source_chunks WHERE embedding IS NOT NULL AND embedding != ''"
                ).fetchone()[0]
                if indexed:
                    raise ValueError("已有向量索引，不能直接修改维度；请先建立新索引版本")
        builtin = int(existing["is_builtin"]) if existing else 0
        conn.execute(
            "INSERT INTO model_profiles (id,gateway_model,provider,family,capability,enabled,is_builtin,context_window,embedding_dimensions,input_cost,output_cost,cost_unit,notes,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime')) "
            "ON CONFLICT(id) DO UPDATE SET gateway_model=excluded.gateway_model,provider=excluded.provider,family=excluded.family,capability=excluded.capability,enabled=excluded.enabled,context_window=excluded.context_window,embedding_dimensions=excluded.embedding_dimensions,input_cost=excluded.input_cost,output_cost=excluded.output_cost,cost_unit=excluded.cost_unit,notes=excluded.notes,updated_at=excluded.updated_at",
            values[:6] + (builtin,) + values[6:],
        )
    return next(m for m in list_models() if m["id"] == model_id)


def set_model_enabled(model_id: str, enabled: bool) -> None:
    if not enabled:
        used = fetch_one("SELECT role FROM model_role_routes WHERE model_id=? AND position=0", (model_id,))
        if used:
            raise ValueError(f"模型仍是角色 {used['role']} 的首选，请先调整路由")
    with transaction() as conn:
        cur = conn.execute("UPDATE model_profiles SET enabled=?,updated_at=datetime('now','localtime') WHERE id=?", (1 if enabled else 0, model_id))
        if cur.rowcount == 0:
            raise KeyError(model_id)


def set_role_models(role: str, model_ids: list[str]) -> list[str]:
    role = role.strip()
    clean = list(dict.fromkeys(x.strip() for x in model_ids if x and x.strip()))
    if not role or len(role) > 64 or not clean:
        raise ValueError("角色至少需要一个候选模型")
    if len(clean) > 5:
        raise ValueError("每个角色最多配置 5 个候选模型")
    with transaction() as conn:
        placeholders = ",".join("?" for _ in clean)
        rows = conn.execute(f"SELECT id,capability FROM model_profiles WHERE enabled=1 AND id IN ({placeholders})", tuple(clean)).fetchall()
        found = {r["id"] for r in rows}
        missing = [m for m in clean if m not in found]
        if missing:
            raise ValueError(f"模型不存在或已停用: {missing}")
        incompatible = [r["id"] for r in rows if not _capability_allowed_for_role(role, r["capability"])]
        if incompatible:
            raise ValueError(f"模型能力与角色 {role} 不兼容: {incompatible}")
        conn.execute("DELETE FROM model_role_routes WHERE role=?", (role,))
        conn.executemany("INSERT INTO model_role_routes(role,position,model_id) VALUES (?,?,?)", [(role, i, m) for i, m in enumerate(clean)])
    return clean
