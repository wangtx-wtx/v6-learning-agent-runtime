# 版本统一说明

## 当前口径（V5.5.1 稳定性补丁）

| 维度 | 值 | 出现位置 |
| --- | --- | --- |
| 产品版本 | **5.5.1** | `app/main.py` FastAPI(version)、`/api/health` 返回、`docs/` 全部、MODIFICATION_REPORT、frontend/package.json |
| DB Schema | **8**（migrations 0001–0008） | `schema_migrations`、`PRAGMA user_version`、tests、restore_snapshot |
| API | `/api` v1 路由风格（无版本化前缀变更） | `app/main.py` |
| 提示词 | `<name>.v1.md`（版本化文件名 + sha256） | `backend/prompts/` |

> V5.5 收尾修订：0008 落地后 DB Schema 实际为 8；`PRAGMA user_version` 由
> `app.database.sync_user_version(conn)` 在 `ensure_schema` 内部与
> `schema_migrations MAX(version)` 同步。

## 沿革

| 版本 | 要点 |
| --- | --- |
| v5.1 | 初始单体：课程/材料/四条工作流/复习 |
| v5.2 | 备份 API 化、jieba 分词、mobile-token |
| v5.4 | DAG 引擎重写、Worker 管理器、证据校验 |
| v5.5 | 稳定闭环：连接管理重做、Worker 恢复/真并发/取消/重试、blob 去重+物理GC、复习 SM-2 闭环、听课 critic 修订闭环、错题事件、作业 OCR 状态机与先做后看、Obsidian 真实路径、Prompt 外置+混合检索+模型审计、前端 router/SSE/DTO、备份恢复工具、测试/CI/文档体系 |
| **5.5.1** | 稳定性补丁：原子单实例锁、UTC 统一租约、PRAGMA user_version 同步、恢复工具严格校验、health 拆分为 public/admin/lifespan 启动失败释放锁 |

## 一致性检查点

改动以下任意一处必须同步全部其余位置：

1. `app/main.py` → `PRODUCT_VERSION`、`FastAPI(version=...)` 与 `/api/health` 返回
2. `frontend/package.json` 的 `version` 字段
3. `backend/migrations/` 最新版本号 ↔ `docs/README.md`/`docs/OPERATIONS.md`/`docs/VERSIONS.md` 的 Schema 值
4. `MODIFICATION_REPORT.md` 的版本标题
5. `backend/app/main.py` `FastAPI(title=...)` 中的版本号
