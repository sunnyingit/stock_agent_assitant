"""主 Agent 裁决：同一份输入跑多次，逐次校验，取多数。"""

from __future__ import annotations

from app.synthesis import policy
from app.synthesis.decider import prompts
from app.synthesis.decider.validate import validate_chief
from app.synthesis.gateway import Gateway
from app.synthesis.types import FactTable


def arguments_of(sides: dict) -> list[dict]:
    rows = []
    for side in ("bull", "bear"):
        rows.extend(sides.get(side, {}).get("points") or [])
    return rows


def run_chief(gateway: Gateway, table: FactTable, sides: dict, runs: int) -> list[dict]:
    """返回每次的 {"raw", "decision"(通过校验才有), "errors"}。"""
    arguments = arguments_of(sides)
    user = prompts.chief_user(table.chief_view(), [
        {"ref": item["ref"], "claim": item["claim"], "cites": item["cites"], "weight": item["weight"]}
        for item in arguments
    ])
    results = gateway.run([("chief", prompts.CHIEF_SYSTEM, user, 0.0)] * runs)
    out = []
    for result in results:
        if not result.ok:
            out.append({"raw": None, "decision": None, "errors": [f"调用:{result.error}"]})
            continue
        decision, errors = validate_chief(result.data, table, arguments)
        out.append({"raw": result.data, "decision": decision, "errors": errors})
    return out


def consensus(runs: list[dict]) -> tuple[dict | None, str]:
    """通过校验的输出里，方向 + 位置相同的占多数（至少 CONSENSUS_MIN 次）就采用，文字取其中第一份。

    返回 (被采用的 decision 或 None, 退回原因)。
    """
    passed = [item["decision"] for item in runs if item["decision"]]
    needed = min(policy.CONSENSUS_MIN, len(runs))
    if len(passed) < needed:
        return None, "validation"
    groups: dict[tuple, list[dict]] = {}
    for decision in passed:
        groups.setdefault((decision["direction"], decision["position"]), []).append(decision)
    best = max(groups.values(), key=len)
    if len(best) < needed:
        return None, "inconsistent"
    return best[0], ""
