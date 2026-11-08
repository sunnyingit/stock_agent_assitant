"""各维度详情页顶部结论的统一字段：verdict（规则）和 headline（模型）都是 source / status / status_text / text。
headline 有数据就用它；为 None 时前端改用 verdict。lean / level / certainty 只给综合判断读，对外响应里去掉。"""

from __future__ import annotations

STATUS_OF_LEAN = {
    "bullish": ("support", "偏多"),
    "bearish": ("warn", "偏空"),
    "unclear": ("neutral", "不明"),
    "neutral": ("neutral", "不明"),   # 基本面用 neutral 表示方向不明
}
STATUS_TEXT = {status: text for status, text in STATUS_OF_LEAN.values()}
STALE_TEXT = "数据过期"
VERDICT_FIELDS = ("source", "status", "status_text", "text")


def rule_fields(lean: str, text: str) -> dict:
    """规则结论的四个字段。lean 为空表示数据过期。"""
    status, status_text = STATUS_OF_LEAN.get(lean or "", ("neutral", STALE_TEXT))
    return {"source": "rule", "status": status, "status_text": status_text, "text": text}


def model_fields(status: str, text: str) -> dict | None:
    """模型结论的四个字段。status 不在 support/warn/neutral 里，或没有文字时返回 None。"""
    if status not in STATUS_TEXT or not text:
        return None
    return {"source": "model", "status": status, "status_text": STATUS_TEXT[status], "text": text}


def slim(verdict: dict) -> dict:
    return {key: verdict[key] for key in VERDICT_FIELDS}


def summary_headline(review: dict | None, key: str, *, allow_template: bool = False) -> dict | None:
    """体检里"一句话总结"那一题。默认只要模型写的；allow_template 时规则模板答案也可作 headline（资金流分析师结论）。"""
    item = next((row for row in (review or {}).get("items") or [] if row.get("key") == key), None)
    if not item:
        return None
    if item.get("source") == "model":
        return model_fields(item.get("status"), item.get("answer"))
    if allow_template and item.get("source") == "template" and item.get("status") in STATUS_TEXT and item.get("answer"):
        return {
            "source": "rule",
            "status": item["status"],
            "status_text": STATUS_TEXT[item["status"]],
            "text": item["answer"],
        }
    return None
