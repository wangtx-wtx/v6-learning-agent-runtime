"""单实例锁（V5.5.1 RC 收尾 D.1）。

**核心机制：持有式 OS 文件锁**（msvcrt / fcntl），文件描述符保持到 lifespan 结束。
锁文件 + JSON 内容**仅作为诊断信息**，不作为互斥正确性的依据。

互斥正确性由操作系统保证：
- 进程持有 fd + OS 锁期间，其他进程 flock 同文件会失败；
- 进程退出时 fd 自动关闭，OS 锁自动释放；
- 不存在基于 JSON 比对/原子 unlink 的 ABA 竞态。

跨平台：
- Windows：``msvcrt.locking(fd, LK_NBLCK, 1)``（锁文件首字节，非阻塞）；
  必须先 ``os.lseek(fd, 0, 0)`` 把文件指针归零，确保所有进程都锁 byte 0，
  否则 msvcrt 会按 current position 锁不同区域，互斥失效。
- POSIX：``fcntl.flock(fd, LOCK_EX | LOCK_NB)``（与位置无关，锁整个 fd）。

PID 复用风险（D.5）：
- 旧 PID 死亡后，OS 锁自动释放；新进程用同一 PID 启动可以重新获得锁，
  但**不**会读旧 JSON 内容（因为它从未读过，直接新建）。这与"新进程正常启动"
  行为一致，不造成问题。
- 但**不**声称"start_time 防止 PID 复用"——当前实现没有比较目标 PID 当前的
  进程创建时间与锁内的 start_time。
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

LOCK_FILENAME = ".instance.lock"

# 进程级稳定 token（D.1）：同一进程内 acquire 与 release 看到同一 token。
_PROC_TOKEN: Optional[str] = None

# 平台特定模块
_IS_WINDOWS = sys.platform.startswith("win")
if _IS_WINDOWS:
    import msvcrt  # type: ignore
else:
    import fcntl  # type: ignore


def _lock_path() -> Path:
    from .config import DATA_DIR
    return Path(DATA_DIR) / LOCK_FILENAME


def _is_pid_alive(pid: int) -> bool:
    """判断 PID 是否仍存活（仅用于诊断与 stale 检测，不作为互斥依据）。

    不读取 /proc/self/stat 的 start_time 字段：当前实现无法区分"PID 复用"
    与"旧进程真的还在"。这在本地单用户场景下风险低。
    """
    if pid <= 0:
        return False
    if _IS_WINDOWS:
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                return code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _os_lock_fd(fd: int, non_blocking: bool = True) -> None:
    """在 fd 上获取 OS 文件锁（持有式，进程退出自动释放）。

    关键：Windows 上 msvcrt.locking 锁的是 current position 起的 n 字节。
    因此**必须**先 lseek(0) 把文件指针移到 0，确保所有进程都锁同一区域。
    锁 1 字节在某些 Windows 环境下互斥不充分（实测发现）；锁更大的范围
    （如 16 字节）能保证可靠互斥。
    POSIX flock 锁整个 fd，与位置无关。

    抛出 OSError / RuntimeError 表示锁被其他进程持有。
    """
    if _IS_WINDOWS:
        try:
            os.lseek(fd, 0, 0)
        except OSError:
            pass
        mode = msvcrt.LK_NBLCK if non_blocking else msvcrt.LK_LOCK
        # 锁 16 字节（实测 1 字节在 multiprocessing.spawn 下不可靠）
        try:
            msvcrt.locking(fd, mode, 16)
        except OSError as e:
            raise RuntimeError(f"OS 锁失败: {e}") from e
    else:
        op = fcntl.LOCK_EX
        if non_blocking:
            op |= fcntl.LOCK_NB
        try:
            fcntl.flock(fd, op)
        except OSError as e:
            raise RuntimeError(f"OS 锁失败: {e}") from e


def _os_unlock_fd(fd: int) -> None:
    if _IS_WINDOWS:
        try:
            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 16)
        except OSError:
            pass
    else:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


def _current_proc_info() -> dict:
    """返回本进程唯一标识（带 start_time，但 D.5 不声称用于 PID 复用防护）。"""
    global _PROC_TOKEN
    if _PROC_TOKEN is None:
        _PROC_TOKEN = uuid.uuid4().hex
    pid = os.getpid()
    return {
        "pid": pid,
        "start_time": time.time(),  # 不作为 PID 复用防护
        "host": socket.gethostname(),
        "exe": sys.executable,
        "token": _PROC_TOKEN,
    }


def _read_lock_content(fd: int) -> Optional[dict]:
    try:
        os.lseek(fd, 0, 0)
        raw = os.read(fd, 4096)
        if not raw:
            return None
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return None


class InstanceLock:
    """持有式 OS 文件锁包装（E.2 收尾：简化无 O_EXCL）。

    生命周期：
    - acquire() 在 lifespan 启动时调用一次；
    - fd 持有到 lifespan 结束；
    - release() 在 lifespan finally 中调用，主动 close fd。

    设计（E.2）：
    - 锁文件**永久存在**，不再区分"新文件 / 旧文件"；
    - ``os.open(path, O_CREAT | O_RDWR, 0o644)`` 打开固定文件；
    - **立即**用非阻塞 OS 锁竞争；成功获得后才 truncate + 写诊断 JSON；
    - 不存在 O_EXCL 分支、不存在创建-加锁窗口、无 ABA / 死亡锁接管概念；
    - 进程崩溃后 OS 自动释放锁；下一个进程直接在同一文件上获得锁。
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = path or _lock_path()
        self.fd: Optional[int] = None
        self._info: Optional[dict] = None

    def acquire(self) -> dict:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        info = _current_proc_info()

        # 单个固定锁文件：所有进程打开同一文件 → 竞争同一段 OS 锁
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as e:
            raise RuntimeError(f"无法打开/创建单实例锁文件 {path}: {e}") from e

        # 立即非阻塞尝试 OS 锁：失败 = 另一实例持锁 → 立即拒绝
        try:
            _os_lock_fd(fd, non_blocking=True)
        except RuntimeError as e:
            os.close(fd)
            existing = _read_existing_info(path)
            pid_info = f"pid={existing.get('pid', '?')}" if existing else "未知持有者"
            raise RuntimeError(
                f"已有后端实例在运行 ({pid_info})；请先关闭旧实例。"
            ) from e

        # 获得 OS 锁后才 truncate + 写诊断 JSON（best effort，不影响互斥）
        try:
            os.lseek(fd, 0, 0)
            os.ftruncate(fd, 0)
            payload = json.dumps(info, ensure_ascii=False).encode("utf-8")
            os.write(fd, payload)
            os.fsync(fd)
        except Exception as e:
            # 写诊断失败不销毁已获得的锁——释放锁并重抛，保持一致
            try:
                _os_unlock_fd(fd)
                os.close(fd)
            except Exception:
                pass
            raise RuntimeError(f"锁文件诊断写入失败: {e}") from e

        self.fd = fd
        self._info = info
        logger.info("已获取单实例锁: pid=%s", info["pid"])
        return info

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            _os_unlock_fd(self.fd)
        except Exception as e:
            logger.warning(f"释放 OS 锁失败: {e}")
        try:
            os.close(self.fd)
        except Exception as e:
            logger.warning(f"关闭锁 fd 失败: {e}")
        # 不在 release() 中 unlink 锁文件：
        # - 锁文件永久存在，无"删除/接管"概念；
        # - OS 锁释放后下一个进程可在同一文件上获得锁。
        self.fd = None
        self._info = None
        logger.info("已释放单实例锁")

    def is_held(self) -> bool:
        return self.fd is not None


def _read_existing_info(path: Path) -> Optional[dict]:
    """读取锁文件 JSON 内容（仅用于诊断）。"""
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            raw = f.read()
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return None


# 模块级全局锁实例（lifespan 使用）
_global_lock: Optional[InstanceLock] = None


def acquire_instance_lock() -> dict:
    """获取全局单实例锁。返回锁信息 dict。E.1：无 force 参数。"""
    global _global_lock
    if _global_lock is not None and _global_lock.is_held():
        raise RuntimeError("单实例锁已被本进程持有，请勿重复 acquire")
    _global_lock = InstanceLock()
    return _global_lock.acquire()


def release_instance_lock() -> None:
    """释放全局单实例锁。"""
    global _global_lock
    if _global_lock is None:
        return
    _global_lock.release()
    _global_lock = None


def read_instance_lock() -> Optional[dict]:
    """读取当前锁信息（仅用于 diagnostics，不影响互斥）。

    优先返回本进程持有的 in-memory 锁信息（避免 Windows 上 msvcrt
    LK_LOCK 是 exclusive lock、连同进程内 Path.read_text 都会被拒）。
    退路是直接 open 文件（其他进程持有锁时可能 PermissionError）。
    """
    if _global_lock is not None and _global_lock._info is not None:
        return _global_lock._info
    return _read_existing_info(_lock_path())


@contextlib.contextmanager
def instance_lock():
    info = acquire_instance_lock()
    try:
        yield info
    finally:
        release_instance_lock()
