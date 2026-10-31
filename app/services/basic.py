"""股票基本行情卡片。价格和估值来自 Tushare daily / daily_basic，ROE 来自 fina_indicator。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from app import config
from app.services.fundamental import wan_to_yi
from app.tools import cache
from app.tools.stock import Stock, is_hk

# 约一个 A 股交易年。2026-08-18 的 250 根日线正好覆盖拓普集团卡片上的 52 周高低。
_TRADING_DAYS_52W = 250


def _num(value, digits: int = 2):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return round(number, digits)


def _date_text(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%Y%m%d")
        except Exception:
            pass
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits[:8]


def _extreme(frame: pd.DataFrame, column: str, how: str):
    """high/low 缺失时用当日 close 补上，再取窗口内最大或最小。"""
    close = frame["close"] if "close" in frame.columns else None
    if column in frame.columns:
        series = frame[column]
        if close is not None:
            series = series.fillna(close)
    else:
        series = close
    if series is None:
        return None
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return None
    picked = values.max() if how == "max" else values.min()
    return _num(picked)


def _latest_roe(frame: pd.DataFrame | None):
    """最新报告期的 roe（百分比）。同一期多条时用 update_flag=1。"""
    if frame is None or frame.empty or "roe" not in frame.columns:
        return None, None
    work = frame.copy()
    if "update_flag" in work.columns:
        flagged = work[work["update_flag"].astype(str) == "1"]
        if not flagged.empty:
            work = flagged
    work["_end"] = work["end_date"].map(_date_text) if "end_date" in work.columns else ""
    work["_ann"] = work["ann_date"].map(_date_text) if "ann_date" in work.columns else ""
    work = work.sort_values(["_end", "_ann"], ascending=False)
    for _, row in work.iterrows():
        roe = _num(row.get("roe"))
        if roe is not None:
            return roe, row["_end"] or None
    return None, None


def assemble_basic(
    daily: pd.DataFrame | None,
    basic: pd.DataFrame | None = None,
    indicator: pd.DataFrame | None = None,
    ts_code: str = "",
) -> dict:
    """把日线、估值和财务指标拼成行情卡片。缺字段记为 null，但没有价格就报错。"""
    label = ts_code or "该股票"
    if daily is None or daily.empty or "close" not in daily.columns:
        raise RuntimeError(f"未获取到 {label} 的最新价格")

    ordered = daily.sort_values("trade_date") if "trade_date" in daily.columns else daily
    latest = ordered.iloc[-1]
    price = _num(latest.get("close"))
    if price is None:
        raise RuntimeError(f"未获取到 {label} 的最新价格")

    pre_close = _num(latest.get("pre_close"))
    change = _num(latest.get("change"))
    if change is None and pre_close is not None:
        change = _num(price - pre_close)
    change_pct = _num(latest.get("pct_chg"))
    if change_pct is None and change is not None and pre_close not in (None, 0):
        change_pct = _num(change / pre_close * 100)

    window = ordered.tail(_TRADING_DAYS_52W)
    row = {}
    if basic is not None and not basic.empty:
        picked = basic
        if "trade_date" in basic.columns:
            picked = basic.sort_values("trade_date")
        row = picked.iloc[-1].to_dict()

    # 卡片上的股息率与 dv_ttm 一致（2026-08-18 拓普集团 dv_ttm=0.9589 → 0.96，dv_ratio=1.0157）。
    dividend_yield = _num(row.get("dv_ttm"))
    if dividend_yield is None:
        dividend_yield = _num(row.get("dv_ratio"))
    roe, roe_period = _latest_roe(indicator)

    return {
        "trade_date": _date_text(latest.get("trade_date")) or None,
        "price": price,
        "change": change,
        "change_pct": change_pct,
        "pe_ttm": _num(row.get("pe_ttm")),
        "pb": _num(row.get("pb")),
        "roe": roe,
        "roe_period": roe_period,
        "total_mv_yi": wan_to_yi(row.get("total_mv")),
        "high_52w": _extreme(window, "high", "max"),
        "low_52w": _extreme(window, "low", "min"),
        "dividend_yield": dividend_yield,
        "turnover_rate": _num(row.get("turnover_rate")),
    }


def get_basic(stock: Stock) -> dict:
    cache_key = f"basic:{stock.ts_code}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    if is_hk(stock):
        result = _hk_basic(stock)
        cache.set(cache_key, result, config.CACHE_TTL["basic"])
        return result

    from app.tools.tushare_client import get_pro

    pro = get_pro()
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=420)).strftime("%Y%m%d")
    try:
        daily = pro.daily(
            ts_code=stock.ts_code,
            start_date=start,
            end_date=end,
            fields="trade_date,high,low,close,pre_close,change,pct_chg",
        )
    except Exception as exc:
        raise RuntimeError(f"未获取到 {stock.ts_code} 的最新价格: {exc}") from exc

    basic = None
    trade_date = None
    if daily is not None and not daily.empty and "trade_date" in daily.columns:
        trade_date = _date_text(daily.sort_values("trade_date").iloc[-1]["trade_date"])
    if trade_date:
        try:
            basic = pro.daily_basic(
                ts_code=stock.ts_code,
                trade_date=trade_date,
                fields="trade_date,pe_ttm,pb,dv_ratio,dv_ttm,turnover_rate,total_mv",
            )
        except Exception:
            basic = None

    indicator = None
    try:
        indicator = pro.fina_indicator(
            ts_code=stock.ts_code,
            start_date=(datetime.now() - timedelta(days=900)).strftime("%Y%m%d"),
            end_date=end,
            fields="ann_date,end_date,roe,update_flag",
        )
    except Exception:
        indicator = None

    result = assemble_basic(daily, basic, indicator, ts_code=stock.ts_code)
    cache.set(cache_key, result, config.CACHE_TTL["basic"])
    return result


def _hk_basic(stock: Stock) -> dict:
    from app.services.hk import as_daily_basic, as_roe, fetch_daily, fetch_indicator

    try:
        daily = fetch_daily(stock.ts_code)
    except Exception as exc:
        raise RuntimeError(f"未获取到 {stock.ts_code} 的最新价格: {exc}") from exc
    indicator = fetch_indicator(stock.ts_code)
    result = assemble_basic(daily, as_daily_basic(indicator), as_roe(indicator), ts_code=stock.ts_code)
    result["currency"] = "HKD"
    return result
