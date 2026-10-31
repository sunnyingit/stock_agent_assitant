"""决策层：事实表进，结论出。纯函数加模型调用，不依赖 app.services，线上和回测共用。"""

from __future__ import annotations

from app.synthesis import policy
from app.synthesis.decider import debate, judge, prompts, stabilize, strength
from app.synthesis.gateway import Gateway
from app.synthesis.types import FactTable

PROMPT_VERSION = prompts.PROMPT_VERSION


def decide(
    table: FactTable,
    gateway: Gateway,
    session: str = "close",
    previous: dict | None = None,
    cache=None,
    model_tag: str = "",
) -> dict:
    """返回：

    {"source": "agent" | "fallback", "reason": 退回原因（agent 时为空）,
     "final": {...} | None, "sides": ..., "runs": [...], "digest": ...}
    退回时 final 为 None，由 service 改用规则版结论。
    """
    out: dict = {"source": "fallback", "reason": "", "final": None, "sides": {}, "runs": [], "digest": table.digest()}
    if not gateway.available:
        out["reason"] = "no_model"
        return out
    sides = debate.run_sides(gateway, table, cache, model_tag)
    out["sides"] = sides
    if sides["bull"]["failed"] and sides["bear"]["failed"]:
        out["reason"] = "model_error"
        return out
    runs = judge.run_chief(gateway, table, sides, policy.CHIEF_RUNS.get(session, 1))
    out["runs"] = runs
    if runs and all(item["raw"] is None for item in runs):
        out["reason"] = "model_error"
        return out
    chosen, reason = judge.consensus(runs)
    if chosen is None:
        out["reason"] = reason
        return out
    done = (table.context or {}).get("done", 0)
    total = (table.context or {}).get("total", len(policy.DIMENSIONS))
    level = strength.strength(chosen["direction"], chosen["spread"], done, total)
    direction, level, held = stabilize.anti_flip(
        {"direction": chosen["direction"], "level": level, "spread": chosen["spread"]}, previous,
    )
    out["source"] = "agent"
    out["final"] = {
        **chosen,
        "raw_direction": chosen["direction"],
        "direction": direction,
        "level": level,
        "held_by_anti_flip": held,
        "change_reason": stabilize.change_reason(previous, direction, level, chosen["adopted"]),
    }
    return out
