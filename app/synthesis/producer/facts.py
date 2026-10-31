"""组装事实表：展平成带编号的事实，去掉代码、名称、日期，只留相对时间。

线上和回测都走这一个函数，输入格式因此完全一致。
"""

from __future__ import annotations

from app.synthesis import policy
from app.synthesis.registry import REGISTRY, render
from app.synthesis.types import STATUS_OK, STATUS_PENDING, STATUS_STALE, Fact, FactTable, Snapshot


def _unavailable_text(snap: Snapshot) -> str:
    label = policy.DIMENSION_LABEL.get(snap.key, snap.key)
    if snap.status == STATUS_STALE:
        return f"{label}：已过期，{snap.reason or '数据停在更早的交易日'}"
    if snap.status == STATUS_PENDING:
        return f"{label}：还在整理，这次没有结论"
    return f"{label}：暂不可用（{snap.reason or '没有数据'}）"


def build_fact_table(
    snaps: list[Snapshot],
    market: dict,
    rule: dict,
    candidates: list[dict] | None = None,
    context: dict | None = None,
) -> FactTable:
    done = {snap.key: snap for snap in snaps if snap.status == STATUS_OK}
    table = FactTable(rule=rule, candidates=list(candidates or []), context=dict(context or {}))
    for fact_id, defn in REGISTRY.items():
        if defn.dim == "market":
            value = market.get(fact_id)
        else:
            snap = done.get(defn.dim)
            value = None if snap is None else snap.facts.get(fact_id)
        if value is None:
            continue
        table.facts.append(Fact(fact_id, value, render(fact_id, value)))
    table.unavailable = [
        {"dim": policy.DIMENSION_LABEL.get(snap.key, snap.key), "text": _unavailable_text(snap)}
        for snap in snaps
        if snap.status != STATUS_OK
    ]
    for snap in snaps:
        if snap.status != STATUS_OK:
            continue
        for event in snap.events:
            table.events.append({
                "id": f"E{len(table.events) + 1}",
                "dim": snap.key,
                "kind": event["kind"],
                "text": event["text"],
            })
    return table

