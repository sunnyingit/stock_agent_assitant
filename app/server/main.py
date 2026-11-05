"""启动服务端。在项目目录执行: python -m app.server.main

profile=server：同一套调度内核，只领 placement=server 的任务；接口在 /v1，调度的查询控制接口在 /v1/admin/sched（需要管理员令牌）。
"""

from __future__ import annotations

import os

from flask import Flask

from app.scheduler import clock as clocks
from app.scheduler import runtime as rt
from app.scheduler.calendar import TradingCalendar
from app.scheduler.core import Scheduler
from app.scheduler.spec import Registry
from app.scheduler.store import SchedStore
from app.server import facts as F
from app.server import security, tasks
from app.server.api import bp as v1_bp
from app.server.store import ServerStore


def build(store: ServerStore | None = None, clock: clocks.Clock | None = None, sched_path=None, **deps) -> tuple[Flask, rt.Runtime]:
    """装配应用和运行时。deps 注入行情、探测器等，测试用。"""
    clock = clock or clocks.Clock()
    store = store or ServerStore()
    registry = Registry()
    registry.add_fanout("subscriptions.union", F.union_fanout(store))
    for gate in tasks.GATES:
        registry.add_gate(gate, lambda date, gate=gate: bool(store.ready(date).get(gate, {}).get("ready")))
    tasks.install(registry, tasks.build(store, **deps))
    sched = Scheduler(SchedStore(sched_path or os.environ.get("SERVER_SCHED_DB") or None), registry, TradingCalendar(), clock, profile="server", code_version=rt.code_version())
    runtime = rt.Runtime(sched)
    sched.listeners.append(lambda run, spec: record_run(store, sched, clock, run, spec))

    app = Flask(__name__)
    app.config.update(SERVER_STORE=store, SERVER_CLOCK=clock, ARTIFACT_PRODUCERS={"stock.facts": F.on_demand})
    app.register_blueprint(v1_bp)

    from flask import request

    from app.scheduler import api as sched_api

    # 复用客户端同一套 /sched 接口，挂在 /v1/admin/sched 下，整个前缀要管理员令牌
    rt.install(runtime)
    app.register_blueprint(sched_api.bp, name="admin_sched", url_prefix="/v1/admin/sched")
    app.before_request(lambda: security.admin_check() if request.path.startswith("/v1/admin/") else None)
    return app, runtime


def record_run(store: ServerStore, sched: Scheduler, clock: clocks.Clock, run: dict, spec) -> None:
    """服务端任务每次跑完，把回执写进系统事件流，客户端拉到后写进自己的收件箱。"""
    from app.scheduler import report
    from app.scheduler.ids import ulid

    now = clock.now_ms()
    if run["state"] in ("failed", "partial"):
        counts = run.get("counts") or {}
        first = next(iter(sched.store.jobs_of(run["run_id"], "dead", limit=1)), None)
        event = {"v": 1, "type": "task.failed", "dedupe_key": f"server:failed:{run['run_id']}", "occurred_at": now, "severity": "normal",
                 "payload": {"task_id": run["task_id"], "title": spec.title, "run_id": run["run_id"], "dead": counts.get("dead", 0) + counts.get("expired", 0),
                             "total": counts.get("jobs", 0), "first_error": ((first or {}).get("error") or {}).get("message", "")}}
    else:
        receipt = report.completion(sched.store, run, spec, sched.effective(spec)["trigger"], "server")
        if receipt is None:
            return
        event = {"v": 1, "type": "task.completed", "dedupe_key": receipt["dedupe_key"], "occurred_at": now, "severity": "info", "payload": receipt["payload"]}
    event["event_id"] = ulid(now)
    store.add_system_event(event, now)


def main() -> None:
    port = int(os.environ.get("PORT", "8100"))
    app, runtime = build()
    runtime.start()
    print(f"服务端: http://localhost:{port}/v1/health")
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
