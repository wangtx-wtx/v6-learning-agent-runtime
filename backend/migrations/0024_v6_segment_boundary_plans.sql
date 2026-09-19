-- 0024_v6_segment_boundary_plans.sql
-- V6 Learning Engine Phase 2 增强：分段边界建议账（节点 05.5 plan_segment_boundaries）.
--
-- 只追加, 不修改 0023 及更早的已发布迁移.
--
-- 关键不变量:
--   * 本表只存「建议」, 不是最终分段。最终分段仍写入 lesson_segments /
--     segment_source_spans, 且仍由 segment.plan_segments 的确定性装箱产生。
--   * (domain_id, input_hash) 唯一: 同一材料 + 同一上界只保留一份建议,
--     保证跨 run 复用且不因模型抖动产生不同切法（幂等）。
--   * status 五态: pending / succeeded / low_quality / fallback / failed。
--     非 succeeded 一律回退结构边界, 不得阻断 run。
--   * input_hash 计入 max_segment_tokens: 上界变化必须使建议失效重算。
--   * boundaries_json 只含 ordinal 整数区间, 不含原文、不含字符偏移
--     （source_spans 无法表达「半个 span」）。

CREATE TABLE segment_boundary_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_id INTEGER NOT NULL REFERENCES material_domains(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    model_used TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    span_count INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    max_segment_tokens INTEGER NOT NULL DEFAULT 0,
    boundaries_json TEXT NOT NULL DEFAULT '{}',
    validation_json TEXT NOT NULL DEFAULT '{}',
    segment_count INTEGER NOT NULL DEFAULT 0,
    ku_count INTEGER NOT NULL DEFAULT 0,
    oversized_ku_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','succeeded','low_quality','fallback','failed')),
    detail TEXT,
    attempts INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    CHECK(span_count >= 0),
    CHECK(segment_count >= 0),
    CHECK(ku_count >= 0),
    CHECK(oversized_ku_count >= 0)
);

CREATE INDEX idx_segment_boundary_plans_run
    ON segment_boundary_plans(run_id);
CREATE INDEX idx_segment_boundary_plans_domain
    ON segment_boundary_plans(domain_id, status);
-- 同一材料 + 同一上界只保留一份建议（跨 run 复用 + 幂等）
CREATE UNIQUE INDEX uq_segment_boundary_plans_domain_hash
    ON segment_boundary_plans(domain_id, input_hash);
