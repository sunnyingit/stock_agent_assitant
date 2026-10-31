"""结论那一句：按已经算好的数字用模板拼。"""

from __future__ import annotations

def write_text(facts: dict) -> str:
    """结论那一句，只用模板，不调用模型。AI 解读在 app/fundamental_review。"""
    return _template(facts)


def _template(facts: dict) -> str:
    metric = facts.get("speak") or facts.get("metric_text") or "估值"
    label = facts.get("history_label") or "近 5 年"
    pctile = facts.get("pctile_5y")
    value = facts.get("value_text")
    if pctile is None or not value:
        head = f"{metric}缺少可比历史"
    else:
        head = f"{metric} {value} 倍，在{label} {int(round(float(pctile) * 100))}% 分位"
        gap = facts.get("gap_text")
        if gap:
            head += f"，{gap}"
    support = facts.get("support") or ""
    counter = facts.get("counter") or "这只是历史和同行中的位置"
    rich = f"{head}，{support}" if support else head
    text = _fit(rich, counter)
    if len(text) <= 90:
        return text
    return _fit(head, counter)


def _fit(head: str, counter: str) -> str:
    parts = [item for item in counter.split("，") if item]
    while parts:
        text = f"{head}。不过{'，'.join(parts)}。"
        if len(text) <= 90:
            return text
        if len(parts) == 1:
            break
        parts.pop()
    text = f"{head}。不过{parts[0] if parts else counter}。"
    if len(text) <= 90:
        return text
    return text[:89] + "。"
