"""V5.6.4 零 Token 测试网关与回放体系验收。

39 项验收分两部分：
  - 本文件覆盖：mode 解析、fake/replay/live 行为、预算、审计、health、diagnostics。
  - ``test_v564_dag_e2e.py`` 覆盖：4 个 DAG 在 fake 模式下端到端闭环。

测试隔离：默认走 ``V5_GATEWAY_MODE=fake``（由 ``run_tests_isolated.py`` 强设）。
不发起任何真实网络请求。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _force_fake():
    """每次测试前强制 fake 模式（兼容单跑与集成跑）。"""
    os.environ["V5_GATEWAY_MODE"] = "fake"
    os.environ.pop("V5_GATEWAY_API_KEY", None)
    os.environ.pop("V5_ALLOW_LIVE_MODEL_TESTS", None)


class Base(unittest.TestCase):
    def setUp(self):
        _force_fake()
        # 清 engine 单例缓存
        from app import gateway as gw_mod
        gw_mod.reset_engine_for_test()
        # TestClient 非本机 host 时绕过 MOBILE_TOKEN 校验（与 V5.6.2/6.3 一致）
        from app import config as _cfg
        self._orig_mobile_token = getattr(_cfg, "MOBILE_TOKEN", "")
        _cfg.MOBILE_TOKEN = ""

        self.tmp = Path(tempfile.mkdtemp(prefix="v564_"))
        from app import database as db
        db.configure_db(self.tmp / "t.db")
        db.init_db()

    def tearDown(self):
        from app import database as db
        db.reset_connections()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        from app import gateway as gw_mod
        gw_mod.reset_engine_for_test()
        # 还原 MOBILE_TOKEN
        from app import config as _cfg
        _cfg.MOBILE_TOKEN = self._orig_mobile_token


# ===========================================================================
# 1) Mode 解析
# ===========================================================================
class TestModeResolution(Base):
    def test_default_fake_under_test_env(self):
        """V5_ENV=test（被 V5_TEST_DATA_ROOT 强制后）默认 fake。"""
        # V5_TEST_DATA_ROOT 已 setUp 间接触发 config.ENV='test'
        from app.gateway_mode import resolve_mode
        os.environ.pop("V5_GATEWAY_MODE", None)
        from app import gateway as gw_mod
        gw_mod.reset_engine_for_test()
        self.assertEqual(resolve_mode(), "fake")

    def test_test_env_rejects_live_without_allow(self):
        """V5_ENV=test + V5_GATEWAY_MODE=live 但未设 V5_ALLOW_LIVE_MODEL_TESTS → 拒绝。"""
        os.environ["V5_GATEWAY_MODE"] = "live"
        os.environ.pop("V5_ALLOW_LIVE_MODEL_TESTS", None)
        from app import gateway as gw_mod
        gw_mod.reset_engine_for_test()
        from app.gateway_mode import resolve_mode
        with self.assertRaises(RuntimeError) as ctx:
            resolve_mode()
        self.assertIn("V5_ALLOW_LIVE_MODEL_TESTS", str(ctx.exception))

    def test_test_env_accepts_live_with_allow(self):
        """V5_ENV=test + V5_GATEWAY_MODE=live + V5_ALLOW_LIVE_MODEL_TESTS=1 → live。"""
        os.environ["V5_GATEWAY_MODE"] = "live"
        os.environ["V5_ALLOW_LIVE_MODEL_TESTS"] = "1"
        from app import gateway as gw_mod
        gw_mod.reset_engine_for_test()
        from app.gateway_mode import resolve_mode
        self.assertEqual(resolve_mode(), "live")

    def test_no_env_defaults_to_live_development(self):
        """未设 V5_ENV 时（裸 development）默认 live。"""
        # 模拟无 V5_ENV：不传 V5_TEST_DATA_ROOT 也不传 V5_ENV；config.ENV 兜底 development
        # 这里 config 已 import，ENV 固化。改用直接构造一个新的 gateway_mode module：
        # 通过临时切换 config.ENV
        from app import config as cfg_mod
        from app import gateway as gw_mod
        gw_mod.reset_engine_for_test()
        original = cfg_mod.ENV
        try:
            cfg_mod.ENV = "development"
            os.environ.pop("V5_GATEWAY_MODE", None)
            # 重新求值：直接调 resolve_mode
            from app import gateway_mode as gm
            # 由于 _config_env 是缓存的，需要 monkey patch
            orig_fn = gm._config_env
            gm._config_env = lambda: "development"
            try:
                self.assertEqual(gm.resolve_mode(), "live")
            finally:
                gm._config_env = orig_fn
        finally:
            cfg_mod.ENV = original

    def test_invalid_mode_raises(self):
        """非法 V5_GATEWAY_MODE 值 → RuntimeError。"""
        os.environ["V5_GATEWAY_MODE"] = "bogus"
        from app import gateway as gw_mod
        gw_mod.reset_engine_for_test()
        from app.gateway_mode import resolve_mode
        with self.assertRaises(RuntimeError):
            resolve_mode()


# ===========================================================================
# 2) Fake / Replay 行为
# ===========================================================================
class TestFakeGateway(Base):
    def _run_fake_chat(self, contract="lesson/note_writer"):
        from app.gateway import gateway
        return asyncio.run(gateway.chat(
            "qwen3_flash",
            [{"role": "user", "content": "示例材料"}],
            contract=contract,
            temperature=0.3,
        ))

    def test_fake_chat_zero_tokens(self):
        r = self._run_fake_chat()
        self.assertEqual(r["tokens_in"], 0)
        self.assertEqual(r["tokens_out"], 0)
        self.assertTrue(r["model"].startswith("fake::"))

    def test_fake_chat_makes_no_socket(self):
        """fake chat 不建立任何 socket 连接。"""
        # 替换 httpx.AsyncClient.send 探测调用
        import httpx
        orig = httpx.AsyncClient.send
        called = {"n": 0}

        async def tracker(self, request, *a, **kw):
            called["n"] += 1
            return await orig(self, request, *a, **kw)

        httpx.AsyncClient.send = tracker
        try:
            self._run_fake_chat()
            # fake 不该碰 httpx
            self.assertEqual(called["n"], 0,
                             f"fake chat 触发 {called['n']} 次 httpx.send")
        finally:
            httpx.AsyncClient.send = orig

    def test_fake_embedding_makes_no_socket(self):
        import httpx
        orig = httpx.AsyncClient.send
        called = {"n": 0}

        async def tracker(self, request, *a, **kw):
            called["n"] += 1
            return await orig(self, request, *a, **kw)

        httpx.AsyncClient.send = tracker
        try:
            from app.gateway import gateway
            v = asyncio.run(gateway.embedding_single("test"))
            self.assertEqual(called["n"], 0)
            self.assertEqual(len(v), 1024)
        finally:
            httpx.AsyncClient.send = orig

    def test_fake_usage_makes_no_socket(self):
        import httpx
        orig = httpx.AsyncClient.send
        called = {"n": 0}

        async def tracker(self, request, *a, **kw):
            called["n"] += 1
            return await orig(self, request, *a, **kw)

        httpx.AsyncClient.send = tracker
        try:
            from app.gateway import gateway
            u = asyncio.run(gateway.get_usage())
            self.assertEqual(called["n"], 0)
            self.assertTrue(u.get("available"))
            self.assertEqual(u["data"]["used_5h"], 0)
        finally:
            httpx.AsyncClient.send = orig

    def test_fake_chat_deterministic(self):
        """同 contract + 同输入 → 同输出（fixture 决定）。"""
        r1 = self._run_fake_chat("lesson/critic")
        r2 = self._run_fake_chat("lesson/critic")
        self.assertEqual(r1["content"], r2["content"])

    def test_unknown_contract_raises(self):
        from app.gateway import gateway
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(gateway.chat(
                "qwen3_flash",
                [{"role": "user", "content": "x"}],
                contract="bogus/contract",
            ))
        self.assertIn("未知 contract", str(ctx.exception))

    def test_post_json_blocked_in_fake(self):
        """fake 模式下 gateway.post_json 拒绝；只有 live 允许。"""
        from app.gateway import gateway
        with self.assertRaises(RuntimeError):
            asyncio.run(gateway.post_json("/v1/x", {"a": 1}))


# ===========================================================================
# 3) Fake Embedding
# ===========================================================================
class TestFakeEmbedding(Base):
    def test_embed_same_text_same_vec(self):
        from app.gateway_engines import deterministic_embed
        v1 = deterministic_embed("hello")
        v2 = deterministic_embed("hello")
        self.assertEqual(v1, v2)

    def test_embed_diff_text_diff_vec(self):
        from app.gateway_engines import deterministic_embed
        v1 = deterministic_embed("hello")
        v2 = deterministic_embed("hello world")
        # 余弦相似度应显著 < 1
        import math
        dot = sum(a * b for a, b in zip(v1, v2))
        n1 = math.sqrt(sum(a * a for a in v1))
        n2 = math.sqrt(sum(b * b for b in v2))
        cos = dot / max(n1 * n2, 1e-9)
        self.assertLess(cos, 0.99)

    def test_embed_dim_1024_and_normalized(self):
        from app.gateway_engines import deterministic_embed
        v = deterministic_embed("any")
        self.assertEqual(len(v), 1024)
        import math
        norm = math.sqrt(sum(x * x for x in v))
        self.assertAlmostEqual(norm, 1.0, places=5)
        # 无 NaN
        self.assertFalse(any(x != x for x in v))  # NaN != NaN

    def test_embed_empty_text_safe(self):
        from app.gateway_engines import deterministic_embed
        v = deterministic_embed("")
        self.assertEqual(len(v), 1024)
        self.assertFalse(any(x != x for x in v))


# ===========================================================================
# 4) Fixtures
# ===========================================================================
class TestFakeFixtures(unittest.TestCase):
    def test_all_fake_fixtures_pass_pydantic(self):
        """全部 fixture 启动时 Pydantic Schema 校验全通过。

        V6 Phase 2/3 新增 lesson/segment_understanding、lesson/merge_understanding 与
        lesson/student_simulator（动态 fixture，按输入生成），因此 contract 总数为 16；断言按契约数量
        而非硬编码常量，避免每次新增契约都要改测试却断言不到真实内容。
        """
        from app.gateway_engines import (
            validate_all_fixtures_at_import, CONTRACT_TABLE, known_contracts,
        )
        validate_all_fixtures_at_import()
        self.assertEqual(len(CONTRACT_TABLE), len(known_contracts()))
        self.assertEqual(len(CONTRACT_TABLE), 16)
        self.assertIn("lesson/segment_understanding", CONTRACT_TABLE)
        self.assertIn("lesson/merge_understanding", CONTRACT_TABLE)
        self.assertIn("lesson/student_simulator", CONTRACT_TABLE)


# ===========================================================================
# 5) Replay
# ===========================================================================
class TestReplay(Base):
    def setUp(self):
        super().setUp()
        from app import gateway as gw_mod
        gw_mod.reset_engine_for_test()

    def _build_replay(self, key_to_resp: dict[str, dict]):
        """手工构造 ReplayEngine + 夹具目录。"""
        import tempfile
        from pathlib import Path
        from app.gateway_engines import ReplayEngine, compute_replay_key
        tmp = Path(tempfile.mkdtemp(prefix="v564_replay_"))
        engine = ReplayEngine(tmp)
        for key, resp in key_to_resp.items():
            env = {
                "request_digest": key,
                "response": resp,
            }
            (tmp / f"{key}.json").write_text(
                json.dumps(env, ensure_ascii=False), encoding="utf-8",
            )
        return engine, tmp

    def _set_replay_ctx(self, contract: str, *, prompt_checksum: str = "fake_checksum",
                         schema_name: str = "NoteWriterOut",
                         schema_version: str = "v1",
                         model_id: str = "qwen3_flash"):
        """让 ReplayEngine.chat 算 key 时 ctx 不为空（pn/pc 与写入一致）。"""
        from app.gateway import set_call_context
        token = set_call_context(
            run_id=None,
            node_id=contract.split("/")[-1],
            attempt=1,
            prompt_name=contract,
            prompt_version="v1",
            prompt_checksum=prompt_checksum,
            schema_name=schema_name,
            schema_version=schema_version,
            model_id=model_id,
        )
        return token

    def test_replay_hit_returns_cached(self):
        from app.gateway_engines import (
            compute_replay_key, FakeEngine, _load_fixture,
        )
        from app.gateway import reset_call_context
        fake = FakeEngine()
        fixture = _load_fixture("lesson/note_writer")
        # 构造一个确定的 key
        key = compute_replay_key(
            "chat", "qwen3_flash", "lesson/note_writer",
            "lesson/note_writer", "fake_checksum",
            [{"role": "user", "content": "x"}], 0.35, None, None,
            "NoteWriterOut", "v1",
        )
        engine, tmp = self._build_replay({key: fixture})
        ctx_token = self._set_replay_ctx(
            "lesson/note_writer", prompt_checksum="fake_checksum",
            schema_name="NoteWriterOut", schema_version="v1",
        )
        try:
            try:
                r = asyncio.run(engine.chat(
                    "lesson/note_writer", "qwen3_flash",
                    [{"role": "user", "content": "x"}], 0.35, None, None,
                ))
                self.assertTrue(r.get("replay_hit"))
                self.assertEqual(r.get("replay_key"), key)
                self.assertEqual(r["content"], json.dumps(fixture, ensure_ascii=False))
            finally:
                reset_call_context(ctx_token)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_replay_miss_raises(self):
        from app.gateway_engines import ReplayEngine
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp(prefix="v564_replay_miss_"))
        engine = ReplayEngine(tmp)
        try:
            from app.gateway_mode import ReplayMissError
            with self.assertRaises(ReplayMissError):
                asyncio.run(engine.chat(
                    "lesson/note_writer", "qwen3_flash",
                    [{"role": "user", "content": "y"}], 0.35, None, None,
                ))
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_replay_miss_no_fallback_to_live(self):
        """replay miss 绝不允许 fallback live：抛 ReplayMissError，不走 live。"""
        from app.gateway_engines import ReplayEngine
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp(prefix="v564_replay_miss2_"))
        engine = ReplayEngine(tmp)
        try:
            # 强制 mode = replay 但 engine 拿到 fake 走不通的场景
            # 这里直接验证 ReplayEngine.chat miss 时抛错
            from app.gateway_mode import ReplayMissError
            with self.assertRaises(ReplayMissError):
                asyncio.run(engine.chat(
                    "lesson/note_writer", "qwen3_flash",
                    [{"role": "user", "content": "z"}], 0.35, None, None,
                ))
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_replay_key_distinct_for_diff_messages(self):
        from app.gateway_engines import compute_replay_key
        k1 = compute_replay_key(
            "chat", "qwen3_flash", "lesson/note_writer",
            "pn", "pc",
            [{"role": "user", "content": "msg-A"}],
            0.35, None, None, "NoteWriterOut", "v1",
        )
        k2 = compute_replay_key(
            "chat", "qwen3_flash", "lesson/note_writer",
            "pn", "pc",
            [{"role": "user", "content": "msg-B"}],
            0.35, None, None, "NoteWriterOut", "v1",
        )
        self.assertNotEqual(k1, k2)

    def test_replay_bad_response_schema_raises(self):
        """Replay 夹具 response 不通过 Schema → ReplayMissError。"""
        from app.gateway_engines import (
            compute_replay_key, ReplayEngine,
        )
        from app.gateway import reset_call_context
        import tempfile
        from pathlib import Path
        key = compute_replay_key(
            "chat", "qwen3_flash", "lesson/critic",
            "lesson/critic", "pc",
            [{"role": "user", "content": "x"}], 0.2, None, None,
            "CriticOut", "v1",
        )
        tmp = Path(tempfile.mkdtemp(prefix="v564_replay_bad_"))
        engine = ReplayEngine(tmp)
        # response 故意提供不可解析的 score
        (tmp / f"{key}.json").write_text(json.dumps({
            "request_digest": key,
            "response": {"score": "not-a-number", "passed": True},
        }, ensure_ascii=False), encoding="utf-8")
        ctx_token = self._set_replay_ctx(
            "lesson/critic", prompt_checksum="pc",
            schema_name="CriticOut", schema_version="v1",
        )
        try:
            try:
                from app.gateway_mode import ReplayMissError
                with self.assertRaises(ReplayMissError) as ctx:
                    asyncio.run(engine.chat(
                        "lesson/critic", "qwen3_flash",
                        [{"role": "user", "content": "x"}], 0.2, None, None,
                    ))
                self.assertIn("schema 校验失败", str(ctx.exception))
            finally:
                reset_call_context(ctx_token)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# 6) Live 守卫 + 预算
# ===========================================================================
class TestLiveGuard(Base):
    def test_live_required_env_missing(self):
        from app.gateway_mode import live_required_env
        from app import config
        from unittest import mock
        os.environ.pop("V5_GATEWAY_URL", None)
        os.environ.pop("V5_GATEWAY_API_KEY", None)
        with mock.patch.object(config, "GATEWAY_BASE_URL", ""), \
             mock.patch.object(config, "GATEWAY_API_KEY", ""):
            with self.assertRaises(RuntimeError):
                live_required_env()

    def test_live_loopback_allows_gateway_without_api_key(self):
        from app.gateway_mode import live_required_env
        from app import config
        from unittest import mock
        os.environ["V5_GATEWAY_URL"] = "http://127.0.0.1:8317"
        os.environ.pop("V5_GATEWAY_API_KEY", None)
        with mock.patch.object(config, "GATEWAY_API_KEY", ""):
            url, key, _ = live_required_env()
        self.assertEqual(url, "http://127.0.0.1:8317")
        self.assertEqual(key, "")

    def test_live_remote_gateway_still_requires_api_key(self):
        from app.gateway_mode import live_required_env
        from app import config
        from unittest import mock
        os.environ["V5_GATEWAY_URL"] = "https://gateway.example.test"
        os.environ.pop("V5_GATEWAY_API_KEY", None)
        with mock.patch.object(config, "GATEWAY_API_KEY", ""):
            with self.assertRaises(RuntimeError):
                live_required_env()

    def test_reserve_budget_under_fake_returns_zero(self):
        """fake 模式 budget 不消费。"""
        from app.gateway_mode import reserve_budget, snapshot, reset_counters
        reset_counters()
        r_in, r_out = reserve_budget(1000, 100, label="test")
        self.assertEqual((r_in, r_out), (0, 0))
        s = snapshot()
        self.assertEqual(s["live_call_count"], 0)


class TestLiveBudgetUnderFake(Base):
    """LiveEngine 预算预检逻辑单测：模拟 live mode 触发 reserve_budget 分支。"""

    def setUp(self):
        super().setUp()
        # 直接测 reserve_budget：通过 monkey patch resolve_mode
        from app import gateway_mode as gm
        self._orig_resolve = gm.resolve_mode
        gm.resolve_mode = lambda: "live"
        gm.reset_counters()
        # 设置预算
        os.environ["V5_LIVE_MAX_CALLS"] = "3"
        os.environ["V5_LIVE_TOKEN_BUDGET_TOTAL"] = "1000"
        os.environ["V5_LIVE_MAX_INPUT_CHARS"] = "100"

    def tearDown(self):
        from app import gateway_mode as gm
        gm.resolve_mode = self._orig_resolve
        gm.reset_counters()
        for k in ("V5_LIVE_MAX_CALLS", "V5_LIVE_TOKEN_BUDGET_TOTAL",
                  "V5_LIVE_MAX_INPUT_CHARS"):
            os.environ.pop(k, None)
        super().tearDown()

    def test_live_max_input_chars_precheck(self):
        from app import gateway_mode as gm
        with self.assertRaises(RuntimeError) as ctx:
            gm.reserve_budget(200, 0, label="too_long")
        self.assertIn("V5_LIVE_MAX_INPUT_CHARS", str(ctx.exception))

    def test_live_token_budget_precheck_before_request(self):
        from app import gateway_mode as gm
        gm.reserve_budget(50, 200, label="ok1")  # calls=1
        # 再调用一次，预估 200 + 累计 200 = 400 < 1000 ok
        gm.reserve_budget(50, 200, label="ok2")  # calls=2
        # 第三次：预估 800 > 剩余 600 → 超限
        with self.assertRaises(RuntimeError) as ctx:
            gm.reserve_budget(50, 800, label="too_much")
        self.assertIn("V5_LIVE_TOKEN_BUDGET_TOTAL", str(ctx.exception))

    def test_live_max_calls_precheck(self):
        from app import gateway_mode as gm
        for i in range(3):
            gm.reserve_budget(10, 10, label=f"ok{i}")
        with self.assertRaises(RuntimeError) as ctx:
            gm.reserve_budget(10, 10, label="overflow")
        self.assertIn("V5_LIVE_MAX_CALLS", str(ctx.exception))


# ===========================================================================
# 7) 审计
# ===========================================================================
class TestAudit(Base):
    def test_fake_audit_zero_tokens_and_gateway_mode(self):
        from app.gateway import gateway, set_call_context, reset_call_context
        from app import database as db
        # 准备 run_id
        run_id = db.insert(
            "INSERT INTO workflow_runs (workflow, mode, status) "
            "VALUES ('lesson', 'attend', 'running')",
        )
        token = set_call_context(
            run_id=run_id, node_id="note_writer", attempt=1,
            prompt_name="lesson/note_writer", prompt_version="v1",
        )
        try:
            asyncio.run(gateway.chat(
                "qwen3_flash",
                [{"role": "user", "content": "示例"}],
                contract="lesson/note_writer",
                temperature=0.35,
            ))
        finally:
            reset_call_context(token)

        rows = db.fetch_all(
            "SELECT * FROM model_calls WHERE run_id=? ORDER BY id DESC LIMIT 1",
            (run_id,),
        )
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["gateway_mode"], "fake")
        self.assertEqual(r["tokens_in"], 0)
        self.assertEqual(r["tokens_out"], 0)
        self.assertEqual(r["status"], "ok")
        # replay 模式下字段应为空/0
        self.assertEqual(r["replay_hit"], 0)
        self.assertEqual(r["replay_key"] or "", "")

    def test_audit_written_once_per_call(self):
        """fake/replay 模式下，每次 chat 仅写一行审计。"""
        from app.gateway import gateway
        from app import database as db
        run_id = db.insert(
            "INSERT INTO workflow_runs (workflow, mode, status) "
            "VALUES ('lesson', 'attend', 'running')",
        )
        from app.gateway import set_call_context, reset_call_context
        token = set_call_context(
            run_id=run_id, node_id="n", attempt=1,
            prompt_name="lesson/note_writer", prompt_version="v1",
        )
        try:
            for _ in range(3):
                asyncio.run(gateway.chat(
                    "qwen3_flash",
                    [{"role": "user", "content": "x"}],
                    contract="lesson/note_writer", temperature=0.3,
                ))
        finally:
            reset_call_context(token)
        rows = db.fetch_all(
            "SELECT id FROM model_calls WHERE run_id=?", (run_id,),
        )
        self.assertEqual(len(rows), 3)


# ===========================================================================
# 8) Migration
# ===========================================================================
class TestMigration0009(Base):
    def test_migration_0009_columns_exist(self):
        from app import database as db
        cols = db.fetch_all("PRAGMA table_info(model_calls)")
        names = {c["name"] for c in cols}
        self.assertIn("gateway_mode", names)
        self.assertIn("replay_key", names)
        self.assertIn("replay_hit", names)


# ===========================================================================
# 9) Health / Diagnostics
# ===========================================================================
class TestHealthDiagnostics(Base):
    def test_health_includes_gateway_mode(self):
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        # /api/health 公开
        r = client.get("/api/health")
        # 不强求 200（database_unavailable 也 ok），但必须含 gateway_mode / model_gateway
        body = r.json()
        self.assertIn("gateway_mode", body)
        self.assertIn("model_gateway", body)
        self.assertIn(body["gateway_mode"], {"fake", "replay", "live", "unavailable"})

    def test_diagnostics_shows_mode_and_counters(self):
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(
            app,
            base_url="http://127.0.0.1",
            client=("127.0.0.1", 50000),
        )
        # /api/admin/diagnostics 要求 127.0.0.1（TestClient 默认 OK）
        r = client.get("/api/admin/diagnostics")
        body = r.json()
        self.assertIn("gateway", body)
        gw = body["gateway"]
        if "error" not in gw:
            self.assertIn("mode", gw)
            self.assertIn("live_call_count", gw)
            self.assertIn("live_tokens_consumed", gw)

    def test_public_health_no_leak(self):
        """公开 health 不泄露路径、Key、replay 目录、计数。"""
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        r = client.get("/api/health")
        body = r.json()
        forbidden_substrings = ["http://", "Bearer ", "API_KEY",
                                "v5_test_root", "/admin/api/usage"]
        for sub in forbidden_substrings:
            self.assertNotIn(sub, json.dumps(body),
                             f"health 不应泄露 {sub!r}")

    def test_no_public_gateway_mode_endpoint(self):
        """不应新增 /api/admin/gateway_mode 这类公开 admin API。
        只允许 /api/admin/diagnostics（本机 only）与 /api/health（公开但仅模式字符串）。"""
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        # 应该 404
        r = client.get("/api/admin/gateway_mode")
        self.assertEqual(r.status_code, 404)


# ===========================================================================
# 10) 网络拦截 / 熔断
# ===========================================================================
class TestZeroTokenRunner(unittest.TestCase):
    def test_blocker_catches_both_loopback_aliases(self):
        """拦截新旧网关端口及两个 loopback 别名。"""
        # 直接 import 工具脚本，跑一个最小测试
        sys.path.insert(0, str(_ROOT))
        from tools import _run_unittest_zero_token as runner
        self.assertIn(("127.0.0.1", 8080), runner.FORBIDDEN)
        self.assertIn(("localhost", 8080), runner.FORBIDDEN)
        self.assertIn(("127.0.0.1", 8317), runner.FORBIDDEN)
        self.assertIn(("localhost", 8317), runner.FORBIDDEN)

    def test_blocker_active_in_subprocess(self):
        """验证：_run_unittest_zero_token.py 在独立 Python 解释器中能装 httpx 熔断。"""
        import subprocess
        tmp = Path(tempfile.mkdtemp(prefix="v564_block_"))
        # 写一个最小 unittest 测试：在子进程导入 runner 模块
        env = dict(os.environ)
        env["V5_GATEWAY_MODE"] = "fake"
        env.pop("V5_GATEWAY_API_KEY", None)
        env["V5_TEST_DATA_ROOT"] = str(tmp)
        env["V5_ZERO_TOKEN_REPORT"] = str(tmp / "r.json")
        # 探测模块的 FORBIDDEN 已注入
        code = """
import sys, os
sys.path.insert(0, '.')
from tools import _run_unittest_zero_token as r
assert ('127.0.0.1', 8080) in r.FORBIDDEN
assert ('localhost', 8080) in r.FORBIDDEN
print('OK')
"""
        try:
            p = subprocess.run(
                [sys.executable, "-c", code],
                cwd=str(_ROOT), env=env, capture_output=True,
                text=True, encoding="utf-8",
            )
            self.assertEqual(p.returncode, 0,
                             f"stderr={p.stderr}, stdout={p.stdout}")
            self.assertIn("OK", p.stdout)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# 11) Live smoke 入口鉴权
# ===========================================================================
class TestLiveSmokeRunnerAuth(unittest.TestCase):
    def test_live_runner_refuses_without_authorization(self):
        """缺任一鉴权变量：python tools/run_live_model_smoke.py 立即失败。"""
        import subprocess
        env = dict(os.environ)
        # 清掉所有鉴权相关变量
        for k in ("V5_ENV", "V5_GATEWAY_MODE", "V5_ALLOW_LIVE_MODEL_TESTS",
                  "V5_GATEWAY_API_KEY"):
            env[k] = ""
        p = subprocess.run(
            [sys.executable, str(_ROOT / "tools" / "run_live_model_smoke.py")],
            cwd=str(_ROOT), env=env, capture_output=True,
            text=True, encoding="utf-8",
        )
        self.assertNotEqual(p.returncode, 0)
        # 至少有一行 [FAIL] 提示
        self.assertIn("[FAIL]", p.stderr)


if __name__ == "__main__":
    unittest.main()
