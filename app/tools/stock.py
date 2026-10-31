"""用 Tushare 股票列表把名称或代码解析成 ts_code。"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd

from app.tools import cache

_TS_CODE = re.compile(r"\d{6}\.[A-Za-z]{2}")
_HK_CODE = re.compile(r"(\d{1,5})\.HK", re.IGNORECASE)
_PLAIN_CODE = re.compile(r"\d{6}")
_HK_PLAIN = re.compile(r"\d{1,5}")


@dataclass
class Stock:
    ts_code: str
    code: str
    name: str
    quote_id: str
    em_code: str
    industry: str | None = None
    price: float | None = None
    change_pct: float | None = None
    pe: float | None = None
    pb: float | None = None
    total_mv: float | None = None


def is_hk(stock: Stock) -> bool:
    return stock.ts_code.endswith(".HK")


def hk_code(query: str) -> str | None:
    """1 到 5 位数字，或显式 .HK。A 股代码固定 6 位，不会进这里。"""
    text = query.strip()
    matched = _HK_CODE.fullmatch(text)
    if matched:
        return matched.group(1).zfill(5)
    if _HK_PLAIN.fullmatch(text):
        return text.zfill(5)
    return None


def exchange_suffix(code: str) -> str:
    if code.startswith(("43", "83", "87", "92")):
        return "BJ"
    if code.startswith(("60", "68")):
        return "SH"
    return "SZ"


def stock_from_code(code: str, suffix: str | None = None, name: str | None = None, industry: str | None = None) -> Stock:
    suffix = (suffix or exchange_suffix(code)).upper()
    if suffix == "HK":
        code = code.zfill(5)
        return Stock(
            ts_code=f"{code}.HK",
            code=code,
            name=name or code,
            quote_id=f"116.{code}",
            em_code=code,
            industry=industry,
        )
    market = "1" if suffix == "SH" else "0"
    return Stock(
        ts_code=f"{code}.{suffix}",
        code=code,
        name=name or code,
        quote_id=f"{market}.{code}",
        em_code=f"{suffix}{code}",
        industry=industry,
    )


def _listed_stocks() -> list[dict]:
    cached = cache.get("tushare:stock_basic")
    if cached is not None:
        return cached
    from app.tools.tushare_client import get_pro

    frame = get_pro().stock_basic(
        exchange="",
        list_status="L",
        fields="ts_code,symbol,name,industry,market,exchange",
    )
    rows = []
    if frame is not None and not frame.empty:
        for _, row in frame.iterrows():
            industry = row.get("industry")
            rows.append({
                "ts_code": str(row["ts_code"]),
                "symbol": str(row["symbol"]),
                "name": str(row["name"]),
                "industry": None if pd.isna(industry) else str(industry),
            })
    cache.set("tushare:stock_basic", rows, 6 * 3600)
    return rows


def _hk_stocks() -> list[dict]:
    cached = cache.get("tushare:hk_basic")
    if cached is not None:
        return cached
    from app.tools.tushare_client import get_pro

    frame = get_pro().hk_basic(list_status="L", fields="ts_code,name,fullname")
    rows = []
    if frame is not None and not frame.empty:
        for _, row in frame.iterrows():
            ts_code = str(row["ts_code"])
            fullname = row.get("fullname")
            rows.append({
                "ts_code": ts_code,
                "symbol": ts_code.split(".")[0],
                "name": str(row["name"]),
                "fullname": None if pd.isna(fullname) else str(fullname),
                "industry": None,
            })
    cache.set("tushare:hk_basic", rows, 6 * 3600)
    return rows


def _match_hk(rows: list[dict], query: str, code: str | None) -> dict | None:
    if code:
        ts_code = f"{code}.HK"
        for row in rows:
            if row["ts_code"] == ts_code or row["symbol"] == code:
                return row
        return None
    for row in rows:
        if query in (row["name"], row.get("fullname")):
            return row
    return None


def _hk_stock(query: str, code: str | None) -> Stock | None:
    matched = None
    try:
        matched = _match_hk(_hk_stocks(), query, code)
    except Exception:
        matched = None
    if matched:
        return stock_from_code(matched["symbol"], "HK", matched["name"], matched.get("industry"))
    if code:
        return stock_from_code(code, "HK")
    return None


def _match(rows: list[dict], query: str, code: str | None, suffix: str | None) -> dict | None:
    if code:
        ts_code = f"{code}.{suffix}"
        for row in rows:
            if row["ts_code"] == ts_code or row["symbol"] == code:
                return row
        return None
    for row in rows:
        if row["name"] == query:
            return row
    return None


def resolve_stock(query: str) -> Stock | None:
    query = query.strip()
    if not query:
        return None
    cache_key = f"stock:{query}"
    cached = cache.get(cache_key)
    if cached is not None and not _unnamed(cached):
        return cached

    hk = hk_code(query)
    if hk:
        stock = _hk_stock(query, hk)
    else:
        code = None
        suffix = None
        if _TS_CODE.fullmatch(query):
            code, suffix = query.upper().split(".")
        elif _PLAIN_CODE.fullmatch(query):
            code = query
            suffix = exchange_suffix(code)

        matched = None
        try:
            matched = _match(_listed_stocks(), query, code, suffix)
        except Exception:
            matched = None

        if matched:
            symbol, market = matched["ts_code"].split(".")
            stock = stock_from_code(symbol, market, matched["name"], matched.get("industry"))
        elif code:
            stock = stock_from_code(code, suffix)
        else:
            stock = _hk_stock(query, None)
    if stock is None:
        return None
    return _remember(query, stock)


def _unnamed(stock: Stock) -> bool:
    name = str(stock.name or "").strip()
    return name in ("", stock.code, stock.ts_code)


def _remember(query: str, stock: Stock) -> Stock:
    if _unnamed(stock):
        return stock
    cache.set(f"stock:{query}", stock, 6 * 3600)
    cache.set(f"stock:{stock.ts_code}", stock, 6 * 3600)
    cache.set(f"stock:{stock.code}", stock, 6 * 3600)
    return stock
