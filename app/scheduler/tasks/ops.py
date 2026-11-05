"""运维任务：看门狗（任务该跑没跑）、摘要、清理、默认规则。设计 10.5。"""

from __future__ import annotations

import logging

from app.scheduler import clock as clocks
from app.scheduler import triggers
from app.scheduler.spec import task

log = logging.getLogger("scheduler")
SUCCESS = ("succeeded", "partial", "skipped")


def find_overdue(rt, now: int) -> list[dict]:
    """声明了 overdue_after_min 的任务：今天最近一个应触发的周期，过了期限还没成功。"""
    sched, found = rt.sched, []
    for spec in sched.tasks():
        eff = sched.effective(spec)
        if not spec.overdue_after_min or not eff["enabled"] or eff["trigger"]["kind"] not in ("daily", "data_ready"):
            continue
        periods = triggers.candidates(eff["trigger"], now, sched.calendar, lookback_days=1)
        if not periods:
            continue
        period = periods[-1]
        deadline = period.scheduled_for + spec.overdue_after_min * 60_000
        if now < deadline:
            continue
        run = sched.store.run_by_period(spec.task_id, period.key)
        installed = (sched.store.state("first_tick") or {}).get("at", now)
        if period.scheduled_for < installed:
            continue                       # 装上之前的不算
        if run is not None and run["state"] in SUCCESS:
            continue
        found.append({"task_id": spec.task_id, "title": spec.title, "period_key": period.key, "expected_by": clocks.from_ms(deadline).strftime("%m-%d %H:%M"),
                      "state": run["state"] if run else "没有运行记录"})
    return found


@task("watchdog", "看门狗", {"kind": "cron", "expr": "*/10 * * * *", "days": "any", "window_min": 10}, queue="light", timeout_s=60, max_attempts=1, misfire="skip")
def watchdog(ctx):
    from app.notify import pipeline
    from app.scheduler import runtime

    rt = runtime.current(create=False)
    if rt is None:
        return {"overdue": 0}
    notifier = pipeline.current()
    overdue = find_overdue(rt, ctx.now_ms())
    for item in overdue:
        notifier.publish("task.overdue", {"kind": "system", "id": item["task_id"]}, item, f"{item['task_id']}:{item['period_key']}",
                         source={"host": "client", "task_id": "watchdog"}, dispatch=False)
    return {"overdue": [item["task_id"] for item in overdue]}


@task("notify.digest", "通知摘要", {"kind": "cron", "expr": "35 8,21 * * *", "days": "trading", "window_min": 30}, queue="notify", timeout_s=60, max_attempts=1, misfire="skip")
def digest(ctx):
    from app.notify import digest as D
    from app.notify import pipeline

    label = "早间摘要" if clocks.from_ms(ctx.now_ms()).hour < 12 else "晚间摘要"
    return D.build(pipeline.current(), label)


@task("maintenance", "清理过期记录", {"kind": "daily", "at": "03:20", "days": "any", "window_min": 600}, queue="light", timeout_s=300, max_attempts=2, catchup_ttl_min=24 * 60)
def maintenance(ctx):
    from app.notify import pipeline
    from app.scheduler import runtime

    out = {"notify": pipeline.current().store.purge(ctx.now_ms())}
    rt = runtime.current(create=False)
    if rt is not None:
        out["scheduler"] = rt.sched.store.purge(ctx.now_ms())
    return out
