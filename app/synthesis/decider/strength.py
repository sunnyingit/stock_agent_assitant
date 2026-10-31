"""强度由代码按采信论据的分差换算，不由任何 Agent 直接给：避免"语气很自信"变成"强度很高"。"""

from __future__ import annotations

from app.synthesis import policy


def level_from_spread(spread: int) -> int:
    level = 0
    for floor, value in policy.SPREAD_STEPS:
        if abs(spread) >= floor:
            level = value
    return level


def strength(direction: str, spread: int, done: int, total: int) -> int:
    """不明对应 0；完成的维度不到一半时强度最高为 LOW_COVERAGE_CAP。"""
    if direction not in ("bullish", "bearish"):
        return 0
    level = level_from_spread(spread)
    if done * 2 < total:
        level = min(level, policy.LOW_COVERAGE_CAP)
    return level
