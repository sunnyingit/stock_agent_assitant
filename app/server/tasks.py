"""服务端任务（placement=server）：就绪探测、离线转发评估和外发、订阅并集预热、清理。

每个任务都写明为什么放在服务端：shared（对所有人一样）、no_private（不含用户私有数据）、costly（取数贵或限流）、offline（客户端关机时也要跑）。
"""

from __future__ import annotations

import logging

from datetime import timedelta

from app.scheduler import clock as clocks
from app.scheduler.errors import NotReady
from app.scheduler.spec import Registry, Unit, TaskSpec

log = logging.getLogger("server")
GATES = ("daily_bars", "limit_list", "moneyflow", "fund_share.d-1", "margin.d-1")
REFERENCE_STOCK = "000001.SZ"


def probe(gate: str, date: str) -> bool:
    """去 tushare 问一下这个门的数据出来没有。取不到当作没就绪。"""
    from app.tools.tushare_client import get_pro

    pro = get_pro()
    day = date.replace("-", "")
    if gate == "daily_bars":
        return len(pro.daily(ts_code=REFERENCE_STOCK, start_date=day, end_date=day)) > 0
    if gate == "limit_list":
        return len(pro.limit_list_d(trade_date=day, limit_type="U")) > 0
    if gate == "moneyflow":
        return len(pro.moneyflow(ts_code=REFERENCE_STOCK, start_date=day, end_date=day)) > 0
    prev = clocks.day_of(clocks.to_ms(date + " 12:00")) - timedelta(days=1)
    if gate == "fund_share.d-1":
        return len(pro.fund_share(ts_code="510300.SH", start_date=prev.strftime("%Y%m%d"), end_date=prev.strftime("%Y%m%d"))) > 0
    if gate == "margin.d-1":
        return len(pro.margin(trade_date=prev.strftime("%Y%m%d"))) > 0
    return False


def build(ctx_store, relay_quotes=None, relay_closes=None, prober=None, facts=None, snapshot=None, news=None, details=None) -> list[TaskSpec]:
    """把服务端任务装成 TaskSpec 列表。依赖（行情源、探测器）从参数注入，方便测试。"""
    from app.server import relay

    prober = prober or probe

    def ready_probe(ctx):
        now = ctx.now_ms()
        day = ctx.trade_date or clocks.day_of(now).isoformat()
        out = {}
        for gate in GATES:
            try:
                ok = bool(prober(gate, day))
            except Exception as exc:
                log.warning("探测 %s 失败：%s", gate, type(exc).__name__)
                ok = False
            ctx_store.set_ready(day, gate, ok, now)
            out[gate] = ok
        return {"date": day, "gates": out}

    def relay_evaluate(ctx):
        quotes = relay_quotes() if relay_quotes else {}
        return relay.evaluate(ctx_store, quotes, ctx.now_ms(), "touch")

    def relay_close(ctx):
        closes = relay_closes(ctx.trade_date) if relay_closes else {}
        if relay_closes and not closes and ctx_store.relay_alerts(confirm="close"):
            raise NotReady("日线还没出", retry_after_s=300)
        return relay.evaluate(ctx_store, {}, ctx.now_ms(), "close", closes)

    def relay_push(ctx):
        return relay.push_due(ctx_store, ctx.now_ms())

    def purge(ctx):
        out = ctx_store.purge(ctx.now_ms())
        for device in ctx_store.stale_devices(ctx.now_ms()):
            ctx_store.delete_device(device)
        return out

    specs = [
        TaskSpec("ready.probe", "数据就绪探测", ready_probe, {"kind": "cron", "expr": "*/10 15-23 * * *", "days": "trading", "window_min": 10},
                 placement="server", placement_reason="shared：所有用户共用同一份就绪标记，不用每个客户端各自去问数据源（costly）", queue="data", timeout_s=120, misfire="skip"),
        TaskSpec("relay.evaluate", "离线转发：盘中评估", relay_evaluate, {"kind": "session_interval", "every_min": 1, "days": "trading"},
                 placement="server", placement_reason="offline：客户端关机时价格提醒也要发出去", queue="light", timeout_s=60, max_attempts=1, misfire="skip", min_interval_min=1),
        TaskSpec("relay.close", "离线转发：收盘确认", relay_close, {"kind": "data_ready", "gate": "daily_bars", "deadline": "23:00", "days": "trading"},
                 placement="server", placement_reason="offline：收盘确认型提醒在客户端关机时也要评估", queue="light", timeout_s=120, max_attempts=3, backoff_s=(300, 900, 1800)),
        TaskSpec("relay.push", "离线转发：外发", relay_push, {"kind": "cron", "expr": "* * * * *", "days": "any", "window_min": 2},
                 placement="server", placement_reason="offline：设备离线或没确认时由服务端外发", queue="notify", timeout_s=60, max_attempts=1, misfire="skip"),
        TaskSpec("server.purge", "清理过期数据", purge, {"kind": "daily", "at": "03:30", "days": "any"},
                 placement="server", placement_reason="shared：服务端自己的库维护", queue="light", timeout_s=120),
    ]
    return specs + warm_specs(ctx_store, facts, snapshot, news, details)


def install(registry: Registry, specs: list[TaskSpec]) -> Registry:
    for spec in specs:
        registry.register(spec)
    return registry


def warm_specs(store, facts=None, snapshot=None, news=None, details=None) -> list[TaskSpec]:
    """订阅并集预热个股事实表；收盘后发布市场快照。skip_if_fresh：同一天同一口径已经有了就不重算。"""
    from app.server import facts as F

    def warm(ctx):
        day = ctx.trade_date or clocks.day_of(ctx.now_ms()).isoformat()
        if store.has_fresh("stock.facts", ctx.unit_key, day, "close", F.FACTS_SCHEMA):
            return {"skipped": "fresh", "ts_code": ctx.unit_key}
        made = ctx.step("facts", lambda: (facts or F.produce_facts)(store, ctx.unit_key, day, "close", ctx.now_ms()))
        return {"ts_code": ctx.unit_key, "facts_hash": made["facts_hash"]}

    def warm_details(ctx):
        day = ctx.trade_date or clocks.day_of(ctx.now_ms()).isoformat()
        if store.has_fresh("stock.details", ctx.unit_key, day, "close", F.DETAILS_SCHEMA):
            return {"skipped": "fresh", "ts_code": ctx.unit_key}
        made = ctx.step("details", lambda: (details or F.produce_details)(store, ctx.unit_key, day, "close", ctx.now_ms()))
        return {"ts_code": ctx.unit_key, "facts_hash": made["facts_hash"]}

    def publish_market(ctx):
        day = ctx.trade_date or clocks.day_of(ctx.now_ms()).isoformat()
        made = ctx.step("snapshot", lambda: (snapshot or F.produce_market)(store, day, ctx.now_ms()))
        return {"date": day, "facts_hash": made["facts_hash"]}

    def collect_news(ctx):
        day = ctx.trade_date or clocks.day_of(ctx.now_ms()).isoformat()
        made = ctx.step("collect", lambda: (news or F.produce_news)(store, ctx.unit_key, day, ctx.now_ms()))
        return {"ts_code": ctx.unit_key, "items": len(made["payload"]["items"]), "facts_hash": made["facts_hash"]}

    return [
        TaskSpec("news.collect", "采集公告并分类", collect_news, {"kind": "cron", "expr": "*/10 7-21 * * *", "days": "any", "window_min": 10},
                 placement="server", placement_reason="shared、no_private、costly：同一只股票的公告所有用户一样，集中采集省限流", queue="data", timeout_s=60,
                 max_attempts=2, backoff_s=(60, 300), fanout="subscriptions.union", misfire="skip"),
        TaskSpec("stock.facts.warm", "预热个股事实表", warm, {"kind": "data_ready", "gate": "daily_bars", "deadline": "23:30", "days": "trading"},
                 placement="server", placement_reason="shared、no_private、costly：同一只股票的事实表所有用户一样，集中算一次省限流", queue="data",
                 timeout_s=240, max_attempts=3, backoff_s=(120, 600, 1800), fanout="subscriptions.union", skip_if_fresh="facts_hash", gates=("daily_bars",)),
        TaskSpec("stock.details.warm", "预热个股五维详情", warm_details, {"kind": "data_ready", "gate": "daily_bars", "deadline": "23:30", "days": "trading"},
                 placement="server", placement_reason="shared、no_private、costly：五维详情是确定性的计算，所有用户一样，集中算一次省限流；分析师在客户端本机读它", queue="data",
                 timeout_s=300, max_attempts=3, backoff_s=(120, 600, 1800), fanout="subscriptions.union", skip_if_fresh="facts_hash", gates=("daily_bars",)),
        TaskSpec("market.publish", "发布市场快照", publish_market, {"kind": "data_ready", "gate": "daily_bars", "deadline": "23:30", "days": "trading"},
                 placement="server", placement_reason="shared、no_private、costly：市场页的计算所有用户一样", queue="data", timeout_s=600, max_attempts=3, backoff_s=(300, 900, 1800)),
    ]
