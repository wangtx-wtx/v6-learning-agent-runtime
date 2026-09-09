"""E.4/E.5/E.6: 真实多进程锁竞争测试（V5.5.1 最终修订）。

与之前版本的关键区别：**子进程调用生产代码** ``InstanceLock(Path).acquire()``，
不在测试里重新实现一套锁流程。

覆盖：
1. 空锁竞争：两个子进程同时用生产 acquire → 严格 1 成功 + 1 RuntimeError。
2. 持锁期间拒绝 + 释放后第三方可获取。
3. **崩溃恢复**：持锁子进程 ``os._exit()``（不 release）→ 新进程可接管。
4. 50 轮压力：每轮严格 1 成功 + 1 拒绝（用生产 acquire）。

结果写独立 per-child result 文件（避免 Windows spawn 下 Queue 竞争 / 并发 append 丢行）。
"""
import multiprocessing as mp
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _child_prod_acquire(lock_path: str, result_file: str, hold_seconds: float,
                        crash_exit: bool = False) -> None:
    """子进程：使用生产 InstanceLock(Path).acquire()。

    成功 → 持有 hold_seconds → 正常 release（除非 crash_exit=True → os._exit）。
    结果写 result_file（独立文件）。"""
    import time as _t
    pid = os.getpid()

    def log(msg):
        with open(result_file, "a", encoding="utf-8") as f:
            f.write(f"[{pid}] {msg}\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass

    # 用生产代码
    from app.instance_lock import InstanceLock  # noqa: E402
    lock = InstanceLock(Path(lock_path))
    try:
        lock.acquire()
    except RuntimeError as e:
        log(f"REJECTED: {e}")
        return
    log("ACQUIRED")
    _t.sleep(hold_seconds)
    if crash_exit:
        # 模拟崩溃：不执行 release，直接 os._exit（OS 自动释放锁）
        log("ABOUT_TO_CRASH")
        try:
            os.fsync(open(result_file, "a").fileno())
        except Exception:
            pass
        os._exit(42)
    # 正常释放
    lock.release()
    log("RELEASED")


def _child_prod_acquire_hold_long(lock_path: str, result_file: str,
                                  hold_seconds: float) -> None:
    """子进程：生产 acquire → 持有 hold_seconds → 正常 release。"""
    import time as _t
    pid = os.getpid()

    def log(msg):
        with open(result_file, "a", encoding="utf-8") as f:
            f.write(f"[{pid}] {msg}\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass

    from app.instance_lock import InstanceLock  # noqa: E402
    lock = InstanceLock(Path(lock_path))
    try:
        lock.acquire()
    except RuntimeError as e:
        log(f"REJECTED: {e}")
        return
    log("ACQUIRED")
    _t.sleep(hold_seconds)
    lock.release()
    log("RELEASED")


class TestInstanceLockConcurrent(unittest.TestCase):
    """真实多进程竞争（生产 InstanceLock）。"""

    def setUp(self):
        self._ctx = mp.get_context("spawn")
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        for p in self.tmp.rglob("*"):
            if p.is_file():
                try: p.unlink()
                except Exception: pass
        try: self.tmp.rmdir()
        except Exception: pass

    def _spawn(self, fn, fn_args):
        p = self._ctx.Process(target=fn, args=fn_args)
        p.start()
        return p

    def _lines(self, files):
        out = []
        for f in files:
            if f.exists():
                out.extend(f.read_text(encoding="utf-8").splitlines())
        return out

    def _count(self, lines, marker):
        return len([l for l in lines if marker in l])

    # ------------------------------------------------------------------ E.4
    def test_two_children_race_for_empty_lock(self):
        """空锁竞争：两个子进程同时用生产 acquire → 严格 1 成功 + 1 拒绝。"""
        subdir = self.tmp / "race"
        subdir.mkdir(exist_ok=True)
        lock_path = subdir / ".instance.lock"
        result_files = [subdir / "r0.txt", subdir / "r1.txt"]

        procs = [
            self._spawn(_child_prod_acquire, (str(lock_path), str(result_files[i]),
                                               0.3, False))
            for i in range(2)
        ]
        time.sleep(0.8)  # 等子进程 spawn + import
        for p in procs:
            p.join(timeout=10.0)
            if p.is_alive():
                p.terminate()

        lines = self._lines(result_files)
        acqu = self._count(lines, "ACQUIRED")
        rej = self._count(lines, "REJECTED")
        self.assertEqual(acqu, 1, f"应 1 ACQUIRED，实际 {acqu}：\n" + "\n".join(lines))
        self.assertEqual(rej, 1, f"应 1 REJECTED，实际 {rej}：\n" + "\n".join(lines))

    # ------------------------------------------------------------------ E.4
    def test_holder_blocks_then_third_acquires(self):
        """持锁期间拒绝；释放后第三方可获取（全生产 acquire）。"""
        subdir = self.tmp / "block"
        subdir.mkdir(exist_ok=True)
        lock_path = subdir / ".instance.lock"
        result_a = subdir / "a.txt"
        result_b = subdir / "b.txt"
        result_c = subdir / "c.txt"

        # A：持有 1.2s
        pa = self._spawn(_child_prod_acquire_hold_long,
                         (str(lock_path), str(result_a), 1.2))
        time.sleep(0.8)
        deadline = time.time() + 5
        while time.time() < deadline:
            if self._count(self._lines([result_a]), "ACQUIRED") == 1:
                break
            time.sleep(0.05)

        # B：A 仍持锁（1.2s 未到），应 REJECTED
        pb = self._spawn(_child_prod_acquire_hold_long,
                         (str(lock_path), str(result_b), 0.1))
        pb.join(timeout=6.0)
        if pb.is_alive():
            pb.terminate()
        lines_b = self._lines([result_b])
        self.assertTrue(self._count(lines_b, "REJECTED") == 1,
                        f"B 持锁期间应 REJECTED：\n" + "\n".join(lines_b))

        # 等 A 释放
        pa.join(timeout=6.0)
        if pa.is_alive():
            pa.terminate()

        # C：A 后面释放，应可 ACQUIRED
        pc = self._spawn(_child_prod_acquire_hold_long,
                         (str(lock_path), str(result_c), 0.2))
        pc.join(timeout=6.0)
        if pc.is_alive():
            pc.terminate()
        lines_c = self._lines([result_c])
        self.assertTrue(self._count(lines_c, "ACQUIRED") == 1,
                        f"A 退出后 C 应 ACQUIRED：\n" + "\n".join(lines_c))

    # ------------------------------------------------------------------ E.6 崩溃恢复
    def test_crash_recovery_exit(self):
        """崩溃恢复：持锁子进程 os._exit()（不 release）→ 新进程可接管。"""
        subdir = self.tmp / "crash"
        subdir.mkdir(exist_ok=True)
        lock_path = subdir / ".instance.lock"
        result_a = subdir / "crash_a.txt"
        result_b = subdir / "crash_b.txt"

        # A：生产 acquire → 持有 → os._exit(42)（模拟崩溃）
        pa = self._spawn(_child_prod_acquire, (str(lock_path), str(result_a),
                                               1.0, True))
        time.sleep(0.8)
        # 等 A acquired
        deadline = time.time() + 6
        while time.time() < deadline:
            if self._count(self._lines([result_a]), "ACQUIRED") == 1:
                break
            time.sleep(0.05)
        # 等 A 崩溃退出
        pa.join(timeout=6.0)
        self.assertEqual(pa.exitcode, 42, f"A 应 os._exit(42)，实际 {pa.exitcode}")

        # 确认 A 崩溃前已获得锁（尚未 RELEASED）
        lines_a = self._lines([result_a])
        self.assertEqual(self._count(lines_a, "ACQUIRED"), 1)
        self.assertEqual(self._count(lines_a, "RELEASED"), 0,
                         "崩溃前不应正常 release")

        # B：用生产 acquire 接管崩溃者释放的锁
        pb = self._spawn(_child_prod_acquire_hold_long,
                         (str(lock_path), str(result_b), 0.3))
        pb.join(timeout=6.0)
        if pb.is_alive():
            pb.terminate()
        lines_b = self._lines([result_b])
        self.assertEqual(self._count(lines_b, "ACQUIRED"), 1,
                         f"崩溃后 B 应能接管：\n" + "\n".join(lines_b))

    # ------------------------------------------------------------------ E.5 50 轮压力
    def test_stress_race_50_rounds(self):
        """50 轮压力：每轮 2 子进程（生产 acquire）严格 1 成功 + 1 拒绝。"""
        n_rounds = 50
        for i in range(n_rounds):
            subdir = self.tmp / f"r{i}"
            subdir.mkdir(exist_ok=True)
            lock_path = subdir / ".instance.lock"
            result_files = [subdir / "x0.txt", subdir / "x1.txt"]
            procs = [
                self._spawn(_child_prod_acquire, (str(lock_path),
                                                   str(result_files[j]),
                                                   0.2, False))
                for j in range(2)
            ]
            time.sleep(0.4)
            for p in procs:
                p.join(timeout=8.0)
                if p.is_alive():
                    p.terminate()
            lines = self._lines(result_files)
            acqu = self._count(lines, "ACQUIRED")
            rej = self._count(lines, "REJECTED")
            if acqu != 1 or rej != 1:
                self.fail(f"Round {i}: 期望 1 ACQUIRED + 1 REJECTED，实际 "
                          f"acquired={acqu}, rejected={rej}。\n" + "\n".join(lines))


if __name__ == "__main__":
    unittest.main()