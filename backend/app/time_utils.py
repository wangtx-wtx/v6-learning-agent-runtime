"""统一 UTC 时间模块（V5.5.1 方案 A.3 阶段 1 验收）。

所有新写入时间戳统一使用 UTC ISO 8601（带 'Z' 后缀或 '+00:00'）。
新代码 **禁止** 使用 ``datetime.utcnow()``（naive）；必须用
``datetime.now(timezone.utc)``。

为了兼容迁移期间已存在大量 ``datetime('now','localtime')`` 写入的
历史数据，本模块提供：

- ``now_utc()`` / ``now_utc_iso()``：用于 Python 侧新时间戳。
- ``now_utc_sql()``：用于嵌入到 SQL 语句的 UTC 字面量。
- ``parse_db_ts()``：宽容解析历史与新格式；返回 tz-aware UTC datetime。
- ``iso_to_utc(s)``：把任意历史时间字符串归一为 UTC ISO。

数据库层（database.py / workers/* / recovery.py）的租约写入与过期
判断已统一使用本模块，租约基准不再有时区漂移。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


# 数据库默认存储格式：UTC ISO 8601（带 'Z' 或 '+00:00'）
UTC_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%f+00:00"
UTC_ISO_FORMAT_SEC = "%Y-%m-%dT%H:%M:%S+00:00"


def now_utc() -> datetime:
    """返回 tz-aware UTC datetime。"""
    return datetime.now(timezone.utc)


def now_utc_iso() -> str:
    """返回 ``2026-09-09T12:34:56.789+00:00`` 形式。"""
    return now_utc().strftime(UTC_ISO_FORMAT)


def now_utc_sql_literal() -> str:
    """返回 ``'2026-09-09 12:34:56'`` 形式（SQLite datetime() 字面量）。"""
    return now_utc().strftime("%Y-%m-%d %H:%M:%S")


def iso_to_utc(value: Optional[str]) -> Optional[datetime]:
    """把任意 ISO 8601 / SQLite 字符串解析为 tz-aware UTC datetime。

    - 已是带时区的：直接转 UTC。
    - naive（无时区）：视为 UTC（V5.5.1 后新写入全是 UTC；历史 mixed
      在 lease 比较中我们只关心同一时间基准，因此 legacy local
      字段不参与未来新比较）。
    - 无法解析：返回 None。
    """
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    # 兼容 SQLite 的 "YYYY-MM-DD HH:MM:SS"
    if "T" not in s and " " in s:
        s = s.replace(" ", "T", 1)
    # 兼容末尾 "Z"
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # 退化：尝试裁掉小数秒
        try:
            if "." in s:
                head, dot, tail = s.partition(".")
                tail = tail.split("+")[0].split("-", 1)[0]
                # 保留前 6 位微秒
                s2 = head + "." + tail[:6]
                # 时区
                if "+" in s:
                    tz = s[s.rfind("+"):]
                    s2 += tz
                elif s.count("-") > 2:
                    tz = s[s.rfind("-"):]
                    s2 += tz
                dt = datetime.fromisoformat(s2)
            else:
                raise
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utc_to_local_str(value: Optional[str] | Optional[datetime]) -> str:
    """把 UTC 时间格式化为本地时间字符串（用于 UI 显示）。"""
    dt = value if isinstance(value, datetime) else iso_to_utc(value)
    if dt is None:
        return ""
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def utc_now_plus(seconds: float) -> str:
    """返回当前 UTC + N 秒 的 ISO 字符串（用于租约到期时间）。"""
    from datetime import timedelta
    return (now_utc() + timedelta(seconds=float(seconds))).isoformat()


def utc_now_plus_sql(seconds: float) -> str:
    """返回当前 UTC + N 秒 的 SQLite datetime 字面量。"""
    from datetime import timedelta
    return (now_utc() + timedelta(seconds=float(seconds))).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 兼容性别名：迁移期间旧代码仍可能引用 utcnow()
# ---------------------------------------------------------------------------
def utcnow() -> datetime:  # pragma: no cover - 兼容 shim
    """兼容旧代码的 shim：等价于 ``datetime.now(timezone.utc)``。"""
    return now_utc()
