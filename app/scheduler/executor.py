"""执行器：从各队列领到期的单元，调任务的处理函数，按错误类别决定重试、等待还是放弃。设计见 3.6、3.8。"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from app.scheduler import clock as clocks
from app.scheduler.context import JobContext
from app.scheduler.core import Scheduler
from app.scheduler.errors import NotReady, classify
from app.scheduler.limits import Limits, budget_exceeded
from app.scheduler.spec import QUEUES, TaskSpec

log = logging.getLogger("scheduler")
LEASE_SLACK_S = 60


class Executor:
    def __init__(self, scheduler: Scheduler, owner: str = "local", limits: Limits | None = None, concurrency: dict | None = None):
        self.sched = scheduler
        self.store = scheduler.store
        self.clock = scheduler.clock
        self.owner = owner
        self.limits = limits or Limits()
        self.concurrency = {**QUEUES, **(concurrency or {})}
        self._running: dict[str, int] = {name: 0 for name in self.concurrency}
        self._lock = threading.Lock()
        self._pool: ThreadPoolExecutor | None = None

    # ------------------------------------------------------------ 领取
    def _free(self, queue: str) -> int:
        with self._lock:
            return self.concurrency[queue] - self._running[queue]

    def claim_batch(self) -> list[dict]:
        now = self.clock.now_ms()
        claimed = []
        for queue in self.concurrency:
            room = self._free(queue)
            if room <= 0:
                continue
            allowed = 0
            for _ in range(room):
                if self.limits.allow(queue, now / 1000):
                    allowed += 1
                else:
                    break
            if not allowed:
                continue
            jobs = self.store.claim(queue, now, self.owner, self._lease_ms(queue), allowed)
            for job in jobs:
                with self._lock:
                    self._running[queue] += 1
            claimed.extend(jobs)
        return claimed

    def _lease_ms(self, queue: str) -> int:
        longest = max((spec.timeout_s for spec in self.sched.tasks() if spec.queue == queue), default=300)
        return (longest + LEASE_SLACK_S) * 1000

    # ------------------------------------------------------------ 执行一个单元
    def execute(self, job: dict) -> str:
        """返回单元的新状态。所有异常在这里消化，不往外抛。"""
        queue = job["queue"]
        try:
            return self._execute(job)
        except Exception:
            log.exception("执行单元 %s 时调度器自身出错", job["job_id"])
            self.store.update_job(job["job_id"], self.clock.now_ms(), state="dead", error={"class": "permanent", "code": "INTERNAL", "message": traceback.format_exc(limit=3)})
            return "dead"
        finally:
            with self._lock:
                self._running[queue] = max(0, self._running[queue] - 1)
            self.sched.refresh_run(job["run_id"])

    def _execute(self, job: dict) -> str:
        now = self.clock.now_ms()
        try:
            spec = self.sched.registry.get(job["task_id"])
        except KeyError:
            self.store.update_job(job["job_id"], now, state="dead", error={"class": "permanent", "code": "UNKNOWN_TASK", "message": "任务已不存在"})
            return "dead"
        budget = spec.budget or {}
        resource = budget.get("resource", "")
        window = clocks.day_of(now).isoformat()
        if resource:
            hit = budget_exceeded(budget, self.store.usage(resource, window))
            if hit:
                self.store.update_job(job["job_id"], now, state="skipped", error={"class": "budget", "code": "BUDGET_EXCEEDED", "message": f"今天 {resource} 的 {hit} 额度用完了"})
                return "skipped"
        attempt = job["attempt"] + 1
        self.store.update_job(job["job_id"], now, state="running", attempt=attempt)
        self.store.begin_attempt(job["job_id"], attempt, self.owner, now)
        job = self.store.job(job["job_id"])
        eff = self.sched.effective(spec)
        ctx = JobContext(job=job, store=self.store, clock=self.clock, params=eff["params"], profile=self.sched.profile,
                         deadline_ms=now + spec.timeout_s * 1000, resource=resource)
        try:
            value = spec.handler(ctx)
        except NotReady as exc:
            # 数据还没到：不消耗尝试次数，过一会儿再来
            after = self.clock.now_ms() + max(5, exc.retry_after_s) * 1000
            self.store.end_attempt(job["job_id"], attempt, self.clock.now_ms(), "not_ready", {"class": "not_ready", "code": exc.code, "message": str(exc)}, ctx.tokens, ctx.calls)
            self.store.update_job(job["job_id"], self.clock.now_ms(), state="not_ready", attempt=attempt - 1, due_at=after, lease_owner=None, lease_until=None,
                                  error={"class": "not_ready", "code": exc.code, "message": str(exc)})
            return "not_ready"
        except Exception as exc:
            return self._failed(spec, job, attempt, ctx, exc)
        done = self.clock.now_ms()
        output = self._output(value, ctx, started=now, finished=done)
        self.store.end_attempt(job["job_id"], attempt, done, "succeeded", None, ctx.tokens, ctx.calls)
        self.store.update_job(job["job_id"], done, state="succeeded", output=output, error=None, lease_owner=None, lease_until=None)
        if resource:
            self.store.add_usage(resource, window, calls=ctx.calls, tokens=ctx.tokens)
        return "succeeded"

    @staticmethod
    def _output(value, ctx: JobContext, started: int, finished: int) -> dict:
        summary = value if isinstance(value, (dict, str, int, float, list)) else ({} if value is None else str(value))
        return {"v": 1, "summary": summary, "refs": ctx.refs, "metrics": {"duration_ms": finished - started, "tokens": ctx.tokens, "calls": ctx.calls}}

    def _failed(self, spec: TaskSpec, job: dict, attempt: int, ctx: JobContext, exc: Exception) -> str:
        klass, code = classify(exc)
        now = self.clock.now_ms()
        error = {"class": klass, "code": code, "message": str(exc)[:500], "attempt": attempt}
        if klass == "transient":
            log.warning("任务 %s/%s 第 %d 次失败（可重试）：%s", job["task_id"], job["unit_key"], attempt, exc)
        else:
            log.error("任务 %s/%s 失败：%s", job["task_id"], job["unit_key"], "".join(traceback.format_exception_only(type(exc), exc)).strip())
        self.store.end_attempt(job["job_id"], attempt, now, "failed", error, ctx.tokens, ctx.calls)
        if ctx.resource and (ctx.tokens or ctx.calls):
            self.store.add_usage(ctx.resource, clocks.day_of(now).isoformat(), calls=ctx.calls, tokens=ctx.tokens)
        if klass == "transient" and attempt < job["max_attempts"]:
            delay = spec.backoff_s[min(attempt - 1, len(spec.backoff_s) - 1)] if spec.backoff_s else 60
            self.store.update_job(job["job_id"], now, state="retrying", due_at=now + delay * 1000, error=error, lease_owner=None, lease_until=None)
            return "retrying"
        self.store.update_job(job["job_id"], now, state="dead", error=error, lease_owner=None, lease_until=None)
        return "dead"

    # ------------------------------------------------------------ 驱动
    def run_once(self) -> int:
        """同步领一批并执行完，返回执行个数。测试和手动触发用。"""
        jobs = self.claim_batch()
        for job in jobs:
            self.execute(job)
        return len(jobs)

    def drain(self, max_rounds: int = 50) -> int:
        total = 0
        for _ in range(max_rounds):
            ran = self.run_once()
            total += ran
            if not ran:
                break
        return total

    def pump(self) -> int:
        """线程池版本：领到的单元丢给线程池，立刻返回。"""
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=sum(self.concurrency.values()), thread_name_prefix="sched")
        jobs = self.claim_batch()
        for job in jobs:
            self._pool.submit(self.execute, job)
        return len(jobs)

    def shutdown(self, wait: bool = False) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=wait, cancel_futures=True)
            self._pool = None
