"""战绩：回填收盘后那份结论的 5/10/20 日后涨跌，按 来源 × 强度 × 是否分歧 统计命中率。

战绩中心要回答的问题：强度分档是否单调（"明显"是否比"比较明显"更准）；
分歧样本里，规则版和 Agent 版谁更常对。
"""

from __future__ import annotations

from typing import Callable

from app.synthesis import policy
from app.synthesis.store import Store


def default_series(ts_code: str) -> list[dict]:
    from app.services.series import get_bars
    from app.tools.stock import resolve_stock

    stock = resolve_stock(ts_code)
    if stock is None:
        return []
    items = get_bars(stock, 250).get("items") or []
    rows = []
    for item in items:
        digits = "".join(ch for ch in str(item.get("trade_date") or "") if ch.isdigit())[:8]
        if len(digits) == 8 and item.get("close"):
            rows.append({"date": f"{digits[:4]}-{digits[4:6]}-{digits[6:]}", "close": float(item["close"])})
    return sorted(rows, key=lambda row: row["date"])


def settle(store: Store, series: Callable[[str], list[dict]] | None = None) -> int:
    """series(ts_code) 返回按日期升序的 [{date, close}]。返回本次回填的条数。"""
    series = series or default_series
    cache: dict[str, list[dict]] = {}
    count = 0
    for horizon in policy.OUTCOME_HORIZONS:
        for run in store.unsettled(horizon):
            rows = cache.setdefault(run["ts_code"], series(run["ts_code"]))
            index = next((i for i, row in enumerate(rows) if row["date"] == run["trade_date"]), None)
            if index is None or index + horizon >= len(rows):
                continue
            ret = rows[index + horizon]["close"] / rows[index]["close"] - 1
            store.save_outcome(run["id"], horizon, round(ret, 6))
            count += 1
    return count
