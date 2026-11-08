"""看多、看空、主分析师的追问。消息外壳和第一次分析相同，kind 为 reply。"""

from __future__ import annotations

import json

from app.synthesis.chief_board import _SIDE, side_message
from app.synthesis.gateway import Gateway
from app.synthesis.types import FactTable

_SIDE_SYSTEM = """你是{name}（{title}）。用户在追问你。
下面的 points / facts 是参考材料，可自由使用，不必受其格式限制。
直接回答问题即可，可用 markdown。不要给具体买卖建议。
只输出回答正文，不要包 JSON，不要写开场白。"""

_CHIEF_SYSTEM = """你是主分析师。用户在追问你。
下面的主持卡、论据和 facts 是参考材料，可自由使用，不必受其格式限制。
直接回答问题即可，可用 markdown。不要给具体买卖建议。
只输出回答正文，不要包 JSON，不要写开场白。"""


def answer_side(side: str, question: str, previous: dict, gateway: Gateway | None = None, facts: list[dict] | None = None) -> dict:
    """追问看多或看空。previous 是第一次分析的 side_message；没有论据时用 facts 兜底。"""
    name, title = _SIDE[side]
    if not question.strip():
        raise ValueError("缺少问题")
    points = [item for item in (previous.get("points") or []) if isinstance(item, dict) and item.get("id")]
    by_fact = {item["id"]: item for item in (_fact_row(row) for row in (facts or [])) if item}
    for point in points:
        for fact in point.get("facts") or []:
            row = _fact_row(fact)
            if row:
                by_fact.setdefault(row["id"], row)
    user = {
        "question": question.strip(),
        "points": [{"id": item["id"], "text": item.get("text") or "", "weight": item.get("weight"), "facts": item.get("facts") or []} for item in points],
        "facts": [{"id": item["id"], "label": item.get("label") or item["id"], "text": item.get("text") or "", "value": item.get("value")} for item in by_fact.values()],
    }
    gateway = gateway or Gateway.from_config()
    system = _SIDE_SYSTEM.format(name=name, title=title)
    result = gateway.run([(side, system, json.dumps(user, ensure_ascii=False), 0.3)], plain=True)[0]
    if not result.ok:
        raise RuntimeError(result.error or "模型没有返回")
    text = str((result.data or {}).get("text") or "")
    if not text.strip():
        raise RuntimeError("模型没有返回")
    return {
        "role": side,
        "name": name,
        "side": title,
        "kind": "reply",
        "quote": question.strip(),
        "address": "主分析师",
        "source": "model",
        "text": text,
        "points": [],
    }


def answer_chief(question: str, previous: dict | None = None, sides: dict | None = None, gateway: Gateway | None = None, facts: list[dict] | None = None) -> dict:
    """追问主分析师。previous 是第一次分析的主持卡；没有裁决时用 facts 兜底。"""
    if not question.strip():
        raise ValueError("缺少问题")
    previous = previous or {}
    bull = ((sides or {}).get("bull") or {}).get("points") or []
    bear = ((sides or {}).get("bear") or {}).get("points") or []
    arguments = [
        {"ref": item.get("ref") or item.get("id"), "claim": item.get("claim") or item.get("text") or "", "weight": item.get("weight"), "cites": item.get("cites") or []}
        for item in list(bull) + list(bear)
    ]
    by_fact = {item["id"]: item for item in (_fact_row(row) for row in (facts or [])) if item}
    user = {
        "question": question.strip(),
        "card": {
            "verdict": previous.get("verdict"),
            "text": previous.get("text"),
            "adopted": previous.get("adopted"),
            "rejected": previous.get("rejected"),
            "score": previous.get("score"),
            "however": previous.get("however"),
            "if_wrong": previous.get("if_wrong"),
        },
        "arguments": arguments,
        "facts": [{"id": item["id"], "label": item.get("label") or item["id"], "text": item.get("text") or "", "value": item.get("value")} for item in by_fact.values()],
    }
    gateway = gateway or Gateway.from_config()
    result = gateway.run([("chief", _CHIEF_SYSTEM, json.dumps(user, ensure_ascii=False), 0.3)], plain=True)[0]
    if not result.ok:
        raise RuntimeError(result.error or "模型没有返回")
    text = str((result.data or {}).get("text") or "")
    if not text.strip():
        raise RuntimeError("模型没有返回")
    shell = {
        "role": "chief",
        "name": "主分析师",
        "side": "主持",
        "verdict": previous.get("verdict") or "",
        "adopted": previous.get("adopted"),
        "rejected": previous.get("rejected"),
        "score": previous.get("score"),
        "however": previous.get("however"),
        "if_wrong": previous.get("if_wrong"),
    }
    return {
        **shell,
        **{key: value for key, value in previous.items() if key not in ("kind", "quote", "text", "source", "points")},
        "kind": "reply",
        "quote": question.strip(),
        "text": text,
        "source": "model",
        "points": [],
    }


def previous_side_from_packed(side: str, packed: dict, table: FactTable, final: dict | None = None) -> dict:
    return side_message(side, packed, table, final)


def table_facts(table: FactTable | None) -> list[dict]:
    if table is None:
        return []
    return [{"id": item.id, "label": item.id, "text": item.text, "value": item.value} for item in table.facts]


def _fact_row(item: dict | None) -> dict | None:
    if not isinstance(item, dict) or not item.get("id"):
        return None
    return {
        "id": str(item["id"]),
        "label": item.get("label") or str(item["id"]),
        "text": item.get("text") or "",
        "value": item.get("value"),
    }
