"""市场页「外盘」：美股隔夜指数 + 韩股（KOSPI / 龙头）。

主源东财全球行情；断连时用腾讯行情补齐（费城半导体退化为 SOXX ETF）。
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from app.tools import cache

_ZONE = ZoneInfo("Asia/Shanghai")
_FIELDS = "f12,f13,f14,f2,f3,f4,f152"
_HOSTS = (
    "https://push2his.eastmoney.com/api/qt/ulist.np/get",
    "https://82.push2.eastmoney.com/api/qt/ulist.np/get",
    "https://push2.eastmoney.com/api/qt/ulist.np/get",
)
_TX_LINE = re.compile(r'v_([^=]+)="([^"]*)"')
TTL = 60
STALE = 3600
FAIL_COOLDOWN = 20

# (secid, group, name, tencent_symbol)
_WATCH = (
    ("100.SPX", "us", "标普 500", "s_us.INX"),
    ("100.NDX", "us", "纳斯达克", "s_usIXIC"),
    ("251.SOX", "us", "费城半导体", "s_usSOXX"),
    ("100.KS11", "kr", "KOSPI", ""),  # 腾讯无稳定码，靠东财
    ("177.000660", "kr", "SK 海力士", "kr000660"),
    ("177.005930", "kr", "三星电子", "kr005930"),
    # 美股科技龙头：可在市场页勾选分钟跟踪
    ("105.NVDA", "tech", "英伟达", "s_usNVDA"),
    ("105.AAPL", "tech", "苹果", "s_usAAPL"),
    ("105.MSFT", "tech", "微软", "s_usMSFT"),
    ("105.TSM", "tech", "台积电", "s_usTSM"),
    ("105.AMD", "tech", "AMD", "s_usAMD"),
    ("105.META", "tech", "Meta", "s_usMETA"),
)


def _num(v):
    if v is None or v == "-" or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _kr_session(now: datetime) -> tuple[str, str]:
    """韩股相对北京时间：约 08:00–14:30 为盘中。"""
    if now.weekday() >= 5:
        return "closed", "休市"
    hm = now.hour * 60 + now.minute
    if 8 * 60 <= hm < 14 * 60 + 30:
        return "live", f"{now:%H:%M} 实时"
    if hm < 8 * 60:
        return "pre", "未开盘"
    return "closed", "已收盘"


def _us_as_of(now: datetime) -> str:
    """美股在北京约 04–05 点收盘；白天展示为上一交易日收盘。"""
    day = now.date()
    if now.hour < 5:
        day = day - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return f"{day.month:02d}-{day.day:02d} 收盘"


def _http_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _scale_em(v):
    """无 fltt 时东财整数价/涨跌通常 ×100。"""
    n = _num(v)
    if n is None:
        return None
    if abs(n) >= 100 and float(n).is_integer():
        return n / 100.0
    return n


def _fetch_eastmoney_one(secid: str) -> dict | None:
    """ulist 挂了时，用单票 stock/get 兜底。"""
    host = "https://push2his.eastmoney.com/api/qt/stock/get"
    url = f"{host}?secid={secid}&fields=f57,f58,f43,f169,f170,f60"
    try:
        data = ((_http_json(url) or {}).get("data") or {})
    except Exception:
        return None
    code = str(data.get("f57") or secid.split(".", 1)[-1])
    if not code:
        return None
    pct = _scale_em(data.get("f170"))
    price = _scale_em(data.get("f43"))
    if pct is None and price is None:
        return None
    return {
        "f12": code,
        "f13": int(secid.split(".", 1)[0]) if secid.split(".", 1)[0].isdigit() else None,
        "f14": data.get("f58") or code,
        "f2": price,
        "f3": pct,
        "f4": _scale_em(data.get("f169")),
    }


def _fetch_eastmoney(secids: str) -> list[dict]:
    key = f"market_overseas:em:{secids}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    if cache.get(key + ":fail") is not None:
        stale = cache.get(key + ":stale")
        if stale is not None:
            return stale

    params = f"fltt=2&fields={_FIELDS}&secids={secids}"
    last_error: Exception | None = None
    for host in _HOSTS:
        for _attempt in range(2):
            try:
                body = _http_json(f"{host}?{params}")
                rows = ((body or {}).get("data") or {}).get("diff") or []
                if rows:
                    cache.set(key, rows, TTL)
                    cache.set(key + ":stale", rows, STALE)
                    return rows
                last_error = RuntimeError("外盘没有返回行情")
            except Exception as exc:
                last_error = exc
            time.sleep(0.35)
        time.sleep(0.25)

    # ulist 全挂：逐个 stock/get
    singles: list[dict] = []
    for secid in secids.split(","):
        row = _fetch_eastmoney_one(secid.strip())
        if row:
            singles.append(row)
        time.sleep(0.2)
    if singles:
        cache.set(key, singles, TTL)
        cache.set(key + ":stale", singles, STALE)
        return singles

    cache.set(key + ":fail", True, FAIL_COOLDOWN)
    stale = cache.get(key + ":stale")
    if stale is not None:
        return stale
    raise last_error or RuntimeError("外盘东财没取到")


def _parse_tencent(text: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for symbol, body in _TX_LINE.findall(text or ""):
        if body in ("", "1"):
            continue
        parts = body.split("~")
        if len(parts) < 6:
            continue
        price = _num(parts[3])
        # 简码 s_* ：现价、涨跌额、涨跌幅
        if symbol.startswith("s_"):
            change, pct = _num(parts[4]), _num(parts[5])
        else:
            prev = _num(parts[4])
            if price is None or prev is None or prev == 0:
                continue
            change = round(price - prev, 4)
            pct = round((price / prev - 1) * 100, 2)
        if price is None and pct is None:
            continue
        out[symbol] = {"price": price, "pct": pct, "change": change}
    return out


def _fetch_tencent(symbols: list[str]) -> dict[str, dict]:
    wanted = [s for s in symbols if s]
    if not wanted:
        return {}
    key = "market_overseas:tx:" + ",".join(wanted)
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        req = urllib.request.Request(
            "http://qt.gtimg.cn/q=" + ",".join(wanted),
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            text = resp.read().decode("gbk", "replace")
        parsed = _parse_tencent(text)
        if parsed:
            cache.set(key, parsed, TTL)
            cache.set(key + ":stale", parsed, STALE)
            return parsed
    except Exception:
        stale = cache.get(key + ":stale")
        if stale is not None:
            return stale
    return {}


def _index_em(rows: list[dict]) -> dict[str, dict]:
    by: dict[str, dict] = {}
    for raw in rows:
        code = str(raw.get("f12") or "")
        mkt = raw.get("f13")
        if not code:
            continue
        by[code] = raw
        if mkt is not None:
            by[f"{mkt}.{code}"] = raw
    return by


def _row(meta: tuple, em: dict, tx: dict, now: datetime) -> dict:
    secid, group, name, tx_sym = meta
    code = secid.split(".", 1)[-1]
    raw = em.get(code) or em.get(secid) or {}
    pct = _num(raw.get("f3"))
    price = _num(raw.get("f2"))
    change = _num(raw.get("f4"))
    source = "eastmoney" if (pct is not None or price is not None) else ""
    note = ""

    if pct is None and tx_sym:
        hit = tx.get(tx_sym) or {}
        if hit:
            pct = hit.get("pct")
            price = hit.get("price") if price is None else price
            change = hit.get("change") if change is None else change
            source = "tencent"
            if code == "SOX":
                note = "SOXX ETF 近似"
                name = "费城半导体"

    if group in ("us", "tech"):
        as_of = _us_as_of(now)
        state = "closed"
    else:
        state, as_of = _kr_session(now)

    return {
        "secid": secid,
        "code": code,
        "name": name,
        "group": group,
        "price": price,
        "pct": None if pct is None else round(pct, 2),
        "change": change,
        "as_of": as_of + (f" · {note}" if note else ""),
        "state": state,
        "source": source,
        "ok": pct is not None or price is not None,
    }


def _fill_kospi_tushare(kr_items: list[dict], errors: list[str]) -> None:
    """东财/腾讯都没有 KOSPI 时，用 tushare 全球指数日线兜底（可能滞后）。"""
    target = next((row for row in kr_items if row.get("code") == "KS11" and not row.get("ok")), None)
    if target is None:
        return
    try:
        from app.services.market_tape import _pro, _ymd
        from datetime import date

        end = date.today()
        start = end - timedelta(days=14)
        df = _pro().index_global(ts_code="KS11", start_date=_ymd(start), end_date=_ymd(end))
        if df is None or getattr(df, "empty", True):
            return
        row = df.sort_values("trade_date").iloc[-1]
        pct = _num(row.get("pct_chg"))
        close = _num(row.get("close"))
        if pct is None and close is None:
            return
        day = str(row.get("trade_date") or "")
        as_of = f"{day[4:6]}-{day[6:8]} 收盘" if len(day) == 8 else "日线"
        target.update({
            "price": close,
            "pct": None if pct is None else round(pct, 2),
            "change": _num(row.get("change")),
            "as_of": as_of + " · tushare",
            "source": "tushare",
            "ok": True,
        })
    except Exception as exc:
        errors.append(f"KOSPI tushare: {exc}")


# 搜索建议里允许加入外盘面板的类型（东财 Suggest 的 Classify / SecurityTypeName）。
_SEARCH_OK = {
    "UsStock", "KRX", "NDI", "NASDAQ", "NYSE", "AMEX", "HK",  # 常见 Classify
}
_SEARCH_OK_NAME = {"美股", "韩股", "指数", "港股"}


def _tx_symbol(secid: str, group: str) -> str:
    code = secid.split(".", 1)[-1]
    if group == "kr":
        return f"kr{code}" if code.isdigit() else ""
    if group in ("us", "tech", "extra"):
        # 简码指数走特例；个股一般是 s_usCODE
        known = {item[0]: item[3] for item in _WATCH}
        if secid in known:
            return known[secid]
        if code.isalpha():
            return f"s_us{code}"
    return ""


def _group_of_mkt(mkt: str, classify: str = "", type_name: str = "") -> str:
    if mkt == "177" or classify == "KRX" or type_name == "韩股":
        return "kr"
    if mkt in {"100", "251"} or type_name == "指数" or classify == "NDI":
        return "us"
    if mkt == "105" or classify == "UsStock" or type_name == "美股":
        return "tech"
    return "extra"


def _parse_extra_secids(raw: str | list[str] | None) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        parts = raw.replace(";", ",").split(",")
    else:
        parts = list(raw)
    out: list[str] = []
    seen = set()
    for p in parts:
        secid = str(p or "").strip().upper()
        if not secid or "." not in secid or secid in seen:
            continue
        mkt, code = secid.split(".", 1)
        if not mkt.isdigit() or not code:
            continue
        seen.add(secid)
        out.append(secid)
    return out


def search(q: str, limit: int = 8) -> list[dict]:
    """按代码/名称搜美股、韩股、相关指数，返回可加入外盘的 secid。"""
    text = (q or "").strip()
    if not text:
        return []
    limit = max(1, min(int(limit or 8), 15))
    key = f"market_overseas:search:{text}:{limit}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    url = (
        "https://searchapi.eastmoney.com/api/suggest/get"
        f"?input={quote(text)}&type=14&token=D43BF785C67F90015BB305A730791580"
    )
    try:
        body = _http_json(url)
    except Exception:
        return []
    rows = ((body or {}).get("QuotationCodeTable") or {}).get("Data") or []
    out: list[dict] = []
    seen = set()
    for raw in rows:
        code = str(raw.get("Code") or "").strip()
        mkt = str(raw.get("MktNum") or "").strip()
        name = str(raw.get("Name") or code).strip()
        classify = str(raw.get("Classify") or "")
        type_name = str(raw.get("SecurityTypeName") or "")
        if not code or not mkt.isdigit():
            continue
        if classify not in _SEARCH_OK and type_name not in _SEARCH_OK_NAME and mkt not in {"100", "105", "177", "251"}:
            continue
        secid = str(raw.get("QuoteID") or f"{mkt}.{code}")
        if secid in seen:
            continue
        seen.add(secid)
        group = _group_of_mkt(mkt, classify, type_name)
        out.append({
            "secid": secid,
            "code": code,
            "name": name,
            "group": group,
            "market": type_name or classify or mkt,
        })
        if len(out) >= limit:
            break
    cache.set(key, out, 5 * 60)
    return out


def _extra_meta(secid: str, names: dict[str, str] | None = None) -> tuple:
    code = secid.split(".", 1)[-1]
    mkt = secid.split(".", 1)[0]
    group = _group_of_mkt(mkt)
    name = (names or {}).get(secid) or (names or {}).get(code) or code
    return (secid, group, name, _tx_symbol(secid, group))


def build(now: datetime | None = None, extra: str | list[str] | None = None, names: dict[str, str] | None = None) -> dict:
    now = now or datetime.now(_ZONE)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_ZONE)
    else:
        now = now.astimezone(_ZONE)

    watch_ids = {item[0] for item in _WATCH}
    extra_metas = [_extra_meta(secid, names) for secid in _parse_extra_secids(extra) if secid not in watch_ids]
    fetch_list = list(_WATCH) + extra_metas

    secids = ",".join(item[0] for item in fetch_list)
    errors: list[str] = []
    em: dict[str, dict] = {}
    try:
        em = _index_em(_fetch_eastmoney(secids))
    except Exception as exc:
        errors.append(str(exc))

    need_tx = []
    for secid, _group, _name, tx_sym in fetch_list:
        code = secid.split(".", 1)[-1]
        if tx_sym and not (em.get(code) or em.get(secid)):
            need_tx.append(tx_sym)
    # 东财偶发只回一半时也补一遍腾讯
    if not need_tx and errors:
        need_tx = [tx for *_rest, tx in fetch_list if tx]
    tx = _fetch_tencent(need_tx) if need_tx else {}
    if need_tx and not tx:
        errors.append("腾讯外盘补源也没有取到")

    # 默认三组只放内置清单；用户新加的一律进 extra，避免目录和自选搅在一起
    us = [_row(meta, em, tx, now) for meta in _WATCH if meta[1] == "us"]
    kr = [_row(meta, em, tx, now) for meta in _WATCH if meta[1] == "kr"]
    tech = [_row(meta, em, tx, now) for meta in _WATCH if meta[1] == "tech"]
    extra_rows = [_row(meta, em, tx, now) for meta in extra_metas]
    for row in extra_rows:
        raw = em.get(row["code"]) or em.get(row["secid"]) or {}
        if raw.get("f14"):
            row["name"] = str(raw["f14"])
    _fill_kospi_tushare(kr, errors)
    built = us + kr + tech + extra_rows
    ok_n = sum(1 for row in built if row.get("ok"))
    partial = bool(errors) or (0 < ok_n < len(built))
    status = "ok" if ok_n else "error"
    kr_state, _ = _kr_session(now)

    return {
        "status": status,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "partial": partial,
        "errors": errors,
        "us": {
            "title": "美股",
            "subtitle": "隔夜收盘",
            "items": us,
        },
        "kr": {
            "title": "韩股",
            "subtitle": "盘中同步" if kr_state == "live" else "相对北京时间",
            "session": kr_state,
            "items": kr,
        },
        "tech": {
            "title": "科技股",
            "subtitle": "可跟踪",
            "items": tech,
        },
        "extra": {
            "title": "自选",
            "subtitle": "你加的",
            "items": extra_rows,
        },
        "note": "隔夜与海外情绪参考，不是买卖建议",
    }
