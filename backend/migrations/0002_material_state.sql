-- 0002_material_state.sql — 材料 Blob 拆分（V5.5 方案 4.1）
-- 相同文件内容只保存一份物理 blob；materials 变为业务引用记录。
-- 去重上传时创建新的 materials 记录并复用 blob，不再修改旧材料归属。

CREATE TABLE IF NOT EXISTS file_blobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    storage_path TEXT NOT NULL,
    size_bytes INTEGER DEFAULT 0,
    mime TEXT,
    ref_count INTEGER NOT NULL DEFAULT 0,
    gc_state TEXT NOT NULL DEFAULT 'live',   -- live | pending_delete | deleted
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 已有库：为 materials 增加 blob 引用列（新列默认 NULL，历史数据由影子迁移回填）
ALTER TABLE materials ADD COLUMN blob_id INTEGER REFERENCES file_blobs(id);

CREATE INDEX IF NOT EXISTS idx_materials_blob ON materials(blob_id);
CREATE INDEX IF NOT EXISTS idx_file_blobs_sha ON file_blobs(sha256);

-- 物理 GC 任务表（删除失败时的重试队列，V5.5 方案 4.2）
CREATE TABLE IF NOT EXISTS blob_gc_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    blob_id INTEGER REFERENCES file_blobs(id) ON DELETE CASCADE,
    storage_path TEXT NOT NULL,
    reason TEXT,
    error TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    finished_at TEXT
);
