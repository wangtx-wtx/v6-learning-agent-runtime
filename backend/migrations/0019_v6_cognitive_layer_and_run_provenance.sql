-- 0019_v6_cognitive_layer_and_run_provenance.sql
-- V6 Phase 2 Closeout（run 级材料来源）+ Phase 3 Cognitive Layer.
--
-- 只追加,不修改 0016/0017/0018 等已发布迁移.
--
-- 设计文档迁移编号顺延（原设计把 Evidence V2 预留为 0019）:
--   Evidence V2   0019 -> 0020
--   Quality States 0020 -> 0021
--   Learning Loop  0021 -> 0022
--
-- 一、run 级材料来源（Phase 2 Closeout，修复历史转写被删除/串课）
--
-- inline 转写此前按 lesson_id 删除并重建 material_id IS NULL 的 chunk，导致
-- 同一课时的其他 run 原始材料被抹掉，且相同 locator 会复用旧正文。现在为
-- source_chunks 增加 origin_run_id + inline_text_hash：
--   * origin_run_id 明确归属：只允许复用**同一 run**的 chunk，绝不跨 run 删除；
--   * inline_text_hash 参与同一 run 内重入幂等判定（locator + 文本哈希）；
--   * 既有两个 NULL 值列可空，历史数据与文件材料 chunk 不受影响（向后兼容）。

ALTER TABLE source_chunks ADD COLUMN origin_run_id INTEGER REFERENCES workflow_runs(id) ON DELETE SET NULL;
ALTER TABLE source_chunks ADD COLUMN inline_text_hash TEXT;

CREATE INDEX idx_source_chunks_origin_run ON source_chunks(origin_run_id);
CREATE INDEX idx_source_chunks_inline ON source_chunks(lesson_id, origin_run_id, locator);

-- 二、Phase 3 Cognitive Layer 五表

CREATE TABLE cognitive_maps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL UNIQUE REFERENCES workflow_runs(id) ON DELETE CASCADE,
    domain_id INTEGER NOT NULL REFERENCES material_domains(id) ON DELETE CASCADE,
    lesson_id INTEGER REFERENCES lessons(id) ON DELETE SET NULL,
    schema_version TEXT NOT NULL DEFAULT 'v6.1',
    prompt_version TEXT NOT NULL DEFAULT 'v1',
    model_used TEXT,
    input_hash TEXT NOT NULL,
    structured_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    knowledge_unit_total INTEGER NOT NULL DEFAULT 0,
    knowledge_unit_processed INTEGER NOT NULL DEFAULT 0,
    cognitive_input_coverage REAL NOT NULL DEFAULT 0.0,
    batch_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'succeeded'
        CHECK(status IN ('succeeded','partial','failed','empty')),
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    CHECK(knowledge_unit_total >= 0),
    CHECK(knowledge_unit_processed >= 0),
    CHECK(cognitive_input_coverage >= 0.0 AND cognitive_input_coverage <= 1.0),
    CHECK(batch_count >= 0)
);
CREATE INDEX idx_cognitive_maps_domain ON cognitive_maps(domain_id);
CREATE INDEX idx_cognitive_maps_status ON cognitive_maps(status);

CREATE TABLE cognitive_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cognitive_map_id INTEGER NOT NULL REFERENCES cognitive_maps(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    stable_key TEXT NOT NULL,
    item_type TEXT NOT NULL
        CHECK(item_type IN ('confusion_point','prerequisite_gap','pitfall','emphasis',
                            'concept_relation','memory_anchor','missing_step','difficulty')),
    title TEXT NOT NULL DEFAULT '',
    explanation TEXT NOT NULL DEFAULT '',
    severity REAL NOT NULL DEFAULT 0.0,
    confidence REAL NOT NULL DEFAULT 0.0,
    recommended_treatment TEXT NOT NULL DEFAULT 'explanation'
        CHECK(recommended_treatment IN ('explanation','example','contrast','drill',
                                        'memory_hook','prerequisite_review','none')),
    origin TEXT NOT NULL DEFAULT 'model_cognitive_inference'
        CHECK(origin IN ('classroom_evidence','confirmed_error','model_cognitive_inference',
                         'chapter_note','mastery_signal','homework_feedback')),
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK(status IN ('proposed','accepted','rejected')),
    ordinal INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(cognitive_map_id, stable_key),
    CHECK(severity >= 0.0 AND severity <= 1.0),
    CHECK(confidence >= 0.0 AND confidence <= 1.0),
    CHECK(ordinal >= 1)
);
CREATE INDEX idx_cognitive_items_map ON cognitive_items(cognitive_map_id, item_type);
CREATE INDEX idx_cognitive_items_run ON cognitive_items(run_id);
CREATE INDEX idx_cognitive_items_type ON cognitive_items(item_type);

CREATE TABLE cognitive_item_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cognitive_item_id INTEGER NOT NULL REFERENCES cognitive_items(id) ON DELETE CASCADE,
    source_span_id INTEGER REFERENCES source_spans(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'supports'
        CHECK(relation IN ('supports','contradicts','illustrates')),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(cognitive_item_id, source_id)
);
CREATE INDEX idx_cognitive_item_sources_item ON cognitive_item_sources(cognitive_item_id);
CREATE INDEX idx_cognitive_item_sources_span ON cognitive_item_sources(source_span_id);

CREATE TABLE cognitive_item_knowledge_units (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cognitive_item_id INTEGER NOT NULL REFERENCES cognitive_items(id) ON DELETE CASCADE,
    knowledge_unit_key TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'about'
        CHECK(relation IN ('about','requires','contrasts','extends')),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(cognitive_item_id, knowledge_unit_key)
);
CREATE INDEX idx_cognitive_item_ku_item ON cognitive_item_knowledge_units(cognitive_item_id);
CREATE INDEX idx_cognitive_item_ku_key ON cognitive_item_knowledge_units(knowledge_unit_key);

-- 认知分析的输入账本：证明每个 Knowledge Unit 都进入了某个 batch（覆盖率 1.0）
CREATE TABLE cognitive_input_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    cognitive_map_id INTEGER REFERENCES cognitive_maps(id) ON DELETE CASCADE,
    knowledge_unit_key TEXT NOT NULL,
    batch_ordinal INTEGER NOT NULL DEFAULT 1,
    input_hash TEXT,
    origin TEXT NOT NULL DEFAULT 'lesson_understanding'
        CHECK(origin IN ('lesson_understanding','chapter_note','confirmed_error',
                         'mastery','homework_feedback')),
    status TEXT NOT NULL DEFAULT 'consumed'
        CHECK(status IN ('consumed','skipped','failed')),
    reason_code TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(run_id, knowledge_unit_key, origin),
    CHECK(status <> 'skipped' OR reason_code IS NOT NULL),
    CHECK(batch_ordinal >= 1)
);
CREATE INDEX idx_cognitive_input_ledger_run ON cognitive_input_ledger(run_id, status);
CREATE INDEX idx_cognitive_input_ledger_map ON cognitive_input_ledger(cognitive_map_id);

-- 三、跨 run segment 复用（Phase 2 Closeout：segment_reuse_index 真正接线）
--
-- domain_hash 含 run_id，不能作为跨 run 内容相等依据；新增不含 run_id 的
-- reuse_scope_hash（material_domains 上落一份，便于 join 判定）。
ALTER TABLE material_domains ADD COLUMN reuse_scope_hash TEXT;
CREATE INDEX idx_material_domains_reuse_scope ON material_domains(reuse_scope_hash);

-- 这里只补充查找索引列（表本身在 0018 已建）。
ALTER TABLE segment_reuse_index ADD COLUMN reuse_scope_hash TEXT;
ALTER TABLE segment_reuse_index ADD COLUMN segment_ordinal INTEGER;
ALTER TABLE segment_reuse_index ADD COLUMN primary_source_ids TEXT;
ALTER TABLE segment_reuse_index ADD COLUMN source_run_id INTEGER REFERENCES workflow_runs(id) ON DELETE SET NULL;
ALTER TABLE segment_reuse_index ADD COLUMN source_segment_id INTEGER;
ALTER TABLE segment_reuse_index ADD COLUMN reuse_reason TEXT;

CREATE INDEX idx_segment_reuse_scope
    ON segment_reuse_index(reuse_scope_hash, input_hash, prompt_version, schema_version, model_used);
