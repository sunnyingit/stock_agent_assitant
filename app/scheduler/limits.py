"""限速和预算。令牌桶：桶里没有令牌就不领任务（不是领了再失败重试）；预算按资源、按天累计。"""

from __future__ import annotations

import threading


class Bucket:
    def __init__(self, per_min: float, burst: float | None = None):
        self.rate = per_min / 60.0
        self.capacity = burst if burst is not None else max(1.0, per_min)
        self.tokens = self.capacity
        self.updated: float | None = None
        self._lock = threading.Lock()

    def take(self, now_s: float, amount: float = 1.0) -> bool:
        with self._lock:
            if self.updated is not None:
                self.tokens = min(self.capacity, self.tokens + (now_s - self.updated) * self.rate)
            self.updated = now_s
            if self.tokens >= amount:
                self.tokens -= amount
                return True
            return False


class Limits:
    def __init__(self, rates: dict[str, float] | None = None):
        self._buckets = {name: Bucket(rate) for name, rate in (rates or {}).items()}

    def allow(self, queue: str, now_s: float) -> bool:
        bucket = self._buckets.get(queue)
        return True if bucket is None else bucket.take(now_s)

    def configure(self, queue: str, per_min: float | None) -> None:
        if per_min is None:
            self._buckets.pop(queue, None)
        else:
            self._buckets[queue] = Bucket(per_min)


def budget_exceeded(budget: dict, used: dict, extra_per_unit: dict | None = None) -> str | None:
    """budget 形如 {"resource": "llm", "per_day": {"calls": 400, "tokens": 2000000}}。返回超了哪一项，没超返回 None。"""
    limit = (budget or {}).get("per_day") or {}
    for key, cap in limit.items():
        if used.get(key, 0) >= cap:
            return key
    return None
