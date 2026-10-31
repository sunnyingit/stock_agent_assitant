"""资金流事实表：代码算好所有数字，模型只引用编号。金额统一成"亿元"，涨跌和占比统一成百分数。

返回 (facts, derived)：facts 给模型看，derived 是原始数值，给异常检测和模板答案用。
"""

from __future__ import annotations

from statistics import mean, pstdev

from app.reviews.core import FactSheet
from app.synthesis.calendar import last_close_date, parse_day, workday_gap
from app.synthesis.types import Fact

_TREND = {
    "accelerating_in": "大资金流入在加快", "slowing_in": "大资金仍在流入但放缓", "steady_in": "大资金持续流入",
    "turning_out": "大资金由流入转为流出", "accelerating_out": "大资金流出在加快", "mixed": "大资金方向不一致",
}
_PATTERN = {
    "up_vol_up": "价涨量增", "up_vol_down": "价涨量缩", "down_vol_up": "价跌量增",
    "down_vol_down": "价跌量缩", "none": "无明显形态",
}


# ---- 对外展示：每条事实属于哪一组、什么类型。前端可以按 group 做卡片（如"龙虎榜""融资融券"）
GROUP_ORDER = ("价格与量能", "资金分档", "持续性与强度", "板块与大盘", "龙虎榜", "融资融券", "大宗交易", "数据失真", "位置提示", "数据质量")
META = {
    "fr.ret_20d": ("价格与量能", "pct"), "fr.ret_5d": ("价格与量能", "pct"), "fr.rel_vol": ("价格与量能", "ratio"),
    "fr.up_down_vol": ("价格与量能", "ratio"), "fr.turnover_pctile": ("价格与量能", "share"),
    "fr.pattern": ("价格与量能", "text"), "fr.from_year_high": ("价格与量能", "pct"),
    "fr.big_net_20d": ("资金分档", "yi"), "fr.retail_net_20d": ("资金分档", "yi"), "fr.elg_net_20d": ("资金分档", "yi"),
    "fr.lg_net_20d": ("资金分档", "yi"), "fr.md_net_20d": ("资金分档", "yi"), "fr.sm_net_20d": ("资金分档", "yi"),
    "fr.net_rate_20d": ("资金分档", "pct"), "fr.net_rate_5d": ("资金分档", "pct"), "fr.concentration": ("资金分档", "share"),
    "fr.circ_mv": ("资金分档", "yi_abs"), "fr.net_to_circ": ("资金分档", "pct"),
    "fr.last_day_net": ("持续性与强度", "yi"), "fr.last_day_rate": ("持续性与强度", "pct"),
    "fr.last_day_z": ("持续性与强度", "sigma"), "fr.streak": ("持续性与强度", "days"),
    "fr.in_out_days": ("持续性与强度", "days"), "fr.trend": ("持续性与强度", "text"), "fr.flow_pctile": ("持续性与强度", "share"),
    "fr.sector_ret": ("板块与大盘", "pct"), "fr.index_ret": ("板块与大盘", "pct"), "fr.sector_flow": ("板块与大盘", "text"),
    "fr.net_rate_vs_sector": ("板块与大盘", "pct"), "fr.sector_rank": ("板块与大盘", "rank"), "fr.market_turnover": ("板块与大盘", "pct"),
    "fr.toplist_days": ("龙虎榜", "days"), "fr.toplist_net": ("龙虎榜", "yi"), "fr.toplist_inst_net": ("龙虎榜", "yi"),
    "fr.margin_buy_pct": ("融资融券", "share"), "fr.margin_change_10d": ("融资融券", "pct"),
    "fr.block_trade": ("大宗交易", "count"),
    "fr.limit_days": ("数据失真", "days"), "fr.limit_last": ("数据失真", "days"), "fr.zero_volume_days": ("数据失真", "days"),
    "fr.hints": ("位置提示", "text"),
    "fr.data_lag": ("数据质量", "days"), "fr.coverage": ("数据质量", "count"),
}
# 值的约定：pct、share 是小数（0.082 = 8.2%）；yi 是带符号的亿元，yi_abs 是不带符号的亿元；days 是天数（streak 为负表示净流出）
UNIT_OVERRIDE = {"fr.block_trade": "笔", "fr.coverage": "天", "fr.in_out_days": "天"}
UNITS = {"pct": "%", "share": "%", "yi": "亿元", "yi_abs": "亿元", "ratio": "倍", "days": "天", "sigma": "个标准差", "rank": "", "count": "", "text": ""}


def public(facts: FactSheet) -> list[dict]:
    """全部事实：编号、名称、分组、类型、单位、原始数值和展示文字。按分组排好，组名从各行的 group 取。"""
    rows = []
    for fact in facts:
        group, kind = META.get(fact.id, ("其他", "text"))
        rows.append({
            "id": fact.id, "label": facts.labels.get(fact.id, fact.id), "group": group, "kind": kind,
            "unit": UNIT_OVERRIDE.get(fact.id, UNITS.get(kind, "")),
            "value": round(fact.value, 6) if isinstance(fact.value, float) else fact.value, "text": fact.text,
        })
    order = {name: index for index, name in enumerate(GROUP_ORDER)}
    rows.sort(key=lambda row: order.get(row["group"], len(order)))   # 稳定排序：组内保持计算顺序
    return rows


def _limit_of(ts_code: str) -> float:
    """涨跌幅限制（百分数）。按板块区分；ST 要另外的数据，这里不区分。"""
    code = ts_code.split(".")[0]
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    if code.startswith(("43", "83", "87", "92")):
        return 30.0
    return 10.0


def _pct(value: float) -> str:
    return f"{value * 100:+.1f}%"


def _yi(value: float) -> str:
    return f"{value:+.2f} 亿元"


def _share(value: float) -> str:
    return f"{value * 100:.0f}%"


def _put(facts: FactSheet, fact_id: str, label: str, value, text: str) -> None:
    if value is None:
        return
    facts.labels[fact_id] = label
    facts.append(Fact(fact_id, value, text))


def _sum(rows: list[dict | None], size: str) -> float | None:
    values = [row.get(size) for row in rows if row and row.get(size) is not None]
    return sum(values) if values else None


def build(payload: dict, aligned: dict, extra: dict, stock, now) -> tuple[FactSheet, dict]:
    bars = aligned.get("bars") or []
    flow_by = {row["date"]: row for row in aligned.get("flow") or [] if row.get("date")}
    window = bars[-20:]
    d: dict = {"limit_days": 0}
    facts = FactSheet()
    if not window:
        return facts, d

    # ---- 价格与量能
    ret_20 = _compound([row.get("chg") for row in window])
    ret_5 = _compound([row.get("chg") for row in bars[-5:]])
    d["ret_20d"], d["ret_5d"] = ret_20, ret_5
    _put(facts, "fr.ret_20d", "近 20 日涨跌幅", ret_20, _pct(ret_20) if ret_20 is not None else "")
    _put(facts, "fr.ret_5d", "近 5 日涨跌幅", ret_5, _pct(ret_5) if ret_5 is not None else "")
    price = payload.get("price_volume") or {}
    row20 = next((row for row in (payload.get("windows") or {}).get("rows") or [] if row.get("days") == 20), {})
    rel_vol = row20.get("rel_vol")
    d["rel_vol"] = rel_vol
    if rel_vol is not None:
        _put(facts, "fr.rel_vol", "近 20 日相对量能（相对此前 60 日均量）", rel_vol, f"{rel_vol:.2f} 倍")
    if price.get("up_down_vol_ratio") is not None:
        _put(facts, "fr.up_down_vol", "上涨日与下跌日的成交量比", price["up_down_vol_ratio"], f"{price['up_down_vol_ratio']:.2f} 倍")
    if price.get("turnover_pctile") is not None:
        _put(facts, "fr.turnover_pctile", "换手率在近 250 日的分位", price["turnover_pctile"], _share(price["turnover_pctile"]))
    pattern = price.get("pattern")
    d["pattern"] = pattern
    if pattern:
        _put(facts, "fr.pattern", "量价形态", pattern, _PATTERN.get(pattern, pattern))
    closes = [row["close"] for row in bars[-250:] if row.get("close")]
    if len(closes) >= 60:
        gap = closes[-1] / max(closes) - 1
        d["from_year_high"] = gap
        _put(facts, "fr.from_year_high", "收盘价距一年高点（负数表示在高点下方）", gap, _pct(gap))

    # ---- 资金：20 日窗口
    rows20 = [flow_by.get(row["date"]) for row in window]
    covered = sum(1 for row in rows20 if row and (row.get("super_large") is not None or row.get("large") is not None))
    d["flow_coverage"] = covered / len(window)
    elg, large = _sum(rows20, "super_large"), _sum(rows20, "large")
    medium, small = _sum(rows20, "medium"), _sum(rows20, "small")
    big = None if elg is None and large is None else (elg or 0) + (large or 0)
    retail = None if medium is None and small is None else (medium or 0) + (small or 0)
    d["big_net_20d"], d["retail_net_20d"] = (None if big is None else big / 1e8), (None if retail is None else retail / 1e8)
    if big is not None:
        _put(facts, "fr.big_net_20d", "近 20 日大资金（超大单加大单）净额", big / 1e8, _yi(big / 1e8))
    if retail is not None:
        _put(facts, "fr.retail_net_20d", "近 20 日小额资金（中单加小单）净额", retail / 1e8, _yi(retail / 1e8))
    for key, name, value in (("elg", "超大单", elg), ("lg", "大单", large), ("md", "中单", medium), ("sm", "小单", small)):
        d[f"{key}_net_20d"] = None if value is None else value / 1e8
        if value is not None:
            _put(facts, f"fr.{key}_net_20d", f"近 20 日{name}净额", value / 1e8, _yi(value / 1e8))
    amount_20 = sum(row.get("amount") or 0 for row, flow in zip(window, rows20) if flow)
    net_rate = None if big is None or not amount_20 else big / amount_20
    d["net_rate_20d"] = net_rate
    if net_rate is not None:
        _put(facts, "fr.net_rate_20d", "近 20 日大资金净额占成交额", net_rate, _pct(net_rate))
    rows5 = [flow_by.get(row["date"]) for row in bars[-5:]]
    big5 = _sum([{"v": (row.get("super_large") or 0) + (row.get("large") or 0)} for row in rows5 if row], "v")
    amount_5 = sum(row.get("amount") or 0 for row, flow in zip(bars[-5:], rows5) if flow)
    if big5 is not None and amount_5:
        _put(facts, "fr.net_rate_5d", "近 5 日大资金净额占成交额", big5 / amount_5, _pct(big5 / amount_5))
    if elg is not None and big and big > 0 and elg > 0:
        _put(facts, "fr.concentration", "超大单占大资金净流入的比例", elg / big, _share(elg / big))
    circ = (extra.get("circ") or {}).get("circ_mv_yi")
    if circ:
        _put(facts, "fr.circ_mv", "流通市值", circ, f"{circ:.0f} 亿元")
        if big is not None:
            _put(facts, "fr.net_to_circ", "近 20 日大资金净额占流通市值", big / 1e8 / circ, _pct(big / 1e8 / circ))

    # ---- 资金：逐日、持续性、单日异常
    nets = []
    for bar in bars[-60:]:
        row = flow_by.get(bar["date"])
        if row and bar.get("amount"):
            nets.append(((row.get("super_large") or 0) + (row.get("large") or 0), bar["amount"]))
    if nets:
        rates = [net / amount for net, amount in nets]
        last_net, last_amount = nets[-1]
        d["last_day_net"], d["last_day_rate"] = last_net / 1e8, last_net / last_amount
        _put(facts, "fr.last_day_net", "最近一个交易日大资金净额", last_net / 1e8, _yi(last_net / 1e8))
        _put(facts, "fr.last_day_rate", "最近一个交易日大资金净额占成交额", d["last_day_rate"], _pct(d["last_day_rate"]))
        history = rates[:-1]
        if len(history) >= 20 and pstdev(history) > 0:
            z = (rates[-1] - mean(history)) / pstdev(history)
            d["last_day_z"] = z
            _put(facts, "fr.last_day_z", "最近一天大资金净流入率偏离近 60 日均值的倍数（标准差）", z, f"{z:+.1f} 个标准差")
        streak = 0
        for net, _amount in reversed(nets):
            sign = 1 if net > 0 else -1 if net < 0 else 0
            if sign == 0 or (streak and (sign > 0) != (streak > 0)):
                break
            streak += sign
        d["streak"] = streak
        if streak:
            more = "以上" if abs(streak) == len(nets) else ""   # 回看只有 60 天：整个回看期都同向，实际可能更久
            _put(facts, "fr.streak", "大资金连续净流入（正）或净流出（负）的天数", streak, f"连续净{'流入' if streak > 0 else '流出'} {abs(streak)} 天{more}")
        last10 = [net for net, _a in nets[-10:]]
        d["in_days_10"], d["out_days_10"] = sum(1 for v in last10 if v > 0), sum(1 for v in last10 if v < 0)
        _put(facts, "fr.in_out_days", "最近 10 天大资金净流入天数", d["in_days_10"], f"{d['in_days_10']} 天流入，{d['out_days_10']} 天流出")
    trend = (payload.get("flow_daily") or {}).get("trend") if nets else None   # 没有逐日资金流时，量价模块的"趋势"只是默认值
    d["trend"] = trend
    if trend:
        _put(facts, "fr.trend", "大资金近期趋势", trend, _TREND.get(trend, trend))
    year_rates = []
    for bar in bars[-250:]:
        row = flow_by.get(bar["date"])
        if row and bar.get("amount"):
            year_rates.append(((row.get("super_large") or 0) + (row.get("large") or 0)) / bar["amount"])
    if len(year_rates) >= 60 and "last_day_rate" in d:
        d["flow_pctile"] = sum(1 for v in year_rates if v < d["last_day_rate"]) / len(year_rates)
        _put(facts, "fr.flow_pctile", "最近一天大资金净流入率在近一年里的分位", d["flow_pctile"], _share(d["flow_pctile"]))

    # ---- 相对强弱：板块、大盘、板块资金排名、两市成交额
    rel = payload.get("relative_strength") or {}
    by_kind = {row.get("kind"): row for row in rel.get("bars") or []}
    sector_ret, index_ret = (by_kind.get("sector") or {}).get("ret"), (by_kind.get("index") or {}).get("ret")
    d["sector_ret"], d["index_ret"] = sector_ret, index_ret
    if sector_ret is not None:
        _put(facts, "fr.sector_ret", "同期所属板块涨跌幅", sector_ret, _pct(sector_ret))
    if index_ret is not None:
        _put(facts, "fr.index_ret", "同期沪深300 涨跌幅", index_ret, _pct(index_ret))
    d["sector_flow"] = rel.get("sector_flow")
    if rel.get("sector_flow"):
        _put(facts, "fr.sector_flow", "同期所属板块资金方向", rel["sector_flow"], "净流入" if rel["sector_flow"] == "in" else "净流出")
    if rel.get("net_rate_vs_sector") is not None:
        _put(facts, "fr.net_rate_vs_sector", "本股大资金净流入率减板块", rel["net_rate_vs_sector"], _pct(rel["net_rate_vs_sector"]))
    rank = extra.get("sector_rank")
    if rank:
        d["sector_rank"] = rank
        _put(facts, "fr.sector_rank", "所属行业板块当日资金净流入排名", rank["rank"], f"第 {rank['rank']} 位（共 {rank['total']} 个行业板块）")
    market = extra.get("market")
    if market:
        d["market_turnover_change"] = market["turnover_change"]
        _put(facts, "fr.market_turnover", "两市最新成交额较此前 20 日均值", market["turnover_change"], _pct(market["turnover_change"]))

    # ---- 印证：龙虎榜、融资融券、大宗交易
    toplist = extra.get("toplist")
    if toplist is not None:
        items = toplist.get("items") or []
        days = len({row["trade_date"] for row in items})
        d["toplist_days"] = days
        _put(facts, "fr.toplist_days", "近 20 个交易日上龙虎榜的天数", days, f"{days} 天")
        if items:
            net = sum(float(row.get("net_amount") or 0) for row in items) / 1e8
            inst = 0.0
            for row in items:
                for seat in row.get("seats") or []:
                    if "机构" in str(seat.get("exalter") or ""):
                        inst += float(seat.get("net_buy") or 0)
            d["toplist_net"], d["toplist_inst_net"] = net, inst / 1e8
            _put(facts, "fr.toplist_net", "上榜日龙虎榜净买额合计", net, _yi(net))
            _put(facts, "fr.toplist_inst_net", "上榜日机构席位净买额合计", inst / 1e8, _yi(inst / 1e8))
    margin = extra.get("margin")
    if margin and margin.get("items"):
        items = margin["items"]
        pcts = [row["rzmre_pct"] for row in items[-5:] if row.get("rzmre_pct") is not None]
        if pcts:
            d["margin_buy_pct"] = mean(pcts) / 100
            _put(facts, "fr.margin_buy_pct", "近 5 日融资买入额占成交额", d["margin_buy_pct"], _share(d["margin_buy_pct"]))
        balances = [row["rzye"] for row in items if row.get("rzye")]
        if len(balances) >= 11:
            change = balances[-1] / balances[-11] - 1
            d["margin_change_10d"] = change
            _put(facts, "fr.margin_change_10d", "融资余额 10 日变化", change, _pct(change))
    block = extra.get("block")
    if block is not None:
        d["block_count"] = block["count"]
        _put(facts, "fr.block_trade", "近 30 日大宗交易", block["count"], f"{block['count']} 笔，合计 {block['amount_yi']:.2f} 亿元" if block["count"] else "无")

    # ---- 失真：涨跌停、停牌
    limit = _limit_of(stock.ts_code)
    limit_rows = [row for row in window if row.get("chg") is not None and abs(row["chg"]) >= limit - 0.3]
    zero = [row for row in window if not row.get("volume")]
    d["limit_days"], d["zero_volume_days"] = len(limit_rows), len(zero)
    _put(facts, "fr.limit_days", "近 20 日疑似涨跌停的天数（按涨跌幅判断）", len(limit_rows), f"{len(limit_rows)} 天")
    _put(facts, "fr.zero_volume_days", "近 20 日没有成交的天数（疑似停牌）", len(zero), f"{len(zero)} 天")
    if limit_rows:
        gap = len(window) - 1 - window.index(limit_rows[-1])   # 按日线根数算，不受节假日影响
        _put(facts, "fr.limit_last", "最近一次疑似涨跌停距今", gap, "就在最近一个交易日" if gap == 0 else f"{gap} 个交易日前")

    # ---- 位置提示、数据质量
    hits = [item.get("text") for item in (payload.get("position_hints") or {}).get("items") or [] if item.get("hit")]
    d["hints"] = hits
    _put(facts, "fr.hints", "命中的位置提示（滞涨、顶背离、天量等）", "、".join(hits) if hits else "无", "、".join(hits) if hits else "无")
    as_of = parse_day((aligned.get("as_of") or (bars[-1].get("date"))))
    lag = workday_gap(as_of, last_close_date(now)) if as_of else None
    d["data_lag"] = lag
    if lag is not None:
        _put(facts, "fr.data_lag", "资金流数据落后最近收盘日的工作日数", lag, "最新" if lag == 0 else f"{lag} 个工作日")
    _put(facts, "fr.coverage", "近 20 日里有资金流数据的天数", covered, f"{covered} / {len(window)} 天")
    return facts, d


def _compound(changes: list) -> float | None:
    values = [value for value in changes if value is not None]
    if not values:
        return None
    acc = 1.0
    for value in values:
        acc *= 1 + float(value) / 100
    return round(acc - 1, 4)
