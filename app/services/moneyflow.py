"""资金流。优先使用 Tushare moneyflow_dc，失败后改用 moneyflow。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from app import config
from app.tools import cache
from app.tools.stock import Stock, is_hk

_PERIODS = (1, 3, 5, 7, 10, 15)
_DC_FIELDS = (
    "trade_date,ts_code,name,pct_change,close,net_amount,net_amount_rate,"
    "buy_elg_amount,buy_elg_amount_rate,buy_lg_amount,buy_lg_amount_rate,"
    "buy_md_amount,buy_md_amount_rate,buy_sm_amount,buy_sm_amount_rate"
)
_ALT_FIELDS = (
    "trade_date,ts_code,buy_sm_amount,sell_sm_amount,buy_md_amount,sell_md_amount,"
    "buy_lg_amount,sell_lg_amount,buy_elg_amount,sell_elg_amount,net_mf_amount"
)


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


def _sum(frame: pd.DataFrame, column: str) -> float:
    if column not in frame.columns:
        return 0.0
    return float(frame[column].fillna(0).sum())


def _records(frame: pd.DataFrame, columns: list[str]) -> list[dict]:
    present = [column for column in columns if column in frame.columns]
    if not present:
        return []
    return _plain(frame[present].to_dict(orient="records"))


def summarize_moneyflow(df: pd.DataFrame, period_days: int) -> dict | None:
    if df is None or df.empty:
        return None
    period_df = df.head(period_days)
    if period_df.empty:
        return None

    total_net = _sum(period_df, "net_amount")
    pct_changes = []
    if "pct_change" in period_df.columns:
        pct_changes = [float(value) for value in period_df["pct_change"].dropna().tolist()]
    positive_days = int((period_df["net_amount"] > 0).sum()) if "net_amount" in period_df.columns else 0
    negative_days = int((period_df["net_amount"] < 0).sum()) if "net_amount" in period_df.columns else 0
    count = len(period_df)

    return {
        "period_days": period_days,
        "data_days": count,
        "start_date": str(period_df.iloc[-1]["trade_date"]),
        "end_date": str(period_df.iloc[0]["trade_date"]),
        "moneyflow_summary": {
            "total_net_amount": total_net,
            "total_net_amount_yi": round(total_net / 10000, 2),
            "avg_daily_net_amount": round(total_net / count / 10000, 2),
            "positive_days": positive_days,
            "negative_days": negative_days,
            "net_flow_trend": "净流入" if total_net > 0 else "净流出",
        },
        "capital_type": {
            "super_large_order": {
                "total_amount": _sum(period_df, "buy_elg_amount"),
                "total_amount_yi": round(_sum(period_df, "buy_elg_amount") / 10000, 2),
                "description": "超大单净额（>100万）",
            },
            "large_order": {
                "total_amount": _sum(period_df, "buy_lg_amount"),
                "total_amount_yi": round(_sum(period_df, "buy_lg_amount") / 10000, 2),
                "description": "大单净额（20-100万）",
            },
            "medium_order": {
                "total_amount": _sum(period_df, "buy_md_amount"),
                "total_amount_yi": round(_sum(period_df, "buy_md_amount") / 10000, 2),
                "description": "中单净额（5-20万）",
            },
            "small_order": {
                "total_amount": _sum(period_df, "buy_sm_amount"),
                "total_amount_yi": round(_sum(period_df, "buy_sm_amount") / 10000, 2),
                "description": "小单净额（<5万）",
            },
        },
        "price_performance": {
            "total_pct_change": round(sum(pct_changes), 2) if pct_changes else None,
            "avg_pct_change": round(sum(pct_changes) / len(pct_changes), 2) if pct_changes else None,
            "positive_pct_days": len([item for item in pct_changes if item > 0]),
            "negative_pct_days": len([item for item in pct_changes if item < 0]),
        },
        "daily_data": _records(
            period_df,
            ["trade_date", "pct_change", "close", "net_amount", "net_amount_rate"],
        ),
    }


def _date_window(days: int) -> tuple[str, str]:
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=days + 10)).strftime("%Y%m%d")
    return start_date, end_date


def _fetch_dc(ts_code: str, days: int) -> pd.DataFrame:
    from app.tools.tushare_client import get_pro

    start_date, end_date = _date_window(days)
    frame = get_pro().moneyflow_dc(
        ts_code=ts_code,
        start_date=start_date,
        end_date=end_date,
        fields=_DC_FIELDS,
    )
    if frame is None or frame.empty:
        return pd.DataFrame()
    return frame.sort_values("trade_date", ascending=False).head(days)


def _fetch_alternative(ts_code: str, days: int) -> pd.DataFrame:
    from app.tools.tushare_client import get_pro

    start_date, end_date = _date_window(days)
    frame = get_pro().moneyflow(
        ts_code=ts_code,
        start_date=start_date,
        end_date=end_date,
        fields=_ALT_FIELDS,
    )
    if frame is None or frame.empty:
        return pd.DataFrame()
    frame = frame.copy()
    frame["net_sm_amount"] = frame["buy_sm_amount"] - frame["sell_sm_amount"]
    frame["net_md_amount"] = frame["buy_md_amount"] - frame["sell_md_amount"]
    frame["net_lg_amount"] = frame["buy_lg_amount"] - frame["sell_lg_amount"]
    frame["net_elg_amount"] = frame["buy_elg_amount"] - frame["sell_elg_amount"]
    frame["net_amount"] = frame["net_mf_amount"]
    frame["buy_elg_amount"] = frame["net_elg_amount"]
    frame["buy_lg_amount"] = frame["net_lg_amount"]
    frame["buy_md_amount"] = frame["net_md_amount"]
    frame["buy_sm_amount"] = frame["net_sm_amount"]
    return frame.sort_values("trade_date", ascending=False).head(days)


def _load_flow(ts_code: str, days: int) -> tuple[pd.DataFrame, str]:
    try:
        frame = _fetch_dc(ts_code, days)
    except Exception as exc:
        frame = pd.DataFrame()
        dc_error = str(exc)
    else:
        dc_error = ""
    if not frame.empty:
        return frame, "moneyflow_dc"
    try:
        frame = _fetch_alternative(ts_code, days)
    except Exception as exc:
        raise RuntimeError(dc_error or str(exc)) from exc
    if frame.empty:
        raise RuntimeError(dc_error or f"未获取到 {ts_code} 的资金流数据")
    return frame, "moneyflow"


def _stock_info(stock: Stock) -> dict:
    info = {
        "ts_code": stock.ts_code,
        "symbol": stock.code,
        "name": stock.name,
        "industry": stock.industry or "未知",
        "market": "未知",
        "exchange": stock.ts_code.split(".")[-1],
    }
    try:
        from app.tools.tushare_client import get_pro

        frame = get_pro().stock_basic(
            ts_code=stock.ts_code,
            fields="ts_code,symbol,name,industry,market,exchange",
        )
    except Exception:
        return info
    if frame is None or frame.empty:
        return info
    row = frame.iloc[0]
    info["name"] = row["name"] or info["name"]
    if pd.notna(row.get("industry")):
        info["industry"] = row["industry"]
    if pd.notna(row.get("market")):
        info["market"] = row["market"]
    if pd.notna(row.get("exchange")):
        info["exchange"] = row["exchange"]
    return info


def get_moneyflow(stock: Stock, days: int = 15) -> dict:
    cache_key = f"moneyflow:{stock.ts_code}:{days}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    if is_hk(stock):
        raise RuntimeError(f"{stock.ts_code} 是港股，暂无与 A 股相同的资金流数据")

    fetch_days = max(days, max(_PERIODS))
    frame, source = _load_flow(stock.ts_code, fetch_days)
    analyses = {}
    for period in _PERIODS:
        if period > days and period != days:
            continue
        result = summarize_moneyflow(frame, period)
        if result:
            analyses[str(period)] = result
    if str(days) not in analyses:
        extra = summarize_moneyflow(frame, min(days, len(frame)))
        if extra:
            analyses[str(days)] = extra

    result = _plain({
        "stock_info": _stock_info(stock),
        "analysis_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_source": source,
        "analyses": analyses,
    })
    cache.set(cache_key, result, config.CACHE_TTL["moneyflow"])
    return result
