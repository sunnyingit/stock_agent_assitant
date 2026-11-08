"""任务运行结束后的「回执」：把一次运行整理成 task.completed 事件的内容。客户端和服务端共用，
客户端直接写进收件箱，服务端写进事件流再由客户端拉取，这样不管任务在哪里跑，都能在收件箱里看到它真的执行了。

失败和部分失败由 task.failed 负责（会弹桌面通知），这里不重复。
"""

from __future__ import annotations

import json

from app.scheduler import clock as clocks

STATE_TEXT = {"succeeded": "成功", "skipped": "已跳过", "expired": "已过期", "partial": "部分失败", "failed": "失败"}
TRIGGER_TEXT = {"schedule": "定时", "catchup": "补跑", "manual": "手动", "event": "事件"}
REPORTED = ("succeeded", "skipped", "expired")


def is_frequent(trigger: dict) -> bool:
    """一天跑几十上百次的任务（每几分钟一次）：成功只在收件箱记当天第一次，免得刷屏。"""
    if trigger["kind"] == "session_interval":
        return True
    if trigger["kind"] == "cron":
        return not trigger["expr"].split()[0].isdigit()
    return False


def _summary(store, run_id: str) -> str:
    jobs = store.jobs_of(run_id, limit=3)
    if len(jobs) != 1:
        return ""
    summary = (jobs[0].get("output") or {}).get("summary")
    if summary in (None, "", {}):
        return ""
    text = summary if isinstance(summary, str) else json.dumps(summary, ensure_ascii=False)
    return text[:160]


def completion(store, run: dict, spec, trigger: dict, host: str) -> dict | None:
    """返回 {payload, dedupe_key}；这次运行不需要回执时返回 None。"""
    if run["state"] not in REPORTED:
        return None
    day = clocks.day_of(run.get("finished_at") or run.get("scheduled_for") or 0).isoformat()
    counts = run.get("counts") or {}
    started, finished = run.get("started_at"), run.get("finished_at")
    payload = {
        "task_id": run["task_id"], "title": spec.title, "run_id": run["run_id"], "state": run["state"], "state_text": STATE_TEXT[run["state"]],
        "trigger": run["trigger"], "trigger_text": TRIGGER_TEXT.get(run["trigger"], run["trigger"]), "period_key": run["period_key"],
        "scheduled_for": run["scheduled_for"], "started_at": started, "finished_at": finished,
        "duration_ms": finished - started if started and finished else None, "jobs": counts.get("jobs", 0), "succeeded": counts.get("succeeded", 0),
        "host": host, "note": run.get("note") or "", "summary": _summary(store, run["run_id"]),
    }
    frequent = is_frequent(trigger) and run["trigger"] in ("schedule", "catchup")
    key = f"{host}:daily:{run['task_id']}:{day}:{run['state']}" if frequent else f"{host}:{run['run_id']}"
    return {"payload": payload, "dedupe_key": key}
