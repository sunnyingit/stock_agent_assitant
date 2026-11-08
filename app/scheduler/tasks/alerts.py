"""价格提醒和通知投递的定时任务。原来提醒只在打开自选页时才检查，现在由调度器在交易时段每 3 分钟查一次。"""

from __future__ import annotations

import logging
import time
from datetime import date

from app.scheduler import clock as clocks
from app.scheduler.errors import NotReady
from app.scheduler.spec import gate, task

log = logging.getLogger("scheduler")


def _synthesis_store():
    from app.synthesis import worker
    return worker.get_store()


def sync_rules(notifier) -> dict:
    from app.notify import rules

    store = _synthesis_store()
    names = {row["ts_code"]: row["name"] for row in store.watch_list()}
    out = rules.sync_legacy_alerts(notifier, store.alerts(), names)
    try:
        subjects = watch_subjects()
        out["defaults"] = rules.ensure_default_rules(notifier, [item["ts_code"] for item in subjects if item["tier"] == "hold"], names)
        from app.notify import announce
        out["announcements"] = announce.ensure_default_rules(notifier, subjects, names)
    except Exception:
        pass
    return out


def _stocks(notifier, kinds: tuple) -> dict[str, str]:
    store = _synthesis_store()
    names = {row["ts_code"]: row["name"] for row in store.watch_list()}
    wanted = {}
    for kind in kinds:
        for rule in notifier.store.rules(kind, enabled_only=True):
            wanted[rule["subject_id"]] = rule["params"].get("name") or names.get(rule["subject_id"]) or rule["subject_id"]
    return wanted


def fetch_quotes(names: dict[str, str]) -> dict[str, dict]:
    from app.synthesis import quotes
    from app.synthesis.watchlist import stock_of

    stocks = [stock_of({"ts_code": code, "name": name}) for code, name in names.items()]
    return quotes.fetch(stocks) if stocks else {}


def fetch_closes(names: dict[str, str], trade_date: str) -> dict[str, tuple[str, float]]:
    """官方收盘价。参考的那只股票还没有当天的日线，说明数据没齐，抛 NotReady 让调度器过几分钟再来。"""
    from app.services.series import get_bars
    from app.synthesis.watchlist import stock_of

    out = {}
    for index, (code, name) in enumerate(names.items()):
        items = get_bars(stock_of({"ts_code": code, "name": name}), 5).get("items") or []
        if not items:
            continue
        last = items[-1]
        day = str(last.get("trade_date") or "")
        day = f"{day[:4]}-{day[4:6]}-{day[6:8]}" if len(day) == 8 else day[:10]
        if index == 0 and day < trade_date:
            raise NotReady("日线还没有今天的数据", retry_after_s=300)
        if day == trade_date and last.get("close"):
            out[code] = (day, float(last["close"]))
    return out


@gate("market_closed")
def market_closed(trade_date: str) -> bool:
    now = time.time() * 1000
    return now >= clocks.at_time(date.fromisoformat(trade_date), "15:20")


@task("alerts.intraday", "盘中价格提醒", {"kind": "session_interval", "every_min": 3, "days": "trading"},
      queue="light", min_interval_min=1, timeout_s=90, max_attempts=1, misfire="skip")
def intraday(ctx):
    from app.notify import pipeline, relay_client, rules

    notifier = pipeline.current()
    synced = ctx.step("sync", lambda: sync_rules(notifier))
    relay = relay_client.RelayClient(notifier)
    names = _stocks(notifier, ("price_alert", "price_move"))
    if not names:
        return {"watching": 0, **synced}
    quotes = ctx.step("quotes", lambda: {code: q for code, q in fetch_quotes(names).items()})
    # 服务端在线时价格提醒以它为准（它在电脑关机时也在评估）；连不上才由本机降级评估
    alerts = {"deferred_to_server": True} if relay.authoritative() else rules.evaluate_price_alerts(notifier, quotes, ctx.now_ms(), "touch", relay_mode=relay.enabled())
    moves = rules.evaluate_price_moves(notifier, quotes, ctx.now_ms())
    sent = notifier.dispatch()
    return {"watching": len(names), "alerts": alerts, "moves": moves, "delivered": sent}


@task("alerts.close", "收盘提醒", {"kind": "data_ready", "gate": "market_closed", "deadline": "23:00", "days": "trading"},
      queue="light", timeout_s=180, max_attempts=3, backoff_s=(300, 900, 1800), misfire="run_latest", catchup_ttl_min=24 * 60)
def close(ctx):
    from app.notify import pipeline, relay_client, rules

    notifier = pipeline.current()
    sync_rules(notifier)
    names = _stocks(notifier, ("price_alert",))
    if not names:
        return {"watching": 0}
    closes = ctx.step("closes", lambda: {code: list(v) for code, v in fetch_closes(names, ctx.trade_date).items()})
    relay = relay_client.RelayClient(notifier)
    alerts = {"deferred_to_server": True} if relay.authoritative() else rules.evaluate_price_alerts(
        notifier, {}, ctx.now_ms(), "close", {code: tuple(v) for code, v in closes.items()}, relay_mode=relay.enabled())
    return {"watching": len(names), "alerts": alerts, "delivered": notifier.dispatch()}


@task("notify.dispatch", "通知投递", {"kind": "cron", "expr": "* * * * *", "days": "any", "window_min": 2},
      queue="notify", timeout_s=60, max_attempts=1, misfire="skip", placement="client")
def dispatch(ctx):
    from app.notify import pipeline

    return pipeline.current().dispatch()


def watch_subjects() -> list[dict]:
    """关注的股票：持仓（hold）、想买和自选（other）。只上报代码和两档标签，不含数量、成本、理由。"""
    from app.services import holdings_calc
    from app.services.holdings_store import get_store as holdings_store

    holds = set()
    wishes = set()
    try:
        store = holdings_store()
        held, _ = holdings_calc.positions(store.trades())
        holds = set(held)
        wishes = {w["ts_code"] for w in store.wishes()}
    except Exception:
        pass
    watch = {row["ts_code"] for row in _synthesis_store().watch_list()}
    return [{"ts_code": code, "tier": "hold" if code in holds else ("wish" if code in wishes else "other")} for code in sorted(holds | wishes | watch)]


@task("relay.sync", "离线转发同步", {"kind": "cron", "expr": "*/2 * * * *", "days": "any", "window_min": 3},
      queue="light", timeout_s=60, max_attempts=1, misfire="skip")
def relay_sync(ctx):
    """把提醒定义和订阅同步给服务端，拉回服务端触发的事件。没开启离线转发就什么都不做。"""
    from app.notify import pipeline, relay_client

    relay = relay_client.RelayClient(pipeline.current())
    if not relay.enabled():
        return {"enabled": False}
    out = {"enabled": True}
    for name, call in (("alerts", relay.sync_alerts), ("subscriptions", lambda: relay.sync_subscriptions([{"ts_code": s["ts_code"], "tier": "hold" if s["tier"] == "hold" else "other"} for s in watch_subjects()])), ("events", relay.pull_events), ("server_tasks", relay.pull_system_events)):
        try:
            out[name] = call()
        except Exception as exc:        # 服务端暂时连不上：本机降级评估，不算任务失败
            out[name] = {"error": str(exc)[:120]}
    return out
