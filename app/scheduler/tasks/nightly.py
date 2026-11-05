"""每晚个股分析：对持仓、想买、自选里的每只股票各跑一遍综合判断，判断变了就产出事件。设计 3.3.1、3.3.2。

每只股票一个单元（失败互不影响）；步骤有检查点，重试从失败那一步继续。LLM 在本机，用用户自己的密钥。
"""

from __future__ import annotations

import logging
import time

from app.scheduler import clock as clocks
from app.scheduler.errors import NotReady, Permanent
from app.scheduler.spec import Unit, fanout, gate, task
from app.scheduler.tasks import alerts as alert_tasks

log = logging.getLogger("scheduler")
_ready_cache: dict[tuple, tuple[float, bool | None]] = {}


def server_ready(trade_date: str, gate_name: str) -> bool | None:
    """先问服务端的就绪标记。没开离线转发、服务端连不上，返回 None，由本机自己判断。"""
    key = (trade_date, gate_name)
    hit = _ready_cache.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    result = None
    try:
        from app.notify import pipeline, relay_client

        client = relay_client.RelayClient(pipeline.current())
        if client.enabled():
            status, body = client.http("GET", client.config()["url"].rstrip("/") + f"/v1/ready?date={trade_date}", {}, None)
            if status == 200:
                result = bool(body.get("gates", {}).get(gate_name, {}).get("ready"))
    except Exception:
        result = None
    _ready_cache[key] = (time.time(), result)
    return result


@gate("daily_bars")
def daily_bars(trade_date: str) -> bool:
    ready = server_ready(trade_date, "daily_bars")
    return ready if ready is not None else alert_tasks.market_closed(trade_date)


@fanout("subjects.watch_union")
def watch_union(ctx):
    """持仓优先级最高，想买次之，自选最低。"""
    out = []
    for item in alert_tasks.watch_subjects():
        out.append(Unit(item["ts_code"], 90 if item["tier"] == "hold" else 50, {"tier": item["tier"]}))
    return out


# ---- 下面几个函数是和综合判断服务的接缝，测试里替换
def analyze(ts_code: str) -> dict:
    from app.synthesis import policy, worker
    from app.synthesis.service import compute
    from app.tools.stock import resolve_stock

    stock = resolve_stock(ts_code)
    if stock is None:
        raise Permanent(f"找不到股票 {ts_code}")
    reply = compute(stock, worker.get_store(), timeout=policy.COLLECT_TIMEOUT_BACKGROUND)
    if reply is None or reply.get("pending"):
        raise NotReady("维度还在整理", retry_after_s=90)
    internal = reply.get("internal") or {}
    return {"run_id": internal.get("run_id"), "reused": bool(internal.get("reused")), "name": getattr(stock, "name", ts_code),
            "facts_hash": internal.get("facts_hash"), "source": internal.get("source")}


def judgments(ts_code: str) -> list[dict]:
    from app.synthesis import worker

    return worker.get_store().judgments(ts_code)


@task("stock.nightly", "每晚个股分析", {"kind": "daily", "at": "20:30", "window_min": 210, "days": "trading"},
      queue="llm", fanout="subjects.watch_union", gates=("daily_bars",), timeout_s=900, max_attempts=3, backoff_s=(120, 600, 1800),
      after=({"task": "market.close", "mode": "soft", "timeout_min": 30},), budget={"resource": "llm", "per_day": {"calls": 300}},
      misfire="run_latest", catchup_ttl_min=720, skip_if_fresh="facts_hash", overdue_after_min=300)
def nightly(ctx):
    from app.notify import pipeline

    code = ctx.unit_key
    done = ctx.step("analyze", lambda: analyze(code))
    ctx.add_usage(calls=0 if done.get("reused") else 1)
    if done.get("run_id"):
        ctx.ref("judgment", f"run:{done['run_id']}")
    changed = ctx.step("diff", lambda: _diff(code, done, ctx.trade_date or clocks.day_of(ctx.now_ms()).isoformat(), ctx, pipeline.current()))
    return {"ts_code": code, "reused": done.get("reused"), "changed": bool(changed.get("changed")), "skipped_llm": bool(done.get("reused"))}


def _diff(code: str, done: dict, trade_date: str, ctx, notifier) -> dict:
    """和上一个交易日的判断比，方向或档位变了就产出 judgment.changed。"""
    points = [p for p in judgments(code) if p.get("trade_date") and p["trade_date"] <= trade_date]
    if len(points) < 2 or points[-1]["trade_date"] != trade_date:
        return {"changed": False}
    before, after = points[-2], points[-1]
    if (before["lean"], before.get("level")) == (after["lean"], after.get("level")):
        return {"changed": False}
    from app.synthesis import policy

    names = policy.DIRECTION_TEXT
    headline = f"从{names.get(before['lean'], before['lean'])}变为{names.get(after['lean'], after['lean'])}"
    made = notifier.publish(
        "judgment.changed", {"kind": "stock", "id": code},
        {"ts_code": code, "name": done.get("name") or code, "trade_date": trade_date, "from": {"lean": before["lean"], "level": before.get("level")},
         "to": {"lean": after["lean"], "level": after.get("level")}, "headline": headline},
        f"{code}:{trade_date}:{after['lean']}:{after.get('level')}", trade_date=trade_date, correlation_id=ctx.job["run_id"],
        source={"host": "client", "task_id": "stock.nightly", "run_id": ctx.job["run_id"], "job_id": ctx.job_id}, dispatch=False)
    if made["status"] == "created":
        ctx.ref("event", f"event:{made['event']['event_id']}")
    return {"changed": True, "headline": headline}
