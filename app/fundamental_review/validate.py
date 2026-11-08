"""公司画像的校验。画像来自模型已有知识，代码核对不了真假，只做格式检查：数量、长度、取值、禁用词、不含数字、去重。
"""

from __future__ import annotations

import re

from app.fundamental_review.prompt import BANNED, PROFILE_NOTE

RELATIONS = ("客户", "供应商", "合作伙伴")
_DIGIT = re.compile(r"\d|[零一二三四五六七八九十百千万亿]+(?:元|家|%|成|倍|年|月|日)|百分")
LIMIT = 5


def _clean(value, limit: int) -> str | None:
    text = str(value or "").strip()
    if not text or len(text) > limit or _DIGIT.search(text) or any(word in text for word in BANNED):
        return None
    return text


def profile(raw, stock_name: str = "") -> tuple[dict | None, list[str]]:
    """返回（画像或 None, 被丢弃的原因）。能留的条目留下，不合格的单条丢掉，整块都空就是 None。"""
    data = raw.get("profile") if isinstance(raw, dict) else None
    if not isinstance(data, dict):
        return None, []
    errors = []
    partners, seen = [], set()
    for row in data.get("partners") or []:
        if len(partners) >= LIMIT:
            break
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        relation = str(row.get("relation") or "").strip()
        note = _clean(row.get("note"), 20) if row.get("note") else ""
        if not name or len(name) > 20 or any(ch.isdigit() for ch in name) or name in seen or (stock_name and name == stock_name):
            errors.append(f"画像:合作商 {name!r} 不合格")
            continue
        if relation not in RELATIONS or note is None:
            errors.append(f"画像:合作商 {name!r} 的关系或说明不合格")
            continue
        seen.add(name)
        partners.append({"name": name, "relation": relation, "note": note})
    up, down = _list(data.get("drivers_up"), errors, "催化因素"), _list(data.get("drivers_down"), errors, "利空因素")
    link = _clean(data.get("link"), 60) or ""
    if data.get("link") and not link:
        errors.append("画像:业务关联不合格")
    if not (partners or up or down or link):
        return None, errors
    return {
        "partners": partners, "drivers_up": up, "drivers_down": down, "link": link,
        "source": "model", "note": PROFILE_NOTE,
    }, errors


def _list(rows, errors: list[str], what: str) -> list[str]:
    out = []
    for value in rows or []:
        if len(out) >= LIMIT:
            break
        text = _clean(value, 30)
        if text is None:
            errors.append(f"画像:{what}不合格 {str(value)[:20]!r}")
        elif text not in out:
            out.append(text)
    return out
