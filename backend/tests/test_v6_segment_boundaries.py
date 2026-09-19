"""节点 05.5 分段边界规划验收测试。

覆盖 ``boundaries.validate_boundaries`` 的 6 条硬约束（H1-H6）、4 条防偷懒
下限（L1-L4）、1 项观测（O1），以及 ``render_span_index`` /
``compute_boundary_input_hash`` 的确定性。

全部为纯函数测试，不触碰数据库。

运行：python -X utf8 backend/tools/run_tests_isolated.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def make_spans(n: int, tokens: int = 100) -> list[dict]:
    """构造 n 个 canonical span（ordinal 从 1 开始）。"""
    return [
        {
            "ordinal": i,
            "source_id": f"T{i:06d}",
            "token_count": tokens,
            "text": f"第{i}条课堂内容",
            "locator": f"cue:{i}",
            "normalized_text_hash": f"sha256:h{i}",
        }
        for i in range(1, n + 1)
    ]


def ku(start: int, end: int, name: str = "知识点") -> dict:
    return {"name": name, "kind": "concept",
            "start_ordinal": start, "end_ordinal": end}


class BoundaryCase(unittest.TestCase):
    def validate(self, data: dict, spans: list[dict], limit: int = 10_000):
        from app.learning_engine.boundaries import validate_boundaries
        return validate_boundaries(data, spans, max_segment_tokens=limit)

    @staticmethod
    def has(items: list[str], prefix: str) -> bool:
        return any(x.startswith(prefix) for x in items)


# ---------------------------------------------------------------------------
# 硬约束 H1-H6：违反 ⇒ 建议不可用（回退结构边界）
# ---------------------------------------------------------------------------
class TestHardConstraints(BoundaryCase):
    def test_h1_reversed_range_is_rejected(self):
        v = self.validate({"knowledge_units": [ku(10, 5)], "breaks": []}, make_spans(20))
        self.assertFalse(v.ok)
        self.assertTrue(self.has(v.hard, "H1"))
        self.assertEqual(v.status, "rejected")

    def test_h0_empty_knowledge_units_is_rejected(self):
        v = self.validate({"knowledge_units": [], "breaks": []}, make_spans(20))
        self.assertTrue(self.has(v.hard, "H0"))

    def test_h3_missing_head_is_rejected(self):
        """起点不是 1 ⇒ 前面有 span 落不进任何段 ⇒ unassigned ⇒ gate failed。"""
        v = self.validate({"knowledge_units": [ku(2, 20)], "breaks": []}, make_spans(20))
        self.assertTrue(self.has(v.hard, "H3"))

    def test_h3_missing_tail_is_rejected(self):
        v = self.validate({"knowledge_units": [ku(1, 19)], "breaks": []}, make_spans(20))
        self.assertTrue(self.has(v.hard, "H3"))

    def test_h2_gap_between_kus_is_rejected(self):
        v = self.validate(
            {"knowledge_units": [ku(1, 10), ku(12, 20)], "breaks": []}, make_spans(20))
        self.assertTrue(self.has(v.hard, "H2/H3"))

    def test_h2_overlap_is_rejected(self):
        v = self.validate(
            {"knowledge_units": [ku(1, 12), ku(10, 20)], "breaks": []}, make_spans(20))
        self.assertTrue(self.has(v.hard, "H2/H3"))

    def test_h4_break_not_at_ku_end_is_rejected(self):
        v = self.validate(
            {"knowledge_units": [ku(1, 10), ku(11, 20)], "breaks": [5]}, make_spans(20))
        self.assertTrue(self.has(v.hard, "H4"))

    def test_h4_duplicate_break_is_rejected(self):
        v = self.validate(
            {"knowledge_units": [ku(1, 10), ku(11, 20)], "breaks": [10, 10]},
            make_spans(20))
        self.assertTrue(self.has(v.hard, "H4"))

    def test_h5_break_at_tail_is_rejected(self):
        v = self.validate({"knowledge_units": [ku(1, 20)], "breaks": [20]}, make_spans(20))
        self.assertTrue(self.has(v.hard, "H5"))

    def test_h6_ku_crossing_segment_is_rejected(self):
        """切点落在知识点内部 ⇒ 知识点被腰斩。"""
        v = self.validate(
            {"knowledge_units": [ku(1, 10), ku(11, 20)], "breaks": [5]}, make_spans(20))
        self.assertTrue(self.has(v.hard, "H6"))


# ---------------------------------------------------------------------------
# 防偷懒下限 L1-L4：合法但无用 ⇒ low_quality（仍回退结构边界）
# ---------------------------------------------------------------------------
class TestSoftConstraints(BoundaryCase):
    def test_l1_tiny_ku_is_low_quality(self):
        v = self.validate(
            {"knowledge_units": [ku(1, 2), ku(3, 20)], "breaks": []}, make_spans(20))
        self.assertFalse(v.hard, "格式本身合法")
        self.assertTrue(self.has(v.soft, "L1"))
        self.assertEqual(v.status, "low_quality")

    def test_l2_too_many_segments_is_low_quality(self):
        """21 个 span、每个知识点 3 个 ⇒ 7 段 > int(21 × 0.3) = 6。"""
        spans = make_spans(21)
        kus = [ku(i, i + 2) for i in range(1, 22, 3)]
        v = self.validate(
            {"knowledge_units": kus, "breaks": [3, 6, 9, 12, 15, 18]}, spans)
        self.assertTrue(self.has(v.soft, "L2"))

    def test_l3_oversized_but_no_break_is_low_quality(self):
        """关键防线：格式完全合法，却等价于不分段（中间迷失 + 审计失效回归）。"""
        spans = make_spans(20, tokens=1000)          # 总 20000 token
        v = self.validate({"knowledge_units": [ku(1, 20)], "breaks": []},
                          spans, limit=10_000)
        self.assertFalse(v.hard, "H1-H6 全都拦不住这种形态")
        self.assertTrue(self.has(v.soft, "L3"))
        self.assertEqual(v.status, "low_quality")

    def test_l4_overweight_segment_with_multiple_kus_is_low_quality(self):
        """段超上界但段内还有多个知识点 ⇒ 本可以再切 ⇒ 退回重画。"""
        spans = make_spans(20, tokens=2000)
        kus = [ku(1, 5), ku(6, 10), ku(11, 15), ku(16, 20)]
        v = self.validate({"knowledge_units": kus, "breaks": [10]},
                          spans, limit=10_000)
        self.assertTrue(self.has(v.soft, "L4"))
        self.assertFalse(self.has(v.soft, "L3"), "已给切点，不该触发 L3")


# ---------------------------------------------------------------------------
# 观测 O1：超大知识点（不拒绝、不降级）
# ---------------------------------------------------------------------------
class TestOversizedObservation(BoundaryCase):
    def test_o1_single_oversized_ku_is_observation_only(self):
        """段重且段内只有一个知识点 ⇒ 该知识点自身超重 ⇒ 无解，只观测。"""
        spans = make_spans(20, tokens=1000)
        v = self.validate({"knowledge_units": [ku(1, 20)], "breaks": []},
                          spans, limit=10_000)
        self.assertEqual(len(v.oversized_ku), 1)
        self.assertEqual(v.oversized_ku[0]["tokens"], 20_000)
        self.assertFalse(self.has(v.soft, "L4"), "单知识点超重不是可修复项")

    def test_o1_absent_when_ku_fits(self):
        spans = make_spans(20, tokens=10)
        v = self.validate({"knowledge_units": [ku(1, 20)], "breaks": []},
                          spans, limit=10_000)
        self.assertEqual(v.oversized_ku, [])


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------
class TestValidPlan(BoundaryCase):
    def test_valid_plan_succeeds(self):
        spans = make_spans(20)
        v = self.validate(
            {"knowledge_units": [ku(1, 10), ku(11, 20)], "breaks": [10]}, spans)
        self.assertTrue(v.ok, f"hard={v.hard} soft={v.soft}")
        self.assertEqual(v.status, "succeeded")
        self.assertEqual(v.breaks, [10])
        self.assertEqual(len(v.segments), 2)
        self.assertEqual(v.segments[0].ku_count, 1)
        self.assertEqual(v.segments[0].start_ordinal, 1)
        self.assertEqual(v.segments[1].end_ordinal, 20)

    def test_single_pass_plan_is_valid_when_fits(self):
        """材料装得下时，一刀不切也应通过（不算偷懒）。"""
        spans = make_spans(20, tokens=10)            # 总 200 token，远小于上界
        v = self.validate({"knowledge_units": [ku(1, 20)], "breaks": []}, spans)
        self.assertTrue(v.ok)
        self.assertEqual(len(v.segments), 1)

    def test_usable_breaks_only_when_ok(self):
        from app.learning_engine.boundaries import usable_breaks
        spans = make_spans(20)
        ok = self.validate(
            {"knowledge_units": [ku(1, 10), ku(11, 20)], "breaks": [10]}, spans)
        self.assertEqual(usable_breaks(ok), [10])
        bad = self.validate({"knowledge_units": [ku(1, 20)], "breaks": [20]}, spans)
        self.assertIsNone(usable_breaks(bad), "校验不过必须回退结构边界")


# ---------------------------------------------------------------------------
# 渲染与哈希
# ---------------------------------------------------------------------------
class TestSpanIndexRender(unittest.TestCase):
    def test_index_contains_ordinal_locator_and_text(self):
        from app.learning_engine.boundaries import render_span_index
        text = render_span_index(make_spans(3))
        for i in (1, 2, 3):
            self.assertIn(f"[{i}]", text)
            self.assertIn(f"cue:{i}", text)
            self.assertIn(f"第{i}条课堂内容", text)

    def test_index_does_not_truncate_text(self):
        """边界判断依赖语义，输入不得摘要或截断。"""
        from app.learning_engine.boundaries import render_span_index
        long_text = "很长的正文段落" * 500
        spans = [{"ordinal": 1, "locator": "cue:1", "text": long_text}]
        self.assertIn(long_text, render_span_index(spans))


class TestBoundaryInputHash(unittest.TestCase):
    def test_hash_is_stable_and_limit_sensitive(self):
        from app.learning_engine.boundaries import compute_boundary_input_hash
        spans = make_spans(10)
        a = compute_boundary_input_hash(spans, max_segment_tokens=12_000)
        b = compute_boundary_input_hash(spans, max_segment_tokens=12_000)
        c = compute_boundary_input_hash(spans, max_segment_tokens=24_000)
        self.assertEqual(a, b, "同输入必须得到同哈希（幂等 / 跨 run 复用）")
        self.assertNotEqual(a, c, "上界变化必须使建议失效重算")
        self.assertTrue(a.startswith("sha256:"))

    def test_hash_changes_when_text_changes(self):
        from app.learning_engine.boundaries import compute_boundary_input_hash
        spans = make_spans(10)
        before = compute_boundary_input_hash(spans, max_segment_tokens=12_000)
        spans[3]["normalized_text_hash"] = "sha256:changed"
        after = compute_boundary_input_hash(spans, max_segment_tokens=12_000)
        self.assertNotEqual(before, after)


class TestCharHint(unittest.TestCase):
    def test_char_hint_is_positive_and_conservative(self):
        from app.learning_engine.boundaries import tokens_to_char_hint
        self.assertGreater(tokens_to_char_hint(0), 0)
        self.assertLess(tokens_to_char_hint(12_000), 12_000,
                        "混合材料实际字数低于 token 数，提示必须保守")


class TestRetryHint(unittest.TestCase):
    def test_hint_empty_on_first_round(self):
        from app.learning_engine.boundaries import build_retry_hint
        self.assertEqual(build_retry_hint(None), "")

    def test_hint_reports_violations_and_asks_full_reoutput(self):
        from app.learning_engine.boundaries import build_retry_hint, validate_boundaries
        spans = make_spans(20, tokens=1000)
        v = validate_boundaries({"knowledge_units": [ku(1, 20)], "breaks": []},
                               spans, max_segment_tokens=10_000)
        hint = build_retry_hint(v)
        self.assertIn("L3", hint)
        self.assertIn("重新输出完整的 JSON", hint)

    def test_hint_mentions_overweight_segments(self):
        from app.learning_engine.boundaries import build_retry_hint, validate_boundaries
        spans = make_spans(20, tokens=2000)
        kus = [ku(1, 5), ku(6, 10), ku(11, 15), ku(16, 20)]
        v = validate_boundaries({"knowledge_units": kus, "breaks": [10]},
                                spans, max_segment_tokens=10_000)
        hint = build_retry_hint(v)
        self.assertIn("过重", hint)
        self.assertNotIn("硬约束", hint)


if __name__ == "__main__":
    unittest.main(verbosity=2)
