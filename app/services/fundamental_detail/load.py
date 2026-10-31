"""从 Tushare 取基本面详情要用的收盘、财报和同行。取数失败的单项留空，不编造。"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from app import config
from app.tools import cache
from app.tools.stock import Stock
from app.services.fundamental_detail.rules import (
    choose_metric,
    earnings_distorted,
    five_year_start,
    industry_level,
    is_st_name,
    parse_ymd,
    pledge_fraction,
    pledge_view,
    weekly_points,
)

_ZONE = ZoneInfo("Asia/Shanghai")
_FINA_FIELDS = (
    "ts_code,ann_date,end_date,roe,debt_to_assets,tr_yoy,netprofit_yoy,"
    "profit_dedt,ocf_to_profit,ocf_to_or,netprofit_margin,eps"
)
_FINA_COLUMNS = [
    "ann_date", "end_date", "roe", "debt_to_assets", "tr_yoy", "netprofit_yoy",
    "profit_dedt", "ocf_to_profit", "ocf_to_or", "netprofit_margin", "eps",
]
_STATEMENT_START_DAYS = 2200
_PLEDGE_FRESH_DAYS = 540


def settled_row(rows: list[dict], now: datetime) -> dict | None:
    """收盘后才用当天这根 K 线。盘中仍用上一交易日收盘。"""
    if not rows:
        return None
    latest = rows[-1]
    day = parse_ymd(latest.get("trade_date"))
    if day is not None and day == now.date() and (now.hour, now.minute) < (15, 0):
        return rows[-2] if len(rows) > 1 else None
    return latest


def load_snapshot(stock: Stock, now: datetime | None = None) -> dict:
    from app.tools.tushare_client import get_pro

    now = now or datetime.now(_ZONE)
    pro = get_pro()
    bars = _daily(pro, stock.ts_code, now)
    chosen = settled_row(bars, now)
    if chosen is None:
        raise RuntimeError(f"未获取到 {stock.ts_code} 的日线数据")
    price_ymd = _ymd(chosen.get("trade_date"))
    close = _num(chosen.get("close"))
    if len(price_ymd) < 8 or close is None:
        raise RuntimeError(f"未获取到 {stock.ts_code} 的日线数据")

    names = _name_map(pro)
    history_rows = _valuation_history(pro, stock.ts_code, price_ymd)
    latest_basic = history_rows[-1] if history_rows else {}
    pe = _num(latest_basic.get("pe_ttm"))
    pb = _num(latest_basic.get("pb"))
    ps = _num(latest_basic.get("ps_ttm"))
    total_mv = _num(latest_basic.get("total_mv"))
    circ_mv = _num(latest_basic.get("circ_mv"))

    statements = _statements(pro, stock.ts_code, price_ymd)
    deducted = _deducted_pe(statements["fina"], total_mv)
    profit_yoy = _deducted_yoy(statements["fina"])
    quality, ocf_years, extras = _self_quality(statements)
    industry = _industry(pro, stock)
    metric, value, metric_text, metric_reason = choose_metric(
        industry.get("name") or stock.industry, pe, pb, ps, deducted,
        quality.get("profitability"), profit_yoy,
    )
    field = {"pb": "pb", "ps_ttm": "ps_ttm"}.get(metric, "pe_ttm")
    history = _weekly(history_rows, field, price_ymd)
    if value is not None and history:
        history[-1]["value"] = value
    elif value is not None:
        history = [{"date": _iso(price_ymd), "value": value}]

    market = _market_basic(pro, price_ymd)
    members, st_excluded = _without_st(industry.get("members") or [], names, stock.ts_code)
    panel = _peer_panel(
        pro, members, stock.ts_code, price_ymd, industry.get("code"),
        "ocf_to_or" if extras.get("thin_profit") else "ocf_to_np", market,
    )
    if extras.get("thin_profit") and extras.get("ocf_to_sales") is not None:
        quality["cash"] = extras["ocf_to_sales"]
    if metric == "pe_ttm_deducted":
        peers = _deducted_peers(members, panel["deducted"], stock.ts_code, value)
    else:
        peers = _peer_values(members, market, field, stock.ts_code, value)
    _apply_names(peers, names, stock)
    usable, excluded = _coverage(peers, stock.ts_code)
    excluded += st_excluded
    industry_block = {
        "code": industry.get("code"),
        "name": industry.get("name") or stock.industry,
        "peer_count": usable,
        "peers_excluded": excluded,
        "status": "empty" if usable <= 0 and industry.get("status") != "unavailable" else (industry.get("status") or "unavailable"),
    }
    if industry.get("status") == "unavailable":
        industry_block["status"] = "unavailable"
    peer_quality = panel["quality"]
    table = _table(
        pe, pb, ps, quality, market, members, peer_quality,
        deducted, panel["deducted"], stock.ts_code, bool(extras.get("thin_profit")),
    )
    events = _holder_events(pro, stock, price_ymd, close, circ_mv)
    roe = quality.get("profitability")
    return {
        "close": close,
        "business": _business(pro, stock.ts_code),
        "stock_name": _listed_name(names, stock),
        "price_date": _iso(price_ymd),
        "list_date": (names.get(stock.ts_code) or {}).get("list_date") or _list_date(pro, stock.ts_code),
        "report_end": statements.get("report_end"),
        "industry": industry_block,
        "metric": metric,
        "metric_text": metric_text,
        "metric_reason": metric_reason,
        "value": value,
        "distorted": earnings_distorted(roe, profit_yoy),
        "pe_reference": deducted if deducted is not None else pe,
        "cash_beats_profit": _cash_counts(roe, extras) and extras.get("ocf_to_np") is not None and extras["ocf_to_np"] > 1,
        "history": history,
        "peers": peers,
        "quality": quality,
        "quality_notes": _quality_notes(extras, roe),
        "peer_quality": peer_quality,
        "ocf_negative_years": ocf_years,
        "quotes": {"pe_ttm": pe, "pb": pb, "ps_ttm": ps},
        "table": table,
        "holder_events": events,
        "trade_dates": _future_dates(pro, price_ymd),
    }


_BIZ_TOTALS = {"产品", "主营业务", "营业收入", "总计", "合计", "主营业务收入"}   # 汇总行，不是产品线，算占比会重复计入


def _bounded_yoy(sales: float, last) -> float | None:
    """同比。上年同期没有或太小（涨幅超过 10 倍）的不给，免得小基数产生没有意义的数字。"""
    if not last or last <= 0:
        return None
    value = sales / last - 1
    return round(value, 4) if abs(value) <= 10 else None


def _business(pro, ts_code: str) -> dict | None:
    """主营业务：公司简介里的主营描述，加最近一期按产品拆分的营收、占比、同比、毛利率。取不到就是 None，不影响其他判断。"""
    try:
        return _business_of(pro, ts_code)
    except Exception:
        return None


def _business_of(pro, ts_code: str) -> dict | None:
    company = _records(
        _call(lambda: pro.stock_company(ts_code=ts_code, fields="ts_code,main_business")), ["main_business"],
    )
    description = str((company[0] if company else {}).get("main_business") or "").strip()[:200]
    rows = _records(
        _call(lambda: pro.fina_mainbz(ts_code=ts_code, type="P")), ["end_date", "bz_item", "bz_sales", "bz_profit", "bz_cost"],
    )
    by_period: dict[str, dict[str, dict]] = {}
    for row in rows:
        item = str(row.get("bz_item") or "").strip()
        sales = _num(row.get("bz_sales"))
        if not item or sales is None or sales <= 0 or "调整" in item or "合计" in item or item in _BIZ_TOTALS:
            continue
        by_period.setdefault(_ymd(row.get("end_date")), {})[item] = row
    if not by_period and not description:
        return None
    # 只剩"其他业务"说明没有真正的产品拆分（有的公司只披露汇总行），不展示
    by_period = {end: rows_ for end, rows_ in by_period.items() if set(rows_) - {"其他业务"}}
    if not by_period and not description:
        return None
    period = max(by_period) if by_period else ""
    items = []
    if period:
        current = by_period[period]
        total = sum(_num(row["bz_sales"]) for row in current.values())
        before = by_period.get(f"{int(period[:4]) - 1}{period[4:]}") or {}
        for name, row in sorted(current.items(), key=lambda pair: -_num(pair[1]["bz_sales"]))[:6]:
            sales = _num(row["bz_sales"])
            last = _num((before.get(name) or {}).get("bz_sales"))
            profit = _num(row.get("bz_profit"))
            items.append({
                "item": name,
                "sales_yi": round(sales / 1e8, 2),
                "share": round(sales / total, 4),
                "yoy": _bounded_yoy(sales, last),
                "gross_margin": None if profit is None else round(profit / sales, 4),
            })
    return {"main_business": description, "period": _iso(period) if period else "", "items": items}


def _daily(pro, ts_code: str, now: datetime) -> list[dict]:
    start = (now - timedelta(days=30)).strftime("%Y%m%d")
    end = now.strftime("%Y%m%d")
    frame = _call(lambda: pro.daily(
        ts_code=ts_code,
        start_date=start,
        end_date=end,
        fields="ts_code,trade_date,close",
    ))
    rows = _records(frame, ["trade_date", "close"])
    rows.sort(key=lambda item: _ymd(item.get("trade_date")))
    return rows


def _valuation_history(pro, ts_code: str, price_ymd: str) -> list[dict]:
    end = parse_ymd(price_ymd)
    start = five_year_start(end)
    rows = []
    seen = set()
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=400))
        frame = _call(lambda left=cursor, right=chunk_end: pro.daily_basic(
            ts_code=ts_code,
            start_date=left.strftime("%Y%m%d"),
            end_date=right.strftime("%Y%m%d"),
            fields="trade_date,pe_ttm,pb,ps_ttm,total_mv,circ_mv",
        ))
        for item in _records(frame, ["trade_date", "pe_ttm", "pb", "ps_ttm", "total_mv", "circ_mv"]):
            ymd = _ymd(item.get("trade_date"))
            if not ymd or ymd in seen or ymd > price_ymd:
                continue
            seen.add(ymd)
            rows.append(item)
        cursor = chunk_end + timedelta(days=1)
    rows.sort(key=lambda item: _ymd(item.get("trade_date")))
    return rows


def _weekly(rows: list[dict], field: str, price_ymd: str) -> list[dict]:
    end = parse_ymd(price_ymd)
    start = five_year_start(end)
    samples = []
    for item in rows:
        day = parse_ymd(item.get("trade_date"))
        if day is None:
            continue
        samples.append((day, _num(item.get(field))))
    return weekly_points(samples, start, end)


def _statements(pro, ts_code: str, price_ymd: str) -> dict:
    start_day = parse_ymd(price_ymd) - timedelta(days=_STATEMENT_START_DAYS)
    start = start_day.strftime("%Y%m%d")
    fina = [
        item for item in _records(_fina(pro, ts_code, start), _FINA_COLUMNS)
        if _announced(item, price_ymd)
    ]
    income = _statement_rows(pro, "income", ts_code, start, price_ymd, "end_date,report_type,total_revenue,n_income_attr_p,basic_eps")
    cash = _statement_rows(pro, "cashflow", ts_code, start, price_ymd, "end_date,report_type,n_cashflow_act")
    dividend = _records(
        _call(lambda: pro.dividend(ts_code=ts_code, fields="end_date,div_proc,cash_div")),
        ["end_date", "div_proc", "cash_div"],
    )
    report_end = ""
    for item in fina:
        end = _ymd(item.get("end_date"))
        if end > report_end:
            report_end = end
    return {"fina": fina, "income": income, "cash": cash, "dividend": dividend, "report_end": report_end or None}


def _fina(pro, ts_code: str, start: str):
    frame = _call(lambda: pro.fina_indicator(ts_code=ts_code, start_date=start, fields=_FINA_FIELDS))
    if frame is None:
        frame = _call(lambda: pro.fina_indicator(ts_code=ts_code, start_date=start))
    return frame


def _statement_rows(pro, method: str, ts_code: str, start: str, end: str, fields: str) -> list[dict]:
    frame = _call(lambda: getattr(pro, method)(
        ts_code=ts_code, start_date=start, end_date=end, fields=f"ts_code,{fields}",
    ))
    rows = _records(frame, fields.split(","))
    cleaned = []
    for item in rows:
        report_type = item.get("report_type")
        if report_type not in (None, "", "1", 1):
            continue
        cleaned.append(item)
    return cleaned or rows


def _deducted_pe(fina_rows: list[dict], total_mv):
    profit = _ttm_profit(fina_rows)
    mv = _num(total_mv)
    if profit is None or mv is None or profit <= 0 or mv <= 0:
        return None
    return mv * 10000 / profit


def _ttm_profit(rows: list[dict]):
    by_end = _profit_by_end(rows)
    if not by_end:
        return None
    return _ttm_at(by_end, max(by_end))


def _deducted_yoy(rows: list[dict]):
    by_end = _profit_by_end(rows)
    if not by_end:
        return None
    latest = max(by_end)
    current = _ttm_at(by_end, latest)
    previous = _ttm_at(by_end, f"{int(latest[:4]) - 1}{latest[4:]}")
    if current is None or previous is None or previous <= 0:
        return None
    return current / previous - 1


def _profit_by_end(rows: list[dict]) -> dict:
    by_end = {}
    for item in rows:
        end = _ymd(item.get("end_date"))
        profit = _num(item.get("profit_dedt"))
        if len(end) < 8 or profit is None:
            continue
        by_end[end] = profit
    return by_end


def _ttm_at(by_end: dict, end: str):
    if end not in by_end and not end.endswith("1231"):
        pass
    if end.endswith("1231"):
        return by_end.get(end)
    if end not in by_end:
        return None
    year = int(end[:4])
    md = end[4:]
    prev_fy = by_end.get(f"{year - 1}1231")
    prev_same = by_end.get(f"{year - 1}{md}")
    if prev_fy is None or prev_same is None:
        return None
    return by_end[end] + prev_fy - prev_same


def _announced(item: dict, price_ymd: str) -> bool:
    ann = _ymd(item.get("ann_date"))
    return not ann or ann <= price_ymd


def _thin_profit(margin, sales_cash) -> bool:
    """净利润占营收低于 3% 时，现金项改用经营现金流/营收。只是 ROE 低不够。"""
    return margin is not None and margin < 0.03 and sales_cash is not None


def _cash_counts(roe, extras) -> bool:
    if extras.get("thin_profit"):
        return True
    return not (roe is not None and roe < 0.05)


def _without_st(members: list[dict], names: dict, ts_code: str) -> tuple[list[dict], int]:
    kept = []
    dropped = 0
    for item in members:
        code = item.get("ts_code")
        listed = (names.get(code) or {}).get("name") if code else None
        if code != ts_code and (is_st_name(item.get("name")) or is_st_name(listed)):
            dropped += 1
            continue
        copied = dict(item)
        copied["name"] = _prefer_name(item.get("name"), listed, code or "")
        kept.append(copied)
    return kept, dropped


def _quality_notes(extras: dict, roe) -> dict:
    period = extras.get("cash_period")

    def labeled(text: str) -> str:
        if not period:
            return text
        return f"{period}口径 · {text}"

    if extras.get("thin_profit"):
        return {"cash": {
            "metric_text": "经营现金流/营收",
            "note_text": labeled("利润偏低，已改用经营现金流/营收"),
            "as_percent": True,
        }}
    if roe is not None and roe < 0.05:
        return {"cash": {"note_text": labeled("利润偏低，比值仅供参考")}}
    if period:
        return {"cash": {"note_text": f"{period}口径"}}
    return {}


def _listed_name(names: dict, stock: Stock) -> str:
    found = str((names.get(stock.ts_code) or {}).get("name") or "").strip()
    if found and found not in (stock.code, stock.ts_code):
        return found
    current = str(stock.name or "").strip()
    if current and current not in (stock.code, stock.ts_code):
        return current
    return found or current or stock.code


def _self_quality(statements: dict) -> tuple[dict, int, dict]:
    fina = statements.get("fina") or []
    latest = _newest(fina)
    income = _annual(statements.get("income") or [], "total_revenue")
    profit = _annual(statements.get("income") or [], "n_income_attr_p")
    eps = dict(_annual(statements.get("income") or [], "basic_eps"))
    ocf = _annual(statements.get("cash") or [], "n_cashflow_act")
    growth = _cagr(income)
    if growth is None and latest:
        growth = _from_percent(latest.get("tr_yoy"))
        if growth is None:
            growth = _from_percent(latest.get("netprofit_yoy"))
    cash = _cash_ratio(ocf, profit)
    cash_year = ocf[-1][0] if cash is not None and ocf else None
    if cash is None and latest:
        cash = _ocf_ratio(latest.get("ocf_to_profit"))
    sales_cash = _ocf_to_sales(ocf, income)
    if sales_cash is None and latest:
        sales_cash = _scaled_ratio(latest.get("ocf_to_or"))
    roe = None if latest is None else _from_percent(latest.get("roe"))
    margin = None if latest is None else _from_percent(latest.get("netprofit_margin"))
    thin = _thin_profit(margin, sales_cash)
    quality = {
        "profitability": roe,
        "growth": growth,
        "solvency": None if latest is None else _from_percent(latest.get("debt_to_assets")),
        "cash": sales_cash if thin else cash,
        "payout": _payout(statements.get("dividend") or [], eps),
    }
    return quality, _ocf_streak(ocf), {
        "thin_profit": thin,
        "ocf_to_np": cash,
        "ocf_to_sales": sales_cash,
        "margin": margin,
        "cash_period": f"{cash_year} 年报" if cash_year else None,
    }


def _cagr(pairs: list[tuple[int, float]]):
    if len(pairs) < 2:
        return None
    last_year, last = pairs[-1]
    base_year = None
    base = None
    for year, value in pairs:
        if year <= last_year - 3 and value > 0:
            base_year, base = year, value
    if base is None or base_year is None or last <= 0:
        return None
    years = last_year - base_year
    if years <= 0:
        return None
    return (last / base) ** (1 / years) - 1


def _cash_ratio(ocf_pairs, profit_pairs):
    profits = dict(profit_pairs)
    if not ocf_pairs:
        return None
    year, ocf = ocf_pairs[-1]
    profit = profits.get(year)
    if profit is None or profit <= 0:
        return None
    return ocf / profit


def _ocf_to_sales(ocf_pairs, revenue_pairs):
    revenues = dict(revenue_pairs)
    if not ocf_pairs:
        return None
    year, ocf = ocf_pairs[-1]
    revenue = revenues.get(year)
    if revenue is None or revenue <= 0:
        return None
    return ocf / revenue


def _ocf_streak(pairs: list[tuple[int, float]]) -> int:
    streak = 0
    for _, value in reversed(pairs):
        if value < 0:
            streak += 1
            continue
        break
    return streak


def _payout(rows: list[dict], eps_by_year: dict):
    by_year: dict[int, float] = {}
    for item in rows:
        proc = str(item.get("div_proc") or "")
        if proc and "实施" not in proc:
            continue
        cash = _num(item.get("cash_div"))
        end = _ymd(item.get("end_date"))
        if cash is None or len(end) < 4:
            continue
        year = int(end[:4])
        by_year[year] = by_year.get(year, 0.0) + cash
    if not by_year:
        return None
    year = max(by_year)
    eps = eps_by_year.get(year)
    if eps is None or eps <= 0:
        return None
    ratio = by_year[year] / eps
    if ratio > 1.5:
        ratio = by_year[year] / 10 / eps
    if ratio <= 0 or ratio > 1.5:
        return None
    return ratio


def _annual(rows: list[dict], field: str) -> list[tuple[int, float]]:
    by_year = {}
    for item in rows:
        end = _ymd(item.get("end_date"))
        if not end.endswith("1231"):
            continue
        value = _num(item.get(field))
        if value is None:
            continue
        by_year[int(end[:4])] = value
    return sorted(by_year.items())


def _newest(rows: list[dict]) -> dict | None:
    picked = None
    picked_end = ""
    for item in rows:
        end = _ymd(item.get("end_date"))
        if end >= picked_end:
            picked = item
            picked_end = end
    return picked


def _industry(pro, stock: Stock) -> dict:
    key = f"fundamental:sw:{stock.ts_code}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    found = _sw_industry(pro, stock)
    if found.get("status") != "unavailable":
        cache.set(key, found, config.CACHE_TTL["fundamental_detail"])
    return found


def _sw_industry(pro, stock: Stock) -> dict:
    blank = {
        "code": None,
        "name": stock.industry,
        "peer_count": 0,
        "status": "unavailable",
        "members": [],
    }
    frame = _call(lambda: pro.index_member_all(ts_code=stock.ts_code, is_new="Y"))
    l3_code, l3_name, l2_code, l2_name = _levels(frame)
    if l3_code:
        members = _member_rows(pro, l3_code, "l3")
        others = 0 if not members else len([item for item in members if item["ts_code"] != stock.ts_code])
        if members and industry_level(others) == "l3":
            return _pack(l3_code, l3_name, members, stock.ts_code)
    if l2_code:
        members = _member_rows(pro, l2_code, "l2")
        if members:
            return _pack(l2_code, l2_name, members, stock.ts_code)
    classified = _call(lambda: pro.index_classify(level="L2", src="SW2021"))
    matched = _match_classify(classified, stock.industry)
    if matched is None:
        return blank
    code, name = matched
    members = _member_rows(pro, code, "l2")
    if members is None:
        return {"code": code, "name": name or stock.industry, "peer_count": 0, "status": "unavailable", "members": []}
    return _pack(code, name or stock.industry, members, stock.ts_code)


def _levels(frame) -> tuple[str | None, str | None, str | None, str | None]:
    rows = _records(frame, ["l2_code", "l2_name", "l3_code", "l3_name"])
    if not rows:
        return None, None, None, None
    row = rows[0]
    return (
        str(row.get("l3_code") or "") or None,
        str(row.get("l3_name") or "") or None,
        str(row.get("l2_code") or "") or None,
        str(row.get("l2_name") or "") or None,
    )


def _member_rows(pro, code: str | None, level: str):
    if not code:
        return None
    frame = _call(lambda: pro.index_member_all(**{f"{level}_code": code, "is_new": "Y"}))
    rows = _member_list(frame, ["ts_code", "con_code", "name"])
    if rows:
        return rows
    frame = _call(lambda: pro.index_member(index_code=code, is_new="Y"))
    rows = _member_list(frame, ["con_code", "ts_code", "name"])
    if not rows:
        return None if frame is None else []
    names = _name_map(pro)
    for item in rows:
        listed = (names.get(item["ts_code"]) or {}).get("name")
        item["name"] = _prefer_name(item.get("name"), listed, item["ts_code"])
    return rows


def _member_list(frame, columns: list[str]) -> list[dict]:
    rows = _records(frame, columns)
    members = []
    seen = set()
    for item in rows:
        code = str(item.get("ts_code") or item.get("con_code") or "")
        if not code or code in seen or not code.endswith((".SH", ".SZ", ".BJ")):
            continue
        seen.add(code)
        members.append({"ts_code": code, "name": _prefer_name(item.get("name"), None, code)})
    return members


def _match_classify(frame, industry: str | None):
    text = (industry or "").strip()
    if not text:
        return None
    rows = _records(frame, ["industry_code", "index_code", "industry_name"])
    hits = []
    for item in rows:
        name = str(item.get("industry_name") or "")
        if name == text or name.startswith(text):
            code = str(item.get("index_code") or item.get("industry_code") or "")
            if code:
                hits.append((code, name))
    if not hits:
        return None
    hits.sort(key=lambda item: (not str(item[1]).endswith("Ⅱ"), len(item[1])))
    return hits[0]


def _pack(code: str, name: str | None, members: list[dict], ts_code: str) -> dict:
    others = [item for item in members if item["ts_code"] != ts_code]
    return {
        "code": code,
        "name": name,
        "peer_count": len(others),
        "status": "ok" if others else "empty",
        "members": members,
    }


def _market_basic(pro, price_ymd: str) -> dict:
    key = f"fundamental:daily_basic:{price_ymd}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    frame = _call(lambda: pro.daily_basic(
        trade_date=price_ymd,
        fields="ts_code,pe_ttm,pb,ps_ttm,total_mv,circ_mv",
    ))
    mapping = {}
    for item in _records(frame, ["ts_code", "pe_ttm", "pb", "ps_ttm", "total_mv", "circ_mv"]):
        code = str(item.get("ts_code") or "")
        if code:
            mapping[code] = item
    if mapping:
        cache.set(key, mapping, config.CACHE_TTL["fundamental_detail"])
    return mapping


def _peer_values(members, market: dict, field: str, ts_code: str, self_value) -> list[dict]:
    rows = []
    for item in members:
        code = item["ts_code"]
        if code == ts_code and self_value is not None:
            value = self_value
        else:
            value = _num((market.get(code) or {}).get(field))
        rows.append({"ts_code": code, "name": item.get("name") or code, "value": value, "is_self": code == ts_code})
    return rows


def _peer_panel(pro, members, ts_code: str, price_ymd: str, industry_code, cash_basis: str, market: dict) -> dict:
    empty = {"profitability": [], "growth": [], "solvency": [], "cash": [], "payout": []}
    blank = {"quality": empty, "deducted": {}}
    if not industry_code:
        return blank
    key = f"fundamental:peerpanel:{industry_code}:{price_ymd[:6]}:{cash_basis}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    start = (parse_ymd(price_ymd) - timedelta(days=800)).strftime("%Y%m%d")
    sample = [item for item in members if item["ts_code"] != ts_code]
    buckets = {name: [] for name in empty}
    deducted = {}
    for item in sample:
        code = item["ts_code"]
        rows = [
            row for row in _records(_fina(pro, code, start), _FINA_COLUMNS)
            if _announced(row, price_ymd)
        ]
        newest = _newest(rows)
        profit = _ttm_profit(rows)
        mv = _num((market.get(code) or {}).get("total_mv"))
        if profit is not None and profit > 0 and mv is not None and mv > 0:
            deducted[code] = mv * 10000 / profit
        else:
            deducted[code] = None
        if newest is None:
            continue
        _append(buckets["profitability"], _from_percent(newest.get("roe")))
        growth = _from_percent(newest.get("tr_yoy"))
        if growth is None:
            growth = _from_percent(newest.get("netprofit_yoy"))
        _append(buckets["growth"], growth)
        _append(buckets["solvency"], _from_percent(newest.get("debt_to_assets")))
        if cash_basis == "ocf_to_or":
            _append(buckets["cash"], _scaled_ratio(newest.get("ocf_to_or")))
        else:
            _append(buckets["cash"], _ocf_ratio(newest.get("ocf_to_profit")))
        _append(buckets["payout"], _peer_payout(pro, code, rows))
    result = {"quality": buckets, "deducted": deducted}
    if any(buckets.values()) or deducted:
        cache.set(key, result, config.CACHE_TTL["fundamental_detail"])
    return result


def _peer_payout(pro, ts_code: str, fina_rows: list[dict]):
    year, eps = _annual_eps(fina_rows)
    if year is None or eps is None or eps <= 0:
        return None
    rows = _records(
        _call(lambda code=ts_code: pro.dividend(ts_code=code, fields="end_date,div_proc,cash_div")),
        ["end_date", "div_proc", "cash_div"],
    )
    return _payout(rows, {year: eps})


def _annual_eps(rows: list[dict]):
    best_end = ""
    best = None
    for item in rows:
        end = _ymd(item.get("end_date"))
        eps = _num(item.get("eps"))
        if end.endswith("1231") and eps is not None and eps > 0 and end >= best_end:
            best_end = end
            best = eps
    if not best_end:
        return None, None
    return int(best_end[:4]), best


def _deducted_peers(members, deducted: dict, ts_code: str, self_value) -> list[dict]:
    rows = []
    for item in members:
        code = item["ts_code"]
        value = self_value if code == ts_code else deducted.get(code)
        rows.append({
            "ts_code": code,
            "name": item.get("name") or code,
            "value": value,
            "is_self": code == ts_code,
        })
    return rows


def _apply_names(peers: list[dict], names: dict, stock: Stock) -> None:
    for item in peers:
        code = item["ts_code"]
        listed = (names.get(code) or {}).get("name")
        if code == stock.ts_code:
            item["name"] = _listed_name(names, stock)
            continue
        item["name"] = _prefer_name(item.get("name"), listed, code)


def _prefer_name(primary, fallback, code: str) -> str:
    symbol = code.split(".")[0]
    for candidate in (primary, fallback):
        text = str(candidate or "").strip()
        if text and text not in (code, symbol):
            return text
    return code


def _coverage(peers: list[dict], ts_code: str) -> tuple[int, int]:
    usable = 0
    excluded = 0
    for item in peers:
        if item.get("ts_code") == ts_code:
            continue
        value = _num(item.get("value"))
        if value is not None and 0 < value <= 200:
            usable += 1
        else:
            excluded += 1
    return usable, excluded


def _table(pe, pb, ps, quality: dict, market: dict, members: list[dict], peer_quality: dict, deducted, deducted_map: dict, ts_code: str, thin_cash: bool) -> dict:
    codes = [item["ts_code"] for item in members if item["ts_code"] != ts_code]
    pe_values = []
    for code in codes:
        value = deducted_map.get(code)
        if value is not None and 0 < value <= 200:
            pe_values.append(value)
    pe_label = "市盈率（TTM，扣非）" if deducted is not None else "市盈率 TTM（含非经常损益）"
    cash_label = "经营现金流/营收" if thin_cash else "经营现金流/净利润"
    return {
        "pe_ttm": {
            "value": deducted if deducted is not None else pe,
            "peer_median": _list_median(pe_values) if deducted is not None else _market_median(market, codes, "pe_ttm"),
            "label": pe_label,
        },
        "pb": {"value": pb, "peer_median": _market_median(market, codes, "pb")},
        "ps_ttm": {"value": ps, "peer_median": _market_median(market, codes, "ps_ttm")},
        "roe": {"value": quality.get("profitability"), "peer_median": _list_median(peer_quality.get("profitability") or [])},
        "debt_to_assets": {"value": quality.get("solvency"), "peer_median": _list_median(peer_quality.get("solvency") or [])},
        "ocf_to_np": {
            "value": quality.get("cash"),
            "peer_median": _list_median(peer_quality.get("cash") or []),
            "label": cash_label,
            "format": "percent" if thin_cash else "ratio",
        },
    }


def _market_median(market: dict, codes: list[str], field: str):
    nums = []
    for code in codes:
        value = _num((market.get(code) or {}).get(field))
        if value is not None and 0 < value <= 200:
            nums.append(value)
    if not nums:
        return None
    nums.sort()
    mid = len(nums) // 2
    if len(nums) % 2:
        return nums[mid]
    return (nums[mid - 1] + nums[mid]) / 2


def _list_median(values: list):
    nums = sorted(float(item) for item in values if item is not None)
    if not nums:
        return None
    mid = len(nums) // 2
    if len(nums) % 2:
        return nums[mid]
    return (nums[mid - 1] + nums[mid]) / 2


def _holder_events(pro, stock: Stock, price_ymd: str, close, circ_mv) -> list[dict]:
    events = []
    buyback = _buyback(pro, stock.ts_code, price_ymd, close, circ_mv)
    if buyback is not None:
        events.append(buyback)
    events.extend(_unlocks(pro, stock.ts_code, price_ymd))
    events.extend(_holder_trades(pro, stock.ts_code, price_ymd))
    pledge = _pledge(pro, stock.ts_code, price_ymd)
    if pledge is not None:
        events.append(pledge)
    holders = _holder_count(pro, stock.ts_code)
    if holders is not None:
        events.append(holders)
    return events


def _buyback(pro, ts_code: str, price_ymd: str, close, circ_mv):
    start = (parse_ymd(price_ymd) - timedelta(days=800)).strftime("%Y%m%d")
    rows = _records(
        _call(lambda: pro.repurchase(ts_code=ts_code, start_date=start, end_date=price_ymd)),
        ["ann_date", "end_date", "exp_date", "proc", "amount", "high_limit"],
    )
    usable = []
    for item in rows:
        proc = str(item.get("proc") or "")
        if "停止" in proc or "失败" in proc:
            continue
        usable.append(item)
    if not usable:
        return None
    usable.sort(key=lambda item: _ymd(item.get("ann_date")))
    latest = usable[-1]
    proc = str(latest.get("proc") or "")
    done = None
    plan = None
    high_limit = None
    for item in usable:
        amount = _to_yi(item.get("amount"))
        text = str(item.get("proc") or "")
        if "预案" in text or "股东大会" in text:
            plan = amount if amount is not None else plan
        if "实施" in text or "完成" in text:
            done = amount if amount is not None else done
        if _num(item.get("high_limit")):
            high_limit = _num(item.get("high_limit"))
    if plan is None:
        plan = _to_yi(latest.get("amount"))
    if done is None and ("实施" in proc or "完成" in proc):
        done = plan
    ongoing = "完成" not in proc
    due = _ymd(latest.get("exp_date")) or _ymd(latest.get("end_date")) or _ymd(latest.get("ann_date"))
    if not ongoing and due and due < price_ymd:
        return None
    yuan = _yuan(latest.get("amount"))
    ratio = None
    mv = _num(circ_mv)
    if yuan and mv and mv > 0:
        ratio = yuan / (mv * 10000)
        if ratio <= 0 or ratio > 1:
            ratio = None
    detail = ""
    if high_limit and close:
        detail = f"现价距回购上限 {round((high_limit / close - 1) * 100):+.0f}%".replace("+", "")
    progress = None
    if done is not None or plan is not None:
        progress = {"done": done or 0, "plan": plan or done or 0, "unit": "amount"}
    return {
        "type": "buyback",
        "title": f"回购{proc}" if proc else "股份回购",
        "impact": "positive",
        "date": _iso(due) if due else None,
        "ongoing": ongoing,
        "progress": progress,
        "detail_text": detail,
        "float_ratio": ratio,
        "source_url": None,
    }


def _unlocks(pro, ts_code: str, price_ymd: str) -> list[dict]:
    end = (parse_ymd(price_ymd) + timedelta(days=80)).strftime("%Y%m%d")
    rows = _records(
        _call(lambda: pro.share_float(ts_code=ts_code, start_date=price_ymd, end_date=end)),
        ["float_date", "float_ratio", "holder_name"],
    )
    events = []
    for item in rows:
        due = _ymd(item.get("float_date"))
        if len(due) < 8 or due < price_ymd:
            continue
        ratio = _fraction(item.get("float_ratio"))
        name = item.get("holder_name") or "限售股"
        events.append({
            "type": "unlock",
            "title": f"{name}解禁",
            "impact": "negative",
            "date": _iso(due),
            "detail_text": "",
            "float_ratio": ratio,
            "source_url": None,
        })
    return events


def _holder_trades(pro, ts_code: str, price_ymd: str) -> list[dict]:
    end = (parse_ymd(price_ymd) + timedelta(days=40)).strftime("%Y%m%d")
    rows = _records(
        _call(lambda: pro.stk_holdertrade(ts_code=ts_code, start_date=price_ymd, end_date=end)),
        ["ann_date", "holder_name", "in_de", "change_ratio"],
    )
    events = []
    for item in rows:
        due = _ymd(item.get("ann_date"))
        if len(due) < 8 or due < price_ymd:
            continue
        side = str(item.get("in_de") or "").upper()
        if side in ("IN", "增持"):
            kind, impact, title = "holder_increase", "positive", "增持"
        elif side in ("DE", "减持"):
            kind, impact, title = "holder_reduce", "negative", "减持"
        else:
            continue
        name = item.get("holder_name") or "股东"
        ratio = _fraction(item.get("change_ratio"))
        progress = None
        if kind == "holder_reduce" and ratio is not None:
            progress = {"done": 0, "plan": ratio, "unit": "float_share"}
        events.append({
            "type": kind,
            "title": f"{name}{title}",
            "impact": impact,
            "date": _iso(due),
            "progress": progress,
            "detail_text": "",
            "float_ratio": ratio,
            "source_url": None,
        })
    return events


def _pledge(pro, ts_code: str, price_ymd: str):
    rows = _records(
        _call(lambda: pro.pledge_stat(ts_code=ts_code)),
        ["end_date", "pledge_ratio"],
    )
    latest = _newest(rows)
    company = None if latest is None else pledge_fraction(latest.get("pledge_ratio"))
    controller = _controller_pledge(pro, ts_code, price_ymd)
    view = pledge_view(company, controller)
    if view is None:
        return None
    return {
        "type": "pledge",
        "title": "股权质押",
        "impact": view["impact"],
        "date": None,
        "counts": False,
        "detail_text": view["detail_text"],
        "float_ratio": None,
        "risk_hit": view["risk_hit"],
        "source_url": None,
    }


def _controller_pledge(pro, ts_code: str, price_ymd: str):
    holders = _records(
        _call(lambda: pro.top10_holders(ts_code=ts_code)),
        ["end_date", "holder_name", "hold_ratio", "holder_type"],
    )
    ends = [_ymd(item.get("end_date")) for item in holders if len(_ymd(item.get("end_date"))) >= 8]
    if not ends:
        return None
    latest_end = max(ends)
    people = [
        item for item in holders
        if _ymd(item.get("end_date")) == latest_end and "自然人" in str(item.get("holder_type") or "")
    ]
    if not people:
        return None
    people.sort(key=lambda item: _num(item.get("hold_ratio")) or 0, reverse=True)
    name = people[0].get("holder_name")
    if not name:
        return None
    cutoff = (parse_ymd(price_ymd) - timedelta(days=_PLEDGE_FRESH_DAYS)).strftime("%Y%m%d")
    details = _records(
        _call(lambda: pro.pledge_detail(ts_code=ts_code)),
        ["ann_date", "holder_name", "is_release", "holding_amount", "pledged_amount"],
    )
    fresh = []
    for item in details:
        if item.get("holder_name") != name:
            continue
        ann = _ymd(item.get("ann_date"))
        if len(ann) < 8 or ann < cutoff or str(item.get("is_release") or "") in ("1", "Y", "是"):
            continue
        holding = _num(item.get("holding_amount"))
        pledged = _num(item.get("pledged_amount"))
        if holding is None or holding <= 0 or pledged is None or pledged < 0:
            continue
        ratio = pledged / holding
        if ratio > 1:
            continue
        fresh.append((ann, ratio))
    if not fresh:
        return None
    fresh.sort()
    return fresh[-1][1]


def _holder_count(pro, ts_code: str):
    rows = _records(
        _call(lambda: pro.stk_holdernumber(ts_code=ts_code)),
        ["end_date", "ann_date", "holder_num"],
    )
    usable = []
    for item in rows:
        num = _num(item.get("holder_num"))
        end = _ymd(item.get("end_date") or item.get("ann_date"))
        if num is None or len(end) < 8:
            continue
        usable.append((end, num))
    usable.sort()
    if len(usable) < 2 or usable[-2][1] <= 0:
        return None
    change = usable[-1][1] / usable[-2][1] - 1
    pct = round(change * 100)
    sign = "−" if pct < 0 else "+" if pct > 0 else ""
    end = usable[-1][0]
    return {
        "type": "holder_count",
        "title": "股东户数",
        "impact": "none",
        "date": None,
        "detail_text": f"较上期 {sign}{abs(pct)}%，截至 {end[4:6]}-{end[6:8]}",
        "float_ratio": None,
        "source_url": None,
    }


def _future_dates(pro, price_ymd: str) -> list[str]:
    end = (parse_ymd(price_ymd) + timedelta(days=50)).strftime("%Y%m%d")
    frame = _call(lambda: pro.trade_cal(exchange="SSE", start_date=price_ymd, end_date=end, is_open="1"))
    return [_ymd(item.get("cal_date")) for item in _records(frame, ["cal_date"])]


def _list_date(pro, ts_code: str) -> str | None:
    names = _name_map(pro)
    listed = (names.get(ts_code) or {}).get("list_date") if isinstance(names.get(ts_code), dict) else None
    if listed:
        return listed
    frame = _call(lambda: pro.stock_basic(ts_code=ts_code, fields="ts_code,list_date"))
    rows = _records(frame, ["list_date"])
    if not rows:
        return None
    text = _ymd(rows[0].get("list_date"))
    return text or None


def _name_map(pro) -> dict:
    cached = cache.get("fundamental:names")
    if cached is not None:
        return cached
    frame = _call(lambda: pro.stock_basic(exchange="", list_status="L", fields="ts_code,name,list_date"))
    mapping = {}
    for item in _records(frame, ["ts_code", "name", "list_date"]):
        code = str(item.get("ts_code") or "")
        if code:
            mapping[code] = {"name": item.get("name") or code, "list_date": _ymd(item.get("list_date"))}
    if mapping:
        cache.set("fundamental:names", mapping, config.CACHE_TTL["fundamental_detail"])
    return mapping


def _append(bucket: list, value):
    if value is not None:
        bucket.append(value)


def _from_percent(value):
    number = _num(value)
    if number is None:
        return None
    return number / 100


def _fraction(value):
    number = _num(value)
    if number is None:
        return None
    if abs(number) > 1:
        return number / 100
    return number


def _scaled_ratio(value):
    number = _num(value)
    if number is None:
        return None
    if abs(number) > 3:
        return number / 100
    return number


def _ocf_ratio(value):
    number = _num(value)
    if number is None:
        return None
    if abs(number) > 3:
        return number / 100
    return number


def _to_yi(value):
    number = _num(value)
    if number is None or number <= 0:
        return None
    if number >= 1_000_000:
        return number / 1e8
    return number / 1e4


def _yuan(value):
    number = _num(value)
    if number is None or number <= 0:
        return None
    if number >= 1_000_000:
        return number
    return number * 10000


def _positive(value) -> bool:
    number = _num(value)
    return number is not None and number > 0


def _call(fn):
    try:
        return fn()
    except Exception:
        return None


def _records(frame, columns: list[str]) -> list[dict]:
    if frame is None or getattr(frame, "empty", True):
        return []
    present = [column for column in columns if column in frame.columns]
    if not present:
        return []
    rows = []
    for record in frame[present].to_dict(orient="records"):
        rows.append({key: _plain(value) for key, value in record.items()})
    return rows


def _plain(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            return _plain(value.item())
        except Exception:
            pass
    return value


def _num(value):
    value = _plain(value)
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _ymd(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:8]


def _iso(value: str) -> str:
    text = _ymd(value)
    if len(text) < 8:
        return ""
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
