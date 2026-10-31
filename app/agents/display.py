"""把主 Agent 的总结收成给用户看的结论。新闻和财报共用这一层。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Callable

_SCORE = {1: 0.4, 2: 0.7, 3: 0.9}
_CONFIDENCE = {"高": "high", "一般": "medium", "低": "low"}
_HEADLINE = re.compile(r"^.{2,8}偏[多空]：")


def lean(value: str, bull: dict, bear: dict) -> str:
    text = str(value or "")
    if "偏多" in text or text in {"多", "看多", "bullish"}:
        return "偏多"
    if "偏空" in text or text in {"空", "看空", "bearish"}:
        return "偏空"
    bull_points = len((bull or {}).get("points") or [])
    bear_points = len((bear or {}).get("points") or [])
    if bull_points > bear_points:
        return "偏多"
    return "偏空"


def level(value) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = 1
    return min(3, max(1, number))


def certainty(value: str) -> str:
    text = str(value or "")
    if text in {"高", "一般", "低"}:
        return text
    return "低"


def headline(side: str, text: str, label: str) -> str:
    prefix = f"{label}偏多：" if side == "偏多" else f"{label}偏空："
    body = str(text or "").strip()
    matched = _HEADLINE.match(body)
    if matched:
        body = body[matched.end():]
    return (prefix + body)[:25]


def cap_points(items: list[dict]) -> list[dict]:
    cleaned = []
    for item in items or []:
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            continue
        row = dict(item)
        row["lean"] = lean(item.get("lean"), {}, {})
        row["title"] = str(item.get("title") or "").strip()
        cleaned.append(row)
    if len(cleaned) <= 3:
        return cleaned
    picked = cleaned[:3]
    leans = {row["lean"] for row in cleaned}
    if len(leans) > 1 and len({row["lean"] for row in picked}) == 1:
        opposing = next(row for row in cleaned if row["lean"] != picked[0]["lean"])
        picked[-1] = opposing
    return picked


def watch(items: list, side: str, empty_watch: Callable[[str], dict]) -> list[dict]:
    cleaned = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        what = str(item.get("what") or "").strip()
        if not what:
            continue
        cleaned.append({
            "what": what,
            "when": str(item.get("when") or "").strip(),
            "if_better": str(item.get("if_better") or "").strip(),
            "if_worse": str(item.get("if_worse") or "").strip(),
        })
    if cleaned:
        return cleaned[:3]
    fallback = empty_watch(side)
    return [{
        "what": str(fallback.get("what") or "").strip(),
        "when": str(fallback.get("when") or "").strip(),
        "if_better": str(fallback.get("if_better") or "").strip(),
        "if_worse": str(fallback.get("if_worse") or "").strip(),
    }]


def finish_display(
    summary: dict,
    bull: dict,
    bear: dict,
    change: str,
    *,
    label: str,
    fine_print: str,
    points_key: str,
    points: list[dict],
    more: dict,
    missing_change: str,
    unmatched_change: str,
    empty_watch: Callable[[str], dict],
    why_limit: int = 80,
) -> dict:
    verdict = summary.get("verdict") if isinstance(summary.get("verdict"), dict) else {}
    today = summary.get("today_move") if isinstance(summary.get("today_move"), dict) else {}
    side = lean(verdict.get("lean"), bull, bear)
    explain = str(today.get("explain") or "").strip()
    if not explain:
        explain = missing_change if not change else unmatched_change
    return {
        "verdict": {
            "lean": side,
            "lean_level": level(verdict.get("lean_level")),
            "headline": headline(side, verdict.get("headline"), label),
            "why": str(verdict.get("why") or "").strip()[:why_limit],
            "certainty": certainty(verdict.get("certainty")),
            "certainty_note": str(verdict.get("certainty_note") or "").strip(),
        },
        "today_move": {
            "change": change,
            "explain": explain,
        },
        points_key: cap_points(points),
        "watch_next": watch(summary.get("watch_next") or [], side, empty_watch),
        "more": more,
        "fine_print": fine_print,
    }


def internal_verdict(verdict: dict) -> dict:
    side = verdict.get("lean")
    magnitude = _SCORE.get(verdict.get("lean_level") or 1, 0.4)
    return {
        "signal": "bullish" if side == "偏多" else "bearish",
        "score": magnitude if side == "偏多" else -magnitude,
        "confidence": _CONFIDENCE.get(verdict.get("certainty"), "low"),
    }


def disputes(bull: dict, bear: dict) -> list[dict]:
    bull_claims: dict[str, list[str]] = {}
    for point in (bull or {}).get("points") or []:
        claim = point.get("claim") or ""
        for fact_id in point.get("fact_ids") or []:
            bull_claims.setdefault(fact_id, []).append(claim)
    rows = []
    for point in (bear or {}).get("points") or []:
        claim = point.get("claim") or ""
        for fact_id in point.get("fact_ids") or []:
            if fact_id in bull_claims:
                rows.append({
                    "fact_id": fact_id,
                    "bull": [item for item in bull_claims[fact_id] if item],
                    "bear": claim,
                })
    return rows


def debate_result(display: dict, facts: list, bull: dict, bear: dict, audit: dict) -> dict:
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "display": display,
        "internal": {
            "events": facts,
            "disputes": disputes(bull, bear),
            "bull": bull,
            "bear": bear,
            "verdict": internal_verdict(display["verdict"]),
            "delta": dict(display["today_move"]),
            "audit": audit,
        },
    }
