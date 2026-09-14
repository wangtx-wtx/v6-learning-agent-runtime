# V6.0 学习 Agent Runtime｜第三方工程交接说明书

更新时间：2026-09-14  
项目目录：`C:\Users\28595\Desktop\新建文件夹 (4)\v5`  
正式入口：`http://127.0.0.1:8800/`  
本地网关管理页：`http://127.0.0.1:8317/admin`

> 本文用于把项目完整交接给新的开发者或开发模型。接手方应先阅读本文，再阅读
> `V6_CURRENT_STATUS_AND_FILE_GUIDE.md`、`V6_LEARNING_ENGINE_IMPLEMENTATION_PLAN.md`
> 和 `V6.0.0_RELEASE_NOTES.md`。不得把仓库中的旧 V5 名称误判为当前产品版本；这些名称
> 主要为路径、数据库和启动方式兼容而保留。

---

## 1. 项目定位

V6 不是普通资料库、简单 RAG 或自动摘要器，而是本地课程学习 Agent：

```text
课程 / 章节 / 课堂
        ↓
录音转写 / PPT / PDF / 图片 / 讲义
        ↓
完整材料理解 + 学生认知加工 + 原始证据约束
        ↓
结构化课堂笔记
        ↓
作业 / 错题 / 掌握度 / 复习反馈
        ↓
HTML / PDF / Obsidian
```

五条不可破坏原则：

1. 所有学习数据最终归属 `course → chapter → lesson`。
2. 课堂事实必须能返回原始转写时间、PPT 页或材料位置。
3. 先理解课堂和学生认知难点，再组装笔记；不能直接对材料做摘要。
4. 作业、错题和复习表现必须能影响后续学习内容与优先级。
5. 任务域内的唯一有效材料必须被处理，或者明确记录未使用原因；禁止静默丢弃。

---

## 2. 当前版本与交接状态

- 产品版本：`6.0.0`。
- 后端：Python + FastAPI + SQLite。
- 前端：Vue 3 + TypeScript + Vite + Pinia。
- 正式后端端口：`8800`。
- 前端开发端口：`5173`；仅用于开发，正式使用统一访问 `8800`。
- 本地模型网关端口：`8317`。
- 数据库 schema：`23`，迁移文件为 `0001`–`0023`。
- 最近一次完整隔离回归：`528/528` 通过，真实网关请求为 `0`。
- 最近一次前端生产构建：`vue-tsc + vite build` 通过。
- 最近一次正式健康检查：`/api/health` 返回版本 `6.0.0`、网关 `live`。

### 2.1 非常重要：Git 状态

当前分支为 `codex/v5.6`，最后可见提交为：

```text
79f4724 fix(rag): initialize jieba tokenizer and report fallback health
```

但 V6 学习引擎、迁移、前端、文档和发布修复中有大量已修改或新增文件尚未提交。
因此最后一个 Git commit **不代表当前可运行的 V6 全貌**。

交接前必须执行以下任一方案：

1. 推荐：备份正式数据后，将当前源码形成一个明确的 `V6.0.0 handoff snapshot` 提交。
2. 如果不能提交：至少完整复制项目目录，并保存 `git status --short` 和 `git diff --stat`。

不要执行 `git reset --hard`、`git checkout -- .` 或清理未跟踪文件；这会直接删除尚未提交的
V6 实现。

---

## 3. 系统运行结构

```text
浏览器 / 手机
    │
    ├─ 正式 SPA + API：http://127.0.0.1:8800
    │                    │
    │                    ├─ FastAPI API
    │                    ├─ SQLite：backend/data/v5.db
    │                    ├─ 上传：backend/data/uploads
    │                    ├─ HTML/PDF：backend/data/artifacts
    │                    └─ Obsidian：backend/data/obsidian_vault
    │
    └─ FastAPI GatewayClient
                         │ HTTP only
                         ▼
              Local LLM Gateway：http://127.0.0.1:8317
                         │
                         ├─ Provider
                         ├─ API Key
                         ├─ Model registration
                         ├─ Fallback / concurrency / usage
                         └─ upstream model APIs
```

V6 与本地网关是两个独立系统：

- V6 负责课程、材料、学习工作流、证据、错题、复习和出版。
- 网关负责上游供应商、密钥、客户端模型 ID、协议适配、并发和调用账本。
- V6 只通过 HTTP 使用网关，不应直接修改网关的 SQLite、主密钥或日志。
- 网关不可用时，V6 的课程和已有产物仍可访问，但真实模型节点不能运行。

---

## 4. 目录与关键文件

### 4.1 根目录

| 路径 | 用途 |
|---|---|
| `README.md` | 项目入口、功能与启动说明。 |
| `启动 v5.bat` | 正式一键启动入口；名称为旧版兼容，实际启动 V6。 |
| `启动远程上传.bat` | Tailscale Serve 私有远程上传入口。 |
| `启动公网上传.bat` | Tailscale Funnel 公网入口，必须启用移动 Token。 |
| `v5_config.yaml` | 历史兼容的可读模型配置参考；正式模型配置以数据库/API 为准。 |
| `backend/` | FastAPI、SQLite、DAG、Learning Engine、出版、测试。 |
| `frontend/` | Vue 3 控制台和手机上传界面。 |
| `docs/` | 架构、运维、测试、发布和 V6 设计资料。 |
| `test_fixtures/` | 合成测试材料，不是正式课程数据。 |
| `output/`、`tmp/`、`backend/tmp/` | 样板、测试或开发输出；不能当作正式数据库。 |

### 4.2 后端核心

| 文件 | 用途 |
|---|---|
| `backend/run.py` | Uvicorn 启动入口，监听 `8800`。 |
| `backend/app/main.py` | FastAPI 主应用；注册约百个课程、材料、运行、模型、校历、产物和移动端 API，并托管生产 SPA。 |
| `backend/app/config.py` | 环境、路径、正式/测试隔离、网关地址和超时的单一真相源。 |
| `backend/app/database.py` | SQLite 读写连接、事务和并发忙等策略。 |
| `backend/app/migration.py` | 启动时 schema 检查。 |
| `backend/app/lifecycle.py` | 启动备份、恢复、垃圾清理、Worker 和健康检查。 |
| `backend/app/dag.py` | 通用 DAG Runner、节点状态、fallback、取消与检查点。 |
| `backend/app/dag_lesson.py` | 课堂学习 16 节点主链。 |
| `backend/app/dag_homework.py` | 作业处理工作流。 |
| `backend/app/dag_error.py` | 错题识别、分析、交叉检查和入库。 |
| `backend/app/dag_review.py` | 复习材料生成工作流。 |
| `backend/app/material_parser.py` | TXT/Markdown/PDF/PPTX/图片等材料解析与长文本安全处理。 |
| `backend/app/document_artifacts.py` | 结构化 HTML 渲染、Chromium PDF、产物哈希与记录。 |
| `backend/app/rag.py` | 跨材料检索；不能重新用于单堂课 Top-K 截断。 |
| `backend/app/review_service.py` | 复习队列、作答和掌握度更新。 |
| `backend/app/schedule_calendar.py` | 学期、固定课表、节假日、调休和补班。 |
| `backend/app/obsidian.py` | Obsidian Markdown 同步。 |
| `backend/app/tailscale.py` | Tailscale Serve/Funnel 状态和命令封装。 |
| `backend/app/workers/` | 后台材料与工作流队列、恢复和取消。 |

### 4.3 V6 Learning Engine

| 文件 | 责任 |
|---|---|
| `learning_engine/contracts.py` | V6 内部结构和状态契约。 |
| `domain.py` | 冻结本次任务应处理的 Material Domain。 |
| `normalize.py` | 清理和标准化材料，同时保留来源位置。 |
| `deduplicate.py` | 合并重复内容并记录原因。 |
| `source_map.py` | 分配稳定 Source ID，建立转写时间/PPT页映射。 |
| `coverage.py` | Coverage Plan、Ledger、报告和硬门禁。 |
| `segment.py` | 按预算、时间和主题切分长课堂；无标点超长文本也必须安全切分。 |
| `understanding.py` | 逐段理解、Knowledge Unit 生成和全局合并。 |
| `cognition.py` | Student Simulator、认知项、模型预算和来源作用域。 |
| `composer.py` | Composer V2；根据材料与认知分析选择动态内容块。 |
| `evidence_v2.py` | 用 Source ID 从数据库确定性绑定证据。 |
| `quality.py` | processing/coverage/evidence/review/publication/sync 多维状态与 revision。 |
| `learning_loop.py` | Knowledge Unit 与作业、错题、复习、mastery 的长期闭环。 |
| `config.py` | V6 开关、shadow/on 行为及版本。 |

### 4.4 网关接入层

| 文件 | 用途 |
|---|---|
| `backend/app/gateway.py` | `GatewayClient` 统一入口和模型调用审计。 |
| `backend/app/gateway_engines.py` | `FakeEngine`、`ReplayEngine`、`LiveEngine`，负责协议、超时和错误分类。 |
| `backend/app/gateway_mode.py` | `fake/replay/live` 模式和测试环境防误连。 |
| `backend/app/models_registry.py` | V6 模型档案、能力和角色路由。 |
| `backend/app/routing.py` | 根据工作流角色选模型及 fallback 顺序。 |
| `backend/app/resources/gateway_fake/` | 离线零 Token 响应夹具。 |

### 4.5 前端核心

| 文件 | 用途 |
|---|---|
| `frontend/src/App.vue` | 应用外壳、侧栏和系统状态。 |
| `frontend/src/router/index.ts` | 页面路由。 |
| `frontend/src/api/client.ts` | API 客户端、超时和错误转换。 |
| `frontend/src/api/endpoints.ts` | 后端接口类型与调用封装。 |
| `DashboardPage.vue` | 今日总览。 |
| `CoursesPage.vue` | 课程与考试管理。 |
| `CalendarManagePage.vue` | 课表、校历、节假日、调休和补班。 |
| `ChaptersPage.vue` | 章节进度和手工备注。 |
| `UploadPage.vue` | 桌面材料收件箱。 |
| `MobileUploadPage.vue` | 手机采集页。 |
| `LessonFlowPage.vue` | 课堂任务、V6质量状态、证据和产物。 |
| `HomeworkFlowPage.vue` | 作业流程。 |
| `ErrorsPage.vue` | 错题确认与驳回。 |
| `ReviewPage.vue` | 复习队列、复习任务和产物。 |
| `ModelsPage.vue` | 模型档案、角色路由、能力和测试。 |
| `RunsPage.vue` | DAG运行、节点详情、错误和取消。 |
| `MobileTokenPage.vue` | 本机移动 Token 管理。 |

完整逐文件用途见 `docs/V6_CURRENT_STATUS_AND_FILE_GUIDE.md`。

---

## 5. 本地网关说明

### 5.1 网关位置与资产

当前便携版网关：

```text
C:\Users\28595\Desktop\新建文件夹 (5)\local-llm-gateway-v1.0.3-windows-x64-portable
```

主要文件：

| 路径 | 用途 |
|---|---|
| `Start-Gateway.ps1` / `启动网关.cmd` | 启动网关。 |
| `Stop-Gateway.ps1` / `停止网关.cmd` | 停止网关。 |
| `Open-Dashboard.ps1` | 打开 `http://127.0.0.1:8317/admin`。 |
| `.env` | 当前机器配置，敏感，不得提交或公开。 |
| `.env.example` | 无密钥配置样例，可交接。 |
| `data/gateway.db` | Provider、模型、路由和用量数据库。 |
| `data/master.key` | 解密数据库中 Provider Key 的主密钥。 |
| `logs/` | 运行日志，可能含调用元数据，不得公开。 |
| `run/gateway.pid` | 当前进程记录。 |

`data/gateway.db` 和 `data/master.key` 必须成对备份。只有数据库而没有主密钥时，已加密
API Key 无法恢复。

### 5.2 网络和安全边界

默认必须保持：

```env
LOCAL_GATEWAY_HOST=127.0.0.1
LOCAL_GATEWAY_PORT=8317
```

不要为了手机访问把网关改成 `0.0.0.0`。手机只应访问 V6 的 `8800`；V6 再从本机调用
网关。若确实需要暴露网关，网关会要求同时设置客户端 API Key 和管理密码，但本项目
没有这种必要。

禁止公开或提交：

- 网关 `.env`
- `data/gateway.db`
- `data/master.key`
- `logs/`
- 供应商 API Key
- V6 的 `backend/.env` 和移动访问 Token

### 5.3 V6 如何调用网关

V6 默认配置：

```env
V5_GATEWAY_MODE=live
V5_GATEWAY_URL=http://127.0.0.1:8317
V5_GATEWAY_TIMEOUT=180
```

长课堂分段节点另有启动脚本设置的 `V6_SEGMENT_GATEWAY_TIMEOUT=600`。

网关若启用了客户端鉴权，V6 使用：

```env
V5_GATEWAY_API_KEY=<gateway client key>
```

不得把 Provider 原始 Key 写入 V6。Provider Key 只在网关管理页面维护。

### 5.4 两层模型 ID

必须区分：

```text
V6 内部模型档案 ID
        ↓ model_profiles.gateway_model
网关“客户端模型 ID”
        ↓ 网关路由
供应商 / 上游模型 ID
```

示例：

```text
qwen3_flash                 ← V6 内部 ID
qwen3.8-flash               ← 网关客户端模型 ID
上游供应商实际模型名称       ← 只由网关维护
```

V6 的 `gateway_model` 必须与网关注册模型的“客户端模型 ID”完全一致，包括大小写、点号、
横线。不要把供应商内部模型名直接填到 V6，除非它恰好就是网关客户端 ID。

### 5.5 模型能力

V6 支持的档案能力：

```text
text / vision / multimodal / ocr / embedding / rerank
```

角色兼容规则：

- `embedding` 只能使用 embedding 模型。
- `rerank` 只能使用 rerank 模型。
- `ocr` 可使用 ocr、vision 或 multimodal。
- `vision_reader` 可使用 vision 或 multimodal。
- 其他文本角色可使用 text、vision 或 multimodal。

当前配置中可能出现的客户端模型 ID 包括：

```text
DeepSeek-flash
deepseekv4-flash
ecnu-plus
qwen3.8-flash
qwen3-vl-flash
minimax-m3
glm-5.3-flash
glm-ocr
text-embedding-v4
qwen3-rerank
```

此清单是项目配置参考，不代表上游凭证当前必然可用。接手方必须在网关管理页分别做最小
请求验证。

### 5.6 网关配置步骤

1. 双击网关 `启动网关.cmd`。
2. 打开 `http://127.0.0.1:8317/admin`。
3. 创建或检查 Provider。
4. 给 Provider 添加 API Key。
5. 注册 Model：客户端模型 ID、上游模型 ID、协议和能力必须正确。
6. 在网关面板执行最小测试，确认模型真实响应。
7. 打开 V6 `模型与额度` 页面。
8. 将 V6 `gateway_model` 对齐网关客户端模型 ID。
9. 为每个角色配置首选模型和 fallback。
10. 在 V6 再做一次最小模型测试，确认端到端链路，而不是只检查 `/v1/models`。

网关健康不能只凭管理页可打开或 `/v1/models` 有记录判断。至少要成功完成一次最小
`chat/completions`；OCR、embedding、rerank 也应分别验证各自协议。

### 5.7 三种网关运行模式

| 模式 | 用途 | 是否访问真实网关 |
|---|---|---:|
| `fake` | 单元测试、UI演示 | 否 |
| `replay` | 使用已记录响应做可复现回归 | 否 |
| `live` | 正式模型调用 | 是 |

测试进程不得静默从 fake/replay 回落到 live。

---

## 6. 启动与停止

### 6.1 正式启动

推荐双击：

```text
启动 v5.bat
```

脚本会：

1. 设置 `V5_ENV=production`、`V5_GATEWAY_MODE=live`、`V6_LEARNING_ENGINE=on`。
2. 检查 `8317`；必要时从便携版目录启动网关。
3. 执行前端生产构建；构建失败则停止启动。
4. 启动后端 `8800`。
5. 可选启动 `5173` production preview，供兼容调试。
6. 打开正式入口 `http://127.0.0.1:8800/`。

### 6.2 零 Token 演示

```powershell
& '.\启动 v5.bat' fake
```

fake 使用独立 `demo_data`，输出仅是测试夹具，不能作为正式学习内容。

### 6.3 开发启动

后端：

```powershell
Set-Location 'C:\Users\28595\Desktop\新建文件夹 (4)\v5\backend'
$env:V5_ENV = 'development'
$env:V5_GATEWAY_MODE = 'live'
python run.py
```

前端：

```powershell
Set-Location 'C:\Users\28595\Desktop\新建文件夹 (4)\v5\frontend'
npm install
npm run dev
```

开发地址是 `http://127.0.0.1:5173`，API 代理到 `8800`。

停止服务前先确认监听端口对应的 PID、命令行和父进程，避免误杀网关或其他开发工具。

---

## 7. 数据、备份和迁移规则

正式数据默认位于：

```text
backend/data/
├─ v5.db
├─ backups/
├─ uploads/
├─ quarantine/
├─ artifacts/
├─ obsidian_vault/
└─ keys/（如果存在）
```

规则：

1. 正式数据库仍叫 `v5.db`，这是兼容名称，不应擅自重命名。
2. 启动会使用 SQLite backup API 自动备份，默认保留最近 7 份。
3. 数据库升级只能新增顺序迁移；不要修改已经发布的 `0001`–`0023`。
4. 新功能从 `0024_*.sql` 开始。
5. 迁移前必须备份数据库并记录 SHA-256、长度、mtime 和 `user_version`。
6. 测试不得使用正式数据库、正式上传目录、正式产物目录或正式 Obsidian 目录。
7. 不要手工删除 SQLite 行来“清理状态”；应通过业务 API 或有审计的维护脚本。

测试环境必须满足：

```env
V5_ENV=test
V5_TEST_DATA_ROOT=<绝对临时目录，且不在正式 data 下>
```

`backend/app/config.py` 会拒绝测试指向正式数据根。

---

## 8. 学习工作流契约

课堂主链：

```text
01 resolve_material_domain
02 normalize_materials
03 deduplicate_materials
04 build_source_map
05 plan_coverage
06 segment_lesson
07 understand_segments
08 merge_lesson_understanding
09 student_simulator
10 compose_learning_content
11 bind_evidence
12 coverage_audit
13 critic
14 persist_note
15 render_document
16 obsidian_sync
```

不可破坏的运行语义：

- 单堂课生成不能重新退化为 Top-K RAG。
- `Material Domain` 是本次任务来源范围的唯一真相。
- 模型只能引用本批真正可见的 Source ID。
- Evidence Binder 是确定性程序，不让模型伪造 exact quote。
- `silent_dropped > 0` 时任务不能显示为完整成功。
- processing、coverage、evidence、review、publication、sync 是独立状态。
- Obsidian 同步失败不能覆盖内容质量状态。
- immutable retry 可以复用域，但 coverage report 必须归属当前 child run。
- 客户端页面断开不等于模型失败，后台任务应继续。
- 失败节点恢复不得无条件重跑全部昂贵节点。

RAG 的正确用途：跨课堂、跨章节、错题回溯、复习和自由问答；不是课堂阅读器。

---

## 9. HTML、PDF 与正式产物

正式产物由 `document_artifacts` 表和 `backend/data/artifacts/` 管理。

当前能力：

- 模型输出结构化学习内容。
- 本地确定性 Renderer 输出英伦报纸风格 HTML。
- Chromium 从同一结构化内容生成 PDF。
- 保存 template ID/version、content hash、状态和文件路径。
- API 支持列表、预览、HTML/PDF 下载和重新生成。

正式链路不是“模型直接写整页 HTML”，也不是浏览器简单打印模型文本。模型负责内容，程序负责
出版。HTML 和 PDF 必须使用同一个 structured document。

后续建议首先新增独立 `#/files` 文件中心，再把模板拆为 template registry。详细路线见本文
第 13 节。

---

## 10. 手机与远程访问

现有能力：

- `#/m-upload`：手机材料采集。
- `#/m-token`：仅本机管理移动 Token。
- Tailscale Serve/Funnel 辅助脚本。
- 非本机 API 可使用 `x-app-token` 或 URL token。

推荐正式网络结构：

```text
手机 → Tailscale 私网 → V6 :8800 → 本机网关 :8317
```

不要直接暴露网关。公网 Funnel 只应临时启用，并确保移动 Token 已配置。

当前移动鉴权仍是单一 Token，下一版建议改成设备 Token、哈希存储、过期时间和 scope；长期
Token 不应继续放在 URL 中。

---

## 11. 测试和验收

### 11.1 全量隔离测试

```powershell
Set-Location 'C:\Users\28595\Desktop\新建文件夹 (4)\v5\backend'
python tools\run_tests_isolated.py
```

要求：

- `V5_ENV=test`。
- 使用新的绝对临时数据根。
- 真实网关访问为 `0`。
- 正式数据库 SHA、长度和 mtime 不变。

### 11.2 前端构建

```powershell
Set-Location 'C:\Users\28595\Desktop\新建文件夹 (4)\v5\frontend'
npm run build
```

必须同时通过 TypeScript 和 Vite 构建。

### 11.3 真实模型测试

真实测试必须与隔离测试分开，人工明确发起：

```powershell
Set-Location 'C:\Users\28595\Desktop\新建文件夹 (4)\v5\backend'
python tools\run_live_model_smoke.py
```

该测试会消耗额度。不要把它放入默认 CI，也不要在未获允许时使用正式课堂材料。

### 11.4 内容级验收

仅有 HTTP 200 和测试通过不足以证明学习效果。真实课堂验收至少检查：

- 唯一材料处理率 `100%`。
- silent drop `0`。
- 转写时间轴覆盖率 `≥99%`。
- PPT 页覆盖率 `100%`。
- 课堂事实 Source Ref 覆盖率 `100%`。
- 无效、跨域、未呈现 Source Ref 为 `0`。
- HTML/PDF 内容一致。
- 错题可以追到 Knowledge Unit 和课堂原始来源。

---

## 12. 已知边界与禁止事项

### 已知边界

1. 当前工作区未形成完整 V6 Git 提交，接手前必须冻结。
2. 手机端目前主要是上传，不是完整管理控制台。
3. 文档已有后端 API，但没有独立文件管理中心。
4. 模板主要集中在 `document_artifacts.py`，尚未形成可插拔模板注册系统。
5. 真实三小时课堂应再做一次受控内容验收，不能只依赖假网关测试。
6. 模型价格、供应商可用性和客户端模型 ID 会变化，必须通过网关重新验证。

### 禁止事项

- 禁止覆盖、清空或用测试修改 `backend/data/v5.db`。
- 禁止公开 V6/网关 `.env`、API Key、移动 Token、`master.key` 和日志。
- 禁止修改已经应用的迁移文件。
- 禁止以 `/v1/models` 成功代替最小推理验证。
- 禁止让测试模式连接真实网关。
- 禁止恢复单堂课 Top-K 材料筛选。
- 禁止把模型教学补充伪装成课堂原话。
- 禁止让同步状态覆盖 evidence/coverage/review 状态。
- 禁止因为清理临时文件而递归删除项目根、数据根或网关 `data/`。

---

## 13. 下一阶段建议

按以下顺序实施：

### Stage A：交接冻结与可维护性

1. 备份 V6 正式数据和网关 `data/`。
2. 创建 V6.0.0 handoff Git snapshot。
3. 补 `.gitignore`，隔离 `backend/data`、`tmp`、`.playwright-cli`、产物与密钥。
4. 清理或归档开发期临时脚本，但先做引用扫描。
5. 更新过时的 `docs/TESTING.md` 测试数量。

### Stage B：手机远程访问和文件中心

1. 新增 `#/files`，统一管理 HTML/PDF、revision、预览、下载和重新排版。
2. 手机端增加任务、课程和产物只读管理。
3. 单 Token 升级为设备 Token + scope + 撤销。
4. 正式使用 Tailscale 私网，不暴露网关。

### Stage C：模板系统

1. 从 `document_artifacts.py` 拆出 template registry、HTML renderer 和 PDF engine。
2. 至少提供课堂笔记、复习讲义和错题册三类模板。
3. 切换模板不能调用模型；HTML/PDF必须共享内容哈希。

### Stage D：材料利用率和成功率

1. 增加输入、理解、发布、证据四层利用率报告。
2. 增加任务 Preflight、成本/分段估算和模型能力检查。
3. 完善节点级 checkpoint、错误分类和可恢复重试。
4. 增加全局/供应商/模型三级并发配额。

### Stage E：学习引擎与错题成品

1. 优化跨 segment 概念合并、术语别名、前置依赖和矛盾检测。
2. Student Simulator 使用掌握度和历史错误做个性化解释。
3. 拆分 factual critic 与 learning critic。
4. 输出单题诊断卡、章节错题册和考前错题精编 HTML/PDF。

详细指导方案应与 `V6_LEARNING_ENGINE_IMPLEMENTATION_PLAN.md` 中的既有不变量共同使用，不能
为了新增功能破坏已完成的 V6 Phase 1–6。

---

## 14. 第三方接手首日清单

1. 只读复制项目与网关，不立即改代码。
2. 阅读四份文档：本说明书、当前状态、Learning Engine Plan、Release Notes。
3. 执行 `git status --short`，确认未提交 V6 文件仍存在。
4. 备份 `backend/data`；网关 `data/gateway.db` 与 `data/master.key` 成对备份。
5. 执行前端 `npm run build`。
6. 执行后端隔离测试，并确认真实网关访问为 0。
7. 启动网关，分别做 chat、vision/OCR、embedding、rerank 的最小验证。
8. 启动 V6，检查 `/api/health`、正式 SPA、模型中心和运行日志。
9. 只用隔离材料做一次 fake/replay 课堂 DAG。
10. 提交接手基线报告：源代码状态、测试、构建、网关能力和未验证边界。

报告必须区分：

- 已从源码确认。
- 已通过隔离测试确认。
- 已通过真实网关确认。
- 仅来自历史报告、尚未复验。
- 当前尚未实现。

---

## 15. 交接时需要单独、安全传递的资产

源码包不应自动包含下列资产。若第三方确实需要，应通过单独安全渠道传递：

- 正式 `backend/data/v5.db`。
- 正式上传材料和生成产物。
- Obsidian Vault。
- V6 `backend/.env`。
- 网关 `.env`。
- 网关 `data/gateway.db` 与 `data/master.key`。
- Provider API Key。
- 移动访问 Token。

如果第三方只负责开发，优先提供脱敏数据库、合成材料和 fake/replay 环境，不提供正式密钥和
真实课程材料。

---

## 16. 一句话交接结论

当前项目是“V5 稳定基础设施 + V6 Learning Engine”的本地学习 Agent。接手方应保留课程、
SQLite、本地网关、DAG、证据、文档和错题闭环的现有资产，先冻结未提交的 V6 工作区，再在
隔离数据环境中继续开发；本地网关只作为 `127.0.0.1:8317` 的独立模型入口，不与 V6 数据库
合并，也不向手机或公网直接暴露。
