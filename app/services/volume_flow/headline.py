"""资金流的 headline：AI 体检第 10 题的一句话总结（模型写的），字段和 verdict 一致（见 app/services/verdict_view.py）。"""

from __future__ import annotations

from app.services.verdict_view import summary_headline


def headline(review: dict | None) -> dict | None:
    """体检第 10 题由模型写成时返回；否则 None，前端改用 verdict。模型的结论不改规则的 lean，综合判断仍读规则。
    体检还没出来时（review_state 为 computing），前端可以用 review_state 提示"AI 解读中"。"""
    return summary_headline(review, "q10")
