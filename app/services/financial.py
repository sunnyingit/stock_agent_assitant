"""财务报表。使用 Tushare 的利润表、资产负债表、现金流量表和财务指标。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from app import config
from app.tools import cache
from app.tools.stock import Stock, is_hk

_INCOME_FIELDS = (
    "ts_code,ann_date,f_ann_date,end_date,report_type,basic_eps,diluted_eps,"
    "total_revenue,operate_profit,n_income,n_income_attr_p,rd_exp"
)
_BALANCE_FIELDS = (
    "ts_code,end_date,report_type,total_assets,total_liab,"
    "total_hldr_eqy_exc_min_int,cap_rese,surplus_rese,undistr_porfit"
)
_CASH_FIELDS = (
    "ts_code,end_date,report_type,net_profit,c_inf_fr_operate_a,"
    "n_cashflow_act,n_cashflow_inv_act,n_cash_flows_fnc_act"
)
_INDICATOR_FIELDS = (
    "ts_code,end_date,roe,roa,grossprofit_margin,netprofit_margin,"
    "debt_to_assets,current_ratio,eps"
)


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
    if isinstance(value, float):
        return value
    return value


def _date_text(value) -> str:
    text = "" if value is None else str(value)
    return text[:8]


def report_desc(end_date) -> str:
    text = _date_text(end_date)
    if len(text) < 8:
        return "未知"
    year, month_day = text[:4], text[4:8]
    labels = {"1231": "年报", "0630": "半年报", "0930": "三季报", "0331": "一季报"}
    label = labels.get(month_day)
    if label:
        return f"{year}{label}"
    return f"{year}财报"


def format_yi(num):
    if num is None:
        return None
    try:
        if pd.isna(num):
            return None
    except Exception:
        pass
    value = float(num)
    if abs(value) >= 100000000:
        return round(value / 100000000, 2)
    if abs(value) >= 10000:
        return round(value / 10000, 2)
    return value


def days_gap(period_end, announce_date):
    if not period_end or not announce_date:
        return None
    try:
        period_date = datetime.strptime(_date_text(period_end), "%Y%m%d")
        ann_date = datetime.strptime(_date_text(announce_date), "%Y%m%d")
    except ValueError:
        return None
    return (ann_date - period_date).days


def _window() -> tuple[str, str]:
    end = datetime.now()
    start = end - timedelta(days=900)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _query(method: str, ts_code: str, fields: str) -> pd.DataFrame:
    from app.tools.tushare_client import get_pro

    start_date, end_date = _window()
    frame = getattr(get_pro(), method)(
        ts_code=ts_code,
        start_date=start_date,
        end_date=end_date,
        fields=fields,
    )
    if frame is None or frame.empty:
        return pd.DataFrame()
    if "report_type" in frame.columns:
        consolidated = frame[frame["report_type"].astype(str) == "1"]
        if not consolidated.empty:
            frame = consolidated
    if "end_date" in frame.columns:
        frame = frame.sort_values("end_date", ascending=False)
        frame = frame.drop_duplicates(subset=["end_date"], keep="first")
    return frame.head(8)


def _map_rows(frame: pd.DataFrame, columns: dict[str, str]) -> list[dict]:
    rows = []
    for _, row in frame.iterrows():
        item = {"report_period": _date_text(row.get("end_date"))}
        for key, column in columns.items():
            item[key] = _cell(row.get(column)) if column in frame.columns else None
        rows.append(item)
    return rows


def _statements(ts_code: str) -> tuple[dict, list]:
    income = _query("income", ts_code, _INCOME_FIELDS)
    balance = _query("balancesheet", ts_code, _BALANCE_FIELDS)
    cashflow = _query("cashflow", ts_code, _CASH_FIELDS)
    indicator = _query("fina_indicator", ts_code, _INDICATOR_FIELDS)
    statements = {
        "income": _map_rows(income, {
            "basic_eps": "basic_eps",
            "diluted_eps": "diluted_eps",
            "total_revenue": "total_revenue",
            "operate_profit": "operate_profit",
            "net_profit": "n_income",
            "net_profit_parent": "n_income_attr_p",
            "rd_expense": "rd_exp",
        }),
        "balance": _map_rows(balance, {
            "total_assets": "total_assets",
            "total_liabilities": "total_liab",
            "total_equity": "total_hldr_eqy_exc_min_int",
            "capital_reserve": "cap_rese",
            "surplus_reserve": "surplus_rese",
            "undistributed_profit": "undistr_porfit",
        }),
        "cashflow": _map_rows(cashflow, {
            "net_profit": "net_profit",
            "operate_cash_inflow": "c_inf_fr_operate_a",
            "operate_cash_flow": "n_cashflow_act",
            "invest_cash_flow": "n_cashflow_inv_act",
            "financing_cash_flow": "n_cash_flows_fnc_act",
        }),
        "indicator": _map_rows(indicator, {
            "roe": "roe",
            "roa": "roa",
            "gross_profit_margin": "grossprofit_margin",
            "net_profit_margin": "netprofit_margin",
            "debt_to_assets": "debt_to_assets",
            "current_ratio": "current_ratio",
            "eps": "eps",
        }),
    }
    disclosure = []
    seen = set()
    for _, row in income.iterrows():
        period = _date_text(row.get("end_date"))
        ann_date = _date_text(row.get("ann_date"))
        key = f"{period}_{ann_date}"
        if key in seen:
            continue
        seen.add(key)
        disclosure.append({
            "ts_code": ts_code,
            "report_period": period,
            "report_desc": report_desc(period),
            "ann_date": ann_date or None,
            "f_ann_date": _date_text(row.get("f_ann_date")) or None,
            "report_type": _cell(row.get("report_type")),
            "source": "income",
        })
    return statements, disclosure


def summarize_financials(financial_data: dict, disclosure_info: list) -> dict:
    analysis = {
        "disclosure_summary": [],
        "financial_trend": {},
        "key_metrics": {},
    }
    for item in disclosure_info:
        analysis["disclosure_summary"].append({
            "report_period": item.get("report_period"),
            "report_desc": item.get("report_desc"),
            "announcement_date": item.get("ann_date"),
            "days_from_period_end": days_gap(item.get("report_period"), item.get("ann_date")),
        })

    income_data = sorted(
        financial_data.get("income") or [],
        key=lambda item: item.get("report_period") or "",
    )
    revenues = [
        {"period": item["report_period"], "revenue_yi": format_yi(item.get("total_revenue"))}
        for item in income_data if item.get("total_revenue") is not None
    ]
    profits = [
        {"period": item["report_period"], "net_profit_yi": format_yi(item.get("net_profit_parent"))}
        for item in income_data if item.get("net_profit_parent") is not None
    ]
    eps_list = [
        {"report_period": item["report_period"], "basic_eps": item.get("basic_eps")}
        for item in income_data if item.get("basic_eps") is not None
    ]
    if revenues:
        analysis["financial_trend"]["revenue_trend"] = revenues
    if profits:
        analysis["financial_trend"]["net_profit_trend"] = profits
    if eps_list:
        analysis["financial_trend"]["eps_trend"] = eps_list

    indicator_data = sorted(
        financial_data.get("indicator") or [],
        key=lambda item: item.get("report_period") or "",
    )
    if indicator_data:
        latest = indicator_data[-1]
        analysis["key_metrics"] = {
            "latest_period": latest.get("report_period"),
            "roe": latest.get("roe"),
            "roa": latest.get("roa"),
            "gross_margin": latest.get("gross_profit_margin"),
            "net_margin": latest.get("net_profit_margin"),
            "debt_ratio": latest.get("debt_to_assets"),
            "current_ratio": latest.get("current_ratio"),
            "eps": latest.get("eps"),
        }
    return analysis


def _valuation(ts_code: str) -> dict:
    """最新价、估值和 52 周区间，数据来自 Tushare daily / daily_basic。"""
    from app.tools.tushare_client import get_pro

    metrics = {
        "trade_date": None,
        "latest_price": None,
        "change": None,
        "pct_chg": None,
        "pre_close": None,
        "pe": None,
        "pe_ttm": None,
        "pb": None,
        "roe": None,
        "total_mv_yi": None,
        "circ_mv_yi": None,
        "dv_ttm": None,
        "turnover_rate": None,
        "volume_ratio": None,
        "high_52w": None,
        "low_52w": None,
        "pct_from_high": None,
        "pct_from_low": None,
    }
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=400)).strftime("%Y%m%d")
    try:
        daily = get_pro().daily(
            ts_code=ts_code,
            start_date=start,
            end_date=end,
            fields="trade_date,high,low,close,pre_close,change,pct_chg",
        )
    except Exception:
        return metrics
    if daily is None or daily.empty:
        return metrics

    daily = daily.sort_values("trade_date")
    latest = daily.iloc[-1]
    close = _cell(latest.get("close"))
    high_52w = _cell(daily["high"].max())
    low_52w = _cell(daily["low"].min())
    metrics.update({
        "trade_date": _date_text(latest.get("trade_date")) or None,
        "latest_price": close,
        "change": _cell(latest.get("change")),
        "pct_chg": _cell(latest.get("pct_chg")),
        "pre_close": _cell(latest.get("pre_close")),
        "high_52w": high_52w,
        "low_52w": low_52w,
    })
    if close and high_52w:
        metrics["pct_from_high"] = round((close - high_52w) / high_52w * 100, 2)
    if close and low_52w:
        metrics["pct_from_low"] = round((close - low_52w) / low_52w * 100, 2)

    trade_date = metrics["trade_date"]
    if not trade_date:
        return metrics
    try:
        basic = get_pro().daily_basic(
            ts_code=ts_code,
            trade_date=trade_date,
            fields="pe,pe_ttm,pb,total_mv,circ_mv,dv_ttm,turnover_rate,volume_ratio",
        )
    except Exception:
        return metrics
    if basic is None or basic.empty:
        return metrics
    row = basic.iloc[0]
    total_mv = _cell(row.get("total_mv"))
    circ_mv = _cell(row.get("circ_mv"))
    metrics.update({
        "pe": _cell(row.get("pe")),
        "pe_ttm": _cell(row.get("pe_ttm")),
        "pb": _cell(row.get("pb")),
        "total_mv_yi": round(total_mv / 10000, 2) if total_mv is not None else None,
        "circ_mv_yi": round(circ_mv / 10000, 2) if circ_mv is not None else None,
        "dv_ttm": _cell(row.get("dv_ttm")),
        "turnover_rate": _cell(row.get("turnover_rate")),
        "volume_ratio": _cell(row.get("volume_ratio")),
    })
    return metrics


def _stock_info(stock: Stock) -> dict:
    info = {
        "ts_code": stock.ts_code,
        "symbol": stock.code,
        "name": stock.name,
        "industry": stock.industry or "未知",
        "market": "未知",
        "exchange": stock.ts_code.split(".")[-1],
        "list_date": None,
    }
    try:
        from app.tools.tushare_client import get_pro

        frame = get_pro().stock_basic(
            ts_code=stock.ts_code,
            fields="ts_code,symbol,name,industry,market,exchange,list_date",
        )
    except Exception:
        return info
    if frame is None or frame.empty:
        return info
    row = frame.iloc[0]
    info["name"] = _cell(row.get("name")) or info["name"]
    industry = _cell(row.get("industry"))
    if industry:
        info["industry"] = industry
    market = _cell(row.get("market"))
    if market:
        info["market"] = market
    exchange = _cell(row.get("exchange"))
    if exchange:
        info["exchange"] = exchange
    info["list_date"] = _cell(row.get("list_date"))
    return info


def get_financials(stock: Stock, current_price: float | None = None) -> dict:
    cache_key = f"financial:{stock.ts_code}:{current_price if current_price is not None else ''}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    if is_hk(stock):
        from app.services.hk import fetch_daily, fetch_indicator, financial_result

        result = financial_result(stock, current_price, fetch_daily(stock.ts_code), fetch_indicator(stock.ts_code))
        cache.set(cache_key, result, config.CACHE_TTL["financial"])
        return result

    statements, disclosure = _statements(stock.ts_code)
    if not any(statements.values()):
        raise RuntimeError(f"未获取到 {stock.ts_code} 的财务报表")
    valuation = _valuation(stock.ts_code)
    indicators = statements.get("indicator") or []
    if indicators and valuation.get("roe") is None:
        valuation["roe"] = indicators[0].get("roe")
    price = current_price if current_price is not None else valuation.get("latest_price")
    result = {
        "price": price,
        "stock_info": _stock_info(stock),
        "analysis_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_source": "tushare",
        "valuation": valuation,
        "disclosure_info": disclosure,
        "financial_statements": statements,
        "analysis": summarize_financials(statements, disclosure),
    }
    cache.set(cache_key, result, config.CACHE_TTL["financial"])
    return result
