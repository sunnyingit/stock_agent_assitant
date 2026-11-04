"""资金流分析师的方向/结论：stream 算完后写入，detail 只读。"""

from __future__ import annotations

from app.services import analyst_store

_PREFIX = "flow_analyst:headline:"


def key(ts_code: str) -> str:
    return f"{_PREFIX}{ts_code}"


def save(store, ts_code: str, headline: dict | None) -> None:
    analyst_store.save(store, key(ts_code), headline)


def load(store, ts_code: str) -> dict | None:
    return analyst_store.load(store, key(ts_code))


def failed(store, ts_code: str) -> bool:
    return analyst_store.failed(store, key(ts_code))
