"""把"为什么是这个结论、这两个价是怎么来的"讲成普通人看得懂的话。

两块：price_logic 讲上方价、下方价；judgment 分开讲看多、看空、主分析员的依据和结论。
所有数字都来自代码已经算好的结果，这里只做措辞。
"""

from __future__ import annotations

from app.synthesis import policy
from app.synthesis.levels.candidates import FORMULA
from app.synthesis.registry import definition
from app.synthesis.types import FactTable

_ROW_WHY = {
    "good_news_unpriced": "有还没反映到价格里的利好，而且大资金在流入",
    "priced_in_outflow": "利好已经反映过，大资金在往外走",
    "bearish_news_or_ebb": "有利空、板块在走弱，或财报碰到了红线",
    "flow_driven": "新闻没有明确方向，但大资金有明确的流入或流出",
    "sector_driven": "板块处在上涨阶段",
    "unclear": "几个方面还没有同一个方向",
}
_CONFIRM = {
    "close": "收盘价穿过",
    "close_2d": "连续两天收盘价穿过",
    "close_volume": "放量之后收盘价穿过",
    "close_outflow": "收盘跌破，并且当天大资金净流出",
}
_ORIGIN = {
    "agent": "主分析员在固定菜单里挑的",
    "default": "按参考表选的（参考表按当前的新闻、资金、板块情况分档）",
    "scan": "前面的选项都不满足条件，按顺序试到这一个",
}
_FALLBACK = {
    "": "这次没有用主分析员，页面上的结论来自规则打分",
    "no_model": "这次没有启用模型，页面上的结论来自规则打分",
    "model_error": "模型这次调用失败了，已退回规则打分",
    "validation": "模型的输出没有通过核对（引用、数字或底线不对），已退回规则打分",
    "inconsistent": "模型三次输出的结论不一致，已退回规则打分，可信度也降了一档",
}
_STATUS = {"kept": "留下", "merged": "合并", "reference": "参考", "dropped": "剔除"}
_SKIP = {"pending": "还在整理", "unavailable": "没有数据", "stale": "数据过期", "invalid": "数据对不上"}


def _pct(value: float) -> str:
    return f"{abs(float(value)) * 100:.1f}%"


def _num(value) -> str:
    text = f"{float(value):.2f}".rstrip("0").rstrip(".")
    return text or "0"


# ---------------------------------------------------------------- 价位
def _how_calc(level: dict) -> list[str]:
    names = [part.strip() for part in str(level.get("base_name") or "").split("+")]
    return [f"{name}：{FORMULA[name]}" for name in names if name in FORMULA]


def _side_logic(side: str, level: dict | None, considered: list[dict], row: str) -> dict:
    title = "上方价" if side == "up" else "下方价"
    rows = [item for item in considered if item["dir"] == side]
    seen = [
        {
            "name": item["name"], "price": round(item["price"], 2),
            "distance": ("+" if item["dir"] == "up" else "-") + _pct(item["dist"]),
            "result": _STATUS[item["status"]], "note": item["note"],
        }
        for item in rows
    ]
    if not level:
        word = "压力（前高、一年高点这类）" if side == "up" else "支撑（均线、成本区、近期低点这类）"
        return {
            "title": title, "price": None, "summary": f"{title}这次没有：现价的{'上' if side == 'up' else '下'}面找不到可以参照的{word}。",
            "how_calc": [], "chosen_because": "", "condition": "", "after_cross": "", "verified": None, "candidates": seen,
        }
    dist = _pct(level["dist"])
    move = "涨" if side == "up" else "跌"
    verified = level.get("verified", True)
    reason = _ORIGIN.get(level.get("origin") or "", "")
    if row in _ROW_WHY and level.get("origin") == "default":
        reason += f"；这次的情况是：{_ROW_WHY[row]}"
    if level.get("why"):
        reason += f"。理由：{level['why']}"
    if not verified:
        reason = "这个方向没有别的价可选，所以给出离范围最近的一个，只当参考"
    buffer = float(level.get("buffer") or 0)
    after = level["consequence"].lstrip("→ ").strip()
    summary = f"{title} {level['price']:.2f} 元（{level['base_name']}），现价还要{move} {dist} 才到。"
    if buffer:
        summary += f"提醒价在基准价下方留了 {buffer * 100:.0f}% 的空间。"
    summary += f"{_CONFIRM.get(level.get('confirm') or 'close', '收盘价穿过')}它才算数。"
    summary += f"穿过之后：{after}。" if verified else "没有通过重算验证，穿过之后会怎样没有算过。"
    return {
        "title": title, "price": level["price"], "summary": summary,
        "how_calc": _how_calc(level), "chosen_because": reason,
        "condition": level["condition"], "after_cross": after, "verified": bool(verified), "candidates": seen,
    }


def price_logic(levels: dict | None) -> dict:
    levels = levels or {}
    considered = levels.get("considered") or []
    row = levels.get("row") or ""
    return {
        "principles": [
            "上方价和下方价都不是「现价加减一个固定比例」，每个价都有固定的算法，见各自的 how_calc。",
            "只看离现价 3% 到 15% 的价：太近会被日内波动反复碰到，太远短期没有意义；相差不到 1% 的合并成一个。",
            "每个价都要验证：假设收盘价穿到它外侧 1%，重算量价和离一年高点的距离，方向、力度或位置变了，这个价才有意义。",
            "价格由代码算，分析员只在固定菜单里选（基准价、留不留空间、怎么确认），不能自己写价格。",
        ],
        "up": _side_logic("up", levels.get("up"), considered, row),
        "down": _side_logic("down", levels.get("down"), considered, row),
    }


# ---------------------------------------------------------------- 判断依据
def evidence(cites: list[str], table: FactTable | None) -> list[str]:
    if table is None:
        return []
    events = {item["id"]: item["text"] for item in table.events}
    rows = []
    for cite in cites or []:
        if cite in events:
            rows.append(events[cite])
            continue
        fact, defn = table.fact(cite), definition(cite)
        if fact is not None and defn is not None:
            rows.append(f"{defn.label}：{fact.text}")
    return rows


def _step_text() -> str:
    steps = policy.SPREAD_STEPS
    parts = []
    for index, (low, level) in enumerate(steps):
        high = steps[index + 1][0] - 1 if index + 1 < len(steps) else None
        span = f"{low}～{high}" if high else f"{low} 以上"
        parts.append(f"{span} 分是「{policy.LEVEL_TEXT[level]}」")
    return "，".join(parts)


def _strength(level: int) -> str:
    return policy.LEVEL_TEXT.get(level or 0, "")


def _conclusion(direction: str, level: int, position: str) -> str:
    label = policy.DIRECTION_TEXT.get(direction, "不明")
    strength = _strength(level)
    return f"{label}" + (f"，{strength}" if strength else "") + f"；位置{policy.POSITION_TEXT.get(position, '适中')}"


def _agent_points(side: str, sides: dict, final: dict, table) -> dict:
    name = "看多" if side == "bull" else "看空"
    adopted = {item["ref"]: item for item in final.get("adopted") or []}
    rejected = {item["ref"]: item for item in final.get("rejected") or []}
    points = []
    for item in (sides.get(side) or {}).get("points") or []:
        got, out = adopted.get(item["ref"]), rejected.get(item["ref"])
        if got:
            lowered = got["weight"] < item["weight"]
            status = f"采信，计 {got['weight']} 分" + (f"（原来自评 {item['weight']} 分，调低：{policy.ADOPT_REASONS.get(got.get('reason'), '')}）" if lowered else "")
        elif out:
            status = f"不采信：{policy.ADOPT_REASONS.get(out['reason'], out['reason'])}"
        else:
            status = "没有被裁决"
        points.append({
            "text": item["claim"], "evidence": evidence(item["cites"], table), "claimed": item["weight"],
            "adopted": bool(got), "counted": got["weight"] if got else 0, "status": status,
        })
    counted = sum(item["counted"] for item in points)
    if points:
        summary = f"{name}方提出 {len(points)} 条，被采信 {sum(1 for item in points if item['adopted'])} 条，合计 {counted} 分。"
    else:
        summary = f"没有找到有力的{name}论据，这本身说明{name}一方证据不足。"
    return {"title": name, "summary": summary, "counted": counted, "points": points}


def _rule_points(side: str, rule: dict, table) -> dict:
    lean = "bullish" if side == "bull" else "bearish"
    name = "看多" if side == "bull" else "看空"
    points = []
    for item in rule.get("per_dim") or []:
        if item["status"] != "ok" or item["lean"] != lean:
            continue
        label = policy.DIMENSION_LABEL.get(item["key"], item["key"])
        facts = []
        if table is not None:
            for fact in table.facts:
                defn = definition(fact.id)
                if defn and defn.dim == item["key"] and not fact.id.endswith(".view"):
                    facts.append(f"{defn.label}：{fact.text}")
        sure = {"high": "把握高", "medium": "把握一般", "low": "把握低"}.get(item.get("certainty"), "")
        points.append({
            "text": f"{label}{name}（力度 {item['level']}，{sure}）" if sure else f"{label}{name}（力度 {item['level']}）",
            "evidence": facts[:3], "claimed": item["level"], "adopted": True,
            "counted": abs(item["contribution"]), "status": f"计 {_num(abs(item['contribution']))} 分",
        })
    counted = round(sum(item["counted"] for item in points), 2)
    if points:
        summary = f"{len(points)} 个方面{name}，合计 {_num(counted)} 分。"
    else:
        summary = f"没有哪个方面{name}。"
    return {"title": name, "summary": summary, "counted": counted, "points": points}


def judgment(
    final: dict | None,
    rule: dict | None,
    sides: dict | None,
    table: FactTable | None,
    fallback_reason: str = "",
    position_text: str = "",
) -> dict:
    rule = rule or {}
    if final and "spread" in final and sides:
        return _agent_judgment(final, sides, table)
    bull, bear = _rule_points("bull", rule, table), _rule_points("bear", rule, table)
    skipped = [
        f"{policy.DIMENSION_LABEL.get(item['key'], item['key'])}（{_SKIP.get(item['status'], '不参与')}）"
        for item in rule.get("per_dim") or [] if item["status"] != "ok"
    ]
    score = float(rule.get("score") or 0)
    direction = rule.get("direction") or "unclear"
    calc = f"看多 {_num(bull['counted'])} 分，减去看空 {_num(bear['counted'])} 分，净得分 {_num(score)}；绝对值不到 {_num(policy.RULE_EDGE)} 分就写「不明」。"
    note = "；".join(part for part in (
        "力度越大分越重，把握低的只算一半",
        ("没有参与打分：" + "、".join(skipped)) if skipped else "",
    ) if part)
    conclusion = _conclusion(direction, rule.get("level") or 0, rule.get("position") or "mid")
    return {
        "source": "rule",
        "summary": f"{bull['summary']}{bear['summary']}{calc}所以这次是：{conclusion}。",
        "bull": bull, "bear": bear,
        "chief": {
            "title": "主分析员",
            "role": _FALLBACK.get(fallback_reason, _FALLBACK[""]),
            "basis": note, "calc": calc, "conclusion": conclusion,
            "position_basis": position_text, "conflict": "", "if_wrong": "", "held": "",
        },
    }


def _agent_judgment(final: dict, sides: dict, table) -> dict:
    bull, bear = _agent_points("bull", sides, final, table), _agent_points("bear", sides, final, table)
    spread = int(final.get("spread") or 0)
    direction, level = final.get("direction"), final.get("level") or 0
    conclusion = _conclusion(direction, level, final.get("position") or "mid")
    calc = (
        f"采信的看多 {bull['counted']} 分，减去采信的看空 {bear['counted']} 分，分差 {spread:+d}。"
        f"分差不到 {policy.SPREAD_STEPS[0][0]} 分写「不明」，{_step_text()}。力度按分差换算，不看语气。"
    )
    note = final.get("conflict_note") or {}
    held = "新的方向还没有连续两次确认（或分差不够大），所以先维持上次的判断。" if final.get("held_by_anti_flip") else ""
    return {
        "source": "agent",
        "summary": f"看多采信 {bull['counted']} 分，看空采信 {bear['counted']} 分，分差 {spread:+d}，所以这次是：{conclusion}。{held}",
        "bull": bull, "bear": bear,
        "chief": {
            "title": "主分析员",
            "role": "不能新编论据，只对看多、看空两方已经提出的论据逐条决定采信与否、最终算几分。",
            "basis": final.get("direction_text", ""),
            "calc": calc, "conclusion": conclusion,
            "position_basis": final.get("position_text", ""),
            "conflict": note.get("text", ""),
            "if_wrong": (final.get("postmortem") or {}).get("text", ""),
            "held": held,
        },
    }
