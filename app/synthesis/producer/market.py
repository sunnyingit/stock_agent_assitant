"""市场口径：本股和大盘的 20 日涨跌、距一年高点。统一从量价模块已对齐的序列里算，保证只有一个口径。"""

from __future__ import annotations


def _compound(changes_pct: list) -> float | None:
    values = [value for value in changes_pct if value is not None]
    if not values:
        return None
    acc = 1.0
    for value in values:
        acc *= 1 + float(value) / 100
    return round(acc - 1, 4)


def market_facts(aligned: dict | None) -> dict:
    """返回 {fact_id: 值}；序列不够长时对应的键缺失。"""
    if not aligned:
        return {}
    bars = aligned.get("bars") or []
    facts: dict = {}
    # 综合判断里的 market.*_20d 固定看 20 日，和量价主窗口（FLOW_WINDOW）分开
    window = 20
    if len(bars) >= window:
        ret = _compound([row.get("chg") for row in bars[-window:]])
        if ret is not None:
            facts["market.stock_ret_20d"] = ret
    closes = [row.get("close") for row in bars[-250:] if row.get("close")]
    if len(closes) >= 60:
        facts["market.from_year_high"] = round(closes[-1] / max(closes) - 1, 4)
    index_rows = (aligned.get("index") or {}).get("rows") or []
    dates = {row.get("date") for row in bars[-window:]}
    chosen = [row.get("chg") for row in index_rows if row.get("date") in dates]
    if len(chosen) >= window - 2:
        ret = _compound(chosen)
        if ret is not None:
            facts["market.index_ret_20d"] = ret
    return facts
