"""页面接口的返回：规则版模板文案，以及把 Agent 结论、代码数字、价位、可信度合并成页面结构。

页面上不区分结论来自 Agent 还是规则；来源只写在 internal 里。
"""

from __future__ import annotations

from datetime import datetime

from app.synthesis import explain, policy, rules, trend
from app.synthesis.producer.adapters import view_text
from app.synthesis.types import Snapshot

_DIM_VIEW_FACT = {
    "volume_flow": "flow.view", "news": "news.view", "sector": "sector.view",
    "finance": "fin.view", "fundamental": "fund.view",
}
_STATUS_TEXT = {
    "ok": "正常", "stale": "已过期", "pending": "还在整理", "unavailable": "暂不可用", "invalid": "数据异常",
}


def _label(key: str) -> str:
    return policy.DIMENSION_LABEL.get(key, key)


def counts_text(counts: dict) -> str:
    neutral = counts.get("neutral", 0)
    parts = []
    if counts.get("bullish"):
        parts.append(f"{counts['bullish']} 个偏多")
    if counts.get("bearish"):
        parts.append(f"{counts['bearish']} 个偏空")
    if neutral:
        parts.append(f"{neutral} 个中性")
    return " · ".join(parts) or "都没有结论"


def subtitle(rule: dict) -> str:
    fresh = "数据都是今天的" if rule.get("fresh") else "数据不全是最新的"
    return f"{rule['total']} 个方面里 {rule['done']} 个有结论：{counts_text(rule['counts'])}，{fresh}"


# ---------------------------------------------------------------- 规则版文案
def rule_headline(direction: str, snaps: list[Snapshot]) -> str:
    done = [snap for snap in snaps if snap.done]
    if direction in ("bullish", "bearish"):
        side = sorted((snap for snap in done if snap.lean == direction), key=lambda snap: -snap.level)
        names = [_label(snap.key) for snap in side[:2]]
        verb = "推" if direction == "bullish" else "拖"
        if len(names) >= 2:
            return f"{names[0]}、{names[1]}都在{verb}"[: policy.HEADLINE_MAX]
        return f"{names[0]}在{verb}，其余不明显"[: policy.HEADLINE_MAX]
    leans = {snap.lean for snap in done}
    if {"bullish", "bearish"} <= leans:
        return "各方面信号不一致"
    return "暂时没有明确方向"


def rule_direction_text(direction: str, snaps: list[Snapshot]) -> str:
    done = [snap for snap in snaps if snap.done]
    if direction not in ("bullish", "bearish"):
        counts = rules.synthesize(snaps, {})["counts"]
        return f"{counts_text(counts)}，没有一方占优"
    opposite = "bearish" if direction == "bullish" else "bullish"
    support = [_label(snap.key) for snap in done if snap.lean == direction]
    oppose = [f"{_label(snap.key)}（{policy.DIRECTION_TEXT[opposite]}）" for snap in done if snap.lean == opposite]
    other = [_label(snap.key) for snap in done if snap.lean not in (direction, opposite)]
    text = "支持：" + "、".join(support)
    if oppose:
        text += "；不支持：" + "、".join(oppose)
    if other:
        text += "；没有明确信号：" + "、".join(other)
    return text


def rule_position_text(position: str, market: dict) -> str:
    gap = market.get("market.from_year_high")
    if gap is None:
        return "缺少一年高点数据，按适中处理"
    where = "下方" if gap <= 0 else "上方"
    return f"收盘价在一年高点{where} {abs(gap) * 100:.1f}%，位置{policy.POSITION_TEXT[position]}"


def rule_reasons(direction: str, snaps: list[Snapshot]) -> list[dict]:
    rows = []
    for snap in snaps:
        if not snap.done or snap.lean not in ("bullish", "bearish"):
            continue
        rows.append({
            "side": "bull" if snap.lean == "bullish" else "bear",
            "text": f"{_label(snap.key)}：{view_text(snap)}",
            "cites": [_DIM_VIEW_FACT[snap.key]],
            "weight": snap.level,
            "adopted": snap.lean == direction,
        })
    rows.sort(key=lambda row: (not row["adopted"], -row["weight"]))
    return rows


def rule_counter(direction: str, snaps: list[Snapshot]) -> dict:
    if direction not in ("bullish", "bearish"):
        return {"text": "", "cites": []}
    opposite = "bearish" if direction == "bullish" else "bullish"
    against = sorted((snap for snap in snaps if snap.done and snap.lean == opposite), key=lambda snap: -snap.level)
    if not against:
        return {"text": "不过，目前还没有看到明显的反向信号", "cites": []}
    top = against[0]
    return {"text": f"不过，{_label(top.key)}{policy.DIRECTION_TEXT[opposite]}", "cites": [_DIM_VIEW_FACT[top.key]]}


def rule_final(rule: dict, snaps: list[Snapshot], market: dict) -> dict:
    direction = rule["direction"]
    return {
        "direction": direction,
        "raw_direction": direction,
        "level": rule["level"],
        "position": rule["position"],
        "headline": rule_headline(direction, snaps),
        "direction_text": rule_direction_text(direction, snaps),
        "position_text": rule_position_text(rule["position"], market),
        "reasons": rule_reasons(direction, snaps),
        "counter": rule_counter(direction, snaps),
        "adopted": [],
        "change_reason": "",
        "held_by_anti_flip": False,
        "watch_ids": [],
    }


# ---------------------------------------------------------------- Agent 版
def counter_of(decision: dict) -> dict:
    """反方证据：偏多取被采信的反方论据里分量最高的一条，偏空反之；另一方一条都没被采信时，用事后复盘。"""
    direction = decision["direction"]
    if direction in ("bullish", "bearish"):
        other = "bear" if direction == "bullish" else "bull"
        pool = [item for item in decision["adopted"] if item["side"] == other]
        if pool:
            top = max(pool, key=lambda item: item["weight"])
            return {"text": "不过，" + top["claim"], "cites": top["cites"]}
    post = decision["postmortem"]
    return {"text": "不过，" + post["text"], "cites": post["cites"]} if post.get("text") else {"text": "", "cites": []}


def agent_final(decision: dict) -> dict:
    final = dict(decision)
    final["reasons"] = [
        {"side": item["side"], "text": item["claim"], "cites": item["cites"], "weight": item["weight"], "adopted": True}
        for item in sorted(decision["adopted"], key=lambda item: -item["weight"])
    ]
    final["counter"] = counter_of(decision)
    if decision.get("held_by_anti_flip"):
        final["headline"] = "新信号还需再确认一次"
        final["direction_text"] = "新的信号和上次不同，需要连续两次确认或分差更大，才会调整方向，当前维持上次的判断"
    return final


# ---------------------------------------------------------------- 页面
def dimension_rows(snaps: list[Snapshot]) -> list[dict]:
    rows = []
    for snap in snaps:
        rows.append({
            "key": snap.key,
            "label": _label(snap.key),
            "status": snap.status,
            "status_text": _STATUS_TEXT.get(snap.status, snap.status),
            "reason": snap.reason,
            "lean": snap.lean if snap.done else "unclear",
            "level": snap.level if snap.done else 0,
            "view": view_text(snap) if snap.done else "",
            "as_of": snap.as_of,
        })
    return rows


_ROW_WHY = {
    "good_news_unpriced": "有还没反映到价格里的利好，而且大资金在流入",
    "priced_in_outflow": "利好已经反映过，大资金在往外走",
    "bearish_news_or_ebb": "有利空、板块在走弱，或财报碰到了红线",
    "flow_driven": "新闻没有明确方向，但大资金有明确的流入或流出",
    "sector_driven": "板块处在上涨阶段",
    "unclear": "几个方面还没有同一个方向",
}
_CONFIRM_PLAIN = {
    "close": "收盘价穿过",
    "close_2d": "连续两天收盘穿过",
    "close_volume": "放量之后收盘穿过",
    "close_outflow": "收盘跌破，并且当天大资金净流出",
}
_SKIP = {
    "pending": "还在整理，不参与打分",
    "unavailable": "没有数据，不参与打分",
    "stale": "数据过期，不参与打分",
    "invalid": "数据对不上，不参与打分",
}


def _num(value: float) -> str:
    text = f"{abs(float(value)):.1f}"
    return text.rstrip("0").rstrip(".")


def level_logic(levels: dict | None) -> str:
    """上方价、下方价的算法，用这次实际选出的价来讲。"""
    levels = levels or {}
    why = _ROW_WHY.get(levels.get("row") or "")
    picked = f"这次按这个情况来选：{why}。" if why else ""
    opening = (
        "上方价和下方价都不是现价加减一个固定比例。"
        "程序先从几个固定的价里挑，只留离现价 3% 到 15% 的。"
        "再假设收盘价穿到价位外侧 1%，只重算量价、以及离一年高点有多远；方向、力度或位置变了，这个价才留下。"
        + picked
    )
    return opening + _level_side("上", levels.get("up")) + _level_side("下", levels.get("down"))


def _level_side(side: str, level: dict | None) -> str:
    title = "上方价" if side == "上" else "下方价"
    move = "涨" if side == "上" else "跌"
    if not level:
        kind = "压力，比如前高和一年高点" if side == "上" else "支撑，比如 20 日均线、大资金成本和近期低点"
        return f"{title}这次没有：现价的{side}面找不到可以参照的{kind.split('，')[0]}。"
    name = level.get("base_name") or "这个价"
    price = float(level["price"])
    dist = abs(float(level.get("dist") or 0)) * 100
    confirm = _CONFIRM_PLAIN.get(level.get("confirm") or "close", "收盘价穿过")
    buffer = float(level.get("buffer") or 0)
    room = f"提醒价还在它下方 {buffer * 100:.0f}%。" if buffer else ""
    text = f"{title}是{name} {price:.2f} 元，现价还要{move} {dist:.1f}% 才到。{room}确认方式是{confirm}这个价。"
    if level.get("verified") is False:
        outside = not policy.DIST_MIN <= abs(float(level.get("dist") or 0)) <= policy.DIST_MAX
        text += (
            "注意：这个价没有通过重算验证（穿过它以后，规则版的结论没有变化）"
            + ("，而且不在 3% 到 15% 里" if outside else "")
            + "；因为这个方向没有别的价可选，所以给出离范围最近的一个，只当参考。"
        )
    return text


def judgment_logic(final: dict | None, rule: dict | None) -> str:
    """看多、看空和主分析员怎么下判断。页面用规则时，先讲这次的分数。"""
    roles = "看多和看空分析员只从事实里找论据，各自最多 3 条，不写结论。主分析员不能新编论据，只决定采信哪几条。看多的分量减去看空的分量，差不到 2 就写不明。力度按这个分差换算，不按语气。"
    if final and "spread" in final:
        return _agent_judgment(final, roles)
    return _rule_judgment(rule or {}) + roles + "这次页面上的方向来自上面的分数，主分析员没有改它。"


def _rule_judgment(rule: dict) -> str:
    parts = []
    for item in rule.get("per_dim") or []:
        label = _label(item.get("key"))
        status = item.get("status")
        if status != "ok":
            parts.append(f"{label}{_SKIP.get(status, '这次不参与打分')}")
            continue
        contribution = float(item.get("contribution") or 0)
        if contribution > 0:
            parts.append(f"{label}偏多，加 {_num(contribution)} 分")
        elif contribution < 0:
            parts.append(f"{label}偏空，减 {_num(contribution)} 分")
        elif item.get("lean") == "neutral":
            parts.append(f"{label}是中性，不加不减")
        else:
            parts.append(f"{label}是不明，不加不减")
    score = float(rule.get("score") or 0)
    shown = _num(score) if score >= 0 else "-" + _num(score)
    direction = policy.DIRECTION_TEXT.get(rule.get("direction"), "不明")
    level = policy.LEVEL_TEXT.get(rule.get("level") or 0, "")
    if rule.get("direction") in ("bullish", "bearish"):
        result = f"这次净得分已经离开不明，方向是{direction}" + (f"、{level}" if level else "") + "。"
    else:
        result = "这次没有离开不明。"
    detail = "；".join(parts)
    if detail:
        detail += "。"
    return (
        f"方向先按规则打分。偏多加分，偏空减分，力度越大分越重，把握低只算一半。{detail}"
        f"净得分 {shown} 分，绝对值不到 1 分就写不明。{result}"
    )


def _agent_judgment(final: dict, roles: str) -> str:
    adopted = list(final.get("adopted") or [])
    bull = sum(item["weight"] for item in adopted if item.get("side") == "bull")
    bear = sum(item["weight"] for item in adopted if item.get("side") == "bear")
    quoted = "、".join(
        f"{'看多' if item.get('side') == 'bull' else '看空'}「{item.get('claim')}」（{_num(item.get('weight') or 0)} 分）"
        for item in adopted if item.get("claim")
    )
    taken = f"这次采信了{quoted}。" if quoted else "这次没有采信论据。"
    spread = int(final.get("spread") or 0)
    direction = policy.DIRECTION_TEXT.get(final.get("direction"), "不明")
    level = policy.LEVEL_TEXT.get(final.get("level") or 0, "")
    strength = f"，力度是{level}" if level else ""
    held = "新的方向还没连续确认，所以先维持上次的判断。" if final.get("held_by_anti_flip") else ""
    return (
        f"{roles}{taken}看多合计 {bull} 分，看空合计 {bear} 分，分差 {spread}，"
        f"所以这次方向是{direction}{strength}。{held}"
    )


def explain_fields(
    levels: dict | None,
    rule: dict | None,
    final: dict | None,
    sides: dict | None = None,
    table=None,
    fallback_reason: str = "",
    position_text: str = "",
) -> dict:
    return {
        "level_logic": level_logic(levels),
        "judgment_logic": judgment_logic(final, rule),
        "price_logic": explain.price_logic(levels),
        "judgment": explain.judgment(final, rule, sides, table, fallback_reason, position_text),
    }


def assemble(
    stock,
    final: dict,
    levels: dict,
    rule: dict,
    snaps: list[Snapshot],
    credibility: dict,
    events: list[dict],
    session: str,
    now: datetime,
    sides: dict | None = None,
    table=None,
    fallback_reason: str = "",
    trend_input: dict | None = None,
) -> dict:
    direction = final["direction"]
    level = final["level"]
    label = policy.DIRECTION_TEXT[direction]
    watch = [item for item in events if item["id"] in set(final.get("watch_ids") or [])]
    if not watch:
        watch = events[:2]
    return {
        "ts_code": stock.ts_code,
        "stock_name": stock.name,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "session": session,
        "direction": {
            "lean": direction,
            "level": level,
            "label": label + (f" · {policy.LEVEL_TEXT[level]}" if level else ""),
            "text": final["direction_text"],
        },
        "position": {
            "value": final["position"],
            "label": policy.POSITION_TEXT[final["position"]],
            "text": final["position_text"],
        },
        "headline": f"{label}：{final['headline']}",
        "credibility": {**credibility, "subtitle": subtitle(rule), "counts": rule["counts"]},
        "reasons": final["reasons"],
        "counter": final["counter"],
        "levels": {side: _level_view(levels.get(side)) for side in ("down", "up")},
        "watch": [{"id": item["id"], "text": item["text"]} for item in watch[:2]],
        "change": {"changed": bool(final.get("change_reason")), "text": final.get("change_reason") or ""},
        "dimensions": dimension_rows(snaps),
        "divergence": [_divergence_view(item) for item in rule.get("divergence") or []],
        "trend": trend.build(
            trend_input, levels,
            {"lean": direction, "level": level, "text": final.get("change_reason") or ""}, credibility,
        ),
        "disclaimer": policy.DISCLAIMER,
        **explain_fields(levels, rule, final if "spread" in final else None, sides, table, fallback_reason, final.get("position_text", "")),
    }


def _divergence_view(item: dict) -> dict:
    names = policy.DIMENSION_LABEL
    left, right = item["pair"]
    word = {"bullish": "偏多", "bearish": "偏空"}
    return {
        "pair": item["pair"],
        "text": f"{names.get(left, left)}{word[item['leans'][left]]}，但{names.get(right, right)}{word[item['leans'][right]]}：方向相反，没有取平均，请分别看双方的依据",
        "facts": item["facts"],
    }


def _level_view(level: dict | None) -> dict | None:
    if not level:
        return None
    return {
        "price": level["price"],
        "line": level["line"],
        "condition": level["condition"],
        "consequence": level["consequence"],
        "why": level.get("why") or "",
        "cites": level.get("cites") or [],
        "verified": level.get("verified", True),
    }
