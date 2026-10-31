"""后台重算：任务表按股票去重，30 秒内的多次触发合并成一次；重启不丢。

页面接口只读存档，重算都在这里做，避免用户请求等 5 次模型调用，也避免并发请求重复调模型。
"""

from __future__ import annotations

import threading
from typing import Callable

from app.synthesis import policy
from app.synthesis.store import Store

DEBOUNCE_SECONDS = 30.0
MAX_WAIT_SECONDS = 120.0
RETRY_SECONDS = 15.0
MAX_ATTEMPTS = 3

_lock = threading.Lock()
_store: Store | None = None
_worker: "Worker | None" = None


def get_store() -> Store:
    global _store
    with _lock:
        if _store is None:
            _store = Store()
        return _store


def set_store(store: Store | None) -> None:
    global _store
    with _lock:
        _store = store


class Worker(threading.Thread):
    def __init__(self, store: Store, runner: Callable[[str, str], dict | None], interval: float = 2.0):
        super().__init__(daemon=True, name="synthesis-worker")
        self.store = store
        self.runner = runner
        self.interval = interval
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        self.store.release_stuck()
        while not self._stop_event.is_set():
            self.tick()
            self._stop_event.wait(self.interval)

    def tick(self, now: float | None = None) -> int:
        """处理所有到期的任务，返回处理个数。拆出来是为了测试不用起线程。"""
        handled = 0
        for job in self.store.claim_due(now):
            handled += 1
            try:
                reply = self.runner(job["ts_code"], job.get("reason") or "")
                retry = bool(reply and reply.get("pending"))
            except Exception:
                retry = True
            if retry and job["attempts"] < MAX_ATTEMPTS:
                self.store.retry(job["ts_code"], RETRY_SECONDS, now)
            else:
                self.store.finish(job["ts_code"])
        return handled


def default_runner(ts_code: str, reason: str) -> dict | None:
    from app.synthesis.service import compute
    from app.tools.stock import resolve_stock

    stock = resolve_stock(ts_code)
    if stock is None:
        return None
    return compute(stock, get_store(), force=(reason == "manual"), timeout=policy.COLLECT_TIMEOUT_BACKGROUND)


def start(runner: Callable[[str, str], dict | None] | None = None) -> Worker:
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = Worker(_store or Store(), runner or default_runner)
            _worker.start()
        return _worker


def running() -> bool:
    return _worker is not None and _worker.is_alive()


def request(ts_code: str, reason: str = "", delay: float = DEBOUNCE_SECONDS) -> None:
    get_store().enqueue(ts_code, delay, reason, MAX_WAIT_SECONDS)


def notify(ts_code: str, dimension: str) -> None:
    """某个维度算出新结果时调用。没有起后台线程时什么都不做（测试、脚本）。"""
    if running():
        request(ts_code, f"dimension:{dimension}")
