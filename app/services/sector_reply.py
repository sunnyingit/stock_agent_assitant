"""板块分析师追问的五份切片。数字在这里算好，模型只引用 text。

归属、走势、成分位置、行业排行走已经算过的板块详情和资金序列。
题材热度用当日龙虎榜、涨跌停、连板天梯，再和申万、同花顺成分做交集。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from app.tools import cache
from app.tools.stock import Stock, is_hk

PARTS = ("membership", "tape", "position", "rank", "heat", "dc", "sw")
DAY_MAX = 120
# 默认多窗口：不再把最长窗口钉死在 20 日。
DEFAULT_WINDOWS = (5, 20, 60, 120)
_ZONE = ZoneInfo("Asia/Shanghai")
_STAGE = {
    "start": "启动", "main": "主升", "climax": "高潮", "diverge": "分歧",
    "ebb": "退潮", "weak": "弱势", "none": "没有明确阶段",
}
_LIMIT = {"U": "涨停", "D": "跌停", "Z": "炸板"}


def clamp_days(value) -> int | None:
    """None/缺省 → 多窗口；否则夹到 1～120。"""
    if value is None or value == "":
        return None
    try:
        days = int(float(value))
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    return max(1, min(DAY_MAX, days))


def windows_for(days: int | None) -> tuple[int, ...]:
    if days is None:
        return DEFAULT_WINDOWS
    return (days,) if days != 5 else (5, 20)


def build(stock: Stock, part: str, days=None) -> dict:
    if part not in PARTS:
        raise ValueError(f"未知切片 {part}")
    if is_hk(stock):
        return _doc(stock, part, [], "港股暂不按这套板块口径比较")
    horizon = clamp_days(days)
    return {
        "membership": lambda s: _membership(s),
        "tape": lambda s: _tape(s, horizon),
        "position": lambda s: _position(s, horizon),
        "rank": lambda s: _rank(s, horizon),
        "heat": lambda s: _heat(s),
        "dc": lambda s: _dc(s, horizon),
        "sw": lambda s: _sw(s, horizon),
    }[part](stock)


def format_membership(row: dict, concepts: list[str]) -> dict:
    facts = []
    for key, label in (("l1_name", "申万一级"), ("l2_name", "申万二级"), ("l3_name", "申万三级")):
        name = str((row or {}).get(key) or "").strip()
        if name:
            facts.append({"id": f"sm.{key[:2]}", "text": f"{label}：{name}"})
    names = []
    for name in concepts or []:
        text = str(name or "").strip()
        if text and text not in names:
            names.append(text)
    shown = names[:20]
    if shown:
        extra = f"，另有 {len(names) - len(shown)} 个" if len(names) > len(shown) else ""
        facts.append({"id": "sm.concepts", "text": "同花顺概念：" + "、".join(shown) + extra})
    if not facts:
        return _body([], "没有取到申万行业，也没有取到同花顺概念")
    return _body(facts, " ".join(item["text"] for item in facts))


def format_tape(block: dict | None, money: dict | None) -> dict:
    if not block:
        return _body([], "没有取到主板块走势")
    facts = []
    name = block.get("name") or "主板块"
    kind = "申万行业" if block.get("type") == "industry" else "同花顺概念"
    stage = _STAGE.get(block.get("stage") or "none", "没有明确阶段")
    days = block.get("days")
    lasted = f"，这一轮已 {int(days)} 个交易日" if days else ""
    facts.append({"id": "sm.stage", "text": f"{name}（{kind}）当前阶段：{stage}{lasted}"})
    windows = block.get("ret_windows") or {}
    if windows:
        for horizon in sorted(int(key) for key in windows):
            row = windows.get(horizon) or windows.get(str(horizon)) or {}
            for key, label in (
                ("sector", f"{name}近 {horizon} 日"),
                ("stock", f"本股近 {horizon} 日"),
                ("index", f"沪深300 近 {horizon} 日"),
            ):
                text = _pct(row.get(key))
                if text:
                    facts.append({"id": f"sm.ret_{key}_{horizon}d", "text": f"{label} {text}"})
            versus = _pct(row.get("stock_vs_sector"))
            if versus:
                facts.append({"id": f"sm.stock_vs_sector_{horizon}d", "text": f"本股近 {horizon} 日相对{name} {versus}"})
    else:
        for key, label in (("ret_sector", f"{name}近 20 日"), ("ret_stock", "本股近 20 日"), ("ret_index", "沪深300 近 20 日")):
            text = _pct(block.get(key))
            if text:
                facts.append({"id": f"sm.{key}", "text": f"{label} {text}"})
        versus = _pct(block.get("stock_vs_sector"))
        if versus:
            facts.append({"id": "sm.stock_vs_sector", "text": f"本股近 20 日相对{name} {versus}"})
    facts.extend(_money_facts(block, money or {}))
    return _body(facts, " ".join(item["text"] for item in facts))


def format_position(block: dict | None, flow: dict | None) -> dict:
    if not block:
        return _body([], "没有取到成分位置")
    facts = []
    name = block.get("name") or "主板块"
    if block.get("role_text"):
        facts.append({"id": "sm.role", "text": f"本股在{name}里是{block['role_text']}"})
    if block.get("mv_rank") and block.get("mv_total"):
        facts.append({"id": "sm.mv_rank", "text": f"市值排名 {block['mv_rank']}/{block['mv_total']}"})
    horizon = int(block.get("ret_days") or 20)
    if block.get("ret_rank") and block.get("ret_total"):
        facts.append({"id": "sm.ret_rank", "text": f"近 {horizon} 日涨幅排名 {block['ret_rank']}/{block['ret_total']}"})
    leaders = _names(block.get("leaders") or [], horizon)
    laggards = _names(block.get("laggards") or [], horizon)
    if leaders:
        facts.append({"id": "sm.leaders", "text": "涨幅靠前：" + "、".join(leaders)})
    if laggards:
        note = "（成分过多，只在已纳入的股票里比较）" if block.get("truncated") else ""
        facts.append({"id": "sm.laggards", "text": "涨幅靠后：" + "、".join(laggards) + note})
    facts.extend(_flow_facts(flow or {}))
    if len(facts) == 0:
        return _body([], "没有取到成分位置")
    return _body(facts, " ".join(item["text"] for item in facts))


def format_rank(rows: list[dict], subject: str, days: int = 20) -> dict:
    ordered = sorted(
        [row for row in rows if row.get("ret") is not None and row.get("code")],
        key=lambda row: (-float(row["ret"]), row.get("code") or ""),
    )
    index = next((i for i, row in enumerate(ordered) if row.get("code") == subject), None)
    if index is None or len(ordered) < 5:
        return _body([], f"没有取到申万一级近 {days} 日排行")
    start, end = max(0, index - 2), min(len(ordered), index + 3)
    facts = [{
        "id": "sm.l1_rank",
        "text": f"{ordered[index].get('name') or subject}近 {days} 日在申万一级中排第 {index + 1}/{len(ordered)}",
    }]
    bits = []
    for place, row in enumerate(ordered[start:end], start=start + 1):
        mark = "（本行业）" if row.get("code") == subject else ""
        bits.append(f"第 {place} {row.get('name') or row['code']} {_pct(row.get('ret'))}{mark}")
    facts.append({"id": "sm.l1_neighbors", "text": "前后名次：" + "；".join(bits)})
    return _body(facts, " ".join(item["text"] for item in facts))


def format_heat(groups: list[dict], lists: dict, stock_code: str) -> dict:
    facts = []
    unavailable = list(lists.get("unavailable") or [])
    limits = [row for row in lists.get("limits") or [] if row.get("ts_code")]
    by_code = {}
    for row in limits:
        by_code.setdefault(row["ts_code"], row)
    for index, group in enumerate(groups or [], start=1):
        codes = set(group.get("codes") or [])
        label = group.get("name") or "板块"
        if not codes:
            facts.append({"id": f"sm.limit_{index}", "text": f"{label}的成分名单这次没有，无法统计涨停"})
            continue
        counts = {"U": 0, "D": 0, "Z": 0}
        for code in codes:
            row = by_code.get(code)
            kind = (row or {}).get("limit")
            if kind in counts:
                counts[kind] += 1
        facts.append({
            "id": f"sm.limit_{index}",
            "text": f"{label}（{group.get('kind') or '板块'}）今日涨停 {counts['U']} 只、炸板 {counts['Z']} 只、跌停 {counts['D']} 只",
        })
    limit_missing = [name for name in unavailable if str(name).startswith("涨跌停")]
    if len(limit_missing) == 3:
        facts.append({"id": "sm.limit_self", "text": "涨跌停名单这次没有取到"})
    else:
        mine = by_code.get(stock_code)
        if mine:
            times = mine.get("limit_times")
            extra = f"，连板 {int(times)} 板" if times else ""
            facts.append({"id": "sm.limit_self", "text": f"本股今日{_LIMIT.get(mine.get('limit'), '在涨跌停名单里')}{extra}"})
        else:
            facts.append({"id": "sm.limit_self", "text": "本股今日不在涨停、跌停或炸板名单里"})
    facts.extend(_ladder(groups, lists.get("steps") or []))
    facts.extend(_billboard(groups, lists.get("tops") or []))
    if unavailable and len(facts) <= 1:
        return _body(facts, "题材热度这次没有取到：" + "、".join(unavailable), unavailable)
    text = " ".join(item["text"] for item in facts)
    if unavailable:
        text += " 没有取到：" + "、".join(unavailable)
    return _body(facts, text, unavailable)


def format_dc(
    boards: list[dict],
    trends: dict,
    hot_rows: list[dict],
    peers: list[dict],
    stock_code: str,
    unavailable: list[str] | None = None,
    trend_days: int = 20,
) -> dict:
    """东财板块。boards 的涨跌幅是百分数，trends 的区间涨跌是小数。"""
    missing = list(unavailable or [])
    facts = []
    industries = [row for row in boards if row.get("idx_type") == "行业板块"]
    concepts = sorted(
        [row for row in boards if row.get("idx_type") == "概念板块"],
        key=lambda row: abs(_num(row.get("pct_change")) or 0),
        reverse=True,
    )
    if industries:
        facts.append({"id": "sm.dc_industry", "text": "东财行业：" + "；".join(_dc_bit(row) for row in industries[:3])})
    elif "东财行业板块" not in missing and "东财成分" not in missing:
        facts.append({"id": "sm.dc_industry", "text": "没有匹配到东财行业板块"})
    if concepts:
        shown = concepts[:8]
        extra = f"，另有 {len(concepts) - len(shown)} 个" if len(concepts) > len(shown) else ""
        facts.append({"id": "sm.dc_concepts", "text": "东财概念：" + "；".join(_dc_bit(row) for row in shown) + extra})
    elif "东财概念板块" not in missing and "东财成分" not in missing:
        facts.append({"id": "sm.dc_concepts", "text": "没有匹配到东财概念板块"})
    trend_bits = []
    seen = set()
    for row in industries[:1] + concepts[:1]:
        code = row.get("code")
        if not code or code in seen or trends.get(code) is None:
            continue
        seen.add(code)
        trend_bits.append(f"{row.get('name') or code} {_pct(trends[code])}")
    if trend_bits:
        facts.append({"id": "sm.dc_trend", "text": f"东财板块近 {trend_days} 日：" + "；".join(trend_bits)})
    elif "东财行情" in missing:
        facts.append({"id": "sm.dc_trend", "text": f"东财板块近 {trend_days} 日行情这次没有取到"})
    mine = next((row for row in hot_rows if row.get("ts_code") == stock_code), None)
    if mine and mine.get("rank") is not None:
        change = _pts(mine.get("pct_change"))
        extra = f"，涨跌幅 {change}" if change else ""
        facts.append({"id": "sm.dc_hot", "text": f"本股在东财人气榜第 {int(mine['rank'])} 名{extra}"})
    elif hot_rows:
        facts.append({"id": "sm.dc_hot", "text": "本股不在东财人气榜"})
    elif "东财人气榜" in missing:
        facts.append({"id": "sm.dc_hot", "text": "东财人气榜这次没有取到"})
    if peers:
        bits = [f"{row.get('name')}第 {int(row['rank'])} 名" for row in peers[:5] if row.get("name") and row.get("rank") is not None]
        if bits:
            facts.append({"id": "sm.dc_hot_peers", "text": "东财行业成分里上了人气榜的：" + "、".join(bits)})
    if not facts:
        return _body([], "东财板块这次没有取到：" + "、".join(missing), missing)
    text = " ".join(item["text"] for item in facts)
    if missing:
        text += " 没有取到：" + "、".join(missing)
    return _body(facts, text, missing)


def _dc_bit(row: dict) -> str:
    pct = _pts(row.get("pct_change"))
    breadth = ""
    if row.get("up_num") is not None and row.get("down_num") is not None:
        breadth = f"，上涨 {int(row['up_num'])} 只、下跌 {int(row['down_num'])} 只"
    lead = ""
    if row.get("leading"):
        lead_pct = _pts(row.get("leading_pct"))
        lead = f"，领涨 {row['leading']}" + (f" {lead_pct}" if lead_pct else "")
    return f"{row.get('name') or row.get('code')} {pct}{breadth}{lead}".strip()


def format_sw(levels: list[dict]) -> dict:
    """申万行业日线。pct、pe、pb 用原值；amount 是万元；区间涨跌是小数。"""
    facts = []
    for row in levels:
        name = row.get("name") or row.get("level") or "申万行业"
        level = row.get("level") or "行业"
        latest = _pts(row.get("pct"))
        close = _num(row.get("close"))
        close_text = f"收盘 {close:.2f}" if close is not None else ""
        pe = _num(row.get("pe"))
        pb = _num(row.get("pb"))
        value = " ".join(bit for bit in (
            close_text,
            f"今日 {latest}" if latest else "",
            f"市盈率 {pe:.1f}" if pe is not None else "",
            f"市净率 {pb:.2f}" if pb is not None else "",
            f"成交额 {_wan(row.get('amount'))}" if row.get("amount") is not None else "",
        ) if bit)
        facts.append({"id": f"sm.sw_{level}", "text": f"申万{level} {name}：{value}"})
        days = []
        for item in (row.get("daily") or [])[-5:]:
            pct = _pts(item.get("pct"))
            if item.get("date") and pct:
                days.append(f"{_short_date(item['date'])} {pct}")
        if days:
            facts.append({"id": f"sm.sw_{level}_days", "text": f"{name}近 5 日：" + "，".join(days)})
        rets = row.get("ret_windows") or {}
        if rets:
            for horizon in sorted(int(key) for key in rets):
                trend = _pct(rets.get(horizon) if horizon in rets else rets.get(str(horizon)))
                if trend:
                    facts.append({"id": f"sm.sw_{level}_{horizon}d", "text": f"{name}近 {horizon} 日 {trend}"})
        else:
            trend = _pct(row.get("ret_20d"))
            if trend:
                facts.append({"id": f"sm.sw_{level}_20d", "text": f"{name}近 20 日 {trend}"})
    if not facts:
        return _body([], "没有取到申万行业日线")
    return _body(facts, " ".join(item["text"] for item in facts))


def window_big(items: list[dict], days: int) -> tuple[float | None, float | None]:
    """超大单加大单合计（万元）和净流入率（百分数）。净流入率用当日净额和净流入率反推成交额。"""
    rows = [row for row in items if isinstance(row, dict)][-days:]
    if not rows:
        return None, None
    big = 0.0
    seen = False
    implied = 0.0
    for row in rows:
        elg, large = _num(row.get("buy_elg_amount")), _num(row.get("buy_lg_amount"))
        if elg is None and large is None:
            continue
        big += (elg or 0.0) + (large or 0.0)
        seen = True
        net, rate = _num(row.get("net_amount")), _num(row.get("net_amount_rate"))
        if net not in (None, 0) and rate not in (None, 0) and (net > 0) == (rate > 0):
            implied += net / (rate / 100.0)
    if not seen:
        return None, None
    rate = None if implied == 0 else big / implied * 100.0
    return big, rate


def window_sector(items: list[dict], days: int) -> tuple[float | None, float | None]:
    """板块净额（元）和净流入率（百分数）。"""
    rows = [row for row in items if isinstance(row, dict)][-days:]
    net = 0.0
    implied = 0.0
    seen = False
    for row in rows:
        value, rate = _num(row.get("net_amount")), _num(row.get("net_amount_rate"))
        if value is None:
            continue
        net += value
        seen = True
        if rate not in (None, 0) and (value > 0) == (rate > 0):
            implied += value / (rate / 100.0)
    if not seen:
        return None, None
    return net, None if implied == 0 else net / implied * 100.0


def _membership(stock: Stock) -> dict:
    from app.services.sector_detail import affiliation as aff
    from app.services.sector_detail.discover import industry_row, ths_catalog
    from app.services.sector_detail.tsutil import call, records
    from app.tools.tushare_client import get_pro

    pro = get_pro()
    row = industry_row(pro, stock.ts_code)
    catalog = ths_catalog(pro) or {}
    names = []
    for item in records(call(lambda: pro.ths_member(con_code=stock.ts_code))):
        meta = catalog.get(str(item.get("ts_code") or ""))
        if meta and meta.get("type") == "N" and aff.usable_concept(meta["name"], int(meta.get("count") or 0)):
            names.append(meta["name"])
    return _doc(stock, "membership", * _pair(format_membership(row, names)))


def _tape(stock: Stock, days: int | None = None) -> dict:
    detail = _detail(stock)
    block = _primary(detail)
    money = _sector_money(stock, days)
    body = format_tape(_tape_block(block, stock, days), money)
    return _doc(stock, "tape", body["facts"], body["text"], body.get("unavailable") or [])


def _position(stock: Stock, days: int | None = None) -> dict:
    detail = _detail(stock)
    block = _primary(detail)
    snap = _snapshot(stock)
    members, truncated = _members(snap, block)
    flow = _stock_flow(stock, days)
    horizon = days or 60
    body = format_position(_position_block(block, members, truncated, stock.ts_code, horizon), flow)
    return _doc(stock, "position", body["facts"], body["text"], body.get("unavailable") or [])


def _rank(stock: Stock, days: int | None = None) -> dict:
    from app.services.sector_detail.discover import industry_row
    from app.services.sector_detail.rules import compound
    from app.services.sector_detail.tsutil import call, iso, records, ret, si
    from app.tools.tushare_client import get_pro

    horizon = days or 60
    pro = get_pro()
    row = industry_row(pro, stock.ts_code)
    subject = si(row.get("l1_code"))
    end = datetime.now(_ZONE).date()
    key = f"sector_l1_rank:{horizon}:{end.isoformat()}"
    rows = cache.get(key)
    if rows is None:
        names = {}
        for item in records(call(lambda: pro.index_classify(level="L1", src="SW2021"))):
            code = si(item.get("index_code"))
            if code:
                names[code] = str(item.get("industry_name") or item.get("name") or code)
        start = end.fromordinal(end.toordinal() - max(45, int(horizon * 1.8) + 10)).strftime("%Y%m%d")
        stop = end.strftime("%Y%m%d")

        def one(code: str):
            points = []
            for item in records(call(lambda current=code: pro.sw_daily(ts_code=current, start_date=start, end_date=stop))):
                day, value = iso(item.get("trade_date")), ret(item.get("pct_change"))
                if day and value is not None:
                    points.append((day, value))
            window = [value for _day, value in sorted(points)[-horizon:]]
            if len(window) < min(horizon, 20):
                return None
            return {"code": code, "name": names.get(code) or code, "ret": compound(window)}

        with ThreadPoolExecutor(max_workers=6) as pool:
            rows = [item for item in pool.map(one, list(names)) if item]
        if len(rows) >= 5:
            cache.set(key, rows, 6 * 3600)
    body = format_rank(rows or [], subject, horizon)
    return _doc(stock, "rank", body["facts"], body["text"])


def _heat(stock: Stock) -> dict:
    detail = _detail(stock)
    snap = _snapshot(stock)
    groups = _groups(detail, snap)
    if not groups:
        return _doc(stock, "heat", [], "没有板块成分，无法对照涨停和龙虎榜")
    as_of = str((detail.get("meta") or {}).get("as_of") or "")[:10].replace("-", "")
    lists = _heat_lists(as_of) if as_of else {"limits": [], "steps": [], "tops": [], "unavailable": ["没有交易日"]}
    body = format_heat(groups, lists, stock.ts_code)
    return _doc(stock, "heat", body["facts"], body["text"], body.get("unavailable") or [])


def _money_facts(block: dict, money: dict) -> list[dict]:
    if block.get("type") != "industry":
        return [{"id": "sm.sector_flow", "text": "主板块是同花顺概念，没有对应的板块资金。不用行业资金解释这个阶段。"}]
    if not money.get("ok"):
        return [{"id": "sm.sector_flow", "text": "板块资金这次没有取到"}]
    facts = []
    board = money.get("board") or ""
    name = block.get("name") or ""
    if board and name and board != name:
        facts.append({"id": "sm.flow_board", "text": f"资金口径是东财行业「{board}」，申万主板块是「{name}」"})
    nets = money.get("net_windows") or {}
    if nets:
        for horizon in sorted(int(key) for key in nets):
            text = _yi(nets.get(horizon) if horizon in nets else nets.get(str(horizon)))
            if text:
                facts.append({"id": f"sm.net_{horizon}d", "text": f"近 {horizon} 日板块资金 {text}"})
    else:
        for key, label in (("net_5d", "近 5 日板块资金"), ("net_20d", "近 20 日板块资金")):
            text = _yi(money.get(key))
            if text:
                facts.append({"id": f"sm.{key}", "text": f"{label} {text}"})
    daily = []
    for row in money.get("daily") or []:
        text = _yi(row.get("net"))
        if text and row.get("date"):
            daily.append(f"{_short_date(row['date'])} {text}")
    if daily:
        facts.append({"id": "sm.flow_daily", "text": "近 5 日逐日板块资金：" + "，".join(daily)})
    return facts


def _flow_facts(flow: dict) -> list[dict]:
    if not flow.get("ok"):
        return [{"id": "sm.stock_flow", "text": "个股资金这次没有取到"}]
    facts = []
    bigs = flow.get("big_windows") or {}
    rates = flow.get("rate_windows") or {}
    if bigs or rates:
        for horizon in sorted({int(key) for key in list(bigs) + list(rates)}):
            text = _wan_yi(bigs.get(horizon) if horizon in bigs else bigs.get(str(horizon)))
            if text:
                facts.append({"id": f"sm.big_{horizon}d", "text": f"近 {horizon} 日大资金净额 {text}"})
            rate = rates.get(horizon) if horizon in rates else rates.get(str(horizon))
            if rate is not None:
                facts.append({"id": f"sm.rate_{horizon}d", "text": f"近 {horizon} 日大资金净流入率 {rate:+.1f}%"})
    else:
        for key, label in (("big_5d", "近 5 日大资金净额"), ("big_20d", "近 20 日大资金净额")):
            text = _wan_yi(flow.get(key))
            if text:
                facts.append({"id": f"sm.{key}", "text": f"{label} {text}"})
        for key, label in (("rate_5d", "近 5 日大资金净流入率"), ("rate_20d", "近 20 日大资金净流入率")):
            value = flow.get(key)
            if value is not None:
                facts.append({"id": f"sm.{key}", "text": f"{label} {value:+.1f}%"})
    gap = flow.get("vs_sector_5d")
    if gap is not None:
        facts.append({"id": "sm.vs_sector", "text": f"近 5 日大资金净流入率相对板块 {gap:+.1f} 个百分点"})
    return facts


def _ladder(groups: list[dict], steps: list[dict]) -> list[dict]:
    parsed = []
    for row in steps:
        try:
            height = int(float(row.get("nums")))
        except (TypeError, ValueError):
            continue
        if row.get("ts_code"):
            parsed.append((height, row))
    if not parsed:
        return [{"id": "sm.ladder", "text": "连板天梯这次没有取到"}]
    highest, top = max(parsed, key=lambda item: item[0])
    bits = [f"今天市场最高 {highest} 板（{top.get('name') or top['ts_code']}）"]
    for group in groups or []:
        codes = set(group.get("codes") or [])
        hits = [f"{row.get('name') or row['ts_code']} {height} 板" for height, row in sorted(parsed, reverse=True) if row["ts_code"] in codes][:5]
        label = group.get("name") or "板块"
        bits.append(f"{label}里上了连板天梯的有：" + ("、".join(hits) if hits else "没有"))
    return [{"id": "sm.ladder", "text": "。".join(bits)}]


def _billboard(groups: list[dict], tops: list[dict]) -> list[dict]:
    if not tops and not groups:
        return []
    codes = set()
    for group in groups or []:
        codes.update(group.get("codes") or [])
    rows = [row for row in tops if row.get("ts_code") in codes]
    if not rows:
        return [{"id": "sm.toplist", "text": "今天这些板块的成分没有上龙虎榜"}]
    bits = []
    for row in rows[:8]:
        amount = _yi(row.get("net_amount"))
        reason = str(row.get("reason") or "").strip()
        bits.append(" ".join(part for part in (row.get("name") or row["ts_code"], amount, reason) if part))
    extra = f"，另有 {len(rows) - 8} 条" if len(rows) > 8 else ""
    return [{"id": "sm.toplist", "text": "成分上榜：" + "；".join(bits) + extra}]


def _names(rows: list[dict], days: int = 20) -> list[str]:
    bits = []
    key = "ret_20d" if days == 20 else f"ret_{days}d"
    for row in rows:
        text = _pct(row.get(key) if key in row else row.get("ret_20d"))
        name = row.get("name") or row.get("ts_code")
        if name and text:
            mark = "（本股）" if row.get("is_self") else ""
            bits.append(f"{name}{mark} {text}")
    return bits


def _tape_block(block: dict | None, stock: Stock | None = None, days: int | None = None) -> dict | None:
    if not block:
        return None
    strength = block.get("strength") or {}
    lines = strength.get("lines") or {}
    gaps = strength.get("gaps") or {}
    stage = block.get("stage") or {}
    out = {
        "name": block.get("name"),
        "type": block.get("type"),
        "stage": stage.get("current"),
        "days": stage.get("days_in_round"),
        "ret_sector": _last(lines.get("sector")),
        "ret_stock": _last(lines.get("stock")),
        "ret_index": _last(lines.get("index")),
        "stock_vs_sector": gaps.get("stock_vs_sector"),
    }
    horizons = [horizon for horizon in windows_for(days) if horizon >= 20]
    if not horizons:
        horizons = [20]
    windows = {}
    for horizon in horizons:
        if horizon == 20:
            windows[20] = {
                "sector": out["ret_sector"],
                "stock": out["ret_stock"],
                "index": out["ret_index"],
                "stock_vs_sector": out["stock_vs_sector"],
            }
            continue
        row = _return_window(block, stock, horizon) if stock is not None else {}
        if row:
            windows[horizon] = row
    if windows:
        out["ret_windows"] = windows
    return out


def _return_window(block: dict, stock: Stock, days: int) -> dict:
    """按指定交易日窗口重算板块 / 个股 / 沪深300 涨跌。"""
    from app.services.sector_detail.rules import compound
    from app.services.sector_detail.tsutil import call, iso, records, ret
    from app.tools.tushare_client import get_pro

    pro = get_pro()
    end = datetime.now(_ZONE).date()
    start = end.fromordinal(end.toordinal() - max(45, int(days * 1.8) + 10)).strftime("%Y%m%d")
    stop = end.strftime("%Y%m%d")
    board = str(block.get("code") or "")
    out = {}
    series = {
        "sector": board,
        "stock": stock.ts_code,
        "index": "000300.SH",
    }
    for key, code in series.items():
        if not code:
            continue
        points = []
        getter = (
            (lambda current=code: pro.sw_daily(ts_code=current, start_date=start, end_date=stop))
            if key == "sector" and block.get("type") == "industry"
            else (lambda current=code: pro.daily(ts_code=current, start_date=start, end_date=stop))
        )
        if key == "sector" and block.get("type") != "industry":
            continue
        for item in records(call(getter)):
            day = iso(item.get("trade_date"))
            value = ret(item.get("pct_change") if key == "sector" else item.get("pct_chg"))
            if day and value is not None:
                points.append((day, value))
        window = [value for _day, value in sorted(points)[-days:]]
        if len(window) >= min(days, 20):
            out[key] = compound(window)
    if out.get("stock") is not None and out.get("sector") is not None:
        out["stock_vs_sector"] = out["stock"] - out["sector"]
    return out


def _position_block(block: dict | None, members: list[dict], truncated: bool, ts_code: str, days: int = 20) -> dict | None:
    if not block:
        return None
    role = block.get("role") or {}
    pos = (block.get("evidence") or {}).get("position") or {}
    ret_key = "ret_20d"
    ordered = []
    for item in members:
        if item.get(ret_key) is None:
            continue
        ordered.append({**item, "is_self": item.get("ts_code") == ts_code})
    ordered.sort(key=lambda item: (-float(item[ret_key]), item.get("ts_code") or ""))
    return {
        "name": block.get("name"),
        "role_text": role.get("role_text"),
        "mv_rank": pos.get("rank"),
        "mv_total": pos.get("total"),
        "ret_rank": role.get("self_rank"),
        "ret_total": role.get("member_count"),
        "ret_days": 20,  # 成分涨幅仍来自详情近 20 日口径；板块/资金窗口已支持更长
        "leaders": ordered[:3],
        "laggards": list(reversed(ordered[-3:])) if len(ordered) > 3 else [],
        "truncated": truncated,
    }


def _sector_money(stock: Stock, days: int | None = None) -> dict:
    try:
        from app.services.series import get_sector

        payload = get_sector(stock, DAY_MAX)
    except Exception:
        return {"ok": False}
    items = payload.get("items") or []
    nets = {}
    for horizon in windows_for(days):
        net, _rate = window_sector(items, horizon)
        if net is not None:
            nets[horizon] = net
    daily = []
    for row in items[-5:]:
        daily.append({"date": str(row.get("trade_date") or ""), "net": _num(row.get("net_amount"))})
    board = (payload.get("board") or {}).get("name") or ""
    return {
        "ok": bool(nets),
        "board": board,
        "net_5d": nets.get(5),
        "net_20d": nets.get(20),
        "net_windows": nets,
        "daily": daily,
        "items": items,
    }


def _stock_flow(stock: Stock, days: int | None = None) -> dict:
    try:
        from app.services.series import get_flow_daily

        payload = get_flow_daily(stock, 250)
    except Exception:
        return {"ok": False}
    items = payload.get("items") or []
    bigs, rates = {}, {}
    for horizon in windows_for(days):
        big, rate = window_big(items, horizon)
        if big is not None:
            bigs[horizon] = big
        if rate is not None:
            rates[horizon] = rate
    sector = _sector_money(stock, days)
    _net, sector_rate = window_sector(sector.get("items") or [], 5)
    rate5 = rates.get(5)
    gap = None if rate5 is None or sector_rate is None else rate5 - sector_rate
    return {
        "ok": bool(bigs) or bool(rates),
        "big_5d": bigs.get(5),
        "big_20d": bigs.get(20),
        "rate_5d": rates.get(5),
        "rate_20d": rates.get(20),
        "big_windows": bigs,
        "rate_windows": rates,
        "vs_sector_5d": gap,
    }


def _heat_lists(day: str) -> dict:
    key = f"sector_heat:{day}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    from app.services.sector_detail.tsutil import call, records
    from app.tools.tushare_client import get_pro

    pro = get_pro()
    unavailable = []
    limits = []
    for kind in ("U", "D", "Z"):
        frame = call(lambda current=kind: pro.limit_list_d(trade_date=day, limit_type=current))
        if frame is None:
            unavailable.append(f"涨跌停{ _LIMIT[kind] }")
            continue
        for row in records(frame):
            limits.append({
                "ts_code": str(row.get("ts_code") or ""),
                "name": str(row.get("name") or ""),
                "limit": str(row.get("limit") or kind),
                "limit_times": _num(row.get("limit_times")),
            })
    steps = []
    frame = call(lambda: pro.limit_step(trade_date=day))
    if frame is None:
        unavailable.append("连板天梯")
    else:
        for row in records(frame):
            steps.append({"ts_code": str(row.get("ts_code") or ""), "name": str(row.get("name") or ""), "nums": row.get("nums")})
    tops = []
    frame = call(lambda: pro.top_list(trade_date=day))
    if frame is None:
        unavailable.append("龙虎榜")
    else:
        for row in records(frame):
            tops.append({
                "ts_code": str(row.get("ts_code") or ""),
                "name": str(row.get("name") or ""),
                "net_amount": _num(row.get("net_amount")),
                "reason": str(row.get("reason") or ""),
            })
    payload = {"limits": limits, "steps": steps, "tops": tops, "unavailable": unavailable}
    cache.set(key, payload, 60 if unavailable else 3600)
    return payload


def _dc(stock: Stock, days: int | None = None) -> dict:
    from app import config
    from app.services.sector_detail.rules import compound
    from app.services.sector_detail.tsutil import call, iso, records, ret
    from app.tools.tushare_client import get_pro

    horizons = [horizon for horizon in windows_for(days) if horizon >= 20] or [20]
    trend_days = max(horizons)
    pro = get_pro()
    day = _trade_day(pro)
    if not day:
        return _doc(stock, "dc", [], "没有取到最近交易日，东财板块无法对照")
    key = f"sector_dc:{stock.ts_code}:{day}:{trend_days}"
    cached = cache.get(key)
    if isinstance(cached, dict):
        return _doc(stock, "dc", cached["facts"], cached["text"], cached.get("unavailable") or [])
    unavailable = []
    member_codes = []
    frame = call(lambda: pro.dc_member(con_code=stock.ts_code, trade_date=day))
    if frame is None:
        unavailable.append("东财成分")
    else:
        member_codes = list(dict.fromkeys(str(row.get("ts_code") or "") for row in records(frame) if row.get("ts_code")))
    boards = []
    for kind in ("行业板块", "概念板块"):
        frame = call(lambda current=kind: pro.dc_index(trade_date=day, idx_type=current))
        if frame is None:
            unavailable.append(f"东财{kind}")
            continue
        for row in records(frame):
            code = str(row.get("ts_code") or "")
            if code not in member_codes:
                continue
            boards.append({
                "code": code,
                "name": str(row.get("name") or code),
                "idx_type": str(row.get("idx_type") or kind),
                "pct_change": _num(row.get("pct_change")),
                "up_num": _num(row.get("up_num")),
                "down_num": _num(row.get("down_num")),
                "leading": str(row.get("leading") or ""),
                "leading_pct": _num(row.get("leading_pct")),
            })
    from app.services.sector_detail.affiliation import NOISE

    boards = [row for row in boards if row["idx_type"] != "概念板块" or not any(word in row["name"] for word in NOISE)]
    industries = [row for row in boards if row["idx_type"] == "行业板块"]
    concepts = sorted(
        [row for row in boards if row["idx_type"] == "概念板块"],
        key=lambda row: abs(row.get("pct_change") or 0),
        reverse=True,
    )
    chosen = []
    for row in industries[:1] + concepts[:1]:
        if row["code"] not in {item["code"] for item in chosen}:
            chosen.append(row)
    end = datetime.now(_ZONE).date()
    start = end.fromordinal(end.toordinal() - max(45, int(trend_days * 1.8) + 10)).strftime("%Y%m%d")
    trends = {}
    for row in chosen:
        points = []
        frame = call(lambda current=row["code"]: pro.dc_daily(ts_code=current, start_date=start, end_date=day))
        if frame is None:
            if "东财行情" not in unavailable:
                unavailable.append("东财行情")
            continue
        for item in records(frame):
            stamp, value = iso(item.get("trade_date")), ret(item.get("pct_change"))
            if stamp and value is not None:
                points.append((stamp, value))
        window = [value for _stamp, value in sorted(points)[-trend_days:]]
        if len(window) >= min(trend_days, 20):
            trends[row["code"]] = compound(window)
    hot_rows = []
    frame = call(lambda: pro.dc_hot(trade_date=day, market="A股市场", hot_type="人气榜", is_new="Y"))
    if frame is None:
        unavailable.append("东财人气榜")
    else:
        for row in records(frame):
            hot_rows.append({
                "ts_code": str(row.get("ts_code") or ""),
                "name": str(row.get("ts_name") or row.get("name") or ""),
                "rank": _num(row.get("rank")),
                "pct_change": _num(row.get("pct_change")),
            })
    peers = []
    industry = industries[0] if industries else None
    if industry and hot_rows:
        frame = call(lambda: pro.dc_member(ts_code=industry["code"], trade_date=day))
        names = {str(row.get("con_code") or ""): str(row.get("name") or "") for row in records(frame)}
        peers = [
            {"name": names[row["ts_code"]], "rank": row["rank"]}
            for row in sorted(hot_rows, key=lambda item: item.get("rank") or 9999)
            if row.get("ts_code") in names and row.get("ts_code") != stock.ts_code and row.get("rank") is not None
        ][:5]
    body = format_dc(boards, trends, hot_rows, peers, stock.ts_code, unavailable, trend_days)
    if boards or hot_rows:
        cache.set(key, body, config.CACHE_TTL["sector"])
    return _doc(stock, "dc", body["facts"], body["text"], body.get("unavailable") or [])


def _sw(stock: Stock, days: int | None = None) -> dict:
    from app import config
    from app.services.sector_detail.discover import industry_row
    from app.services.sector_detail.rules import compound
    from app.services.sector_detail.tsutil import call, iso, records, ret, si
    from app.tools.tushare_client import get_pro

    horizons = [horizon for horizon in windows_for(days) if horizon >= 20] or [20]
    pro = get_pro()
    row = industry_row(pro, stock.ts_code)
    day = _trade_day(pro)
    key = f"sector_sw:{stock.ts_code}:{day or 'none'}:{'-'.join(str(item) for item in horizons)}"
    cached = cache.get(key)
    if isinstance(cached, dict):
        return _doc(stock, "sw", cached["facts"], cached["text"], cached.get("unavailable") or [])
    end = datetime.now(_ZONE).date()
    start = end.fromordinal(end.toordinal() - max(45, int(max(horizons) * 1.8) + 10)).strftime("%Y%m%d")
    stop = day or end.strftime("%Y%m%d")
    levels = []
    for level, code_key, name_key in (("一级", "l1_code", "l1_name"), ("二级", "l2_code", "l2_name"), ("三级", "l3_code", "l3_name")):
        code = si(row.get(code_key))
        if not code:
            continue
        points = []
        for item in records(call(lambda current=code: pro.sw_daily(ts_code=current, start_date=start, end_date=stop))):
            stamp = iso(item.get("trade_date"))
            pct = _num(item.get("pct_change"))
            if stamp and pct is not None:
                points.append((stamp, item, pct))
        points.sort(key=lambda item: item[0])
        if not points:
            continue
        latest = points[-1][1]
        rets = {}
        for horizon in horizons:
            window = [ret(pct) for _stamp, _item, pct in points[-horizon:]]
            if len(window) >= min(horizon, 20):
                rets[horizon] = compound(window)
        levels.append({
            "level": level,
            "name": str(row.get(name_key) or latest.get("name") or code),
            "close": _num(latest.get("close")),
            "pct": _num(latest.get("pct_change")),
            "pe": _num(latest.get("pe")),
            "pb": _num(latest.get("pb")),
            "amount": _num(latest.get("amount")),
            "daily": [{"date": stamp, "pct": pct} for stamp, _item, pct in points[-5:]],
            "ret_20d": rets.get(20),
            "ret_windows": rets,
        })
    body = format_sw(levels)
    if levels:
        cache.set(key, body, config.CACHE_TTL["sector"])
    return _doc(stock, "sw", body["facts"], body["text"], body.get("unavailable") or [])


def _trade_day(pro) -> str:
    from app.services.sector_detail.tsutil import call, records

    end = datetime.now(_ZONE).date()
    start = end.fromordinal(end.toordinal() - 15).strftime("%Y%m%d")
    frame = call(lambda: pro.index_daily(
        ts_code="000300.SH", start_date=start, end_date=end.strftime("%Y%m%d"), fields="trade_date",
    ))
    days = [str(row.get("trade_date") or "") for row in records(frame) if str(row.get("trade_date") or "").isdigit()]
    return max(days) if days else ""


def _detail(stock: Stock) -> dict:
    from app.services.sector_detail.detail import build_detail

    return build_detail(stock)


def _snapshot(stock: Stock) -> dict:
    today = datetime.now(_ZONE).date().isoformat()
    found = cache.get(f"sector_snap:{stock.ts_code}:{today}")
    return found if isinstance(found, dict) else {}


def _primary(detail: dict) -> dict | None:
    sectors = detail.get("sectors") or {}
    block = sectors.get("primary") if isinstance(sectors, dict) else None
    return block if isinstance(block, dict) else None


def _members(snap: dict, block: dict | None) -> tuple[list[dict], bool]:
    code = (block or {}).get("code")
    for sector in snap.get("sectors") or []:
        if code and sector.get("code") != code:
            continue
        if sector.get("role_in_stock") == "primary" or sector.get("code") == code:
            return list(sector.get("members") or []), bool(sector.get("truncated"))
    return [], False


def _groups(detail: dict, snap: dict) -> list[dict]:
    sectors = detail.get("sectors") if isinstance(detail.get("sectors"), dict) else {}
    groups = []
    for role, kind in (("primary", None), ("secondary", None)):
        block = sectors.get(role)
        if not isinstance(block, dict):
            continue
        codes = []
        truncated = False
        for sector in snap.get("sectors") or []:
            if sector.get("code") == block.get("code"):
                codes = [item.get("ts_code") for item in sector.get("members") or [] if item.get("ts_code")]
                truncated = bool(sector.get("truncated"))
                break
        groups.append({
            "name": block.get("name"),
            "kind": "申万行业" if block.get("type") == "industry" else "同花顺概念",
            "codes": codes,
            "truncated": truncated,
        })
    return groups


def _doc(stock: Stock, part: str, facts: list[dict], text: str, unavailable: list[str] | None = None) -> dict:
    return {
        "ts_code": stock.ts_code,
        "stock_name": stock.name,
        "part": part,
        "facts": facts,
        "text": text,
        "unavailable": unavailable or [],
    }


def _pair(body: dict) -> tuple[list[dict], str]:
    return body["facts"], body["text"]


def _body(facts: list[dict], text: str, unavailable: list[str] | None = None) -> dict:
    return {"facts": facts, "text": text, "unavailable": unavailable or []}


def _last(values) -> float | None:
    if not values:
        return None
    try:
        return float(values[-1])
    except (TypeError, ValueError):
        return None


def _num(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _short_date(value) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[4:6]}-{digits[6:8]}"
    return str(value or "")


def _pts(value) -> str:
    number = _num(value)
    if number is None:
        return ""
    return f"{number:+.1f}%"


def _pct(value) -> str:
    number = _num(value)
    if number is None:
        return ""
    return f"{number * 100:+.1f}%"


def _yi(value) -> str:
    number = _num(value)
    if number is None:
        return ""
    return f"{number / 1e8:+.2f} 亿元"


def _wan(value) -> str:
    number = _num(value)
    if number is None:
        return ""
    return f"{number / 10000:.2f} 亿元"


def _wan_yi(value) -> str:
    number = _num(value)
    if number is None:
        return ""
    return f"{number / 10000:+.2f} 亿元"
