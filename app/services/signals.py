"""收盘后的个股信号：涨跌停、炸板、20 日和 60 日新高新低。只读日线，所有人一样，不调模型。

涨跌幅限制按板块估：科创板、创业板 20%，北交所 30%，ST 5%，其余 10%。炸板指盘中摸到涨停价、收盘没封住。
这是用日线估的，不是交易所的涨跌停价表；极端行情下可能偏一两分。
"""

from __future__ import annotations

EXTREME_WINDOWS = (60, 20)


def limit_pct(ts_code: str, name: str = "") -> float:
    code = ts_code.split(".")[0]
    if "ST" in (name or "").upper():
        return 0.05
    if code.startswith(("688", "689", "300", "301")):
        return 0.20
    if code.startswith(("8", "4", "920")):
        return 0.30
    return 0.10


def compute(ts_code: str, name: str, bars: list[dict]) -> dict:
    """bars：按日期升序的日线 [{trade_date, high, close}]。返回 {trade_date, limit, extreme}，没有信号的为 None。"""
    rows = [bar for bar in bars if bar.get("close")]
    if len(rows) < 2:
        return {"trade_date": "", "limit": None, "extreme": None}
    last, prev = rows[-1], rows[-2]
    pct = limit_pct(ts_code, name)
    up = round(float(prev["close"]) * (1 + pct), 2)
    down = round(float(prev["close"]) * (1 - pct), 2)
    close, high = float(last["close"]), float(last.get("high") or last["close"])
    limit = None
    if close >= up - 0.005:
        limit = {"kind": "limit_up", "close": close}
    elif close <= down + 0.005:
        limit = {"kind": "limit_down", "close": close}
    elif high >= up - 0.005:
        limit = {"kind": "limit_open", "close": close}
    extreme = None
    for window in EXTREME_WINDOWS:
        tail = [float(bar["close"]) for bar in rows[-window:]]
        if len(tail) < window:
            continue
        if close >= max(tail):
            extreme = {"kind": "high", "window_days": window, "close": close}
            break
        if close <= min(tail):
            extreme = {"kind": "low", "window_days": window, "close": close}
            break
    return {"trade_date": str(last.get("trade_date") or ""), "limit": limit, "extreme": extreme}
