"""港股行情和财务。日线用 hk_daily，估值和报表用 hk_fina_indicator。金额单位是元。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from app.services.financial import report_desc
from app.tools.stock import Stock


def _cell(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            return _cell(value.item())
        except Exception:
            pass
    return value


def _date_text(value) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[:8]


def _window(days: int) -> tuple[str, str]:
    end = datetime.now()
    start = end - timedelta(days=days)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def fetch_daily(ts_code: str, days: int = 420) -> pd.DataFrame:
    from app.tools.tushare_client import get_pro

    start, end = _window(days)
    frame = get_pro().hk_daily(
        ts_code=ts_code,
        start_date=start,
        end_date=end,
        fields="ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount",
    )
    if frame is None or frame.empty:
        return pd.DataFrame()
    return frame.sort_values("trade_date")


def fetch_indicator(ts_code: str, days: int = 900) -> pd.DataFrame:
    from app.tools.tushare_client import get_pro

    start, end = _window(days)
    try:
        frame = get_pro().hk_fina_indicator(
            ts_code=ts_code,
            start_date=start,
            end_date=end,
            fields=(
                "ts_code,end_date,basic_eps,diluted_eps,operate_income,operate_profit,"
                "holder_profit,gross_profit_ratio,net_profit_ratio,roe_avg,roa,"
                "debt_asset_ratio,current_ratio,total_assets,total_liabilities,"
                "total_parent_equity,netcash_operate,netcash_invest,netcash_finance,"
                "pe_ttm,pb_ttm,dividend_rate,total_market_cap"
            ),
        )
    except Exception:
        return pd.DataFrame()
    if frame is None or frame.empty or "end_date" not in frame.columns:
        return pd.DataFrame()
    return frame.sort_values("end_date", ascending=False).drop_duplicates("end_date", keep="first")


def yuan_to_wan(value):
    number = _cell(value)
    if number is None:
        return None
    try:
        return float(number) / 10000
    except (TypeError, ValueError):
        return None


def as_daily_basic(indicator: pd.DataFrame) -> pd.DataFrame | None:
    """收成 A 股 daily_basic 的一行。总市值从元换成万元。"""
    if indicator is None or indicator.empty:
        return None
    row = indicator.iloc[0]
    return pd.DataFrame([{
        "trade_date": _date_text(row.get("end_date")),
        "pe_ttm": _cell(row.get("pe_ttm")),
        "pb": _cell(row.get("pb_ttm")),
        "dv_ttm": _cell(row.get("dividend_rate")),
        "total_mv": yuan_to_wan(row.get("total_market_cap")),
    }])


def as_roe(indicator: pd.DataFrame) -> pd.DataFrame | None:
    if indicator is None or indicator.empty or "roe_avg" not in indicator.columns:
        return None
    rows = []
    for _, row in indicator.iterrows():
        rows.append({
            "end_date": _date_text(row.get("end_date")),
            "roe": _cell(row.get("roe_avg")),
            "update_flag": "1",
        })
    if not rows:
        return None
    return pd.DataFrame(rows)


def statements(indicator: pd.DataFrame) -> tuple[dict, list]:
    income, balance, cashflow, ratios, disclosure = [], [], [], [], []
    if indicator is None or indicator.empty:
        return {"income": [], "balance": [], "cashflow": [], "indicator": []}, []
    for _, row in indicator.head(8).iterrows():
        period = _date_text(row.get("end_date"))
        income.append({
            "report_period": period,
            "basic_eps": _cell(row.get("basic_eps")),
            "diluted_eps": _cell(row.get("diluted_eps")),
            "total_revenue": _cell(row.get("operate_income")),
            "operate_profit": _cell(row.get("operate_profit")),
            "net_profit": _cell(row.get("holder_profit")),
            "net_profit_parent": _cell(row.get("holder_profit")),
            "rd_expense": None,
        })
        balance.append({
            "report_period": period,
            "total_assets": _cell(row.get("total_assets")),
            "total_liabilities": _cell(row.get("total_liabilities")),
            "total_equity": _cell(row.get("total_parent_equity")),
            "capital_reserve": None,
            "surplus_reserve": None,
            "undistributed_profit": None,
        })
        cashflow.append({
            "report_period": period,
            "net_profit": _cell(row.get("holder_profit")),
            "operate_cash_inflow": None,
            "operate_cash_flow": _cell(row.get("netcash_operate")),
            "invest_cash_flow": _cell(row.get("netcash_invest")),
            "financing_cash_flow": _cell(row.get("netcash_finance")),
        })
        ratios.append({
            "report_period": period,
            "roe": _cell(row.get("roe_avg")),
            "roa": _cell(row.get("roa")),
            "gross_profit_margin": _cell(row.get("gross_profit_ratio")),
            "net_profit_margin": _cell(row.get("net_profit_ratio")),
            "debt_to_assets": _cell(row.get("debt_asset_ratio")),
            "current_ratio": _cell(row.get("current_ratio")),
            "eps": _cell(row.get("basic_eps")),
        })
        disclosure.append({
            "report_period": period,
            "report_desc": report_desc(period),
            "ann_date": None,
        })
    return {
        "income": income,
        "balance": balance,
        "cashflow": cashflow,
        "indicator": ratios,
    }, disclosure


def valuation(daily: pd.DataFrame, indicator: pd.DataFrame) -> dict:
    metrics = {
        "trade_date": None,
        "latest_price": None,
        "change": None,
        "pct_chg": None,
        "pre_close": None,
        "pe": None,
        "pe_ttm": None,
        "pb": None,
        "total_mv_yi": None,
        "circ_mv_yi": None,
        "dv_ttm": None,
        "turnover_rate": None,
        "high_52w": None,
        "low_52w": None,
    }
    if daily is not None and not daily.empty and "close" in daily.columns:
        ordered = daily.sort_values("trade_date") if "trade_date" in daily.columns else daily
        latest = ordered.iloc[-1]
        window = ordered.tail(250)
        metrics.update({
            "trade_date": _date_text(latest.get("trade_date")) or None,
            "latest_price": _cell(latest.get("close")),
            "change": _cell(latest.get("change")),
            "pct_chg": _cell(latest.get("pct_chg")),
            "pre_close": _cell(latest.get("pre_close")),
            "high_52w": _cell(window["high"].max()) if "high" in window.columns else None,
            "low_52w": _cell(window["low"].min()) if "low" in window.columns else None,
        })
    card = as_daily_basic(indicator)
    if card is not None:
        row = card.iloc[0]
        total_mv = _cell(row.get("total_mv"))
        metrics.update({
            "pe_ttm": _cell(row.get("pe_ttm")),
            "pb": _cell(row.get("pb")),
            "dv_ttm": _cell(row.get("dv_ttm")),
            "total_mv_yi": round(total_mv / 10000, 2) if total_mv is not None else None,
        })
    return metrics


def financial_result(stock: Stock, price: float | None, daily: pd.DataFrame, indicator: pd.DataFrame) -> dict:
    from app.services.financial import summarize_financials

    report, disclosure = statements(indicator)
    if not any(report.values()):
        raise RuntimeError(f"未获取到 {stock.ts_code} 的港股财务数据")
    market = valuation(daily, indicator)
    if price is None:
        price = market.get("latest_price")
    return {
        "price": price,
        "currency": "HKD",
        "stock_info": {
            "ts_code": stock.ts_code,
            "symbol": stock.code,
            "name": stock.name,
            "industry": stock.industry or "未知",
            "market": "港股",
            "exchange": "HK",
        },
        "analysis_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_source": "tushare_hk",
        "valuation": market,
        "disclosure_info": disclosure,
        "financial_statements": report,
        "analysis": summarize_financials(report, disclosure),
    }
