-- 0003_workflow_queue.sql — 任务队列恢复/并发/取消支持（V5.5 方案 3.2/3.3/3.4）
-- lease 机制避免多 Worker 重复领取；cancel_requested 支持运行中取消；
-- parent_run_id 支持不可变重试运行（V5.5 方案 3.5）。

ALTER TABLE run_tasks ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE run_tasks ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3;
ALTER TABLE run_tasks ADD COLUMN lease_owner TEXT;
ALTER TABLE run_tasks ADD COLUMN lease_expires_at TEXT;
ALTER TABLE run_tasks ADD COLUMN started_at TEXT;
ALTER TABLE run_tasks ADD COLUMN updated_at TEXT;
ALTER TABLE run_tasks ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;

ALTER TABLE parse_tasks ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE parse_tasks ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3;
ALTER TABLE parse_tasks ADD COLUMN lease_owner TEXT;
ALTER TABLE parse_tasks ADD COLUMN lease_expires_at TEXT;
ALTER TABLE parse_tasks ADD COLUMN started_at TEXT;
ALTER TABLE parse_tasks ADD COLUMN updated_at TEXT;
ALTER TABLE parse_tasks ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;

ALTER TABLE sync_jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sync_jobs ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3;
ALTER TABLE sync_jobs ADD COLUMN lease_owner TEXT;
ALTER TABLE sync_jobs ADD COLUMN lease_expires_at TEXT;
ALTER TABLE sync_jobs ADD COLUMN updated_at TEXT;

ALTER TABLE workflow_runs ADD COLUMN parent_run_id INTEGER REFERENCES workflow_runs(id) ON DELETE SET NULL;
ALTER TABLE workflow_runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_run_tasks_status ON run_tasks(status, id);
CREATE INDEX IF NOT EXISTS idx_parse_tasks_status ON parse_tasks(status, id);
