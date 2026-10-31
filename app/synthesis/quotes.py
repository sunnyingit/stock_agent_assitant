"""自选列表用的最新价和涨跌幅：腾讯行情一次请求取一批，短缓存。搜索用 Tushare 的 A 股和港股列表按名称或代码匹配。"""

from __future__ import annotations

import re
from typing import Callable

from app.tools import cache
from app.tools.stock import Stock, is_hk

_HK_CODE_QUERY = re.compile(r"^(\d{1,5})(?:\.HK)?$", re.IGNORECASE)

_URL = "http://qt.gtimg.cn/q="
_LINE = re.compile(r'v_(\w+)="([^"]*)"')
QUOTE_TTL = 10   # 秒：列表页会被前端轮询，这里挡掉重复请求


def _symbol(stock: Stock) -> str:
    if is_hk(stock):
        return "hk" + stock.code.zfill(5)
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(stock.ts_code.split(".")[-1].upper(), "")
    return prefix + stock.code if prefix else ""


def _parse(text: str) -> dict[str, dict]:
    quotes = {}
    for symbol, body in _LINE.findall(text):
        parts = body.split("~")
        if len(parts) < 5:
            continue
        try:
            price, prev = float(parts[3]), float(parts[4])
        except ValueError:
            continue
        if price <= 0 or prev <= 0:   # 停牌或还没开盘：没有可用的价格
            continue
        quotes[symbol] = {
            "price": round(price, 4), "prev": round(prev, 4),
            "change": round(price - prev, 4), "change_pct": round((price / prev - 1) * 100, 2),
        }
    return quotes


def _fetch_text(symbols: list[str]) -> str:
    import requests

    session = requests.Session()
    session.trust_env = False
    response = session.get(_URL + ",".join(symbols), timeout=(2, 4))
    response.raise_for_status()
    return response.content.decode("gbk", "replace")


def fetch(stocks: list[Stock], fetch_text: Callable[[list[str]], str] | None = None) -> dict[str, dict]:
    """返回 {ts_code: {price, prev, change, change_pct}}。取不到的股票不在结果里，不报错。"""
    result: dict[str, dict] = {}
    missing: list[tuple[Stock, str]] = []
    for stock in stocks:
        symbol = _symbol(stock)
        if not symbol:
            continue
        hit = cache.get(f"quote:{symbol}")
        if hit is not None:
            result[stock.ts_code] = hit
        else:
            missing.append((stock, symbol))
    if missing:
        try:
            parsed = _parse((fetch_text or _fetch_text)([symbol for _stock, symbol in missing]))
        except Exception:
            parsed = {}
        for stock, symbol in missing:
            quote = parsed.get(symbol)
            if quote:
                cache.set(f"quote:{symbol}", quote, QUOTE_TTL)
                result[stock.ts_code] = quote
    return result


def search(
    query: str,
    limit: int = 8,
    listed: Callable[[], list[dict]] | None = None,
    hk_listed: Callable[[], list[dict]] | None = None,
) -> list[dict]:
    """按名称包含、或代码前缀匹配。名称开头匹配的排在前面。A 股和港股放在同一个结果里。"""
    text = query.strip()
    if not text:
        return []
    if listed is None:
        from app.tools.stock import _listed_stocks as listed
    if hk_listed is None:
        from app.tools.stock import _hk_stocks as hk_listed
    upper = text.upper()
    pool = list(listed())
    try:
        pool.extend(hk_listed())
    except Exception:
        pass
    rows = []
    for row in pool:
        name, symbol, ts_code = row["name"], row["symbol"], row["ts_code"]
        names = [name]
        fullname = row.get("fullname")
        if fullname:
            names.append(fullname)
        if any(item.startswith(text) for item in names):
            rank = 0
        elif any(text in item for item in names):
            rank = 1
        elif _code_match(symbol, ts_code, text, upper):
            rank = 0
        else:
            continue
        rows.append((rank, ts_code, {"ts_code": ts_code, "code": symbol, "name": name, "industry": row.get("industry")}))
    rows.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in rows[:limit]]


def _code_match(symbol: str, ts_code: str, text: str, upper: str) -> bool:
    if symbol.startswith(text) or ts_code.startswith(upper):
        return True
    matched = _HK_CODE_QUERY.fullmatch(text)
    if not matched or not ts_code.upper().endswith(".HK"):
        return False
    digits = matched.group(1)
    padded = digits.zfill(5)
    if symbol == padded or symbol.startswith(padded):
        return True
    bare_symbol = symbol.lstrip("0")
    bare_query = digits.lstrip("0")
    return bool(bare_query) and bare_symbol.startswith(bare_query)

