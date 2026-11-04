"""板块事实表：归属证据、驱动占比、两个板块的状态都由代码算好，模型只引用编号。

返回 (facts, derived)：facts 给模型看，derived 是原始结构，给模板答案和核对用。
主板块 sp.p.*，次板块 sp.s.*。归属由程序判定，模型不能改。
"""

from __future__ import annotations

from app.reviews.core import FactSheet
from app.synthesis.types import Fact

STAGE_TEXT = {"start": "启动", "main": "主升", "climax": "高潮", "diverge": "分歧", "ebb": "退潮", "weak": "弱势", "none": "没有明确阶段"}
ROLE_TEXT = {"leader": "领涨", "resilient": "抗跌", "core": "中军", "follower": "跟风", "independent": "独立"}
KIND_TEXT = {"industry": "申万行业", "concept": "同花顺概念"}
BULLISH, BEARISH = ("start", "main", "climax"), ("diverge", "ebb", "weak")
HANG_LIMIT = 3

GROUP_ORDER = ("主板块归属", "次板块归属", "主营构成", "只挂名", "驱动", "主板块状态", "次板块状态", "综合")


def _put(facts: FactSheet, fact_id: str, label: str, group: str, kind: str, value, text: str) -> None:
    if value is None:
        return
    facts.labels[fact_id] = label
    facts.meta[fact_id] = (group, kind)
    facts.append(Fact(fact_id, value, text))


def _pct(value: float, digits: int = 0) -> str:
    return f"{value * 100:.{digits}f}%"


def _signed(value: float) -> str:
    return f"{value * 100:+.1f}%"


def basis_text(evidence: dict) -> str:
    """为什么算"属于"，一句话。"""
    business = evidence.get("business") or {}
    link = evidence.get("linkage") or {}
    pos = evidence.get("position") or {}
    parts = []
    if (business.get("share") or 0) >= 0.2 or evidence.get("basis") == "business":
        note = business.get("basis") or ("、".join(business.get("items") or []) or "主营产品")
        parts.append(f"业务占比 {_pct(business.get('share') or 0)}（{note}）")
    if link.get("corr60") is not None and pos.get("rank"):
        parts.append(f"近 60 日与板块指数相关 {link['corr60']:.2f}，市值排名 {pos['rank']}/{pos['total']}")
    return "；".join(parts) or "证据不足"


def _membership(facts: FactSheet, d: dict, tag: str, group: str, label: str, block: dict | None) -> None:
    if not block:
        return
    ev = block.get("evidence") or {}
    business, link, pos = ev.get("business") or {}, ev.get("linkage") or {}, ev.get("position") or {}
    p = f"sp.{tag}"
    d[tag] = block
    _put(facts, f"{p}.name", f"{label}", group, "text", block["name"], f"{block['name']}（{KIND_TEXT.get(block['type'], '')}）")
    _put(facts, f"{p}.basis", f"{label}为什么算属于", group, "text", ev.get("basis"), basis_text(ev))
    if business.get("share") is not None:
        items = "、".join(business.get("items") or [])
        note = business.get("basis") or (f"对应主营产品：{items}" if items else "没有对应的主营产品")
        _put(facts, f"{p}.biz", f"{label}的业务占比", group, "share", business["share"], f"{_pct(business['share'])}（{note}）")
    if link.get("corr60") is not None:
        _put(facts, f"{p}.corr60", f"本股与{label}指数近 60 日相关", group, "ratio", link["corr60"], f"{link['corr60']:.2f}")
    if link.get("corr20") is not None:
        _put(facts, f"{p}.corr20", f"本股与{label}指数近 20 日相关", group, "ratio", link["corr20"], f"{link['corr20']:.2f}")
    if link.get("beta60") is not None:
        _put(facts, f"{p}.beta60", f"本股对{label}指数的 beta（60 日）", group, "ratio", link["beta60"], f"{link['beta60']:.2f}")
    if link.get("up_capture") is not None and link.get("down_capture") is not None:
        _put(facts, f"{p}.capture", f"{label}涨时和跌时本股的跟随程度", group, "text", [link["up_capture"], link["down_capture"]],
             f"涨时跟随 {link['up_capture']:.2f}，跌时跟随 {link['down_capture']:.2f}")
    if pos.get("rank"):
        _put(facts, f"{p}.rank", f"本股在{label}成分里的市值排名", group, "rank", pos["top_pct"], f"{pos['rank']}/{pos['total']}")
    _state(facts, d, tag, group.replace("归属", "状态"), label, block)


def _state(facts: FactSheet, d: dict, tag: str, group: str, label: str, block: dict) -> None:
    p = f"sp.{tag}"
    stage = (block.get("stage") or {})
    current = stage.get("current")
    _put(facts, f"{p}.stage", f"{label}当前阶段", group, "text", current, STAGE_TEXT.get(current, "没有明确阶段"))
    if stage.get("days_in_round"):
        _put(facts, f"{p}.stage_days", f"{label}这一轮已持续", group, "days", stage["days_in_round"], f"{stage['days_in_round']} 个交易日")
    role = block.get("role") or {}
    if role.get("value"):
        rank = f"，在 {role['member_count']} 只里排第 {role['self_rank']}" if role.get("self_rank") and role.get("member_count") else ""
        _put(facts, f"{p}.role", f"本股在{label}里的角色", group, "text", role["value"], f"{ROLE_TEXT.get(role['value'], role['value'])}{rank}")
    strength = block.get("strength") or {}
    lines = (strength.get("lines") or {})
    for key, name in (("sector", f"{label}近 20 日涨跌幅"), ("stock", "本股近 20 日涨跌幅"), ("index", "沪深300 近 20 日涨跌幅")):
        series = lines.get(key) or []
        if series:
            _put(facts, f"{p}.ret_{key}", name, group, "pct", series[-1], _signed(series[-1]))
    gaps = strength.get("gaps") or {}
    if gaps.get("sector_vs_index") is not None:
        _put(facts, f"{p}.vs_index", f"{label}近 20 日相对沪深300", group, "pct", gaps["sector_vs_index"], _signed(gaps["sector_vs_index"]))
    if gaps.get("stock_vs_sector") is not None:
        _put(facts, f"{p}.stock_vs_sector", f"本股近 20 日相对{label}", group, "pct", gaps["stock_vs_sector"], _signed(gaps["stock_vs_sector"]))
    breadth = next((item for item in stage.get("signals") or [] if item.get("key") == "breadth"), None)
    if breadth and breadth.get("value") is not None:
        _put(facts, f"{p}.breadth", f"{label}站上 20 日线的成分占比", group, "share", breadth["value"], _pct(breadth["value"]))
    rank = strength.get("rank") or {}
    if rank.get("value") and rank.get("total"):
        _put(facts, f"{p}.sw_rank", f"{label}近 20 日涨幅在{rank.get('level_text') or '申万行业'}里的名次", group, "rank", rank["value"], f"{rank['value']}/{rank['total']}")
    hint = block.get("position_hint") or {}
    items = (block.get("sustain") or {}).get("items") or block.get("sustain_items") or []
    warns = [item.get("text") for item in items if item.get("status") == "warn" and item.get("text")]
    if hint.get("climax") or hint.get("overdue") or warns:
        bits = ["高潮阶段"] if hint.get("climax") else []
        bits.extend(text for text in warns if text not in bits)
        if not bits and hint.get("overdue"):
            bits.append("这一轮持续天数已超过同类行情的中位数")
        _put(facts, f"{p}.warn", f"{label}的位置提示", group, "text", True, "；".join(bits))


def build(payload: dict) -> tuple[FactSheet, dict]:
    facts = FactSheet()
    sectors = payload.get("sectors") or {}
    d: dict = {"independent": bool(sectors.get("independent")), "primary": None, "secondary": None}
    primary, secondary = sectors.get("primary"), sectors.get("secondary")
    _membership(facts, d, "p", "主板块归属", "主板块", primary)
    _membership(facts, d, "s", "次板块归属", "次板块", secondary)
    d["primary"], d["secondary"] = primary, secondary

    lines = sectors.get("business_lines") or []
    if lines:
        shown = "、".join(f"{row['name']} {_pct(row['share'])}" for row in lines[:5])
        _put(facts, "sp.biz.lines", "最近一期主营产品构成", "主营构成", "text", [row["name"] for row in lines], shown)

    hang = [c for c in sectors.get("candidates") or [] if not c.get("belongs") and (c.get("linkage") or {}).get("corr60") is not None]
    hang.sort(key=lambda c: -(c["linkage"]["corr60"]))
    strong = [c for c in hang if c["linkage"]["corr60"] >= 0.6][:HANG_LIMIT]
    d["hang"] = strong
    if strong:
        text = "、".join(f"{c['name']}（相关 {c['linkage']['corr60']:.2f}，{c['basis']}）" for c in strong)
        _put(facts, "sp.hang", "联动强却不算属于的板块", "只挂名", "text", [c["name"] for c in strong], text)
    _put(facts, "sp.cand.count", "参与判断的候选板块数", "只挂名", "count", len(sectors.get("candidates") or []), f"{len(sectors.get('candidates') or [])} 个")
    merged = sectors.get("merged") or []
    if merged:
        _put(facts, "sp.merged", "因走势几乎相同而并入主板块的板块", "只挂名", "text", [row["name"] for row in merged], "、".join(row["name"] for row in merged))

    driver = sectors.get("driver") or {}
    d["driver"] = driver
    _put(facts, "sp.driver.type", "本股走势的驱动方式", "驱动", "text", driver.get("type"), driver.get("type_text"))
    if driver.get("r2") is not None:
        _put(facts, "sp.driver.r2", "板块合计解释了本股收益的比例（近 60 日）", "驱动", "share", driver["r2"], _pct(driver["r2"]))
    if driver.get("primary_share") is not None:
        _put(facts, "sp.driver.p_share", "主板块解释的比例", "驱动", "share", driver["primary_share"], _pct(driver["primary_share"]))
    if driver.get("secondary_share") is not None:
        _put(facts, "sp.driver.s_share", "次板块解释的比例", "驱动", "share", driver["secondary_share"], _pct(driver["secondary_share"]))
    if driver.get("weights") and secondary:
        w = driver["weights"]
        _put(facts, "sp.driver.weights", "两个板块在已解释部分里的占比", "驱动", "text", [w.get("primary"), w.get("secondary")],
             f"主板块 {_pct(w['primary'])}，次板块 {_pct(w['secondary'])}")

    divergence = sectors.get("divergence") or {}
    d["divergent"] = bool(divergence.get("active"))
    if secondary:
        stages = divergence.get("stages") or {}
        same = "对立" if d["divergent"] else "同向" if _lean(stages.get("primary")) == _lean(stages.get("secondary")) and _lean(stages.get("primary")) else "不明"
        _put(facts, "sp.consistency", "两个板块的阶段是否一致", "综合", "text", same,
             f"主板块{STAGE_TEXT.get(stages.get('primary'), '')}，次板块{STAGE_TEXT.get(stages.get('secondary'), '')}，方向{same}")
    return facts, d


def _lean(stage) -> int:
    return 1 if stage in BULLISH else -1 if stage in BEARISH else 0


def public(facts: FactSheet) -> list[dict]:
    rows = []
    for fact in facts:
        group, kind = facts.meta.get(fact.id, ("其他", "text"))
        rows.append({
            "id": fact.id, "label": facts.labels.get(fact.id, fact.id), "group": group, "kind": kind,
            "value": round(fact.value, 6) if isinstance(fact.value, float) else fact.value, "text": fact.text,
        })
    order = {name: index for index, name in enumerate(GROUP_ORDER)}
    rows.sort(key=lambda row: order.get(row["group"], len(order)))
    return rows
