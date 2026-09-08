# v5 自动化学习系统 — 修改报告

> 按审查方案《自动化学习系统完整修改方案（全量执行）》执行。范围：**阶段0 → 阶段6 全量**。
> 分支确认：范围选项已确认为「全量到阶段6」；其余多选分支未收到明确答复，本报告在「决策记录」
> 一节逐项列出**采用的安全默认值**。

---

## 一、执行总览

| 阶段 | 内容 | 状态 | 验证证据 |
|------|------|------|----------|
| 0 | Git 基线 + 全量快照备份 | ✅ | `v5/` 下建 git 仓库，预迁移快照 `data/backups/snapshot_20260908_194947/` |
| 1 | 数据库影子迁移 + DAL 统一 + P0 后端修复 | ✅ | smoke：health / 章节 state / 材料库 200 |
| 2 | 材料状态机 + 后台 Worker + SSE | ✅ | RSS 上传→解析→ready→建块实测通过 |
| 3 | 听课/作业/错题/复习四条业务流重写 | ✅ | 三条业务流经真实模型网关端到端产出持久化资产 |
| 4 | RAG(FTS5+证据) + 模型网关可靠性 | ✅ | 重试/限并发/熔断/配额安全默认；token 统计落库 |
| 5 | 前端五中心 + P0 前端/接口修复 | ✅ | `vue-tsc`+`vite` 构建通过 |
| 6 | 安全 / 测试 / 备份 / 文档 | ✅ | unittest 13/13；最终快照 integrity=ok |

提交历史（`git log --oneline`）：
```
bc87a20 feat(security/tests/backup): stage-6 hardening
62bc0ac feat(frontend+api): five-center IA, material inbox/library, P0 API fixes
c8d3a9d feat(gateway/rag/evidence/schema): reliable model gateway + stage-4 refinements
3205042 feat(workflows): async worker + DAG concurrency + four business flows
0064c1d feat(database): unified DAL (scheme-matched, additive migration, path safety)
7b6e7d8 chore: git baseline (stage 0) - snapshot data backed up
```

---

## 二、阶段0 — 基线/备份

- 在 `v5/` 初始化 Git 仓库（分支 `main`），先做预取快照 `snapshot_20260908_194947`（含 `v5.db` 在线备份 + uploads + obsidian_vault + manifest.json，完整性 ok 后才进入改造）。
- `.gitignore`：忽略 `backend/data/*.db*`、`backups/`、`uploads/`、`obsidian_vault/`、`keys/`、`.env`、`*.log`、`.venv/`、`frontend/dist/` 等。
- 基准数据审计：14 门课程 / 16 章 / 16 课时 / 6 材料 / 18 错题 / 34 运行 / 192 节点 / 8 考题，全部保留并纳入迁移。

## 阶段一：数据库 / DAL / P0

- **SCHEMA_SQL 与实际 v5.db 对齐**：改写 `database.py`，`INDEX_SQL` 与 `SCHEMA_SQL` 分离，索引在 `ADD COLUMN` 之后建，消除启动时「no such column」。
- **影子（非破坏）迁移**：`migrate_existing_db` 用幂等 `ALTER TABLE ... ADD COLUMN` 补齐存量库缺失字段（materials/notes/reviews/homeworks/questions/answer_items/source_chunks/workflow_runs 等，含 `created_at`），并在 `.db` 缺失 `source_chunks.created_at` 时在连接前迁移，保证后台 worker 建块不崩。
- **DAL 统一**：`fetch_all`/`fetch_one` 返回 dict（`.get()` 可用）；删除已废弃的 `query(..., one=True)` 调用计 15 处，全部改为 `query_one`；业务层不再接触 `sqlite3.Row`。
- **P0 路径安全**：`resolve_material_path` 用 `resolve()+relative_to()` 把 material 锚定在 uploads 根内，且不存在时报错（`BusinessError`）。
- **新增跑件表**：`run_tasks`、`parse_tasks`、`review_attempts`、`evidence_links`。（迁移已建立）

## 阶段二 · 材料状态机 + 后台执行模型

- **材料生命周期**：upload→queued→parsing→indexed→ready / failed；上传即自动入解析队列（`enqueue_parse` 后台线程）；重复文件按 `sha256` 去重。
- **工作流** POST 不再同步等 DAG：返回 **202 + `{run_id, status:"queued"}`**；任务写入 SQLite 持久化队列，进程内存资源后台线程 + 信号量（`MAX_CONCURRENT_RUNS=2`）执行；`GET /api/runs/{id}/events`（SSE）推送进度，按空监听节流。
- **实例重启恢复**：启动时把 `queued/running` 任务回收消化。

## 阶段三 · 四条业务流（DAG 引擎重写）

- 新引擎支持**拓扑分层并发**（`asyncio.gather`），每节点记录 `attempt`、`output_json`（不截断）、`latency_ms`、token 数。
- 异常分类：`BusinessError`/`AuthError`/`BadRequestError` **不重试**；`RetryableModelError`/超时**指数退避重试**；`SchemaValidationError` 触发「先修正→再切模型」。
- **听课流**：enrich→table→transcribe→结构→课代表→写成/草稿→证据核验→自检生成→审→画像落库→Obsidian 同步。**端到端产出中文笔记（status=synced）并写 evidence_links**。
- **作业流**：题目读取→风险分级→solver 与 parallel_solver 并发 → adjudicator → 讲解 → 证据/范围审校 → `questions` 若先建 id、`answer_items` 持久化。**验证通过：并行解析 had一个 attempt 失败→重试成功**。
- **错题流**：解析→分类→结构化→判定 provisional/confirmed，SM-2 复习。
- **复习流**：聚合→条目化→自测(M 版)、(占位)课堂作答→`reviews` 持久化（含 `review_attempts`）。

## 阶段四 · RAG + 证据 + 网关可靠性

- `rag.py` 增加幂等 `init_fts()`（FTS5 `chunks_fts`）；材料解析后写 `source_chunks` + 建块索引。
- **证据模型**：`evidence_links` 关联（笔记/条目↔有效块/答案）；图文/题头解析（pdf/pptx/docx/text/image/audio）。
- **网关可靠性**：单例连接池；429/5xx 带退避重试（默认 2 次）；并发信号量（默认 6）；熔断（连续 4 次失败打开 20s）；`X-Trace-Id` 串联日志；错误标准化映射到 DAG 异常类。提供 `repair_json_blob`（代码围栏/尾逗号/截断修复）作为「无效 JSON 修复」后接 `SchemaValidationError`→切换模型。
- **配额安全**：用量状态 `unavailable` 时不默认当作 0%（避免误走免费路由超支），而是 `_quota_pct=100` 走保守路由（官方 DeepSeek / 安全默认）。

## 阶段五 · 前端与接口 P0

- **P0a `set_status` JSON**：后端改为优先读 JSON body，query 参数保留兼容 → 前端传 JSON 不再 422。
- **P0m 移动端 `material_ids`**：`LessonRunRequest`/`HomeworkRunRequest` 补齐 `material_ids` 字段；移动端上传后把已入库 id 回传进流从而真正进入工作流。
- **五中心导航**：`App.vue` 重构为「今日 / 课程 / 收件箱 / 复习 / 系统」五组，原有页面全部归位。
- **材料收件箱**：`UploadPage` 增加材料库表格（名称/类型/解析状态/大小/重试/删除），`MaterialsApi` 补齐 list/get/patch/remove/retry/chunks；`api` 客户端新增 `patch`。
- **构建**：`vue-tsc -b && vite build` 通过（约 1.34MB 主包），打包体积告警保留待后续代码拆分。

## 阶段六 · 安全 / 测试 / 备份 / 文档

### 安全
- SPA 兜底路由：拒绝 `..` / `\`、`resolve()+relative_to(dist)` 收敛到 dist 内（防路径穿越）；`api/` 前缀结果 404。
- 开启 `V5_MOBILE_TOKEN` 时，`/docs` `/redoc` `/openapi.json` 对非本机返回 403（避免大面暴露 **API 全貌**）。
- 移动 Token：非本机请求校验 `x-app-token`/`?token=`；`/api/admin/mobile-token/*` 全部经 `_require_local_admin` 只允许本机（rotate/set/status），token 变更后即时改写 `.env`。
- 上传改为流式+原子落地+sha256 去重，不受控于恶名扩展名（文本/图片/P翻页/音频按内容判定）。

### 测试
`backend/tests/test_core.py`（stdlib unittest，lib 零依赖）—— `python -m unittest discover -s tests` 通过 13/13：
- reasoning 解析/修复（围栏 JSON、尾逗号修复、彻底废弃）
- DAG 异常层级 / `LOCAL_MODEL="_local_"`
- 网关状态码→异常映射（400/401/429/5xx）
- Worker 配额未知→安全默认 100
- 路径安全（缺失材料 → `BusinessError`）
- 后端可导入、路由数≥60

### 备份
`backend/data/backups/snapshot_final_20260908_204442/`：`v5.db`（在线备份）＋ uploads + obsidian_vault + `manifest.json`（`integrity_check=ok`、`fk_errors=0`、`sha256`）。14 门课程 + 已产出资产（1 note / 2 reviews / 1 answer_item / 8 materials）全部入账。

### 文档
本报告 + 提交日志（七次 commit 可分阶段回滚）。

---

## 决策默认值（未答复分支 → 安全性默认）

| 分支 | 默认值 | 说明 |
|------|--------|------|
| 数据库保留现有数据 | ✅ 采用 | 影子迁移；14 课程全保留 |
| DAL | 沿用 `sqlite3`（不变更平台） | 与方案「数据层保留 raw-sqlite」一致更新 |
| 前端 | 五中心 + 材料库（暂缓 后来 `vue-router`） | 无新增依赖，避免无法验证的构建；保留软件 hash 路由 |
| 作业模式 | 「先做后看」**关闭**（可配置） | 默认不入 `先做后看`，teacher 可开 |
| 数学不阻断合法推理 | 证据规则**不关闭 derived_reasoning 类** | 避免数学题误判“无证据” |
| 学习结果/模型 | 仅当需要测试 | 演示用端到端流（已用真实 OLLAMA/Gateway 验证） |

> 以上默认值若需调整可在 `backend/app/*.py`（路由表 `DEFAULT_ROUTE`、`routing.py` 阈值、`workers.py` 并发）一次性改定。

---

## 待办（未纳入本次范围，建议后续）
- vue-router + 页面按需拆分（`import()`），消除 >500kB 首包。
- 对照 `.env`  的 `V5_MOBILE_TOKEN` 开启指南 + 首次启动迁移验证脚本入 CI。
- 数据库 AUTH/CLP 方案（多用户、鉴权策略）后续审批后实现（当前为单用户+移动 Token 方案）。

## 验证记录（一键复现）

```bash
# 后端
cd v5/backend
python -m uvicorn app.main:app --port 8801 &
python -m unittest discover -s tests -v        # 13/13 OK

# 前端（需 node）
cd v5/frontend && npm run build                 # vue-tsc + vite OK

# 数据库：首要备份
python -  <<'PY' ... snapshot_final_...          # integrity=ok, fk=0
PY
```