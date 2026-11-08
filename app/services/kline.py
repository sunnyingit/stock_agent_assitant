"""K 线：日、周、月，带 5/20/60 均线。指数、ETF、个股都能取。

周线、月线由日线合成（开盘取第一天、最高最低取区间极值、收盘取最后一天），均线按合成后的收盘价算，
所以周线上的 MA20 是 20 周线。个股用前复权，指数和 ETF 用原值。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.tools import cache
from app.tools.stock import resolve_stock

# 宽基指数和它最有名的 ETF（没有把握的不写）
INDEXES = [
    ("000001.SH", "上证指数", ""),
    ("399001.SZ", "深证成指", ""),
    ("000300.SH", "沪深300", "510300"),
    ("000016.SH", "上证50", "510050"),
    ("000905.SH", "中证500", "510500"),
    ("000852.SH", "中证1000", "512100"),
    ("399006.SZ", "创业板指", "159915"),
    ("000688.SH", "科创50", "588000"),
    ("000510.SH", "中证A500", ""),
]
PERIODS = ("day", "week", "month")
DEFAULT_LIMIT = {"day": 250, "week": 104, "month": 60}   # 一年的日线、两年的周线、五年的月线
HISTORY_DAYS = {"day": 1100, "week": 2300, "month": 4500}  # 多取一些，让 60 均线从图的开头就有
TTL = 30 * 60
MA_DAYS = (5, 20, 60)


def kind_of(code: str) -> str:
    """只看代码长相：沪市 000 开头、深市 399 开头、中证/申万/国证后缀的是指数，沪 5 开头、深 15/16 开头的是 ETF。"""
    num, _, market = code.partition(".")
    if code in {c for c, _, _ in INDEXES}:
        return "index"
    if market in ("CSI", "SI", "CNI", "MSCI", "CI"):
        return "index"
    if market == "SH" and num.startswith("000") or market == "SZ" and num.startswith("399"):
        return "index"
    if market == "SH" and num.startswith("5") or market == "SZ" and num.startswith(("15", "16")):
        return "etf"
    return "stock"


def _ymd(d) -> str:
    return str(d).replace("-", "")[:8]


def _iso(d) -> str:
    t = _ymd(d)
    return f"{t[:4]}-{t[4:6]}-{t[6:8]}"


def daily_bars(code: str, period: str) -> list[dict]:
    """升序的日线 [{d, o, h, l, c, v}]。"""
    key = f"kline:daily:{code}:{period}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    from app.tools.tushare_client import get_pro

    pro = get_pro()
    end = datetime.now()
    start = (end - timedelta(days=HISTORY_DAYS[period])).strftime("%Y%m%d")
    stop = end.strftime("%Y%m%d")
    kind = kind_of(code)
    if kind == "index":
        frame = pro.index_daily(ts_code=code, start_date=start, end_date=stop)
    elif kind == "etf":
        frame = pro.fund_daily(ts_code=code, start_date=start, end_date=stop)
    else:
        import tushare as ts

        frame = ts.pro_bar(ts_code=code, adj="qfq", start_date=start, end_date=stop, api=pro)
        if frame is None or getattr(frame, "empty", True):
            frame = pro.daily(ts_code=code, start_date=start, end_date=stop)
    rows = []
    if frame is not None and not getattr(frame, "empty", True):
        for rec in frame.to_dict(orient="records"):
            try:
                rows.append({"d": _iso(rec["trade_date"]), "o": float(rec["open"]), "h": float(rec["high"]), "l": float(rec["low"]), "c": float(rec["close"]), "v": float(rec.get("vol") or 0)})
            except (KeyError, TypeError, ValueError):
                continue
    rows.sort(key=lambda r: r["d"])
    if rows:
        cache.set(key, rows, TTL)
    return rows


def _bucket(day: str, period: str) -> str:
    d = date.fromisoformat(day)
    if period == "week":
        year, week, _ = d.isocalendar()
        return f"{year}-W{week:02d}"
    return day[:7]


def aggregate(bars: list[dict], period: str) -> list[dict]:
    if period == "day":
        return list(bars)
    out: list[dict] = []
    last_key = None
    for b in bars:
        key = _bucket(b["d"], period)
        if key != last_key:
            out.append({"d": b["d"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]})
            last_key = key
        else:
            cur = out[-1]
            cur["d"], cur["c"] = b["d"], b["c"]      # 日期取这一周、这一月的最后一个交易日
            cur["h"], cur["l"] = max(cur["h"], b["h"]), min(cur["l"], b["l"])
            cur["v"] += b["v"]
    return out


def moving_average(closes: list[float], n: int) -> list[float | None]:
    out: list[float | None] = []
    total = 0.0
    for i, c in enumerate(closes):
        total += c
        if i >= n:
            total -= closes[i - n]
        out.append(round(total / n, 3) if i >= n - 1 else None)
    return out


def name_of(code: str) -> str:
    for c, name, _ in INDEXES:
        if c == code:
            return name
    if kind_of(code) != "stock":
        for row in _index_pool():
            if row["ts_code"] == code:
                return row["name"]
        return code
    stock = resolve_stock(code)
    return stock.name if stock else code


def kline(code: str, period: str = "day", limit: int | None = None) -> dict:
    if period not in PERIODS:
        raise ValueError(f"period 要是 day、week、month 之一")
    limit = max(10, min(int(limit or DEFAULT_LIMIT[period]), 600))
    bars = aggregate(daily_bars(code, period), period)
    if not bars:
        raise LookupError(f"没有取到 {code} 的行情")
    closes = [b["c"] for b in bars]
    ma = {f"ma{n}": moving_average(closes, n)[-limit:] for n in MA_DAYS}
    shown = bars[-limit:]
    prev = bars[-2]["c"] if len(bars) > 1 else None
    last = bars[-1]
    return {
        "ts_code": code, "name": name_of(code), "kind": kind_of(code), "period": period, "count": len(shown),
        "items": shown, "ma": ma,
        "last": {"d": last["d"], "c": last["c"], "chg_pct": None if not prev else round((last["c"] / prev - 1) * 100, 2)},
    }


# ---------------------------------------------------------------- 搜索
SEARCH_LIMIT = 10
_A_SHARE = (".SH", ".SZ", ".BJ")


def _index_pool() -> list[dict]:
    """可以画图的指数和 ETF：宽基常用的排最前，其余来自 tushare 的指数和场内基金列表。"""
    cached = cache.get("kline:pool")
    if cached is not None:
        return cached
    pool = [{"ts_code": c, "name": n, "kind": "index", "full": ""} for c, n, _ in INDEXES]
    seen = {r["ts_code"] for r in pool}
    for _, name, etf in INDEXES:
        if etf:
            pool.append({"ts_code": f"{etf}.{'SH' if etf.startswith('5') else 'SZ'}", "name": f"{name}ETF", "kind": "etf", "full": ""})
            seen.add(pool[-1]["ts_code"])
    ok = True
    try:
        from app.tools.tushare_client import get_pro

        pro = get_pro()
        for market in ("SSE", "SZSE", "CSI"):
            frame = pro.index_basic(market=market, fields="ts_code,name,fullname")
            for rec in ([] if frame is None or getattr(frame, "empty", True) else frame.to_dict(orient="records")):
                code = str(rec.get("ts_code") or "")
                if code and code not in seen:
                    seen.add(code)
                    pool.append({"ts_code": code, "name": str(rec.get("name") or code), "kind": "index", "full": str(rec.get("fullname") or "")})
        frame = pro.fund_basic(market="E", status="L", fields="ts_code,name")
        for rec in ([] if frame is None or getattr(frame, "empty", True) else frame.to_dict(orient="records")):
            code = str(rec.get("ts_code") or "")
            if code and code not in seen:
                seen.add(code)
                pool.append({"ts_code": code, "name": str(rec.get("name") or code), "kind": "etf", "full": ""})
    except Exception:  # noqa: BLE001 - 取不到完整列表时，宽基这几个照样能搜
        ok = False
    cache.set("kline:pool", pool, 24 * 3600 if ok else 5 * 60)
    return pool


def search(query: str, kind: str = "index", limit: int = SEARCH_LIMIT) -> list[dict]:
    """行情弹窗的搜索：kind=index 找指数和 ETF，kind=stock 找 A 股。名称开头匹配的排前面，其次是名称包含、代码前缀。"""
    text = (query or "").strip()
    if not text:
        return []
    upper = text.upper()
    if kind == "stock":
        from app.synthesis import quotes

        rows = [r for r in quotes.search(text, limit=limit * 3) if str(r["ts_code"]).endswith(_A_SHARE)]
        return [{"ts_code": r["ts_code"], "name": r["name"], "kind": "stock", "note": r.get("industry") or ""} for r in rows[:limit]]
    ranked = []
    for order, row in enumerate(_index_pool()):
        names = [row["name"]] + ([row["full"]] if row["full"] else [])
        code = row["ts_code"]
        if any(n.startswith(text) for n in names) or code.startswith(upper):
            rank = 0
        elif any(text in n for n in names) or upper in code:
            rank = 1
        else:
            continue
        ranked.append((rank, order, row))
    ranked.sort(key=lambda t: (t[0], t[1]))
    return [{"ts_code": r["ts_code"], "name": r["name"], "kind": r["kind"], "note": "ETF" if r["kind"] == "etf" else "指数"} for _, _, r in ranked[:limit]]
