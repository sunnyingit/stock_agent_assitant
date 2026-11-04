"""把日线、资金流、板块和指数对齐成同一套单位。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from app import config
from app.tools import cache
from app.tools.stock import Stock

_HISTORY = 520


def qfq(price, factor, latest):
    if price is None or factor in (None, 0) or latest in (None, 0):
        return price
    return round(float(price) * float(factor) / float(latest), 4)


def lots_to_shares(volume):
    if volume is None:
        return None
    return float(volume) * 100


def wan_to_yuan(amount):
    if amount is None:
        return None
    return float(amount) * 10000


def qian_to_yuan(amount):
    if amount is None:
        return None
    return float(amount) * 1000


def load_aligned(stock: Stock) -> dict:
    key = f"volume_flow:aligned:{stock.ts_code}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    from app.tools.tushare_client import get_pro

    pro = get_pro()
    start, end = _span(_HISTORY)
    daily = _frame(lambda: pro.daily(
        ts_code=stock.ts_code,
        start_date=start,
        end_date=end,
        fields="trade_date,open,high,low,close,pct_chg,vol,amount",
    ))
    if daily.empty:
        raise RuntimeError(f"没有查到 {stock.ts_code} 的日线数据")
    basic = _frame(lambda: pro.daily_basic(
        ts_code=stock.ts_code,
        start_date=start,
        end_date=end,
        fields="trade_date,turnover_rate",
    ))
    factor = _frame(lambda: pro.adj_factor(ts_code=stock.ts_code, start_date=start, end_date=end))
    bars = _bars(daily, basic, factor)
    flow = _flow(stock)
    sector = _sector(stock)
    index = _index(pro, start, end)
    margin = _margin(stock)
    percentile = _margin_percentile(pro, stock.ts_code, margin)
    aligned = {
        "name": stock.name,
        "bars": bars,
        "flow": flow,
        "sector": sector,
        "index": index,
        "margin": margin,
        "margin_percentile": percentile,
        "as_of": bars[-1]["date"] if bars else "",
    }
    cache.set(key, aligned, config.CACHE_TTL["volume_flow"])
    return aligned


def _bars(daily: pd.DataFrame, basic: pd.DataFrame, factor: pd.DataFrame) -> list[dict]:
    frame = _dated(daily)
    if not basic.empty:
        frame = frame.merge(_dated(basic)[["trade_date", "turnover_rate"]], on="trade_date", how="left")
    if not factor.empty and "adj_factor" in factor.columns:
        frame = frame.merge(_dated(factor)[["trade_date", "adj_factor"]], on="trade_date", how="left")
    frame = frame.sort_values("trade_date")
    latest = frame["adj_factor"].dropna().iloc[-1] if "adj_factor" in frame.columns and frame["adj_factor"].notna().any() else None
    rows = []
    for record in frame.to_dict(orient="records"):
        date = _iso(record.get("trade_date"))
        factor_value = _num(record.get("adj_factor"))
        close = _num(record.get("close"))
        high = _num(record.get("high"))
        low = _num(record.get("low"))
        rows.append({
            "date": date,
            "close": qfq(close, factor_value, latest) if latest else close,
            "high": qfq(high, factor_value, latest) if latest else high,
            "low": qfq(low, factor_value, latest) if latest else low,
            "volume": lots_to_shares(_num(record.get("vol"))),
            "amount": qian_to_yuan(_num(record.get("amount"))),
            "chg": _num(record.get("pct_chg")),
            "turnover": _num(record.get("turnover_rate")),
        })
    return [row for row in rows if row["date"]]


def _flow(stock: Stock) -> list[dict]:
    try:
        from app.services.series import get_flow_daily

        payload = get_flow_daily(stock, 250)
    except Exception:
        return []
    rows = []
    for item in payload.get("items") or []:
        rows.append({
            "date": _iso(item.get("trade_date")),
            "super_large": wan_to_yuan(item.get("buy_elg_amount")),
            "large": wan_to_yuan(item.get("buy_lg_amount")),
            "medium": wan_to_yuan(item.get("buy_md_amount")),
            "small": wan_to_yuan(item.get("buy_sm_amount")),
        })
    return rows


def _sector(stock: Stock) -> dict:
    try:
        from app.services.series import get_sector

        payload = get_sector(stock, 80)
    except Exception:
        return {"name": "", "rows": []}
    board = payload.get("board") or {}
    rows = []
    for item in payload.get("items") or []:
        rows.append({
            "date": _iso(item.get("trade_date")),
            "chg": item.get("pct_change"),
            "net": item.get("net_amount"),
            "rate": item.get("net_amount_rate"),
        })
    return {"name": board.get("name") or "", "rows": rows}


def _index(pro, start: str, end: str) -> dict:
    frame = _frame(lambda: pro.index_daily(
        ts_code="000300.SH",
        start_date=start,
        end_date=end,
        fields="trade_date,pct_chg",
    ))
    rows = []
    for record in _dated(frame).to_dict(orient="records"):
        rows.append({"date": _iso(record.get("trade_date")), "chg": _num(record.get("pct_chg"))})
    return {"name": "沪深300", "rows": [row for row in rows if row["date"]]}


def _margin(stock: Stock) -> list[dict]:
    try:
        from app.services.series import get_margin

        payload = get_margin(stock, 20)
    except Exception:
        return []
    return [
        {"date": _iso(item.get("trade_date")), "rzye": item.get("rzye")}
        for item in payload.get("items") or []
        if item.get("trade_date")
    ]


def _margin_percentile(pro, ts_code: str, margin: list[dict]):
    if len(margin) < 11 or margin[-1].get("rzye") in (None, 0) or margin[-11].get("rzye") in (None, 0):
        return None
    latest = margin[-1]["date"].replace("-", "")
    prior = margin[-11]["date"].replace("-", "")
    key = f"margin:cross:{latest}:{prior}"
    cached = cache.get(key)
    if cached is None:
        try:
            current = _frame(lambda: pro.margin_detail(trade_date=latest, fields="ts_code,rzye"))
            previous = _frame(lambda: pro.margin_detail(trade_date=prior, fields="ts_code,rzye"))
        except Exception:
            return None
        if current.empty or previous.empty:
            return None
        old = {str(row.ts_code): row.rzye for row in previous.itertuples() if row.rzye not in (None, 0)}
        changes = {}
        for row in current.itertuples():
            base = old.get(str(row.ts_code))
            if base in (None, 0) or row.rzye is None:
                continue
            changes[str(row.ts_code)] = float(row.rzye) / float(base) - 1
        if not changes:
            return None
        cached = changes
        cache.set(key, cached, config.CACHE_TTL["margin"])
    value = cached.get(ts_code)
    if value is None:
        return None
    below = sum(1 for item in cached.values() if item < value)
    return below / len(cached)


def _dated(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return pd.DataFrame()
    out = frame.copy()
    out["trade_date"] = out["trade_date"].map(lambda value: "".join(ch for ch in str(value) if ch.isdigit())[:8])
    return out[out["trade_date"] != ""]


def _frame(fn) -> pd.DataFrame:
    try:
        frame = fn()
    except Exception:
        return pd.DataFrame()
    if frame is None or not isinstance(frame, pd.DataFrame):
        return pd.DataFrame()
    return frame


def _span(trading_days: int) -> tuple[str, str]:
    end = datetime.now()
    start = end - timedelta(days=int(trading_days * 1.7) + 20)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _iso(value) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())[:8]
    if len(digits) < 8:
        return ""
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"


def _num(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number
