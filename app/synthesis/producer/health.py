"""输入体检：在调用任何模型之前，先把单位、日期和跨模块口径不一致的维度挡掉。

Agent 会认真分析错误的数据，并给出很自信的错误结论，所以这一步必须在它之前。
"""

from __future__ import annotations

from datetime import date, datetime

from app.synthesis import policy
from app.synthesis.calendar import last_close_date, parse_day, workday_gap
from app.synthesis.registry import REGISTRY
from app.synthesis.types import STATUS_INVALID, STATUS_OK, STATUS_STALE, Snapshot

PCT_LIMIT = 3.0   # 20 日涨跌超过 300% 基本是单位错了（把百分数当成小数）


def inspect(snaps: list[Snapshot], market: dict, now: datetime) -> dict:
    """原地修改 snaps 的状态，返回体检记录。"""
    expected = last_close_date(now)
    notes: list[dict] = []
    for snap in snaps:
        if snap.status != STATUS_OK:
            continue
        _freshness(snap, expected, notes)
    for snap in snaps:
        if snap.status == STATUS_OK:
            _units(snap, notes)
    by_key = {snap.key: snap for snap in snaps}
    _cross(by_key, market, notes)
    return {"expected_as_of": expected.isoformat(), "notes": notes}


def fresh(snaps: list[Snapshot], now: datetime) -> bool:
    """完成的维度数据日期是否都不早于最近一个收盘日。"""
    expected = last_close_date(now)
    done = [snap for snap in snaps if snap.status == STATUS_OK]
    if not done:
        return False
    for snap in done:
        day = parse_day(snap.as_of)
        if day is None or day < expected:
            return False
    return True


def lag_days(as_of: str, expected: date) -> int | None:
    day = parse_day(as_of)
    if day is None:
        return None
    return workday_gap(day, expected)


def _freshness(snap: Snapshot, expected: date, notes: list) -> None:
    gap = lag_days(snap.as_of, expected)
    if gap is None:
        snap.status = STATUS_INVALID
        snap.reason = "没有数据日期"
        notes.append({"dim": snap.key, "check": "date", "detail": snap.reason})
    elif gap > policy.STALE_TRADING_DAYS:
        snap.status = STATUS_STALE
        snap.reason = f"数据停在 {gap} 个工作日前"
        notes.append({"dim": snap.key, "check": "stale", "detail": snap.reason})


def _units(snap: Snapshot, notes: list) -> None:
    for fact_id, value in snap.facts.items():
        defn = REGISTRY.get(fact_id)
        if defn is not None and defn.kind == "pct" and isinstance(value, (int, float)) and abs(value) > PCT_LIMIT:
            snap.status = STATUS_INVALID
            snap.reason = "涨跌幅数值异常，疑似单位不一致"
            notes.append({"dim": snap.key, "check": "unit", "detail": f"{fact_id}={value}"})
            return


def _cross(by_key: dict[str, Snapshot], market: dict, notes: list) -> None:
    """同一个口径在不同模块里的数字必须对得上。"""
    tolerance = policy.RET_TOLERANCE
    flow = by_key.get("volume_flow")
    own = market.get("market.stock_ret_20d")
    if flow is not None and flow.status == STATUS_OK and own is not None:
        theirs = flow.flags.get("window_ret")
        if theirs is not None and abs(theirs - own) > tolerance:
            flow.status = STATUS_INVALID
            flow.reason = "量价窗口涨幅和统一口径对不上"
            notes.append({"dim": "volume_flow", "check": "cross_ret", "detail": f"{theirs} vs {own}"})
    index = market.get("market.index_ret_20d")
    if index is None:
        return
    for key in ("volume_flow", "sector"):
        snap = by_key.get(key)
        if snap is None or snap.status != STATUS_OK:
            continue
        theirs = snap.flags.get("index_ret")
        if theirs is not None and abs(theirs - index) > tolerance:
            snap.status = STATUS_INVALID
            snap.reason = "大盘涨跌和统一口径对不上"
            notes.append({"dim": key, "check": "cross_index", "detail": f"{theirs} vs {index}"})
