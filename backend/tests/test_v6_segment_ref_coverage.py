"""段内引用覆盖率观测验收测试（V6 Phase 2 增强）。

覆盖两件事:

1. ``compute_segment_ref_coverage`` 的比例计算 —— 这是把「中间迷失」从黑盒
   变成可定位问题的唯一依据，算错会让它被误报或漏报。
2. ``evaluate_gate`` **不因低引用覆盖而降级** —— 本次改动最关键的边界。
   引用覆盖率是**诊断指标**，一旦被纳入门禁，几乎所有 run 都会误降级
   （课堂材料里大量过渡性内容本就不会被引用）。

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


def src(sid: str, role: str = "primary") -> dict:
    return {"source_id": sid, "source_span_id": 1, "role": role}


class TestComputeRefCoverage(unittest.TestCase):
    def cov(self, sources, refs):
        from app.learning_engine.understanding import compute_segment_ref_coverage
        return compute_segment_ref_coverage(sources, refs)

    def test_all_primary_referenced(self):
        r = self.cov([src("T000001"), src("T000002")], ["T000001", "T000002"])
        self.assertEqual(r["primary_span_count"], 2)
        self.assertEqual(r["referenced_primary_count"], 2)
        self.assertEqual(r["unreferenced_source_ids"], [])
        self.assertAlmostEqual(r["ratio"], 1.0)

    def test_partial_reference_reports_unreferenced_ids(self):
        r = self.cov([src("T000001"), src("T000002"), src("T000003")], ["T000002"])
        self.assertEqual(r["primary_span_count"], 3)
        self.assertEqual(r["referenced_primary_count"], 1)
        self.assertEqual(r["unreferenced_source_ids"], ["T000001", "T000003"])
        self.assertAlmostEqual(r["ratio"], 1 / 3)

    def test_overlap_role_excluded_from_denominator(self):
        """overlap 只为上下文，覆盖率只按 primary 计算。"""
        r = self.cov([src("T000001"), src("T000002", role="overlap")], ["T000002"])
        self.assertEqual(r["primary_span_count"], 1)
        self.assertEqual(r["referenced_primary_count"], 0)
        self.assertAlmostEqual(r["ratio"], 0.0)

    def test_refs_outside_segment_are_ignored(self):
        """模型引用了别的段的 Source ID 时不虚增本段覆盖率。"""
        r = self.cov([src("T000001")], ["T000001", "T999999"])
        self.assertEqual(r["referenced_primary_count"], 1)
        self.assertAlmostEqual(r["ratio"], 1.0)

    def test_empty_primary_does_not_divide_by_zero(self):
        r = self.cov([src("T000001", role="overlap")], ["T000001"])
        self.assertEqual(r["primary_span_count"], 0)
        self.assertEqual(r["ratio"], 0.0)

    def test_no_reference_at_all(self):
        """中间迷失的极端形态：段「成功」但一个段内 span 都没被引用。"""
        r = self.cov([src("T000001"), src("T000002")], [])
        self.assertEqual(r["referenced_primary_count"], 0)
        self.assertAlmostEqual(r["ratio"], 0.0)
        self.assertEqual(len(r["unreferenced_source_ids"]), 2)


class TestGateNotAffectedByRefCoverage(unittest.TestCase):
    """门禁边界：引用覆盖率必须**不参与**判定。"""

    @staticmethod
    def base_metrics() -> dict:
        """一份本来就能 passed 的指标。"""
        return {
            "silent_dropped": 0,
            "domain_accounting_rate": 1.0,
            "required_items_failed": 0,
            "required_items_unsupported": 0,
            "required_items_without_canonical_span": 0,
            "unassigned_count": 0,
            "canonical_non_noise_spans": 10,
            "semantic_processing_rate": 1.0,
            "required_spans_unprocessed": 0,
            "source_refs_invalid": 0,
            "segment_failed_count": 0,
            "segment_count": 2,
            "merge_consumed_all_segments": True,
            "segments_consumed_by_merge": 2,
            "structures_dropped": 0,
        }

    def test_baseline_passes(self):
        from app.learning_engine.coverage import evaluate_gate
        gate, reason, issues = evaluate_gate(self.base_metrics())
        self.assertEqual(gate, "passed", f"基线应 passed，reason={reason}")
        self.assertIsNone(reason)
        self.assertEqual(issues, [])

    def test_low_ref_coverage_does_not_change_gate(self):
        from app.learning_engine.coverage import evaluate_gate
        m = self.base_metrics()
        m.update({
            "segment_ref_coverage_min": 0.0,
            "segment_ref_coverage_avg": 0.05,
            "low_ref_coverage_count": 2,
            "low_ref_coverage_segments": [
                {"ordinal": 1, "primary_span_count": 5, "referenced_primary_count": 0,
                 "ratio": 0.0, "unreferenced_sample": ["T000001"]},
                {"ordinal": 2, "primary_span_count": 4, "referenced_primary_count": 1,
                 "ratio": 0.25, "unreferenced_sample": ["T000010"]},
            ],
        })
        gate, reason, issues = evaluate_gate(m)
        self.assertEqual(gate, "passed",
                         f"低引用覆盖不得改变门禁（否则几乎所有 run 误降级），reason={reason}")
        self.assertIsNone(reason)
        self.assertEqual(issues, [])


class TestMigration0025(unittest.TestCase):
    """迁移 0025 必须只做 ALTER，不得重建表（重建会丢历史理解结果）。"""

    def migration_sql(self) -> str:
        from app.database import MIGRATIONS_DIR
        files = sorted(MIGRATIONS_DIR.glob("0025_*.sql"))
        self.assertEqual(len(files), 1, f"应恰好有一个 0025 迁移，实际 {[f.name for f in files]}")
        return files[0].read_text(encoding="utf-8")

    def test_columns_present(self):
        sql = self.migration_sql()
        self.assertIn("ALTER TABLE segment_understandings", sql)
        self.assertIn("primary_span_count", sql)
        self.assertIn("referenced_primary_count", sql)
        self.assertIn("unreferenced_source_ids", sql)

    def test_no_table_rebuild(self):
        sql = self.migration_sql()
        self.assertNotIn("DROP TABLE", sql)
        self.assertNotIn("CREATE TABLE", sql)


if __name__ == "__main__":
    unittest.main(verbosity=2)
