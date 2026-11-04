"""主分析师看到的五份 headline。顺序固定：板块、资金流、新闻、财报、基本面。"""

from __future__ import annotations

ORDER = (
    ("sector", "板块", "板块分析师", "sector"),
    ("volume_flow", "资金流", "资金流分析师", "flow"),
    ("news", "新闻", "新闻分析师", None),
    ("finance", "财报", "财报分析师", "fin"),
    ("fundamental", "基本面", "基本面分析师", "fund"),
)


def dimension_headlines(payloads: dict | None, ts_code: str, store=None) -> list[dict]:
    """每位分析师一条。还没有核对过的结论时 headline 为 null。"""
    payloads = payloads or {}
    rows = []
    for key, label, name, kind in ORDER:
        payload = payloads.get(key)
        headline = _from_payload(payload)
        if headline is None and kind:
            headline = _from_review(key, payload, _review(store, ts_code, kind))
        rows.append({
            "dimension": key,
            "label": label,
            "name": name,
            "headline": _slim(headline),
        })
    return rows


def _from_payload(payload) -> dict | None:
    headline = (payload or {}).get("headline") if isinstance(payload, dict) else None
    if isinstance(headline, dict) and str(headline.get("text") or "").strip():
        return headline
    return None


def _review(store, ts_code: str, kind: str):
    if store is None:
        return None
    from app.reviews.attach import latest_key

    entry = store.get(latest_key(ts_code, kind))
    result = (entry or {}).get("result") if isinstance(entry, dict) else None
    return result if isinstance(result, dict) else None


def _from_review(key: str, payload, review) -> dict | None:
    if not review:
        return None
    if key == "volume_flow":
        from app.services.volume_flow.headline import headline

        return headline(review)
    if key == "sector":
        from app.sector_review.review import headline

        return headline(review)
    if key == "finance":
        from app.finance_review.review import headline

        days = (((payload or {}).get("meta") or {}).get("report") or {}).get("days_since")
        return headline(review, days)
    if key == "fundamental":
        raw = review.get("headline")
        if isinstance(raw, dict) and str(raw.get("text") or "").strip():
            return raw
    return None


def _slim(headline) -> dict | None:
    if not isinstance(headline, dict) or not str(headline.get("text") or "").strip():
        return None
    facts = []
    for fact in headline.get("facts") or []:
        if not isinstance(fact, dict) or not fact.get("id"):
            continue
        facts.append({
            "id": fact["id"],
            "label": fact.get("label") or fact["id"],
            "text": fact.get("text") or "",
            "value": fact.get("value"),
        })
        if len(facts) >= 5:
            break
    return {
        "source": headline.get("source") or "",
        "status": headline.get("status") or "",
        "status_text": headline.get("status_text") or "",
        "text": str(headline.get("text") or "").strip(),
        "facts": facts,
    }
