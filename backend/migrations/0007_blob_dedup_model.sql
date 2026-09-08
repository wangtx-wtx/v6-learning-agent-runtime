-- 0007_blob_dedup_model.sql — Blob 去重模型修正（V5.5 方案 4.1）
-- materials 不再是去重主体：同一内容 = 一条 file_blobs + N 条 materials 引用。
-- 0006 的 materials(file_hash) 唯一索引与该模型冲突（去重上传必须新建材料记录），
-- 降级为普通索引；内容级唯一性由 file_blobs.sha256 承担。

DROP INDEX IF EXISTS idx_materials_file_hash;
CREATE INDEX IF NOT EXISTS idx_materials_file_hash
    ON materials(file_hash) WHERE file_hash IS NOT NULL AND file_hash != '';
