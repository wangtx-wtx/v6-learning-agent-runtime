-- 0009_v564_gateway_mode.sql — V5.6.4 网关模式审计字段
--
-- 背景：
--   V5.5.1 model_calls 表（migration 0005）记录每次模型调用的 prompt / 用量 / 错误码；
--   但无法区分调用来自「真实网关」还是「fake / replay」。V5.6.4 引入统一网关模式
--   后，必须在审计层明确：调用走 fake / replay / live 哪条路径，replay 是否命中。
--
-- 本迁移：
--   - gateway_mode: 'fake' / 'replay' / 'live'；CHECK 约束防止非法值。
--     DEFAULT 'live' 兼容 v8 库升级：历史记录默认视为 live（最保守）。
--   - replay_key: replay 模式下唯一标识 (op, model, contract, prompt, schema, ...)
--     的 64-hex SHA-256；replay 命中时非空。
--   - replay_hit: 1=命中, 0=miss / 未尝试。
--   - 双索引便于按 mode 统计与按 replay key 调试。

ALTER TABLE model_calls ADD COLUMN gateway_mode TEXT NOT NULL DEFAULT 'live'
    CHECK (gateway_mode IN ('fake', 'replay', 'live'));

ALTER TABLE model_calls ADD COLUMN replay_key TEXT;

ALTER TABLE model_calls ADD COLUMN replay_hit INTEGER
    CHECK (replay_hit IN (0, 1) OR replay_hit IS NULL);

CREATE INDEX IF NOT EXISTS idx_model_calls_gateway_mode
    ON model_calls(gateway_mode, created_at);

CREATE INDEX IF NOT EXISTS idx_model_calls_replay_key
    ON model_calls(replay_key);
