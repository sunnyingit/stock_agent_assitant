"""关键价位：各维度提出候选 → Agent 在固定菜单里选 → 代码算数字并验证后果。

模型不碰价格：数字没有完整行情就无法核对，也无法回测。
"""

from app.synthesis import policy
from app.synthesis.levels.candidates import fallback_rows, raw_candidates, screen, volatility_row
from app.synthesis.levels.defaults import reference_row
from app.synthesis.levels.evaluate import Evaluator
from app.synthesis.levels.resolve import resolve_side, verify_candidates

__all__ = [
    "Evaluator",
    "build_candidates",
    "reference_row",
    "resolve_levels",
]


def build_candidates(aligned: dict | None, snaps, evaluator: Evaluator) -> list[dict]:
    """候选价位：算出来、筛选合并、验证后果。

    某个方向没有任何候选通过验证时，补上离范围最近的候选（verified=False，没有后果行）；
    历史收盘价里这一侧一个价都没有时，用波动带兜底。保证上下价位都不为空。
    """
    by_key = {snap.key: snap for snap in snaps}
    flow = by_key.get("volume_flow")
    news = by_key.get("news")
    pattern = flow.facts.get("flow.pattern") if flow is not None and flow.done else None
    news_event = None
    if news is not None and news.done:
        news_event = {
            "impact": news.flags.get("main_impact"),
            "published": news.flags.get("main_published"),
            "session": news.flags.get("main_session"),
        }
    raw = raw_candidates(aligned or {}, pattern, news_event)
    screened = screen(raw)
    kept = verify_candidates(screened, evaluator)
    extra = []
    for side in ("down", "up"):
        if any(item["dir"] == side for item in kept):
            continue
        picked = fallback_rows(raw, side)
        if not picked:   # 历史收盘价里这一侧一个价都没有（创了新低或新高）：用波动带兜底，保证不为空
            band = volatility_row(aligned, side)
            if band is not None:
                picked = [band]
                raw = raw + [{k: band[k] for k in ("id", "name", "dir", "price", "dist", "priority")}]
        extra.extend(picked)
    evaluator.considered = _considered(raw, screened, kept, extra)
    return kept + extra


def _considered(raw: list[dict], screened: list[dict], kept: list[dict], extra: list[dict]) -> list[dict]:
    """每个候选价最后怎么样了：留下、只作参考，还是被剔除以及为什么。"""
    merged_into = {}
    for item in screened + extra:
        for other in item.get("merged_ids") or []:
            if other != item["id"]:
                merged_into[other] = item["name"]
    kept_ids = {item["id"]: item for item in kept}
    extra_ids = {item["id"] for item in extra}
    passed = {item["id"] for item in screened}
    rows = []
    for item in sorted(raw, key=lambda row: (row["dir"], row["priority"])):
        dist = abs(item["dist"])
        if item["id"] in kept_ids:
            status, note = "kept", f"留下：穿过它以后，{kept_ids[item['id']]['consequence']}"
        elif item["id"] in merged_into:
            status, note = "merged", f"和「{merged_into[item['id']]}」相差不到 1%，合并成一个"
        elif dist < policy.DIST_MIN:
            status, note = "dropped", "离现价太近（不到 3%），日内波动就会反复碰到"
        elif dist > policy.DIST_MAX:
            status, note = "dropped", "离现价太远（超过 15%），短期没有意义"
        elif item["id"] in passed:
            status, note = "dropped", "穿过它以后，规则版的结论没有变化"
        else:
            status, note = "dropped", "没有留下"
        if item["id"] in extra_ids:
            status, note = "reference", "没有通过验证，但这个方向没有别的价可选，只作参考"
        rows.append({
            "id": item["id"], "name": item["name"], "dir": item["dir"], "price": item["price"],
            "dist": item["dist"], "status": status, "note": note,
        })
    return rows


def resolve_levels(selection: dict | None, candidates: list[dict], current: float | None, evaluator: Evaluator, snaps) -> dict:
    """selection 形如 {"down": {...}, "up": {...}}，来自 Agent；为空就用参考表。"""
    row = reference_row(snaps)
    result = {"row": row, "down": None, "up": None, "steps": [], "considered": getattr(evaluator, "considered", [])}
    if not current:
        return result
    selection = selection or {}
    for side in ("down", "up"):
        picked, steps = resolve_side(side, selection.get(side), candidates, current, evaluator, row)
        result[side] = picked
        result["steps"].extend(steps)
    return result
