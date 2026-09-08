"""v5 核心逻辑测试（标准库 unittest，零额外依赖）。

运行：python -m unittest discover -s tests -v
覆盖：reasoning 解析/修复、DAG 异常层级、模型网关状态码映射、
Worker 额度未知安全默认、DB 路径安全、后端可导入与路由数。
"""
import os
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent  # backend/
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)

from app import reasoning
from app.dag import AuthError, BadRequestError, BusinessError, RetryableModelError, LOCAL_MODEL
from app.gateway import _map_status_error


class TestReasoning(unittest.TestCase):
    def test_extract_json_fenced(self):
        self.assertEqual(reasoning.extract_json('```json\n{"a":1}\n```'), {"a": 1})

    def test_extract_json_inline(self):
        self.assertEqual(reasoning.extract_json('正文 {"ok":[1,{"x":2}]} 结尾'), {"ok": [1, {"x": 2}]})

    def test_repair_trailing_comma(self):
        self.assertEqual(reasoning.repair_json_blob('{"a":1,"b":[1,2,],}'), {"a": 1, "b": [1, 2]})

    def test_repair_bad_truncation_returns_none(self):
        self.assertIsNone(reasoning.repair_json_blob('{"a":'))

    def test_clean_thinking(self):
        self.assertEqual(reasoning.clean_minimax_thinking('<thinking>h</thinking>hi'), "hi")


class TestDAGErrors(unittest.TestCase):
    def test_hierarchy(self):
        self.assertTrue(issubclass(RetryableModelError, Exception))
        self.assertTrue(issubclass(AuthError, Exception))
        self.assertTrue(issubclass(BadRequestError, Exception))
        self.assertTrue(issubclass(BusinessError, Exception))

    def test_local_constant(self):
        self.assertEqual(LOCAL_MODEL, "_local_")


class TestGatewayStatusMap(unittest.TestCase):
    def test_bad_request(self):
        with self.assertRaises(BadRequestError):
            _map_status_error(400)

    def test_auth(self):
        with self.assertRaises(AuthError):
            _map_status_error(401)

    def test_retryable(self):
        for code in (429, 502, 503, 504, 500):
            with self.assertRaises(RetryableModelError):
                _map_status_error(code)


class TestWorkersQuotaSafe(unittest.TestCase):
    def test_unknown_usage_safe_default(self):
        from app.workers import _quota_pct
        self.assertEqual(_quota_pct(None), 100.0)
        self.assertEqual(_quota_pct({"available": False}), 100.0)
        self.assertEqual(_quota_pct({"available": True, "data": {"used_5h": 0, "limit_5h": 1200}}), 0.0)
        self.assertEqual(_quota_pct("garbage"), 100.0)


class TestPathSafety(unittest.TestCase):
    def test_missing_material_path_raises(self):
        from app.database import resolve_material_path
        with self.assertRaises(BusinessError):
            resolve_material_path(999999)


class TestAppBoosts(unittest.TestCase):
    def test_import_and_route_count(self):
        from app.main import app
        self.assertGreaterEqual(len(app.routes), 60)


class TestReviewSelfTestFailure(unittest.TestCase):
    """审计修复回归：_self_test 不得静默返回空，网关失败应抛 BusinessError。"""

    def test_gateway_failure_raises_business_error(self):
        import asyncio
        import app.gateway as _gws
        from app.dag import BusinessError

        class FakeGateway:
            async def chat(self, *a, **k):
                raise RuntimeError("网关 500")

        ctx = type("ctx", (), {"outputs": {"aggregator": {"resources": {"confirmed_errors": []}}}})()
        # _self_test 内部 `from .gateway import gateway`,指向 app.gateway.gateway
        orig = _gws.gateway
        _gws.gateway = FakeGateway()
        try:
            async def _go():
                from app.dag_review import _self_test
                return await _self_test(ctx, "deepseek_v4_free")

            with self.assertRaises(BusinessError):
                asyncio.run(_go())
        finally:
            _gws.gateway = orig


if __name__ == "__main__":
    unittest.main()