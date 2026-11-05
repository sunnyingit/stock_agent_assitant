"""资金流分析师追问的切片。数字在这里算好，模型只引用 text。

个股资金用东方财富 moneyflow_dc，行情用前复权日线，技术指标用 stk_factor，
筹码用 cyq_perf，涨跌停价用盘前股本，涨跌停名单和龙虎榜只取本股。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app import config
from app.tools import cache
from app.tools.stock import Stock, is_hk

PARTS = ("dc", "factor", "chip", "bar", "premarket", "limit", "toplist", "margin")
_ZONE = ZoneInfo("Asia/Shanghai")
_LIMIT = {"U": "涨停", "D": "跌停", "Z": "炸板"}
MAX_DAYS = 120          # 追问最多能看多少个交易日
WINDOWS = (1, 3, 5, 10, 20, 60)   # 默认给出的时间尺度：脉冲、短线、波段、中期


def clamp_days(days) -> int | None:
    """追问可以指定看多少个交易日；不指定就用默认的多尺度。"""
    try:
        value = int(days)
    except (TypeError, ValueError):
        return None
    return max(1, min(value, MAX_DAYS)) if value > 0 else None


def build(stock: Stock, part: str, days=None) -> dict:
    if part not in PARTS:
        raise ValueError(f"未知切片 {part}")
    days = clamp_days(days)
    if is_hk(stock):
        return _doc(stock, part, [], "港股暂无与 A 股相同的资金流数据")
    return {
        "dc": _dc,
        "factor": _factor,
        "chip": _chip,
        "bar": _bar,
        "premarket": _premarket,
        "limit": _limit,
        "toplist": _toplist,
        "margin": _margin,
    }[part](stock, days)


def format_dc(items: list[dict], days: int | None = None) -> dict:
    """东财个股资金：多个时间尺度并排（1、3、5、10、20、60 日），用户指定了天数就再加一个窗口和逐日明细。"""
    rows = [row for row in items if isinstance(row, dict)]
    if not rows:
        return _body([], "没有取到东财个股资金流")
    windows = sorted({n for n in WINDOWS if n <= len(rows)} | ({days} if days else set()))
    facts = []
    for n in windows:
        window = rows[-n:]
        label = f"近 {n} 日" if len(window) == n else f"近 {len(window)} 日（不足 {n} 日）"
        bits = [f"{label}东财净流入 {_wan(_sum(window, 'net_amount'))}"]
        for key, name in (
            ("buy_elg_amount", "超大单"),
            ("buy_lg_amount", "大单"),
            ("buy_md_amount", "中单"),
            ("buy_sm_amount", "小单"),
        ):
            bits.append(f"{name} {_wan(_sum(window, key))}")
        facts.append({"id": f"fl.dc_{n}d", "text": "，".join(bits)})
    shown = min(days or 5, len(rows), MAX_DAYS)
    daily = [f"{_short_date(row.get('trade_date'))} {_wan(_num(row.get('net_amount')))}" for row in rows[-shown:]]
    facts.append({"id": "fl.dc_daily", "text": f"近 {shown} 日逐日净流入：" + "，".join(daily)})
    return _body(facts, " ".join(item["text"] for item in facts))


def format_factor(rows: list[dict]) -> dict:
    if not rows:
        return _body([], "没有取到技术指标")
    latest = rows[-1]
    close = _num(latest.get("close"))
    facts = [{
        "id": "fl.macd",
        "text": (
            f"{_short_date(latest.get('trade_date'))} MACD DIF {_num_text(latest.get('macd_dif'))}，"
            f"DEA {_num_text(latest.get('macd_dea'))}，柱 {_num_text(latest.get('macd'))}"
        ),
    }, {
        "id": "fl.rsi",
        "text": f"RSI6 {_num_text(latest.get('rsi_6'))}，RSI12 {_num_text(latest.get('rsi_12'))}，RSI24 {_num_text(latest.get('rsi_24'))}",
    }, {
        "id": "fl.kdj",
        "text": f"KDJ K {_num_text(latest.get('kdj_k'))}，D {_num_text(latest.get('kdj_d'))}，J {_num_text(latest.get('kdj_j'))}",
    }]
    upper, mid, lower = _num(latest.get("boll_upper")), _num(latest.get("boll_mid")), _num(latest.get("boll_lower"))
    if upper is not None and lower is not None:
        place = ""
        if close is not None:
            if close > upper:
                place = "，收盘在上轨之上"
            elif close < lower:
                place = "，收盘在下轨之下"
            else:
                place = "，收盘在上下轨之间"
        facts.append({"id": "fl.boll", "text": f"布林上轨 {upper:.2f}，中轨 {mid:.2f}，下轨 {lower:.2f}{place}"})
    if latest.get("cci") is not None:
        facts.append({"id": "fl.cci", "text": f"CCI {_num_text(latest.get('cci'))}"})
    hist = [f"{_short_date(row.get('trade_date'))} {_num_text(row.get('macd'))}" for row in rows[-5:]]
    facts.append({"id": "fl.macd_days", "text": "近 5 日 MACD 柱：" + "，".join(hist)})
    return _body(facts, " ".join(item["text"] for item in facts))


def format_chip(row: dict | None, close: float | None = None) -> dict:
    if not row:
        return _body([], "没有取到筹码和胜率")
    avg = _num(row.get("weight_avg"))
    rate = _num(row.get("winner_rate"))
    facts = [{
        "id": "fl.chip",
        "text": (
            f"{_short_date(row.get('trade_date'))} 加权平均成本 {_price(avg)}，"
            f"胜率 {_pct_points(rate)}，"
            f"5 分位成本 {_price(_num(row.get('cost_5pct')))}，"
            f"50 分位 {_price(_num(row.get('cost_50pct')))}，"
            f"95 分位 {_price(_num(row.get('cost_95pct')))}"
        ),
    }]
    if close is not None and avg:
        gap = (close - avg) / avg * 100
        side = "上方" if gap >= 0 else "下方"
        facts.append({"id": "fl.chip_vs_close", "text": f"收盘 {close:.2f} 在加权成本{side} {abs(gap):.1f}%"})
    return _body(facts, " ".join(item["text"] for item in facts))


def format_bar(rows: list[dict], days: int | None = None) -> dict:
    if not rows:
        return _body([], "没有取到前复权行情")
    latest = rows[-1]
    facts = [{
        "id": "fl.bar",
        "text": (
            f"{_short_date(latest.get('trade_date'))} 前复权收盘 {_price(_num(latest.get('close')))}，"
            f"涨跌幅 {_pct_points(_num(latest.get('pct_chg') if latest.get('pct_chg') is not None else latest.get('pct_change')))}，"
            f"5 日均线 {_price(_num(latest.get('ma5') if latest.get('ma5') is not None else latest.get('ma_5')))}，"
            f"20 日均线 {_price(_num(latest.get('ma20') if latest.get('ma20') is not None else latest.get('ma_20')))}"
        ),
    }]
    tor = _num(latest.get("turnover_rate") if latest.get("turnover_rate") is not None else latest.get("tor"))
    if tor is not None:
        facts.append({"id": "fl.turnover", "text": f"换手率 {tor:.2f}%"})
    vols = [_num(row.get("vol")) for row in rows[-6:-1]]
    vols = [item for item in vols if item]
    today = _num(latest.get("vol"))
    if today and vols:
        base = sum(vols) / len(vols)
        if base:
            facts.append({"id": "fl.vol", "text": f"今日成交量是前 {len(vols)} 日均量的 {today / base:.2f} 倍"})
    shown = min(days or 5, len(rows), MAX_DAYS)
    daily = []
    for row in rows[-shown:]:
        pct = _num(row.get("pct_chg") if row.get("pct_chg") is not None else row.get("pct_change"))
        daily.append(f"{_short_date(row.get('trade_date'))} {_pct_points(pct)}")
    facts.append({"id": "fl.bar_days", "text": f"近 {shown} 日涨跌：" + "，".join(daily)})
    span = []
    for n in sorted({n for n in WINDOWS if 1 < n <= len(rows)} | ({days} if days and days <= len(rows) else set())):
        total = 1.0
        for row in rows[-n:]:
            total *= 1 + (_num(row.get("pct_chg") if row.get("pct_chg") is not None else row.get("pct_change")) or 0) / 100
        span.append(f"近 {n} 日 {_pct_points((total - 1) * 100)}")
    if span:
        facts.append({"id": "fl.bar_windows", "text": "各时间尺度累计涨跌：" + "，".join(span)})
    return _body(facts, " ".join(item["text"] for item in facts))


def format_premarket(row: dict | None) -> dict:
    if not row:
        return _body([], "没有取到盘前股本和涨跌停价")
    facts = [{
        "id": "fl.premarket",
        "text": (
            f"{_short_date(row.get('trade_date'))} 总股本 {_lots(row.get('total_share'))}，"
            f"流通股本 {_lots(row.get('float_share'))}，"
            f"昨收 {_price(_num(row.get('pre_close')))}，"
            f"涨停价 {_price(_num(row.get('up_limit')))}，"
            f"跌停价 {_price(_num(row.get('down_limit')))}"
        ),
    }]
    return _body(facts, facts[0]["text"])


def format_limit(rows: list[dict], days: int | None = None) -> dict:
    span = days or 20
    if not rows:
        return _body([{"id": "fl.limit", "text": f"近 {span} 个交易日没有涨停、跌停或炸板记录"}], f"近 {span} 个交易日没有涨停、跌停或炸板记录")
    bits = []
    for row in rows[-8:]:
        kind = _LIMIT.get(str(row.get("limit") or ""), "涨跌停")
        times = _num(row.get("limit_times"))
        opened = _num(row.get("open_times"))
        extra = []
        if times:
            extra.append(f"连板 {int(times)}")
        if opened:
            extra.append(f"开板 {int(opened)} 次")
        tail = ("，" + "，".join(extra)) if extra else ""
        bits.append(f"{_short_date(row.get('trade_date'))} {kind}{tail}")
    text = "涨跌停记录：" + "；".join(bits)
    return _body([{"id": "fl.limit", "text": text}], text)


def format_toplist(rows: list[dict], days: int | None = None) -> dict:
    span = days or 20
    if not rows:
        return _body([{"id": "fl.toplist", "text": f"近 {span} 个交易日没有上龙虎榜"}], f"近 {span} 个交易日没有上龙虎榜")
    bits = []
    for row in rows[-8:]:
        reason = str(row.get("reason") or "").strip()
        bits.append(" ".join(part for part in (
            _short_date(row.get("trade_date")),
            _yi(_num(row.get("net_amount"))),
            reason,
        ) if part))
    text = "龙虎榜：" + "；".join(bits)
    return _body([{"id": "fl.toplist", "text": text}], text)


def format_margin(rows: list[dict], days: int | None = None) -> dict:
    usable = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        usable.append({
            **row,
            "net": None if _num(row.get("rzmre")) is None or _num(row.get("rzche")) is None else _num(row.get("rzmre")) - _num(row.get("rzche")),
        })
    if not usable:
        return _body([], "没有取到融资余额和融资买入")
    latest = usable[-1]
    facts = [{
        "id": "fl.margin",
        "text": (
            f"{_short_date(latest.get('trade_date'))} 融资余额 {_yi(_num(latest.get('rzye')))}，"
            f"融资买入额 {_yi(_num(latest.get('rzmre')))}，"
            f"融资净买入 {_yi(latest.get('net'))}"
        ),
    }]
    def total(n: int) -> str:
        part = usable[-n:]
        buy = sum(_num(row.get("rzmre")) or 0 for row in part)
        net = sum(row.get("net") or 0 for row in part if row.get("net") is not None)
        return f"近 {len(part)} 日融资买入额合计 {_yi(buy)}，融资净买入合计 {_yi(net)}"
    facts.append({"id": "fl.margin_5d", "text": total(5)})
    for n in sorted({n for n in (10, 20) if n <= len(usable)} | ({days} if days and days not in (5,) else set())):
        facts.append({"id": f"fl.margin_{n}d", "text": total(n)})
    window = usable[-min(days or 5, len(usable)):]
    daily = [
        f"{_short_date(row.get('trade_date'))} 余额 {_yi(_num(row.get('rzye')))} 买入 {_yi(_num(row.get('rzmre')))} 净买入 {_yi(row.get('net'))}"
        for row in window
    ]
    facts.append({"id": "fl.margin_days", "text": f"近 {len(window)} 日：" + "；".join(daily)})
    return _body(facts, " ".join(item["text"] for item in facts))


def _dc(stock: Stock, days=None) -> dict:
    from app.services.series import get_flow_daily

    try:
        payload = get_flow_daily(stock, 250)
    except Exception:
        return _doc(stock, "dc", [], "没有取到东财个股资金流")
    body = format_dc(payload.get("items") or [], days)
    return _doc(stock, "dc", body["facts"], body["text"])


def _factor(stock: Stock, days=None) -> dict:
    rows = _cached_rows(stock, "factor", lambda pro, start, end: pro.stk_factor(
        ts_code=stock.ts_code, start_date=start, end_date=end,
        fields="ts_code,trade_date,close,macd_dif,macd_dea,macd,kdj_k,kdj_d,kdj_j,rsi_6,rsi_12,rsi_24,boll_upper,boll_mid,boll_lower,cci",
    ))
    body = format_factor(rows)
    return _doc(stock, "factor", body["facts"], body["text"])


def _chip(stock: Stock, days=None) -> dict:
    rows = _cached_rows(stock, "chip", lambda pro, start, end: pro.cyq_perf(
        ts_code=stock.ts_code, start_date=start, end_date=end,
    ), span=20)
    latest = rows[-1] if rows else None
    close = None
    bar = _cached_rows(stock, "barclose", lambda pro, start, end: pro.daily(
        ts_code=stock.ts_code, start_date=start, end_date=end, fields="trade_date,close",
    ), span=10)
    if bar:
        close = _num(bar[-1].get("close"))
    body = format_chip(latest, close)
    return _doc(stock, "chip", body["facts"], body["text"])


def _bar(stock: Stock, days=None) -> dict:
    from app.services.sector_detail.tsutil import call, records
    from app.tools.tushare_client import get_pro

    end = datetime.now(_ZONE).date()
    back = max(100, int((days or 0) * 1.7) + 20)          # 默认够算到 60 日；指定更长的天数再往前取
    start = end.fromordinal(end.toordinal() - back).strftime("%Y%m%d")
    stop = end.strftime("%Y%m%d")
    key = f"flow_reply:bar:{stock.ts_code}:{stop}:{back}"
    cached = cache.get(key)
    if isinstance(cached, list):
        body = format_bar(cached, days)
        return _doc(stock, "bar", body["facts"], body["text"])
    import tushare as ts

    get_pro()
    frame = call(lambda: ts.pro_bar(
        ts_code=stock.ts_code, adj="qfq", start_date=start, end_date=stop, ma=[5, 20], factors=["tor"],
    ))
    if frame is not None and hasattr(frame, "reset_index") and "trade_date" not in getattr(frame, "columns", []):
        frame = frame.reset_index()
    rows = sorted(records(frame), key=lambda row: str(row.get("trade_date") or ""))
    if rows:
        cache.set(key, rows, config.CACHE_TTL["bars"])
    body = format_bar(rows, days)
    return _doc(stock, "bar", body["facts"], body["text"])


def _premarket(stock: Stock, days=None) -> dict:
    rows = _cached_rows(stock, "premarket", lambda pro, start, end: pro.stk_premarket(
        ts_code=stock.ts_code, start_date=start, end_date=end,
    ), span=15)
    body = format_premarket(rows[-1] if rows else None)
    return _doc(stock, "premarket", body["facts"], body["text"])


def _limit(stock: Stock, days=None) -> dict:
    span = max(40, int((days or 0) * 1.7) + 10)
    rows = _cached_rows(stock, f"limit{span}", lambda pro, start, end: pro.limit_list_d(
        ts_code=stock.ts_code, start_date=start, end_date=end,
    ), span=span)
    if days:                              # 只留最近 days 个交易日（按日期近似：每个交易日约 1.45 个日历日）
        cut = datetime.now(_ZONE).date().fromordinal(datetime.now(_ZONE).date().toordinal() - int(days * 1.45) - 2).strftime("%Y%m%d")
        rows = [row for row in rows if str(row.get("trade_date") or "") >= cut]
    body = format_limit(rows, days)
    return _doc(stock, "limit", body["facts"], body["text"])


def _margin(stock: Stock, days=None) -> dict:
    span = max(40, int((days or 0) * 1.7) + 10)
    rows = _cached_rows(stock, f"margin{span}", lambda pro, start, end: pro.margin_detail(
        ts_code=stock.ts_code, start_date=start, end_date=end,
        fields="trade_date,rzye,rzmre,rzche",
    ), span=span)
    body = format_margin(rows, days)
    return _doc(stock, "margin", body["facts"], body["text"])


def _toplist(stock: Stock, days=None) -> dict:
    from app.services.series import get_toplist

    try:
        payload = get_toplist(stock, days or 20)
    except Exception:
        return _doc(stock, "toplist", [], "没有取到龙虎榜")
    body = format_toplist(payload.get("items") or [], days)
    return _doc(stock, "toplist", body["facts"], body["text"])


def _cached_rows(stock: Stock, name: str, fetch, span: int = 40) -> list[dict]:
    from app.services.sector_detail.tsutil import call, records
    from app.tools.tushare_client import get_pro

    end = datetime.now(_ZONE).date()
    start = end.fromordinal(end.toordinal() - span).strftime("%Y%m%d")
    stop = end.strftime("%Y%m%d")
    key = f"flow_reply:{name}:{stock.ts_code}:{stop}"
    cached = cache.get(key)
    if isinstance(cached, list):
        return cached
    frame = call(lambda: fetch(get_pro(), start, stop))
    rows = sorted(records(frame), key=lambda row: str(row.get("trade_date") or ""))
    if rows or frame is not None:
        cache.set(key, rows, config.CACHE_TTL["flow_daily"])
    return rows


def _doc(stock: Stock, part: str, facts: list[dict], text: str) -> dict:
    return {
        "ts_code": stock.ts_code,
        "stock_name": stock.name,
        "part": part,
        "facts": facts,
        "text": text,
        "unavailable": [] if facts else ["没有取到"],
    }


def _body(facts: list[dict], text: str) -> dict:
    return {"facts": facts, "text": text}


def _sum(rows: list[dict], key: str) -> float | None:
    values = [_num(row.get(key)) for row in rows]
    values = [item for item in values if item is not None]
    if not values:
        return None
    return sum(values)


def _num(value):
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _num_text(value) -> str:
    number = _num(value)
    if number is None:
        return "没有"
    return f"{number:.2f}"


def _lots(value) -> str:
    number = _num(value)
    if number is None:
        return "没有"
    return f"{number:.2f} 万股"


def _price(value) -> str:
    if value is None:
        return "没有"
    return f"{value:.2f}"


def _wan(value) -> str:
    if value is None:
        return "没有"
    return f"{value / 10000:+.2f} 亿元"


def _yi(value) -> str:
    if value is None:
        return ""
    return f"{value / 1e8:+.2f} 亿元"


def _pct_points(value) -> str:
    if value is None:
        return "没有"
    return f"{value:+.2f}%"


def _short_date(value) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[4:6]}-{digits[6:8]}"
    return str(value or "")
