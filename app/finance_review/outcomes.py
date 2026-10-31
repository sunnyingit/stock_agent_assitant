"""财报解读的回测：每次披露记下规则版结论和各题状态，披露后满 5 个、20 个交易日再回填相对行业指数的超额收益。

回答三件事（设计稿第八节）：每个阈值有没有区分度、"需警惕"的误报率、哪些题和后续收益基本无关（该降权或删掉）。
调阈值只改 config.py 并升 THRESHOLD_VERSION，旧版本的记录因为版本号不同，可以直接对照。
"""

from __future__ import annotations

from datetime import date
from typing import Callable

from app.finance_review import tracking
from app.services.finance_detail import metrics
from app.services.finance_detail.config import THRESHOLD_VERSION

HORIZONS = (5, 20)


def version(prompt_version: str) -> str:
    return f"{THRESHOLD_VERSION}|{prompt_version}"


def record(store, ts_code: str, payload: dict, items: list[dict], source: str, prompt_version: str) -> bool:
    """记一条信号：规则版方向，和每道题的状态（模型的或模板的，以最终展示的为准）。只记第一次。"""
    if store is None:
        return False
    m = payload["inputs"]["metrics"]
    announced = (m["time"] or {}).get("announced")
    if not announced:
        return False
    statuses = {row["key"]: row["status"] for row in items}
    return store.save_finance_signal(
        ts_code, m["report_end"], version(prompt_version), announced, payload["verdict"]["lean"], source, statuses, tracking.to_record(m, statuses),
    )


def default_market(ts_code: str, today: date) -> tuple[list[dict], list[dict]]:
    """个股日线和所在行业指数日线（取近 150 天，足够回填披露后 20 个交易日）。"""
    from app.services.finance_detail.load import _market
    from app.tools.tushare_client import get_pro

    data = _market(get_pro(), ts_code, today)
    return data["bars"], data["sector"]["rows"]


def settle(store, today: date | None = None, market: Callable[[str, date], tuple[list[dict], list[dict]]] | None = None) -> int:
    """回填已经满 5 个或 20 个交易日的信号。返回本次回填的档位数。"""
    today = today or date.today()
    market = market or default_market
    cache: dict[str, tuple] = {}
    count = 0
    for row in store.finance_signals():
        missing = [h for h in HORIZONS if row[f"excess_{h}"] is None and row[f"ret_{h}"] is None]
        if not missing:
            continue
        announced = metrics.parse_ymd(row["announced"])
        if announced is None:
            continue
        bars, sector = cache.setdefault(row["ts_code"], market(row["ts_code"], today))
        forward = metrics.forward_returns(bars, sector, announced, tuple(missing))
        for horizon, window in forward.items():
            store.save_finance_outcome(row["ts_code"], row["report_end"], row["version"], horizon, window["stock"], window["excess"])
            count += 1
    return count


def stats(store, horizon: int = 5) -> list[dict]:
    """按 题 × 状态 汇总披露后的平均超额收益和"方向命中率"。

    命中：偏正面后跑赢行业、需警惕后跑输行业；中性、不适用、数据缺失只计数，不算命中。
    """
    field = f"excess_{horizon}"
    buckets: dict[tuple, dict] = {}
    for row in store.finance_signals():
        excess = row[field]
        if excess is None:
            continue
        for key, status in row["statuses"].items():
            bucket = buckets.setdefault((key, status), {"question": key, "status": status, "n": 0, "sum": 0.0, "hits": 0, "judged": 0})
            bucket["n"] += 1
            bucket["sum"] += excess
            if status in ("support", "warn"):
                bucket["judged"] += 1
                bucket["hits"] += 1 if (excess > 0 if status == "support" else excess < 0) else 0
        lean = row["lean"]
        bucket = buckets.setdefault(("rule", lean), {"question": "规则版方向", "status": lean, "n": 0, "sum": 0.0, "hits": 0, "judged": 0})
        bucket["n"] += 1
        bucket["sum"] += excess
        if lean in ("bullish", "bearish"):
            bucket["judged"] += 1
            bucket["hits"] += 1 if (excess > 0 if lean == "bullish" else excess < 0) else 0
    out = []
    for bucket in buckets.values():
        bucket["avg_excess"] = round(bucket.pop("sum") / bucket["n"], 4)
        bucket["hit_rate"] = round(bucket["hits"] / bucket["judged"], 4) if bucket["judged"] else None
        out.append(bucket)
    return sorted(out, key=lambda item: (item["question"], item["status"]))
