"""交给任务处理函数的上下文：输入、检查点步骤、结果存放、预算记账。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from app.scheduler.errors import Transient


@dataclass
class PlanContext:
    """交给扇出提供者：要展开哪一次运行。"""
    task_id: str
    period_key: str
    trade_date: str
    now_ms: int
    params: dict
    store: object
    profile: str = "client"


@dataclass
class JobContext:
    job: dict
    store: object
    clock: object
    params: dict = field(default_factory=dict)
    profile: str = "client"
    deadline_ms: int | None = None
    tokens: int = 0
    calls: int = 0
    refs: list = field(default_factory=list)
    resource: str = ""

    # ---- 基本信息
    @property
    def task_id(self) -> str:
        return self.job["task_id"]

    @property
    def unit_key(self) -> str:
        return self.job["unit_key"]

    @property
    def period_key(self) -> str:
        return self.job["period_key"]

    @property
    def input(self) -> dict:
        return self.job.get("input") or {}

    @property
    def attempt(self) -> int:
        return self.job["attempt"]

    @property
    def job_id(self) -> str:
        return self.job["job_id"]

    @property
    def trade_date(self) -> str:
        """周期键里带的交易日：'trading_day:2026-11-05'、'min:2026-11-05T14:30' → 2026-11-05。手动和事件触发取输入里的 trade_date。"""
        key = self.period_key
        if ":" in key:
            tail = key.split(":", 1)[1]
            if len(tail) >= 10 and tail[4] == "-" and tail[7] == "-":
                return tail[:10]
        return str(self.input.get("trade_date") or "")

    def now_ms(self) -> int:
        return self.clock.now_ms()

    def check_deadline(self) -> None:
        """长任务在循环里调用：超过单元的时限就抛瞬时错误，让调度器按重试处理，不无限占着一个 worker。"""
        if self.deadline_ms is not None and self.clock.now_ms() > self.deadline_ms:
            raise Transient("单元超过了时限", "TIMEOUT")

    # ---- 检查点：失败重试时从断点继续，已经成功的步骤不重复执行（也不重复花 token）
    def step(self, name: str, fn: Callable, tokens: int = 0):
        done = self.store.step(self.job_id, name)
        if done and done["state"] == "succeeded" and done.get("output_ref"):
            saved = self.store.result(done["output_ref"])
            if saved is not None:
                return saved["content"]
        now = self.clock.now_ms()
        self.store.put_step(self.job_id, name, "running", now, self.attempt)
        try:
            value = fn()
        except BaseException:
            self.store.put_step(self.job_id, name, "failed", self.clock.now_ms(), self.attempt)
            raise
        ref = self.store.result(self.store.put_result(value if value is not None else {}, self.clock.now_ms(), self.job_id, name))["ref"]
        self.store.put_step(self.job_id, name, "succeeded", self.clock.now_ms(), self.attempt, ref, tokens)
        self.tokens += tokens
        return value

    def skip_step(self, name: str, note: str = "") -> None:
        self.store.put_step(self.job_id, name, "skipped", self.clock.now_ms(), self.attempt)

    # ---- 结果和用量
    def result(self, content, role: str = "result", kind: str | None = None) -> str:
        ref = self.store.put_result(content, self.clock.now_ms(), self.job_id, kind=kind)
        self.refs.append({"role": role, "ref": ref})
        return ref

    def ref(self, role: str, ref: str) -> None:
        self.refs.append({"role": role, "ref": ref})

    def add_usage(self, tokens: int = 0, calls: int = 0) -> None:
        self.tokens += tokens
        self.calls += calls
