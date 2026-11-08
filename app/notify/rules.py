"""规则评估：边沿触发的转移函数，和价格类评估器。设计 6.2、6.4、6.5。

observe 是纯函数：给定规则、状态、这次观测，返回新状态和要不要产出事件。评估器负责读数据、调 observe、发事件。
"""

from __future__ import annotations

import logging

from app.notify.pipeline import Notifier
from app.scheduler import clock as clocks

log = logging.getLogger("notify")
STALE_MS = 120_000
JUMP = 0.20


def observe(rule: dict, state: dict, holds: bool, now_ms: int, trade_date: str, away: bool = False) -> tuple[dict, bool]:
    """返回 (状态更新, 是否触发)。

    armed  + 条件成立 → fired（冷却内不触发）；fired + 满足 rearm → armed；suspended 不评估。
    away 由评估器算：价格离开触发价是否已经足够远（回差）。
    """
    if state["state"] == "suspended":
        return {}, False
    update: dict = {"last_eval_at": now_ms}
    if state["state"] == "armed":
        if not holds:
            return update, False
        cooldown = rule.get("cooldown_s", 0) * 1000
        if cooldown and state.get("fired_at") and now_ms - state["fired_at"] < cooldown:
            return update, False
        update.update(state="fired", fired_at=now_ms, fire_seq=state["fire_seq"] + 1)
        return update, True
    # fired
    kind = (rule.get("rearm") or {}).get("kind", "hysteresis")
    rearm = False
    if kind == "hysteresis":
        rearm = away or not holds and (rule.get("rearm") or {}).get("pct", 1.0) == 0
    elif kind == "next_day":
        fired_day = clocks.day_of(state["fired_at"]).isoformat() if state.get("fired_at") else ""
        rearm = bool(fired_day) and trade_date > fired_day
    if rearm:
        update.update(state="armed", rearm_at=now_ms)
        if holds:                       # 隔天重新待命时条件仍然成立：当场触发，不用再等下一轮
            again, fired = observe(rule, {**state, "state": "armed"}, holds, now_ms, trade_date, away)
            update.update(again)
            return update, fired
    return update, False


def _side_holds(side: str, price: float, trigger: float) -> bool:
    return price <= trigger if side == "down" else price >= trigger


def _away(side: str, price: float, trigger: float, pct: float) -> bool:
    if side == "down":
        return price >= trigger * (1 + pct / 100)
    return price <= trigger * (1 - pct / 100)


def sync_legacy_alerts(notifier: Notifier, alerts: list[dict], names: dict[str, str]) -> dict:
    """把原来 synthesis 库里的价格提醒同步成规则（设计 12 迁移）。库里删了，规则也跟着删。"""
    store, now = notifier.store, notifier.clock.now_ms()
    live = set()
    added = 0
    for alert in alerts:
        rule_id = f"alert-{alert['id']}"
        live.add(rule_id)
        if store.rule(rule_id) is None:
            added += 1
        store.put_rule({
            "rule_id": rule_id, "type": "price_alert", "subject_kind": "stock", "subject_id": alert["ts_code"], "origin": "legacy", "origin_ref": str(alert["id"]),
            "params": {"side": alert["side"], "price": float(alert["price"]), "confirm": alert.get("confirm") or "close",
                       "note": alert.get("base_name") or "", "name": names.get(alert["ts_code"]) or alert.get("base_name") or ""},
            "rearm": {"kind": "hysteresis", "pct": 1.0},
        }, now)
    removed = 0
    for rule in store.rules("price_alert", origin="legacy"):
        if rule["rule_id"] not in live:
            store.delete_rule(rule["rule_id"])
            removed += 1
    return {"added": added, "removed": removed}


def evaluate_price_alerts(notifier: Notifier, quotes: dict[str, dict], now_ms: int | None = None, confirm: str = "touch",
                          closes: dict[str, tuple[str, float]] | None = None, source: dict | None = None, relay_mode: bool = False) -> dict:
    """touch：用最新行情；close：用官方收盘价（closes = {ts_code: (trade_date, close)}），每个交易日只评估一次。"""
    store = notifier.store
    now = now_ms or notifier.clock.now_ms()
    out = {"checked": 0, "fired": 0, "skipped": 0}
    for rule in store.rules("price_alert", enabled_only=True):
        params = rule["params"]
        mode = "close" if params.get("confirm") == "close" else "touch"
        if mode != ("close" if confirm == "close" else "touch"):
            continue
        code, state = rule["subject_id"], store.rule_state(rule["rule_id"])
        if mode == "close":
            hit = (closes or {}).get(code)
            if not hit:
                out["skipped"] += 1
                continue
            trade_date, price = hit
            if (state.get("last_value") or {}).get("trade_date") == trade_date:
                continue                       # 这个交易日已经评估过
            observed_at = clocks.at_time(clocks.day_of(now), "15:00") if trade_date == clocks.day_of(now).isoformat() else now
        else:
            quote = quotes.get(code) or {}
            price = quote.get("price")
            trade_date = clocks.day_of(now).isoformat()
            observed_at = quote.get("observed_at") or now
            if not price or price <= 0 or now - observed_at > STALE_MS:
                out["skipped"] += 1            # 取不到、陈旧、停牌：不评估，也不当作没触发
                continue
            last = (state.get("last_value") or {})
            if last.get("price") and abs(price / last["price"] - 1) > JUMP:
                if abs(price / (last.get("candidate") or 1) - 1) > 0.02:
                    store.put_rule_state(rule["rule_id"], last_value={**last, "candidate": price})
                    out["skipped"] += 1        # 跳变超过 20%：先当异常丢掉一次，连续两次一致才采信
                    continue
        out["checked"] += 1
        holds = _side_holds(params["side"], price, params["price"])
        away = _away(params["side"], price, params["price"], (rule.get("rearm") or {}).get("pct", 1.0))
        update, fired = observe(rule, state, holds, now, trade_date, away)
        update["last_value"] = {"price": price, "trade_date": trade_date}
        if fired:
            result = notifier.publish(
                "price.alert.fired", {"kind": "stock", "id": code},
                {"alert_id": rule["origin_ref"] or rule["rule_id"], "rule_id": rule["rule_id"], "ts_code": code, "name": params.get("name") or code,
                 "side": params["side"], "trigger_price": params["price"], "observed_price": price, "observed_at": observed_at,
                 "confirm": mode, "note": params.get("note", ""), "source": (source or {}).get("name", "tencent")},
                f"relay:{rule['rule_id']}:{trade_date}" if relay_mode else f"{rule['rule_id']}:{update['fire_seq']}", occurred_at=now, trade_date=trade_date, routing=rule.get("routing") or None,
                source={"host": "client", "task_id": "alerts.intraday" if mode == "touch" else "alerts.close"}, dispatch=False)
            if result["status"] == "duplicate":
                fired = False
        store.put_rule_state(rule["rule_id"], **update)
        out["fired"] += bool(fired)
    return out


def evaluate_price_moves(notifier: Notifier, quotes: dict[str, dict], now_ms: int | None = None) -> dict:
    """相对昨收涨跌超过 X%（day_pct）。下一交易日重新待命。"""
    store = notifier.store
    now = now_ms or notifier.clock.now_ms()
    trade_date = clocks.day_of(now).isoformat()
    out = {"checked": 0, "fired": 0}
    for rule in store.rules("price_move", enabled_only=True):
        params = rule["params"]
        if params.get("metric", "day_pct") != "day_pct":
            continue
        code = rule["subject_id"]
        quote = quotes.get(code) or {}
        price, prev = quote.get("price"), quote.get("prev")
        if not price or not prev:
            continue
        pct = (price / prev - 1) * 100
        direction = params.get("direction", "both")
        threshold = abs(float(params["threshold"]))
        holds = (pct >= threshold and direction in ("up", "both")) or (pct <= -threshold and direction in ("down", "both"))
        state = store.rule_state(rule["rule_id"])
        update, fired = observe({**rule, "rearm": {"kind": "next_day"}}, state, holds, now, trade_date)
        out["checked"] += 1
        if fired:
            result = notifier.publish(
                "price.move", {"kind": "stock", "id": code},
                {"ts_code": code, "name": params.get("name") or code, "metric": "day_pct", "pct": round(pct, 2), "observed_price": price,
                 "prev_close": prev, "observed_at": now},
                f"{rule['rule_id']}:{trade_date}:{update['fire_seq']}", occurred_at=now, trade_date=trade_date, dispatch=False,
                source={"host": "client", "task_id": "alerts.intraday"})
            fired = result["status"] == "created"
        store.put_rule_state(rule["rule_id"], **update)
        out["fired"] += bool(fired)
    return out


def ensure_default_rules(notifier: Notifier, held: list[str], names: dict[str, str]) -> dict:
    """新增持仓默认带一条日内涨跌 ±7% 的提醒（用户可以关或删）；不再持有的，系统建的规则跟着删。
    用户改过的（enabled 被关、阈值被改）不会被覆盖：只在规则不存在时创建。自选和想买不自动建。"""
    store, now = notifier.store, notifier.clock.now_ms()
    want = {f"default-move-{code}": code for code in held}
    created = 0
    for rule_id, code in want.items():
        if store.rule(rule_id) is None:
            store.put_rule({"rule_id": rule_id, "type": "price_move", "subject_kind": "stock", "subject_id": code, "origin": "system",
                            "params": {"metric": "day_pct", "threshold": 7.0, "direction": "both", "name": names.get(code) or code},
                            "rearm": {"kind": "next_day"}}, now)
            created += 1
    removed = 0
    for rule in store.rules("price_move", origin="system"):
        if rule["rule_id"] not in want:
            store.delete_rule(rule["rule_id"])
            removed += 1
    return {"created": created, "removed": removed}
