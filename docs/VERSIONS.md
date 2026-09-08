# 版本统一说明

## 当前口径（V5.5 稳定闭环版）

| 维度 | 值 | 出现位置 |
| --- | --- | --- |
| 产品版本 | **5.5.0** | `app/main.py` FastAPI(version)、`/api/health` 返回、docs 全部 |
| DB Schema | **7**（migrations 0001–0007） | `schema_migrations`、`PRAGMA user_version`、tests |
| API | `/api` v1 路由风格（无版本化前缀变更） | `app/main.py` |
| 提示词 | `<name>.v1.md`（版本化文件名 + sha256） | `backend/prompts/` |

> 说明：V5.5 方案文本曾写 "Schema 版本 6"，0007 迁移落地后实际为 **7**，以数据库为准。

## 沿革

| 版本 | 要点 |
| --- | --- |
| v5.1 | 初始单体：课程/材料/四条工作流/复习 |
| v5.2 | 备份 API 化、jieba 分词、mobile-token |
| v5.4 | DAG 引擎重写、Worker 管理器、证据校验 |
| **5.5.0** | 稳定闭环：连接管理重做、Worker 恢复/真并发/取消/重试、blob 去重+物理GC、复习 SM-2 闭环、听课 critic 修订闭环、错题事件、作业 OCR 状态机与先做后看、Obsidian 真实路径、Prompt 外置+混合检索+模型审计、前端 router/SSE/DTO、备份恢复工具、测试/CI/文档体系 |

## 一致性检查点

改动以下任意一处必须同步全部其余位置：

1. `app/main.py` → `FastAPI(version=...)` 与 `/api/health` 返回
2. `backend/migrations/` 最新版本号 ↔ `docs/README.md`/`docs/OPERATIONS.md`/`docs/VERSIONS.md` 的 Schema 值
3. `docs/` 各文档顶部版本行
4. `MODIFICATION_REPORT.md` 的版本标题
