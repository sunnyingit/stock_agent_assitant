"""调度器和通知的连接：任务运行结束时，有单元失败就产出一条聚合的 task.failed（不是每个单元一条）。"""

from __future__ import annotations

import logging

log = logging.getLogger("scheduler")
QUIET_TASKS = ("notify.",)      # 通知自己出错不再通知，避免循环


def on_run_finished(run: dict, spec) -> None:
    if run is None or run["state"] not in ("failed", "partial") or run["task_id"].startswith(QUIET_TASKS):
        return
    from app.notify import pipeline

    counts = run.get("counts") or {}
    dead = counts.get("dead", 0) + counts.get("expired", 0)
    total = counts.get("jobs", 0)
    first_error = ""
    notifier = pipeline.current()
    try:
        from app.scheduler import runtime
        rt = runtime.current(create=False)
        if rt is not None:
            for job in rt.sched.store.jobs_of(run["run_id"], "dead", limit=1):
                first_error = (job.get("error") or {}).get("message", "")
    except Exception:
        pass
    notifier.publish("task.failed", {"kind": "system", "id": run["task_id"]},
                     {"task_id": run["task_id"], "title": getattr(spec, "title", ""), "run_id": run["run_id"], "dead": dead, "total": total,
                      "first_error": first_error}, run["run_id"], source={"host": "client", "task_id": run["task_id"], "run_id": run["run_id"]})
