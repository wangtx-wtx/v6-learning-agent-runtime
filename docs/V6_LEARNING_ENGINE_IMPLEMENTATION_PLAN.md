# V6 Learning Engine 实施设计与补丁级任务书

版本：Draft 1  
日期：2026-09-13  
定位：**V6 = Learning Engine Rewrite on V5 Infrastructure**

## 1. 本文边界

V6 不重写课程、章节、课表、SQLite、本地网关、模型管理、DAG Runner、运行恢复、Blob、错题确认、复习调度、HTML/PDF、Obsidian 和前端框架。

V6 仅重构：

```text
材料域 → 唯一材料 → 全量覆盖计划 → 课堂理解 → 学生认知加工
      → 学习内容组装 → 确定性证据绑定 → 覆盖审计 → 出版
```

本文是实施规格，不是代码补丁。Phase 1 开始前需再次备份正式数据库，并继续使用隔离测试根。

## 2. 已核实基线与非核实材料

### 2.1 当前仓库已核实事实

- 当前数据库迁移最高版本为 `0015`。
- 当前听课 DAG 为 10 个节点，尚无 `student_simulator`。
- 本次听课材料选择仍包含：运行内 chunk 最多 12 块、RAG `top_k=8`。
- `lesson_outline` 最多读取前 20 个 chunk，且每块最多 300 字。
- `note_writer` 对每个候选 chunk 最多提供 500 字。
- 当前 Evidence 协议仍由模型产生 `chunk_id + quote`，程序再做模糊包含校验。
- 当前 `notes.status` 同时承载内容可信度和同步结果；Obsidian 成功会写成 `synced`。
- 现有 `document_artifacts`、确定性 HTML Renderer 与 Chromium PDF 可保留。
- 2026-09-13 本机隔离回归：**212/212 通过**，真实网关访问 0，正式数据库哈希和长度未变化；前端构建通过。

### 2.2 作为迁移动机、尚未在本轮复算的历史数据

附件 Session 给出的 47,416 字、262 chunks、2,349 字进入生成、约 5% 覆盖和 517 字笔记，作为 V6 Benchmark 的历史基线。Phase 1 必须提供可重复脚本重新计算；在脚本复算前，不将这些数字写成当前运行保证。

附件中的 218/218 属于另一时点的 Session 记录，不覆盖本轮实际验证的 212/212。

## 3. 五项不变量

1. 所有学习实体最终归属 `course → chapter → lesson`。
2. 课堂事实必须绑定真实来源，不能把模型补充伪装成课堂内容。
3. 笔记生成必须显式经过学生认知加工层。
4. 错题和掌握状态必须能够改变后续解释及复习优先级。
5. Material Domain 内任何材料或 source span 必须被处理，或留下机器可审计的未使用原因；禁止 silent drop。

## 4. 关键术语与质量公式

### 4.1 Material Domain

一次运行理论上必须处理的材料集合。创建后冻结，后续节点只能增加处理结果，不能删除域成员。

### 4.2 唯一有效材料

按内容哈希、规范化文本哈希和来源位置识别后的 canonical source spans。重复项仍保留账目，但不重复进入语义理解。

### 4.3 覆盖不是“每句话都写进笔记”

“全部利用”表示每个唯一、非噪声 source span 至少经过一个理解 segment；最终文档可根据信息价值压缩。未进入最终文档不等于未处理，但必须能追踪其理解结果和省略理由。

### 4.4 强制指标

```text
domain_accounting_rate
  = 有终态的 domain_items / domain_items 总数

semantic_processing_rate
  = 已进入 segment understanding 的 canonical 非噪声 spans
    / canonical 非噪声 spans 总数

source_ref_validity
  = 可解析 source refs / 全部 source refs

critical_claim_evidence_rate
  = 已绑定有效课堂来源的关键课堂 claim / 关键课堂 claim 总数

silent_drop_count
  = 没有处理记录且没有显式原因的 domain item 或 source span 数量
```

硬门禁：

- `domain_accounting_rate = 100%`
- `semantic_processing_rate = 100%`
- `source_ref_validity = 100%`
- `critical_claim_evidence_rate = 100%`
- `silent_drop_count = 0`

任一硬门禁不满足，`workflow_runs.status` 不得写为 `completed`，应写为 `degraded` 或 `failed`。

## 5. V6 模块布局

新增目录，避免继续膨胀 `dag_lesson.py`：

```text
backend/app/learning_engine/
  contracts.py             # 16 节点 Pydantic 契约
  domain.py                # Material Domain 冻结与归属校验
  normalize.py             # 文本、时间轴、页码和噪声规范化
  deduplicate.py           # 域内语义前去重
  source_map.py            # 稳定 Source ID
  coverage.py              # Planner、Ledger、Auditor
  segment.py               # 无 Top-K 的全量分段
  understanding.py         # segment 理解与全局合并
  cognition.py             # Student Simulator
  composer.py              # 动态内容块与 claim 生成
  evidence_v2.py           # Source ID 确定性绑定
  quality.py               # Critic 与质量门禁
  dag.py                   # V6 课堂 DAG 装配
```

现有模块继续保留：

- `app/dag.py`：通用 DAG 执行、重试、取消、审计。
- `app/material_parser.py`：文件解析入口；Phase 1 只扩展输出元数据。
- `app/gateway*.py`：模型调用与审计。
- `app/document_artifacts.py`：确定性出版。
- `app/obsidian.py`：同步执行；不再修改内容可信状态。

## 6. 数据结构

以下为内部契约的最低字段。实现时 Pydantic 模型必须 `extra='forbid'`，所有版本化输出携带 `schema_version`。

### 6.1 MaterialDomainSnapshot

```json
{
  "schema_version": "v6.1",
  "domain_id": 81,
  "run_id": 602,
  "scope": "lesson",
  "course_id": 7,
  "chapter_id": 31,
  "lesson_id": 42,
  "frozen": true,
  "domain_hash": "sha256:...",
  "items": [
    {
      "domain_item_id": 1,
      "material_id": 90,
      "source_kind": "transcript",
      "required": true,
      "ordinal": 1,
      "state": "included",
      "reason_code": null
    }
  ]
}
```

### 6.2 SourceSpan

```json
{
  "source_id": "T000128",
  "domain_id": 81,
  "domain_item_id": 1,
  "material_id": 90,
  "chunk_id": 788,
  "source_kind": "transcript",
  "locator": "00:31:18-00:31:51",
  "ordinal": 128,
  "start_ms": 1878000,
  "end_ms": 1911000,
  "page_no": null,
  "slide_no": null,
  "text": "真实原文……",
  "normalized_hash": "sha256:...",
  "token_count": 163,
  "canonical_source_id": "T000128"
}
```

Source ID 规则：

- 转写：`T` + 域内六位顺序号。
- PPT：`P` + 四位页码 + 可选段号，如 `P0017.02`。
- PDF/讲义：`D` + 四位页码 + 可选段号。
- 图片 OCR：`I` + 六位顺序号。
- ID 在 Material Domain 冻结后不可重排；重跑同一 domain version 必须稳定。

### 6.3 CoveragePlan

```json
{
  "plan_id": 12,
  "domain_id": 81,
  "model_profile_id": "qwen3_flash",
  "context_window": 1048576,
  "reserved_output_tokens": 65536,
  "reserved_system_tokens": 12000,
  "input_budget_tokens": 850000,
  "strategy": "single_pass",
  "segment_count": 1,
  "planned_source_count": 284,
  "unassigned_source_ids": []
}
```

若超过上下文，`strategy = segmented_map_merge`。Planner 只能分段，不能相关性淘汰。

### 6.4 SegmentUnderstanding

```json
{
  "segment_id": 9,
  "source_refs": ["T000128", "T000129", "P0017.01"],
  "units": [
    {
      "temp_id": "SU-09-03",
      "topic": "……",
      "kind": "definition",
      "summary": "……",
      "source_refs": ["T000128", "P0017.01"],
      "teacher_emphasis": 0.86,
      "relations": ["supports:SU-09-02"]
    }
  ],
  "unresolved": []
}
```

### 6.5 LessonUnderstanding

```json
{
  "lesson_id": 42,
  "knowledge_units": [
    {
      "id": "KU-0017",
      "topic": "……",
      "kind": "derivation",
      "summary": "……",
      "source_refs": ["T000128", "P0017.01"],
      "teacher_emphasis": 0.91,
      "relations": ["requires:KU-0012"],
      "coverage_source_refs": ["T000128", "T000129"]
    }
  ],
  "source_differences": [],
  "unresolved_conflicts": []
}
```

### 6.6 CognitiveMap

```json
{
  "lesson_id": 42,
  "items": [
    {
      "id": "CG-0031",
      "type": "missing_step",
      "knowledge_unit_ids": ["KU-0017"],
      "description": "老师从 A 直接跳到 C，学生可能缺少 B。",
      "severity": 0.8,
      "source_refs": ["T000128", "T000129"],
      "recommended_treatment": "explanation"
    }
  ]
}
```

允许类型：`confusion_point`、`prerequisite_gap`、`pitfall`、`emphasis`、`concept_relation`、`memory_anchor`、`missing_step`、`difficulty`。

### 6.7 LearningDocument V2

保留现有 `StructuredDocument` 外形，给每个 block 增加稳定 `block_id`、`claims` 和选择依据：

```json
{
  "block_id": "B-0027",
  "type": "pitfall",
  "title": "容易混淆的两个条件",
  "content": "……",
  "selection_reason": "cognitive_map:CG-0031",
  "claims": [
    {
      "claim_id": "CL-0088",
      "claim_type": "course_paraphrase",
      "text": "……",
      "source_refs": ["T000128", "P0017.01"]
    }
  ]
}
```

`claim_type`：

- `course_fact`：必须有有效 Source ID；关键事实必须通过绑定。
- `course_paraphrase`：必须有有效 Source ID，允许语义改写。
- `ai_explanation`：可无课堂来源，但渲染器必须显示“模型补充”。

### 6.8 BoundEvidence

模型不再提供 exact quote。程序按 Source ID 查询 SourceSpan：

```json
{
  "claim_id": "CL-0088",
  "source_id": "T000128",
  "material_id": 90,
  "chunk_id": 788,
  "locator": "00:31:18-00:31:51",
  "quote": "由程序从 source span 取得的真实文字",
  "binding_status": "bound",
  "binding_method": "source_id_exact"
}
```

### 6.9 MaterialCoverageReport

```json
{
  "run_id": 602,
  "domain_id": 81,
  "total_domain_items": 2,
  "unique_domain_items": 2,
  "total_source_spans": 284,
  "canonical_non_noise_spans": 270,
  "processed_spans": 270,
  "duplicate_spans": 9,
  "noise_spans": 5,
  "silent_dropped": 0,
  "domain_accounting_rate": 1.0,
  "semantic_processing_rate": 1.0,
  "timeline_coverage_rate": 0.997,
  "ppt_pages_total": 42,
  "ppt_pages_processed": 42,
  "source_refs_total": 91,
  "source_refs_valid": 91,
  "gate": "passed",
  "issues": []
}
```

## 7. 数据库迁移方案

所有迁移只追加，不修改既有 migration checksum。新表优先引用现有实体，旧表继续兼容读取。

### 7.1 `0016_v6_material_domain.sql`

新增：

```text
material_domains
  id, run_id UNIQUE, scope, course_id, chapter_id, lesson_id,
  version, domain_hash, state, frozen_at, created_at

material_domain_items
  id, domain_id, material_id, source_kind, ordinal, required,
  raw_chars, raw_tokens, content_hash, state, reason_code,
  duplicate_of_item_id, created_at
```

约束：

- `(domain_id, material_id)` 唯一。
- `state` 仅允许 `included / duplicate / unsupported / failed / excluded_with_reason`。
- `excluded_with_reason`、`unsupported` 和 `failed` 必须有 `reason_code`。
- 冻结后的 domain 禁止删除 item；调整范围必须创建新 version。

### 7.2 `0017_v6_source_spans_coverage.sql`

新增：

```text
source_spans
  id, source_id, domain_id, domain_item_id, source_chunk_id,
  material_id, source_kind, locator, ordinal, start_ms, end_ms,
  page_no, slide_no, text, normalized_text_hash, token_count,
  canonical_span_id, span_state, reason_code, created_at

coverage_plans
  id, run_id, domain_id, strategy, model_profile_id,
  context_window, input_budget_tokens, output_budget_tokens,
  planned_span_count, unassigned_count, plan_json, created_at

coverage_ledger
  id, run_id, domain_id, source_span_id, stage,
  outcome, reason_code, segment_id, knowledge_unit_id, created_at

coverage_reports
  id, run_id UNIQUE, domain_id, metrics_json, gate,
  silent_dropped, created_at, updated_at
```

约束：

- `(domain_id, source_id)` 唯一。
- canonical 非噪声 span 必须出现 `understand_segments / processed` ledger 记录。
- `outcome=not_used` 必须有受控 `reason_code`。
- `silent_dropped` 由 SQL/确定性程序计算，禁止模型填写。

### 7.3 `0018_v6_learning_intermediates.sql`

> **迁移编号顺延说明（Phase 2/3 实施时更新）**
>
> 原设计把 Evidence V2 预留为 `0019`，但 Phase 2 Closeout 需要先解决「inline 转写
> 的 run 级来源」问题、Phase 3 需要 Cognitive Layer，二者合并为
> `0019_v6_cognitive_layer_and_run_provenance.sql`。随后 Phase 4.1（逐 claim
> 生产者可见性修复）又占用了 `0021`，因此 Quality States / Learning Loop 再次顺延：
>
> | 主题 | 原编号 | 现编号 |
> |---|---|---|
> | 分段理解中间产物 | 0018 | **0018**（不变） |
> | 认知层 + run 材料来源 | — | **0019** |
> | Evidence V2 | 0019 | **0020** |
> | Evidence producer provenance（Phase 4.1） | — | **0021** |
> | Quality States | 0020 | **0022** |
> | Learning Loop | 0021 | **0023** |
>
> 已发布的 `0016`—`0020` 迁移**不得原地修改**。

新增：

```text
lesson_segments
  id, run_id, domain_id, ordinal, title, start_source_id,
  end_source_id, token_count, status, output_json, created_at

lesson_segment_sources
  segment_id, source_span_id, role, ordinal

lesson_understandings
  id, run_id UNIQUE, lesson_id, schema_version,
  structured_json, content_hash, status, created_at

cognitive_maps
  id, run_id UNIQUE, lesson_id, schema_version,
  structured_json, content_hash, status, created_at

knowledge_units
  id, course_id, chapter_id, lesson_id, stable_key,
  topic, kind, summary, emphasis, status, created_at, updated_at

knowledge_unit_sources
  knowledge_unit_id, source_span_id, relation
```

### 7.4 `0020_v6_evidence_v2.sql`

> 编号由原 `0019` 顺延为 `0020`（见 §7.3 说明）。

新增：

```text
content_claims
  id, run_id, note_id, block_id, claim_key, claim_type,
  claim_text, importance, evidence_status, created_at

claim_sources
  id, claim_id, source_span_id, relation,
  binding_status, binding_method, bound_quote, locator, created_at
```

保留 `evidence_links`，由 Evidence Binder 写兼容投影，不再作为 V6 的事实主表。

### 7.5 `0022_v6_quality_states.sql`

> 编号由原 `0020` 顺延为 `0022`（Phase 4.1 占用 0021；见 §7.3 说明）。

新增：

```text
learning_quality_states
  run_id PRIMARY KEY,
  processing_status,
  coverage_status,
  evidence_status,
  review_status,
  publication_status,
  sync_status,
  degradation_reason,
  updated_at

note_revisions
  id, note_id, revision, schema_version, structured_json,
  content_hash, evidence_status, coverage_status,
  review_status, created_at
```

`notes.status` 迁移期仅作为兼容字段，不再由 Obsidian 修改。

### 7.6 `0023_v6_learning_loop.sql`

> 编号由原 `0021` 顺延为 `0023`（见 §7.3 说明）。

新增：

```text
knowledge_mastery
  id, knowledge_unit_id, course_id, chapter_id,
  mastery, confidence, updated_from, updated_at

learning_feedback_links
  id, knowledge_unit_id, source_type, source_id,
  relation, weight, evidence_json, created_at
```

`source_type` 允许 `homework_question / error / review_attempt / manual`。

## 8. 状态模型

### 8.1 run_status

沿用 `queued / running / completed / failed / cancelled / interrupted`，新增终态 `degraded`。

允许：

```text
running → completed | degraded | failed | cancelled | interrupted
degraded → queued（显式重试产生子 run 时，原 run 保持不可变）
```

### 8.2 独立质量状态

```text
processing_status  pending | running | completed | failed
coverage_status    pending | passed | degraded | failed
evidence_status    pending | passed | partial | failed | not_required
review_status      pending | passed | revision_required | failed
publication_status pending | rendering | rendered | partial | failed
sync_status        pending | syncing | synced | failed | skipped
```

总体完成门禁：

- processing、coverage、evidence、review 均 passed/completed；
- publication 至少 rendered；
- sync 可 failed，不阻断内容完成，但不得覆盖其他状态；
- 任一 coverage 硬指标失败时 run 必须 degraded/failed。

## 9. 16 节点 DAG 输入输出契约

| # | 节点 | 类型 | 主要输入 | 主要输出 | 失败与幂等规则 |
|---|---|---|---|---|---|
| 01 | `resolve_material_domain` | local | run/course/chapter/lesson/material_ids/transcript | `MaterialDomainSnapshot` | 归属错误 fail；同 run 重入返回相同 frozen domain |
| 02 | `normalize_materials` | local | frozen domain items | normalized items、字符/token/时间轴统计 | 每项必须终态；解析失败显式记账，禁止跳过 |
| 03 | `deduplicate_materials` | local | normalized items/spans | canonical map、duplicate reasons | 只合并重复，不删除账目；哈希算法版本化 |
| 04 | `build_source_map` | local | canonical + duplicate spans | 稳定 `SourceSpan[]` | Source ID 冲突 fail；同 domain version 结果稳定 |
| 05 | `plan_coverage` | local | source map + 模型上下文能力 | `CoveragePlan` | `unassigned_source_ids` 非空不得继续 |
| 06 | `segment_lesson` | local | source map + plan | ordered `LessonSegment[]` | 每个 canonical 非噪声 span 必须恰有一个 primary segment |
| 07 | `understand_segments` | LLM map | 每个 segment 全量 spans | `SegmentUnderstanding[]` | 单 segment 可模型重试；不得以丢弃 segment 降级成功 |
| 08 | `merge_lesson_understanding` | LLM reduce | 全部 segment understanding + source map 索引 | `LessonUnderstanding` | 必须消费全部成功 segment；冲突写 unresolved，不得静默覆盖 |
| 09 | `student_simulator` | LLM | LessonUnderstanding + mastery/prerequisite | `CognitiveMap` | 必须执行；无认知问题也返回空 items 和分析说明 |
| 10 | `compose_learning_content` | LLM | LessonUnderstanding + CognitiveMap + Source Map 摘要 | `LearningDocumentV2` | 动态 block；每个 block 有 selection_reason；禁止模型生成 quote |
| 11 | `bind_evidence` | local | claims + source_refs + source_spans | `BoundEvidence[]` | 无效 Source ID fail；AI 补充必须标记；写 V2 主表和旧表投影 |
| 12 | `coverage_audit` | local | domain/ledger/segments/understanding/claims | `MaterialCoverageReport` | 硬门禁；silent drop > 0 直接 degraded/failed |
| 13 | `critic` | LLM | document + bound evidence + coverage report | `QualityReview` | 不得改变证据；最多一次定向修订，修订后重跑 11–13 |
| 14 | `persist_note` | local | passed/degraded quality package | note + immutable revision + quality states | 事务写入；不满足门禁不得 confirmed |
| 15 | `render_document` | local | persisted structured document | HTML/PDF artifact | 保留确定性 renderer；HTML/PDF 使用同一 content hash |
| 16 | `obsidian_sync` | local | persisted note/artifact metadata | sync result | 同步失败仅改变 sync_status；绝不改 evidence/coverage/content 状态 |

### 9.1 节点公共信封

所有节点输出统一带：

```json
{
  "schema_version": "v6.1",
  "run_id": 602,
  "node": "student_simulator",
  "input_hash": "sha256:...",
  "output_hash": "sha256:...",
  "status": "success",
  "warnings": [],
  "payload": {}
}
```

### 9.2 LLM 节点规则

- 全部使用显式 contract 名：`v6/segment_understanding` 等。
- Prompt、Schema、模型 ID 和输入哈希进入 `model_calls`。
- 结构错误先同模型修复一次，再按模型管理页候选链切换。
- LLM 不得决定 source span 是否存在、覆盖率、quote 内容或最终质量门禁。
- 并行 `understand_segments` 受现有 Worker/Gateway 并发上限控制。

## 10. 分段与上下文预算算法

1. 从模型配置读取 context window 和 max output。
2. 预留 system、schema、输出和 10% 安全余量。
3. 若全部 canonical 非噪声 spans 可容纳，使用 `single_pass`。
4. 否则按来源顺序、时间轴和主题边界形成 segments。
5. 每个 span 必须分配到一个 primary segment；必要 overlap 只作 secondary，不重复计覆盖。
6. PPT 页与同期口头讲解可放入同一 segment，但必须保留各自 Source ID。
7. 禁止用相关性 top-k 决定单堂课哪些内容进入理解。
8. RAG 仅用于跨课、跨章、自由问答、错题回溯及复习召回。

## 11. Phase 1–6 补丁级任务书

### Phase 1 — Material Integrity

目标：任何材料都不能静默消失，先让覆盖可计算、可阻断。

补丁：

1. 新增 `0016_v6_material_domain.sql` 与 `0017_v6_source_spans_coverage.sql`。
2. 新增 `learning_engine/domain.py`、`normalize.py`、`deduplicate.py`、`source_map.py`、`coverage.py`。
3. 扩展 parser，使 transcript 有稳定时间 locator，PPT/PDF 保留页/slide 元数据。
4. 新增 Material Domain 冻结 API 与 Coverage Report 查询 API。
5. 在现有 lesson DAG 前插入 01–05；暂时仍可调用旧 composer，但低覆盖必须 degraded。
6. 前端听课流增加材料域、覆盖账本和未处理原因展示。
7. 新增只读 benchmark 脚本，复算历史三小时课堂指标。

验收：

- 域成员识别 100%；duplicate/noise/unsupported 均有 reason code。
- 人为漏掉一个 span 时测试必须得到 `degraded`，不能 completed。
- 正式数据库未被隔离测试修改。

主要文件：

```text
backend/migrations/0016_*.sql
backend/migrations/0017_*.sql
backend/app/learning_engine/{contracts,domain,normalize,deduplicate,source_map,coverage}.py
backend/app/material_parser.py
backend/app/main.py
frontend/src/components/LessonFlowPage.vue
frontend/src/api/endpoints.ts
backend/tests/test_v6_material_integrity.py
```

### Phase 2 — Full Lesson Understanding

目标：移除单堂课 top-k 选择，完整材料通过 single-pass 或 map/merge 理解。

补丁：

1. 新增 `0018_v6_learning_intermediates.sql`。
2. 实现节点 06–08 与契约 `LessonSegment / SegmentUnderstanding / LessonUnderstanding`。
3. 删除 V6 路径对 `retrieve_context_node`、`[:12]`、`top_k=8`、`transcript[:800]` 的依赖。
4. 保留旧 V5 lesson DAG 作为 feature flag 回退，不能与 V6 输出混写。
5. 增加 segment 并发、取消、重试、断点复用与输入哈希幂等。
6. Coverage Ledger 记录每个 span 被哪个 segment 处理。

验收：

- canonical 非噪声 span 全部分配且全部进入理解。
- 顺序覆盖整条转写时间轴和全部 PPT 页。
- 任一 segment 失败时整个理解不得伪装 completed。
- 使用合成长材料测试验证首、中、尾埋点均进入 LessonUnderstanding。

### Phase 3 — Cognitive Layer（已实施：迁移 0019）

目标：实现真正的 `student_simulator`。

补丁：

1. 实现节点 09 与 `CognitiveMap` schema/prompt。
2. 将章节备注、已有 mastery、已确认错题作为可选认知输入；不存在时显式为空。
3. 为八类 cognitive item 建立枚举、严重度和推荐处理方式。
4. 禁止 simulator 直接写最终笔记或新增课堂事实。
5. 前端增加内部“认知分析”审计面板，不默认塞入最终文档。

验收：

- 每次 V6 课堂运行 simulator 执行率 100%。
- 空问题课程返回合法空 CognitiveMap，不虚构 pitfall。
- 有明确提醒/跳步/易混埋点的 fixture 能产生对应 cognitive item 且绑定 KU/Source ID。

#### Phase 2/3 Final Closeout（已实施，无新迁移）

独立复现并修复的问题（回归测试：`backend/tests/test_v6_final_closeout.py`）:

| 编号 | 问题（已复现） | 修复 |
| --- | --- | --- |
| 1 (P0) | 旧链候选走 `lesson_id` 全局 RAG，同一课时的历史 run 会串课；且 `retrieve_context` 与 `build_source_map` 同层并发，inline 转写物化前读到空 Source Map | 候选改为**只取当前 frozen Material Domain 的 canonical span**（`_v6_domain_chunks`），禁用 lesson 级 RAG；`retrieve_context` 依赖改为 `build_source_map`；未进入候选窗口的内容显式记账 |
| 2 (P0) | `_material_excerpts` 只按 `source_id` 查询，而 Source ID 是**按 domain** 分配的，`T000001` 会把别课堂正文渲染进本堂课 prompt | 签名改为 `_material_excerpts(domain_id, source_ids)`，SQL 同时限定 domain + source_id、按域内 ordinal 排序；域外 ID 剔除并回报；`cognitive_item_sources.source_span_id` 只在本域解析 |
| 3 (P0) | 账本为每个 KU 无条件写 `consumed` + `batch_ordinal=1`，失败 batch 被伪造成成功；异常被吞，DAG 不会换模型 | 账本逐 batch 落真实序号/KU/模型/`input_hash`/终态（consumed/failed）；失败 batch 后重抛 `RetryableModelError`；`status=empty` 仅限「全成功且无 item」 |
| 4 (P1) | 生产 `plan_ku_batches(units)` 不传预算，每批重复全部 KU/Source/证据 | 预算由模型 `context_window` 派生（`_cognitive_input_budget`），批次按最终 messages 复算（`_verify_batch_budgets`），超预算即失败；材料块只提供本批 KU 引用的 Source |
| 5 (P1) | `knowledge_mastery` 读全表前 200 行（不限课程/章节/KU）；作业反馈查询引用了不存在的 `questions.lesson_id` 列而**永久静默失效** | mastery 按 course+chapter 过滤，表不存在记 `absent`、作用域未知记 `deferred`；作业反馈改走 `homeworks.lesson_id` |
| 6 (P1) | 同 run 复用只比 `input_hash` + 模型，忽略 prompt/schema 版本 | 同 run 复用补比 `prompt_version` / `schema_version`（分段算法与版本已在 `input_hash` 内） |
| 7 (P1) | `cognitive_maps.input_hash` 用输出 item 的 `stable_key` 计算（用输出算输入） | 改为由真实输入派生：LessonUnderstanding 的 input/content hash + KU 内容 + Source 正文哈希 + 章节备注 + 错题 + 作业反馈 + mastery + prompt/schema 版本 + 候选模型 + 批次计划 |

诚实的边界（不得表述为已完成）：

- 认知产物**不进入** HTML/PDF（`included_in_document=False`），正式笔记仍由旧链产出并标记为 legacy publication。
- `merge_lesson_understanding` 仍是**确定性并集**（1 层）；`merge_understanding` 契约与 prompt 保留但未启用。
- `real_notes_publish_enabled()` 恒为 `False`，`publication_mode()='v6_understanding_only_legacy_publication'`。
- 认知层并发只受 `V6_SEGMENT_CONCURRENCY` 约束，未接入全局网关配额。
- `knowledge_mastery` 表属 Phase 6；在此之前 mastery 一律记为 `absent`/`deferred`，绝不全局读取。

#### Phase 3 Final Micro-Closeout（已实施，无新迁移）

补齐认知层的**边界收紧**（回归测试：`backend/tests/test_v6_cognitive_boundary.py`）:

| 编号 | 问题（已复现） | 修复 |
| --- | --- | --- |
| P1-A | 生产代码恒调 `_cognitive_input_budget(None)`：候选模型配成 8192 上下文时，认知层仍按默认 32768 规划批次（实测预算 23347 vs 应为 1228），真实请求会溢出 | `_cognitive_input_budget(effective_model)` 按候选模型档案的 `context_window` 派生；审计返回 `model_profile_id`/`context_window`/`reserved_output`/`input_budget`/`context_window_source`；档案缺失或窗口为空才回落保守默认；模型 fallback 后 DAG 以新候选重跑整个节点，批次按新候选预算重新规划 |
| P1-B | 每批的 `allowed_source_ids` 是整个 domain，`material_clues` 重复全部 KU：单 KU 批次只读 `T000001` 却允许引用 `T000002/3`，并看到其他批次的 KU 内容 | `allowed_source_ids` 收紧为**本批 KU 实际引用**的集合；`material_clues` 与 KU 行只含本批 KU；节点对模型输出做**硬校验**（`source_refs` 必须是本批 allowed 的子集，越界即该 batch 失败并重抛，触发 fallback）；跨 batch 只在 `_normalize_items` 的稳定键归并处做 domain 级并集 |
| P1-C | `_material_excerpts` 先把请求截到 40 条再反推 `foreign`：同 domain 第 41 条以后的**合法** Source ID 被错报为域外 | 先对**全部**请求 ID 做域内校验；`foreign_source_ids` 只表示确实不存在/属于别的 domain；受预算限制未发送的来源记 `omitted_due_budget`（带 `max_spans_exceeded` / `char_budget_exceeded` 原因）；正文截断记 `excerpt_truncated` + 逐条 `sent_chars`/`total_chars`；返回结构含 `included_source_ids`，`included + omitted` 等于请求数（无静默丢弃） |
| P1-D | 全部 batch 失败时 `map.status` 写 `partial`，`CognitiveMapStatus.FAILED` 从未被生产代码使用，报告与实现不一致 | 状态矩阵对齐：全失败 → `failed`；部分成功 → `partial`；全成功且有 item → `succeeded`；全成功且无 item → `empty`；取消 → 不写半成品 map（只向上传播）。相关测试更名为 `test_all_models_failed_marks_failed_and_run_not_completed` 并断言 `failed` |

同时修掉的**静默失效**：`_collect_source_refs` 只扫 segment understanding 的输出形状
（`knowledge_units`/`definitions`/…），**没有覆盖 `lesson/student_simulator` 的
`items[].source_refs`** —— 认知项的 source_refs 实际上从未被校验过，越界只会被静默
剔除。现已覆盖 `items`，越界引用成为显式失败点。

### Phase 4 — Evidence V2（已实施：迁移 0020）

目标：模型只选 Source ID，程序确定性绑定真实原文。

补丁：

1. 新增 `0020_v6_evidence_v2.sql`。
2. 实现节点 11 和 `content_claims / claim_sources` 持久化。
3. 修改 Composer schema，删除模型填写 quote 的责任。
4. Source ID → span → text/locator/page/timestamp 完全本地解析。
5. 写入旧 `evidence_links` 兼容投影；V6 审计读取新表。
6. 区分课堂事实、课堂重述和模型教学补充，并更新 Renderer 标识。

验收：

- 无效 Source ID 为 0；故意伪造 ID 必须阻断。
- bound quote 全部来自数据库，不来自模型输出。
- 关键课堂 claim 证据率 100%。
- `ai_explanation` 在 HTML/PDF 有稳定、可见标识。

#### Phase 4 实施记录与诚实边界

实施前独立检查发现的**真实旧 Evidence 缺陷**（全部在本次修掉）:

| 缺陷 | 说明 |
| --- | --- |
| 旧 `evidence_verifier_node` 用**模糊匹配**验证模型给的 quote | `evidence._fuzzy_contains`（Unicode/数学符号归一 + OCR 混淆对）判「模型 quote 是否出现在 chunk 里」。它证明的是「模型抄得像」，不是「这句话来自数据库」；且 quote 本身就由模型生成，与「模型不得生成 exact quote」直接冲突 |
| `evidence_links` 无法追溯到 claim | 表结构是 `owner_type/owner_id/chunk_id/quote/...`，没有 `content_claim_id`；owner 只有 note/review |
| 笔记证据为空时算「无证据要求」 | `audit_evidence_block` 对空证据返回 `passed=True` —— 空证据被当成通过 |
| V6 侧完全没有 claim 概念 | LessonUnderstanding / CognitiveMap 的 `source_refs` 只是字符串数组，没有任何「主张 → 来源」绑定与门禁 |
| 渲染层无法区分模型补充 | `AllowedBlockType` 没有 `ai_explanation`，HTML/PDF 只会按颜色/标题区分，无稳定机器标识 |

本阶段落地内容:

1. **迁移 0020**：`content_claims`（19 列，`UNIQUE(run_id, claim_key)`，`claim_type`/`importance`/`evidence_status` 三组 CHECK，FK 到 run/domain/lesson_understanding/cognitive_map/notes 全 `ON DELETE CASCADE`/`SET NULL`）+ `claim_sources`（19 列，`UNIQUE(claim_id, source_id, relation)`，`relation`/`binding_status`/`binding_method`/`presented_to_producer` CHECK，FK 到 claims/spans）；8 + 5 个索引。
2. **契约**：`ClaimType` / `ClaimImportance` / `ClaimEvidenceStatus` / `ClaimSourceRelation` / `ClaimBindingStatus` / `ClaimBindingMethod` / `ClaimDraft` / `BoundClaimSource` / `BoundEvidence` / `EvidenceReport`，全部 `extra="forbid"`，枚举与 DB CHECK 一一对应。
3. **确定性 shadow projection**（`project_claims`）：从 LessonUnderstanding（KU summary、relations、definitions/formulas/derivations/examples）与 CognitiveMap（按 `origin` 映射类型）生成 ClaimDraft；`claim_key = producer:type:hash(projection, producer, type, origin_ref, text)`，同输入重跑稳定。**不调模型**、**不从 legacy note 反猜**、**不做模糊检索**。
4. **确定性 Binder**（`bind_claims`）：格式校验 → 限定 `domain_id + source_id` 查询 → `bound_quote` 直取 `source_spans.text` → locator/时间戳/页码/source_kind 全部从库复制 → `quote_hash`/`source_text_hash` 程序计算 → 逐条终态 `bound/invalid/missing/not_required`。
5. **模型可见证据边界**（Phase 3 遗留 P1 的非阻断项）：`cognition.render_cognitive_messages` 的 `allowed_source_ids` 收紧为 `included_source_ids ∩` 本批引用；`presented_source_ids()` 只认 `status=consumed` 批次里真正发送的来源。**「数据库里存在」与「生成该 claim 时模型可见」是两件事** —— `omitted_due_budget` 的来源被引用即 `unread_source_refs`，门禁失败。
6. **门禁**：`invalid_source_refs>0` / `cross_domain_refs>0` / `unbound_source_refs>0` / `unread_source_refs>0` / `failed_claims>0` / `critical_claim_evidence_rate<1.0` → `failed`。没有 required claim 时证据率记 1.0，但必须同时记录 `critical_evidence_denominator=0`，不得伪装成「验证了很多证据」。
7. **DAG**：`merge_understanding → student_simulator → bind_evidence → coverage_audit`；`bind_evidence` 是 **local 节点（不调模型）**。`coverage_audit` 展示 evidence gate 与全部计数，**Evidence gate 失败时整体 gate 不得为 passed**。legacy `evidence` 节点保留。
8. **legacy 兼容投影**：`owner_type='content_claim'` + `owner_id=content_claims.id` 保证可追溯；只写本 run、先清后写、历史 legacy 行不动；Evidence V2 的 API/审计**不反向依赖** `evidence_links`。
9. **API**：`GET /api/runs/{run_id}/evidence-v2`（只读；不返回 reasoning / prompt / 密钥 / 绝对路径）。
10. **前端**：`EvidenceV2Panel.vue`（gate、证据率含分母说明、三类 claim 分组、invalid/cross/unbound/unread 计数、点击 Source ID 查看数据库原文与 locator、`ai_explanation` 显示「模型教学补充（非课堂原话）」）；全部按钮 `type="button"`；三套主题复用既有设计 token。
11. **Renderer**：新增 `ai_explanation` block 类型，输出 `data-claim-type="ai_explanation"` + 可见文字徽标；HTML 与 PDF 来自同一结构化输入与 content hash。

诚实的边界（**不得**表述为已完成）:

- **Composer V2 未实施**：claim 来自确定性 shadow projection，不是正式笔记块；`note_id` / `block_id` 仍为 NULL。
- **正式 V6 HTML/PDF 出版未接管**：`real_notes_publish_enabled()` 恒为 `False`，`publication_mode()='v6_understanding_only_legacy_publication'`。
- **Binder 不做语义评审**：只验证「引用存在 / 归属正确 / quote 来自数据库 / 类型与证据要求一致」；`classroom_paraphrase` 只证明绑定存在，**语义等价属 Phase 5 Critic**。
- 多维 Quality States、Note revision、Obsidian 状态拆分、Learning Loop 属 Phase 5/6。
- 旧 `evidence_verifier_node`（模糊 quote 匹配）仍在链上以保持兼容，但**不是** V6 证据口径；两者结论不得互相覆盖。

#### Phase 4 关键不变量（回归测试 `backend/tests/test_v6_evidence_v2.py`）

模型**绝不**生成 exact quote：`bound_quote` 只可能由程序从 `source_spans.text` 复制，
`quote_hash` / `source_text_hash` 由程序计算；模型输出里出现 `quote` 字段一律忽略。
查询**必须**同时限定 `domain_id + source_id`，禁止仅按 `source_id` 全库命中。

| 注入场景 | 期望 |
| --- | --- |
| 伪造 Source ID（本 domain 不存在） | `missing` → `failed` → gate failed |
| 格式非法 Source ID | `invalid` → `failed`（**不静默剔除**） |
| 跨 domain 引用（同 ID 存在于别的 domain） | `invalid` + `cross_domain_refs` → gate failed |
| `omitted_due_budget` 来源（库里有、模型没读） | `unread_source_refs` → gate failed |
| `classroom_fact` 无来源 | `failed` → gate failed |
| `classroom_paraphrase` 有合法可见来源 | `passed`（只证明绑定，不声称语义等价） |
| `ai_explanation` 无来源 | `not_required`，不计入证据率分母，有可见标识 |
| `ai_explanation` 伪装 `classroom_fact` | `failed`（类型字段说了算） |
| critical 缺证据 | `rate<1.0` → gate failed |
| 全部 critical 有证据 | `rate=1.0` → gate passed |
| 无 required claim | `rate=1.0` 但 `denominator=0` 且显式记录 |
| 同 run 重复 bind | claims/sources/投影行数不变 |
| 新 run | 不改动历史 run 的 claims/sources |
| 删除 run | claims/sources 级联清理，无孤儿行 |
| 事务中途失败/取消 | 整体回滚，不留半套 |

#### Phase 4.1 — 逐 claim、逐生产者可见性（已实施：迁移 0021）

**独立复现的 P1 缺陷**（修复前为真）:

Phase 4 的 Binder 用 ``presented_source_ids(domain_id, run_id)`` —— 把该 run 内
**所有认知批次**的已呈现来源合并成一个集合 —— 来判断 ``presented_to_producer``。
于是：

* batch 1 只读过 ``T000001``、batch 2 只读过 ``T000002`` 时，把 batch 1 产出的
  claim 篡改为引用 ``T000002``，会被误判为「模型已读过」（实测
  ``status=passed, presented=True``）；
* LessonUnderstanding 投影出的 claim 也被**认知层**的来源集合判定，而不是按它
  自己 segment 模型调用的输入判定。

这违反 V6 原则：**「数据库中存在」≠「该 producer 实际读过」，
「其他 batch 读过」≠「生成该 claim 的 batch 读过」**。

**修复**:

1. **迁移 0021**：``content_claims.producer_provenance_json``（逐 claim 的生产
   可见性）、``claim_sources.visibility`` / ``visible_invocations_json``（逐来源
   终态与真实 invocation），并对历史行做保守回填（``presented_to_producer=1`` →
   ``visible``，其余 → ``not_visible``/``unknown``；provenance 一律 ``{}`` →
   不可用 → failed）。
2. **逐 claim provenance**（契约 ``ClaimProvenance``）：
   ``producer_stage / producer_node / producer_kind / invocation_refs /
   visible_source_ids / visibility_basis / provenance_version /
   per_source_invocations``。只存 run/domain/模型级集合是**不允许**的。
3. **CognitiveMap 逐 item 溯源**：收集模型输出时为每个 item 记录产出它的那次
   调用（batch ordinal + input_hash + allowed/visible 来源）；同 ``stable_key``
   合并时逐来源保留 ``per_source_invocations``，**绝不**扩大为所有批次的并集。
   越界引用不再静默剔除，而是保留在 ``source_refs`` 并记入
   ``out_of_batch_source_refs`` 以便审计。
4. **LessonUnderstanding 逐 segment 溯源**：按 Phase 2 已持久化的
   ``segment_source_spans``（``visibility_basis=segment_input_ledger``）判定，
   **与认知层完全无关**；overlap 不算可见（只取 primary）。
5. **Binder 重写**：``presented_source_ids()`` 停用为通过依据（仅留诊断）。
   逐来源判定：本 domain 不存在 → missing/invalid；属于其他 domain →
   invalid/cross-domain；存在但**不在该 claim 的 ``visible_source_ids``** →
   ``presented_to_producer=False`` + ``source_not_presented_to_this_claim_producer``
   → claim failed；provenance 缺失/损坏 → ``producer_visibility_missing`` →
   claim failed。新增门禁指标 ``not_in_claim_producer_refs``、
   ``claims_without_provenance``。
6. **API / 前端**：只暴露安全 provenance 摘要（producer_node / invocation_refs /
   visible_source_ids / visibility_basis / available），前端明确显示
   「该来源**未进入生成此主张的模型输入**」，而不是笼统的「未验证」。

回归测试：``backend/tests/test_v6_evidence_provenance.py``（A–F 六类）。判别性结论
（修复后全部为 False / 符合预期）:

| 判别项 | 含义 | 修复后 |
| --- | --- | --- |
| ``A_cross_batch_presented`` | batch1 的 claim 引用 batch2 来源时被误判为已读 | False |
| ``B_lu_uses_cognitive_visibility`` | LU claim 借用认知层可见性 | False |
| ``C_same_batch_failed`` | 正常同批来源被误判失败（误伤） | False |
| ``D_merge_widened`` | 跨批合并扩大可见性 | False |
| ``E_fallback`` / ``E_corrupt_fallback`` | provenance 缺失/损坏时回退到全局集合 | False |

**Phase 4 只有在 Phase 4.1 修复通过后才算正式关闭。** 未完成项仍归 Phase 5/6
（Composer V2、正式 V6 出版、Quality States、Critic V2、Obsidian 状态拆分、
Learning Loop）。

### Phase 5 — Note Composer V2 与状态拆分

目标：由理解层和认知层驱动动态内容块，并彻底拆开质量/出版/同步状态。

补丁：

1. 新增 `0022_v6_quality_states.sql`。
2. 实现节点 10、12、13、14 的 V2 版本。
3. `selection_reason` 必须引用 KU 或 CognitiveMap item。
4. `pitfall/notice/key_point/derivation` 仅在前置分析支持时生成。
5. 修改 Obsidian 同步，禁止再写 `notes.status='synced'`。
6. 更新运行状态机、Worker、API 和前端，支持 `degraded` 与多维质量状态。
7. Renderer 使用同一个 structured JSON 生成 HTML/PDF，保留 content hash 一致性校验。

验收：

- 未出现的内容类型不会为填版面而生成。
- 低覆盖、证据失败、评审失败分别可见，不相互覆盖。
- Obsidian 失败不改变 note 的 evidence/coverage 状态。
- HTML/PDF 结构与内容哈希一致。

### Phase 6 — Learning Loop

目标：知识单元与作业、错题、复习状态形成长期闭环。

补丁：

1. 新增 `0023_v6_learning_loop.sql`。
2. 作业题、确认错题和 review attempt 绑定 Knowledge Unit。
3. 更新 mastery 时保留来源、权重和历史证据。
4. 课堂/章节复习 Composer 接收 mastery 分布，调整解释深度与优先级。
5. 错题解释使用 RAG 回溯课堂 Source ID，并显示老师原始讲解位置。
6. 章节与学期复习继续使用 embedding/rerank；不得回写或篡改课堂源。

验收：

- 一个确认错题可追到 KU，再追到课堂 SourceSpan。
- mastery 变化会改变后续复习排序，且可解释原因。
- 删除/拒绝错题不会留下悬空权重。
- 跨材料 RAG 与单堂课全量理解路径有明确测试边界。

## 12. API 与前端最小增量

建议 API：

```text
GET /api/runs/{id}/material-domain
GET /api/runs/{id}/coverage
GET /api/runs/{id}/understanding
GET /api/runs/{id}/cognitive-map
GET /api/runs/{id}/claims
GET /api/source-spans/{source_id}
POST /api/runs/{id}/retry-from/{node}
```

前端新增而不推翻现有布局：

- 听课流：Material Domain、覆盖率、时间轴、PPT 页覆盖、silent drop。
- 运行详情：16 节点、实际网关模型名、输入/输出哈希、降级原因。
- 笔记详情：block 的选择依据、claim 类型、Source ID 与原文定位。
- 模型补充统一视觉标识。

## 13. Benchmark 与验收矩阵

### 13.1 工程回归

- 后端隔离测试全通过，真实网关调用 0，正式库哈希/mtime/length 不变。
- 前端 `vue-tsc` 和 Vite build 通过。
- migration fresh install、v15→v21 upgrade、备份恢复均通过。
- 取消、重试、进程恢复、并发 segment 均有测试。

### 13.2 内容回归

建立三类 fixture：

1. 小课：全部内容单次可容纳。
2. 长课：强制分成 3 个以上 segment，在首/中/尾布置可验证埋点。
3. 多材料课：转写 + PPT + 重复讲义 + 噪声，验证去重与显式忽略。

长课 Benchmark 必须报告：

```text
raw chars/tokens
domain items / unique items
source spans / canonical spans
processed spans
timeline range and gaps
PPT pages processed
knowledge units
cognitive items
claims / valid refs
silent drops
final document chars
HTML/PDF content hash
```

历史三小时课堂只在用户明确授权 live benchmark 时调用真实模型；普通 CI 使用结构相同的脱敏 fixture。

## 14. Feature Flag 与兼容策略

```text
V6_LEARNING_ENGINE=off   # 默认继续 V5，迁移期
V6_LEARNING_ENGINE=shadow # V6 运行但不出版，用于对比覆盖
V6_LEARNING_ENGINE=on    # V6 成为课堂主链
```

- Shadow 输出写 V6 新表，不覆盖现有 notes/artifacts。
- V5 与 V6 run 使用 `engine_version` 区分。
- 只有 Phase 1–5 门禁全部通过后才允许默认 `on`。
- 回退只切 feature flag，不回滚数据库迁移。

## 15. 首个可执行里程碑

第一个开发里程碑不是生成更漂亮的笔记，而是：

> 给任意一次课堂运行冻结 Material Domain，建立完整 Source Map 与 Coverage Ledger，并在故意遗漏一个 span 时阻止运行显示 completed。

完成这个里程碑，才进入 Full Lesson Understanding。这样可以先证明 V6 已消灭“静默丢掉 95% 仍显示成功”的根因，再投入模型 Prompt 与认知层建设。

## 16. Phase 5/6 Final Closeout（2026-09-13）

Phase 5 与 Phase 6 已在 V5 基础设施上落地：

- `V6_LEARNING_ENGINE=on` 时，正式课堂产物只经过 Composer V2，不再由 legacy `note_writer` 旁路生成。
- Composer 使用 `LessonUnderstanding + CognitiveMap + Evidence V2` 动态选择内容块；每个课堂内容块保存 `claim_key`、`selection_reason` 与确定性绑定的 Source ID。
- 新增互相独立的 processing / coverage / evidence / review / publication / sync 状态；Obsidian 同步不再覆盖内容质量状态。
- 同一份结构化 JSON 同时驱动 HTML 与 PDF，并以 revision 与 content hash 留存。
- Knowledge Unit 已规范化，并与课堂 SourceSpan、作业题、确认错题、复习作答及 mastery 历史联动。
- 错题可以沿 `error -> Knowledge Unit -> SourceSpan` 返回课堂转写时间位置或 PPT 页；拒绝反馈会停用关联并重新计算掌握度。
- 章节复习会读取按作用域过滤的 mastery，优先处理低掌握知识单元；跨材料检索继续由既有 RAG 负责，不改变原始 Source Map。
- 前端听课流新增 V6 六维质量状态与 revision/content hash 展示；新增只读质量、掌握度和错题学习追踪 API。

数据库迁移：`0022_v6_quality_states.sql`、`0023_v6_learning_loop.sql`。正式数据库不在测试中迁移；启用生产版本前应先做正式备份，再由正常启动流程应用迁移。

