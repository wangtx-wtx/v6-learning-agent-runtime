"""V5.5 阶段H 测试：快照恢复闭环 + Fake Gateway 契约。"""
import sqlite3
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


class TestRestoreSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = self.tmp / "v5.db"
        db.configure_db(self.db_path)
        db.init_db()

    def tearDown(self):
        db.reset_connections()

    def test_full_restore_cycle(self):
        import shutil
        from tools.restore_snapshot import restore_snapshot, validate_snapshot

        # 1) 写入数据并生成快照
        db.insert("INSERT INTO courses (name) VALUES ('快照前课程')")
        snapshot = self.tmp / "snap.db"
        src = sqlite3.connect(str(self.db_path))
        dst = sqlite3.connect(str(snapshot))
        src.backup(dst)
        dst.close()
        src.close()

        # 2) 破坏当前库（删除课程）
        db.execute("DELETE FROM courses")
        self.assertEqual(db.fetch_all("SELECT * FROM courses"), [])

        # 3) 校验快照
        info = validate_snapshot(snapshot)
        self.assertEqual(info["integrity"], "ok")
        self.assertIn(7, info["migrations"])

        # 4) 恢复 → 数据回来
        result = restore_snapshot(snapshot, db_path=self.db_path)
        self.assertFalse(result["dry_run"])
        self.assertTrue(result["pre_backup"])
        row = db.fetch_one("SELECT name FROM courses")
        self.assertEqual(row["name"], "快照前课程")

    def test_validate_rejects_garbage(self):
        import os
        from tools.restore_snapshot import validate_snapshot
        bad = self.tmp / "bad.db"
        bad.write_bytes(b"not a database")
        with self.assertRaises(ValueError):
            validate_snapshot(bad)
        os.unlink(bad)

    def test_validate_rejects_db_without_migrations(self):
        from tools.restore_snapshot import validate_snapshot
        plain = self.tmp / "plain.db"
        conn = sqlite3.connect(str(plain))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        conn.close()
        with self.assertRaises(ValueError):
            validate_snapshot(plain)


class TestFakeGatewayContract(unittest.TestCase):
    """Fake Gateway 契约测试（方案 16.2）：离线验证 DAG 节点对网关返回结构的使用。"""

    def _fake_gateway(self, script: dict):
        class FakeGateway:
            def __init__(self):
                self.calls = []

            async def chat(self, model_id, messages, temperature=0.7, max_tokens=None,
                           response_format=None, contract=None):
                self.calls.append({"model": model_id, "messages": messages})
                node = messages[0]["content"][:20]
                content = script.get("default", "{}")
                for key, val in script.items():
                    if key != "default" and key in messages[0]["content"] + (messages[1]["content"] if len(messages) > 1 else ""):
                        content = val
                        break
                return {"content": content, "reasoning_content": "", "tokens_in": 10,
                        "tokens_out": 5, "model": f"fake::{model_id}", "elapsed_ms": 1}

            async def embedding_single(self, text):
                return [0.1, 0.2, 0.3]

            async def aclose(self):
                pass
        return FakeGateway()

    def test_gateway_response_contract(self):
        """gateway.chat 契约：content/tokens_in/tokens_out/model 键必须存在——
        DAG 节点全部依赖这些键，Fake 网关按此契约离线跑通节点逻辑。"""
        import asyncio

        async def scenario():
            gw = self._fake_gateway({"default": json.dumps({"score": 0.9, "passed": True, "issues": []})})
            from app.integrations.schemas import CriticOut, parse_model_output
            resp = await gw.chat("fake_model", [
                {"role": "system", "content": "critic prompt"},
                {"role": "user", "content": "note"},
            ])
            # 契约键检查
            for key in ("content", "tokens_in", "tokens_out", "model"):
                self.assertIn(key, resp)
            # 节点层 schema 绑定在 Fake 输出上同样成立
            data = parse_model_output(CriticOut, resp["content"], "critic")
            self.assertTrue(data["passed"])
            # embedding 契约：返回 list[float]
            vec = await gw.embedding_single("test")
            self.assertIsInstance(vec, list)
            self.assertTrue(all(isinstance(x, float) for x in vec))
            return gw

        gw = asyncio.run(scenario())
        self.assertEqual(len(gw.calls), 1)

    def test_note_writer_with_fake_gateway(self):
        """note_writer_node 使用 Fake 网关跑通：外置 prompt + schema 绑定 + 修订输入。"""
        import asyncio
        import json as _json
        from app.dag import DAGContext
        import app.dag_lesson as dl

        note = {"title": "T", "body": "B", "evidence": []}

        class FakeGw:
            async def chat(self, model_id, messages, temperature=0.7, max_tokens=None,
                           response_format=None, contract=None):
                return {"content": _json.dumps(note), "tokens_in": 1, "tokens_out": 1,
                        "model": "fake", "elapsed_ms": 1}

        ctx = DAGContext()
        ctx.set_output("retrieve_context", {"candidates": []})
        ctx.set_output("lesson_outline", {"outline": []})

        orig_gateway = dl.__dict__.get("gateway")
        async def scenario():
            saved = sys.modules.get("app.gateway")
            try:
                # 注入 Fake 网关：note_writer_node 内部 `from .gateway import gateway`
                import types
                fake_module = types.ModuleType("app.gateway")
                fake_module.gateway = FakeGw()
                sys.modules["app.gateway"] = fake_module
                out = await dl.note_writer_node(ctx, "fake_model")
            finally:
                if saved is not None:
                    sys.modules["app.gateway"] = saved
            return out

        out = asyncio.run(scenario())
        self.assertEqual(out["note"]["title"], "T")
        self.assertTrue(out["revised"] is False or out["revised"] is True)
        # 缺少 _critic_issues → 首次撰写路径
        self.assertIn("tokens_in", out)


import json  # noqa: E402

if __name__ == "__main__":
    unittest.main()
