"""调度的查询和控制接口：/sched/...  设计见 10.2、10.3。只绑本机，数据来自 scheduler.db。"""

from __future__ import annotations

import json
import logging
import time

from flask import Blueprint, Response, jsonify, request

from app.scheduler import clock as clocks
from app.scheduler import runtime, triggers

bp = Blueprint("sched", __name__, url_prefix="/sched")
log = logging.getLogger("scheduler")


def _rt() -> runtime.Runtime:
    return runtime.current()


def _err(status: int, code: str, message: str, **details):
    return jsonify({"error": {"code": code, "message": message, "details": details}}), status


def _page(items: list, limit: int, offset: int):
    page = items[offset:offset + limit]
    more = len(items) > offset + limit
    return {"items": page, "next_cursor": str(offset + limit) if more else None}


def _paging() -> tuple[int, int]:
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 500))
        offset = max(0, int(request.args.get("cursor") or 0))
    except ValueError:
        limit, offset = 50, 0
    return limit, offset


def _pick(item, fields: str | None):
    if not fields or not isinstance(item, dict):
        return item
    keep = {name.strip() for name in fields.split(",") if name.strip()}
    return {key: value for key, value in item.items() if key in keep}


def _run_view(run: dict) -> dict:
    return {key: run[key] for key in ("run_id", "task_id", "period_key", "trigger", "scheduled_for", "state", "counts", "started_at", "finished_at", "note", "code_version")}


def _job_view(job: dict) -> dict:
    keep = ("job_id", "run_id", "task_id", "unit_key", "period_key", "queue", "state", "priority", "attempt", "max_attempts", "due_at", "expire_at", "input", "output", "error", "updated_at")
    return {key: job.get(key) for key in keep}


# ------------------------------------------------------------ 任务
@bp.route("/tasks", methods=["GET"])
def tasks():
    sched = _rt().sched
    out = [{**sched.describe(spec), "v": 1} for spec in sched.tasks()]
    return jsonify({"items": [_pick(item, request.args.get("fields")) for item in out], "next_cursor": None})


@bp.route("/tasks/<task_id>", methods=["GET"])
def task_detail(task_id: str):
    sched = _rt().sched
    try:
        spec = sched.registry.get(task_id)
    except KeyError:
        return _err(404, "not_found", "没有这个任务：" + task_id)
    out = sched.describe(spec)
    out.update({"timeout_s": spec.timeout_s, "max_attempts": spec.max_attempts, "backoff_s": list(spec.backoff_s), "misfire": spec.misfire,
                "catchup_ttl_min": spec.catchup_ttl_min, "fanout": spec.fanout, "priority": spec.priority,
                "recent_runs": [_run_view(run) for run in sched.store.runs(task_id, limit=10)]})
    return jsonify(out)


@bp.route("/tasks/<task_id>/config", methods=["PUT"])
def put_config(task_id: str):
    sched = _rt().sched
    try:
        spec = sched.registry.get(task_id)
    except KeyError:
        return _err(404, "not_found", "没有这个任务：" + task_id)
    body = request.get_json(silent=True) or {}
    trigger = None
    if body.get("trigger") is not None:
        try:
            trigger = triggers.validate(body["trigger"], spec.min_interval_min)
        except ValueError as exc:
            return _err(400, "invalid_trigger", str(exc))
        if trigger["kind"] != spec.trigger["kind"]:
            return _err(400, "invalid_trigger", f"这个任务只能用 {spec.trigger['kind']} 类型的触发器")
    enabled = body.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        return _err(400, "invalid_enabled", "enabled 必须是 true 或 false")
    before = sched.effective(spec)
    sched.store.set_override(task_id, sched.clock.now_ms(), enabled=enabled, trigger=trigger, params=body.get("params"))
    log.info("sched config changed task=%s before=%s after=%s", task_id, before, sched.effective(spec))
    return jsonify(sched.describe(spec))


@bp.route("/tasks/<task_id>/config", methods=["DELETE"])
def delete_config(task_id: str):
    sched = _rt().sched
    try:
        spec = sched.registry.get(task_id)
    except KeyError:
        return _err(404, "not_found", "没有这个任务：" + task_id)
    sched.store.clear_override(task_id)
    return jsonify(sched.describe(spec))


@bp.route("/tasks/<task_id>/preview", methods=["POST"])
def preview(task_id: str):
    sched = _rt().sched
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(sched.preview(task_id, body.get("trigger"), int(body.get("count", 5))))
    except KeyError:
        return _err(404, "not_found", "没有这个任务：" + task_id)
    except ValueError as exc:
        return _err(400, "invalid_trigger", str(exc))


@bp.route("/tasks/<task_id>/run", methods=["POST"])
def run_now(task_id: str):
    rt = _rt()
    body = request.get_json(silent=True) or {}
    try:
        out = rt.sched.manual_run(task_id, body.get("units"), body.get("input"), body.get("period_key"), bool(body.get("dry_run")))
    except KeyError:
        return _err(404, "not_found", "没有这个任务：" + task_id)
    except ValueError as exc:
        return _err(409, "conflict", str(exc))
    if not out.get("dry_run"):
        rt.wake("manual")
        out["run"] = _run_view(out["run"])
    return jsonify(out), 200 if out.get("dry_run") else 202


@bp.route("/tasks/<task_id>/pause", methods=["POST"])
@bp.route("/tasks/<task_id>/resume", methods=["POST"])
def pause_resume(task_id: str):
    sched = _rt().sched
    try:
        spec = sched.registry.get(task_id)
    except KeyError:
        return _err(404, "not_found", "没有这个任务：" + task_id)
    sched.store.set_override(task_id, sched.clock.now_ms(), enabled=request.path.endswith("/resume"))
    return jsonify(sched.describe(spec))


# ------------------------------------------------------------ 运行和单元
@bp.route("/runs", methods=["GET"])
def runs():
    store = _rt().sched.store
    limit, offset = _paging()
    states = tuple(filter(None, (request.args.get("state") or "").split(","))) or None
    rows = store.runs(request.args.get("task_id"), states, limit=limit + offset + 1,
                      since=_int(request.args.get("since")), until=_int(request.args.get("until")))
    return jsonify(_page([_pick(_run_view(row), request.args.get("fields")) for row in rows], limit, offset))


@bp.route("/runs/<run_id>", methods=["GET"])
def run_detail(run_id: str):
    store = _rt().sched.store
    run = store.run(run_id)
    if run is None:
        return _err(404, "not_found", "没有这个运行：" + run_id)
    out = _run_view(run)
    out["config"] = run["config"]
    out["jobs"] = store.job_counts(run_id)
    return jsonify(out)


@bp.route("/runs/<run_id>/jobs", methods=["GET"])
def run_jobs(run_id: str):
    store = _rt().sched.store
    if store.run(run_id) is None:
        return _err(404, "not_found", "没有这个运行：" + run_id)
    limit, offset = _paging()
    jobs = store.jobs_of(run_id, request.args.get("state"), request.args.get("unit_key"), limit=limit + offset + 1)
    return jsonify(_page([_job_view(job) for job in jobs], limit, offset))


@bp.route("/jobs/<job_id>", methods=["GET"])
def job_detail(job_id: str):
    store = _rt().sched.store
    job = store.job(job_id)
    if job is None:
        return _err(404, "not_found", "没有这个单元：" + job_id)
    out = _job_view(job)
    out["steps"] = [{"step": s["step"], "state": s["state"], "tokens": s["tokens"], "output_ref": s["output_ref"]} for s in store.steps(job_id)]
    out["attempts"] = [_attempt_view(a) for a in store.attempts(job_id)]
    out["lease"] = {"owner": job.get("lease_owner"), "until": job.get("lease_until")}
    return jsonify(out)


def _attempt_view(a: dict) -> dict:
    out = {"attempt": a["attempt"], "state": a["state"], "started_at": a["started_at"], "ended_at": a["ended_at"], "error": a["error"],
           "tokens": a["tokens"], "calls": a["calls"]}
    out["duration_ms"] = None if a["ended_at"] is None else a["ended_at"] - a["started_at"]
    return out


@bp.route("/jobs/<job_id>/attempts", methods=["GET"])
def job_attempts(job_id: str):
    store = _rt().sched.store
    if store.job(job_id) is None:
        return _err(404, "not_found", "没有这个单元：" + job_id)
    return jsonify({"items": [_attempt_view(a) for a in store.attempts(job_id)], "next_cursor": None})


@bp.route("/jobs/<job_id>/steps/<step>", methods=["GET"])
def job_step(job_id: str, step: str):
    found = _rt().sched.store.step(job_id, step)
    if found is None:
        return _err(404, "not_found", "没有这一步")
    return jsonify({key: found[key] for key in ("step", "state", "attempt", "tokens", "output_ref", "updated_at")})


@bp.route("/jobs/<job_id>/retry", methods=["POST"])
def job_retry(job_id: str):
    rt = _rt()
    body = request.get_json(silent=True) or {}
    try:
        job = rt.sched.retry_job(job_id, body.get("from_step"))
    except KeyError:
        return _err(404, "not_found", "没有这个单元：" + job_id)
    except ValueError as exc:
        return _err(409, "conflict", str(exc))
    rt.wake("retry")
    return jsonify(_job_view(job)), 202


@bp.route("/jobs/<job_id>/cancel", methods=["POST"])
def job_cancel(job_id: str):
    try:
        job = _rt().sched.cancel_job(job_id)
    except KeyError:
        return _err(404, "not_found", "没有这个单元：" + job_id)
    return jsonify(_job_view(job))


# ------------------------------------------------------------ 结果、预算、健康
@bp.route("/results/<path:ref>", methods=["GET"])
def result(ref: str):
    store = _rt().sched.store
    prefix = ref.split(":", 1)[0]
    if prefix == "res":
        found = store.result(ref)
        if found is None:
            return _err(404, "not_found", "没有这个结果，可能已过保留期")
        return jsonify({"ref": ref, "kind": found["kind"], "content_type": found["content_type"], "size": found["size"], "content": found["content"]})
    if prefix == "run":
        try:
            from app.synthesis.worker import get_store as synthesis_store
            row = synthesis_store().run(int(ref.split(":", 1)[1]))
        except Exception:
            row = None
        if not row:
            return _err(404, "not_found", "没有这次综合判断")
        return jsonify({"ref": ref, "kind": "json", "content_type": "application/json", "content": row})
    return _err(404, "unsupported_ref", "暂不支持这种引用：" + prefix)


@bp.route("/budget", methods=["GET"])
def budget():
    store = _rt().sched.store
    resource = request.args.get("resource", "llm")
    window = request.args.get("window") or clocks.day_of(_rt().sched.clock.now_ms()).isoformat()
    return jsonify({"resource": resource, "window": window, "used": store.usage(resource, window)})


@bp.route("/health", methods=["GET"])
def health():
    rt = _rt()
    out = rt.sched.health()
    leader = rt.sched.store.state("leader") or {}
    out["leader"] = {"owner": leader.get("owner"), "me": leader.get("owner") == rt.exec.owner}
    out["running"] = bool(rt._thread and rt._thread.is_alive())
    return jsonify(out)


@bp.route("/tick", methods=["POST"])
def tick():
    """唤醒：电脑从休眠恢复、网络恢复、用户点了立即检查。"""
    rt = _rt()
    reason = request.args.get("reason") or (request.get_json(silent=True) or {}).get("reason") or "manual"
    return jsonify(rt.tick(reason))


@bp.route("/stream", methods=["GET"])
def stream():
    """SSE：有运行状态变化就推一条。页面做实时视图用；断开重连即可。"""
    store = _rt().sched.store

    def events():
        seen: dict[str, str] = {}
        idle = 0
        while idle < 120:
            changed = False
            for run in store.runs(limit=20):
                mark = f"{run['state']}:{json.dumps(run['counts'], sort_keys=True)}"
                if seen.get(run["run_id"]) != mark:
                    seen[run["run_id"]] = mark
                    changed = True
                    yield f"event: run\ndata: {json.dumps(_run_view(run), ensure_ascii=False)}\n\n"
            idle = 0 if changed else idle + 1
            if not changed:
                yield ": ping\n\n"
            time.sleep(2)

    return Response(events(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _int(value):
    try:
        return None if value in (None, "") else int(value)
    except ValueError:
        return None
