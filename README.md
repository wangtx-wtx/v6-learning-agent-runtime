# v5 自动化学习系统(当前:MVP 稳定化中)

## 1. 四级状态说明

| 级别 | 含义 | 当前适用组件 |
|---|---|---|
| ✅ 生产可用 | 本地运行、基本上传、Token 管理、基础 DAG 执行 | 本地桌面 + Token 管理 + lesson/homework 主链路 |
| 🧪 实验可用 | 依赖外部模型有效，未覆盖边界与并发 | vision/vl、中文 RAG、parallel_solver |
| 🔶 降级/占位 | 调用失败时走 fallback,可继续但不保证最优 | note_writer、evidence、scope_checker |
| ❌ 未接入 | 依赖未完成或不在本轮范围 | 知识图谱可视化、Notion/IMA 同步 |

## 2. 安全说明

- **Token 鉴权**(V5.2 新增):启用 `V5_MOBILE_TOKEN` 后,除白名单路径(`/api/health`、`/api/mobile/server-info`、`/api/admin/mobile-token/*`)和本机 `127.0.0.1` / `::1` 外,所有 `/api/*` 请求必须带 `x-app-token` header 或 `?token=xxx`。管理 Token 见 `#/m-token`(仅本机访问)。
- **上传安全**(V5.2):扩展名白名单(15 种)+ UUID 安全文件名 + 50MB 单文件上限 + magic bytes 校验 + 响应不返回绝对路径。
- **CORS**(V5.2):收紧到 4 个明确原点(`localhost:5173` /`127.0.0.1:5173` / `localhost:8800` / `127.0.0.1:8800`),不再 `allow_origins=["*"]`。
- **备份**:SQLite 原生 `Connection.backup()` API,每次启动自动备份到 `backend/data/backups/`,保留最近 7 个。
- **状态机**:work run 状态强制白名单转换,禁止从终态回到非终态。

## 3. 已知限制

- 中文 RAG 使用 jieba + 混合加权,**未接**向量索引(sqlite-vec 留到 V5.3)
- DAG 暂无并行执行
- evidence 硬 Gate 要求 explainer 返回 `evidence` 字段
- 知识图谱依赖历史数据,需调用 `POST /api/migrate` 重建

---

# v5.1 本地学习 Agent Runtime

以课程章节为核心、以原始材料证据链为约束、以学生听课认知过程为生成逻辑、以错题反馈驱动复习的本地学习 Agent Runtime。

## 技术选型

| 项目 | 选型 |
|---|---|
| 后端 | Python 3.12 + FastAPI |
| 数据库 | SQLite (MVP 向量检索用 NumPy 暴力召回) |
| LLM 入口 | 本地网关 `http://127.0.0.1:8080`（Unified API Gateway，纯转发代理，只读访问） |
| 前端 | Vue 3 + Vite + TypeScript + Pinia + Tailwind |
| 图表 | ECharts |
| 工作流 | 固定 DAG(V5.2 已加 fallback_models + 状态机) |
| 同步 | Obsidian 一期 |

## 目录结构

```
v5/
├── backend/
│   ├── run.py                     # 后端入口 (uvicorn, port 8800)
│   ├── requirements.txt
│   ├── data/
│   │   ├── v5.db                  # SQLite 数据库
│   │   ├── obsidian_vault/        # Obsidian vault 自动生成
│   │   ├── uploads/               # 上传文件
│   │   └── backups/               # 自动备份 (最多保留最近 7 个)
│   └── app/
│       ├── config.py              # 配置 (网关 URL, 路径, MOBILE_TOKEN)
│       ├── database.py            # SQLite schema + 查询工具
│       ├── models_registry.py     # 模型注册表
│       ├── gateway.py             # 网关客户端 (只读)
│       ├── reasoning.py           # Reasoning 解析器
│       ├── routing.py             # 配额路由
│       ├── dag.py                 # DAG 引擎 (含 fallback_models + 状态机)
│       ├── dag_lesson.py          # 听课流 DAG
│       ├── dag_homework.py        # 作业流 DAG
│       ├── dag_error.py           # 错题流 DAG
│       ├── dag_review.py          # 复习流 DAG
│       ├── evidence.py            # 证据审计
│       ├── obsidian.py            # Obsidian 同步
│       ├── rag.py                 # RAG 检索 (V5.2: jieba + 关键词加权)
│       ├── schemas.py             # V5.2 Pydantic 数据契约
│       ├── env_file.py            # V5.2 .env 安全读写(原子写入)
│       ├── tailscale.py           # V5.2 Tailscale CLI 封装
│       ├── seed_data.py           # 校历/课程表导入
│       ├── migration.py           # V5.2 旧数据迁移 + schema 演进(run_migrations)
│       ├── backup.py              # SQLite backup() API 自动备份
│       └── main.py                # FastAPI 主应用 + 移动端 Token 管理 API
├── frontend/
│   ├── src/
│   │   ├── App.vue                # 侧边栏布局 12 页面
│   │   ├── components/            # 各页面组件
│   │   └── style.css              # Tailwind 样式
│   └── package.json
├── v5_config.yaml                 # 模型配置参考
└── README.md
```

## 快速开始

### 后端

```bash
cd backend
pip install -r requirements.txt
python run.py          # http://127.0.0.1:8800
```

### 前端

```bash
cd frontend
npm install
npm run dev            # http://127.0.0.1:5173 (API 代理到 8800)
```

### Windows 一键启动

双击 `启动 v5.bat` 即可同时启动后端和前端。

## 模型路由

优先顺序：

```
微信免费 DeepSeek V4 Flash 优先
→ 官方 DeepSeek V4 Flash 兜底
→ Qwen3.8-27B 做独立审查
→ Qwen3.8 Flash 做轻任务
→ Qwen3-VL Flash 做视觉
→ MiniMax M3 只做高风险并行 / 备选
```

模型 ID 映射（内部逻辑 ID → 网关模型名）：

| 内部 ID | 网关模型名 | 用途 |
|---|---|---|
| `deepseek_v4_free` | `Deepseek-v4-flash` | 默认生成/解题，微信免费 |
| `deepseek_v4_official` | `deepseekv4-flash` | 官方兜底 |
| `qwen3_8_27b` | `ecnu-plus` | 独立审查/教学化改写 |
| `qwen3_flash` | `qwen3.8-flash` | 轻量结构化任务 |
| `qwen3_vl` | `qwen3-vl-flash` | 视觉识别 |
| `minimax_m3` | `minimax-m3` | 高风险并行/备选审查 |
| `glm_flash` | `glm-5.3-flash` | 后备 |
| `embedding` | `ecnu-embedding-small` | RAG Embedding |
| `rerank` | `ecnu-rerank` | RAG Rerank |

### 配额路由（微信免费额度）

| 5h 用量 | 策略 |
|---|---|
| `<70%` | 微信免费承担低风险、中风险、常规写作、常规解题 |
| `70%-90%` | 低风险任务继续走微信免费；正式笔记/解答/错因转官方 |
| `>=90%` | 所有 DeepSeek 任务转官方，低优先级批量任务暂停 |

## 工作流

### 1. 听课流 (`/api/workflows/lesson`)

```
local_extract → vision_reader → splitter → lesson_structurer → student_simulator → note_writer → critic → publish
```

输出：正式听课笔记（带证据链）、课堂结构图、学生认知模拟层、疑难点列表。

### 2. 作业流 (`/api/workflows/homework`)

```
vision_reader → splitter → risk_classifier → solver → parallel_solver → explainer → evidence_checker → scope_checker
```

输出：切题结果、知识点标注、风险分级、最终答案、详细解题过程、教学化解释、证据链、超纲风险。

### 3. 错题流 (`/api/workflows/error`)

```
vision_reader → analyst → cross_check → ingest
```

输出：题目结构化提取、AI 错因假设、候选错因、相关笔记和知识点。状态为 `provisional`，人工确认后变 `confirmed`。

### 4. 复习流 (`/api/workflows/review`)

```
aggregator → writer → self_test → auditor
```

章末复习 / 考前复习两种模式。输出章节大纲、复习材料、错题热点、知识地图、自测卷、考前倒排计划。

## API 端点

### 健康检查

| 方法 | Path | 描述 |
|---|---|---|
| GET | `/api/health` | 健康检查 |

### 课程管理

| 方法 | Path | 描述 |
|---|---|---|
| GET | `/api/courses` | 课程列表 |
| POST | `/api/courses` | 创建课程 `{name, code?, semester?, teacher?, schedule?}` |
| GET | `/api/chapters` | 章节列表（可按 course_id 过滤） |
| POST | `/api/chapters` | 创建章节 `{course_id, chapter_no, title, syllabus_ref}` |
| GET | `/api/lessons` | 课时列表（可按 chapter_id / course_id 过滤） |
| POST | `/api/lessons` | 创建课时 `{chapter_id, course_id, lesson_no, title, date}` |

### 章节状态机

| 方法 | Path | 描述 |
|---|---|---|
| GET | `/api/chapters/{id}/state` | 获取章节状态及可能转移列表 |
| POST | `/api/chapters/{id}/advance` | 推进到下一状态 |
| POST | `/api/chapters/{id}/set_status?status=...` | 手动设置章节状态（需为合法值） |
| POST | `/api/chapters/{id}/status?status=...` | 更新章节状态 |
| POST | `/api/chapters/{id}/review` | 标记复习已生成 |

### 模型与路由

| 方法 | Path | 描述 |
|---|---|---|
| GET | `/api/models` | 模型注册表列表 |
| GET | `/api/usage` | 实时用量（从网关读取） |
| GET | `/api/routes/{workflow}` | 工作流路由（根据配额计算） |

### 工作流

| 方法 | Path | 描述 |
|---|---|---|
| POST | `/api/workflows/lesson` | 运行听课流 |
| GET | `/api/workflows/lesson/{run_id}` | 获取听课流运行详情 |
| POST | `/api/workflows/homework` | 运行作业流 |
| GET | `/api/workflows/homework/{run_id}` | 获取作业流运行详情 |
| POST | `/api/workflows/error` | 运行错题流 |
| POST | `/api/workflows/review` | 运行复习流 |

### 错题

| 方法 | Path | 描述 |
|---|---|---|
| GET | `/api/errors` | 错题列表（可按 status / chapter_id 过滤） |
| POST | `/api/errors/{id}/confirm` | 确认错题 → confirmed |
| POST | `/api/errors/{id}/reject` | 拒绝错题 → rejected |
| GET | `/api/notes` | 学习笔记列表 |
| GET | `/api/answer_items` | 答题项列表（可按 question_id 过滤） |

### 材料上传

| 方法 | Path | 描述 |
|---|---|---|
| POST | `/api/materials/upload` | 上传材料文件（PDF/PPT/图片/文本） |

### 校历 & 考试安排

| 方法 | Path | 描述 |
|---|---|---|
| GET | `/api/calendar` | 校历事件（可按 course_id / event_type 筛选） |
| POST | `/api/calendar` | 新增校历事件 / 手动考试时间 |
| PUT | `/api/calendar/{event_id}` | 改期 / 修改事件 |
| DELETE | `/api/calendar/{event_id}` | 删除事件 |
| POST | `/api/calendar/seed` | 从旧项目校历幂等导入 |
| GET | `/api/exams` | 考试安排（含课程名/编码联查） |

### 教学大纲导入

| 方法 | Path | 描述 |
|---|---|---|
| POST | `/api/syllabus/import` | 导入课程教学大纲 JSON，自动创建 chapter + lesson 节点 |

### 知识图谱



| 方法 | Path | 描述 |
|---|---|---|
| GET | `/api/graph` | 图谱节点+关系 |
| GET | `/api/graph/nodes` | 图谱节点 |
| GET | `/api/graph/edges` | 图谱关系 |

### 作业与题目

| 方法 | Path | 描述 |
|---|---|---|
| GET | `/api/homeworks` | 作业列表 |
| POST | `/api/homeworks` | 创建作业 |
| GET | `/api/questions` | 题目列表（可按 homework_id 过滤） |

### 同步

| 方法 | Path | 描述 |
|---|---|---|
| GET | `/api/sync` | 同步状态列表 |
| GET | `/api/sync/vault` | Obsidian vault 路径 |

### 运行日志

| 方法 | Path | 描述 |
|---|---|---|
| GET | `/api/runs` | 运行列表 |
| GET | `/api/runs/{run_id}` | 运行详情（含 run_nodes） |

### 数据迁移 & 备份

| 方法 | Path | 描述 |
|---|---|---|
| POST | `/api/migrate` | 从旧项目迁移数据 |
| GET | `/api/backups` | 备份列表 |
| POST | `/api/backups` | 手动创建备份 |

### RAG

| 方法 | Path | 描述 |
|---|---|---|
| GET | `/api/rag/search?q=...` | RAG 检索 |

## 数据模型

```
course → chapter → lesson → material → source_chunk → evidence
```

章节状态机：`not_started → in_progress → material_ready → homework_ready → review_pending → review_generated → reviewed`

核心表：`courses`, `chapters`, `lessons`, `materials`, `source_chunks`, `evidence`, `notes`, `homeworks`, `questions`, `answer_items`, `errors`, `reviews`, `workflow_runs`, `run_nodes`, `sync_jobs`, `graph_nodes`, `graph_edges`, `academic_calendar`

### 数据自动备份

后端每次启动时自动备份 SQLite 数据库到 `backend/data/backups/v5_YYYYMMDD_HHMMSS.db`，最多保留最近 7 个备份。

## Obsidian 同步

Vault 结构自动创建在 `backend/data/obsidian_vault/`：

```
vault/
  00 Inbox/
  01 Courses/
    ${课程}/
      Chapter XX/
        LXX 笔记.md
        LXX 作业.md
        Chapter XX 复习.md
  02 Homework/
  03 Errors/
  04 Reviews/
  05 Exams/
```

笔记为带 YAML front matter 的 Markdown，包含课程、章节、课时、状态、前后章链接。

## 前端页面

| 页面 | 功能 |
|---|---|
| 总览 | 课程/章节/课时/运行统计 |
| 课程 | 课程列表和创建 |
| 章节 | 章节树：按课程分组，状态推进 / 标记复习 |
| 上传 | 材料文件上传 |
| 听课流 | 运行听课 DAG 并展示结果 |
| 作业流 | 运行作业 DAG 并展示结果 |
| 错题 | 错题列表 + 确认/拒绝 |
| 复习中心 | 运行复习流 |
| 知识图谱 | ECharts 可视化（读取 /api/graph 数据） |
| 模型路由 | 查看模型列表和配额状态 |
| 同步 | Obsidian 同步状态 |
| 运行日志 | 所有 workflow run 记录 |

## 当前状态

### 已完成 ✅
- FastAPI + SQLite 后端骨架
- 全部数据模型 + 18 张核心表
- 模型路由注册表（9 个模型）
- 网关只读客户端（Unified API Gateway，纯转发代理，服务本身不含 LLM）
- 配额路由（微信免费优先 / 官方兜底 / MiniMax 高风险）
- 听课流 DAG（8 节点，全链路）
- 作业流 DAG（并行解题 + 审查 + 证据校验 + 超纲审计）
- 错题流 DAG（provisional/confirmed 状态机）
- 复习流 DAG（章末/考前）
- 证据审计（chunk 存在校验 + quote 匹配 + locator）
- Obsidian 同步 MVP
- RAG 检索（基于 token 命中）
- 旧数据迁移（14 课程 / 13 错误 / 23 图谱节点）
- Vue 3 前端：深色模式、12 页面、ECharts
- 章节状态机（7 状态自动推进）
- SQLite 自动备份 + 手动备份 API
- DAG 运行超时和错误降级
- 校历导入 + 期末考试建议日期（13 门课程自动排期，可手动修改）
- 校历 / 考试安排 CRUD API
- 教学大纲导入 API（自动建 chapter / lesson）
- 前端新增「章节树」页
- API 完整文档（共 49 个 API 端点）
- IMA OpenAPI 客户端已安装并验证（skill + 凭据 + list_notebook 鉴权成功）

### 需要网关提供后完善 🚧
- [x] 真实 LLM 调用（已通过 Unified API Gateway 鉴权，DeepSeek/Qwen/MiniMax 实测 200）
- [ ] 真实 embedding 索引（当前为 keyword 匹配）
- [ ] 多模态图片理解（仅当 qwen3-vl 可用）

### 需要用户提供 🕐
- [ ] 各课教学大纲
- [x] 考试安排已按校历自动生成建议日期，需按教务处通知手动核正（已导入 13 门课 + 全局事件）
- [ ] IMA 官方接入文档
- [ ] 指定 Obsidian vault 路径（当前自动在 backend/data）
- [ ] 是否需要 Notion

## 权限说明

网关路径 `http://127.0.0.1:8080` 只读访问，绝不修改、不重启。
