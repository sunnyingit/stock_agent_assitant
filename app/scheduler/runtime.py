"""把计划器和执行器跑起来：后台线程定时 tick，有人唤醒（电脑恢复、手动触发）就立刻 tick。"""

from __future__ import annotations

import logging
import os
import threading
import uuid

from app.scheduler.calendar import TradingCalendar
from app.scheduler.core import Scheduler
from app.scheduler.executor import Executor
from app.scheduler.spec import registry as default_registry
from app.scheduler.store import SchedStore

log = logging.getLogger("scheduler")
TICK_S = 15
LEASE_S = 60


class Runtime:
    def __init__(self, scheduler: Scheduler, executor: Executor | None = None, tick_s: float = TICK_S):
        self.sched = scheduler
        self.exec = executor or Executor(scheduler, owner=f"{os.getpid()}-{uuid.uuid4().hex[:6]}")
        self.tick_s = tick_s
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- 领导者：同一份数据库只让一个进程调度（Flask 重载、两个实例同时开都不会重复触发）
    def leader(self) -> bool:
        store, now, me = self.sched.store, self.sched.clock.now_ms(), self.exec.owner
        held = store.state("leader") or {}
        if held.get("owner") not in (None, me) and held.get("until", 0) > now:
            return False
        store.set_state("leader", {"owner": me, "until": now + LEASE_S * 1000}, now)
        return True

    def tick(self, reason: str = "timer") -> dict:
        if not self.leader():
            return {"leader": False}
        out = self.sched.tick()
        out["executed"] = self.exec.pump()
        out["reason"] = reason
        return out

    def wake(self, reason: str = "wake") -> None:
        self._reason = reason
        self._wake.set()

    def _loop(self) -> None:
        reason = "start"
        while not self._stop.is_set():
            try:
                self.tick(reason)
            except Exception:
                log.exception("调度循环出错")
            self._wake.wait(self.tick_s)
            reason = getattr(self, "_reason", "timer") if self._wake.is_set() else "timer"
            self._wake.clear()

    def start(self) -> "Runtime":
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self.exec.shutdown()


_runtime: Runtime | None = None
_lock = threading.Lock()


def code_version() -> str:
    try:
        import subprocess
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        return subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=2).stdout.strip()
    except Exception:
        return ""


def current(create: bool = True) -> Runtime | None:
    global _runtime
    with _lock:
        if _runtime is None and create:
            from app.scheduler import tasks  # noqa: F401  注册任务
            store = SchedStore(os.environ.get("SCHED_DB") or None)
            sched = Scheduler(store, default_registry, TradingCalendar(), profile="client", code_version=code_version())
            from app.scheduler import notify_hook
            sched.listeners.append(lambda run, spec: notify_hook.on_run_finished(run, spec, sched))
            _runtime = Runtime(sched)
        return _runtime


def install(runtime: Runtime | None) -> None:
    """测试里换成自己的运行时。"""
    global _runtime
    with _lock:
        _runtime = runtime


def start() -> Runtime:
    return current().start()
