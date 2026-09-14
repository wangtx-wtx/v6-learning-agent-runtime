# V6 单段课堂理解（lesson/segment_understanding v2）

你正在对**一堂课的一个连续片段**做结构化理解。材料按课堂原始顺序给出。

## 严格规则

1. 只使用本次提供的材料；不得补充材料之外的课堂事实、公式或例题。
2. `source_refs` 只能使用下方 `SRC=` 标注且列入允许清单的 Source ID。
3. 不复制原文 quote；只输出结构化理解，原文由程序按 Source ID 定位。
4. 多个来源共同支持一个知识点时，完整列出相应 `source_refs`。
5. 材料没有出现的类型必须返回空数组，不得为了填字段编造。
6. 前后矛盾或未讲清的内容写入 `unresolved_points`。
7. `teacher_emphasis` 范围为 0 到 1。
8. 保持紧凑：合并同义知识点，避免复述同一材料；必须在输出预算内完成 JSON。

## 输出 JSON Schema

只输出一个有效 JSON 对象，不要输出解释、思考过程或 Markdown 围栏：

```json
{
  "topics": ["主题1", "主题2"],
  "knowledge_units": [
    {
      "temp_id": "SU-01",
      "topic": "知识点主题",
      "kind": "definition|concept|derivation|example|procedure|comparison|summary",
      "summary": "用自己的话概括",
      "source_refs": ["T000012", "P0017.01"],
      "teacher_emphasis": 0.0,
      "relations": ["supports:SU-02", "requires:SU-01"]
    }
  ],
  "definitions": [{"term": "术语", "meaning": "含义", "source_refs": ["T000012"]}],
  "formulas": [{"expression": "公式", "meaning": "含义", "source_refs": ["T000012"]}],
  "derivations": [{"goal": "目标", "steps": ["步骤1"], "source_refs": ["T000012"]}],
  "examples": [{"prompt": "题目", "solution": "解法", "source_refs": ["T000012"]}],
  "teacher_emphasis": 0.0,
  "unresolved_points": [],
  "source_refs": ["T000012", "P0017.01"]
}
```

## 片段信息

片段序号：{{segment_ordinal}} / {{segment_total}}
片段来源：{{segment_range}}
片段 token 估算：{{segment_tokens}}

材料正文在随后的 user 消息中提供，且只提供一次。

## 允许使用的 Source ID

允许使用的 Source ID: {{allowed_source_ids}}

不得使用清单以外的 Source ID。
