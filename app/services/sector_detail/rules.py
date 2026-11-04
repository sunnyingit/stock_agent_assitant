"""板块阶段、角色和力度。

单个板块的阶段、角色规则与 signal_bt/rules/sector.py 对齐，版本 sector-3。
回测目前仍是「一只股票只归一个行业」：没有概念板块，也没有连续 3 天才切换阶段。线上概念板块的结论因此没有回测依据。
概念成分若没有历史进出日期，线上只能用当前名单，不能假装已经回测过。
本股属于哪两个板块由 affiliation.py 判断；这里的 combine 把两个板块的结论按驱动占比合成一个方向，两个板块阶段对立时不平均，标成分歧。

接口枚举：
stage.current：start 启动，main 主升，climax 高潮，diverge 分歧，ebb 退潮，weak 弱势，none 没有明确阶段。
role.value：leader 领涨，resilient 抗跌，core 中军，follower 跟风，independent 独立。
sustain.items[].status：ok 中性，warn 反向证据，support 支持当前方向，na 没有这项数据。
弱势阶段里，持续天数没超过同类中位数是支持（status 为 support），不写入「不过」和 certainty_reasons。
超过中位数才是 warn，可以放进「不过」。certainty_reasons 在弱势阶段只写「弱势阶段，把握上限为低」。
"""

from __future__ import annotations

RULE_VERSION = "sector-3"
DISCLAIMER = "只描述板块状态和个股位置，不推荐个股。研究分析，不构成投资建议。"

INDEPENDENT_CORR = 0.6   # 本股与板块 20 日相关低于这个值时，角色记为"独立"
CONFIRM_DAYS = 3
LEADER_TOP_N = 3
CORE_TOP_PCT = 0.3
BROKEN_MIN = 3

_BULLISH = {"start", "main", "climax"}
_BEARISH = {"diverge", "ebb", "weak"}
_CERTAINTY = ("high", "medium", "low")

_TAG = {
    "start": "板块启动",
    "main": "板块主升",
    "climax": "板块高潮",
    "diverge": "板块分歧",
    "ebb": "板块退潮",
    "weak": "板块弱势",
    "none": "板块没有明确阶段",
}
_ROLE_TAG = {
    "leader": "本股领涨",
    "resilient": "本股抗跌",
    "core": "本股是中军",
    "follower": "本股跟风",
    "independent": "本股走势独立",
}
_ROLE_TEXT = {
    "leader": "领涨",
    "resilient": "抗跌",
    "core": "中军",
    "follower": "跟风",
    "independent": "独立",
}


def compound(returns: list[float | None]) -> float | None:
    if not returns:
        return None
    acc = 1.0
    for value in returns:
        if value is None:
            return None
        acc *= 1.0 + float(value)
    return acc - 1.0


def pearson(left: list[float | None], right: list[float | None]) -> float | None:
    pairs = [
        (float(a), float(b))
        for a, b in zip(left, right)
        if a is not None and b is not None
    ][-20:]
    count = len(pairs)
    if count < 10:
        return None
    mean_left = sum(item[0] for item in pairs) / count
    mean_right = sum(item[1] for item in pairs) / count
    cov = sum((a - mean_left) * (b - mean_right) for a, b in pairs)
    left_scale = sum((a - mean_left) ** 2 for a, _ in pairs) ** 0.5
    right_scale = sum((b - mean_right) ** 2 for _, b in pairs) ** 0.5
    if left_scale == 0 or right_scale == 0:
        return None
    return cov / (left_scale * right_scale)


def cumulative(returns: list[float | None]) -> list[float]:
    acc = 0.0
    points = []
    for value in returns:
        step = 0.0 if value is None else float(value)
        acc = (1.0 + acc) * (1.0 + step) - 1.0
        points.append(round(acc, 4))
    return points


def strength_band(excess: float | None) -> int:
    if excess is None:
        return 1
    gap = abs(excess)
    if gap <= 0.03:
        return 1
    if gap <= 0.08:
        return 2
    return 3


def raw_stage(day: dict, prior: dict | None, limit_pctile: float | None) -> str:
    """单日阶段。优先级：退潮 > 分歧 > 高潮 > 主升 > 启动 > 弱势。"""
    rank = day.get("rank_pct")
    breadth = day.get("breadth")
    if rank is None or breadth is None:
        return "none"
    rank_prior = None if prior is None else prior.get("rank_pct")
    breadth_prior = None if prior is None else prior.get("breadth")
    limit_up = day.get("limit_up") or 0
    sector_5d = day.get("sector_5d")
    index_5d = day.get("index_5d")
    ebb = (
        rank_prior is not None
        and rank - rank_prior > 0.30
        and sector_5d is not None
        and index_5d is not None
        and sector_5d < index_5d
        and limit_up == 0
    )
    diverge = (
        rank_prior is not None
        and rank_prior <= 0.30
        and breadth_prior is not None
        and breadth_prior - breadth > 0.15
    )
    leading = rank <= 0.30 and breadth > 0.50
    climax = leading and breadth > 0.80 and limit_pctile is not None and limit_pctile >= 0.90
    start = rank_prior is not None and rank_prior - rank > 0.30 and breadth <= 0.50
    sector_20d = day.get("sector_20d")
    index_20d = day.get("index_20d")
    weak = (
        rank >= 0.70
        and sector_20d is not None
        and index_20d is not None
        and sector_20d < index_20d
    )
    if ebb:
        return "ebb"
    if diverge:
        return "diverge"
    if climax:
        return "climax"
    if leading:
        return "main"
    if start:
        return "start"
    if weak:
        return "weak"
    return "none"


def confirm(raws: list[str], need: int = CONFIRM_DAYS) -> tuple[list[str], list[int]]:
    """连续 need 天同一个原始阶段，才切换已确认阶段。"""
    current = "none"
    run = 0
    pending = None
    pending_n = 0
    stages: list[str] = []
    runs: list[int] = []
    for item in raws:
        if item == current:
            pending = None
            pending_n = 0
            run += 1
        elif pending == item:
            pending_n += 1
            if pending_n >= need:
                current = item
                run = need
                pending = None
                pending_n = 0
            else:
                run += 1
        else:
            pending = item
            pending_n = 1
            run += 1
        stages.append(current)
        runs.append(run)
    return stages, runs


def evaluate(sector: dict, ts_code: str) -> dict:
    days = list(sector.get("days") or [])
    prepared = _prepare(days)
    raws = []
    limit_hist: list[float] = []
    for index, day in enumerate(prepared):
        limit_hist.append(float(day.get("limit_up") or 0))
        pctile = _percentile(limit_hist, limit_hist[-1]) if len(limit_hist) >= 20 else None
        prior = prepared[index - 5] if index >= 5 else None
        raws.append(raw_stage(day, prior, pctile))
    stages, runs = confirm(raws, CONFIRM_DAYS) if raws else ([], [])
    stage = stages[-1] if stages else "none"
    confirmed_days = runs[-1] if stage != "none" and runs else 0
    if stage == "none":
        confirmed_days = 0
    days_in_round = confirmed_days
    corr = sector.get("corr_20d")
    if corr is None:
        corr = pearson(
            [day.get("stock") for day in prepared],
            [day.get("sector") for day in prepared],
        )
    role, self_rank, ranked = _roles(sector.get("members") or [], ts_code, corr, stage)
    items = _sustain(sector, prepared, days_in_round, stage)
    excess = _excess(prepared)
    gaps = _gaps(prepared)
    lean, level, certainty, reasons = _direction(stage, role, excess, items)
    window = prepared[-20:]
    dates = [day.get("date") for day in window]
    round_start = None
    if days_in_round and len(prepared) >= days_in_round:
        round_start = prepared[-days_in_round].get("date")
    last = prepared[-1] if prepared else {}
    prior_rank = prepared[-6] if len(prepared) >= 6 else None
    overdue = lean == "bullish" and any(
        item["key"] == "duration" and item["status"] == "warn" for item in items
    )
    return {
        "corr_20d": None if corr is None else round(float(corr), 2),
        "lean": lean,
        "level": level,
        "certainty": certainty,
        "certainty_reasons": reasons,
        "tag_text": _tag(stage, role),
        "stage": stage,
        "confirmed_days": confirmed_days,
        "days_in_round": days_in_round,
        "round_start": round_start,
        "signals": _signals(last, prepared[-2] if len(prepared) >= 2 else None),
        "why_text": _why(stage, confirmed_days),
        "next_text": _next(stage),
        "role": role,
        "reason_text": _role_reason(role, self_rank, stage),
        "role_text": _ROLE_TEXT.get(role, "跟风"),
        "self_rank": self_rank,
        "member_count": sum(1 for item in sector.get("members") or [] if not _is_st_name(item.get("name"))),
        "top": ranked,
        "lines": {
            "dates": dates,
            "stock": cumulative([day.get("stock") for day in window]),
            "sector": cumulative([day.get("sector") for day in window]),
            "index": cumulative([day.get("index") for day in window]),
            "today": {
                "stock": None if not window else window[-1].get("stock"),
                "sector": None if not window else window[-1].get("sector"),
                "index": None if not window else window[-1].get("index"),
            },
        },
        "rank": {
            "value": last.get("rank"),
            "total": last.get("rank_total"),
            "value_5d_ago": None if prior_rank is None else prior_rank.get("rank"),
            "level": last.get("rank_level"),
            "level_text": last.get("rank_level_text"),
            "basis_name": last.get("rank_basis_name"),
            "basis_text": last.get("rank_basis_text"),
        },
        "flow_rate": sector.get("flow_rate"),
        "turnover_share": _turnover(sector),
        "excess": excess,
        "gaps": gaps,
        "sustain_items": items,
        "watch_next": _watch(stage),
        "position_hint": {
            "climax": stage == "climax",
            "overdue": overdue,
        },
        "counter_text": _counter(items, stage, prepared[-1] if prepared else None),
    }


_SIGN = {"bullish": 1, "bearish": -1}


def combine(primary: dict, secondary: dict | None, weights: dict | None) -> dict:
    """两个板块的方向按驱动占比合成一个。阶段对立（一多一空）时不平均成"中性"，标成分歧，把握降一档。
    返回 lean / level / certainty / certainty_reasons / divergent。"""
    if secondary is None or not weights:
        return {
            "lean": primary["lean"], "level": primary["level"], "certainty": primary["certainty"],
            "certainty_reasons": list(primary["certainty_reasons"]), "divergent": False,
        }
    wp, ws = float(weights.get("primary") or 0.0), float(weights.get("secondary") or 0.0)
    sp, ss = _SIGN.get(primary["lean"], 0) * primary["level"], _SIGN.get(secondary["lean"], 0) * secondary["level"]
    divergent = sp * ss < 0
    blend = wp * sp + ws * ss
    if divergent:
        lean, level = "neutral", 1
    elif abs(blend) < 0.5:
        lean, level = "unclear", 0
    else:
        lean, level = ("bullish" if blend > 0 else "bearish"), min(3, max(1, round(abs(blend))))
    reasons = list(primary["certainty_reasons"])
    ranked = [value for value in (primary["certainty"], secondary["certainty"]) if value in _CERTAINTY]
    if divergent:
        certainty = "low"
        reasons.append("两个板块阶段对立")
    elif lean in _SIGN:
        dominant = primary if wp >= ws else secondary
        certainty = max(ranked, key=_CERTAINTY.index) if ranked and min(wp, ws) >= 0.25 else dominant["certainty"]
    else:
        certainty = None
    return {"lean": lean, "level": level, "certainty": certainty, "certainty_reasons": reasons, "divergent": divergent}


def relative_verdict(stock_vs_index: float | None, warn_texts: list[str]) -> dict:
    """没有归属的板块、或板块解释不了本股走势时，只保留个股相对大盘的强弱，力度固定 1。"""
    if stock_vs_index is None:
        lean = "unclear"
    elif stock_vs_index > 0.03:
        lean = "bullish"
    elif stock_vs_index < -0.03:
        lean = "bearish"
    else:
        lean = "neutral"
    reasons = ["没有可归属的板块"]
    reasons.extend(warn_texts)
    return {
        "lean": lean,
        "level": 1,
        "certainty": "low",
        "certainty_reasons": reasons,
        "tag_text": "独立走势，板块结论不适用",
        "counter_text": warn_texts[0] if warn_texts else "没有能解释本股走势的板块",
        "excess": stock_vs_index,
        "gaps": {
            "sector_vs_index": None,
            "stock_vs_sector": None,
            "stock_vs_index": stock_vs_index,
        },
    }


def _prepare(days: list[dict]) -> list[dict]:
    prepared = []
    sector_rets: list[float | None] = []
    index_rets: list[float | None] = []
    for day in days:
        item = dict(day)
        rank = item.get("rank")
        total = item.get("rank_total")
        if item.get("rank_pct") is None and rank and total:
            item["rank_pct"] = rank / total
        sector_rets.append(item.get("sector"))
        index_rets.append(item.get("index"))
        item["sector_5d"] = compound(sector_rets[-5:]) if len(sector_rets) >= 5 else None
        item["index_5d"] = compound(index_rets[-5:]) if len(index_rets) >= 5 else None
        item["sector_20d"] = compound(sector_rets[-20:]) if len(sector_rets) >= 20 else None
        item["index_20d"] = compound(index_rets[-20:]) if len(index_rets) >= 20 else None
        prepared.append(item)
    return prepared


def _percentile(history: list[float], value: float) -> float:
    below = sum(1 for item in history if item < value)
    equal = sum(1 for item in history if item == value)
    return (below + 0.5 * equal) / len(history)


def _roles(members: list[dict], ts_code: str, corr: float | None, stage: str) -> tuple[str, int | None, list[dict]]:
    eligible = [item for item in members if not _is_st_name(item.get("name"))]
    ordered = sorted(eligible, key=lambda item: (
        item.get("ret_20d") is None,
        -(item.get("ret_20d") or 0),
        item.get("ts_code") or "",
    ))
    bearish = stage in _BEARISH
    count = len(ordered)
    rows = []
    self_rank = None
    self_role = "follower" if corr is None or corr >= INDEPENDENT_CORR else "independent"
    for index, item in enumerate(ordered, start=1):
        is_self = item.get("ts_code") == ts_code
        role = _role_of(index, count, is_self, corr)
        if bearish and role == "leader":
            role = "resilient"
        if is_self:
            self_rank = index
            self_role = role
        rows.append({
            "rank": index,
            "ts_code": item.get("ts_code"),
            "name": item.get("name") or item.get("ts_code"),
            "ret_20d": None if item.get("ret_20d") is None else round(float(item["ret_20d"]), 4),
            "role": role,
            "role_text": _ROLE_TEXT.get(role, "跟风"),
            "is_self": is_self,
        })
    shown = rows[:4]
    if self_rank is not None and self_rank > 4:
        shown.append(rows[self_rank - 1])
    return self_role, self_rank, shown


def _is_st_name(name) -> bool:
    text = str(name or "").strip().upper().replace(" ", "")
    return text.startswith(("*ST", "S*ST", "SST", "ST"))


def _role_of(rank: int, count: int, is_subject: bool, corr: float | None) -> str:
    if is_subject and corr is not None and corr < INDEPENDENT_CORR:
        return "independent"
    if rank <= LEADER_TOP_N:
        return "leader"
    if count and rank / count <= CORE_TOP_PCT:
        return "core"
    return "follower"


def _excess(days: list[dict]) -> float | None:
    window = days[-20:]
    sector = compound([day.get("sector") for day in window])
    index = compound([day.get("index") for day in window])
    if sector is None or index is None:
        return None
    return sector - index


def _gaps(days: list[dict]) -> dict:
    window = days[-20:]
    stock = compound([day.get("stock") for day in window])
    sector = compound([day.get("sector") for day in window])
    index = compound([day.get("index") for day in window])

    def diff(left, right):
        if left is None or right is None:
            return None
        return left - right

    return {
        "sector_vs_index": diff(sector, index),
        "stock_vs_sector": diff(stock, sector),
        "stock_vs_index": diff(stock, index),
    }


def _direction(stage: str, role: str, excess: float | None, items: list[dict]):
    warns = [item["text"] for item in items if item["status"] == "warn"]
    if stage == "weak":
        return "bearish", 1, "low", ["弱势阶段，把握上限为低"]
    if stage in _BULLISH:
        lean = "bullish"
    elif stage in _BEARISH:
        lean = "bearish"
    else:
        return "unclear", 0, None, []
    level = strength_band(excess)
    if lean == "bullish" and role == "leader":
        level += 1
    elif lean == "bullish" and role == "follower":
        level -= 1
    elif lean == "bearish" and role == "follower":
        level += 1
    level = min(3, max(1, level))
    return lean, level, _lower(_base_certainty(stage, role), len(warns)), warns


def _base_certainty(stage: str, role: str) -> str:
    if stage in {"climax", "diverge", "weak"}:
        return "low"
    if stage == "main" and role in {"leader", "core"}:
        return "high"
    return "medium"


def _lower(level: str, steps: int) -> str:
    index = _CERTAINTY.index(level)
    return _CERTAINTY[min(len(_CERTAINTY) - 1, index + steps)]


def _tag(stage: str, role: str) -> str:
    return f"{_TAG.get(stage, _TAG['none'])}，{_ROLE_TAG.get(role, _ROLE_TAG['follower'])}"


def _signals(day: dict, prior: dict | None) -> list[dict]:
    breadth = day.get("breadth")
    breadth_prior = None if prior is None else prior.get("breadth")
    if breadth is None:
        breadth_text = "—"
    elif breadth > 0.80:
        breadth_text = "普涨"
    elif breadth > 0.50:
        breadth_text = "多数在 20 日线上"
    elif breadth > 0.20:
        breadth_text = "部分在 20 日线上"
    else:
        breadth_text = "普遍在 20 日线下"
    up_ratio = day.get("up_ratio_today")
    limit_up = day.get("limit_up")
    limit_prior = None if prior is None else prior.get("limit_up")
    if limit_up is None:
        limit_text = "—"
    elif limit_up <= 0:
        limit_text = "没有涨停"
    elif limit_up <= 2:
        limit_text = "温和"
    else:
        limit_text = "偏多"
    streak = day.get("max_streak")
    streak_prior = None if prior is None else prior.get("max_streak")
    if streak is None:
        streak_text = "—"
    elif streak <= 0:
        streak_text = "没有连板"
    else:
        streak_text = f"{int(streak)} 板"
    broken = day.get("broken_rate")
    broken_prior = None if prior is None else prior.get("broken_rate")
    if broken is None:
        broken_text = "—"
    elif broken < 0.20:
        broken_text = "分歧小"
    else:
        broken_text = "分歧大"
    return [
        {
            "key": "breadth",
            "value": breadth,
            "level_text": breadth_text,
            "value_text": _pct_text(breadth),
            "change_text": _point_change(breadth, breadth_prior),
        },
        {
            "key": "up_ratio_today",
            "value": up_ratio,
            "level_text": "—" if up_ratio is None else "当日上涨",
            "value_text": _pct_text(up_ratio),
            "change_text": None,
        },
        {
            "key": "limit_up",
            "value": limit_up,
            "level_text": limit_text,
            "value_text": "—" if limit_up is None else f"{int(limit_up)} 家",
            "change_text": _count_change(limit_up, limit_prior),
        },
        {
            "key": "max_streak",
            "value": streak,
            "level_text": streak_text,
            "value_text": "—" if streak is None else ("没有连板" if streak <= 0 else f"{int(streak)} 板"),
            "change_text": _count_change(streak, streak_prior),
        },
        {
            "key": "broken_rate",
            "value": broken,
            "level_text": broken_text,
            "value_text": _pct_text(broken),
            "change_text": _point_change(broken, broken_prior),
        },
    ]


def _pct_text(value) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.0f}%"


def _point_change(current, previous) -> str:
    if current is None or previous is None:
        return "—"
    delta = round((float(current) - float(previous)) * 100)
    if delta > 0:
        return f"+{delta}pt"
    return f"{delta}pt"


def _count_change(current, previous) -> str:
    if current is None or previous is None:
        return "—"
    delta = int(current) - int(previous)
    if delta > 0:
        return f"+{delta}"
    return str(delta)


def _why(stage: str, confirmed_days: int) -> str:
    text = {
        "start": "近 5 日排名明显抬升，站上 20 日线的家数还没过半。",
        "main": "涨幅排名在前 30%，站上 20 日线的家数过半。",
        "climax": "主升还在，站上 20 日线的家数超过八成，涨停到了近期高位。",
        "diverge": "之前在领涨，近 5 日站上 20 日线的家数明显回落。",
        "ebb": "近 5 日排名下滑，跑输大盘，而且没有涨停。",
        "weak": "涨幅排名在后 30%，并且近 20 日跑输大盘。",
        "none": "还没有连续 3 天满足任一阶段。",
    }.get(stage, "还没有连续 3 天满足任一阶段。")
    if stage != "none" and confirmed_days:
        text = text[:-1] + f"，已连续 {confirmed_days} 天。"
    return text


def _next(stage: str) -> str:
    return {
        "start": "若排名稳住且站上 20 日线的家数过半，会进入主升。",
        "main": "若站上 20 日线的家数超过八成且涨停变多，会进入高潮。",
        "climax": "这个阶段通常不久，涨停减少就会离开高潮。",
        "diverge": "若排名继续下滑并跑输大盘，会进入退潮。",
        "ebb": "等排名重新抬升，才有下一轮。",
        "weak": "若排名连续 3 天回到前 30%，会转为偏多。",
        "none": "等排名和站上 20 日线的比例连续 3 天达标，才会定阶段。",
    }.get(stage, "等排名和站上 20 日线的比例连续 3 天达标，才会定阶段。")


def _role_reason(role: str, self_rank: int | None, stage: str) -> str:
    if role == "independent":
        return "个股和板块近 20 日相关度低于 0.6"
    if self_rank is None:
        return "成分股里没有这只股票的涨幅"
    if role == "resilient":
        return f"近 20 日跌幅较小，在成分股里排第 {self_rank}，属于抗跌"
    if role == "leader":
        return f"近 20 日涨幅在成分股里排第 {self_rank}"
    if role == "core":
        return f"近 20 日涨幅在成分股前 30%，排第 {self_rank}"
    if stage in _BEARISH:
        return f"近 20 日涨幅在成分股里排第 {self_rank}，属于跟风"
    return f"近 20 日涨幅在成分股里排第 {self_rank}，属于跟风"


def _turnover(sector: dict) -> dict:
    raw = sector.get("turnover_share")
    if isinstance(raw, dict):
        return {
            "value": raw.get("value"),
            "vs_last_week": raw.get("vs_last_week"),
            "pctile_1y": raw.get("pctile_1y"),
            "basis_text": raw.get("basis_text") or "成分股成交额占沪深两市",
        }
    return {"value": None, "vs_last_week": None, "pctile_1y": None}


def _sustain(sector: dict, days: list[dict], days_in_round: int, stage: str) -> list[dict]:
    catalyst = sector.get("catalyst")
    if isinstance(catalyst, dict) and catalyst.get("event_id"):
        catalyst_item = {
            "key": "catalyst",
            "status": "ok",
            "text": catalyst.get("text") or "有对应的板块消息",
            "ref": {"event_id": catalyst.get("event_id"), "source_grade": catalyst.get("source_grade")},
        }
    else:
        catalyst_item = {
            "key": "catalyst",
            "status": "na",
            "text": "近期没有对应的板块消息",
            "ref": None,
        }
    share = _turnover(sector)
    pctile = share.get("pctile_1y")
    if pctile is None:
        crowding = {"key": "crowding", "status": "na", "text": "没有成交占比数据", "ref": None}
    elif pctile >= 0.90:
        crowding = {"key": "crowding", "status": "warn", "text": f"成交占比处在近一年高位（分位 {round(pctile * 100)}%）", "ref": {"pctile_1y": pctile}}
    else:
        crowding = {"key": "crowding", "status": "ok", "text": "成交占比不在近一年高位", "ref": {"pctile_1y": pctile}}
    median = sector.get("round_median")
    if median is None:
        median = _round_median(days)
    bearish = stage in _BEARISH
    if median is None or not days_in_round:
        duration = {"key": "duration", "status": "na", "text": "同类行情太少，还没法比持续天数", "ref": None}
    elif days_in_round > float(median):
        shown = _day_count(median)
        if bearish:
            duration = {
                "key": "duration",
                "status": "warn",
                "text": f"这轮{_round_name(stage)}已经 {days_in_round} 天，超过同类行情的中位数（{shown} 天），可能接近尾声",
                "ref": {"days": days_in_round, "median": median},
            }
        else:
            duration = {
                "key": "duration",
                "status": "warn",
                "text": f"本轮持续天数超过同类行情中位数（已 {days_in_round} 天，中位数 {shown} 天）",
                "ref": {"days": days_in_round, "median": median},
            }
    else:
        shown = _day_count(median)
        if bearish:
            name = _round_name(stage)
            duration = {
                "key": "duration",
                "status": "support",
                "text": f"这轮{name}已经 {days_in_round} 天，还没超过同类行情的中位数（{shown} 天），{name}可能还会继续",
                "ref": {"days": days_in_round, "median": median},
            }
        else:
            duration = {
                "key": "duration",
                "status": "ok",
                "text": f"本轮已经 {days_in_round} 天，还没超过同类行情的中位数（{shown} 天）",
                "ref": {"days": days_in_round, "median": median},
            }
    index_20d = compound([day.get("index") for day in days[-20:]])
    if index_20d is None:
        environment = {"key": "environment", "status": "na", "text": "没有大盘走势", "ref": None}
    elif index_20d < -0.03 and bearish:
        shown = f"{index_20d * 100:+.1f}%"
        environment = {"key": "environment", "status": "support", "text": f"大盘近 20 日 {shown}，偏弱，和支持偏空一致", "ref": {"index_20d": round(index_20d, 4)}}
    elif index_20d < -0.03:
        shown = f"{index_20d * 100:+.1f}%"
        environment = {"key": "environment", "status": "warn", "text": f"大盘近 20 日 {shown}，偏弱", "ref": {"index_20d": round(index_20d, 4)}}
    else:
        environment = {"key": "environment", "status": "ok", "text": "大盘没有明显走弱", "ref": {"index_20d": round(index_20d, 4)}}
    return [catalyst_item, crowding, duration, environment]


def _round_name(stage: str) -> str:
    return {"weak": "弱势", "ebb": "退潮", "diverge": "分歧"}.get(stage, "行情")


def _day_count(value) -> str:
    number = float(value)
    if number == int(number):
        return str(int(number))
    return f"{number:.0f}"


def _round_median(days: list[dict]) -> float | None:
    """用这段行情里已经结束的阶段长度做中位数。样本少于 2 段则不作比较。"""
    if len(days) < 20:
        return None
    prepared = days if days and "rank_pct" in days[0] else _prepare(days)
    raws = []
    limit_hist: list[float] = []
    for index, day in enumerate(prepared):
        limit_hist.append(float(day.get("limit_up") or 0))
        pctile = _percentile(limit_hist, limit_hist[-1]) if len(limit_hist) >= 20 else None
        prior = prepared[index - 5] if index >= 5 else None
        raws.append(raw_stage(day, prior, pctile))
    stages, _runs = confirm(raws, CONFIRM_DAYS)
    lengths = []
    previous = None
    run = 0
    for item in stages:
        if item == previous:
            run += 1
            continue
        if previous not in (None, "none") and run:
            lengths.append(run)
        previous = item
        run = 1
    if len(lengths) < 2:
        return None
    lengths.sort()
    return float(lengths[len(lengths) // 2])


def _counter(items: list[dict], stage: str, last: dict | None) -> str:
    for item in items:
        if item["key"] == "duration" and item["status"] == "warn":
            return item["text"]
    if stage in _BEARISH:
        bounce = _bounce_counter(last)
        if bounce:
            return bounce
    for item in items:
        if item["status"] == "warn":
            return item["text"]
    return "还没有看到反向信号"


def _bounce_counter(day: dict | None) -> str | None:
    """弱势里的单日反弹是反方证据。全部上涨时写明，没超过中位数的持续天数不放这里。"""
    if not day:
        return None
    sector = day.get("sector")
    ratio = day.get("up_ratio_today")
    if sector is None or sector <= 0:
        return None
    pct = f"{sector * 100:.1f}"
    if ratio is not None and ratio >= 0.999:
        return f"今天板块全部上涨，反弹 {pct}%，要看能不能持续"
    if ratio is not None and ratio >= 0.8:
        return f"今天板块多数上涨，反弹 {pct}%，要看能不能持续"
    if sector >= 0.02:
        return f"今天板块反弹 {pct}%，要看能不能持续"
    return None


def _watch(stage: str) -> list[dict]:
    if stage == "weak":
        return [
            {
                "condition_id": "sector_rank_leave_bottom",
                "text": "排名连续 3 天回到后 30% 之外",
                "then_text": "弱势结束，转为不明",
                "subscribed": False,
                "alert_id": None,
            },
            {
                "condition_id": "sector_rank_recover",
                "text": "排名连续 3 天回到前 30%",
                "then_text": "转为偏多",
                "subscribed": False,
                "alert_id": None,
            },
        ]
    rows = []
    if stage != "climax":
        rows.append({
            "condition_id": "sector_enter_climax",
            "text": "进入高潮",
            "then_text": "位置变为偏高",
            "subscribed": False,
            "alert_id": None,
        })
    else:
        rows.append({
            "condition_id": "sector_leave_climax",
            "text": "离开高潮",
            "then_text": "位置不再只按高潮看",
            "subscribed": False,
            "alert_id": None,
        })
    if stage in {"main", "climax", "start"}:
        rows.append({
            "condition_id": "sector_breadth_drop",
            "text": "站上 20 日线的家数 5 日内回落超过 15 个百分点",
            "then_text": "阶段可能变成分歧",
            "subscribed": False,
            "alert_id": None,
        })
    return rows
