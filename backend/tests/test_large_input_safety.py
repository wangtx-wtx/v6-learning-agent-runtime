"""长文本/大文件边界与轻量运行 DTO 回归（全程隔离、零真实网关）。"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient


class TestLargeInputSafety(unittest.TestCase):
    def setUp(self):
        os.environ["V5_GATEWAY_MODE"] = "fake"
        self.tmp = Path(tempfile.mkdtemp(prefix="v5_large_input_"))
        from app import database as db
        from app import config as app_config
        self._mobile_token = app_config.MOBILE_TOKEN
        app_config.MOBILE_TOKEN = ""
        db.configure_db(self.tmp / "test.db")
        db.init_db()
        self.db = db

    def tearDown(self):
        from app import config as app_config
        app_config.MOBILE_TOKEN = self._mobile_token
        self.db.reset_connections()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_limits_and_inline_transcript_413(self):
        from app.main import MAX_INLINE_TRANSCRIPT_CHARS, app
        client = TestClient(app)
        limits = client.get("/api/capabilities/limits")
        self.assertEqual(limits.status_code, 200)
        self.assertEqual(limits.json()["max_inline_transcript_chars"],
                         MAX_INLINE_TRANSCRIPT_CHARS)
        res = client.post("/api/workflows/lesson", json={
            "transcript": "字" * (MAX_INLINE_TRANSCRIPT_CHARS + 1),
        })
        self.assertEqual(res.status_code, 413)
        self.assertIn("TXT/SRT/VTT", res.json()["detail"])

    def test_run_endpoints_do_not_repeat_large_payloads(self):
        from app.main import app
        run_id = self.db.insert(
            "INSERT INTO workflow_runs (workflow, mode, status, input_json, output_json) "
            "VALUES ('lesson','attend','completed',?,?)",
            ("x" * 100_000, "y" * 100_000),
        )
        self.db.insert(
            "INSERT INTO run_nodes (run_id,node_name,status,output_json) VALUES (?,?,?,?)",
            (run_id, "segment_lesson", "success", "z" * 10_000),
        )
        client = TestClient(app)
        listed = client.get("/api/runs?limit=5").json()[0]
        self.assertNotIn("input_json", listed)
        self.assertNotIn("output_json", listed)
        generic = client.get(f"/api/runs/{run_id}").json()
        self.assertNotIn("input_json", generic["run"])
        self.assertNotIn("output_json", generic["run"])
        self.assertLessEqual(len(generic["nodes"][0]["output_ref"]), 4000)
        lesson = client.get(f"/api/workflows/lesson/{run_id}").json()
        self.assertNotIn("output_json", lesson["run"])
        final = client.get(
            f"/api/workflows/lesson/{run_id}?include_payloads=true").json()
        self.assertEqual(len(final["run"]["output_json"]), 100_000)


# 复用现有 V6 隔离夹具，专门覆盖“最终消息仅比预算多几个 token”这一真实故障。
from tests.test_v6_phase2_closeout import CloseoutBase, LONG_TRANSCRIPT


class TestFinalMessageBudgetRepair(CloseoutBase):
    def test_large_context_model_still_uses_latency_safe_segments(self):
        from app.learning_engine.segment import (
            DEFAULT_PERFORMANCE_INPUT_BUDGET,
            fit_segment_plan_to_budget,
            plan_segments,
        )
        transcript = "\n\n".join(
            f"教师 00:{i // 60:02d}:{i % 60:02d} 第{i}段 " + ("独立课堂内容" * 120)
            for i in range(80)
        )
        ctx, _ = self.run_dag(transcript=transcript)
        plan = plan_segments(
            ctx.domain_id,
            model_profile_id="huge_ctx_for_latency",
            context_window=1_000_000,
        )
        plan, check = fit_segment_plan_to_budget(ctx.domain_id, plan)
        self.assertEqual(plan["budget"], DEFAULT_PERFORMANCE_INPUT_BUDGET)
        self.assertGreater(plan["context_input_budget"], plan["budget"])
        self.assertGreater(len(plan["drafts"]), 1)
        self.assertEqual(check["over_budget_segments"], 0)
        self.assertLessEqual(check["max_input_tokens"], DEFAULT_PERFORMANCE_INPUT_BUDGET)

    def test_small_final_prompt_drift_is_resegmented_without_loss(self):
        from app.learning_engine.segment import (
            fit_segment_plan_to_budget, plan_segments, verify_segment_budget,
        )
        ctx, _ = self.run_dag(transcript=LONG_TRANSCRIPT)
        plan = plan_segments(ctx.domain_id)
        before = [s["source_id"] for d in plan["drafts"] for s in d.primary]
        initial = verify_segment_budget(ctx.domain_id, plan)
        plan["budget"] = max(initial["max_input_tokens"] - 1, 1)

        repaired, check = fit_segment_plan_to_budget(ctx.domain_id, plan)
        after = [s["source_id"] for d in repaired["drafts"] for s in d.primary]
        self.assertEqual(check["over_budget_segments"], 0)
        self.assertEqual(before, after)
        self.assertGreater(repaired.get("budget_repairs", 0), 0)


class TestGatewayTimeoutPolicy(unittest.IsolatedAsyncioTestCase):
    async def test_read_timeout_is_not_replayed_three_times(self):
        import httpx
        from app.gateway_engines import GatewayReadTimeoutError, LiveEngine

        engine = LiveEngine("http://127.0.0.1:9", "", 180)
        engine._client = AsyncMock()
        engine._client.post.side_effect = httpx.ReadTimeout("slow upstream")
        with self.assertRaises(GatewayReadTimeoutError):
            await engine._post_with_retry(
                "/v1/chat/completions", {"model": "x"}, read_timeout=240)
        self.assertEqual(engine._client.post.await_count, 1)


if __name__ == "__main__":
    unittest.main()
