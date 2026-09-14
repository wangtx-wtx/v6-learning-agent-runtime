# V6.0.0 项目详细现状与文件指南

更新时间：2026-09-14  
项目目录：`C:\Users\28595\Desktop\新建文件夹 (4)\v5`  
正式入口：`http://127.0.0.1:8800/`  
本地网关管理页：`http://127.0.0.1:8317/admin`

## 1. 当前结论

项目已经从 V5 工程底座升级为 **V6.0.0 正式版**。当前不是单纯的资料库或摘要器，而是以课程、章节和课堂为长期组织单元的本地学习 Agent Runtime。

正式课堂链路为：

```text
任务材料域
  → 材料规范化与去重
  → 稳定 Source ID / Source Map
  → 覆盖计划与保序安全分段
  → Lesson Understanding
  → Student Simulator
  → Composer V2 动态学习内容
  → Evidence V2 确定性绑定
  → Coverage / Quality Gate
  → 笔记持久化
  → HTML / PDF
  → Obsidian
  → 作业 / 错题 / 掌握度 / 复习反馈
```

当前健康状态：后端 `6.0.0`、数据库 schema `23`、FTS ready、jieba tokenizer ready、本地网关 live、无活动中的幽灵任务。正式根路径已经能够直接返回生产 SPA。

## 2. 已完成能力

### 2.1 课程与时间组织

- 课程、章节、课时的创建、编辑和防误删。
- 章节进度支持手工章节名称、状态和备注。
- 学期基准、固定课表和七日有效课表。
- 临时调整只保留节假日、调休、补班；补班按“来源日期”复制当天真实课程规则，兼容起止周和单双周。
- 总览展示七日课程，考试位于页面底部。

### 2.2 材料处理

- 支持文本、Markdown、PDF、PPT/PPTX、Word、图片及音频等材料入口。
- 上传文件采用安全文件名、扩展名白名单、magic bytes 校验和容量限制。
- blob 内容寻址、重复材料复用、引用计数和延迟垃圾回收。
- 长转写按时间、段落和长度规范化；无标点超长内容也会强制切片。
- 单次课堂使用全部唯一有效材料，不用 Top-K 从课堂材料中淘汰内容。

### 2.3 V6 Learning Engine

- Material Domain：冻结当前任务应处理的完整材料集合。
- Coverage Ledger：每个材料和 Source Span 必须有明确处理状态，不允许 silent drop。
- Lesson Segmenter：默认约 12,000 输入 Token 一段，最终消息再次核算预算。
- Lesson Understanding：分段理解后确定性合并知识单元、定义、公式、推导、例题和关系。
- Student Simulator：识别困惑点、前置缺口、易错点、强调、记忆锚点和缺失推理步骤。
- Composer V2：根据实际分析选择内容块，不为了模板强行生成易错点或注意事项。
- Evidence V2：模型只提供 Source Ref，程序从数据库绑定原文、位置和哈希。
- Coverage Auditor 与 Quality State：覆盖、证据、审查、出版和同步状态相互独立。

### 2.4 学习闭环

- 作业 DAG：OCR、解题、并行求解、裁决、教学解释与验证。
- 错题 DAG：分析、交叉检查、证据关联和 provisional/confirmed/rejected 状态。
- 已确认错题可关联 Knowledge Unit 和课堂来源，并影响 mastery。
- 复习 DAG：按课程、章节、考试范围、错题和掌握度组织复习材料。

### 2.5 输出与出版

- 正式学习产物支持结构化 HTML 和 PDF。
- 程序负责确定性排版，模型不直接拼 HTML。
- 文档记录模板版本、内容哈希、生成状态和文件位置。
- HTML/PDF 内容来自同一结构化内容对象。
- 支持 Obsidian Markdown 同步，但同步状态不会覆盖证据状态。

### 2.6 模型与网关

- V5 只访问本地网关 HTTP API，不读取网关数据库和密钥。
- 支持 fake、replay、live 三种网关模式。
- 模型注册、能力声明、协议映射、候选链、回退和调用审计可在前端管理。
- 已适配聊天、OCR、Embedding 和 Rerank 类模型配置。
- 长课堂分段请求等待上限为 600 秒；读取超时后交给 DAG 切换候选模型，不对同一请求机械重放三次。

## 3. 运行结构

```text
浏览器
  └─ FastAPI 同源站点 :8800
       ├─ /              前端生产 SPA
       ├─ /assets/*      Vite 构建资源
       └─ /api/*         业务 API
            ├─ SQLite v5.db
            ├─ Worker Manager / DAG Runner
            ├─ V6 Learning Engine
            ├─ HTML/PDF Renderer
            ├─ Obsidian Sync
            └─ Local LLM Gateway :8317
                  └─ 各上游国产模型服务
```

正式启动脚本会设置 `V5_ENV=production`、`V6_LEARNING_ENGINE=on`，先执行前端生产构建，构建失败则阻止后端启动。`fake` 参数仍保留给零 Token 演示，不使用正式数据根。

## 4. 根目录文件

| 文件/目录 | 用途 |
|---|---|
| `启动 v5.bat` | 当前正式启动入口；名称为兼容旧快捷方式保留，实际启动 V6.0.0。检查 Python/Node/npm、启动网关、构建前端、启动后端并打开正式页面。 |
| `启动远程上传.bat` | 启动面向 Tailscale Serve 的远程上传访问。 |
| `启动公网上传.bat` | 启动带 Token 保护的 Tailscale Funnel 公网上传入口。 |
| `README.md` | 项目首页、安装、运行、API 与总体能力说明。 |
| `v5_config.yaml` | 历史兼容名称；模型路由和角色配置的可读参考。正式动态配置以数据库/API 为准。 |
| `docs/` | 架构、运维、测试、版本与 V6 设计文档。 |
| `backend/` | FastAPI、SQLite、DAG、Learning Engine、出版和测试。 |
| `frontend/` | Vue 3 控制台和移动上传界面。 |
| `output/` | 样板、人工导出或开发期输出；正式文档记录由 backend artifacts 管理。 |
| `test_fixtures/` | 合成课程、超长文本、PDF/PPT、OCR 图片和异常文件测试集，不是正式课程数据。 |
| `tmp/`、`backend/tmp/` | 隔离验证和临时调试产物，不属于正式数据。 |
| `FULL_CODE_EXPORT.txt` | 历史代码导出快照，不参与运行。 |
| `MODIFICATION_REPORT.md`、`V5.*报告.md` | 历史阶段报告，不是当前运行契约。 |

## 5. 后端核心文件

### 5.1 应用与基础设施

| 文件 | 用途 |
|---|---|
| `backend/run.py` | Uvicorn 启动入口，监听 8800，打印本地、局域网和 Tailscale 地址。 |
| `backend/app/main.py` | FastAPI 主应用；注册课程、章节、材料、工作流、模型、校历、文档、移动端和管理 API，并托管生产前端。 |
| `backend/app/lifecycle.py` | 启停生命周期：实例锁、迁移、备份、分词器、FTS、课表种子、任务恢复和 WorkerManager。 |
| `backend/app/config.py` | 环境与路径单一真相源；严格隔离 production/development/test 数据根。 |
| `backend/app/database.py` | SQLite 连接、事务、查询、初始化和 schema 辅助函数。 |
| `backend/app/db_migrate.py` | SQL migration 应用、校验和与 user_version 管理。 |
| `backend/app/migration.py` | 旧业务数据兼容迁移与重建工具。 |
| `backend/app/backup.py` | SQLite 原生 backup、备份轮换和恢复前检查。 |
| `backend/app/instance_lock.py` | Windows 单实例锁，阻止两个后端同时写正式库。 |
| `backend/app/time_utils.py` | 数据库与运行租约统一时间工具。 |
| `backend/app/env_file.py` | `.env` 的安全读取和原子更新。 |
| `backend/app/tailscale.py` | Tailscale Serve/Funnel 状态和命令封装。 |

### 5.2 工作流和业务 DAG

| 文件 | 用途 |
|---|---|
| `backend/app/dag.py` | 通用 DAG、节点依赖、状态机、候选模型回退、重试和运行记录。 |
| `backend/app/dag_lesson.py` | 课堂主链；组合 V6 材料、理解、认知、证据、质量、笔记、出版和同步节点。 |
| `backend/app/dag_homework.py` | 作业 OCR、求解、并行审查、裁决、教学与证据链。 |
| `backend/app/dag_error.py` | 错题识别、分析、交叉检查与知识来源关联。 |
| `backend/app/dag_review.py` | 章节/考前复习生成、自测审查和出版入口。 |
| `backend/app/review_service.py` | 复习调度、掌握度和复习数据服务。 |

### 5.3 材料与存储

| 文件 | 用途 |
|---|---|
| `backend/app/material_parser.py` | 文本、PDF、Office、图片、音频等材料解析和 source chunk 生成。 |
| `backend/app/services/file_types.py` | 文件类型白名单、扩展名和内容类型判断。 |
| `backend/app/services/blob.py` | 内容寻址 blob、哈希去重和引用关系。 |
| `backend/app/services/blob_gc.py` | 引用归零后的可恢复删除任务和物理垃圾回收。 |
| `backend/app/tokenizer.py` | jieba 中文分词；缺失时明确降级为字符级。 |
| `backend/app/rag.py` | FTS/关键词/长期材料检索；服务跨材料复习和问答，不负责淘汰单堂课内容。 |

### 5.4 模型访问

| 文件 | 用途 |
|---|---|
| `backend/app/gateway.py` | 业务侧统一 GatewayClient、调用审计上下文和兼容入口。 |
| `backend/app/gateway_engines.py` | FakeEngine、ReplayEngine、LiveEngine；HTTP、超时、错误分类和结构化请求。 |
| `backend/app/gateway_mode.py` | fake/replay/live 模式解析与测试环境防误连。 |
| `backend/app/models_registry.py` | 内置模型档案、能力、上下文窗口与默认角色候选。 |
| `backend/app/routing.py` | 根据角色、能力、启用状态和配额选择候选模型。 |
| `backend/app/reasoning.py` | 清理 reasoning/thinking，提取和修复结构化 JSON 输出。 |
| `backend/app/integrations/prompts.py` | 读取版本化 prompt 并计算校验信息。 |
| `backend/app/integrations/schemas.py` | 模型输入输出 Pydantic 严格契约。 |
| `backend/app/resources/gateway_fake/*.json` | 零 Token 测试的确定性模型响应夹具。 |

### 5.5 V6 Learning Engine 文件

| 文件 | 用途 |
|---|---|
| `learning_engine/config.py` | V6 off/shadow/on 开关、正式出版模式和引擎版本。 |
| `learning_engine/contracts.py` | Material、Segment、Understanding、Cognition、Coverage 等枚举和契约。 |
| `learning_engine/domain.py` | 冻结 Material Domain、域哈希、任务内来源边界和可复用范围。 |
| `learning_engine/normalize.py` | 转写/PPT/文本规范化、时间解析、噪声处理和超长片段切分。 |
| `learning_engine/deduplicate.py` | 材料级与内容级重复识别，保留唯一有效材料。 |
| `learning_engine/source_map.py` | 生成稳定 Source ID、Source Span 和位置映射。 |
| `learning_engine/segment.py` | 全量材料保序分段、上下文预算、overlap、最终消息预算复核和账本。 |
| `learning_engine/understanding.py` | 分段模型调用、并发限制、JSON 校验、来源作用域、候选回退和全局合并。 |
| `learning_engine/cognition.py` | Student Simulator 批处理、认知项、来源可见性和批次审计。 |
| `learning_engine/composer.py` | 用 LessonUnderstanding + CognitiveMap 生成动态结构化笔记。 |
| `learning_engine/evidence_v2.py` | 逐 claim Evidence V2、生产者可见性、确定性原文绑定和 legacy 投影。 |
| `learning_engine/coverage.py` | Coverage Plan/Ledger、覆盖率计算、报告和 fail-closed 门禁。 |
| `learning_engine/quality.py` | processing/evidence/coverage/review/publication/sync 多维质量状态。 |
| `learning_engine/learning_loop.py` | Knowledge Unit、错题、作业反馈、mastery 和复习优先级联动。 |
| `learning_engine/__init__.py` | 对外导出 Learning Engine 稳定接口。 |

### 5.6 出版、同步与课表

| 文件 | 用途 |
|---|---|
| `backend/app/document_artifacts.py` | 结构化英伦报纸风格 HTML 渲染、Chromium PDF、产物哈希与下载记录。 |
| `backend/app/obsidian.py` | 将课程笔记和复习内容同步为带 front matter 的 Markdown。 |
| `backend/app/schedule_calendar.py` | 学期、固定课程规则、节假日、调休、按来源日期补班和七日有效课表。 |
| `backend/app/seed_data.py` | 初始课程/校历兼容数据导入。 |
| `backend/app/evidence.py` | V5 legacy evidence 兼容层；正式可信门禁由 Evidence V2 负责。 |
| `backend/app/schemas.py` | FastAPI 业务请求/响应模型。 |

### 5.7 异步 Worker

| 文件 | 用途 |
|---|---|
| `workers/manager.py` | 工作流与解析任务并发槽、进程内任务管理、取消和优雅停止。 |
| `workers/workflow_worker.py` | 领取 run、租约心跳、执行 DAG、终态和质量门禁收口。 |
| `workers/material_worker.py` | 后台材料解析队列。 |
| `workers/recovery.py` | 重启后恢复 interrupted/过期租约，避免重复领取。 |
| `workers/__init__.py` | Worker 对外入口和 enqueue 辅助函数。 |

## 6. Prompt 文件

| 目录/文件 | 用途 |
|---|---|
| `prompts/lesson/segment_understanding.v2.md` | 当前课堂分段理解正式 prompt；要求单一 JSON、紧凑输出和合法 Source Ref。 |
| `prompts/lesson/merge_understanding.v1.md` | 课堂分段理解的合并规则。 |
| `prompts/lesson/student_simulator.v1.md` | 学生认知困难、易混、前置缺口和记忆锚点分析。 |
| `prompts/lesson/critic.v1.md` | 笔记结构、证据和学习价值审查。 |
| `prompts/lesson/lesson_outline.v1.md`、`note_writer.v1.md` | V5/兼容链和部分对照测试保留，不是 V6 Composer 的唯一来源。 |
| `prompts/homework/*.md` | OCR、solver、parallel_solver、adjudicator、teaching。 |
| `prompts/error/*.md` | 错题 vision、analyst 和 cross_check。 |
| `prompts/review/*.md` | 复习讲义 writer 和 self_test 审查。 |

## 7. 数据库迁移

| 迁移 | 用途 |
|---|---|
| `0001_baseline.sql` | 课程、章节、材料、笔记、作业、错题、复习等基础表。 |
| `0002_material_state.sql` | 材料解析与处理状态。 |
| `0003_workflow_queue.sql` | 工作流、节点和后台队列。 |
| `0004_review_attempts.sql` | 复习尝试记录。 |
| `0005_evidence_links.sql` | legacy evidence 关系。 |
| `0006_v55_constraints.sql` | 状态约束和一致性增强。 |
| `0007_blob_dedup_model.sql` | blob 去重、引用和 GC。 |
| `0008_v551_user_version_utc.sql` | user_version 与时间语义。 |
| `0009_v564_gateway_mode.sql` | 模型调用的 fake/replay/live 审计字段。 |
| `0010_model_control_center.sql` | 前端模型注册、路由和能力配置。 |
| `0011_schedule_calendar.sql` | 学期、固定课表和日历调整。 |
| `0012_calendar_workday_source_date.sql` | 补班来源日期语义。 |
| `0013_document_artifacts.sql` | HTML/PDF 文档产物。 |
| `0014_specialized_gateway_models.sql` | OCR、Embedding、Rerank 等专用模型。 |
| `0015_chapter_manual_notes.sql` | 章节手工备注。 |
| `0016_v6_material_domain.sql` | V6 Material Domain。 |
| `0017_v6_source_spans_coverage.sql` | Source Span、覆盖计划、账本和报告。 |
| `0018_v6_learning_intermediates.sql` | Segment 与 Lesson Understanding。 |
| `0019_v6_cognitive_layer_and_run_provenance.sql` | Cognitive Map、批次审计和运行来源。 |
| `0020_v6_evidence_v2.sql` | content claims、claim sources 与 Evidence V2。 |
| `0021_v6_evidence_producer_provenance.sql` | 逐生产者/逐调用来源可见性。 |
| `0022_v6_quality_states.sql` | 多维质量状态。 |
| `0023_v6_learning_loop.sql` | Knowledge Unit、mastery 与学习反馈闭环。 |

## 8. 前端文件

### 8.1 入口与基础层

| 文件 | 用途 |
|---|---|
| `frontend/src/main.ts` | 创建 Vue、Pinia 和 Router。 |
| `frontend/src/App.vue` | V6 品牌、侧边栏、顶栏、主题/玻璃控制和 RouterView。 |
| `frontend/src/router/index.ts` | 页面路由与懒加载。 |
| `frontend/src/style.css` | Apple 语义化玻璃界面、报纸主题、响应式布局和公共组件样式。 |
| `frontend/src/api/client.ts` | `/api` 同源客户端、错误标准化和 JSON 请求。 |
| `frontend/src/api/endpoints.ts` | 所有业务 API 类型与封装。 |
| `frontend/src/stores/health.ts` | 后端/网关在线状态。 |
| `frontend/src/stores/data.ts` | 课程、章节、课时和考试共享数据。 |
| `frontend/src/composables/useRunEvents.ts` | SSE 优先、轮询兜底的运行状态更新。 |
| `useTheme.ts`、`useGlass.ts`、`useToast.ts` | 主题、玻璃效果和全局提示。 |

### 8.2 页面组件

| 文件 | 用途 |
|---|---|
| `DashboardPage.vue` | 今日总览、统计、七日课程、最近运行、错题分布和底部考试。 |
| `CoursesPage.vue` | 课程列表、新增、删除影响预览和精确名称防误删。 |
| `CalendarManagePage.vue` | 临时调整置顶、学期、固定课表、节假日/调休/补班和七日预览。 |
| `ChaptersPage.vue` | 按课程展开章节，弹窗编辑章节名称、状态和备注，支持防误删。 |
| `UploadPage.vue` | 桌面材料上传与解析状态。 |
| `LessonFlowPage.vue` | 创建课堂任务、查看 16+ 节点 V6 DAG、Segment、Coverage、Cognition、Evidence 和产物。 |
| `HomeworkFlowPage.vue` | 作业工作流输入、运行和结果。 |
| `ErrorsPage.vue` | 错题列表、确认/拒绝与状态。 |
| `ReviewPage.vue` | 章节/考前复习任务和产物。 |
| `ModelsPage.vue` | 模型注册、能力、协议、路由、网关服务和额度。 |
| `RunsPage.vue` | 全部运行、节点状态、耗时、Token、错误和重试。 |
| `SyncPage.vue` | Obsidian 同步状态和操作。 |
| `GraphPage.vue` | 知识关系 ECharts 可视化。 |
| `MobileUploadPage.vue` | 手机端材料上传。 |
| `MobileTokenPage.vue` | 本机生成、轮换和管理移动访问 Token。 |
| `NotFoundPage.vue` | 未知路由页面。 |

### 8.3 通用组件

| 文件 | 用途 |
|---|---|
| `widgets/DagFlow.vue` | DAG 节点图。 |
| `widgets/NodeDetail.vue` | 单节点输入、输出、模型、耗时和错误详情。 |
| `widgets/EvidenceV2Panel.vue` | Evidence V2 claims、来源和门禁展示。 |
| `widgets/States.vue` | Loading/Empty/Error 状态容器。 |
| `widgets/StatusBadge.vue` | 统一业务状态徽标。 |
| `widgets/ProgressBar.vue` | 进度条。 |
| `widgets/StatCard.vue` | 总览统计卡片。 |
| `widgets/PageHeader.vue` | 页面标题与操作区。 |
| `widgets/ToastHost.vue` | 全局通知。 |
| `widgets/Icon.vue`、`icons.ts` | 本地 SVG 图标体系。 |
| `widgets/GlassControl.vue` | 玻璃强度/主题控制。 |
| `widgets/dag-types.ts` | DAG 前端显示类型。 |

## 9. 测试与工具

| 文件/组 | 用途 |
|---|---|
| `tools/run_tests_isolated.py` | 官方完整隔离入口；创建临时数据根、拦截真实网关、核对正式库三元组。 |
| `tools/_run_unittest_zero_token.py` | 安装网络熔断并执行 unittest discovery。 |
| `tools/migrate_database.py` | 显式数据库升级工具。 |
| `tools/restore_snapshot.py` | 校验后恢复快照。 |
| `tools/v6_coverage_benchmark.py` | V6 材料覆盖基准。 |
| `tools/run_live_model_smoke.py` | 人工真实模型冒烟；默认不会被隔离测试调用。 |
| `test_v6_material_integrity.py` | Material Domain、Source Map、覆盖账本、silent drop。 |
| `test_v6_phase2_understanding.py` | 全量分段、理解、合并、时间轴和迁移。 |
| `test_v6_phase2_closeout.py` | 域隔离、预算、并发取消、缓存复用和 fallback。 |
| `test_v6_phase3_cognitive.py`、`test_v6_cognitive_boundary.py` | Student Simulator、认知类型、批次预算和来源边界。 |
| `test_v6_evidence_v2.py`、`test_v6_evidence_provenance.py` | Evidence V2 和逐生产者可见性攻击测试。 |
| `test_v6_phase5_phase6.py` | Composer V2、出版、错题/mastery/复习闭环。 |
| `test_large_input_safety.py` | 超长输入、强制切分、最终消息预算和超时策略。 |
| `test_workers.py` | 队列、租约、执行、取消与幽灵运行修复。 |
| `test_document_artifacts.py` | HTML/PDF 产物和内容结构。 |
| `test_schedule_calendar.py` | 课表、节假日、调休和补班。 |
| `test_model_control_center.py`、`test_v564_gateway_mode.py` | 模型管理和网关模式。 |
| `tests/unit/test_v551_*` | 实例锁、恢复、数据库和稳定性历史回归。 |
| `tests/unit/test_v561_isolation.py` | 正式库隔离 fail-closed。 |
| `tests/unit/test_v562_fts.py` | FTS 并发、锁和降级。 |
| `tests/unit/test_v563_tokenizer.py` | jieba 初始化、并发和降级。 |

## 10. 正式数据目录

`backend/data/` 是正式运行数据，不应加入代码级清理：

| 路径 | 用途 |
|---|---|
| `v5.db` | 当前正式 SQLite 主库；文件名为兼容历史版本保留。 |
| `backups/` | 启动自动备份，默认保留最近 7 份。 |
| `uploads/` | 已上传材料的 blob/文件。 |
| `artifacts/` | 正式 HTML/PDF 产物。 |
| `obsidian_vault/` | 默认 Obsidian 输出根。 |
| `quarantine/` | 不安全或解析失败文件隔离区。 |
| `.instance.lock` | 后端单实例锁。 |

密钥由本地网关和 `.env` 管理，不应写入文档、前端、Git 或模型提示词。

## 11. 最后发布验收结果

- 后端完整隔离回归：**528/528 通过**。
- 失败/错误：**0/0**。
- 隔离测试真实网关访问：**0**。
- 正式数据库测试前后：SHA-256、修改时间、长度完全一致。
- 前端：`vue-tsc` 通过，Vite 生产构建通过，共转换 698 modules。
- Python 关键模块 `py_compile` 通过。
- 正式根路径 `/`：HTTP 200，返回生产 SPA。
- `/api/health`：HTTP 200，版本 `6.0.0`，gateway `live`。
- 迁移：schema version `23`，外键和升级路径纳入隔离测试。
- 新增发布修复定向测试：长输入/超时 25 项、并发取消 3 项、重试覆盖归属 1 项、孤儿任务取消 3 项均通过。

## 12. 已知边界

- OCR、视觉、Embedding、Rerank 的实际效果与对应上游模型是否可用有关；本轮发布没有消耗真实模型额度做内容质量基准。
- `GraphPage` 使用 ECharts，首屏对应 chunk 较大，但已按路由懒加载，不阻塞其他页面。
- 历史 V5 运行记录和 `v6-phase2-*` engine_version 会原样保留；V6.0.0 新运行使用 `v6.0.0-on`，这是审计所需，不应批量改写历史数据。
- 根目录和数据库仍有 `v5` 命名以保持快捷方式、路径和数据兼容；产品/API 展示版本已经统一为 V6.0.0。
- 测试夹具中的假密钥仅用于泄露防护测试，不是可用凭证。

## 13. 当前建议操作

1. 使用 `http://127.0.0.1:8800/`，不要再把 5173 开发服务器作为正式入口。
2. 在“模型与额度”确认角色路由和候选模型启用状态。
3. 新建正式课程、章节和课表。
4. 上课后上传本次课堂唯一材料，创建新的听课任务。
5. 只有 Coverage、Evidence 和 Quality 都通过时，把产物视为正式学习笔记。
6. 后续通过作业与已确认错题持续更新 mastery 和复习优先级。

