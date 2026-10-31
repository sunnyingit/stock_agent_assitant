"""第三层：按选项算价格、检查距离、代入重算验证后果；不通过就依次退让并记录每一步。"""

from __future__ import annotations

import math

from app.synthesis import policy
from app.synthesis.levels import defaults
from app.synthesis.levels.evaluate import MEANING_TEXT, Evaluator
from app.synthesis.levels.render import condition_line, consequence_line, price_line


def tick(price: float) -> float:
    return round(math.floor(price / policy.PRICE_TICK + 0.5) * policy.PRICE_TICK, 2)


def _in_range(dist: float) -> bool:
    return policy.DIST_MIN <= abs(dist) <= policy.DIST_MAX


def verify_candidates(rows: list[dict], evaluator: Evaluator) -> list[dict]:
    """第一层：各候选先按默认确认条件做后果验证，验证不通过的不进入候选列表。"""
    kept = []
    for row in rows:
        result = evaluator.evaluate(row["price"], row["dir"], "close")
        if not result["changed"]:
            continue
        row = dict(row)
        row["consequence"] = result["text"] or "结论会变化"
        row["verified"] = True
        kept.append(row)
    return kept


def _try_down(candidate: dict, current: float, buffer: float, confirm: str, evaluator: Evaluator, steps: list):
    buffers = [value for value in sorted(policy.DOWN_BUFFERS, reverse=True) if value <= buffer]
    confirms = [confirm] if confirm == "close" else [confirm, "close"]
    for use_confirm in confirms:
        for use_buffer in buffers:
            price = tick(candidate["price"] * (1 - use_buffer))
            dist = price / current - 1
            if not _in_range(dist):
                steps.append(f"{candidate['id']} 缓冲 {use_buffer:.0%}：距离 {dist:+.1%} 不在范围内")
                continue
            result = evaluator.evaluate(price, "down", use_confirm)
            if result["changed"]:
                return price, dist, use_buffer, use_confirm, result
            steps.append(f"{candidate['id']} 缓冲 {use_buffer:.0%}、{use_confirm}：重算后结论没变")
    return None


def _try_up(candidate: dict, current: float, confirm: str, meaning: str, evaluator: Evaluator, steps: list):
    for use_confirm in dict.fromkeys([confirm, "close"]):
        price = tick(candidate["price"])
        dist = price / current - 1
        if not _in_range(dist):
            steps.append(f"{candidate['id']}：距离 {dist:+.1%} 不在范围内")
            return None
        result = evaluator.evaluate(price, "up", use_confirm)
        if not result["changed"]:
            steps.append(f"{candidate['id']}、{use_confirm}：重算后结论没变")
            continue
        if meaning == "position_high" and result["to"] and result["to"][2] != "high":
            steps.append(f"{candidate['id']}：突破后位置没有变成偏高，含义不成立")
            continue
        return price, dist, use_confirm, result
    return None


def resolve_side(side: str, selection: dict | None, candidates: list[dict], current: float, evaluator: Evaluator, row: str):
    """返回 (结果或 None, 退让记录)。选项来自 Agent 或参考表；Agent 的选项不通过时，先试参考表，再试其余候选。"""
    steps: list[str] = []
    pool = [item for item in candidates if item["dir"] == side]
    by_id = {item["id"]: item for item in pool}
    attempts = []
    if selection:
        attempts.append((selection, "agent"))
    fallback = defaults.default_selection(row, candidates).get(side)
    if fallback and fallback != selection:
        attempts.append((fallback, "default"))
    for item in pool:
        if side == "down":
            attempts.append(({"base": item["id"], "buffer": 0.0, "confirm": "close", "why": "", "cites": []}, "scan"))
        else:
            meaning = "position_high" if "位置" in str(item.get("consequence") or "") else "stronger"
            attempts.append(({"base": item["id"], "confirm": "close", "meaning": meaning, "why": "", "cites": []}, "scan"))
    for choice, origin in attempts:
        candidate = by_id.get(choice.get("base"))
        if candidate is None:
            continue
        if candidate.get("verified") is False:
            steps.append(f"{candidate['id']}：没有通过后果验证，因为这个方向没有别的候选，按离范围最近给出")
            return _unverified(side, candidate, choice, current, origin), steps
        if side == "down":
            outcome = _try_down(candidate, current, float(choice.get("buffer") or 0.0), choice.get("confirm") or "close", evaluator, steps)
            if outcome is None:
                continue
            price, dist, buffer, confirm, result = outcome
            return {
                "side": side, "base": candidate["id"], "base_name": candidate["name"], "buffer": buffer,
                "confirm": confirm, "price": price, "dist": round(dist, 4), "origin": origin, "verified": True,
                "line": price_line(side, price, dist),
                "condition": condition_line(side, confirm, candidate["name"], buffer),
                "consequence": consequence_line(result["text"]),
                "why": choice.get("why") or "", "cites": choice.get("cites") or [],
            }, steps
        meaning = choice.get("meaning") or "stronger"
        outcome = _try_up(candidate, current, choice.get("confirm") or "close", meaning, evaluator, steps)
        if outcome is None:
            continue
        price, dist, confirm, result = outcome
        return {
            "side": side, "base": candidate["id"], "base_name": candidate["name"], "buffer": 0.0,
            "confirm": confirm, "meaning": meaning, "price": price, "dist": round(dist, 4), "origin": origin,
            "verified": True, "line": price_line(side, price, dist),
            "condition": condition_line(side, confirm, candidate["name"], 0.0),
            "consequence": consequence_line(MEANING_TEXT.get(meaning, "")),
            "why": choice.get("why") or "", "cites": choice.get("cites") or [],
        }, steps
    return None, steps


def _unverified(side: str, candidate: dict, choice: dict, current: float, origin: str) -> dict:
    """没有验证过后果的价位：不留缓冲、收盘即可，后果行只说"需要重新评估"，不编造结论变化。"""
    price = tick(candidate["price"])
    dist = price / current - 1
    return {
        "side": side, "base": candidate["id"], "base_name": candidate["name"], "buffer": 0.0,
        "confirm": "close", "price": price, "dist": round(dist, 4), "origin": origin, "verified": False,
        "line": price_line(side, price, dist),
        "condition": condition_line(side, "close", candidate["name"], 0.0),
        "consequence": consequence_line("跌破后，这轮走势需要重新评估" if side == "down" else "突破后，这轮走势需要重新评估"),
        "why": "", "cites": [],
    }
