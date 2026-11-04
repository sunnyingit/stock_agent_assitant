"""五位维度分析师的追问。消息外壳和第一次分析相同，kind 用来区分。"""

from __future__ import annotations

import json

from app.synthesis.gateway import Gateway

_REPLY_SYSTEM = """你是{name}。用户在追问你。
下面的 headline / facts 是参考材料，可自由使用，不必受其格式限制。
直接回答问题即可，可用 markdown。不要给具体买卖建议。
只输出回答正文，不要包 JSON，不要写开场白。"""


def analysis_message(dimension: str, name: str, headline: dict | None) -> dict:
    text = (headline or {}).get("text") or ""
    return {
        "role": "analyst",
        "dimension": dimension,
        "name": name,
        "kind": "analysis",
        "quote": "",
        "text": text,
        "headline": headline,
    }


def reply_message(dimension: str, name: str, question: str, headline: dict | None, data: dict) -> dict:
    """追问的发言。headline 仍是上一轮那份（没有就给空壳），text 是这一次的回答。"""
    return {
        "role": "analyst",
        "dimension": dimension,
        "name": name,
        "kind": "reply",
        "quote": question.strip(),
        "text": str(data.get("text") or ""),
        "headline": headline or {"text": "", "facts": [], "status": "neutral", "status_text": ""},
    }


def answer(name: str, question: str, headline: dict | None, gateway: Gateway | None = None, facts: list[dict] | None = None) -> dict:
    headline = headline if isinstance(headline, dict) else {}
    rows = [item for item in (facts if facts is not None else headline.get("facts") or []) if isinstance(item, dict) and item.get("id")]
    if not question.strip():
        raise ValueError("缺少问题")
    gateway = gateway or Gateway.from_config()
    user = {
        "question": question.strip(),
        "headline": {"text": headline.get("text") or "", "status_text": headline.get("status_text") or ""},
        "facts": [{"id": item["id"], "label": item.get("label") or item["id"], "text": item.get("text") or "", "value": item.get("value")} for item in rows],
    }
    result = gateway.run([(name, _REPLY_SYSTEM.format(name=name), json.dumps(user, ensure_ascii=False), 0.3)], plain=True)[0]
    if not result.ok:
        raise RuntimeError(result.error or "模型没有返回")
    text = str((result.data or {}).get("text") or "")
    if not text.strip():
        raise RuntimeError("模型没有返回")
    return {"text": text}
