"""离线转发：服务端评估价格提醒，产出事件；设备在线只走事件流，离线或超时没确认才外发。设计第 8 节。"""

from __future__ import annotations

import logging

from app.notify import channels as chan
from app.notify import rules
from app.scheduler import clock as clocks
from app.scheduler.ids import ulid
from app.server import security
from app.server.store import ServerStore

log = logging.getLogger("relay")
ACK_TIMEOUT_MS = 180_000
STALE_MS = 120_000


def _event(alert: dict, price: float, now: int, trade_date: str, confirm: str, source: str) -> dict:
    return {
        "v": 1, "event_id": ulid(now), "type": "price.alert.fired", "subject": {"kind": "stock", "id": alert["ts_code"]},
        "occurred_at": now, "trade_date": trade_date, "severity": "urgent",
        "dedupe_key": f"relay:{alert['alert_id']}:{trade_date}",     # 和客户端本机评估用同一把键：两边先后触发也只入箱一次
        "source": {"task_id": "relay.evaluate", "host": "server"}, "ttl_s": 86400,
        "payload": {"alert_id": alert["alert_id"], "rule_id": alert["alert_id"], "ts_code": alert["ts_code"], "name": alert["label"] or alert["ts_code"],
                    "side": alert["side"], "trigger_price": alert["price"], "observed_price": price, "observed_at": now, "confirm": confirm, "source": source},
    }


def evaluate(store: ServerStore, quotes: dict[str, dict], now: int, confirm: str = "touch", closes: dict | None = None) -> dict:
    """触发状态以服务端为准。取不到、陈旧的观测不评估，也不当作没触发。"""
    out = {"checked": 0, "fired": 0, "skipped": 0}
    trade_date = clocks.day_of(now).isoformat()
    for alert in store.relay_alerts(confirm=confirm):
        code = alert["ts_code"]
        if confirm == "close":
            hit = (closes or {}).get(code)
            if not hit:
                out["skipped"] += 1
                continue
            day, price = hit
            if (alert["last_value"] or {}).get("trade_date") == day:
                continue
        else:
            quote = quotes.get(code) or {}
            price, day = quote.get("price"), trade_date
            if not price or price <= 0 or now - (quote.get("observed_at") or now) > STALE_MS:
                out["skipped"] += 1
                continue
        out["checked"] += 1
        holds = rules._side_holds(alert["side"], price, alert["price"])
        away = rules._away(alert["side"], price, alert["price"], (alert["rearm"] or {}).get("pct", 1.0))
        state = {"state": alert["state"], "fire_seq": alert["fire_seq"], "fired_at": alert["fired_at"], "last_value": alert["last_value"]}
        update, fired = rules.observe({"rearm": alert["rearm"], "cooldown_s": 0}, state, holds, now, day, away)
        update["last_value"] = {"price": price, "trade_date": day}
        if fired:
            added = store.add_relay_event(alert["device_id"], _event(alert, price, now, day, confirm, "server"), now)
            fired = added is not None
        store.update_relay_alert(alert["device_id"], alert["alert_id"], **update)
        out["fired"] += bool(fired)
    return out


def push_due(store: ServerStore, now: int, adapters=None, fernet=None) -> dict:
    """外发。设备在线（事件流连着）且事件还没超过 ack 超时：只走事件流，等客户端确认；
    设备离线、或超时仍未确认：走 only_offline 渠道；only_offline=0 的渠道总是外发。每个渠道每个事件只发一次。"""
    adapters = adapters or chan.ADAPTERS
    out = {"pushed": 0, "waiting": 0, "failed": 0}
    for item in store.unpushed_events():
        event, device = item["event"], item["device_id"]
        alert = next((a for a in store.relay_alerts(device) if a["alert_id"] == event["payload"].get("alert_id")), None)
        channels = store.relay_channels(device)
        wanted = [channels[cid] for cid in (alert or {}).get("channels", []) if cid in channels]
        online = store.online(device, now)
        acked = item["acked_at"] is not None
        waiting_for_ack = online and not acked and now - item["created_at"] < ACK_TIMEOUT_MS
        failed = False
        remaining = 0
        for channel in wanted:
            cid = channel["channel_id"]
            if cid in item["pushed_to"]:
                continue
            if channel["only_offline"] and acked:
                continue                              # 客户端已经收到，不用再打扰
            if channel["only_offline"] and waiting_for_ack:
                remaining += 1
                continue
            adapter = adapters.get(channel["type"])
            if adapter is None or adapter.pull:
                continue
            try:
                secret = security.decrypt(channel["secret_enc"], fernet)
                payload = event["payload"]
                title = f"{payload['name']} {'跌到' if payload['side'] == 'down' else '涨到'} {payload['trigger_price']:.2f}"
                body = f"现价 {payload['observed_price']:.2f}，已到你设定的 {payload['trigger_price']:.2f}。"
                adapter.send({"notif_id": event["event_id"], "title": title, "body": body}, {"channel_id": cid, "secret_ref": "literal:" + secret, "config": {}})
                store.mark_channel_pushed(item["seq"], cid)
                out["pushed"] += 1
            except Exception as exc:                 # 日志里不带渠道地址和密钥
                failed = True
                log.warning("离线转发失败 device=%s channel=%s：%s", device, cid, type(exc).__name__)
        if failed:
            store.mark_pushed(item["seq"], now, False)
            out["failed"] += 1
        elif remaining:
            out["waiting"] += 1
        else:
            store.mark_pushed(item["seq"], now, True)
    return out
