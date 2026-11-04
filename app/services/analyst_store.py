"""各维度分析师结论的共用落库：stream 写入，detail 只读。

存两种结果：
- 有 text → 成功
- {"failed": True} → 分析失败，详情才回退规则 verdict
- 还没有记录 → 分析师尚未出结果，详情显示「分析中」
"""

from __future__ import annotations

TTL = 7 * 24 * 3600


def save(store, key: str, headline: dict | None) -> None:
    if store is None:
        return
    if headline and headline.get("text"):
        store.set(key, headline, TTL)
        return
    store.set(key, {"failed": True}, TTL)


def load(store, key: str) -> dict | None:
    if store is None:
        return None
    row = store.get(key)
    if not isinstance(row, dict) or row.get("failed"):
        return None
    return row if row.get("text") else None


def failed(store, key: str) -> bool:
    if store is None:
        return False
    row = store.get(key)
    return isinstance(row, dict) and bool(row.get("failed"))
