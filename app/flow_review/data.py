"""体检要用、但量价模块没有的数据：流通市值、龙虎榜、融资融券、板块资金排名、两市成交额。

每一项都允许取不到：取不到就记进 missing，问题对应的那一项会标"数据缺失"，不会编造。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from typing import Callable

LOAD_TIMEOUT = 60   # 龙虎榜冷启动要逐日查，最慢的一项


def _circ_mv(stock) -> dict | None:
    from app.tools.tushare_client import get_pro
    from datetime import datetime, timedelta

    end = datetime.now()
    frame = get_pro().daily_basic(
        ts_code=stock.ts_code,
        start_date=(end - timedelta(days=15)).strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        fields="trade_date,circ_mv,volume_ratio,turnover_rate_f",
    )
    if frame is None or frame.empty:
        return None
    row = frame.sort_values("trade_date").iloc[-1]
    circ = row.get("circ_mv")
    if circ is None or circ != circ or float(circ) <= 0:
        return None
    return {"circ_mv_yi": float(circ) / 1e4}   # 万元 → 亿元


def _toplist(stock) -> dict | None:
    from app.services.series import get_toplist

    return get_toplist(stock, 20)


def _margin(stock) -> dict | None:
    from app.services.series import get_margin

    return get_margin(stock, 20)


def _sector_rank(stock) -> dict | None:
    from app.services import series
    from app.tools.tushare_client import get_pro

    data = series.get_sector(stock, 20)
    board = data.get("board") or {}
    as_of = data.get("latest_trade_date") or ""
    if not board.get("ts_code") or not as_of:
        return None
    frame, _error = series._industry_boards(get_pro(), as_of)
    if frame is None or frame.empty or "net_amount" not in frame.columns:
        return None
    ranked = frame.sort_values("net_amount", ascending=False).reset_index(drop=True)
    hit = ranked.index[ranked["ts_code"].astype(str) == str(board["ts_code"])].tolist()
    if not hit:
        return None
    return {"rank": hit[0] + 1, "total": len(ranked), "name": board.get("name") or ""}


def _market() -> dict | None:
    from app.services.series import get_market

    data = get_market(30)
    items = (data.get("turnover") or {}).get("items") or []
    amounts = [row.get("total") for row in items if row.get("total")]
    if len(amounts) < 10:
        return None
    base = sum(amounts[-21:-1]) / len(amounts[-21:-1]) if len(amounts) > 20 else sum(amounts[:-1]) / len(amounts[:-1])
    return {"turnover_change": amounts[-1] / base - 1} if base else None


def _block_trade(stock) -> dict | None:
    from datetime import datetime, timedelta
    from app.tools.tushare_client import get_pro

    end = datetime.now()
    frame = get_pro().block_trade(
        ts_code=stock.ts_code,
        start_date=(end - timedelta(days=30)).strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
    )
    if frame is None:
        return None
    rows = frame.to_dict(orient="records")
    amount = sum(float(row.get("amount") or 0) for row in rows)   # 万元
    return {"count": len(rows), "amount_yi": amount / 1e4}


LOADERS: dict[str, tuple[str, Callable]] = {
    "circ": ("流通市值", _circ_mv),
    "toplist": ("龙虎榜", _toplist),
    "margin": ("融资融券", _margin),
    "sector_rank": ("板块资金排名", _sector_rank),
    "market": ("两市成交额", lambda stock: _market()),
    "block": ("大宗交易", _block_trade),
}


def load_extra(stock, loaders: dict[str, Callable] | None = None) -> dict:
    """并发取各项补充数据。返回 {"circ": ..., "toplist": ..., "missing": ["大宗交易：…", ...]}。"""
    chosen = loaders if loaders is not None else {key: fn for key, (_label, fn) in LOADERS.items()}
    labels = {key: label for key, (label, _fn) in LOADERS.items()}
    pool = ThreadPoolExecutor(max_workers=max(1, len(chosen)))
    futures = {pool.submit(fn, stock): key for key, fn in chosen.items()}
    done, pending = wait(futures, timeout=LOAD_TIMEOUT)
    pool.shutdown(wait=False, cancel_futures=True)
    out: dict = {"missing": []}
    for future in done:
        key = futures[future]
        try:
            out[key] = future.result()
        except Exception as exc:
            out[key] = None
            out["missing"].append(f"{labels.get(key, key)}：取数失败（{str(exc)[:40]}）")
        if out.get(key) is None and not any(item.startswith(labels.get(key, key)) for item in out["missing"]):
            out["missing"].append(f"{labels.get(key, key)}：没有取到数据")
    for future in pending:
        key = futures[future]
        out[key] = None
        out["missing"].append(f"{labels.get(key, key)}：取数超时")
    return out
