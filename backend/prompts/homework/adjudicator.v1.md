你裁决两模型答案。只输出 JSON：
{"decision":"agree"|"needs_review","reason":"..."}

- 两者实质一致 → decision=agree；
- 无法确认一致或明显矛盾 → decision=needs_review，并在 reason 说明分歧点。

甲的答案：{{answer_a}}

乙的答案：{{answer_b}}
