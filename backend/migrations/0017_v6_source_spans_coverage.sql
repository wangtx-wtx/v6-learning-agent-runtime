-- 0017_v6_source_spans_coverage.sql
-- V6 Learning Engine Phase 1: 稳定 Source Map + Coverage Plan / Ledger / Report.
--
-- 只追加,不修改既有 migration; 不破坏 source_chunks (V6 在其之上建立稳定映射).
--
-- 关键不变量:
--   * (domain_id, source_id) 唯一 —— Source ID 在同一个 frozen domain version 内稳定.
--   * 重复 span 仍保留记录, 通过 canonical_span_id 指向唯一版本.
--   * 噪声/重复/失败/未使用都必须有明确 span_state, 非 included 的必须有 reason_code.
--   * outcome='not_used' 必须有 reason_code (CHECK).
--   * silent_dropped 由确定性程序计算写入 coverage_reports, 禁止模型填写.

CREATE TABLE source_spans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    domain_id INTEGER NOT NULL REFERENCES material_domains(id) ON DELETE CASCADE,
    domain_item_id INTEGER REFERENCES material_domain_items(id) ON DELETE SET NULL,
    source_chunk_id INTEGER REFERENCES source_chunks(id) ON DELETE SET NULL,
    material_id INTEGER REFERENCES materials(id) ON DELETE SET NULL,
    source_kind TEXT NOT NULL,
    locator TEXT,
    ordinal INTEGER NOT NULL,
    start_ms INTEGER,
    end_ms INTEGER,
    page_no INTEGER,
    slide_no INTEGER,
    text TEXT NOT NULL DEFAULT '',
    normalized_text_hash TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    char_count INTEGER NOT NULL DEFAULT 0,
    canonical_span_id INTEGER REFERENCES source_spans(id) ON DELETE SET NULL,
    span_state TEXT NOT NULL DEFAULT 'included'
        CHECK(span_state IN ('included','duplicate','noise','unsupported','failed','excluded_with_reason')),
    reason_code TEXT,
    reason_detail TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(domain_id, source_id),
    CHECK(span_state = 'included' OR reason_code IS NOT NULL),
    CHECK(span_state <> 'duplicate' OR canonical_span_id IS NOT NULL)
);
CREATE INDEX idx_source_spans_domain ON source_spans(domain_id);
CREATE INDEX idx_source_spans_span ON source_spans(canonical_span_id);
CREATE INDEX idx_source_spans_material ON source_spans(material_id);
CREATE INDEX idx_source_spans_chunk ON source_spans(source_chunk_id);
CREATE INDEX idx_source_spans_state ON source_spans(domain_id, span_state);
CREATE INDEX idx_source_spans_domain_item ON source_spans(domain_item_id);

CREATE TABLE coverage_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    domain_id INTEGER NOT NULL REFERENCES material_domains(id) ON DELETE CASCADE,
    strategy TEXT NOT NULL CHECK(strategy IN ('single_pass','segmented_map_merge')),
    model_profile_id TEXT,
    context_window INTEGER NOT NULL DEFAULT 0,
    reserved_output_tokens INTEGER NOT NULL DEFAULT 0,
    reserved_system_tokens INTEGER NOT NULL DEFAULT 0,
    input_budget_tokens INTEGER NOT NULL DEFAULT 0,
    output_budget_tokens INTEGER NOT NULL DEFAULT 0,
    planned_span_count INTEGER NOT NULL DEFAULT 0,
    unassigned_count INTEGER NOT NULL DEFAULT 0,
    plan_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(domain_id)
);
CREATE INDEX idx_coverage_plans_run ON coverage_plans(run_id);

CREATE TABLE coverage_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    domain_id INTEGER NOT NULL REFERENCES material_domains(id) ON DELETE CASCADE,
    source_span_id INTEGER REFERENCES source_spans(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    outcome TEXT NOT NULL
        CHECK(outcome IN ('processed','not_used','duplicate','noise','unsupported','failed','excluded')),
    reason_code TEXT,
    reason_detail TEXT,
    segment_id INTEGER,
    knowledge_unit_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    CHECK(outcome <> 'not_used' OR reason_code IS NOT NULL),
    UNIQUE(domain_id, source_span_id, stage)
);
CREATE INDEX idx_coverage_ledger_run ON coverage_ledger(run_id, stage);
CREATE INDEX idx_coverage_ledger_domain ON coverage_ledger(domain_id);
CREATE INDEX idx_coverage_ledger_span ON coverage_ledger(source_span_id);
CREATE INDEX idx_coverage_ledger_outcome ON coverage_ledger(domain_id, stage, outcome);

CREATE TABLE coverage_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL UNIQUE REFERENCES workflow_runs(id) ON DELETE CASCADE,
    domain_id INTEGER NOT NULL REFERENCES material_domains(id) ON DELETE CASCADE,
    engine_version TEXT NOT NULL DEFAULT 'v6-phase1',
    engine_mode TEXT NOT NULL DEFAULT 'off' CHECK(engine_mode IN ('off','shadow','on')),
    metrics_json TEXT NOT NULL DEFAULT '{}',
    gate TEXT NOT NULL CHECK(gate IN ('passed','degraded','failed')),
    degradation_reason TEXT,
    silent_dropped INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_coverage_reports_domain ON coverage_reports(domain_id);
CREATE INDEX idx_coverage_reports_gate ON coverage_reports(gate);
