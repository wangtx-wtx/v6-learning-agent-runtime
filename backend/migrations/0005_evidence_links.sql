-- 0005_evidence_links.sql — 证据/检索/模型调用审计（V5.5 方案 10.3/11.3）
-- retrieval_runs 让"输出差"可归因于生成问题还是检索问题；
-- model_calls 记录每次模型调用的版本与用量（不保存密钥、不保存完整敏感输入）。

CREATE TABLE IF NOT EXISTS retrieval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_run_id INTEGER REFERENCES workflow_runs(id) ON DELETE CASCADE,
    query_text TEXT,
    filters_json TEXT,
    retrieval_mode TEXT,          -- hybrid | keyword_only
    candidate_count INTEGER DEFAULT 0,
    selected_chunk_ids TEXT,      -- JSON array
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS model_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES workflow_runs(id) ON DELETE SET NULL,
    node_id TEXT,
    attempt INTEGER DEFAULT 1,
    model_id TEXT,
    gateway_model TEXT,
    trace_id TEXT,
    prompt_name TEXT,
    prompt_version TEXT,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    status TEXT,                  -- ok | error
    error_code TEXT,
    input_digest TEXT,            -- 敏感输入只存哈希/长度摘要
    input_chars INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_retrieval_runs_run ON retrieval_runs(workflow_run_id);
CREATE INDEX IF NOT EXISTS idx_model_calls_run ON model_calls(run_id);
CREATE INDEX IF NOT EXISTS idx_model_calls_status ON model_calls(status, created_at);
