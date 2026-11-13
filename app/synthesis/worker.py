"""后台重算：任务表按股票去重，30 秒内的多次触发合并成一次；重启不丢。

页面接口只读存档，重算都在这里做，避免用户请求等 5 次模型调用，也避免并发请求重复调模型。
"""

from __future__ import annotations

import threading
from typing import Callable

from app.synthesis import policy
from app.synthesis.store import Store, get_store, set_store

DEBOUNCE_SECONDS = 30.0
MAX_WAIT_SECONDS = 120.0
RETRY_SECONDS = 15.0
MAX_ATTEMPTS = 3

_lock = threading.Lock()
_worker: "Worker | None" = None


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
    """后台触发：数据服务不调模型，也不就地算结论。

    结论（五位分析师、看多看空、主分析师）由客户端的定时任务在本机生成，这里只是不再替它做。"""
    return None


def start(runner: Callable[[str, str], dict | None] | None = None) -> Worker:
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = Worker(get_store(), runner or default_runner)
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
