"""资金流的 headline：体检第 10 题一句话 + 点名的关键事实。逐项是规则时也可作分析师结论。"""

from __future__ import annotations

from app.services.verdict_view import summary_headline


def headline(review: dict | None, *, allow_template: bool = False) -> dict | None:
    """第 10 题有合格答案时返回；否则 None，前端改用 verdict。
    facts 按 cited_fact_ids 从事实表原样抄出。"""
    shown = summary_headline(review, "q10", allow_template=allow_template)
    if shown is None:
        return None
    catalog = {row["id"]: row for row in (review or {}).get("facts") or [] if isinstance(row, dict) and row.get("id")}
    facts = []
    for fact_id in (review or {}).get("cited_fact_ids") or []:
        row = catalog.get(str(fact_id))
        if row is None:
            continue
        facts.append({
            "id": row["id"],
            "label": row.get("label") or row["id"],
            "text": row.get("text") or "",
            "value": row.get("value"),
        })
    return {**shown, "facts": facts}
