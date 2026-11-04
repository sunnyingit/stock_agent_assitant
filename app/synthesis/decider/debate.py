"""正方、反方各说一轮：并行调用、互相看不到，也看不到规则底线。"""

from __future__ import annotations

from app.synthesis import policy
from app.synthesis.decider import prompts
from app.synthesis.decider.validate import validate_risk, validate_side
from app.synthesis.gateway import Gateway
from app.synthesis.types import FactTable

SIDES = (("bull", prompts.BULL_SYSTEM), ("bear", prompts.BEAR_SYSTEM))


def cache_key(table: FactTable, model_tag: str) -> str:
    return f"sides:{table.sides_digest()}:{prompts.PROMPT_VERSION}:{model_tag}"


def side_cache_key(table: FactTable, side: str, model_tag: str) -> str:
    return f"side:{side}:{table.sides_digest()}:{prompts.PROMPT_VERSION}:{model_tag}"


def _pack(side: str, result, table: FactTable) -> dict:
    if not result.ok:
        return {"points": [], "errors": [f"调用:{result.error}"], "raw": None, "failed": True, "risk": None}
    points, errors = validate_side(result.data, table, side)
    risk, risk_errors = validate_risk((result.data or {}).get("risk") if isinstance(result.data, dict) else None, table, side)
    raw_rows = (result.data or {}).get("points") if isinstance(result.data, dict) else None
    attempted = isinstance(raw_rows, list) and any(isinstance(row, dict) for row in raw_rows)
    # 模型写了论据但校验全灭：算失败，不要当成「故意空列表」去缓存
    failed = attempted and not points
    return {
        "points": points,
        "errors": errors + risk_errors,
        "risk": risk,
        "raw": result.data,
        "failed": failed,
    }


def run_side(gateway: Gateway, table: FactTable, side: str, cache=None, model_tag: str = "") -> dict:
    """只跑看多或看空一边。算过的记在自己的缓存里，主分析师再跑时不会把这一边重算。"""
    systems = dict(SIDES)
    if side not in systems:
        raise KeyError(side)
    key = side_cache_key(table, side, model_tag)
    if cache is not None:
        hit = cache.get(key)
        if hit and not hit.get("failed"):
            hit = dict(hit)
            hit["cached"] = True
            return hit
        both = cache.get(cache_key(table, model_tag))
        saved = (both or {}).get(side) if isinstance(both, dict) else None
        if saved and not saved.get("failed"):
            saved = dict(saved)
            saved["cached"] = True
            return saved
    user = prompts.side_user(table.sides_view())
    result = gateway.run([(side, systems[side], user, 0.3)])[0]
    packed = _pack(side, result, table)
    # 失败或「有错误且空论据」不落缓存，避免看多一整天都显示没有论据
    if cache is not None and not packed["failed"] and (packed["points"] or not packed["errors"]):
        cache.set(key, packed, policy.SIDE_CACHE_SECONDS)
    return packed


def run_sides(gateway: Gateway, table: FactTable, cache=None, model_tag: str = "") -> dict:
    """返回 {"bull": {...}, "bear": {...}, "cached": bool}。每个 side 含 points（已逐条校验）、errors、raw。

    按事实表哈希缓存：盘中只有价格变化时，复用当天的正反方论据，只重跑主 Agent。
    某一边已经单独算过，就只补另一边。
    """
    key = cache_key(table, model_tag)
    if cache is not None:
        hit = cache.get(key)
        if hit:
            hit["cached"] = True
            return hit
    user = prompts.side_user(table.sides_view())
    out: dict = {"cached": False}
    pending = []
    for side, system in SIDES:
        saved = cache.get(side_cache_key(table, side, model_tag)) if cache is not None else None
        if saved and not saved.get("failed"):
            out[side] = dict(saved)
        else:
            pending.append((side, system))
    if pending:
        results = gateway.run([(side, system, user, 0.3) for side, system in pending])
        for (side, _system), result in zip(pending, results):
            out[side] = _pack(side, result, table)
    if cache is not None and not any(out[side]["failed"] for side, _ in SIDES):
        for side, _system in SIDES:
            packed = out[side]
            if packed["points"] or not packed["errors"]:
                cache.set(side_cache_key(table, side, model_tag), packed, policy.SIDE_CACHE_SECONDS)
        if all(out[side]["points"] or not out[side]["errors"] for side, _ in SIDES):
            cache.set(key, out, policy.SIDE_CACHE_SECONDS)
    return out
