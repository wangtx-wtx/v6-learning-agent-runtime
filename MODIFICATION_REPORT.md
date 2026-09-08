# v5 学习系统 — V5.5 稳定闭环 最终修改报告

> 执行依据：《V5.5 稳定闭环版本》方案（20 节）。执行方式：A→H 逐阶段实施，每阶段通过
> 编译检查 + 全量单测 + 真实端到端冒烟 + git 提交后进入下一阶段。
> 本报告严格区分「已实现 / 已测试 / Mock 验证 / 真实模型验证 / 待办 / 已知限制」，
> **不把"函数存在"描述为"功能已完成"**。

- 产品版本：**5.5.0**（`/api/health` 实测返回）
- DB Schema：**v7**（migrations 0001–0007，生产库已应用，`schema_migrations=[1..7]`）
- 测试：**81/81 通过**（`python -m unittest discover -s tests -t .`，约 21s）
- 提交历史（本阶段 A–H）：

```
89d1536 feat(web): vue-router + SSE composable + typed DTOs + material picker + echarts on-demand (G)
233562b feat(rag): externalized prompts + schema binding + hybrid retrieval + model_calls audit (F)
384f73f feat(flows): critic loop, evidence gating, error events, homework OCR states, obsidian paths (E)
08240b5 feat(review): answer attempts with simplified SM-2 mastery loop (D)
52057fe feat(storage): blob dedup/refcount/physical GC + storage audit + type matrix (C)
1772797 feat(workers): lifespan, recovery, real concurrency, cancel, retry (B)
0426143 feat(database): connection policy, schema v7 blob model, shadow migration hardening (A)
```

---

## 一、总览：各阶段状态

| 阶段 | 内容 | 状态 | 提交 | 验证 |
| --- | --- | --- | --- | --- |
| A | 连接策略重做 / Schema v7 / 影子迁移加固 | ✅ 已完成 | `0426143` | 61 tests + boot smoke |
| B | lifespan / Worker 恢复 / 真并发 / 取消 / 重试 | ✅ 已完成 | `1772797` | 13 worker tests + 取消/重试真实运行 |
| C | 材料 blob 拆分 / 去重引用 / 物理 GC / 类型矩阵 | ✅ 已完成 | `52057fe` | 10 tests + 去重/复活真实冒烟 |
| D | 复习作答 → 判分 → SM-2 闭环 | ✅ 已完成 | `08240b5` | 13 tests + 真实出题作答冒烟 |
| E | 听课 critic 闭环 / 错题事件 / 作业状态机 / Obsidian 真实路径 | ✅ 已完成 | `384f73f` | 10 tests + 三流真实冒烟 |
| F | Prompt 外置 / Schema 绑定 / 混合检索 / 调用审计 | ✅ 已完成 | `233562b` | 10 tests + 审计/检索真实冒烟 |
| G | 前端 vue-router / SSE / 业务 DTO / 材料选择器 | ✅ 已完成 | `89d1536` | vue-tsc 0 错 + 分包构建 + preview 200 |
| H | 快照恢复 / 契约测试 / CI / 文档版本统一 | ✅ 已完成 | （本次提交） | 5 tests + 真实备份 dry-run 恢复 |

---

## 二、已实现（代码已落地，且经测试或真实运行验证）

### A. 数据库 / 连接策略
- **连接策略（方案 2.4）**：读=线程本地连接；写（`execute/insert/executemany`）=专用短连接；`transaction()` 独立连接 + `BEGIN IMMEDIATE` + 3 次忙等重试。依据：FastAPI 协程共享事件循环线程，共享连接导致交错 BEGIN 冲突（此前 `database is locked` 实测复现，现 61→81 用例无锁冲突）。
- **WAL 设置**仅在模式非 wal 时执行（避免写竞争下 `journal_mode` 需要排他锁报错）。
- **Schema v7**：`0007_blob_dedup_model.sql` 撤销 0006 的 `materials.file_hash` 唯一索引（与 blob 去重模型冲突）；生产库已迁移，`schema_migrations` checksum 锁定。

### B. Worker / 生命周期
- FastAPI `lifespan`（无 on_event）：init → 备份（测试覆写时跳过）→ `recover_all()` → 临时文件清理（24h，5 分钟活动上传保护）→ WorkerManager 启动 → 健康自检；停机反序。
- **恢复**：重启后 running/interrupted → queued（error='recovered_after_restart'）；超 max_attempts → failed；孤儿 queued run 重新入队。
- **真并发**：`asyncio.to_thread` 认领，2 个 run + 2 个 parse 槽位真实并行（单测以多线程认领互斥验证）。
- **取消**：`POST /api/runs/{id}/cancel`（queued 直接取消 / running 置 cancel_requested 由 Worker 检查 / 终态 409）；`RunCancelledError(BaseException)` 贯穿 DAG 层/节点/模型重试边界；节点取消也落 `run_nodes` 记录。
- **重试**：`POST /api/runs/{id}/retry` 创建子 run，`_retry` 标记复用父 run 成功节点输出（节点状态记 'reused'）；attempts/max_attempts=3 + 600s 租约 + 心跳续约。

### C. 材料 blob 化 / GC
- `file_blobs`（sha256 UNIQUE、storage_path、ref_count、gc_state）；`acquire_blob` 统一去重/复活/重写路径（修复"死 blob 重传文件缺失"缺陷）；上传响应含 `deduped`。
- 删除材料=解引用（先删 evidence_links → FTS → source_chunks → parse_tasks → materials，再 `release_blob_ref`）；`gc_blob_now` 失败落 `blob_gc_tasks` 补偿队列；`/api/admin/storage/audit|gc`。
- **类型支持矩阵**：pdf/ppt/doc/text/txt/md 可解析；image→`needs_ocr`；audio→`transcribing` 短路（无假占位 chunk）；其余类型上传即拒（含 text/txt/md 别名修复）。

### D. 复习闭环
- `POST /api/reviews/{id}/attempts`：判分 → SM-2 简化变体（0/1→1d，2→3d，3→×1.8，4→×2.5，5→×3.2，clamp[1,180]；连续 2 错重置；掌握度 +0.15/−0.25 clamp[0,1]），before 值从 errors→上次 after 快照→默认值链取。
- `POST /api/reviews/{id}/complete`（score=正确率，重复提交 409）；复习详情默认隐藏答案（answered 前 `reveal=1` 才返回）。

### E. 业务流质量
- **听课 critic 闭环**：真实 critic 模型审查（prompt 外置 + pydantic schema），不通过时带问题修订重写（最多 1 轮：重写→证据重校验→复审），token 合计入账。
- **证据规则**：证据为空**不得**自动确认（`ok = bool(ev_list) and all(verified)`），证据空 → note 强制 draft。
- **错题**：confirm/reject 用 rowcount 判定（0 → 409），终态拒绝再次操作，全部写 `error_events`（created/confirmed/rejected + old/new status + payload）；`ai_error_json` 存结构化错因 `{phenomenon, direct_cause, root_cause, knowledge_gaps, possible_causes, confirmed_causes, uncertain}`；cross_check 捕获 `uncertain`。
- **作业状态机**：仅图片 → `homework_ocr` 小 DAG（resolve→persist→`awaiting_confirmation`）；`POST /api/homeworks/{id}/confirm` 校正文本→confirmed→自动排队求解（非 awaiting 状态 409）；裁决冲突 → `requires_human_review` 落库 → 作业 `needs_review`。
- **先做后看**：`POST /api/homeworks/{id}/answer` 提交学生答案（重复提交 409）才置 `reveal_allowed=1`；作业详情与 run result 只返回已揭示题的解答。
- **Obsidian**：路径来自数据库级联（lesson→chapter→course），`01 Courses/<课程>/<第N章 章节名>/[note-<id>] 标题.md`；Windows 文件名清洗（非法字符/保留名/尾点空格）；稳定 ID 防重命名断链。

### F. Prompt / 检索 / 审计
- **Prompt 外置**：`backend/prompts/<workflow>/<name>.v1.md`（13 个文件）；loader 支持 `{{变量}}` 渲染、sha256 checksum、缓存、测试夹具覆盖（`V5_PROMPT_FIXTURE_DIR`）。
- **Schema 绑定**：`integrations/schemas.py` 13 个 pydantic 模型；所有模型节点输出必须过校验，失败抛 `SchemaValidationError`（走"先修复再换模型"），移除全部 `extract_json(...) or {}` 静默兜底。
- **混合检索**：`0.45×BM25 + 0.40×向量余弦 + 0.10×层级 + 0.05×短语`；embedding 不可用自动降级 `keyword_only`（0.60/0.30/0.10）；每次检索写 `retrieval_runs`（query/过滤/模式/候选数/选中 chunk）。
- **调用审计**：`gateway.chat` 每次调用写 `model_calls`（run/node/attempt/model/trace/prompt_name+version/tokens/latency/status/error_code/输入 sha256 摘要），成功与失败均落库。

### G. 前端
- **vue-router 4**：hash 历史（兼容旧 `#/` 链接），15 个路由全部动态 `import()` 分包；App.vue 重写为 `<router-view>` + Router 驱动导航；404 兜底路由。
- **SSE**：`useRunEvents` 组合式函数——`EventSource(/api/runs/{id}/events)` 实时推送；断线指数退避重连（1s→16s，5 次）；耗尽降级 2s 轮询；RunsPage 已接入（实时/轮询状态徽标，终态自动停止并刷新列表）。
- **业务 DTO**：`RunsApi.result()` + `LessonResultDto/HomeworkResultDto/ReviewResultDto/ErrorResultDto` 类型化。
- **材料选择器**：听课流页可勾选已解析材料（`material_ids` 注入检索）。
- **ECharts 按需**：`echarts/core` + GraphChart/Tooltip/Legend/CanvasRenderer，GraphPage 独立分包（主包 119KB，图页单独 487KB 懒加载）。

### H. 运维 / 工程化
- **快照恢复**：`python -m tools.restore_snapshot --latest|--file [--dry-run]`；integrity_check + schema_migrations 校验、恢复前 pre_restore 备份、原子替换 + WAL 清理；对真实生产备份 dry-run 通过（33 表、migrations [1..7]、integrity ok）。
- **CI**：`.github/workflows/ci.yml`——后端（compileall + unittest，Python 3.12/windows-latest）+ 前端（vue-tsc + build，node 22）。
- **文档**：`docs/README.md`（索引+结构）、`PRODUCT.md`、`ARCHITECTURE.md`、`OPERATIONS.md`、`TESTING.md`、`VERSIONS.md`；版本口径统一为 产品 5.5.0 / Schema 7 / API v1 / prompts v1。

---

## 三、已测试（自动化用例覆盖，81/81 通过）

- **数据库**：版本 7、migrations [1..7]、多线程读写互斥/事务连接策略、自增语义。
- **Worker**：认领原子性、租约与心跳、恢复语义（interrupted→queued、超限→failed）、并发槽位、取消置位与检查点、TestClient 下 Event 重建。
- **Blob/GC**：去重引用、复活、重写、物理删除、补偿队列、审计口径、类型矩阵与别名。
- **复习**：normalize/判分/SM-2 边界（clamp、连续错重置）、before 链、complete 幂等（409）、答案隐藏。
- **Phase E**：Windows 文件名清洗、稳定 ID 命名、数据库级联路径、证据空→draft、错题 confirm→事件→终态 409、结构化错因落库、图片路由到 homework_ocr、confirm 触发求解 run、先做后看揭示与 409。
- **Phase F**：prompt 加载/缓存/夹具覆盖/缺失报错、13 类 schema 合法/非法 JSON/形状错误、混合检索排序与 keyword_only 降级、retrieval_runs 落库、model_calls 成功与失败行（run/node/attempt/prompt 名版本）。
- **Phase H**：快照恢复全闭环（写→破坏→校验→恢复→数据回来）、垃圾文件/缺迁移表快照拒绝、Fake Gateway 契约（chat 响应键、schema 绑定离线跑通 note_writer）。

## 四、已用 Mock/夹具验证（离线、不依赖外部组件）

- Fake Gateway（`tests/test_phase_h.py`）：验证 DAG 节点对网关契约（content/tokens/model 键）的依赖正确；note_writer 在 Fake 网关下走完"外置 prompt→schema→revised 标记"。
- embed_text 强制失败注入：验证混合检索在 embedding 不可用时正确降级 `keyword_only` 且权重切换（不真实调用网关的封闭验证）。
- prompt 夹具目录覆盖（`V5_PROMPT_FIXTURE_DIR`）：验证测试可注入固定提示词（checksum 随夹具变化）。

## 五、已用真实模型验证（本地网关 127.0.0.1:8080 实际调用）

- **听课流端到端**（run 57/61）：材料解析→混合检索（hybrid，11 候选→8 选中，retrieval_runs 落库）→大纲（deepseek_v4_free，1505 in/96 out）→笔记（2841/300）→**真实 critic**（qwen3_8_27b，score 0.95 passed，0 轮修订）→证据校验→落库→Obsidian 真实路径 `01 Courses\计算机语言及程序设计\第1章 .../[note-11] ….md`（物理文件存在）。
- **错题流端到端**（run 58/62）：真实分层错因（root_cause 非空、结构化 JSON）→ provisional → confirm 200 + error_events=['confirmed'] → reject 409。
- **作业流端到端**（run 59）：文本双题求解→solved；先做后看：未提交无解答→提交第一题→仅该题揭示→重复提交 409。
- **复习流端到端**（阶段 D）：真实出题（自适应题数）→作答→判分→SM-2 间隔/掌握度更新→complete。
- **审计实证**：`model_calls` 逐行含真实 prompt 名/版本、模型、token、时延；`retrieval_runs` 模式 hybrid、候选数与选中数一致。
- **恢复实证**：对生产最新备份 `v5_20260909_000849.db` 执行 dry-run 恢复：integrity ok、migrations [1..7]、33 表。

## 六、待办（方案内未完成或后续版本）

1. **图片作业 OCR 全链路真实验证**：`qwen3_vl` 已在模型注册表，但未跑通"图片上传→homework_ocr→用户确认→求解"的带图端到端（需要一张真实错题图片做冒烟）；当前仅单测验证了路由与状态机。
2. **qwen3_vl 视觉节点**：`error_vision_reader`/OCR 已接外置 prompt 与 schema，但视觉模型真实可用性未确认（若网关未部署该模型，相关运行会以 RetryableModelError 暴露）。
3. **向量分量的完整闭环**：source_chunks 尚无存量 embedding 列数据，混合检索的 0.40 向量分量对存量块近 0（新块如启用 embedding 入库才生效）；需要一次全量回填任务。
4. **前端构建产物托管**：当前前端 dev(5173)/preview 独立运行，后端未挂载 dist 静态托管与 SPA fallback（部署形态待定）。
5. **CI 首次云端运行**：workflow 已就绪，未在本机之外的 GitHub runner 上实际跑过（本机验证等价命令通过）。
6. **移动端上传入口与 mobile-token 的回归**（G 阶段未改动其逻辑，但未在本轮重新端到端实测）。

## 七、已知限制（如实说明）

1. **混合检索的 BM25 为候选集内计算**（轻量实现）：IDF 在过滤后的候选集内估计，不是全局语料统计；对"过滤后小候选集"场景排序质量足够，但与全文检索引擎的 BM25 数值不具可比性。
2. **critic 修订环最多 1 轮**（方案 10.4 上限即 1）：复审仍不通过时保留 draft 状态交人工，不做无限修订。
3. **课程名/章节名为空或 lesson 脱链时**，Obsidian 路径回退 `未分类课程/未分类章节`（有课时级联补齐，无任何 id 时才回退）。
4. **error_events 不含 deleted 事件**：错题删除直接物理删除（方案未定义删除事件流）。
5. **`retrieval_runs`/`model_calls` 无清理策略**：随使用量线性增长（含输入 sha256 摘要而非原文，敏感面可控）；建议后续加保留窗口。
6. **恢复工具要求停机执行**：原子替换前只释放本进程连接，不处理其他进程占用（Windows 文件锁）。
7. **SSE 为单实例进程内实现**（DB 轮询生成流）：多副本部署时每副本独立轮询，未做广播优化。
8. **测试对 Windows 控制台编码有约定**：脚本须 UTF-8 reconfigure；过滤脚本的 `[exit code: 1]` 为管道噪声，以打印的 OK/FAILED 为准。

---

## 八、验证环境

- Windows 11 / Python 3.12.10 / Node v24.19.0（npm 11.17.0）
- 后端 `127.0.0.1:8801`（uvicorn app.main:app），模型网关 `127.0.0.1:8080`（真实可用，usage/chat 200）
- 生产库 `backend/data/v5.db`：schema v7、材料 1–8 blob 引用 live、`without_blob=0`
- 全量命令：`python -m unittest discover -s tests -t .` → `Ran 81 tests ... OK`；`npx vue-tsc -b` → 0 错；`npm run build` → 成功
