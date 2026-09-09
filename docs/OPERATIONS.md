# 运维手册

## 日常启动

```powershell
cd v5\backend
python -m uvicorn app.main:app --port 8801     # 后端
cd ..\frontend
npm run dev                                     # 前端开发（或用 dist/ 静态产物）
```

- 后端健康检查：`GET http://127.0.0.1:8801/api/health` → `{"status":"ok","version":"5.5.1"}`
- 启动时自动：备份 → 中断恢复 → 清理 24h 前临时文件 → 启动 Worker → 健康自检。

## 数据库迁移

```powershell
cd v5\backend
python -m tools.migrate_database      # 影子迁移：构建新库→校验→备份→替换
```

- 迁移文件在 `backend/migrations/`，**已应用的文件带 sha256 锁定，不可修改**；变更一律新增 `000N_xxx.sql`。
- 旧库首次启动会抛 `MigrationRequiredError`，按提示执行上述命令。

## 备份与恢复

```powershell
# 自动：每次启动备份到 backend/data/backups/v5_*.db（保留 7 份）
# 手动：POST /api/backups

# 恢复（先校验、pre_restore 备份、原子替换）
python -m tools.restore_snapshot --latest --dry-run              # 只校验，绝不修改目标库
python -m tools.restore_snapshot --latest                        # 实际恢复（停机状态下执行）
python -m tools.restore_snapshot --file backups\v5_20250101_120000.db

# 旧版本 v7 备份升级到 v8（dry-run 也安全，不写永久副本）
python -m tools.restore_snapshot --file v7_snap.db --upgrade-to 8 --dry-run
python -m tools.restore_snapshot --file v7_snap.db --upgrade-to 8           # 永久恢复
python -m tools.restore_snapshot --file v7_snap.db --upgrade-to 8 --overwrite  # 覆盖已有副本
```

## 存储管理

- 存储审计：`GET /api/admin/storage/audit`（blob 引用、孤儿、without_blob 计数）
- 手动 GC：`POST /api/admin/storage/gc`
- 物理文件在 `backend/data/blobs/<sha2>/<sha256>.<ext>`；删除材料只解引用，GC 异步清理。

## 常见排障

| 现象 | 处置 |
| --- | --- |
| `database is locked` | 确认写路径走短连接/`transaction()`；WAL 模式下 busy_timeout=15s 兜底 |
| 运行卡在 running | 重启后端（recovery 自动重置）；或 `POST /api/runs/{id}/cancel` |
| 材料解析失败 | `POST /api/materials/{id}/retry`；`needs_ocr`/`transcribing` 为类型矩阵预期状态 |
| Obsidian 未同步 | 查 `sync_jobs` 的 retries/last_error；vault 路径见 `GET /api/sync/vault` |
| 模型调用失败 | 查 `model_calls.status='error'` 的 error_code；网关用量 `GET /api/usage` |

## 版本口径（统一）

- 产品版本：**5.5.1**（`/api/health` 返回值）
- DB Schema：**v8**（migrations 0001–0008）
- API：`/api` 前缀 v1 路由风格
