-- 0021_v6_evidence_producer_provenance.sql
-- V6 Learning Engine Phase 4.1: 逐 claim、逐生产者的证据可见性（P1 修复）。
--
-- 只追加,不修改 0016—0020; 不改动 0020 已建的 content_claims / claim_sources 结构语义。
--
-- 背景（已在隔离库复现的缺陷）:
--   Phase 4 的 Binder 用「本 run 内所有认知批次的已呈现来源并集」判断
--   presented_to_producer。于是 batch 1 产出的 claim 只要被篡改为引用
--   「batch 2 读过」的来源，就会被误判成「模型已读过」。这违反 V6 原则:
--     「数据库中存在」≠「该 producer 实际读过」；
--     「其他 batch 读过」≠「生成该 claim 的 batch 读过」。
--
-- 修复: 每条 claim 持久化**自己**的生产可见性 provenance，Binder 只认它。
--   provenance 缺失 / 无法解析 → producer_visibility_missing → claim failed,
--   绝不 fallback 到 run/domain 级全局集合。
--
-- 字段（producer_provenance_json, JSON 对象）至少包含:
--   producer_stage      产生该 claim 的阶段标识
--   producer_node       产生该 claim 的节点
--   producer_kind       lesson_understanding | cognitive_map
--   invocation_refs     该 claim 来自哪些 segment / cognitive batch
--   visible_source_ids  该 producer invocation **实际发送**的 Source ID
--   visibility_basis    segment_input_ledger | cognitive_batch_audit | unavailable
--   provenance_version  口径版本（变更即让旧 provenance 失效）

ALTER TABLE content_claims ADD COLUMN producer_provenance_json TEXT NOT NULL DEFAULT '{}';

-- 便于按 producer 反查（审计「这条 claim 用了哪个 batch/segment 的输入」）。
-- 刻意**不**给 JSON 文本列建索引：SQLite 上对长文本建索引只会浪费空间，
-- 逐 claim 的 provenance 查询一律走 claim_id / run_id。
CREATE INDEX idx_content_claims_producer_node ON content_claims(run_id, producer_node);

-- 逐来源的可见性终态：把「模型是否真的在本 claim 的 producer 调用里读过它」
-- 落库，API / 审计 / 前端都能如实展示，而不必再回推全局集合。
ALTER TABLE claim_sources ADD COLUMN visibility TEXT NOT NULL DEFAULT 'unknown'
    CHECK(visibility IN ('visible','not_visible','unknown'));

-- 真正发过该来源的 invocation（segment / cognitive batch ordinal 的 JSON 数组）
ALTER TABLE claim_sources ADD COLUMN visible_invocations_json TEXT NOT NULL DEFAULT '[]';

-- 回填历史行（0020 时期写入的 claim_sources）：
--   presented_to_producer=1 的按 visible 记，其余记为 not_visible（保守：宁可不通过）。
-- visibility 未知一律落到 not_visible，绝不因为「默认值」而放行。
UPDATE claim_sources SET visibility = CASE
    WHEN presented_to_producer = 1 THEN 'visible'
    WHEN binding_status = 'bound' THEN 'not_visible'
    ELSE 'unknown'
END;

CREATE INDEX idx_claim_sources_visibility ON claim_sources(visibility);
