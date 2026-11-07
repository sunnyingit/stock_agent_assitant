"""共享产物的生产：个股事实表（stock.facts）和市场快照（market.snapshot）。设计 4.2、4.4。

产物里只放确定性的部分，不放 LLM 输出（headlines 来自模型体检，剔除），也不放任何用户信息。
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from app.scheduler import clock as clocks
from app.scheduler.spec import Unit
from app.server.store import ServerStore, hash_of

log = logging.getLogger("server")
FACTS_SCHEMA = "facts@1"
SNAPSHOT_SCHEMA = "market@1"
DETAILS_SCHEMA = "details@1"


def facts_payload(table_dict: dict) -> dict:
    """事实表里确定性的部分。"""
    return {key: table_dict.get(key) for key in ("facts", "unavailable", "events", "candidates", "rule", "context")}


def _load_table(ts_code: str, now: datetime):
    from app.synthesis import service, worker
    from app.synthesis.watchlist import stock_of

    stock = stock_of({"ts_code": ts_code, "name": ts_code})
    return service.load_table(stock, worker.get_store(), now)[9]


def produce_facts(store: ServerStore, ts_code: str, as_of: str | None = None, session: str = "close", now_ms: int | None = None,
                  loader=None) -> dict:
    from app.synthesis import policy
    from app.synthesis.calendar import last_close_date

    now_ms = now_ms or clocks.Clock().now_ms()
    now = clocks.from_ms(now_ms)
    day = as_of or last_close_date(now).isoformat()
    table = (loader or _load_table)(ts_code, now)
    payload = facts_payload(table.as_dict() if hasattr(table, "as_dict") else table)
    return store.put_artifact("stock.facts", ts_code, day, session, FACTS_SCHEMA, payload, now_ms, policy_version=policy.POLICY_VERSION)


def _json_safe(value):
    import json

    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def collect_details(ts_code: str, now: datetime) -> dict:
    """五个维度的详情和对齐后的日线，都是确定性的计算，所有用户一样。"""
    from app.synthesis import policy
    from app.synthesis.producer.collect import collect
    from app.synthesis.watchlist import stock_of

    stock = stock_of({"ts_code": ts_code, "name": ts_code})
    bundle = collect(stock, now, None, None, policy.COLLECT_TIMEOUT_BACKGROUND)
    return {"payloads": bundle.payloads, "errors": bundle.errors, "aligned": bundle.aligned, "timed_out": sorted(bundle.timed_out)}


def produce_details(store: ServerStore, ts_code: str, as_of: str | None = None, session: str = "close", now_ms: int | None = None, collector=None) -> dict:
    """个股五维详情（stock.details）。不含任何模型输出，也不含用户信息；LLM 分析师在客户端本机读它。"""
    from app.synthesis import policy
    from app.synthesis.calendar import last_close_date

    now_ms = now_ms or clocks.Clock().now_ms()
    now = clocks.from_ms(now_ms)
    day = as_of or last_close_date(now).isoformat()
    body = _json_safe((collector or collect_details)(ts_code, now))
    return store.put_artifact("stock.details", ts_code, day, session, DETAILS_SCHEMA, body, now_ms, policy_version=policy.POLICY_VERSION)


def details_on_demand(store: ServerStore, subject: str, as_of: str | None, session: str | None) -> dict | None:
    return produce_details(store, subject, as_of, session or "close")


def on_demand(store: ServerStore, subject: str, as_of: str | None, session: str | None) -> dict | None:
    """客户端要的这只股票没预热：现算并缓存。第一次慢，之后命中。"""
    return produce_facts(store, subject, as_of, session or "close")


def produce_market(store: ServerStore, day: str, now_ms: int, builder=None) -> dict:
    from app.services import market_page

    when = date.fromisoformat(day)
    if builder is None:
        market_page.refresh(when)
        builder = market_page.build_summary
    summary = builder(when)
    return store.put_artifact("market.snapshot", "market", day, "close", SNAPSHOT_SCHEMA, summary, now_ms)


def union_fanout(store: ServerStore):
    """subscriptions.union：有设备持有的先，其次订阅设备数多的。"""
    def provide(ctx) -> list[Unit]:
        rows = store.subscription_union(ctx.now_ms)
        return [Unit(row["ts_code"], 90 if row["held"] else min(80, 40 + row["devices"] * 5), {"held": row["held"], "devices": row["devices"]}) for row in rows]
    return provide


NEWS_SCHEMA = "news@1"
NEWS_KEEP_MS = 7 * 86400_000


def produce_news(store: ServerStore, ts_code: str, day: str, now_ms: int, fetcher=None) -> dict:
    """采集并规则分类，结果追加到当天的 stock.news 产物里（按 item_id 去重，只留最近 7 天）。不含 LLM 输出。"""
    from app.services import announcements

    if fetcher is None:
        from app.services.news import fetch_announcements
        from app.synthesis.watchlist import stock_of

        stock = stock_of({"ts_code": ts_code, "name": ts_code})
        fetcher = lambda: fetch_announcements(stock, 40)
    fresh = announcements.enrich(ts_code, fetcher())
    old = store.artifact("stock.news", ts_code, day, "day", NEWS_SCHEMA)
    merged = {item["item_id"]: item for item in ((old or {}).get("payload") or {}).get("items", [])}
    merged.update({item["item_id"]: item for item in fresh})
    cutoff = clocks.from_ms(now_ms - NEWS_KEEP_MS).strftime("%Y-%m-%d")
    items = sorted((i for i in merged.values() if i["published_at"][:10] >= cutoff), key=lambda i: (i["published_at"], i["item_id"]))
    return store.put_artifact("stock.news", ts_code, day, "day", NEWS_SCHEMA, {"items": items}, now_ms, ttl_days=7)
