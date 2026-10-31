"""规则版综合：纯函数。它是安全网，也是对照基准；价位的"代入重算"也复用它。"""

from __future__ import annotations

from app.synthesis import policy
from app.synthesis.types import Snapshot

_SIGN = {"bullish": 1, "bearish": -1}


def position_of(from_year_high) -> str:
    """位置只看收盘价距一年高点：这样"位置变偏高价"可以由阈值直接反推。"""
    if from_year_high is None:
        return "mid"
    if from_year_high >= -policy.POSITION_HIGH_FROM_HIGH:
        return "high"
    if from_year_high <= -policy.POSITION_LOW_FROM_HIGH:
        return "low"
    return "mid"


def synthesize(snaps: list[Snapshot], market: dict, fresh: bool = True) -> dict:
    total = len(policy.DIMENSIONS)
    done = [snap for snap in snaps if snap.done]
    score = 0.0
    per_dim = []
    for snap in snaps:
        contribution = 0.0
        if snap.done and snap.lean in _SIGN and snap.level > 0:
            contribution = (
                _SIGN[snap.lean]
                * snap.level
                * policy.CERTAINTY_WEIGHT.get(snap.certainty, 0.5)
                * policy.DIMENSION_WEIGHT.get(snap.key, 1.0)
                * snap.flags.get("time_weight", 1.0)
            )
        score += contribution
        per_dim.append({
            "key": snap.key,
            "status": snap.status,
            "lean": snap.lean if snap.done else "unclear",
            "level": snap.level if snap.done else 0,
            "certainty": snap.certainty if snap.done else None,
            "as_of": snap.as_of,
            "contribution": round(contribution, 3),
            "time_weight": snap.flags.get("time_weight", 1.0),
        })
    score = round(score, 3)
    magnitude = abs(score)
    if not done or magnitude < policy.RULE_EDGE:
        direction, level = "unclear", 0
    else:
        direction = "bullish" if score > 0 else "bearish"
        level = sum(1 for step in policy.RULE_STRENGTH_STEPS if magnitude >= step)
        if len(done) * 2 < total:
            level = min(level, policy.LOW_COVERAGE_CAP)
    red_line = any(snap.done and snap.flags.get("red_line") for snap in snaps)
    red_capped = False
    if direction == "bullish" and red_line:
        direction, level, red_capped = "unclear", 0, True
    counts = {"bullish": 0, "bearish": 0, "neutral": 0}
    for snap in done:
        counts[snap.lean if snap.lean in _SIGN else "neutral"] += 1
    lead = counts["bullish"] - counts["bearish"]
    majority = "bullish" if lead > 0 else "bearish" if lead < 0 else "unclear"
    return {
        "direction": direction,
        "level": level,
        "position": position_of(market.get("market.from_year_high")),
        "score": score,
        "done": len(done),
        "total": total,
        "fresh": bool(fresh and done),
        "counts": counts,
        "majority": majority,
        "red_line": red_line,
        "red_capped": red_capped,
        "per_dim": per_dim,
        "divergence": divergence(snaps),
    }


def divergence(snaps: list[Snapshot]) -> list[dict]:
    """财报和资金流（或新闻）方向相反：输出分歧标记，并列出双方各自最关键的事实编号。分歧本身往往比结论更有信息量。
    时效权重为 0 的财报（太旧，只作背景）不算分歧。"""
    by_key = {snap.key: snap for snap in snaps}
    out = []
    for left_key, right_key in policy.DIVERGENCE_PAIRS:
        left, right = by_key.get(left_key), by_key.get(right_key)
        if not left or not right or not (left.done and right.done):
            continue
        if left.lean not in _SIGN or right.lean not in _SIGN or left.lean == right.lean or left.level <= 0 or right.level <= 0:
            continue
        if left.flags.get("time_weight", 1.0) <= 0:
            continue
        out.append({
            "pair": [left_key, right_key],
            "leans": {left_key: left.lean, right_key: right.lean},
            "facts": {
                key: [fact for fact in policy.KEY_FACTS.get(key, ()) if fact in by_key[key].facts]
                for key in (left_key, right_key)
            },
        })
    return out


def credibility(rule: dict, inconsistent: bool = False) -> dict:
    """可信度：完成几个、分母多少，再按新鲜度、红线、模型不一致降档。"""
    total = rule.get("total") or 1
    done = rule.get("done") or 0
    ratio = done / total
    score = 2 if ratio >= policy.CREDIBILITY_HIGH_RATIO else 1 if ratio >= policy.CREDIBILITY_MID_RATIO else 0
    score -= 0 if rule.get("fresh") else 1
    score -= 1 if rule.get("red_line") else 0
    score -= 1 if inconsistent else 0
    score = max(0, min(2, score))
    return {
        "done": done,
        "total": total,
        "fresh": bool(rule.get("fresh")),
        "level": ("low", "medium", "high")[score],
        "level_text": ("低", "一般", "高")[score],
    }
