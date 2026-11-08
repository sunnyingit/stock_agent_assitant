"""公告监控：拉最新公告（先问服务端的共享产物，没有就本机取并分类），按规则产出事件。设计 6.6。"""

from __future__ import annotations

import logging

from app.scheduler import clock as clocks
from app.scheduler.spec import task
from app.scheduler.tasks import alerts as alert_tasks

log = logging.getLogger("scheduler")


def items_from_server(code: str, day: str) -> list[dict] | None:
    """服务端采集好的 stock.news。没开离线转发、服务端没有、连不上，返回 None。"""
    from app.notify import pipeline, relay_client

    client = relay_client.RelayClient(pipeline.current())
    if not client.authoritative():
        return None
    try:
        status, body = client._call("GET", f"/v1/artifacts/stock.news/{code}")
    except Exception:
        return None
    return body.get("payload", {}).get("items") if status == 200 else None


def items_local(code: str) -> list[dict]:
    from app.services import announcements
    from app.services.news import fetch_announcements
    from app.synthesis.watchlist import stock_of

    return announcements.enrich(code, fetch_announcements(stock_of({"ts_code": code, "name": code}), 40))


def fetch_items(code: str, day: str) -> tuple[list[dict], str]:
    served = items_from_server(code, day)
    return (served, "server") if served is not None else (items_local(code), "local")


@task("news.watch", "公告监控", {"kind": "cron", "expr": "*/10 7-21 * * *", "days": "any", "window_min": 10},
      queue="data", fanout="subjects.watch_union", timeout_s=60, max_attempts=2, backoff_s=(60, 300), misfire="skip")
def watch(ctx):
    from app.notify import announce, pipeline

    notifier = pipeline.current()
    code = ctx.unit_key
    day = clocks.day_of(ctx.now_ms()).isoformat()
    tier = ctx.input.get("tier", "other")
    if notifier.store.rule(f"default-ann-{code}") is None:
        announce.ensure_default_rules(notifier, [{"ts_code": code, "tier": tier}], {})
    fetched = ctx.step("fetch", lambda: dict(zip(("items", "source"), fetch_items(code, day))))
    items, source = fetched["items"], fetched["source"]
    name = notifier.store.rule(f"default-ann-{code}")["params"].get("name", code)
    return {"ts_code": code, "source": source, **announce.evaluate(notifier, code, name, items, ctx.now_ms())}
