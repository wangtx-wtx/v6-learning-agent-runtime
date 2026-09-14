"""模型中心：隔离数据库下验证配置写入与真实路由读取。"""
from __future__ import annotations

import asyncio
from unittest import mock

from fastapi.testclient import TestClient

from tests.test_v564_gateway_mode import Base


class TestModelControlCenter(Base):
    def test_seeded_models_and_routes(self):
        from app.models_registry import get_role_models, list_models
        models = {m["id"]: m for m in list_models()}
        self.assertEqual(models["deepseek_v4_free"]["gateway_model"], "DeepSeek-flash")
        self.assertEqual(models["embedding"]["embedding_dimensions"], 1024)
        self.assertEqual(get_role_models("critic")[0], "glm_flash")

    def test_save_model_and_route_changes_runtime_selection(self):
        from app.models_registry import get_model, get_role_models, save_model, set_role_models
        save_model({
            "id": "custom_reviewer", "gateway_model": "custom-review-v1",
            "provider": "custom", "family": "custom", "capability": "text",
            "enabled": True, "cost_unit": "unknown", "notes": "test",
        })
        set_role_models("critic", ["custom_reviewer", "glm_flash"])
        self.assertEqual(get_model("custom_reviewer").gateway_model, "custom-review-v1")
        self.assertEqual(get_role_models("critic"), ["custom_reviewer", "glm_flash"])

    def test_cannot_disable_primary_model(self):
        from app.models_registry import set_model_enabled
        with self.assertRaises(ValueError):
            set_model_enabled("glm_flash", False)

    def test_api_crud_and_route(self):
        from app.main import app
        client = TestClient(app)
        payload = {
            "id": "api_model", "gateway_model": "api-model-v1", "provider": "api",
            "family": "api", "capability": "text", "enabled": True,
            "cost_unit": "CNY/1M tokens", "notes": "api test",
        }
        self.assertEqual(client.put("/api/models/api_model", json=payload).status_code, 200)
        response = client.put("/api/model-routes/critic", json={"model_ids": ["api_model", "glm_flash"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["model_ids"][0], "api_model")

    def test_embedding_dimension_change_blocked_with_existing_vectors(self):
        from app.database import insert
        from app.models_registry import list_models, save_model
        insert("INSERT INTO source_chunks(type,locator,text,embedding) VALUES (?,?,?,?)", ("test", "x", "x", "[0.1]"))
        model = next(m for m in list_models() if m["id"] == "embedding")
        model["embedding_dimensions"] = 768
        with self.assertRaises(ValueError):
            save_model(model)

    def test_homework_ocr_uses_configured_ocr_role(self):
        from app.dag import DAGContext
        from app.dag_homework import resolve_input_node
        from app.models_registry import set_role_models
        set_role_models("ocr", ["deepseek_v4_free", "ocr"])
        ctx = DAGContext({"images": ["data:image/png;base64,AA=="]})
        with mock.patch("app.dag_homework._ocr_image", new=mock.AsyncMock(return_value="1. 测试题")) as call:
            result = asyncio.run(resolve_input_node(ctx, "_local_"))
        self.assertEqual(call.await_args.args[2], "deepseek_v4_free")
        self.assertEqual(result["source"], "ocr")

    def test_specialized_models_cannot_be_used_as_chat_roles(self):
        from app.models_registry import set_role_models
        with self.assertRaises(ValueError):
            set_role_models("solver", ["embedding"])
        with self.assertRaises(ValueError):
            set_role_models("critic", ["rerank"])

    def test_glm_ocr_uses_markdown_result(self):
        from app.dag import DAGContext
        from app.dag_homework import _ocr_image

        fake_gateway = mock.Mock()
        fake_gateway.ocr_document = mock.AsyncMock(
            return_value={"md_results": "# 题目\n求函数的极限"}
        )
        text = asyncio.run(_ocr_image(
            fake_gateway, DAGContext({}), "ocr", "data:image/png;base64,AA=="
        ))
        self.assertEqual(text, "# 题目\n求函数的极限")

    def test_dag_never_resurrects_disabled_source_fallback(self):
        from app.dag import DAG, DAGContext, DAGNode, RetryableModelError
        from app.models_registry import set_model_enabled, set_role_models

        set_role_models("note_writer", ["deepseek_v4_free", "glm_flash"])
        set_model_enabled("qwen3_8_27b", False)
        attempted: list[str] = []

        async def handler(ctx, model):
            attempted.append(model)
            if model == "deepseek_v4_free":
                raise RetryableModelError("force fallback")
            return {"ok": True}

        dag = DAG("disabled-filter-test", "test")
        node = DAGNode(
            "note_writer", "note_writer", handler,
            preferred_models=["deepseek_v4_free"],
            fallback_models=["qwen3_8_27b", "glm_flash"],
        )
        dag.add(node)
        result = asyncio.run(dag.run(DAGContext({"mode": "test"})))

        self.assertTrue(result["note_writer"]["ok"])
        self.assertEqual(attempted, ["deepseek_v4_free", "glm_flash"])
