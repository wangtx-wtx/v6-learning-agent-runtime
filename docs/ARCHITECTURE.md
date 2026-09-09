# 技术架构

## 技术栈

- **后端**：Python 3.12 / FastAPI（lifespan，无 on_event）/ pydantic 2 / sqlite3 原生（WAL, busy_timeout=15000）
- **前端**：Vue 3 + Vite + TypeScript + Tailwind 4 + vue-router 4（动态分包）+ ECharts 按需引入
- **模型**：本地网关 `http://127.0.0.1:8080`（deepseek_v4_free、qwen3_8_27b、qwen3_flash、minimax_m3、qwen3_vl、embedding），只读访问，密钥经环境变量

## 模块划分（backend/app）

| 模块 | 职责 |
| --- | --- |
| `main.py` | FastAPI 路由（课程/材料/工作流/run/复习/错题/备份/管理端点） |
| `lifecycle.py` | lifespan：init → 备份 → 恢复 → GC 临时文件 → Worker 启动 → 健康自检；停机反序 |
| `database.py` | 连接策略：读=线程本地连接；写（execute/insert/executemany）=专用短连接；`transaction()` 独立连接 + BEGIN IMMEDIATE + 3 次忙等重试；测试 `configure_db/is_override` |
| `dag.py` | DAG 引擎：拓扑分层、模型重试（schema 错误先修复）、取消检查（层/节点/模型重试边界）、输出落库 |
| `dag_lesson/homework/error/review.py` | 四条业务流节点（prompt 外置 + pydantic schema 绑定） |
| `workers/` | `manager.py`（supervisor，max 2 runs + 2 parses）、`workflow_worker.py`（原子认领 + 600s 租约 + 心跳续约）、`material_worker.py`、`recovery.py`（重启恢复） |
| `integrations/prompts.py` | 提示词外置加载：name+version、sha256 checksum、进程内缓存、测试夹具覆盖（`V5_PROMPT_FIXTURE_DIR`） |
| `integrations/schemas.py` | 每个模型节点的输出 schema（pydantic），失败抛 `SchemaValidationError`，禁止 `or {}` 静默兜底 |
| `rag.py` | 混合检索：`0.45×BM25 + 0.40×向量余弦 + 0.10×层级 + 0.05×短语`；embedding 不可用自动降级 `keyword_only`；每次检索写 `retrieval_runs` |
| `gateway.py` | 网关客户端：连接池、退避重试、熔断、trace id；**每次 chat 写 `model_calls` 审计**（run/node/attempt/prompt 版本/token/时延/状态，敏感输入只存 sha256 摘要） |
| `services/blob.py` + `blob_gc.py` | blob 去重/引用/复活/物理 GC/存储审计/临时文件清理 |
| `review_service.py` | SM-2 简化变体：rating 0/1→1d, 2→3d, 3→×1.8, 4→×2.5, 5→×3.2，clamp [1,180]；连续 2 次错重置；掌握度 ±0.15/−0.25 clamp [0,1] |
| `obsidian.py` | vault 路径解析（级联 lesson→chapter→course）、Windows 文件名清洗、稳定 ID 命名 `[note-<id>]` |
| `backup.py` | SQLite backup API 在线备份，保留 7 份 |

## 数据模型

- 迁移：`backend/migrations/0001-0008`，`schema_migrations` 记录版本+sha256（篡改锁定：已应用文件不可改，只能新增）。当前 **schema_version=8**（0007：materials.file_hash 去唯一化，配合 blob 去重；0008：新增通用 system_metadata 表 + 同步 PRAGMA user_version）。
- 关键表：`workflow_runs`（状态机含 interrupted/取消请求）、`run_nodes`（attempt/租约）、`file_blobs`（ref_count/gc_state）、`questions`（student_answer/submitted_at/reveal_allowed）、`answer_items`（conflict）、`errors` + `error_events`、`retrieval_runs`、`model_calls`、`notes`（markdown_path/status）。
- 旧库升级：`python -m tools.migrate_database`（影子迁移：新文件构建→完整性校验→备份原库→原子替换）。

## 可靠性设计

1. **并发写安全**：asyncio 单线程下共享连接会交错 BEGIN——写一律短连接；跨语句原子性用 `transaction()` 独立连接。
2. **任务不丢**：认领即置 running+lease；心跳续约；崩溃后 recovery 将 running/interrupted 重置 queued，超过 max_attempts 才 failed。
3. **取消语义**：`RunCancelledError(BaseException)` 贯穿 DAG；节点取消也落 run_nodes 记录；解析循环每 50 段检查取消。
4. **可归因**：`retrieval_runs` 区分检索问题 vs 生成问题；`model_calls` 逐次调用审计；提示词 checksum 可复现。
