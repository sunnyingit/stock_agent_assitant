"""基本面。使用 Tushare 的日线、估值、周线、股东增减持和公司资料。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from app import config
from app.tools import cache
from app.tools.stock import Stock, is_hk


def _plain(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return None if value != value else value
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "item"):
        try:
            return _plain(value.item())
        except Exception:
            pass
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def _records(frame: pd.DataFrame | None, columns: list[str]) -> list[dict]:
    if frame is None or frame.empty:
        return []
    present = [column for column in columns if column in frame.columns]
    if not present:
        return []
    return [_plain(row) for row in frame[present].to_dict(orient="records")]


def wan_to_yi(value):
    """Tushare 市值单位是万元。"""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return round(float(value) / 10000, 2)


def _trade_window(days_back: int) -> tuple[str, str]:
    from app.tools.tushare_client import get_pro

    end = datetime.now().strftime("%Y%m%d")
    start_cal = (datetime.now() - timedelta(days=days_back + 40)).strftime("%Y%m%d")
    try:
        calendar = get_pro().trade_cal(
            exchange="SSE",
            start_date=start_cal,
            end_date=end,
            is_open="1",
        )
    except Exception:
        calendar = None
    if calendar is not None and not calendar.empty:
        dates = sorted(str(item) for item in calendar["cal_date"].tolist())
        latest = dates[-1]
        start = dates[max(0, len(dates) - days_back)]
        return start, latest
    start = (datetime.now() - timedelta(days=days_back + 7)).strftime("%Y%m%d")
    return start, end


def _company(ts_code: str) -> dict | None:
    from app.tools.tushare_client import get_pro

    pro = get_pro()
    try:
        frame = pro.stock_company(
            ts_code=ts_code,
            fields="com_name,main_business,business_scope,introduction,employees",
        )
        if frame is not None and not frame.empty:
            return _plain(frame.iloc[0].to_dict())
    except Exception:
        pass
    try:
        frame = pro.stock_basic(ts_code=ts_code, fields="name,industry,area")
        if frame is not None and not frame.empty:
            return _plain(frame.iloc[0].to_dict())
    except Exception:
        return None
    return None


def get_fundamental(stock: Stock, days_back: int = 15) -> dict:
    cache_key = f"fundamental:{stock.ts_code}:{days_back}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    if is_hk(stock):
        result = _hk_fundamental(stock, days_back)
        cache.set(cache_key, result, config.CACHE_TTL["fundamental"])
        return result

    from app.tools.tushare_client import get_pro

    pro = get_pro()
    start_date, latest_date = _trade_window(days_back)
    weekly_start = (datetime.strptime(latest_date, "%Y%m%d") - timedelta(days=120)).strftime("%Y%m%d")
    holder_start = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")

    daily = pro.daily(
        ts_code=stock.ts_code,
        start_date=start_date,
        end_date=latest_date,
        fields="ts_code,trade_date,open,high,low,close,vol,amount",
    )
    if daily is None or daily.empty:
        raise RuntimeError(f"未获取到 {stock.ts_code} 的日线数据")
    daily = daily.sort_values("trade_date")
    latest_price = _plain(daily.iloc[-1]["close"])

    valuation = {}
    try:
        basic = pro.daily_basic(
            ts_code=stock.ts_code,
            trade_date=latest_date,
            fields="volume_ratio,total_mv,circ_mv,pe_ttm,pb,pct_chg,pre_close",
        )
        if basic is not None and not basic.empty:
            valuation = _plain(basic.iloc[0].to_dict())
    except Exception:
        valuation = {}

    weekly_rows = []
    try:
        weekly = pro.weekly(
            ts_code=stock.ts_code,
            start_date=weekly_start,
            end_date=latest_date,
            fields="trade_date,close",
        )
        if weekly is not None and not weekly.empty:
            weekly = weekly.sort_values("trade_date").copy()
            weekly["ma7"] = weekly["close"].rolling(window=7).mean()
            weekly_rows = _records(weekly, ["trade_date", "close", "ma7"])
    except Exception:
        weekly_rows = []

    holder_rows = []
    try:
        holders = pro.stk_holdertrade(
            ts_code=stock.ts_code,
            start_date=holder_start,
            end_date=latest_date,
            fields="ann_date,holder_name,in_de,change_vol,change_ratio",
        )
        holder_rows = _records(
            holders,
            ["ann_date", "holder_name", "in_de", "change_vol", "change_ratio"],
        )
    except Exception:
        holder_rows = []

    total_mv = valuation.get("total_mv")
    circ_mv = valuation.get("circ_mv")
    result = {
        "ts_code": stock.ts_code,
        "name": stock.name,
        "industry": stock.industry,
        "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "latest_price": latest_price,
        "pre_close": valuation.get("pre_close"),
        "pct_chg": valuation.get("pct_chg"),
        "volume_ratio": valuation.get("volume_ratio"),
        "pe_ttm": valuation.get("pe_ttm"),
        "pb": valuation.get("pb"),
        "total_mv": total_mv,
        "total_mv_yi": wan_to_yi(total_mv),
        "circ_mv": circ_mv,
        "circ_mv_yi": wan_to_yi(circ_mv),
        "daily": _records(daily, ["trade_date", "open", "high", "low", "close", "vol", "amount"]),
        "weekly_ma7": weekly_rows,
        "shareholder_trade": holder_rows,
        "company": _company(stock.ts_code),
    }
    cache.set(cache_key, result, config.CACHE_TTL["fundamental"])
    return result


def _hk_fundamental(stock: Stock, days_back: int) -> dict:
    from app.services.hk import as_daily_basic, fetch_daily, fetch_indicator

    daily = fetch_daily(stock.ts_code, max(days_back + 30, 60))
    if daily is None or daily.empty:
        raise RuntimeError(f"未获取到 {stock.ts_code} 的日线数据")
    latest = daily.iloc[-1]
    card = as_daily_basic(fetch_indicator(stock.ts_code))
    valuation = {} if card is None else _plain(card.iloc[0].to_dict())
    total_mv = valuation.get("total_mv")
    return {
        "ts_code": stock.ts_code,
        "name": stock.name,
        "industry": stock.industry,
        "currency": "HKD",
        "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "latest_price": _plain(latest.get("close")),
        "pre_close": _plain(latest.get("pre_close")),
        "pct_chg": _plain(latest.get("pct_chg")),
        "volume_ratio": None,
        "pe_ttm": valuation.get("pe_ttm"),
        "pb": valuation.get("pb"),
        "total_mv": total_mv,
        "total_mv_yi": wan_to_yi(total_mv),
        "circ_mv": None,
        "circ_mv_yi": None,
        "daily": _records(daily.tail(days_back), ["trade_date", "open", "high", "low", "close", "vol", "amount"]),
        "weekly_ma7": [],
        "shareholder_trade": [],
        "company": {"name": stock.name, "exchange": "港交所"},
    }
