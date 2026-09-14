-- 0020_v6_evidence_v2.sql
-- V6 Learning Engine Phase 4: Evidence V2（模型只选 Source ID，程序绑定真实原文）.
--
-- 只追加,不修改既有 migration; 不改动 0016—0019; 不触碰 source_spans / source_chunks.
--
-- 关键不变量:
--   * 模型**不得**生成 exact quote: bound_quote 一律由程序从 source_spans.text 复制,
--     quote_hash / source_text_hash 由程序计算（模型 output 里的 quote 字段被忽略）。
--   * (claim_id, source_id, relation) 唯一 —— 同一 claim 对同一来源只有一条绑定。
--   * claim_type / importance / evidence_status / relation / binding_status /
--     binding_method 全部有 CHECK, 与 Pydantic 契约一一对应。
--   * 历史 run 的 claims 不受新 run 影响; 同一 run 重跑按 (run_id, claim_key) 幂等收敛。
--   * ai_explanation 不要求课堂来源（not_required），但必须显式标注类型,
--     不得降级成 classroom_fact。

-- 一、内容主张（claim）：由确定性 shadow projection 从 V6 中间产物生成
CREATE TABLE IF NOT EXISTS content_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    domain_id INTEGER NOT NULL REFERENCES material_domains(id) ON DELETE CASCADE,
    lesson_understanding_id INTEGER REFERENCES lesson_understandings(id) ON DELETE SET NULL,
    cognitive_map_id INTEGER REFERENCES cognitive_maps(id) ON DELETE SET NULL,
    -- Phase 5 Composer V2 才产出正式笔记块; 在此之前允许 NULL（不得谎称已完成）
    note_id INTEGER REFERENCES notes(id) ON DELETE SET NULL,
    block_id TEXT,
    claim_key TEXT NOT NULL,
    claim_type TEXT NOT NULL
        CHECK(claim_type IN ('classroom_fact','classroom_paraphrase','ai_explanation')),
    claim_text TEXT NOT NULL DEFAULT '',
    importance TEXT NOT NULL DEFAULT 'supporting'
        CHECK(importance IN ('critical','major','supporting')),
    source_refs_json TEXT NOT NULL DEFAULT '[]',
    -- 生成该 claim 的节点（便于归因：lesson_understanding / cognitive_map / ...）
    producer_node TEXT NOT NULL DEFAULT '',
    -- 生成该 claim 的输入哈希（可复算：输入变则 claim 变）
    producer_input_hash TEXT NOT NULL DEFAULT '',
    evidence_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(evidence_status IN ('pending','passed','failed','not_required')),
    -- 确定性程序计算, 不接受外部传入
    input_hash TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(run_id, claim_key)
);
CREATE INDEX idx_content_claims_run ON content_claims(run_id);
CREATE INDEX idx_content_claims_domain ON content_claims(domain_id);
CREATE INDEX idx_content_claims_status ON content_claims(run_id, evidence_status);
CREATE INDEX idx_content_claims_type ON content_claims(run_id, claim_type);
CREATE INDEX idx_content_claims_importance ON content_claims(run_id, importance);
CREATE INDEX idx_content_claims_key ON content_claims(claim_key);
CREATE INDEX idx_content_claims_lu ON content_claims(lesson_understanding_id);
CREATE INDEX idx_content_claims_cm ON content_claims(cognitive_map_id);

-- 二、claim 的逐来源绑定（bound_quote 只可能来自数据库）
CREATE TABLE IF NOT EXISTS claim_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES content_claims(id) ON DELETE CASCADE,
    -- 可空: 模型给出伪造/跨 domain Source ID 时无法解析到 span, 但绑定行必须留痕
    source_span_id INTEGER REFERENCES source_spans(id) ON DELETE SET NULL,
    source_id TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'supports'
        CHECK(relation IN ('supports','contradicts','illustrates')),
    binding_status TEXT NOT NULL DEFAULT 'missing'
        CHECK(binding_status IN ('bound','invalid','missing','not_required')),
    binding_method TEXT NOT NULL DEFAULT 'deterministic_source_id'
        CHECK(binding_method IN ('deterministic_source_id')),
    bound_quote TEXT NOT NULL DEFAULT '',
    quote_hash TEXT NOT NULL DEFAULT '',
    source_text_hash TEXT NOT NULL DEFAULT '',
    locator TEXT,
    start_ms INTEGER,
    end_ms INTEGER,
    page_no INTEGER,
    slide_no INTEGER,
    source_kind TEXT,
    -- 模型是否真的在本批看过这条来源（Phase 3 的 included/omitted 边界）
    presented_to_producer INTEGER NOT NULL DEFAULT 0
        CHECK(presented_to_producer IN (0,1)),
    failure_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(claim_id, source_id, relation)
);
CREATE INDEX idx_claim_sources_claim ON claim_sources(claim_id);
CREATE INDEX idx_claim_sources_run_span ON claim_sources(source_span_id);
CREATE INDEX idx_claim_sources_source ON claim_sources(source_id);
CREATE INDEX idx_claim_sources_status ON claim_sources(binding_status);
CREATE INDEX idx_claim_sources_method ON claim_sources(binding_method);
