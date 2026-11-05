"""通知管线：事件 → 策略 → 收件箱 + 各渠道投递。设计 7.1。

publish() 只管「产生事件」的一侧：去重、写事件、写收件箱、排投递。
dispatch() 管「发出去」的一侧：到点的投递交给渠道适配器，失败按类别退避重试。
"""

from __future__ import annotations

import logging
import threading

from app.notify import channels as chan
from app.notify import policy, types
from app.notify.store import NotifyStore
from app.scheduler import clock as clocks
from app.scheduler.ids import ulid

log = logging.getLogger("notify")
BACKOFF_S = (60, 300, 1800)
MAX_ATTEMPTS = 3
DEFAULT_CHANNELS = (
    {"channel_id": "ch_desktop", "type": "desktop", "name": "桌面通知", "min_severity": "normal"},
)
LEGACY_WECOM = {"channel_id": "ch_wecom", "type": "wecom_webhook", "name": "企业微信群", "secret_ref": "env:WECOM_WEBHOOK_URL", "min_severity": "normal"}


class Notifier:
    def __init__(self, store: NotifyStore | None = None, clock: clocks.Clock | None = None, on_delivery=None):
        self.store = store or NotifyStore()
        self.clock = clock or clocks.Clock()
        self.on_delivery = on_delivery
        self._lock = threading.Lock()
        self.ensure_channels()

    def ensure_channels(self) -> None:
        for item in DEFAULT_CHANNELS:
            if self.store.channel(item["channel_id"]) is None:
                self.store.put_channel(dict(item))
        # 以前配过企业微信 webhook 的，自动变成一个渠道，行为不变
        if self.store.channel(LEGACY_WECOM["channel_id"]) is None and chan.resolve_secret(LEGACY_WECOM["secret_ref"]):
            self.store.put_channel(dict(LEGACY_WECOM))

    # ------------------------------------------------------------ 产生
    def publish(self, type_: str, subject: dict, payload: dict, dedupe_key: str, *, severity: str | None = None, occurred_at: int | None = None,
                trade_date: str | None = None, source: dict | None = None, correlation_id: str | None = None, ttl_s: int | None = None,
                routing: dict | None = None, dispatch: bool = True) -> dict:
        spec = types.get(type_)
        missing = [key for key in spec.required if key not in payload]
        if missing:
            raise ValueError(f"事件 {type_} 缺少字段：{'、'.join(missing)}")
        now = self.clock.now_ms()
        event = {
            "v": 1, "event_id": ulid(now), "type": type_, "subject": subject, "occurred_at": occurred_at or now,
            "trade_date": trade_date or clocks.day_of(occurred_at or now).isoformat(), "severity": severity or spec.severity,
            "dedupe_key": f"{type_}:{dedupe_key}", "source": source or {"host": "client"}, "correlation_id": correlation_id,
            "ttl_s": ttl_s if ttl_s is not None else spec.ttl_s, "payload": payload,
        }
        stored = self.store.add_event(event)
        if stored is None:
            return {"status": "duplicate", "dedupe_key": event["dedupe_key"]}
        title, body = spec.render(payload)
        link = spec.link(payload)
        group = clocks.day_of(now).isoformat()
        notification = self.store.add_notification(stored, title, body, spec.tone, link, group, now)
        collapsed = False
        if types.SEVERITY[stored["severity"]] == 1 and spec.group == "stock":
            window = self.store.settings().get("collapse_s", 600) * 1000
            collapsed = self.store.recent_similar(type_, subject["id"], now - window, stored["event_id"])
        decision = policy.decide(stored, spec, self.store.settings(), self.store.channels(), now, routing, collapsed)
        deliveries = self._plan(notification, decision, stored, now)
        if dispatch and any(item["state"] == "pending" and item["deliver_after"] <= now for item in deliveries):
            self.dispatch()
        return {"status": "created", "event": stored, "notification": notification, "decision": decision, "deliveries": deliveries}

    def _plan(self, notification: dict, decision: dict, event: dict, now: int) -> list[dict]:
        expires = None
        ttl = event.get("ttl_s")
        if ttl:
            expires = event["occurred_at"] + ttl * 1000
        out = []
        if decision["suppressed"] and not decision["deliver"]:
            return out
        for item in decision["deliver"]:
            made = self.store.add_delivery(notification["notif_id"], item["channel_id"], "pending", item["after"], expires)
            if made:
                out.append(made)
        return out

    def publish_text(self, key: str, title: str, text: str, *, severity: str = "normal", link: dict | None = None, dispatch: bool = True) -> dict:
        """旧代码 emit(key, title, text) 的落点：没有专门的事件类型时用通用通知。同一天同一个 key 只会有一条。"""
        day = clocks.day_of(self.clock.now_ms()).isoformat()
        return self.publish("generic.notice", {"kind": "market", "id": "market"}, {"title": title, "text": text, "link": link or {}},
                            f"{day}:{key}", severity=severity, dispatch=dispatch)

    # ------------------------------------------------------------ 发出去
    def dispatch(self, limit: int = 50) -> dict:
        """把到点的投递发出去。返回各状态的个数。拉取式渠道（桌面）不在这里发。"""
        out = {"sent": 0, "failed": 0, "dead": 0, "suppressed": 0, "waiting": 0}
        if not self._lock.acquire(blocking=False):
            return out
        try:
            now = self.clock.now_ms()
            for delivery in self.store.due_deliveries(now, limit):
                channel = self.store.channel(delivery["channel_id"])
                result = self._deliver(delivery, channel, now)
                out[result] = out.get(result, 0) + 1
        finally:
            self._lock.release()
        return out

    def _deliver(self, delivery: dict, channel: dict | None, now: int) -> str:
        did = delivery["delivery_id"]
        if delivery.get("expires_at") and now > delivery["expires_at"]:
            self.store.update_delivery(did, state="suppressed", suppressed_reason="expired")
            return "suppressed"
        if channel is None or not channel["enabled"]:
            self.store.update_delivery(did, state="suppressed", suppressed_reason="channel_disabled")
            return "suppressed"
        adapter = chan.ADAPTERS.get(channel["type"])
        if adapter is None:
            self.store.update_delivery(did, state="dead", last_error=f"没有 {channel['type']} 这种渠道")
            return "dead"
        if adapter.pull:
            return "waiting"
        limit = channel.get("rate_limit") or {}
        if limit.get("per_min") and self.store.sent_count(channel["channel_id"], now - 60_000) >= limit["per_min"]:
            self.store.update_delivery(did, state="suppressed", suppressed_reason="rate_limit")
            return "suppressed"
        if limit.get("per_day") and self.store.sent_count(channel["channel_id"], clocks.at_time(clocks.day_of(now), "00:00")) >= limit["per_day"]:
            self.store.update_delivery(did, state="suppressed", suppressed_reason="rate_limit")
            return "suppressed"
        notification = self.store.notification(delivery["notif_id"])
        attempts = delivery["attempts"] + 1
        self.store.update_delivery(did, state="sending", attempts=attempts)
        try:
            sent = adapter.send(notification, channel)
        except chan.DeliveryError as exc:
            return self._failed(delivery, channel, attempts, str(exc), exc.klass, now)
        except Exception as exc:
            return self._failed(delivery, channel, attempts, str(exc), "transient", now)
        self.store.update_delivery(did, state="sent", sent_at=self.clock.now_ms(), provider_msg_id=sent.get("provider_msg_id") or "", last_error=None)
        return "sent"

    def _failed(self, delivery: dict, channel: dict, attempts: int, message: str, klass: str, now: int) -> str:
        did = delivery["delivery_id"]
        if klass == "transient" and attempts < MAX_ATTEMPTS:
            self.store.update_delivery(did, state="pending", last_error=message[:300], next_attempt_at=now + BACKOFF_S[min(attempts - 1, len(BACKOFF_S) - 1)] * 1000)
            return "failed"
        self.store.update_delivery(did, state="dead", last_error=message[:300])
        log.warning("渠道 %s 不可用：%s", channel["channel_id"], message)
        if self.on_delivery:
            self.on_delivery("dead", channel, message)
        return "dead"

    # ------------------------------------------------------------ 桌面通知（拉取式）
    def desktop_pending(self, limit: int = 20) -> list[dict]:
        """给 Electron 取：桌面渠道里到点还没弹的通知。过期的当场标成 suppressed。"""
        now = self.clock.now_ms()
        out = []
        for delivery in self.store.due_deliveries(now, 200):
            channel = self.store.channel(delivery["channel_id"])
            if channel is None or channel["type"] != "desktop":
                continue
            if not channel["enabled"]:
                self.store.update_delivery(delivery["delivery_id"], state="suppressed", suppressed_reason="channel_disabled")
                continue
            if delivery.get("expires_at") and now > delivery["expires_at"]:
                self.store.update_delivery(delivery["delivery_id"], state="suppressed", suppressed_reason="expired")
                continue
            notification = self.store.notification(delivery["notif_id"])
            out.append({"delivery_id": delivery["delivery_id"], "notif_id": notification["notif_id"], "title": notification["title"], "body": notification["body"],
                        "severity": notification["severity"], "link": notification["link"], "created_at": notification["created_at"]})
            if len(out) >= limit:
                break
        return out

    def desktop_ack(self, delivery_ids: list[str]) -> int:
        now, count = self.clock.now_ms(), 0
        for did in delivery_ids:
            delivery = self.store.delivery(did)
            if delivery and delivery["state"] == "pending":
                self.store.update_delivery(did, state="sent", sent_at=now)
                count += 1
        return count

    # ------------------------------------------------------------ 测试消息
    def send_test(self, channel_id: str) -> dict:
        channel = self.store.channel(channel_id)
        if channel is None:
            raise KeyError("没有这个渠道：" + channel_id)
        adapter = chan.ADAPTERS.get(channel["type"])
        if adapter is None:
            raise ValueError("不支持的渠道类型：" + channel["type"])
        note = {"notif_id": "test", "title": "测试通知", "body": "收到这条说明渠道配置正确。", "severity": "normal", "link": {}}
        if adapter.pull:
            made = self.publish("generic.notice", {"kind": "system", "id": "system"}, {"title": note["title"], "text": note["body"]},
                                "test:" + ulid(), routing={"channels": [channel_id]})
            return {"ok": True, "pending": True, "status": made["status"]}
        try:
            adapter.send(note, channel)
        except chan.DeliveryError as exc:
            return {"ok": False, "error": str(exc), "class": exc.klass}
        self.store.put_channel({"channel_id": channel_id, "verified_at": self.clock.now_ms()})
        return {"ok": True}


_notifier: Notifier | None = None
_lock = threading.Lock()


def current() -> Notifier:
    global _notifier
    with _lock:
        if _notifier is None:
            _notifier = Notifier()
        return _notifier


def install(notifier: Notifier | None) -> None:
    global _notifier
    with _lock:
        _notifier = notifier
