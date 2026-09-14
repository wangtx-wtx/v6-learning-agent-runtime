-- 0018_v6_learning_intermediates.sql
-- V6 Learning Engine Phase 2: Full Lesson Understanding 中间产物.
--
-- 只追加,不修改 0016/0017 等已发布迁移.
--
-- 关键不变量:
--   * 每个 canonical 非噪声 span 必须恰好属于一个 **primary** segment
--     ((domain_id, source_span_id) 在 primary 角色下唯一) —— overlap 只为上下文,
--     不得计入覆盖率。
--   * segment 输入哈希稳定, 支持幂等与断点复用。
--   * segment 顺序 (ordinal) 保持课堂原始顺序, 不允许相关性重排。
--   * LessonUnderstanding 必须记录 consumed_segment_count / segment_count,
--     以便覆盖审计证明"全部 segment 都被消费"。
--   * lesson_segments.run_id 与 domain_id 必须属于同一 run (由应用层保证, 见
--     segment.py; 此处以 CHECK 保证非负与计数一致性)。

CREATE TABLE lesson_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    domain_id INTEGER NOT NULL REFERENCES material_domains(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    strategy TEXT NOT NULL DEFAULT 'single_pass'
        CHECK(strategy IN ('single_pass','segmented_map_merge')),
    title TEXT,
    input_hash TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    primary_span_count INTEGER NOT NULL DEFAULT 0,
    overlap_span_count INTEGER NOT NULL DEFAULT 0,
    start_ms INTEGER,
    end_ms INTEGER,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','running','succeeded','failed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    output_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(domain_id, ordinal),
    CHECK(primary_span_count >= 0),
    CHECK(overlap_span_count >= 0),
    CHECK(ordinal >= 1)
);
CREATE INDEX idx_lesson_segments_run ON lesson_segments(run_id);
CREATE INDEX idx_lesson_segments_domain ON lesson_segments(domain_id, ordinal);
CREATE INDEX idx_lesson_segments_status ON lesson_segments(domain_id, status);
CREATE INDEX idx_lesson_segments_input_hash ON lesson_segments(run_id, input_hash);

CREATE TABLE segment_source_spans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id INTEGER NOT NULL REFERENCES lesson_segments(id) ON DELETE CASCADE,
    source_span_id INTEGER NOT NULL REFERENCES source_spans(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'primary'
        CHECK(role IN ('primary','overlap')),
    ordinal INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_segment_source_spans_segment ON segment_source_spans(segment_id, role, ordinal);
CREATE INDEX idx_segment_source_spans_span ON segment_source_spans(source_span_id);
-- 一个 span 只能是一个 segment 的 primary（覆盖率分母的唯一性保证）
CREATE UNIQUE INDEX uq_segment_primary_span
    ON segment_source_spans(source_span_id) WHERE role = 'primary';

CREATE TABLE segment_understandings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id INTEGER NOT NULL UNIQUE REFERENCES lesson_segments(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    domain_id INTEGER NOT NULL REFERENCES material_domains(id) ON DELETE CASCADE,
    schema_version TEXT NOT NULL DEFAULT 'v6.1',
    prompt_version TEXT NOT NULL DEFAULT 'v1',
    model_used TEXT,
    input_hash TEXT NOT NULL,
    structured_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_ref_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'succeeded'
        CHECK(status IN ('succeeded','failed')),
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_segment_understandings_run ON segment_understandings(run_id, status);
CREATE INDEX idx_segment_understandings_hash ON segment_understandings(input_hash);

CREATE TABLE lesson_understandings (
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
    consumed_segment_count INTEGER NOT NULL DEFAULT 0,
    segment_count INTEGER NOT NULL DEFAULT 0,
    merge_levels INTEGER NOT NULL DEFAULT 1,
    valid_source_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'succeeded'
        CHECK(status IN ('succeeded','partial','failed')),
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    CHECK(consumed_segment_count >= 0),
    CHECK(segment_count >= 0),
    CHECK(merge_levels >= 1)
);
CREATE INDEX idx_lesson_understandings_domain ON lesson_understandings(domain_id);
CREATE INDEX idx_lesson_understandings_status ON lesson_understandings(status);

-- 按 (run, 节点, 输入哈希) 复用成功 segment 的查找索引（断点复用 / child run 复用）
CREATE TABLE segment_reuse_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    model_used TEXT,
    segment_id INTEGER NOT NULL REFERENCES lesson_segments(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(domain_hash, input_hash, prompt_version, schema_version)
);
CREATE INDEX idx_segment_reuse_lookup
    ON segment_reuse_index(domain_hash, input_hash, prompt_version, schema_version);
