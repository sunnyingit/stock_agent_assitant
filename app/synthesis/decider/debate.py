"""正方、反方各说一轮：并行调用、互相看不到，也看不到规则底线。"""

from __future__ import annotations

from app.synthesis import policy
from app.synthesis.decider import prompts
from app.synthesis.decider.validate import validate_side
from app.synthesis.gateway import Gateway
from app.synthesis.types import FactTable

SIDES = (("bull", prompts.BULL_SYSTEM), ("bear", prompts.BEAR_SYSTEM))


def cache_key(table: FactTable, model_tag: str) -> str:
    return f"sides:{table.sides_digest()}:{prompts.PROMPT_VERSION}:{model_tag}"


def run_sides(gateway: Gateway, table: FactTable, cache=None, model_tag: str = "") -> dict:
    """返回 {"bull": {...}, "bear": {...}, "cached": bool}。每个 side 含 points（已逐条校验）、errors、raw。

    按事实表哈希缓存：盘中只有价格变化时，复用当天的正反方论据，只重跑主 Agent。
    """
    key = cache_key(table, model_tag)
    if cache is not None:
        hit = cache.get(key)
        if hit:
            hit["cached"] = True
            return hit
    user = prompts.side_user(table.sides_view())
    results = gateway.run([(side, system, user, 0.3) for side, system in SIDES])
    out: dict = {"cached": False}
    for (side, _system), result in zip(SIDES, results):
        if not result.ok:
            out[side] = {"points": [], "errors": [f"调用:{result.error}"], "raw": None, "failed": True}
            continue
        points, errors = validate_side(result.data, table, side)
        out[side] = {"points": points, "errors": errors, "raw": result.data, "failed": False}
    if cache is not None and not any(out[side]["failed"] for side, _ in SIDES):
        cache.set(key, out, policy.SIDE_CACHE_SECONDS)
    return out
