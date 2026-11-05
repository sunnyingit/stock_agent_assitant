"""调度器和通知的连接：任务运行结束时，有单元失败就产出一条聚合的 task.failed（不是每个单元一条）。"""

from __future__ import annotations

import logging

log = logging.getLogger("scheduler")
QUIET_TASKS = ("notify.",)      # 通知自己出错不再通知，避免循环


def on_run_finished(run: dict, spec, sched=None) -> None:
    """每次运行结束：成功、跳过、过期写一条 task.completed 回执（只进收件箱，不弹窗）；失败和部分失败写 task.failed。"""
    if run is None:
        return
    if run["state"] in ("failed", "partial"):
        _failed(run, spec, sched)
    else:
        _completed(run, spec, sched)


def _sched(sched):
    if sched is not None:
        return sched
    from app.scheduler import runtime

    rt = runtime.current(create=False)
    return rt.sched if rt is not None else None


def _completed(run: dict, spec, sched=None) -> None:
    from app.notify import pipeline
    from app.scheduler import report

    sched = _sched(sched)
    if sched is None:
        return
    receipt = report.completion(sched.store, run, spec, sched.effective(spec)["trigger"], "client")
    if receipt is None:
        return
    pipeline.current().publish("task.completed", {"kind": "system", "id": run["task_id"]}, receipt["payload"], receipt["dedupe_key"],
                               source={"host": "client", "task_id": run["task_id"], "run_id": run["run_id"]}, dispatch=False)


def _failed(run: dict, spec, sched=None) -> None:
    if run["task_id"].startswith(QUIET_TASKS):
        return
    from app.notify import pipeline

    counts = run.get("counts") or {}
    dead = counts.get("dead", 0) + counts.get("expired", 0)
    total = counts.get("jobs", 0)
    first_error = ""
    notifier = pipeline.current()
    try:
        sched = _sched(sched)
        if sched is not None:
            for job in sched.store.jobs_of(run["run_id"], "dead", limit=1):
                first_error = (job.get("error") or {}).get("message", "")
    except Exception:
        pass
    notifier.publish("task.failed", {"kind": "system", "id": run["task_id"]},
                     {"task_id": run["task_id"], "title": getattr(spec, "title", ""), "run_id": run["run_id"], "dead": dead, "total": total,
                      "first_error": first_error}, run["run_id"], source={"host": "client", "task_id": run["task_id"], "run_id": run["run_id"]})
