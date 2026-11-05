"""10 个问题的模板答案：模型失败、某一项没通过校验、或没有配置模型时用它。

只用事实表里的数字，每条都带引用。状态：support 偏正面、neutral 中性、warn 偏负面或需警惕、missing 数据缺失。
"""

from __future__ import annotations

QUESTIONS = [
    ("q1", "资金和价格一致吗", "涨时有没有净流入，有没有背离"),
    ("q2", "谁在买谁在卖", "大额资金和小额资金方向是否相反"),
    ("q3", "持续性", "连续流入或流出几天，在加速还是放缓"),
    ("q4", "力度", "相对成交额、流通市值、自身近期是强还是弱"),
    ("q5", "顺势还是逆势", "对照板块、大盘的资金方向"),
    ("q6", "有没有更真的印证", "龙虎榜席位、融资买入、大宗交易能否印证"),
    ("q7", "数据有没有失真", "涨跌停、停牌、数据缺失是否影响（财报、解禁等事件日暂未接入）"),
    ("q8", "位置", "在高位还是低位，有没有放量滞涨"),
    ("q9", "风险提示", "最需要留意的一件事"),
    ("q10", "一句话总结", "用白话讲清楚现在资金在干什么"),
]
TITLES = {key: title for key, title, _ in QUESTIONS}

# 每个问题该看的事实：给模型一张"看哪几条"的索引，减少漏看和乱引用
RELEVANT = {
    "q1": ["fr.vs_self_5d", "fr.vs_self_1d", "fr.vs_self_3d", "fr.vs_self_10d", "fr.vs_self_20d", "fr.ret_5d", "fr.ret_3d", "fr.ret_10d", "fr.ret_20d", "fr.ret_60d", "fr.big_net_5d", "fr.big_net_10d", "fr.big_net_60d", "fr.net_rate_10d", "fr.net_rate_60d", "fr.big_net_20d", "fr.net_rate_5d", "fr.net_rate_3d", "fr.net_rate_20d", "fr.pattern", "fr.high_vol_down", "fr.close_pos"],
    "q2": ["fr.elg_net_5d", "fr.lg_net_5d", "fr.md_net_5d", "fr.sm_net_5d", "fr.concentration", "fr.elg_net_20d", "fr.lg_net_20d"],
    "q3": ["fr.vs_self_3d", "fr.vs_self_5d", "fr.vs_self_10d", "fr.vs_self_20d", "fr.streak", "fr.in_out_days", "fr.trend", "fr.net_rate_5d", "fr.net_rate_3d", "fr.net_rate_20d", "fr.last_day_net", "fr.last_day_z", "fr.last_share_5d"],
    "q4": ["fr.vs_self_5d", "fr.vs_self_20d", "fr.net_rate_5d", "fr.net_rate_3d", "fr.net_rate_20d", "fr.net_to_circ", "fr.circ_mv", "fr.flow_pctile", "fr.last_day_rate", "fr.last_day_z"],
    "q5": [
        "fr.ret_5d", "fr.sector_ret", "fr.index_ret", "fr.sector_flow", "fr.net_rate_vs_sector",
        "fr.ret_3d", "fr.sector_ret_3d", "fr.index_ret_3d", "fr.sector_flow_3d", "fr.net_rate_vs_sector_3d",
        "fr.ret_20d", "fr.sector_ret_20d", "fr.index_ret_20d", "fr.sector_flow_20d", "fr.net_rate_vs_sector_20d",
        "fr.sector_rank", "fr.market_turnover",
    ],
    "q6": ["fr.toplist_days", "fr.toplist_net", "fr.toplist_inst_net", "fr.margin_buy_pct", "fr.margin_change_10d", "fr.margin_pctile", "fr.block_trade"],
    "q7": ["fr.limit_days", "fr.limit_last", "fr.zero_volume_days", "fr.data_lag", "fr.coverage"],
    "q8": ["fr.from_year_high", "fr.hints", "fr.hint_pv", "fr.hint_ind", "fr.hint_stall", "fr.hint_record", "fr.rel_vol_5d", "fr.rel_vol", "fr.up_down_vol", "fr.turnover_pctile", "fr.pattern"],
}


def relevant(available: set[str]) -> dict[str, list[str]]:
    return {key: [fact for fact in ids if fact in available] for key, ids in RELEVANT.items()}


def _missing(key: str, what: str) -> dict:
    return {"key": key, "status": "missing", "answer": f"数据缺失：{what}，这一项无法判断。", "cites": [], "source": "template"}


def _item(key: str, status: str, answer: str, cites: list[str]) -> dict:
    return {"key": key, "status": status, "answer": answer, "cites": cites, "source": "template"}


def answers(d: dict, text: dict[str, str], anomalies: list[dict]) -> list[dict]:
    has = text.__contains__
    out = []

    # q1：主结论近 5 日
    if d.get("ret_5d") is None or d.get("big_net_5d") is None:
        out.append(_missing("q1", "缺少价格或资金流数据"))
    else:
        up, inflow = d["ret_5d"] > 0, d["big_net_5d"] > 0
        cites = ["fr.ret_5d", "fr.big_net_5d"]
        own = (d.get("vs_self_5d") or {}).get("state")
        if own:
            # 和自己平时比：大单净流出是常态，只有比平时明显更流出，涨时才算背离
            cites.append("fr.vs_self_5d")
            said = f"近 5 日股价 {text['fr.ret_5d']}，大资金同期净额 {text['fr.big_net_5d']}，{text['fr.vs_self_5d']}。"
            if up:
                status = "support" if own in ("in", "in_strong") else "warn" if own in ("out", "out_strong") else "neutral"
                said += {"support": "涨的同时大资金在加码，价格和资金方向一致。", "warn": "涨的同时大资金反而比平时撤得多，出现背离。",
                         "neutral": "涨的同时大资金和平时差不多，没有明显的配合，也没有撤退。"}[status]
            else:
                status = "warn" if own in ("out", "out_strong") else "neutral"
                said += {"warn": "跌的同时大资金比平时撤得多，价格和资金同步走弱。",
                         "neutral": "跌的同时大资金没有比平时撤得更多，看不出集中出逃；若比平时更流入，则是有资金承接，但未必企稳。"}[status]
        elif up and inflow:
            said = f"近 5 日股价 {text['fr.ret_5d']}，大资金同期净额 {text['fr.big_net_5d']}，价格和资金方向一致。"
            status = "support"
        elif up:
            said = f"近 5 日股价 {text['fr.ret_5d']}，但大资金同期净额 {text['fr.big_net_5d']}，上涨没有大资金配合；历史不够长，还没法和自己平时比。"
            status = "neutral"
        elif inflow:
            said = f"近 5 日股价 {text['fr.ret_5d']}，大资金同期净额 {text['fr.big_net_5d']}，下跌中有资金承接，需要观察是否企稳。"
            status = "neutral"
        else:
            said = f"近 5 日股价 {text['fr.ret_5d']}，大资金同期净额 {text['fr.big_net_5d']}，价格和资金同步走弱；历史不够长，还没法和自己平时比。"
            status = "neutral"
        if d.get("high_vol_down"):
            said += f"其中放量下跌 {text['fr.high_vol_down']}。"
            cites.append("fr.high_vol_down")
        if has("fr.close_pos"):
            said += f"近 5 日收盘强度 {text['fr.close_pos']}。"
            cites.append("fr.close_pos")
        out.append(_item("q1", status, said, cites))

    # q2：近 5 日分档；东财四档净额合计约等于 0，要看超大单和大单是否同向
    sizes = [d.get(f"{key}_net_5d") for key in ("elg", "lg", "md", "sm")]
    if any(value is None for value in sizes):
        out.append(_missing("q2", "缺少分档资金流"))
    else:
        elg, lg = sizes[0], sizes[1]
        cites = ["fr.elg_net_5d", "fr.lg_net_5d", "fr.md_net_5d", "fr.sm_net_5d"]
        tail = (f"近 5 日超大单 {text['fr.elg_net_5d']}，大单 {text['fr.lg_net_5d']}，中单 {text['fr.md_net_5d']}，小单 {text['fr.sm_net_5d']}。"
                "各档净额合计接近 0，大额和小额方向相反是统计口径的必然，真正要看的是超大单和大单是否同向。")
        if elg > 0 and lg > 0:
            out.append(_item("q2", "support", "超大单和大单同时净流入，大额资金内部方向一致。" + tail, cites))
        elif elg < 0 and lg < 0:
            out.append(_item("q2", "warn", "超大单和大单同时净流出，大额资金内部方向一致地在撤。" + tail, cites))
        else:
            out.append(_item("q2", "neutral", "超大单和大单方向不一致，大额资金内部有分歧。" + tail, cites))

    # q3
    if not has("fr.trend") and not has("fr.streak"):
        out.append(_missing("q3", "缺少逐日资金流"))
    else:
        streak, cites = d.get("streak") or 0, [c for c in ("fr.vs_self_3d", "fr.vs_self_5d", "fr.vs_self_20d", "fr.streak", "fr.trend", "fr.in_out_days", "fr.last_share_5d") if has(c)]
        parts = [text[c] for c in cites]
        own = (d.get("vs_self_5d") or {}).get("state")
        if own:                      # 连续净流出天数在 A 股很常见，只有同时比自己平时更流出才算要警惕
            status = "support" if streak >= 3 and own in ("normal", "in", "in_strong") else "warn" if streak <= -3 and own in ("out", "out_strong") else "neutral"
        else:
            status = "support" if streak >= 3 else "neutral"
        out.append(_item("q3", status, "；".join(parts) + "。", cites))

    # q4：力度看近 5 日
    if d.get("net_rate_5d") is None:
        out.append(_missing("q4", "缺少资金流和成交额"))
    else:
        rate = d["net_rate_5d"]
        own = d.get("vs_self_5d")
        cites = [c for c in ("fr.vs_self_5d", "fr.net_rate_5d", "fr.flow_pctile", "fr.net_to_circ", "fr.net_rate_20d") if has(c)]
        side = "流入" if rate > 0 else "流出"
        extra = "，".join(f"{text[c]}" if c != "fr.flow_pctile" else f"单日强度处于近一年 {text[c]} 分位" for c in cites if c not in ("fr.vs_self_5d", "fr.net_rate_5d"))
        if own:                      # 力度看和自己平时的差距，不看绝对的百分比
            z = own["z"]
            strength = "较强" if abs(z) >= 1.5 else "一般" if abs(z) >= 0.8 else "很弱"
            status = ("support" if z > 0 else "warn") if strength != "很弱" else "neutral"
            lead = f"近 5 日大资金净{side}占成交额 {text['fr.net_rate_5d']}，{text['fr.vs_self_5d']}，力度{strength}"
        else:
            strength = "较强" if abs(rate) >= 0.05 else "一般" if abs(rate) >= 0.02 else "很弱"
            status = ("support" if rate > 0 else "neutral") if strength != "很弱" else "neutral"
            lead = f"近 5 日大资金净{side}占成交额 {text['fr.net_rate_5d']}，力度{strength}（历史不够长，按绝对比例看）"
        out.append(_item("q4", status, lead + (f"；{extra}" if extra else "") + "。", cites))

    # q5：主结论近 5 日；3 / 20 日对照说明是个股强还是大家一起涨
    cites = [c for c in ("fr.sector_ret", "fr.index_ret", "fr.sector_flow", "fr.sector_rank", "fr.market_turnover") if has(c)]
    if not cites or d.get("ret_5d") is None:
        out.append(_missing("q5", "缺少板块或大盘数据"))
    else:
        bits = []
        if d.get("sector_ret") is not None:
            bits.append(f"近 5 日板块 {text['fr.sector_ret']}")
        if d.get("index_ret") is not None:
            bits.append(f"沪深300 {text['fr.index_ret']}")
        if has("fr.sector_flow"):
            bits.append(f"板块资金{text['fr.sector_flow']}")
        if has("fr.sector_rank"):
            bits.append(f"行业资金排名{text['fr.sector_rank']}")
        if has("fr.market_turnover"):
            bits.append(f"两市成交额较均值 {text['fr.market_turnover']}")
        ref = d.get("sector_ret") if d.get("sector_ret") is not None else d.get("index_ret")
        if ref is None:
            status, lead = "neutral", ""
        elif d["ret_5d"] > ref + 0.03:
            status, lead = "support", f"本股近 5 日涨幅 {text['fr.ret_5d']}，跑赢参照，"
        elif d["ret_5d"] < ref - 0.03:
            status, lead = "warn", f"本股近 5 日涨幅 {text['fr.ret_5d']}，跑输参照，"
        else:
            status, lead = "neutral", f"本股近 5 日涨幅 {text['fr.ret_5d']}，和参照基本同步，"
        contrast = []
        for days in (3, 20):
            ret_key, sec_key = f"ret_{days}d", f"sector_ret_{days}d"
            fact_ret, fact_sec = f"fr.ret_{days}d", f"fr.sector_ret_{days}d"
            if d.get(ret_key) is None or d.get(sec_key) is None or not has(fact_ret) or not has(fact_sec):
                continue
            gap = d[ret_key] - d[sec_key]
            label = "跑赢板块" if gap > 0.03 else "跑输板块" if gap < -0.03 else "与板块同步"
            contrast.append(f"近 {days} 日{label}（本股 {text[fact_ret]} / 板块 {text[fact_sec]}）")
            cites.extend([fact_ret, fact_sec])
        if contrast:
            bits.append("对照：" + "；".join(contrast))
        seen, ordered = set(), []
        for fact_id in ["fr.ret_5d"] + cites:
            if fact_id not in seen:
                seen.add(fact_id)
                ordered.append(fact_id)
        out.append(_item("q5", status, lead + "，".join(bits) + "。", ordered))

    # q6
    cites = [c for c in ("fr.toplist_days", "fr.toplist_inst_net", "fr.margin_buy_pct", "fr.margin_change_10d", "fr.margin_pctile", "fr.block_trade") if has(c)]
    if not cites:
        out.append(_missing("q6", "龙虎榜、融资融券、大宗交易都没有取到"))
    else:
        bits = []
        if has("fr.toplist_days"):
            bits.append("近 20 日上龙虎榜 " + text["fr.toplist_days"])
        if has("fr.toplist_inst_net"):
            bits.append("机构席位合计 " + text["fr.toplist_inst_net"])
        if has("fr.margin_buy_pct"):
            bits.append("融资买入占成交额 " + text["fr.margin_buy_pct"])
        if has("fr.margin_change_10d"):
            bits.append("融资余额 10 日变化 " + text["fr.margin_change_10d"])
        if has("fr.margin_pctile"):
            bits.append("融资余额增幅 " + text["fr.margin_pctile"])
        if has("fr.block_trade"):
            bits.append("大宗交易 " + text["fr.block_trade"])
        inst = d.get("toplist_inst_net")
        status = "support" if (inst or 0) > 0.5 else "warn" if (inst or 0) < -0.5 else "neutral"
        out.append(_item("q6", status, "；".join(bits) + "。", cites))

    # q7
    if not has("fr.limit_days"):
        out.append(_missing("q7", "缺少日线"))
    elif d.get("limit_days") or d.get("zero_volume_days"):
        out.append(_item("q7", "warn", f"近 20 日有 {text['fr.limit_days']}疑似涨跌停、{text['fr.zero_volume_days']}疑似停牌，那几天的成交和资金流会失真，解读要打折扣。", ["fr.limit_days", "fr.zero_volume_days"]))
    else:
        out.append(_item("q7", "support", "近 20 日没有发现涨跌停或停牌，数据可以直接参考；财报、解禁等事件日没有接入，未做核对。", ["fr.limit_days", "fr.zero_volume_days"]))

    # q8
    if not has("fr.from_year_high"):
        out.append(_missing("q8", "缺少一年的日线"))
    else:
        hints, gap = d.get("hints") or [], d["from_year_high"]
        where = "接近一年高点" if gap >= -0.1 else "处在一年高点下方较远的位置" if gap <= -0.3 else "处在一年高点下方的中间位置"
        detail = [fact for fact in ("fr.hint_pv", "fr.hint_ind", "fr.hint_stall", "fr.hint_record") if d.get(fact)]
        if hints:
            out.append(_item("q8", "warn", f"收盘价距一年高点 {text['fr.from_year_high']}，{where}，同时出现：{'、'.join(hints)}。", ["fr.from_year_high", "fr.hints"] + detail))
        else:
            out.append(_item("q8", "support" if gap < -0.1 else "neutral", f"收盘价距一年高点 {text['fr.from_year_high']}，{where}，没有出现滞涨或顶背离。", ["fr.from_year_high", "fr.hints"]))

    # q9
    if anomalies:
        top = anomalies[0]
        out.append(_item("q9", "warn", f"{top['title']}。{top['detail']}", top["cites"]))
    else:
        out.append(_item("q9", "support", "按目前的数据，没有发现需要特别留意的异常。", ["fr.coverage"] if has("fr.coverage") else []))

    # q10
    by_key = {item["key"]: item for item in out}
    lead = []
    if d.get("ret_5d") is not None:
        lead.append(f"近 5 日股价 {text['fr.ret_5d']}")
    if d.get("big_net_5d") is not None:
        lead.append(f"大资金净{'流入' if d['big_net_5d'] > 0 else '流出'} {text['fr.big_net_5d'].lstrip('+-')}")
    statuses = [by_key[k]["status"] for k in ("q1", "q2", "q3", "q4") if k in by_key and by_key[k]["status"] != "missing"]
    pos, neg = statuses.count("support"), statuses.count("warn")
    status = "support" if pos > neg + 1 else "warn" if neg > pos + 1 else "neutral"
    risk = f"需要留意：{anomalies[0]['title']}。" if anomalies else "暂未发现明显异常。"
    cites = [c for c in ("fr.ret_5d", "fr.big_net_5d") if has(c)]
    if lead:
        out.append(_item("q10", status, "，".join(lead) + "；" + risk, cites))
    else:
        out.append(_missing("q10", "缺少价格和资金流数据"))
    return out


_FOLLOW_STATUS = {
    "strong": "support", "yes": "support", "weak": "neutral",
    "mixed": "neutral", "none": "warn", "flat": "neutral", "unknown": "neutral",
}


def rule_signals(payload: dict, items: list[dict]) -> list[dict]:
    """页面四句判断：全部由规则/模板答案生成，不调模型。"""
    by = {row["key"]: row for row in items}
    out: list[dict] = []
    follow = payload.get("follow") or {}
    if follow.get("label"):
        out.append({
            "key": "follow",
            "status": _FOLLOW_STATUS.get(follow.get("level"), "neutral"),
            "text": str(follow["label"])[:40],
            "cites": [],
        })
    for key, question in (("price", "q1"), ("internal", "q2"), ("sector", "q5")):
        row = by.get(question)
        if not row or row.get("status") == "missing" or not row.get("answer"):
            continue
        out.append({
            "key": key,
            "status": row["status"],
            "text": str(row["answer"])[:40],
            "cites": list(row.get("cites") or []),
        })
    return out
