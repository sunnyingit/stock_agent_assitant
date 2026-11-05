"""计划器：把触发器、交易日历、补跑策略、依赖和就绪门，变成 task_run 和 job。设计见 3.2、3.5、3.7。

tick() 只做「规划」，不执行：执行在 executor。两者靠 SQLite 里的 job 表交接。
"""

from __future__ import annotations

import logging
from datetime import date

from app.scheduler import clock as clocks
from app.scheduler import triggers
from app.scheduler.calendar import TradingCalendar
from app.scheduler.context import PlanContext
from app.scheduler.ids import ulid
from app.scheduler.spec import QUEUES, Registry, TaskSpec, Unit, registry as default_registry
from app.scheduler.store import SchedStore, TERMINAL

log = logging.getLogger("scheduler")
SUCCESS = ("succeeded", "partial", "skipped")
STUCK_WAIT_MS = 6 * 3600_000     # 手动或事件触发的运行，等依赖和就绪门最多等这么久


class Scheduler:
    def __init__(self, store: SchedStore, registry: Registry | None = None, calendar: TradingCalendar | None = None,
                 clock: clocks.Clock | None = None, profile: str = "client", code_version: str = ""):
        self.store = store
        self.registry = registry or default_registry
        self.calendar = calendar or TradingCalendar()
        self.clock = clock or clocks.Clock()
        self.profile = profile
        self.code_version = code_version
        self.listeners: list = []          # 运行结束时回调 fn(run, spec)，通知层用它

    # ------------------------------------------------------------ 配置
    def effective(self, spec: TaskSpec) -> dict:
        override = self.store.override(spec.task_id) or {}
        trigger, overridden = spec.trigger, False
        if override.get("trigger"):
            try:
                trigger, overridden = triggers.validate(override["trigger"], spec.min_interval_min), True
            except ValueError as exc:
                log.warning("任务 %s 的触发器覆盖不合法，用默认值：%s", spec.task_id, exc)
        enabled = True if override.get("enabled") is None else bool(override["enabled"])
        return {"enabled": enabled, "trigger": trigger, "params": override.get("params") or {}, "overridden": bool(override) and (overridden or override.get("enabled") is not None or bool(override.get("params")))}

    def tasks(self) -> list[TaskSpec]:
        return self.registry.for_profile(self.profile)

    # ------------------------------------------------------------ tick
    def tick(self) -> dict:
        now = self.clock.now_ms()
        summary = {"planned": 0, "started": 0, "finished": 0, "reclaimed": self.store.reclaim(now)}
        if self.store.state("first_tick") is None:
            self.store.set_state("first_tick", {"at": now}, now)
        for spec in self.tasks():
            eff = self.effective(spec)
            try:
                if eff["enabled"]:
                    summary["planned"] += self._plan(spec, eff, now)
                for run in self.store.runs(spec.task_id, active=True, limit=200):
                    before = run["state"]
                    after = self._advance(spec, run, eff, now)
                    summary["started"] += before in ("planned", "waiting") and after == "running"
                    summary["finished"] += after in ("succeeded", "partial", "failed", "skipped", "expired") and before not in ("succeeded", "partial", "failed", "skipped", "expired")
            except Exception:
                log.exception("规划任务 %s 失败", spec.task_id)
        self.store.set_state("last_tick", {"at": now}, now)
        return summary

    # ------------------------------------------------------------ 规划：该有哪些 run
    def _plan(self, spec: TaskSpec, eff: dict, now: int) -> int:
        trig = eff["trigger"]
        if trig["kind"] in ("event", "manual"):
            return 0
        window = triggers.window_ms(trig) or 0
        horizon = window * 3 if spec.misfire == "skip" else (max(window, spec.catchup_ttl_min * 60_000) if trig["kind"] == "cron" else None)
        missing = [p for p in triggers.candidates(trig, now, self.calendar, horizon_ms=horizon) if self.store.run_by_period(spec.task_id, p.key) is None]
        created = 0
        ttl_ms = spec.catchup_ttl_min * 60_000
        installed = (self.store.state("first_tick") or {}).get("at", now)
        config = {"trigger": trig, "params": eff["params"], "queue": spec.queue, "budget": spec.budget}
        for index, period in enumerate(missing):
            age = now - period.scheduled_for
            window = triggers.window_ms(trig, period.scheduled_for) or 0
            if age <= window:
                state, how, note = "planned", "schedule", ""
            elif period.slot:
                state, how, note = "expired", "schedule", "错过了这个时刻，盘中的任务不补跑"
            elif spec.misfire == "skip":
                state, how, note = "expired", "schedule", "misfire:skip"
            elif spec.misfire == "run_latest" and index != len(missing) - 1:
                state, how, note = "expired", "schedule", "misfire:run_latest，只补最近一次"
            elif age <= ttl_ms:
                state, how, note = "planned", "catchup", "补跑"
            else:
                state, how, note = "expired", "schedule", "错过太久，超过补跑有效期"
            if state == "expired" and period.scheduled_for < installed:
                continue            # 装上之前的周期，谈不上「错过」，不留记录
            if self.store.create_run(spec.task_id, period.key, how, period.scheduled_for, state, config, self.code_version, note, now):
                created += state == "planned"
        return created

    # ------------------------------------------------------------ 推进：等待 → 展开 → 结束
    def _advance(self, spec: TaskSpec, run: dict, eff: dict, now: int) -> str:
        state = run["state"]
        if state in ("planned", "waiting"):
            blocked = self._blocked(spec, run, eff, now)
            if blocked == "failed":
                return "failed"
            if blocked == "expired":
                self.store.update_run(run["run_id"], state="expired", finished_at=now, note="等依赖或数据就绪超过了截止时间")
                self._finished(run["run_id"], spec)
                return "expired"
            if blocked:
                if state != "waiting" or run["note"] != blocked:
                    self.store.update_run(run["run_id"], state="waiting", note=blocked)
                return "waiting"
            return self._start(spec, run, eff, now)
        if state == "running":
            return self.refresh_run(run["run_id"], spec) or "running"
        return state

    def _blocked(self, spec: TaskSpec, run: dict, eff: dict, now: int) -> str | None:
        trig = eff["trigger"]
        window = triggers.window_ms(trig, run["scheduled_for"])
        ttl = spec.catchup_ttl_min * 60_000 if run["trigger"] == "catchup" else 0
        deadline = run["scheduled_for"] + (max(window or 0, ttl) if window is not None or ttl else STUCK_WAIT_MS)
        trade_date = triggers.period_date(run["period_key"]) or clocks.day_of(now).isoformat()
        wait = None
        gates = list(spec.gates) + ([trig["gate"]] if trig["kind"] == "data_ready" else [])
        not_ready = [name for name in gates if not self.registry.gate_ready(name, trade_date)]
        if not_ready:
            wait = "等数据就绪：" + "、".join(not_ready)
        for dep in spec.after:
            reason = self._dependency(dep, trade_date, run, now)
            if reason == "failed":
                self.store.update_run(run["run_id"], state="failed", finished_at=now, note=f"依赖的 {dep['task']} 没有成功")
                self._finished(run["run_id"], spec)
                return "failed"
            if reason:
                wait = reason
        if wait and now > deadline:
            return "expired"
        return wait

    def _dependency(self, dep: dict, trade_date: str, run: dict, now: int) -> str | None:
        """None 表示可以往下走；字符串是在等什么；'failed' 是硬依赖失败。"""
        try:
            dep_spec = self.registry.get(dep["task"])
        except KeyError:
            return None
        if not self.effective(dep_spec)["enabled"] or dep_spec.placement != self.profile:
            return None
        mode, timeout = dep.get("mode", "soft"), dep.get("timeout_min", 30) * 60_000
        found = next((item for item in self.store.runs(dep["task"], limit=10) if (triggers.period_date(item["period_key"]) or trade_date) == trade_date), None)
        if found and found["state"] in SUCCESS:
            return None
        if found and found["state"] in ("failed", "expired"):
            return "failed" if mode == "hard" else None
        if mode == "soft" and now - run["scheduled_for"] >= timeout:
            return None
        return f"等 {dep['task']} 先跑完"

    def _start(self, spec: TaskSpec, run: dict, eff: dict, now: int) -> str:
        trade_date = triggers.period_date(run["period_key"]) or clocks.day_of(now).isoformat()
        try:
            units = self._units(spec, run, eff, trade_date, now)
        except Exception as exc:
            log.exception("展开任务 %s 失败", spec.task_id)
            self.store.update_run(run["run_id"], state="failed", finished_at=now, note=f"展开单元失败：{exc}")
            self._finished(run["run_id"], spec)
            return "failed"
        expire = None
        window = triggers.window_ms(eff["trigger"], run["scheduled_for"])
        if spec.expire_after_min is not None:
            expire = run["scheduled_for"] + spec.expire_after_min * 60_000
        elif eff["trigger"]["kind"] == "session_interval":
            expire = run["scheduled_for"] + (window or 0)
        self.store.add_jobs(run["run_id"], spec.task_id, run["period_key"], [{"unit_key": u.unit_key, "priority": u.priority, "input": u.input} for u in units],
                            spec.queue, max(now, run["scheduled_for"]), spec.max_attempts, now, expire)
        self.store.update_run(run["run_id"], state="running", started_at=now, note="", counts={"jobs": len(units)})
        if not units:
            self.store.update_run(run["run_id"], state="succeeded", finished_at=now, note="没有需要处理的对象")
            self._finished(run["run_id"], spec)
            return "succeeded"
        return "running"

    def _units(self, spec: TaskSpec, run: dict, eff: dict, trade_date: str, now: int) -> list[Unit]:
        manual = (run.get("config") or {}).get("units")
        if manual:
            return [Unit(item if isinstance(item, str) else item["unit_key"], 50, {} if isinstance(item, str) else item.get("input", {})) for item in manual]
        ctx = PlanContext(spec.task_id, run["period_key"], trade_date, now, eff["params"], self.store, self.profile)
        units = self.registry.fanout(spec.fanout)(ctx)
        extra = (run.get("config") or {}).get("input") or {}
        out = []
        for unit in units:
            out.append(Unit(unit.unit_key, unit.priority, {**unit.input, **extra, "trade_date": trade_date}))
        return out

    # ------------------------------------------------------------ 结束
    def refresh_run(self, run_id: str, spec: TaskSpec | None = None) -> str | None:
        run = self.store.run(run_id)
        if run is None or run["state"] != "running":
            return run["state"] if run else None
        counts = self.store.job_counts(run_id)
        left = sum(counts.get(state, 0) for state in ("pending", "leased", "running", "retrying", "not_ready"))
        if left:
            self.store.update_run(run_id, counts=counts)
            return "running"
        done, skipped = counts.get("succeeded", 0), counts.get("skipped", 0)
        bad = counts.get("dead", 0) + counts.get("expired", 0) + counts.get("cancelled", 0)
        if bad == 0:
            state = "skipped" if done == 0 and skipped else "succeeded"
        else:
            state = "partial" if done else "failed"
        self.store.update_run(run_id, state=state, counts=counts, finished_at=self.clock.now_ms())
        self._finished(run_id, spec)
        return state

    def _finished(self, run_id: str, spec: TaskSpec | None) -> None:
        run = self.store.run(run_id)
        spec = spec or (self.registry.get(run["task_id"]) if run else None)
        for listener in self.listeners:
            try:
                listener(run, spec)
            except Exception:
                log.exception("运行结束回调失败")

    # ------------------------------------------------------------ 手动、事件触发
    def manual_run(self, task_id: str, units: list | None = None, input: dict | None = None, period_key: str | None = None,
                   dry_run: bool = False) -> dict:
        spec = self.registry.get(task_id)
        eff = self.effective(spec)
        now = self.clock.now_ms()
        if dry_run:
            fake = {"run_id": "dry", "period_key": period_key or "manual:dry", "config": {"units": units, "input": input or {}}, "trigger": "manual", "scheduled_for": now}
            trade_date = clocks.day_of(now).isoformat()
            planned = [{"unit_key": u.unit_key, "priority": u.priority} for u in self._units(spec, fake, eff, trade_date, now)]
            gates = {name: self.registry.gate_ready(name, trade_date) for name in spec.gates}
            return {"dry_run": True, "units": planned, "gates": gates, "queue": spec.queue}
        key = period_key or "manual:" + ulid(now)
        config = {"trigger": {"kind": "manual"}, "params": eff["params"], "units": units, "input": input or {}, "queue": spec.queue}
        run = self.store.create_run(task_id, key, "manual", now, "planned", config, self.code_version, "手动运行", now)
        if run is None:
            raise ValueError("这个周期已经有运行了：" + key)
        return {"run": run, "dry_run": False}

    def trigger_event(self, task_id: str, key: str, input: dict | None = None) -> dict:
        """事件触发：同一个 key 在去抖时间内合并成一个单元，等最后一次事件之后 debounce 秒再执行。"""
        spec = self.registry.get(task_id)
        eff = self.effective(spec)
        trig = eff["trigger"]
        if trig["kind"] != "event" or not eff["enabled"]:
            return {"accepted": False, "reason": "任务没有启用事件触发"}
        now = self.clock.now_ms()
        merged = self.store.coalesce(task_id, key, now, trig["debounce_s"] * 1000, trig["max_wait_s"] * 1000)
        if merged:
            return {"accepted": True, "merged": True, "job_id": merged["job_id"]}
        run = self.store.create_run(task_id, f"event:{key}:{now}", "event", now, "running", {"trigger": trig, "params": eff["params"]}, self.code_version, "", now)
        self.store.add_jobs(run["run_id"], task_id, run["period_key"], [{"unit_key": key, "priority": spec.priority, "input": {**(input or {}), "trade_date": clocks.day_of(now).isoformat()}}],
                            spec.queue, now + trig["debounce_s"] * 1000, spec.max_attempts, now, None, key)
        self.store.update_run(run["run_id"], started_at=now, counts={"jobs": 1})
        return {"accepted": True, "merged": False, "run_id": run["run_id"]}

    def retry_job(self, job_id: str, from_step: str | None = None) -> dict:
        job = self.store.job(job_id)
        if job is None:
            raise KeyError("没有这个单元：" + job_id)
        if job["state"] not in ("dead", "expired", "cancelled", "skipped", "succeeded"):
            raise ValueError("这个单元还没有结束，不能重试")
        now = self.clock.now_ms()
        self.store.reset_steps(job_id, from_step, now)
        self.store.update_job(job_id, now, state="pending", due_at=now, expire_at=None, error=None, lease_owner=None, lease_until=None,
                              max_attempts=max(job["max_attempts"], job["attempt"] + 1))
        run = self.store.run(job["run_id"])
        if run and run["state"] not in ("planned", "waiting", "running"):
            self.store.update_run(run["run_id"], state="running", finished_at=None)
        return self.store.job(job_id)

    def cancel_job(self, job_id: str) -> dict:
        job = self.store.job(job_id)
        if job is None:
            raise KeyError("没有这个单元：" + job_id)
        if job["state"] in ("pending", "retrying", "not_ready"):
            self.store.update_job(job_id, self.clock.now_ms(), state="cancelled")
        return self.store.job(job_id)

    # ------------------------------------------------------------ 查询（给接口用）
    def preview(self, task_id: str, trigger: dict | None = None, count: int = 5) -> dict:
        spec = self.registry.get(task_id)
        trig = triggers.validate(trigger, spec.min_interval_min) if trigger else self.effective(spec)["trigger"]
        now = self.clock.now_ms()
        times = triggers.next_fires(trig, now, self.calendar, count)
        return {"trigger": trig, "approximate_calendar": self.calendar.approximate(clocks.day_of(now)),
                "fires": [{"at": ms, "local": clocks.from_ms(ms).strftime("%Y-%m-%d %H:%M"), "trade_date": clocks.day_of(ms).isoformat()} for ms in times]}

    def describe(self, spec: TaskSpec) -> dict:
        eff = self.effective(spec)
        now = self.clock.now_ms()
        nxt = triggers.next_fire(eff["trigger"], now, self.calendar)
        last = self.store.last_run(spec.task_id)
        return {
            "v": 1, "task_id": spec.task_id, "title": spec.title, "placement": spec.placement, "enabled": eff["enabled"],
            "overridden": eff["overridden"], "trigger": eff["trigger"], "trigger_default": spec.trigger,
            "next_fire_at": nxt, "next_fire_trade_date": clocks.day_of(nxt).isoformat() if nxt else None,
            "queue": spec.queue, "gates": list(spec.gates), "after": list(spec.after), "budget": spec.budget, "params": eff["params"],
            "last_run": None if not last else {key: last[key] for key in ("run_id", "period_key", "state", "counts", "started_at", "finished_at", "note", "trigger")},
        }

    def health(self) -> dict:
        now = self.clock.now_ms()
        tick = self.store.state("last_tick") or {}
        depth: dict[str, int] = {}
        for queue in QUEUES:
            with self.store._conn() as conn:
                depth[queue] = conn.execute("SELECT COUNT(*) FROM job WHERE queue=? AND state IN ('pending','retrying','not_ready','leased','running')", (queue,)).fetchone()[0]
        return {"now": now, "last_tick_at": tick.get("at"), "tick_age_s": None if not tick else round((now - tick["at"]) / 1000, 1),
                "queue_depth": depth, "waiting_runs": len(self.store.runs(states=("waiting",), limit=100)),
                "approximate_calendar": self.calendar.approximate(clocks.day_of(now)), "profile": self.profile}
