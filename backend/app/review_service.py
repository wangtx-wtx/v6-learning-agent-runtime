"""
复习作答与掌握度闭环（V5.5 方案 5.3/5.4）。

简化 SM-2 规则（方案 5.4 定版）：
- 用户自评 self_rating ∈ 0..5，只影响下次复习间隔；
  0/1 → 1 天；2 → 3 天；3 → ×1.8；4 → ×2.5；5 → ×3.2；夹在 [1, 180] 天；
- 掌握度只由作答正误驱动：对 +0.15，错 -0.25，夹在 [0, 1]；
- 同一错题连续 2 次答错 → 间隔重置为 1 天（连续错误重置）；
- 每次作答写完整历史（review_attempts：before/after 快照），不覆盖历史。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

MIN_INTERVAL = 1.0
MAX_INTERVAL = 180.0
MASTERY_STEP_CORRECT = 0.15
MASTERY_STEP_WRONG = -0.25


def normalize_answer(s: str) -> str:
    """答案归一化：去空白/标点，转小写。"""
    return re.sub(r"[\s，。,.、;；:：!！?？'\"“”‘’()（）\[\]【】]", "", (s or "")).lower()


def grade_answer(expected: str, user: str) -> bool:
    """确定性判分：归一化后相等，或（较长期望答案）被完整包含于作答。"""
    e, u = normalize_answer(expected), normalize_answer(user)
    if not e:
        return False
    return e == u or (len(e) >= 4 and e in u)


def next_interval_days(prev_interval: float, rating: int, consecutive_wrong: int) -> float:
    """简化 SM-2 间隔（天）。self_rating 只影响间隔。"""
    base = max(float(prev_interval or 0), 1.0)
    r = max(0, min(5, int(rating)))
    if r <= 1:
        iv = 1.0
    elif r == 2:
        iv = 3.0
    elif r == 3:
        iv = base * 1.8
    elif r == 4:
        iv = base * 2.5
    else:
        iv = base * 3.2
    if consecutive_wrong >= 2:
        iv = MIN_INTERVAL  # 连续错误重置
    return round(min(MAX_INTERVAL, max(MIN_INTERVAL, iv)), 4)


def next_mastery(prev: float, is_correct: bool) -> float:
    """掌握度只由正误驱动，夹在 [0,1]。"""
    return round(min(1.0, max(0.0, float(prev or 0) + (MASTERY_STEP_CORRECT if is_correct else MASTERY_STEP_WRONG))), 4)


def consecutive_wrong_count(prior_attempts: list) -> int:
    """统计（按时间正序的）历史作答中，末尾连续答错的次数。"""
    n = 0
    for a in reversed(prior_attempts):
        if int(a.get("is_correct") or 0) == 0:
            n += 1
        else:
            break
    return n


def current_interval_days(next_review_at: str | None) -> float:
    """由下次复习时间反推当前间隔（无记录按首次 1 天）。"""
    if not next_review_at:
        return 1.0
    try:
        dt = datetime.fromisoformat(str(next_review_at))
        days = (dt - datetime.now()).total_seconds() / 86400
        return round(min(MAX_INTERVAL, max(MIN_INTERVAL, days)), 4)
    except Exception:
        return 1.0
