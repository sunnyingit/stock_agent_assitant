"""全市场资金和情绪切片。只接当前账号有权限的 Tushare 接口。

etf_share_size、rt_etf_k、limit_step、yc_cb 没有权限，不在这里。
南向 south_money 是存量，当日方向用港股通买卖额差，并写明口径。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app import config
from app.tools import cache

PARTS = (
    "turnover", "etf", "margin", "margin_industry", "south", "shibor",
    "supply", "funds", "breadth", "limits", "index", "sw", "macro",
    "fx", "us", "hk", "ipo", "calendar", "unlock", "valuation", "pulse",
)
_ZONE = ZoneInfo("Asia/Shanghai")
_TTL = 6 * 3600

# 9 只宽基。份额用 fund_share，价格和成交用 fund_daily。净申购金额 = 份额变化 × 收盘价。
ETF_BOOK = (
    ("510300.SH", "沪深300ETF", "大盘"),
    ("510310.SH", "沪深300ETF", "大盘"),
    ("159919.SZ", "沪深300ETF", "大盘"),
    ("510050.SH", "上证50ETF", "大盘"),
    ("560610.SH", "中证A500ETF", "大盘"),
    ("510500.SH", "中证500ETF", "中小盘"),
    ("512100.SH", "中证1000ETF", "中小盘"),
    ("588000.SH", "科创50ETF", "科创创业"),
    ("159915.SZ", "创业板ETF", "科创创业"),
)
_INDEXES = (
    ("000001.SH", "上证指数"),
    ("000300.SH", "沪深300"),
    ("000852.SH", "中证1000"),
    ("399006.SZ", "创业板指"),
)
_L1 = (
    "801010.SI", "801030.SI", "801040.SI", "801050.SI", "801080.SI",
    "801110.SI", "801120.SI", "801130.SI", "801140.SI", "801150.SI",
    "801160.SI", "801170.SI", "801180.SI", "801200.SI", "801210.SI",
    "801230.SI", "801710.SI", "801720.SI", "801730.SI", "801740.SI",
    "801750.SI", "801760.SI", "801770.SI", "801780.SI", "801790.SI",
    "801880.SI", "801890.SI", "801950.SI", "801960.SI", "801970.SI",
    "801980.SI",
)


def build(part: str, today: date | None = None) -> dict:
    if part not in PARTS:
        raise ValueError(f"未知切片 {part}")
    day = today or datetime.now(_ZONE).date()
    if part == "pulse":
        return _pulse(day)
    return {
        "turnover": _turnover,
        "etf": _etf,
        "margin": _margin,
        "margin_industry": _margin_industry,
        "south": _south,
        "shibor": _shibor,
        "supply": _supply,
        "funds": _funds,
        "breadth": _breadth,
        "limits": _limits,
        "index": _index,
        "sw": _sw,
        "macro": _macro,
        "fx": _fx,
        "us": _us,
        "hk": _hk,
        "ipo": _ipo,
        "calendar": _calendar,
        "unlock": _unlock,
        "valuation": _valuation,
    }[part](day)


def judge_turnover(ratio) -> str:
    if ratio is None:
        return "没有"
    if ratio < -0.05:
        return "冷清"
    if ratio > 0.05:
        return "活跃"
    return "平时"


def judge_etf(share_ratio, buy_days: int) -> str:
    if share_ratio is None:
        return "没有"
    if share_ratio >= 0.01 and buy_days >= 4:
        return "净流入"
    if share_ratio <= -0.01:
        return "净流出"
    return "基本没变"


def judge_margin(ratio_4w) -> str:
    if ratio_4w is None:
        return "没有"
    if ratio_4w < -0.05:
        return "加速下降"
    if ratio_4w < -0.02:
        return "在降"
    if ratio_4w > 0.02:
        return "在升"
    return "持平"


def judge_pulse(turnover: str, etf: str, margin: str) -> str:
    if etf == "净流入" and turnover != "冷清":
        return "在进"
    if turnover == "冷清" and margin in ("在降", "加速下降"):
        return "偏弱"
    return "观望"


def format_turnover(rows: list[dict], index_pct: dict[str, float] | None = None) -> dict:
    rows = [row for row in rows if _num(row.get("amount")) is not None]
    rows.sort(key=lambda row: _ymd(row.get("trade_date")))
    if len(rows) < 6:
        return _body([], "没有取到足够的全市场成交额")
    latest = rows[-1]
    avg5 = _mean(rows[-5:], "amount")
    avg60 = _mean(rows[-60:], "amount")
    avg20 = _mean(rows[-20:], "amount")
    ratio5 = None if not avg60 else avg5 / avg60 - 1
    ratio20 = None if not avg60 else avg20 / avg60 - 1
    label = judge_turnover(ratio5)
    year = rows[-240:]
    amounts = [_num(row.get("amount")) for row in year]
    amounts = [item for item in amounts if item is not None]
    pctile = _pctile(amounts, _num(latest.get("amount")))
    down = 0
    for row in rows[-5:]:
        pct = (index_pct or {}).get(_ymd(row.get("trade_date")))
        if pct is not None and pct < 0:
            down += 1
    facts = [
        {"id": "mkt.turnover", "text": f"{_iso(latest.get('trade_date'))} 沪深成交额 {_yi_plain(_num(latest.get('amount')))}，近 5 日相对过去 60 日 {_pct(ratio5)}，判定 {label}"},
        {"id": "mkt.turnover_20", "text": f"近 20 日相对过去 60 日 {_pct(ratio20)}"},
        {"id": "mkt.turnover_pctile", "text": f"成交额处在近一年 {'' if pctile is None else str(round(pctile * 100)) + '%'} 分位"},
    ]
    if down:
        facts.append({"id": "mkt.turnover_down", "text": f"近 5 天里 {down} 天指数下跌"})
    metrics = {
        "date": _iso(latest.get("trade_date")),
        "amount": _num(latest.get("amount")),
        "ratio5": ratio5,
        "ratio20": ratio20,
        "pctile": pctile,
        "label": label,
        "down_days": down,
        "series": [
            {"date": _iso(row.get("trade_date")), "value": _num(row.get("amount")),
             "up": (index_pct or {}).get(_ymd(row.get("trade_date")), 0) >= 0}
            for row in rows[-60:]
        ],
    }
    return _body(facts, " ".join(item["text"] for item in facts) + "。成交额只说明热不热，不代表钱在进还是出。盘中同一时刻没有接。", metrics)


def format_etf(shares: dict[str, list[dict]], closes: dict[str, list[dict]]) -> dict:
    """shares[code] = [{trade_date, fd_share}]，单位万份。closes 用来把份额变化换成亿元。"""
    groups = {"大盘": 0.0, "中小盘": 0.0, "科创创业": 0.0}
    group_known = {"大盘": False, "中小盘": False, "科创创业": False}
    start_share = 0.0
    end_share = 0.0
    counted = 0
    day_net: dict[str, float] = {}
    for code, _name, group in ETF_BOOK:
        rows = sorted(shares.get(code) or [], key=lambda row: _ymd(row.get("trade_date")))
        rows = [row for row in rows if _num(row.get("fd_share")) is not None]
        if len(rows) < 2:
            continue
        recent = rows[-6:]
        counted += 1
        start_share += _num(recent[0].get("fd_share")) or 0
        end_share += _num(recent[-1].get("fd_share")) or 0
        for prev, row in zip(recent, recent[1:]):
            day = _ymd(row.get("trade_date"))
            delta = (_num(row.get("fd_share")) or 0) - (_num(prev.get("fd_share")) or 0)
            day_net[day] = day_net.get(day, 0.0) + delta
            price = _close_on(closes.get(code) or [], day)
            if price is not None:
                groups[group] += delta * price / 10000
                group_known[group] = True
    buy_days = sum(1 for value in list(day_net.values())[-5:] if value > 0)
    if not counted or start_share <= 0:
        return _body([], "没有取到宽基 ETF 份额。etf_share_size 没有权限，这里用的是 fund_share")
    ratio = end_share / start_share - 1
    label = judge_etf(ratio, buy_days)
    money = sum(groups.values())
    bits = [
        f"9 只名单里取到 {counted} 只，份额变化 {_pct(ratio)}，判定 {label}",
        f"近 5 个交易日里篮子净申购的天数 {buy_days}",
    ]
    if any(group_known.values()):
        bits.append(
            "净申购金额约 "
            f"{money:+.2f} 亿元（份额变化 × 收盘价）："
            f"大盘 {groups['大盘']:+.2f}，中小盘 {groups['中小盘']:+.2f}，科创创业 {groups['科创创业']:+.2f}"
        )
    text = "。".join(bits) + "。这是 fund_share 加收盘价的近似，不是交易所的份额规模接口。"
    metrics = {
        "ratio": ratio, "buy_days": buy_days, "label": label, "money": money,
        "groups": groups, "counted": counted,
        **_etf_series(shares),
    }
    return _body([{"id": "mkt.etf", "text": text}], text, metrics)


def _etf_series(shares: dict[str, list[dict]]) -> dict:
    """9 只合计的总份额（亿份）按日对齐。只取每只都有数据的日子，免得有的缺一天就掉一截。"""
    by_day: dict[str, dict[str, float]] = {}
    codes = []
    for code, _name, _group in ETF_BOOK:
        rows = [row for row in shares.get(code) or [] if _num(row.get("fd_share")) is not None]
        if len(rows) < 2:
            continue
        codes.append(code)
        for row in rows:
            by_day.setdefault(_ymd(row.get("trade_date")), {})[code] = _num(row.get("fd_share"))
    days = sorted(day for day, slot in by_day.items() if len(slot) == len(codes))
    if len(days) < 2:
        return {"series": [], "total": None, "day_change": None, "ratio20": None, "pctile": None, "latest_date": ""}
    totals = [sum(by_day[day].values()) / 1e4 for day in days]
    series = [
        {"date": _iso(day), "value": total, "delta": 0.0 if index == 0 else total - totals[index - 1]}
        for index, (day, total) in enumerate(zip(days, totals))
    ]
    ratio20 = _growth(totals[-1], totals[-21]) if len(totals) > 20 else None
    return {
        "series": series[-60:],
        "total": totals[-1],
        "day_change": totals[-1] - totals[-2],
        "ratio20": ratio20,
        "pctile": _pctile(totals[-240:], totals[-1]),
        "latest_date": _iso(days[-1]),
        "items": [_etf_item(code, shares.get(code) or []) for code in codes],
    }


def _etf_item(code: str, rows: list[dict]) -> dict:
    rows = sorted([row for row in rows if _num(row.get("fd_share")) is not None], key=lambda row: _ymd(row.get("trade_date")))
    name = next((item[1] for item in ETF_BOOK if item[0] == code), code)
    last = _num(rows[-1].get("fd_share")) / 1e4
    prev = _num(rows[-2].get("fd_share")) / 1e4
    return {"code": code, "name": name, "share": last, "day_change": last - prev}


def format_margin(rows: list[dict], turnover: float | None = None) -> dict:
    by_day: dict[str, dict] = {}
    for row in rows:
        day = _ymd(row.get("trade_date"))
        if len(day) < 8:
            continue
        slot = by_day.setdefault(day, {"rzye": 0.0, "rzmre": 0.0, "rzche": 0.0, "ex": set()})
        exchange = str(row.get("exchange_id") or "")
        if exchange:
            slot["ex"].add(exchange)
        for key in ("rzye", "rzmre", "rzche"):
            value = _num(row.get(key))
            if value is not None:
                slot[key] += value
    days = sorted(day for day, slot in by_day.items() if len(slot["ex"]) >= 2 or not slot["ex"])
    if len(days) < 6:
        return _body([], "没有取到全市场两融")
    latest = by_day[days[-1]]
    prev = by_day[days[-2]]
    base5 = by_day[days[-6]]
    week = days[-21] if len(days) >= 21 else days[0]
    ratio_day = _growth(latest["rzye"], prev["rzye"])
    ratio5 = _growth(latest["rzye"], by_day[days[-6]]["rzye"])
    ratio4 = _growth(latest["rzye"], by_day[week]["rzye"])
    label = judge_margin(ratio4)
    net = latest["rzmre"] - latest["rzche"]
    share = None if not turnover else latest["rzmre"] / (turnover * 1e8)
    text = (
        f"{_iso(days[-1])} 融资余额 {_yuan_yi(latest['rzye'])}，当日 {_pct(ratio_day)}，"
        f"5 日 {_pct(ratio5)}，约 4 周 {_pct(ratio4)}，判定 {label}。"
        f"当日融资买入 {_yuan_yi(latest['rzmre'])}，偿还 {_yuan_yi(latest['rzche'])}，净买入 {_yuan_yi(net)}"
    )
    if share is not None:
        text += f"，融资买入占成交 {_pct(share)}"
    text += "。"
    metrics = {
        "date": _iso(days[-1]),
        "balance": latest["rzye"] / 1e8,
        "ratio_day": ratio_day,
        "ratio5": ratio5,
        "ratio4": ratio4,
        "label": label,
        "buy": latest["rzmre"] / 1e8,
        "repay": latest["rzche"] / 1e8,
        "net": net / 1e8,
        "share": share,
        "pctile": _pctile([by_day[day]["rzye"] for day in days[-240:]], latest["rzye"]),
        "series": [
            {"date": _iso(day), "value": by_day[day]["rzye"] / 1e8,
             "delta": (by_day[day]["rzmre"] - by_day[day]["rzche"]) / 1e8}
            for day in days[-60:]
        ],
    }
    return _body([{"id": "mkt.margin", "text": text}], text, metrics)


def format_margin_industry(members: dict[str, str], rows: list[dict]) -> dict:
    buckets: dict[str, dict] = {}
    for row in rows:
        name = members.get(str(row.get("ts_code") or ""))
        if not name:
            continue
        slot = buckets.setdefault(name, {"rzye": 0.0, "net": 0.0})
        rzye = _num(row.get("rzye"))
        buy = _num(row.get("rzmre"))
        repay = _num(row.get("rzche"))
        if rzye is not None:
            slot["rzye"] += rzye
        if buy is not None and repay is not None:
            slot["net"] += buy - repay
    if not buckets:
        return _body([], "没有取到行业两融")
    ranked = sorted(buckets.items(), key=lambda item: item[1]["net"], reverse=True)
    top = "，".join(f"{name} {_yuan_yi(slot['net'])}" for name, slot in ranked[:5])
    bottom = "，".join(f"{name} {_yuan_yi(slot['net'])}" for name, slot in ranked[-5:])
    text = f"按申万一级加总当日融资净买入。净买入最多：{top}。净买入最少：{bottom}。"
    rows = [{"name": name, "net": slot["net"] / 1e8} for name, slot in ranked]
    return _body([{"id": "mkt.margin_ind", "text": text}], text, {"rows": rows})


def format_south(hsgt: list[dict], ggt: list[dict]) -> dict:
    hsgt = sorted(hsgt, key=lambda row: _ymd(row.get("trade_date")))
    if len(hsgt) < 6:
        return _body([], "没有取到南向资金")
    latest = hsgt[-1]
    level = _num(latest.get("south_money"))
    prev = _num(hsgt[-2].get("south_money"))
    day_diff = None if level is None or prev is None else level - prev
    diffs = []
    for older, newer in zip(hsgt, hsgt[1:]):
        a, b = _num(older.get("south_money")), _num(newer.get("south_money"))
        if a is not None and b is not None:
            diffs.append(b - a)
    diff5 = sum(diffs[-5:]) if diffs else None
    ggt_rows = sorted(ggt, key=lambda row: _ymd(row.get("trade_date")))
    net = None
    if ggt_rows:
        buy = _num(ggt_rows[-1].get("buy_amount"))
        sell = _num(ggt_rows[-1].get("sell_amount"))
        if buy is not None and sell is not None:
            net = buy - sell
    text = (
        f"{_iso(latest.get('trade_date'))} south_money 存量 {_round(level)}，相对前一日差额 {_round(day_diff)}（接口单位，不是已经换算的亿元）。"
        f"近 5 日差额合计 {_round(diff5)}。这个字段近期几乎不动，不能直接写成当日净买入。"
    )
    if net is not None:
        text += (
            f" {_iso(ggt_rows[-1].get('trade_date'))} 港股通买卖额差 {net:+.2f}"
            "（buy_amount − sell_amount，单位沿用 ggt_daily）。"
        )
    text += " 北向不按日使用。"
    nets = [
        (_ymd(row.get("trade_date")), _num(row.get("buy_amount")) - _num(row.get("sell_amount")))
        for row in ggt_rows
        if _num(row.get("buy_amount")) is not None and _num(row.get("sell_amount")) is not None
    ]
    sums5 = [sum(item[1] for item in nets[index - 5:index]) for index in range(5, len(nets) + 1)]
    sums20 = [sum(item[1] for item in nets[index - 20:index]) for index in range(20, len(nets) + 1)]
    return _body([{"id": "mkt.south", "text": text}], text, {
        "date": _iso(latest.get("trade_date")),
        "level": level,
        "day_diff": day_diff,
        "diff5": diff5,
        "ggt_date": _iso(ggt_rows[-1].get("trade_date")) if ggt_rows else "",
        "ggt_net": net,
        "net5": sums5[-1] if sums5 else None,
        "net20": sums20[-1] if sums20 else None,
        "five_avg": (sum(sums5) / len(sums5)) if sums5 else None,
        "pctile20": _pctile(sums20[-240:], sums20[-1]) if sums20 else None,
        "series": [{"date": _iso(day), "value": value} for day, value in nets[-60:]],
    })


def format_shibor(rows: list[dict]) -> dict:
    rows = sorted([row for row in rows if _num(row.get("1w")) is not None], key=lambda row: _ymd(row.get("date")))
    if not rows:
        return _body([], "没有取到 Shibor")
    latest = rows[-1]
    avg = _mean(rows[-60:], "1w")
    now = _num(latest.get("1w"))
    side = "没有比较" if avg is None or now is None else ("低于" if now < avg else "高于")
    text = (
        f"{_iso(latest.get('date'))} Shibor 1周 {now}%，近 60 日均值 {None if avg is None else round(avg, 3)}%，{side}自身均值。"
        "这是 DR007 的近似，不是 DR007，也不是政策利率。"
    )
    return _body([{"id": "mkt.shibor", "text": text}], text, {
        "date": _iso(latest.get("date")),
        "rate": now,
        "avg": None if avg is None else round(avg, 3),
        "side": side,
        "label": "偏松" if side == "低于" else "偏紧" if side == "高于" else "没有",
    })


def format_supply(trades: list[dict], buybacks: list[dict]) -> dict:
    buy = sell = 0.0
    buy_known = sell_known = False
    for row in trades:
        vol = _num(row.get("change_vol"))
        price = _num(row.get("avg_price"))
        if vol is None or price is None:
            continue
        money = vol * price
        side = str(row.get("in_de") or "").upper()
        if side == "IN":
            buy += money
            buy_known = True
        elif side == "DE":
            sell += money
            sell_known = True
    latest_repo: dict[str, tuple[str, float]] = {}
    for row in buybacks:
        if "实施" not in str(row.get("proc") or ""):
            continue
        amount = _num(row.get("amount"))
        code = str(row.get("ts_code") or "")
        if amount is None or not code:
            continue
        stamp = _ymd(row.get("ann_date")) or _ymd(row.get("end_date"))
        prev = latest_repo.get(code)
        if prev is None or stamp >= prev[0]:
            latest_repo[code] = (stamp, amount)
    repo = sum(item[1] for item in latest_repo.values())
    if not buy_known and not sell_known and not repo:
        return _body([], "没有取到增减持和回购金额")
    net = buy + repo - sell
    label = "在拿走" if net < 0 else "有人在接"
    text = (
        f"公告区间增持 {_yuan_yi(buy)}，减持 {_yuan_yi(sell)}，回购 {_yuan_yi(repo)}，"
        f"增持 + 回购 − 减持 = {_yuan_yi(net)}，判定 {label}。"
        "回购按公司只保留最新一条实施公告，金额是公告里的累计回购，不是这段时间的增量。"
        "定增和再融资没有全市场日接口，没有算进去。"
    )
    return _body([{"id": "mkt.supply", "text": text}], text, {
        "buy": buy / 1e8, "sell": sell / 1e8, "repo": repo / 1e8, "net": net / 1e8, "label": label,
    })


def format_funds(rows: list[dict], today: date) -> dict:
    start = f"{today.year}{today.month:02d}01"
    recent_start = _ymd_offset(today, 70)
    picked = []
    recent = []
    for row in rows:
        kind = str(row.get("fund_type") or "")
        if kind not in ("股票型", "混合型"):
            continue
        issued = _ymd(row.get("issue_date") or row.get("found_date"))
        if len(issued) < 8:
            continue
        amount = _num(row.get("issue_amount"))
        item = (issued, amount, str(row.get("name") or ""))
        if issued >= start:
            picked.append(item)
        elif issued >= recent_start:
            recent.append(item)
    months = monthly_funds(rows, today)
    if not picked:
        if not recent:
            return _body([], "近两个月没有取到新发偏股基金")
        known = [item[1] for item in recent if item[1]]
        amount = f"{sum(known):.2f} 亿份" if known else "份额多数没公布"
        text = f"本月还没有新发偏股基金。近两个月 {len(recent)} 只，发行份额 {amount}。月频，只看方向，不参与短期判断。"
        return _body([{"id": "mkt.funds", "text": text}], text, {
            "count": len(recent), "amount": sum(known) if known else None, "scope": "近两个月", "months": months,
        })
    known = [item[1] for item in picked if item[1]]
    amount = f"{sum(known):.2f} 亿份" if known else "份额多数没公布"
    text = f"本月以来新发偏股基金 {len(picked)} 只，发行份额 {amount}。月频，只看方向，不参与短期判断。"
    return _body([{"id": "mkt.funds", "text": text}], text, {
        "count": len(picked), "amount": sum(known) if known else None, "scope": "本月", "months": months,
    })


def monthly_funds(rows: list[dict], today: date) -> list[dict]:
    """近 12 个月新发偏股基金（股票型 + 混合型）的只数和发行份额（亿份，没公布的不算）。"""
    keys = []
    year, month = today.year, today.month
    for _ in range(12):
        keys.append(f"{year}{month:02d}")
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    keys.reverse()
    slots = {key: {"count": 0, "amount": 0.0} for key in keys}
    for row in rows:
        if str(row.get("fund_type") or "") not in ("股票型", "混合型"):
            continue
        key = _ymd(row.get("issue_date") or row.get("found_date"))[:6]
        if key not in slots:
            continue
        slots[key]["count"] += 1
        amount = _num(row.get("issue_amount"))
        if amount:
            slots[key]["amount"] += amount
    return [{"month": f"{key[:4]}-{key[4:]}", **slots[key]} for key in keys]


def format_breadth(today_rows: list[dict], history: dict[str, list[float]]) -> dict:
    up = down = flat = 0
    highs = lows = 0
    for row in today_rows:
        pct = _num(row.get("pct_chg"))
        if pct is None:
            continue
        if pct > 0:
            up += 1
        elif pct < 0:
            down += 1
        else:
            flat += 1
        code = str(row.get("ts_code") or "")
        close = _num(row.get("close"))
        past = history.get(code) or []
        if close is None or len(past) < 20:
            continue
        if close >= max(past):
            highs += 1
        if close <= min(past):
            lows += 1
    if not (up or down):
        return _body([], "没有取到涨跌家数")
    text = f"上涨 {up} 家，下跌 {down} 家，平盘 {flat} 家。近 25 个交易日新高 {highs} 家，新低 {lows} 家。"
    return _body([{"id": "mkt.breadth", "text": text}], text, {"up": up, "down": down, "flat": flat, "highs": highs, "lows": lows})


def format_limits(rows: list[dict], today_pct: dict[str, float] | None = None, prev_rows: list[dict] | None = None) -> dict:
    """rows 是当天涨跌停名单；prev_rows 是前一交易日的名单，今天的涨跌幅 today_pct 用来算昨涨停今日表现。"""
    counts = {"U": 0, "D": 0, "Z": 0}
    height = 0
    for row in rows:
        kind = str(row.get("limit") or "")
        if kind in counts:
            counts[kind] += 1
        times = _num(row.get("limit_times"))
        if kind == "U" and times is not None:
            height = max(height, int(times))
    if not any(counts.values()):
        return _body([], "没有取到涨跌停")
    follow = None
    follow_count = 0
    if today_pct and prev_rows:
        vals = [today_pct[str(row.get("ts_code") or "")] for row in prev_rows
                if str(row.get("limit") or "") == "U" and str(row.get("ts_code") or "") in today_pct]
        if vals:
            follow = sum(vals) / len(vals)
            follow_count = len(vals)
    tried = counts["U"] + counts["Z"]
    broken_rate = counts["Z"] / tried if tried else None
    text = f"涨停 {counts['U']}，跌停 {counts['D']}，炸板 {counts['Z']}，最高连板 {height}。不含 ST。"
    if follow is not None:
        text += f"昨天涨停的 {follow_count} 只，今天平均涨跌 {follow:+.2f}%。"
    text += "连板天梯接口没有权限，高度来自 limit_times。"
    return _body([{"id": "mkt.limits", "text": text}], text, {
        "up": counts["U"], "down": counts["D"], "broken": counts["Z"], "height": height,
        "broken_rate": broken_rate, "follow": follow, "follow_count": follow_count,
    })


def format_index(bars: dict[str, list[dict]], basics: dict[str, dict]) -> dict:
    bits = []
    series = []
    for code, name in _INDEXES:
        rows = sorted(bars.get(code) or [], key=lambda row: _ymd(row.get("trade_date")))
        rows = [row for row in rows if _num(row.get("close")) is not None]
        if not rows:
            continue
        last = rows[-1]
        ret20 = _window_return(rows, 20)
        pe = _num((basics.get(code) or {}).get("pe_ttm"))
        closes = [_num(row.get("close")) for row in rows]
        ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else None
        ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
        close = closes[-1]
        # 前低 = 今天之前 20 个交易日的最低收盘价；今天收在它下面就算破位
        prior = closes[-21:-1] if len(closes) >= 21 else []
        low = min(prior) if prior else None
        bits.append(f"{name} {_iso(last.get('trade_date'))} {close}，近 20 日 {_pct(ret20)}，PE(TTM) {pe}")
        series.append({
            "name": name, "code": code, "date": _iso(last.get("trade_date")),
            "close": close, "pct": _num(last.get("pct_chg")), "ret20": ret20, "pe": pe,
            "ma60": ma60, "above_ma60": None if ma60 is None else close >= ma60,
            "above_ma20": None if ma20 is None else close >= ma20,
            "prior_low": low, "broke_low": None if low is None else close < low,
        })
    if not bits:
        return _body([], "没有取到指数")
    text = "；".join(bits) + "。"
    return _body([{"id": "mkt.index", "text": text}], text, {"rows": series})


def format_sw(rows: list[dict]) -> dict:
    usable = []
    for row in rows:
        code = str(row.get("ts_code") or "")
        if code not in _L1:
            continue
        pct = _num(row.get("pct_change"))
        if pct is None:
            continue
        usable.append((str(row.get("name") or code), pct))
    if not usable:
        return _body([], "没有取到申万行业日线")
    usable.sort(key=lambda item: item[1], reverse=True)
    lead = "，".join(f"{name} {pct:+.2f}%" for name, pct in usable[:5])
    lag = "，".join(f"{name} {pct:+.2f}%" for name, pct in usable[-5:])
    text = f"申万一级涨幅前五：{lead}。后五：{lag}。"
    return _body([{"id": "mkt.sw", "text": text}], text, {
        "lead": [{"name": name, "pct": pct} for name, pct in usable[:5]],
        "lag": [{"name": name, "pct": pct} for name, pct in usable[-5:]],
    })


def format_macro(money: list[dict], ppi: list[dict], finance: list[dict]) -> dict:
    bits = []
    metrics = {}
    by_month: dict[str, dict] = {}
    for row in money:
        by_month.setdefault(str(row.get("month") or ""), {}).update(m1=_num(row.get("m1_yoy")), m2=_num(row.get("m2_yoy")))
    for row in ppi:
        by_month.setdefault(str(row.get("month") or ""), {}).update(ppi=_num(row.get("ppi_yoy")))
    metrics["history"] = [{"month": _month(key), **by_month[key]} for key in sorted(by_month) if key][-6:]
    if money:
        row = sorted(money, key=lambda item: str(item.get("month") or ""))[-1]
        bits.append(f"{_month(row.get('month'))} M1 同比 {_num(row.get('m1_yoy'))}%，M2 同比 {_num(row.get('m2_yoy'))}%")
        metrics.update({"month": _month(row.get("month")), "m1_yoy": _num(row.get("m1_yoy")), "m2_yoy": _num(row.get("m2_yoy"))})
    if ppi:
        row = sorted(ppi, key=lambda item: str(item.get("month") or ""))[-1]
        bits.append(f"PPI 同比 {_num(row.get('ppi_yoy'))}%")
        metrics["ppi_yoy"] = _num(row.get("ppi_yoy"))
    if finance:
        row = sorted(finance, key=lambda item: str(item.get("month") or ""))[-1]
        bits.append(f"社融当月增量 {_num(row.get('inc_month'))} 亿元")
        metrics["sf_month"] = _num(row.get("inc_month"))
    if not bits:
        return _body([], "没有取到宏观月频")
    text = "；".join(bits) + "。这是接口里最新的月份，月频，不参与短期资金判断。中国 10 年国债收益率没有权限。"
    return _body([{"id": "mkt.macro", "text": text}], text, metrics)


def format_fx(rows: list[dict]) -> dict:
    rows = sorted(rows, key=lambda row: _ymd(row.get("trade_date")))
    if not rows:
        return _body([], "没有取到汇率")
    last = rows[-1]
    close = _num(last.get("bid_close"))
    base = _num(rows[-6].get("bid_close")) if len(rows) > 5 else None
    text = f"{_iso(last.get('trade_date'))} 美元兑离岸人民币收盘 {close}。"
    return _body([{"id": "mkt.fx", "text": text}], text, {
        "date": _iso(last.get("trade_date")), "close": close, "change5": _growth(close, base),
    })


def format_us(rows: list[dict]) -> dict:
    rows = [row for row in rows if _num(row.get("y10")) is not None]
    rows.sort(key=lambda row: _ymd(row.get("date")))
    if not rows:
        return _body([], "没有取到美债收益率")
    last = rows[-1]
    y10 = _num(last.get("y10"))
    base = _num(rows[-6].get("y10")) if len(rows) > 5 else None
    text = f"{_iso(last.get('date'))} 美国 10 年国债收益率 {y10}%。"
    return _body([{"id": "mkt.us", "text": text}], text, {
        "date": _iso(last.get("date")), "y10": y10, "change5": None if base is None or y10 is None else y10 - base,
    })


def format_hk(rows: list[dict]) -> dict:
    rows = sorted(rows, key=lambda row: _ymd(row.get("trade_date")))
    if not rows:
        return _body([], "没有取到恒生科技")
    last = rows[-1]
    close = _num(last.get("close"))
    pct = _num(last.get("pct_chg"))
    base = _num(rows[-6].get("close")) if len(rows) > 5 else None
    text = f"{_iso(last.get('trade_date'))} 恒生科技 {close}，涨跌 {pct}%。代码是 HKTECH。"
    return _body([{"id": "mkt.hk", "text": text}], text, {
        "date": _iso(last.get("trade_date")), "close": close, "pct": pct, "change5": _growth(close, base),
    })


def format_ipo(rows: list[dict]) -> dict:
    if not rows:
        return _body([{"id": "mkt.ipo", "text": "这个窗口没有新股"}], "这个窗口没有新股", {"count": 0, "days": []})
    bits = []
    for row in rows[:8]:
        bits.append(f"{row.get('name') or row.get('ts_code')} {_iso(row.get('ipo_date'))} 发行价 {_num(row.get('price'))}")
    text = f"新股 {len(rows)} 只：" + "；".join(bits) + "。"
    days: dict[str, int] = {}
    for row in rows:
        day = _iso(row.get("ipo_date"))
        if day:
            days[day] = days.get(day, 0) + 1
    return _body([{"id": "mkt.ipo", "text": text}], text, {
        "count": len(rows), "days": [{"date": day, "count": days[day]} for day in sorted(days)],
    })


_US_KEYWORDS = ("利率决议", "美联储", "联邦公开市场", "非农", "CPI", "PPI", "失业率", "PMI", "GDP", "PCE")


def format_calendar(events: list[dict], opens: list[dict]) -> dict:
    picked = []
    for row in events:
        country = str(row.get("country") or "")
        if country not in ("中国", "美国"):
            continue
        event = str(row.get("event") or "")
        if not (event.startswith("中国") or event.startswith("美国")):
            continue
        if event.startswith("美国") and not any(word in event for word in _US_KEYWORDS):
            continue
        event = event.replace("(年度)", "").replace("(%)", "")
        picked.append((0 if event.startswith("中国") else 1, f"{_iso(row.get('date'))} {event}"))
    picked.sort()
    chosen = [item[1] for item in sorted(picked, key=lambda item: item[1])[:8]]
    ordered = sorted(opens, key=lambda row: _ymd(row.get("cal_date")))
    days = [f"{_iso(row.get('cal_date'))} {'开市' if str(row.get('is_open')) == '1' else '休市'}" for row in ordered[:6]]
    text = "宏观：" + ("；".join(chosen) if chosen else "没有筛到中国或美国事件") + "。交易日：" + "，".join(days) + "。"
    return _body([{"id": "mkt.calendar", "text": text}], text, {
        "events": [{"date": item.split(" ", 1)[0], "name": item.split(" ", 1)[-1]} for item in chosen],
        "days": [{"date": _iso(row.get("cal_date")), "open": str(row.get("is_open")) == "1"} for row in ordered[:6]],
    })


def format_unlock(rows: list[dict]) -> dict:
    if not rows:
        return _body([{"id": "mkt.unlock", "text": "这个窗口没有解禁"}], "这个窗口没有解禁")
    total = 0.0
    ranked = []
    for row in rows:
        shares = _num(row.get("float_share"))
        if shares is None:
            continue
        total += shares
        ranked.append((shares, str(row.get("ts_code") or ""), _iso(row.get("float_date"))))
    ranked.sort(reverse=True)
    top = "，".join(f"{code} {day} {shares / 1e8:.2f} 亿股" for shares, code, day in ranked[:5])
    text = f"窗口内解禁 {len(ranked)} 条，合计 {total / 1e8:.2f} 亿股。较大的几条：{top}。"
    capped = len(rows) >= 6000
    if capped:
        text += " 单次最多 6000 条，后面的没有算进来。"
    return _body([{"id": "mkt.unlock", "text": text}], text, {
        "count": len(ranked),
        "total": total / 1e8,
        "capped": capped,
        "top": [{"code": code, "date": day, "shares": shares / 1e8} for shares, code, day in ranked[:5]],
    })


def _pulse(today: date) -> dict:
    turn = _turnover(today)
    etf = _etf(today)
    margin = _margin(today)
    labels = {}
    for key, doc in (("turnover", turn), ("etf", etf), ("margin", margin)):
        labels[key] = _label_from(doc.get("text") or "")
    lean = judge_pulse(labels["turnover"], labels["etf"], labels["margin"])
    text = f"短期判定 {lean}。成交 {labels['turnover']}，宽基 {labels['etf']}，两融 {labels['margin']}。改成在进要宽基净流入并且成交不是冷清。"
    return _doc("pulse", [{"id": "mkt.pulse", "text": text}], text + " " + " ".join(doc.get("text") or "" for doc in (turn, etf, margin)))


def _label_from(text: str) -> str:
    for label in ("加速下降", "净流入", "净流出", "基本没变", "冷清", "活跃", "平时", "在降", "在升", "持平"):
        if label in text:
            return label
    return "没有"


def _turnover(today: date) -> dict:
    start = _ymd_offset(today, 400)
    rows = []
    index_pct = {}
    for market in ("SH_MARKET", "SZ_MARKET"):
        frame = _cached(f"turnover:{market}:{start}", lambda m=market: _pro().daily_info(
            ts_code=m, start_date=start, end_date=_ymd(today), fields="trade_date,amount",
        ))
        for row in frame:
            rows.append(row)
    totals: dict[str, float] = {}
    for row in rows:
        amount = _num(row.get("amount"))
        day = _ymd(row.get("trade_date"))
        if amount is None or len(day) < 8:
            continue
        totals[day] = totals.get(day, 0.0) + amount
    merged = [{"trade_date": day, "amount": amount} for day, amount in totals.items()]
    if merged:
        latest = max(merged, key=lambda row: row["trade_date"])
        cache.set(f"market_tape:turnover:total:{_ymd(today)}", {"latest": latest["amount"]}, _TTL)
    idx = _cached(f"indexpct:{start}", lambda: _pro().index_daily(
        ts_code="000001.SH", start_date=start, end_date=_ymd(today), fields="trade_date,pct_chg",
    ))
    for row in idx:
        index_pct[_ymd(row.get("trade_date"))] = _num(row.get("pct_chg")) or 0
    body = format_turnover(merged, index_pct)
    return _from("turnover", body)


def _etf(today: date) -> dict:
    start = _ymd_offset(today, 400)
    shares: dict[str, list] = {}
    closes: dict[str, list] = {}
    for code, _name, _group in ETF_BOOK:
        shares[code] = _cached(f"fdshare:{code}:{start}", lambda c=code: _pro().fund_share(
            ts_code=c, start_date=start, end_date=_ymd(today),
        ))
        closes[code] = _cached(f"fddaily:{code}:{start}", lambda c=code: _pro().fund_daily(
            ts_code=c, start_date=start, end_date=_ymd(today), fields="ts_code,trade_date,close,amount",
        ))
    body = format_etf(shares, closes)
    return _from("etf", body)


def _margin(today: date) -> dict:
    start = _ymd_offset(today, 400)
    rows = _cached(f"margin:{start}", lambda: _pro().margin(start_date=start, end_date=_ymd(today)))
    turn = _turnover(today)
    latest_amount = None
    # turnover text starts with a date; amount is recovered from cached totals via a second cheap read
    totals = _cached_value(f"turnover:total:{_ymd(today)}")
    if isinstance(totals, dict):
        latest_amount = totals.get("latest")
    body = format_margin(rows, latest_amount)
    _ = turn
    return _from("margin", body)


def _margin_industry(today: date) -> dict:
    members = _member_map()
    rows: list[dict] = []
    used = _ymd(today)
    for trade_day in reversed(_open_dates(today, 5) or [used]):
        rows = _cached(f"margin_detail:{trade_day}", lambda d=trade_day: _pro().margin_detail(
            trade_date=d, fields="ts_code,rzye,rzmre,rzche",
        ))
        if rows:
            used = trade_day
            break
    body = format_margin_industry(members, rows)
    text = body["text"] if not rows else f"{_iso(used)} " + body["text"]
    facts = [{"id": "mkt.margin_ind", "text": text}] if rows else body["facts"]
    return _doc("margin_industry", facts, text, body.get("metrics"))


def _member_map() -> dict[str, str]:
    cached = cache.get("market_tape:l1members")
    if isinstance(cached, dict):
        return cached
    mapping = {}
    for code in _L1:
        rows = _records(lambda c=code: _pro().index_member_all(l1_code=c, is_new="Y"))
        name = ""
        for row in rows:
            name = str(row.get("l1_name") or name)
            stock = str(row.get("ts_code") or "")
            if stock:
                mapping[stock] = name or code
    if mapping:
        cache.set("market_tape:l1members", mapping, 24 * 3600)
    return mapping


def _south(today: date) -> dict:
    start = _ymd_offset(today, 400)
    hsgt = _cached(f"hsgt:{start}", lambda: _pro().moneyflow_hsgt(start_date=start, end_date=_ymd(today)))
    ggt = _cached(f"ggt:{start}", lambda: _pro().ggt_daily(start_date=start, end_date=_ymd(today)))
    body = format_south(hsgt, ggt)
    return _from("south", body)


def _shibor(today: date) -> dict:
    start = _ymd_offset(today, 120)
    rows = _cached(f"shibor:{start}", lambda: _pro().shibor(start_date=start, end_date=_ymd(today)))
    body = format_shibor(rows)
    return _from("shibor", body)


def _supply(today: date) -> dict:
    start = _ymd_offset(today, 28)
    wide = _ymd_offset(today, 84)
    trades = _cached(f"holder:{start}", lambda: _pro().stk_holdertrade(start_date=start, end_date=_ymd(today)))
    repos = _cached(f"repo:{start}", lambda: _pro().repurchase(start_date=start, end_date=_ymd(today)))
    body = format_supply(trades, repos)
    wide_trades = _cached(f"holder:{wide}", lambda: _pro().stk_holdertrade(start_date=wide, end_date=_ymd(today)))
    body["metrics"]["weeks"] = weekly_supply(wide_trades, today)
    return _from("supply", body)


def weekly_supply(trades: list[dict], today: date) -> list[dict]:
    """近 12 周每周增持、减持金额（亿元）。回购是累计口径，不能拆到周，这里不放。"""
    weeks = []
    for back in range(11, -1, -1):
        end = today - timedelta(days=back * 7)
        begin = end - timedelta(days=6)
        weeks.append([_ymd(begin), _ymd(end), 0.0, 0.0])
    for row in trades:
        day = _ymd(row.get("ann_date"))
        vol = _num(row.get("change_vol"))
        price = _num(row.get("avg_price"))
        if len(day) < 8 or vol is None or price is None:
            continue
        side = str(row.get("in_de") or "").upper()
        for week in weeks:
            if week[0] <= day <= week[1]:
                week[2 if side == "IN" else 3] += vol * price / 1e8
                break
    return [
        {"label": f"W{index + 1}", "start": _iso(week[0]), "end": _iso(week[1]), "buy": week[2], "sell": week[3]}
        for index, week in enumerate(weeks)
    ]


def _funds(today: date) -> dict:
    rows = _cached(f"fund_basic:{today.year}{today.month:02d}", lambda: _pro().fund_basic(
        market="O", fields="ts_code,name,fund_type,issue_date,found_date,issue_amount",
    ))
    body = format_funds(rows, today)
    return _from("funds", body)


def _data_days(today: date, count: int = 4) -> list[str]:
    """最近几个交易日，新的在前。当天数据没发布时，调用方往前找第一个有数据的日子。"""
    return list(reversed(_open_dates(today, count) or [_ymd(today)]))


def _breadth(today: date) -> dict:
    days = _data_days(today)
    day = days[0]
    today_rows: list[dict] = []
    for day in days:
        today_rows = _cached(f"daily:{day}", lambda d=day: _pro().daily(trade_date=d, fields="ts_code,close,pct_chg"))
        if today_rows:
            break
    history: dict[str, list[float]] = {}
    dates = [item for item in _open_dates(today, 26) if item < day][-24:]
    for trade_day in dates:
        rows = _cached(f"daily:{trade_day}", lambda d=trade_day: _pro().daily(trade_date=d, fields="ts_code,close,pct_chg"))
        for row in rows:
            close = _num(row.get("close"))
            code = str(row.get("ts_code") or "")
            if close is None or not code:
                continue
            history.setdefault(code, []).append(close)
    body = format_breadth(today_rows, history)
    body["metrics"]["date"] = _iso(day)
    return _from("breadth", body)


def _limits(today: date) -> dict:
    days = _data_days(today)
    for index, day in enumerate(days):
        rows = _cached(f"limit:{day}", lambda d=day: _pro().limit_list_d(trade_date=d))
        if not rows:
            continue
        prev_rows: list[dict] = []
        today_pct: dict[str, float] = {}
        if index + 1 < len(days):
            prev = days[index + 1]
            prev_rows = _cached(f"limit:{prev}", lambda d=prev: _pro().limit_list_d(trade_date=d))
            daily = _cached(f"daily:{day}", lambda d=day: _pro().daily(trade_date=d, fields="ts_code,close,pct_chg"))
            today_pct = {str(row.get("ts_code") or ""): _num(row.get("pct_chg")) for row in daily if _num(row.get("pct_chg")) is not None}
        body = format_limits(rows, today_pct, prev_rows)
        body["metrics"]["date"] = _iso(day)
        return _from("limits", body)
    return _from("limits", format_limits([]))


def _index(today: date) -> dict:
    start = _ymd_offset(today, 200)
    bars = {}
    basics = {}
    for code, _name in _INDEXES:
        bars[code] = _cached(f"idx:{code}:{start}", lambda c=code: _pro().index_daily(
            ts_code=c, start_date=start, end_date=_ymd(today), fields="trade_date,close,pct_chg",
        ))
        basic_rows = _cached(f"idxb:{code}:{start}", lambda c=code: _pro().index_dailybasic(
            ts_code=c, start_date=start, end_date=_ymd(today), fields="trade_date,pe_ttm,pb",
        ))
        if basic_rows:
            basics[code] = sorted(basic_rows, key=lambda row: _ymd(row.get("trade_date")))[-1]
    body = format_index(bars, basics)
    return _from("index", body)


def _sw(today: date) -> dict:
    for day in _data_days(today):
        rows = _cached(f"sw:{day}", lambda d=day: _pro().sw_daily(trade_date=d))
        if rows:
            body = format_sw(rows)
            body["metrics"]["date"] = _iso(day)
            return _from("sw", body)
    return _from("sw", format_sw([]))


def _macro(today: date) -> dict:
    month = today.month - 8
    year = today.year
    while month <= 0:
        month += 12
        year -= 1
    start = f"{year}{month:02d}"
    end = f"{today.year}{today.month:02d}"
    money = _cached(f"cnm:{start}", lambda: _pro().cn_m(start_m=start, end_m=end))
    ppi = _cached(f"ppi:{start}", lambda: _pro().cn_ppi(start_m=start, end_m=end))
    finance = _cached(f"sf:{start}", lambda: _pro().sf_month(start_m=start, end_m=end))
    body = format_macro(money, ppi, finance)
    return _from("macro", body)


def _fx(today: date) -> dict:
    start = _ymd_offset(today, 15)
    rows = _cached(f"fx:{start}", lambda: _pro().fx_daily(ts_code="USDCNH.FXCM", start_date=start, end_date=_ymd(today)))
    body = format_fx(rows)
    return _from("fx", body)


def _us(today: date) -> dict:
    start = _ymd_offset(today, 15)
    rows = _cached(f"ust:{start}", lambda: _pro().us_tycr(start_date=start, end_date=_ymd(today)))
    body = format_us(rows)
    return _from("us", body)


def _hk(today: date) -> dict:
    start = _ymd_offset(today, 15)
    rows = _cached(f"hktech:{start}", lambda: _pro().index_global(ts_code="HKTECH", start_date=start, end_date=_ymd(today)))
    body = format_hk(rows)
    return _from("hk", body)


def _ipo(today: date) -> dict:
    end = today + timedelta(days=14)
    rows = _cached(f"ipo:{_ymd(today)}", lambda: _pro().new_share(start_date=_ymd(today), end_date=_ymd(end)))
    body = format_ipo(rows)
    return _doc("ipo", body["facts"], body["text"], body.get("metrics"))


def _calendar(today: date) -> dict:
    end = today + timedelta(days=14)
    events = _cached(f"eco:{_ymd(today)}", lambda: _pro().eco_cal(start_date=_ymd(today), end_date=_ymd(end)))
    opens = _cached(f"cal:{_ymd(today)}", lambda: _pro().trade_cal(exchange="SSE", start_date=_ymd(today), end_date=_ymd(end)))
    body = format_calendar(events, opens)
    return _from("calendar", body)


def format_valuation(rows: list[dict]) -> dict:
    """沪深 300 盈利收益率（1/PE_TTM）在近 3 年里的分位。没有国债收益率权限，所以这不是 ERP。"""
    rows = sorted([row for row in rows if _num(row.get("pe_ttm"))], key=lambda row: _ymd(row.get("trade_date")))
    if len(rows) < 60:
        return _body([], "没有取到足够的沪深 300 估值")
    yields = [1 / _num(row.get("pe_ttm")) for row in rows]
    now = yields[-1]
    pctile = _pctile(yields, now)
    text = (
        f"{_iso(rows[-1].get('trade_date'))} 沪深 300 PE(TTM) {_num(rows[-1].get('pe_ttm')):.2f}，"
        f"盈利收益率 {now * 100:.2f}%，处在近 {len(rows)} 个交易日的 {round(pctile * 100)}% 分位。"
        "中国 10 年国债收益率没有权限，所以这里不是 ERP，只是盈利收益率自身的分位。"
    )
    return _body([{"id": "mkt.valuation", "text": text}], text, {
        "date": _iso(rows[-1].get("trade_date")), "pe": _num(rows[-1].get("pe_ttm")),
        "earn_yield": now, "pctile": pctile, "days": len(rows),
    })


def _valuation(today: date) -> dict:
    start = _ymd_offset(today, 1100)
    rows = _cached(f"idxb300:{start}", lambda: _pro().index_dailybasic(
        ts_code="000300.SH", start_date=start, end_date=_ymd(today), fields="trade_date,pe_ttm",
    ))
    return _from("valuation", format_valuation(rows))


def unlock_rows(today: date) -> list[dict]:
    end = today + timedelta(days=10)
    return _cached(f"unlock:{_ymd(today)}", lambda: _pro().share_float(start_date=_ymd(today), end_date=_ymd(end)))


def _unlock(today: date) -> dict:
    body = format_unlock(unlock_rows(today))
    return _from("unlock", body)


def _open_dates(today: date, count: int) -> list[str]:
    start = _ymd_offset(today, count * 2 + 10)
    rows = _cached(f"calopen:{start}", lambda: _pro().trade_cal(exchange="SSE", start_date=start, end_date=_ymd(today), is_open="1"))
    days = sorted(_ymd(row.get("cal_date")) for row in rows if len(_ymd(row.get("cal_date"))) >= 8)
    return days[-count:]


def _cached(name: str, fetch) -> list[dict]:
    key = f"market_tape:{name}"
    cached = cache.get(key)
    if isinstance(cached, list):
        return cached
    rows = _records(fetch)
    if rows:
        cache.set(key, rows, _TTL)
    return rows


def _cached_value(name: str):
    return cache.get(f"market_tape:{name}")


def _records(fetch) -> list[dict]:
    from app.services.sector_detail.tsutil import call, records

    return records(call(fetch))


def _pro():
    from app.tools.tushare_client import get_pro

    return get_pro()


def _close_on(rows: list[dict], day: str):
    for row in rows:
        if _ymd(row.get("trade_date")) == day:
            return _num(row.get("close"))
    return None


def _window_return(rows: list[dict], n: int):
    if len(rows) <= n:
        return None
    last = _num(rows[-1].get("close"))
    base = _num(rows[-1 - n].get("close"))
    return _growth(last, base)


def _doc(part: str, facts: list[dict], text: str, metrics: dict | None = None) -> dict:
    return {
        "part": part,
        "facts": facts,
        "text": text,
        "metrics": metrics or {},
        "unavailable": [] if facts else ["没有取到"],
    }


def _from(part: str, body: dict) -> dict:
    return _doc(part, body["facts"], body["text"], body.get("metrics"))


def _body(facts: list[dict], text: str, metrics: dict | None = None) -> dict:
    return {"facts": facts, "text": text, "metrics": metrics or {}}


def _mean(rows: list[dict], key: str):
    values = [_num(row.get(key)) for row in rows]
    values = [item for item in values if item is not None]
    if not values:
        return None
    return sum(values) / len(values)


def _growth(now, base):
    if now is None or not base:
        return None
    return now / base - 1


def _pctile(values: list[float], point) -> float | None:
    if point is None or not values:
        return None
    below = sum(1 for item in values if item <= point)
    return below / len(values)


def _round(value) -> str:
    if value is None:
        return "没有"
    return f"{value:.2f}"


def _pct(value) -> str:
    if value is None:
        return "没有"
    return f"{value * 100:+.2f}%"


def _yi_plain(value) -> str:
    if value is None:
        return "没有"
    if value >= 10000:
        return f"{value / 10000:.2f} 万亿元"
    return f"{value:.0f} 亿元"


def _yuan_yi(value) -> str:
    if value is None:
        return "没有"
    return f"{value / 1e8:+.2f} 亿元"


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


def _ymd(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:8]


def _month(value) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(text) < 6:
        return str(value or "")
    return f"{text[:4]}年{int(text[4:6])}月"


def _iso(value) -> str:
    text = _ymd(value)
    if len(text) < 8:
        return ""
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


def _ymd_offset(day: date, days: int) -> str:
    return (day - timedelta(days=days)).strftime("%Y%m%d")


# 其他市场模块复用的取数小工具
cached = _cached
pro = _pro
open_dates = _open_dates
num = _num
ymd = _ymd
iso = _iso
growth = _growth
pctile = _pctile
