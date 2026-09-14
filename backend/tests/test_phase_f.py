"""V5.5 阶段F 测试：Prompt 外置加载、Schema 绑定、混合检索、model_calls 审计。"""
import asyncio
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

from app import database as db  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        db.configure_db(self.tmp / "t.db")
        db.init_db()

    def tearDown(self):
        db.reset_connections()


class TestPromptLoader(Base):
    def test_load_builtin_with_checksum(self):
        from app.integrations import prompts as P
        P.clear_cache()
        entry = P.load_prompt("lesson/critic", "v1")
        self.assertIn("score", entry["text"])
        self.assertEqual(len(entry["checksum"]), 16)
        # 缓存命中：再次加载同一对象
        self.assertIs(entry, P.load_prompt("lesson/critic", "v1"))
        P.clear_cache()

    def test_render_variables(self):
        from app.integrations import prompts as P
        P.clear_cache()
        p = P.render_prompt("lesson/note_writer", "v1",
                            revise_section="R", outline_json="[]", material_blocks="M")
        self.assertNotIn("{{revise_section}}", p["text"])
        self.assertIn("R", p["text"])
        P.clear_cache()

    def test_fixture_override(self):
        from app.integrations import prompts as P
        P.clear_cache()
        with tempfile.TemporaryDirectory() as td:
            fx = Path(td)
            (fx / "lesson__critic.v1.md").write_text("FIXTURE-PROMPT-内容", encoding="utf-8")
            import os
            old = os.environ.get("V5_PROMPT_FIXTURE_DIR")
            os.environ["V5_PROMPT_FIXTURE_DIR"] = str(fx)
            try:
                p = P.render_prompt("lesson/critic", "v1")
                self.assertIn("FIXTURE-PROMPT", p["text"])
                self.assertTrue(p["source"].startswith("fixture:"))
            finally:
                if old is None:
                    os.environ.pop("V5_PROMPT_FIXTURE_DIR", None)
                else:
                    os.environ["V5_PROMPT_FIXTURE_DIR"] = old
            P.clear_cache()

    def test_missing_prompt_raises(self):
        from app.integrations import prompts as P
        P.clear_cache()
        with self.assertRaises(FileNotFoundError):
            P.load_prompt("no/such_prompt", "v9")
        P.clear_cache()


class TestSchemaBinding(Base):
    def test_valid_output(self):
        from app.integrations.schemas import NoteWriterOut, parse_model_output
        data = parse_model_output(NoteWriterOut, '{"title":"T","body":"B","evidence":[]}', "note_writer")
        self.assertEqual(data["title"], "T")

    def test_invalid_json_raises(self):
        from app.integrations.schemas import NoteWriterOut, parse_model_output
        from app.dag import SchemaValidationError
        with self.assertRaises(SchemaValidationError):
            parse_model_output(NoteWriterOut, "这不是JSON", "note_writer")

    def test_wrong_shape_raises(self):
        from app.integrations.schemas import CriticOut, parse_model_output
        from app.dag import SchemaValidationError
        with self.assertRaises(SchemaValidationError):
            parse_model_output(CriticOut, '{"score": "high"}', "critic")


class TestHybridRetrieval(Base):
    def _seed(self):
        db.insert("INSERT INTO courses (name) VALUES ('C')")
        db.insert("INSERT INTO chapters (course_id, chapter_no, title) VALUES (1, 1, '光学')")
        db.insert("INSERT INTO lessons (chapter_id, course_id, lesson_no, title) VALUES (1, 1, 1, '折射')")
        db.insert("INSERT INTO lessons (chapter_id, course_id, lesson_no, title) VALUES (1, 1, 2, '其他')")
        db.insert("INSERT INTO materials (course_id, display_name, kind, status) VALUES (1, 't.pdf', 'pdf', 'ready')")
        for text, lesson in [("折射定律 sin i / sin r = n", 1), ("完全无关的经济学供需曲线", 2),
                             ("折射率与波长的色散关系", 1)]:
            db.insert(
                "INSERT INTO source_chunks (material_id, chapter_id, lesson_id, type, locator, text) "
                "VALUES (1, 1, ?, 'text', '', ?)", (lesson, text))

    def test_keyword_ranking_and_persistence(self):
        self._seed()
        import app.rag as ragmod
        from app.rag import retrieve_chunks
        # 测试必须封闭：强制 embedding 不可用 → 验证自动降级 keyword_only
        async def _no_embed(text):
            return []
        orig = ragmod.embed_text
        ragmod.embed_text = _no_embed
        try:
            out = asyncio.run(retrieve_chunks("折射定律 折射率", chapter_id=1, top_k=2,
                                              run_id=None, mode="hybrid"))
        finally:
            ragmod.embed_text = orig
        self.assertTrue(out)
        texts = [o["text"] for o in out]
        self.assertIn("折射定律 sin i / sin r = n", texts)
        row = db.fetch_one("SELECT * FROM retrieval_runs ORDER BY id DESC LIMIT 1")
        self.assertEqual(row["retrieval_mode"], "keyword_only")
        self.assertEqual(json.loads(row["selected_chunk_ids"]), [o["chunk_id"] for o in out])

    def test_keyword_only_mode(self):
        self._seed()
        from app.rag import retrieve_chunks
        out = asyncio.run(retrieve_chunks("折射定律", lesson_id=1, top_k=2, mode="keyword_only"))
        row = db.fetch_one("SELECT retrieval_mode, candidate_count FROM retrieval_runs ORDER BY id DESC LIMIT 1")
        self.assertEqual(row["retrieval_mode"], "keyword_only")
        self.assertEqual(row["candidate_count"], 2)  # lesson_id=1 过滤后 2 条候选


class TestModelCallAudit(Base):
    def test_audit_on_success_and_failure(self):
        from app.gateway import GatewayClient, set_call_context, reset_call_context
        from app.gateway import _get_engine, reset_engine_for_test
        from app import gateway as gw_mod
        from app.dag import RetryableModelError

        gc = GatewayClient()
        # V5.6.4: gateway.chat 直接委托 engine.chat；monkey-patch 当前引擎实例
        # 切到 live 模式才能进入真实路径（fake 永远 tokens=0）
        os.environ["V5_GATEWAY_MODE"] = "live"
        os.environ["V5_ALLOW_LIVE_MODEL_TESTS"] = "1"
        os.environ["V5_GATEWAY_URL"] = "http://127.0.0.1:8080"
        os.environ["V5_GATEWAY_API_KEY"] = "test-key"
        reset_engine_for_test()

        async def ok_chat(contract, model_id, messages, temperature, max_tokens,
                          response_format):
            return {
                "content": "hi",
                "reasoning_content": "",
                "tokens_in": 5,
                "tokens_out": 2,
                "model": "test-model",
                "elapsed_ms": 1,
            }

        async def fail_chat(contract, model_id, messages, temperature, max_tokens,
                            response_format):
            raise RetryableModelError("网络不可达")

        run_id = db.insert("INSERT INTO workflow_runs (workflow, mode, status) VALUES ('t','solve','running')")
        token = set_call_context(run_id=run_id, node_id="solver", attempt=2,
                                 prompt_name="homework/solver", prompt_version="v1")
        try:
            # 先跑成功用例：临时替换 engine 实例
            mode, engine = _get_engine()
            orig_chat = engine.chat
            engine.chat = ok_chat
            try:
                r = asyncio.run(gc.chat("qwen3_flash",
                                        [{"role": "user", "content": "你好"}],
                                        contract="homework/solver"))
                self.assertEqual(r["tokens_in"], 5)
            finally:
                engine.chat = orig_chat
            # 再跑失败用例
            engine.chat = fail_chat
            try:
                with self.assertRaises(RetryableModelError):
                    asyncio.run(gc.chat("qwen3_flash",
                                        [{"role": "user", "content": "再见"}],
                                        contract="homework/solver"))
            finally:
                engine.chat = orig_chat
        finally:
            reset_call_context(token)
            # 恢复 fake 模式避免污染其他用例
            os.environ["V5_GATEWAY_MODE"] = "fake"
            reset_engine_for_test()

        rows = db.fetch_all("SELECT * FROM model_calls ORDER BY id")
        self.assertEqual(len(rows), 2)
        ok_row, err_row = rows
        self.assertEqual(ok_row["status"], "ok")
        self.assertEqual(ok_row["run_id"], run_id)
        self.assertEqual(ok_row["node_id"], "solver")
        self.assertEqual(ok_row["attempt"], 2)
        self.assertEqual(ok_row["prompt_name"], "homework/solver")
        self.assertEqual(ok_row["tokens_in"], 5)
        self.assertEqual(err_row["status"], "error")
        self.assertTrue(err_row["error_code"])


if __name__ == "__main__":
    unittest.main()
