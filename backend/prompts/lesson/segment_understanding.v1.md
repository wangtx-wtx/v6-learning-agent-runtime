# V6 单段课堂理解（lesson/segment_understanding v1）

你正在对**一堂课的一个片段**做结构化理解。这段内容是课堂原始材料的连续片段，
按课堂原始顺序给出。

## 严格规则

1. 只使用下方提供的材料片段。**不得**补充材料之外的课堂事实、公式或例题。
2. `source_refs` 只能使用下方以 `SRC=` 标注的 Source ID，**不得编造 ID**。
   引用不存在的 Source ID 会导致该片段理解直接失败。
3. **不要复制原文引用（exact quote）**。只输出结构化理解；原文由系统按
   Source ID 自行定位。summary 用你自己的话概括。
4. 如果某个知识点由多个 Source ID 共同支持，把它们都列在 `source_refs` 中。
5. 材料里没有出现的内容类型（定义/公式/推导/例题）请留空数组，**不要为了
   填满字段而编造**。
6. 材料中若有前后矛盾或明显未讲清的地方，写入 `unresolved_points`。
7. `teacher_emphasis` 表示该片段整体被老师强调的程度（0~1）。

## 输出 JSON Schema

只输出 JSON，不要输出解释文字：

```json
{
  "topics": ["主题1", "主题2"],
  "knowledge_units": [
    {
      "temp_id": "SU-01",
      "topic": "知识点主题",
      "kind": "definition|concept|derivation|example|procedure|comparison|summary",
      "summary": "用你自己的话概括",
      "source_refs": ["T000012", "P0017.01"],
      "teacher_emphasis": 0.0,
      "relations": ["supports:SU-02", "requires:SU-01"]
    }
  ],
  "definitions": [{"term": "术语", "meaning": "含义", "source_refs": ["T000012"]}],
  "formulas": [{"expression": "公式", "meaning": "物理含义", "source_refs": ["T000012"]}],
  "derivations": [{"goal": "推导目标", "steps": ["步骤1", "步骤2"], "source_refs": ["T000012"]}],
  "examples": [{"prompt": "题目", "solution": "解法", "source_refs": ["T000012"]}],
  "teacher_emphasis": 0.0,
  "unresolved_points": ["未讲清的地方"],
  "source_refs": ["T000012", "P0017.01"]
}
```

## 片段信息

片段序号：{{segment_ordinal}} / {{segment_total}}
片段来源（Source ID 范围）：{{segment_range}}
片段 token 估算：{{segment_tokens}}

## 材料片段

**材料正文在随后的 user 消息中提供**（只提供一次，避免重复占用上下文）。

## 本片段允许使用的 Source ID（唯一权威清单）

允许使用的 Source ID: {{allowed_source_ids}}

不得使用此清单以外的任何 Source ID。
