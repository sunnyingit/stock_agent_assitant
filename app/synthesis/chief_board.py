"""主分析师的两份对外结果：主持卡给流式接口，结论详情给 detail。"""

from __future__ import annotations

from app.synthesis import policy
from app.synthesis.types import FactTable

_CIRCLED = "①②③④⑤⑥⑦⑧⑨"
_REASON = {
    "priced_in": "已反应",
    "stale": "数据过期",
    "low_grade": "来源等级低",
    "duplicate": "重复",
    "weak": "证据不足",
    "contradicted": "被其他事实反驳",
}
_ORDER = (
    ("sector", "板块"),
    ("volume_flow", "资金流"),
    ("news", "新闻"),
    ("finance", "财报"),
    ("fundamental", "基本面"),
)
_PREFIX = {
    "sector": "sector", "sp": "sector",
    "flow": "volume_flow", "fr": "volume_flow", "volume_flow": "volume_flow",
    "news": "news",
    "fin": "finance", "ff": "finance", "finance": "finance",
    "fund": "fundamental", "fd": "fundamental", "fundamental": "fundamental",
}


def card(data: dict) -> dict:
    """主持气泡：裁决、采纳、得分、不过、如果我错了。不含走势和价位详情。"""
    page = _page(data)
    final = _final(data)
    direction = (final or {}).get("direction") or (page.get("direction") or {}).get("lean") or "unclear"
    level = (final or {}).get("level")
    if level is None:
        level = (page.get("direction") or {}).get("level") or 0
    level_text = policy.LEVEL_TEXT.get(level or 0, "")
    label = policy.DIRECTION_TEXT.get(direction, "不明")
    adopted = (final or {}).get("adopted") or []
    rejected = (final or {}).get("rejected") or []
    bull = sum(item.get("weight") or 0 for item in adopted if item.get("side") == "bull")
    bear = sum(item.get("weight") or 0 for item in adopted if item.get("side") == "bear")
    spread = (final or {}).get("spread")
    if spread is None:
        spread = bull - bear
    down = (_levels(data).get("down") or {})
    wrong = ((final or {}).get("postmortem") or {}).get("text") or ""
    price = down.get("price")
    if price is not None and f"{float(price):.2f}" not in wrong:
        wrong = f"{wrong}，并且收盘跌破 {float(price):.2f}" if wrong else f"收盘跌破 {float(price):.2f}"
    note = (final or {}).get("conflict_note") or {}
    however = note.get("text") if isinstance(note, dict) else ""
    if not however:
        however = ((page.get("counter") or {}).get("text") or "")
    score = f"{bull}:{bear}，分差 {spread}"
    if level_text:
        score += f" → {level_text}"
    return {
        "role": "chief",
        "name": "主分析师",
        "side": "主持",
        "kind": "analysis",
        "quote": "",
        "source": "model" if final else "rule",
        "verdict": label + (f" · {level_text}" if level_text else ""),
        "text": (final or {}).get("direction_text") or (page.get("direction") or {}).get("text") or "",
        "adopted": _side_line(adopted),
        "rejected": _rejected_line(rejected),
        "score": {"bull": bull, "bear": bear, "spread": spread, "text": score},
        "however": however or None,
        "if_wrong": {
            "text": wrong,
            "alert": None if price is None else {
                "side": "down", "price": float(price), "condition": down.get("condition") or "",
            },
        },
    }


def detail(data: dict) -> dict:
    """结论详情：五维注意项、两个价，以及走势图需要的均线、价位线和每日判断。"""
    page = _page(data)
    final = _final(data)
    table = FactTable.from_dict(data["table"]) if data.get("table") else FactTable()
    shown = card(data)
    position = page.get("position") or {}
    year = _year_high(table)
    return {
        "source": shown["source"],
        "updated_at": page.get("generated_at") or "",
        "direction": {"verdict": shown["verdict"], "text": shown["text"]},
        "position": {"label": position.get("label") or policy.POSITION_TEXT.get((final or {}).get("position") or "", ""), "text": year or position.get("text") or ""},
        "invalidate": shown["if_wrong"]["text"],
        "basis": _basis(final, table),
        "notices": _notices(final, table),
        "levels": {
            "down": _price(_levels(data).get("down")),
            "up": _price(_levels(data).get("up")),
        },
        "trend": page.get("trend"),
    }


def _page(data: dict) -> dict:
    return data.get("display_agent") or data.get("display_rule") or {}


def _final(data: dict) -> dict | None:
    decision = data.get("decision") or {}
    final = decision.get("final")
    if decision.get("source") == "agent" and isinstance(final, dict):
        return final
    return None


def _levels(data: dict) -> dict:
    return data.get("levels_agent") or data.get("levels_rule") or {}


def _mark(ref: str) -> str:
    number = str(ref or "").rsplit(".", 1)[-1]
    if number.isdigit() and 0 < int(number) <= len(_CIRCLED):
        return _CIRCLED[int(number) - 1]
    return number


def _side_name(item: dict) -> str:
    if item.get("side") == "bull" or str(item.get("ref") or "").startswith("bull"):
        return "看多"
    return "看空"


def _side_line(adopted: list[dict]) -> str:
    parts = []
    for side, name in (("bull", "看多"), ("bear", "看空")):
        marks = "".join(_mark(item.get("ref")) for item in adopted if item.get("side") == side or str(item.get("ref") or "").startswith(side))
        if marks:
            parts.append(name + marks)
    return " · ".join(parts)


def _rejected_line(rejected: list[dict]) -> str:
    parts = []
    for item in rejected:
        reason = _REASON.get(item.get("reason") or "", item.get("reason") or "")
        label = f"{_side_name(item)}{_mark(item.get('ref'))}"
        parts.append(f"{label}（{reason}）" if reason else label)
    return " · ".join(parts)


def _year_high(table: FactTable) -> str:
    fact = table.fact("market.from_year_high")
    if fact is None or not fact.text:
        return ""
    return f"离一年高点还有 {fact.text.lstrip('-')}"


def _basis(final: dict | None, table: FactTable) -> list[dict]:
    headlines = {row.get("dimension"): row for row in table.headlines or []}
    chips, seen = [], set()
    for cite in (final or {}).get("direction_cites") or []:
        dimension = _PREFIX.get(str(cite).split(".", 1)[0])
        if not dimension or dimension in seen:
            continue
        seen.add(dimension)
        headline = ((headlines.get(dimension) or {}).get("headline") or {})
        label = next((name for key, name in _ORDER if key == dimension), dimension)
        chips.append({"dimension": dimension, "label": label, "text": headline.get("text") or cite})
    return chips


def _notices(final: dict | None, table: FactTable) -> list[dict]:
    headlines = {row.get("dimension"): row for row in table.headlines or []}
    picked = {item.get("dimension"): item for item in (final or {}).get("notices") or []}
    rows = []
    for key, label in _ORDER:
        headline = ((headlines.get(key) or {}).get("headline") or {})
        choice = picked.get(key) or {}
        fact = _cited_fact(headline, choice.get("cites") or [])
        rows.append({
            "dimension": key,
            "label": label,
            "tag": choice.get("tag"),
            "text": headline.get("text") or "",
            "value": None if fact is None else fact.get("value"),
            "value_text": "" if fact is None else (fact.get("text") or ""),
        })
    return rows


def _cited_fact(headline: dict, cites: list) -> dict | None:
    facts = [item for item in headline.get("facts") or [] if isinstance(item, dict)]
    by_id = {str(item.get("id")): item for item in facts}
    for cite in cites:
        if str(cite) in by_id:
            return by_id[str(cite)]
    return facts[0] if facts else None


def _price(level: dict | None) -> dict | None:
    if not level or level.get("price") is None:
        return None
    return {
        "price": level.get("price"),
        "dist": level.get("dist"),
        "name": level.get("base_name") or "",
        "why": level.get("why") or "",
        "condition": level.get("condition") or "",
        "consequence": level.get("consequence") or "",
        "verified": level.get("verified", True),
    }


_WEIGHT = {3: "强", 2: "中", 1: "弱"}
_SIDE = {
    "bull": ("看多分析师", "正方"),
    "bear": ("看空分析师", "反方"),
}
_ANALYST = {
    "sector": "板块分析师",
    "volume_flow": "资金流分析师",
    "news": "新闻分析师",
    "finance": "财报分析师",
    "fundamental": "基本面分析师",
}


def side_message(side: str, packed: dict, table: FactTable, final: dict | None = None) -> dict:
    """看多或看空的一条发言。主分析师还没裁决时，每条的 verdict 为空。"""
    name, title = _SIDE[side]
    judged = {}
    for item in (final or {}).get("adopted") or []:
        judged[item.get("ref")] = {**item, "adopt": True}
    for item in (final or {}).get("rejected") or []:
        judged[item.get("ref")] = {**item, "adopt": False}
    points = []
    for point in packed.get("points") or []:
        cites = point.get("cites") or []
        dimension = _main_dimension(cites)
        points.append({
            "id": point.get("ref"),
            "text": point.get("claim") or "",
            "weight": point.get("weight"),
            "analyst": None if dimension is None else {"dimension": dimension, "name": _ANALYST.get(dimension, "")},
            "facts": _cited_rows(table, cites),
            "verdict": _chip(point, judged.get(point.get("ref"))),
        })
    return {
        "role": side,
        "name": name,
        "side": title,
        "kind": "analysis",
        "quote": "",
        "address": "主分析师",
        "source": "rule" if packed.get("failed") else "model",
        "points": points,
        "errors": list(packed.get("errors") or []) if not points else [],
    }


def _main_dimension(cites: list) -> str | None:
    counts: dict[str, int] = {}
    for cite in cites:
        dimension = _PREFIX.get(str(cite).split(".", 1)[0])
        if dimension:
            counts[dimension] = counts.get(dimension, 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)


def _cited_rows(table: FactTable, cites: list) -> list[dict]:
    rows = []
    for cite in cites:
        fact = table.fact(str(cite))
        if fact is None:
            continue
        rows.append({"id": fact.id, "label": fact.id, "text": fact.text, "value": fact.value})
        if len(rows) >= 5:
            break
    return rows


def _chip(point: dict, judged: dict | None) -> dict | None:
    if not judged:
        return None
    claimed = point.get("weight") or 0
    if judged.get("adopt"):
        got = judged.get("weight") or 0
        if got < claimed:
            text = f"采纳 · {_WEIGHT.get(claimed, claimed)}{claimed}→{_WEIGHT.get(got, got)}{got}"
        else:
            text = f"采纳 · {_WEIGHT.get(got, got)}{got}"
        return {"adopt": True, "weight": got, "claimed": claimed, "reason": judged.get("reason") or "", "text": text}
    reason = _REASON.get(judged.get("reason") or "", judged.get("reason") or "")
    return {"adopt": False, "weight": 0, "claimed": claimed, "reason": judged.get("reason") or "", "text": f"未采纳 · {reason}" if reason else "未采纳"}
