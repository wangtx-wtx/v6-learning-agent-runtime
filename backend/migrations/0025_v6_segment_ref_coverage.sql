-- 0025_v6_segment_ref_coverage.sql
-- V6 Phase 2 增强：段内引用覆盖率观测（不参与门禁）.
--
-- 只追加, 不修改 0024 及更早的已发布迁移.
--
-- 背景（这是「覆盖审计困难」的正面修复）:
--   * 现行语义：segment 理解成功 ⇒ 该段**全部** primary span 无条件记账为
--     processed（understanding._understand_with_retries）。覆盖率因此只能证明
--     「该段被调用过」，无法证明「段内内容被模型注意到」。
--   * 在长上下文下这会掩盖「中间迷失」：模型可能只引用开头与结尾，中段被忽略，
--     而 semantic_processing_rate 仍报 100%。
--
-- 本迁移只**新增观测列**，不改变任何门禁语义：
--   * primary_span_count          该段 primary span 数（分母）
--   * referenced_primary_count    模型实际引用的 primary span 数（分子）
--   * unreferenced_source_ids     未被引用的 Source ID（JSON 数组，限量），供人工定位
--
-- 刻意**不**把「未被引用」当作 not_used 记账，也**不**纳入 evaluate_gate：
-- 课堂材料里大量过渡性内容（「我们来看下一页」）本就不会被引用，
-- 若按引用率卡门禁会让几乎所有 run 误降级。它是**诊断指标**，不是门禁条件。

ALTER TABLE segment_understandings
    ADD COLUMN primary_span_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE segment_understandings
    ADD COLUMN referenced_primary_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE segment_understandings
    ADD COLUMN unreferenced_source_ids TEXT NOT NULL DEFAULT '[]';
