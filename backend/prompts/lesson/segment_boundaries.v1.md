# V6 分段边界规划（lesson/segment_boundaries v1）

你在为一堂课的**全部材料**规划分段边界。材料已按课堂原始顺序编号，每条形如
`[序号] (定位信息)` 后跟**完整原文**。

你的任务只有一件：**找出这堂课讲了哪几件事（知识点），并给出建议的段边界。**

## 硬约束（违反会被程序拒绝，不是建议）

1. **每个知识点必须完整落在一段内**，不得跨段。
2. **每段必须至少包含 1 个完整知识点**。不要产出小于一个知识点的段。
3. `knowledge_units` 的序号区间必须**从 1 开始、到最后一个序号结束**，
   **不重叠、不跳号**（`后一个.start = 前一个.end + 1`）。
4. `breaks` 表示「在此序号**之后**切段」，必须**严格递增**、不得重复。
5. `breaks` 的每个值都必须是**某个知识点的 `end_ordinal`**。
6. `breaks` 不得等于最后一个序号（那是空段）。
7. **只输出数字区间**。不要输出原文、不要改写内容、不要给出摘要。

## 尺寸要求

每段建议不超过约 **{{max_segment_tokens}} token**（大致相当于
**{{max_segment_chars}} 个汉字**）。请按字数估计，务必不要让某一段明显过大。

若某个知识点**本身就超过这个尺寸**，允许该段超出，但必须在 `notes` 里说明。
不要为了压尺寸而把一个知识点拆成两段。

## 粒度要求

- 每个知识点至少覆盖 **{{min_spans_per_ku}} 个序号**，不要切得过碎。
- 不要把所有材料归成一个知识点（那等于没有分段）。
- 也不要切成几十上百个小段。

## 输出 JSON Schema

只输出一个有效 JSON 对象，不要输出解释、思考过程或 Markdown 围栏：

```json
{
  "knowledge_units": [
    {"name": "知识点名称", "kind": "concept", "start_ordinal": 1, "end_ordinal": 24},
    {"name": "知识点名称", "kind": "derivation", "start_ordinal": 25, "end_ordinal": 41}
  ],
  "breaks": [24, 41],
  "notes": ["若有知识点本身超尺寸，在此说明"]
}
```

`kind` 取值参考：`definition` / `concept` / `derivation` / `example` /
`procedure` / `comparison` / `summary`。

## 材料编号表

共 {{span_count}} 条，总 token 估算 {{total_tokens}}。

{{span_index}}

---

**再次强调**：知识点区间从 1 开始到最后一个序号结束、不重叠不跳号；
每个知识点完整落在一段内；每段至少含 1 个完整知识点；
`breaks` 严格递增且是知识点结束位置；每段尽量不超过约 {{max_segment_chars}} 个汉字。
