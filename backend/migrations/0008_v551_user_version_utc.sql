-- 0008_v551_user_version_utc.sql — V5.5.1 一致性补丁
--
-- 背景（V5.5 阶段六遗留问题）：
--   V5.5 阶段六生产库 schema_migrations MAX(version) = 7，但
--   PRAGMA user_version 仍为 0。docs/VERSIONS.md 与 restore_snapshot.py
--   都把 user_version 列为版本依据。
--
-- 本迁移未在生产库正式应用；它是为升级路径预置的目标。
-- 升级前生产库通常为 schema 7 + user_version 0；
-- 应用 0008 后（首次 ensure_schema() 运行）：schema 8 + user_version 8。
-- 仅在新库或经由 tools.restore_snapshot --upgrade-to 8 升级的旧库上
-- 才会实际执行本 SQL。
--
-- 设计（D.1 D 阶段）：
--   - 不再创建一次性 marker 表（_v551_user_version_marker）；
--   - 引入通用 system_metadata(key,value) 表作为长期元数据载体；
--     当前无字段需求，仅占位（不写值）；
--   - PRAGMA user_version 的同步由 database.sync_user_version(conn)
--     在 ensure_schema 内部、迁移 commit 之后作用于目标连接完成；
--     本 SQL 文件不直接做 PRAGMA（否则会与 DDL 同事务被回滚）。

CREATE TABLE IF NOT EXISTS system_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
