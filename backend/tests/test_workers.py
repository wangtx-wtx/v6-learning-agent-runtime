"""V5.5 阶段B Worker 测试（方案 15.2）：
恢复 / 并发 / 取消 / 幂等。使用临时库 + Fake DAG，不访问真实模型。
"""
import asyncio
import io
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import database as db  # noqa: E402


def _mk_dag(node_fn_a, node_fn_b=None):
    from app.dag import DAG, DAGNode
    dag = DAG("test_wf", "test")
    dag.add(DAGNode(name="a", agent_role="", handler=node_fn_a, kind="local"))
    if node_fn_b is not None:
        dag.add(DAGNode(name="b", agent_role="", handler=node_fn_b, kind="local",
                        depends_on=["a"]))
    return dag


def _insert_run(status="queued", workflow="lesson", input_json="{}") -> int:
    return db.insert(
        "INSERT INTO workflow_runs (workflow, status, input_json, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (workflow, status, input_json, "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    )


def _insert_task(run_id: int, status="queued"):
    db.insert(
        "INSERT INTO run_tasks (run_id, status, created_at, updated_at) VALUES (?,?,?,?)",
        (run_id, status, "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    )


class WorkerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        db.configure_db(self.tmp / "test.db")
        db.init_db()
        # TestClient 的 host 不是 127.0.0.1，会被移动端 Token 中间件 403；
        # 测试期间禁用 Token（middleware 每请求读取 config.MOBILE_TOKEN）。
        from app import config as app_config
        self._saved_token = app_config.MOBILE_TOKEN
        app_config.MOBILE_TOKEN = ""

    def tearDown(self):
        db.reset_connections()
        from app import config as app_config
        app_config.MOBILE_TOKEN = self._saved_token

    def _wait_status(self, run_id: int, statuses: tuple, timeout: float = 15.0) -> str:
        deadline = time.time() + timeout
        last = "?"
        while time.time() < deadline:
            row = db.fetch_one("SELECT status FROM workflow_runs WHERE id=?", (run_id,))
            last = (row or {}).get("status") or "?"
            if last in statuses:
                return last
            time.sleep(0.1)
        return last


class TestRecovery(WorkerTestBase):
    def test_running_run_recovers_after_restart(self):
        rid = _insert_run(status="running")
        _insert_task(rid, status="running")
        from app.workers import recover_all
        report = recover_all()
        self.assertEqual(report["run_tasks_recovered"], 1)
        self.assertEqual(db.fetch_one("SELECT status FROM run_tasks")["status"], "queued")
        self.assertEqual(db.fetch_one("SELECT status FROM workflow_runs WHERE id=?", (rid,))["status"], "queued")
        # 恢复标记写入
        self.assertEqual(db.fetch_one("SELECT error FROM run_tasks")["error"], "recovered_after_restart")

    def test_completed_run_not_recovered(self):
        rid = _insert_run(status="completed")
        _insert_task(rid, status="done")
        from app.workers import recover_all
        recover_all()
        self.assertEqual(db.fetch_one("SELECT status FROM run_tasks")["status"], "done")
        self.assertEqual(db.fetch_one("SELECT status FROM workflow_runs WHERE id=?", (rid,))["status"], "completed")

    def test_exceeded_attempts_gives_up(self):
        rid = _insert_run(status="interrupted")
        db.insert(
            "INSERT INTO run_tasks (run_id, status, attempts, max_attempts) VALUES (?,?,?,?)",
            (rid, "interrupted", 3, 3),
        )
        from app.workers import recover_all
        report = recover_all()
        self.assertEqual(report["run_tasks_gave_up"], 1)
        self.assertEqual(db.fetch_one("SELECT status FROM run_tasks")["status"], "failed")

    def test_orphan_queued_run_reenqueued(self):
        _insert_run(status="queued")  # 没有 run_tasks 行
        from app.workers import recover_all
        report = recover_all()
        self.assertEqual(report["runs_reenqueued"], 1)
        self.assertEqual(len(db.fetch_all("SELECT * FROM run_tasks")), 1)


class TestClaimAndEnqueue(WorkerTestBase):
    def test_duplicate_enqueue_is_idempotent(self):
        rid = _insert_run(status="queued")
        from app.workers import enqueue_workflow
        enqueue_workflow(rid)
        enqueue_workflow(rid)
        self.assertEqual(len(db.fetch_all("SELECT * FROM run_tasks WHERE run_id=?", (rid,))), 1)

    def test_claim_is_atomic_and_increments_attempts(self):
        rid = _insert_run(status="queued")
        _insert_task(rid)
        from app.workers.workflow_worker import claim_next_run_task
        t1 = claim_next_run_task()
        t2 = claim_next_run_task()
        self.assertIsNotNone(t1)
        self.assertIsNone(t2)  # 只有一个 queued，第二次领取为空
        row = db.fetch_one("SELECT * FROM run_tasks WHERE run_id=?", (rid,))
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["attempts"], 1)
        self.assertTrue(row["lease_owner"])


class TestCancelSemantics(WorkerTestBase):
    def test_cancel_queued_run(self):
        rid = _insert_run(status="queued")
        _insert_task(rid)
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)  # 不进入 lifespan：manager 不跑，语义确定
        resp = client.post(f"/api/runs/{rid}/cancel")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "cancelled")
        self.assertEqual(db.fetch_one("SELECT status FROM workflow_runs WHERE id=?", (rid,))["status"], "cancelled")
        self.assertEqual(db.fetch_one("SELECT status FROM run_tasks")["status"], "cancelled")

    def test_cancel_terminal_run_conflict(self):
        rid = _insert_run(status="completed")
        from fastapi.testclient import TestClient
        from app.main import app
        resp = TestClient(app).post(f"/api/runs/{rid}/cancel")
        self.assertEqual(resp.status_code, 409)

    def test_cancel_running_run_requests_cancel(self):
        rid = _insert_run(status="running")
        _insert_task(rid, status="running")
        from fastapi.testclient import TestClient
        from app.main import app
        resp = TestClient(app).post(f"/api/runs/{rid}/cancel")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "cancellation_requested")
        self.assertEqual(db.fetch_one("SELECT cancel_requested FROM workflow_runs WHERE id=?", (rid,))["cancel_requested"], 1)


class TestExecution(WorkerTestBase):
    """完整生命周期：TestClient 进入 lifespan → supervisor 真执行。"""

    def _fake_build_flow(self, timeline: list, sleep_s: float = 0.0):
        async def node_a(ctx, model):
            timeline.append(("a_start", time.monotonic()))
            if sleep_s:
                await asyncio.sleep(sleep_s)
            timeline.append(("a_end", time.monotonic()))
            return {"v": 1}

        async def node_b(ctx, model):
            return {"w": 2}
        dag = _mk_dag(node_a, node_b)
        return lambda wf: dag

    def test_queued_run_executes(self):
        timeline = []
        from app.workers import workflow_worker as ww
        orig = getattr(__import__("app.workers", fromlist=["build_flow"]), "build_flow")
        import app.workers as wpkg
        wpkg.build_flow = self._fake_build_flow(timeline)
        ww.build_flow = wpkg.build_flow
        try:
            from fastapi.testclient import TestClient
            from app.main import app
            rid = _insert_run(status="queued")
            from app.workers import enqueue_workflow
            with TestClient(app) as client:
                enqueue_workflow(rid)
                self.assertEqual(self._wait_status(rid, ("completed", "failed")), "completed")
            row = db.fetch_one("SELECT output_json FROM workflow_runs WHERE id=?", (rid,))
            self.assertIn('"b"', row["output_json"])
            nodes = db.fetch_all("SELECT node_name, status FROM run_nodes WHERE run_id=?", (rid,))
            self.assertEqual(sorted((n["node_name"], n["status"]) for n in nodes),
                             [("a", "success"), ("b", "success")])
        finally:
            wpkg.build_flow = orig
            ww.build_flow = orig

    def test_two_runs_execute_concurrently(self):
        timeline = []
        import app.workers as wpkg
        from app.workers import workflow_worker as ww
        orig = wpkg.build_flow
        wpkg.build_flow = self._fake_build_flow(timeline, sleep_s=0.6)
        ww.build_flow = wpkg.build_flow
        try:
            from fastapi.testclient import TestClient
            from app.main import app
            r1 = _insert_run(status="queued")
            r2 = _insert_run(status="queued")
            from app.workers import enqueue_workflow
            with TestClient(app) as client:
                t0 = time.monotonic()
                enqueue_workflow(r1)
                enqueue_workflow(r2)
                s1 = self._wait_status(r1, ("completed", "failed"))
                s2 = self._wait_status(r2, ("completed", "failed"), timeout=20)
                elapsed = time.monotonic() - t0
            self.assertEqual(s1, "completed")
            self.assertEqual(s2, "completed")
            # 串行执行需要 >= 1.2s（两个 0.6s 节点）；真并发应明显更短
            self.assertLess(elapsed, 1.15, f"两个 run 未并发执行（耗时 {elapsed:.2f}s）")
        finally:
            wpkg.build_flow = orig
            ww.build_flow = orig

    def test_retry_creates_immutable_child_run(self):
        from fastapi.testclient import TestClient
        from app.main import app
        import app.workers as wpkg
        orig = wpkg.build_flow
        wpkg.build_flow = self._fake_build_flow([])
        parent = _insert_run(status="failed", input_json='{"x": 1}')
        client = TestClient(app)  # 无 lifespan，仅测端点语义
        try:
            resp = client.post(f"/api/runs/{parent}/retry",
                               json={"from_node": "a", "reuse_successful_dependencies": True})
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertNotEqual(body["run_id"], parent)
            child = db.fetch_one("SELECT * FROM workflow_runs WHERE id=?", (body["run_id"],))
            self.assertEqual(child["parent_run_id"], parent)
            self.assertEqual(child["status"], "queued")
            self.assertIn("_retry", child["input_json"])
            # 原运行不可变
            p = db.fetch_one("SELECT status, output_json FROM workflow_runs WHERE id=?", (parent,))
            self.assertEqual(p["status"], "failed")
        finally:
            wpkg.build_flow = orig

    def test_retry_unknown_node_rejected(self):
        from fastapi.testclient import TestClient
        from app.main import app
        import app.workers as wpkg
        orig = wpkg.build_flow
        wpkg.build_flow = self._fake_build_flow([])
        parent = _insert_run(status="failed")
        try:
            resp = TestClient(app).post(f"/api/runs/{parent}/retry", json={"from_node": "nope"})
            self.assertEqual(resp.status_code, 400)
        finally:
            wpkg.build_flow = orig


if __name__ == "__main__":
    unittest.main()
