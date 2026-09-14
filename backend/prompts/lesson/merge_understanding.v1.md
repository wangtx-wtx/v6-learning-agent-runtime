# V6 分层合并课堂理解（lesson/merge_understanding v1）

你正在把**同一堂课**的若干片段理解结果合并为一个全局理解。

## 严格规则

1. 你只能使用下方给出的片段理解结果。**不得**新增任何片段结果中没有、
   也没有 Source ID 支持的课堂事实、公式或例题。
2. 合并后的每个知识点必须保留其原始 `source_refs`（Source ID 并集），
   不得丢弃、不得替换为编造的 ID。
3. 同一知识点出现在多个片段时合并为一条，`relations` 取并集。
4. 片段之间若有**互相矛盾**的表述，必须写入 `unresolved_conflicts`，
   **禁止静默覆盖或二选一**。
5. `topics` 按课堂原始顺序给出全局主题结构。

## 输出 JSON Schema

只输出 JSON：

```json
{
  "topics": ["全局主题1", "全局主题2"],
  "knowledge_units": [
    {
      "temp_id": "KU-001",
      "topic": "知识点主题",
      "kind": "concept",
      "summary": "合并后的概括",
      "source_refs": ["T000012", "P0017.01"],
      "teacher_emphasis": 0.0,
      "relations": ["requires:KU-002"]
    }
  ],
  "unresolved_conflicts": ["片段1说X，片段3说Y，需人工确认"],
  "teacher_emphasis": 0.0
}
```

## 待合并的片段理解（共 {{segment_total}} 段，本批 {{batch_size}} 段）

{{segment_blocks}}

## 允许使用的 Source ID（全集）

{{allowed_source_ids}}
