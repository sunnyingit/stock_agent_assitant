"""客户端的离线转发对接：同步提醒定义和订阅到服务端，拉取服务端触发的事件，写进本机收件箱。设计第 8 节。

用户明确开启才工作（settings.relay.enabled）。渠道密钥只有 channel.relay=1 的才上传。
服务端在线（最近 10 分钟成功拉取过）时，价格提醒以服务端为准，本机不再评估，避免两边重复提醒；服务端不可达时本机降级评估。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

from app.notify.pipeline import Notifier
from app.notify import channels as chan

log = logging.getLogger("notify")
HEALTHY_MS = 10 * 60_000
_TOKEN_FILE = Path(__file__).resolve().parents[2] / "data" / "relay_token"


def default_http(method: str, url: str, headers: dict, body: dict | None, timeout: float = 8):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={**headers, **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            return exc.code, {}


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def load_token() -> str:
    try:
        import keyring
        token = keyring.get_password("stock-agent", "relay-token")
        if token:
            return token
    except Exception:
        pass
    env = os.environ.get("RELAY_TOKEN", "").strip()
    if env:
        return env
    try:
        return _TOKEN_FILE.read_text().strip()
    except OSError:
        return ""


def save_token(token: str) -> str:
    """优先系统钥匙串；没有就落一个只有本人可读（0600）的文件。返回存在哪。"""
    try:
        import keyring
        keyring.set_password("stock-agent", "relay-token", token)
        return "keychain"
    except Exception:
        _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_FILE.write_text(token)
        _TOKEN_FILE.chmod(0o600)
        return "file"


class RelayClient:
    def __init__(self, notifier: Notifier, http=None, token_loader=None):
        self.n = notifier
        self.http = http or default_http
        self.token_loader = token_loader or load_token

    # ---- 配置
    def config(self) -> dict:
        return self.n.store.settings()["relay"]

    def save(self, **changes) -> dict:
        return self.n.store.set_settings({"relay": {**self.config(), **changes}})["relay"]

    def enabled(self) -> bool:
        conf = self.config()
        return bool(conf["enabled"] and conf["url"] and self.token_loader())

    def authoritative(self) -> bool:
        """服务端最近还连得上：价格提醒以它为准。"""
        return self.enabled() and self.n.clock.now_ms() - self.config()["healthy_at"] <= HEALTHY_MS

    def _call(self, method: str, path: str, body: dict | None = None):
        conf = self.config()
        return self.http(method, conf["url"].rstrip("/") + path, {"Authorization": "Bearer " + self.token_loader()}, body)

    # ---- 注册和撤销
    def register(self, url: str) -> dict:
        status, body = self.http("POST", url.rstrip("/") + "/v1/devices", {}, {})
        if status != 201:
            raise RuntimeError(f"服务端注册失败（{status}）")
        where = save_token(body["token"])
        self.save(enabled=True, url=url.rstrip("/"), device_id=body["device_id"], rev=0, sub_rev=0, cursor=0, defs_hash="", subs_hash="", secrets={})
        return {"device_id": body["device_id"], "token_stored_in": where}

    def revoke(self) -> bool:
        ok = False
        if self.enabled():
            status, _ = self._call("DELETE", "/v1/devices/me")
            ok = status == 200
        self.save(enabled=False)
        return ok

    # ---- 同步提醒定义
    def definitions(self) -> tuple[list[dict], list[dict]]:
        store = self.n.store
        channels = [c for c in store.channels() if c["relay"] and c["enabled"] and c["type"] in ("wecom_webhook", "bark", "serverchan")]
        ids = [c["channel_id"] for c in channels]
        alerts = []
        for rule in store.rules("price_alert", enabled_only=True):
            routing = (rule.get("routing") or {}).get("channels")
            alerts.append({"alert_id": rule["rule_id"], "ts_code": rule["subject_id"], "side": rule["params"]["side"], "price": rule["params"]["price"],
                           "confirm": "close" if rule["params"].get("confirm") == "close" else "touch", "rearm": rule["rearm"],
                           "label": rule["params"].get("name") or rule["subject_id"], "channels": [i for i in ids if routing is None or i in routing]})
        out_channels = []
        for channel in channels:
            item = {"channel_id": channel["channel_id"], "type": channel["type"], "only_offline": True}
            out_channels.append(item)
        return alerts, out_channels

    def sync_alerts(self) -> dict:
        if not self.enabled():
            return {"skipped": "disabled"}
        alerts, channels = self.definitions()
        conf = self.config()
        secrets = dict(conf["secrets"])
        digests = {}
        for item in channels:       # 密钥变了才重新上传
            secret = chan.resolve_secret(self.n.store.channel(item["channel_id"]).get("secret_ref"))
            digests[item["channel_id"]] = _hash(secret)
            if secrets.get(item["channel_id"]) != digests[item["channel_id"]] and secret:
                item["secret"] = secret
        defs_hash = _hash([alerts, [{k: v for k, v in c.items() if k != "secret"} for c in channels], digests])
        if defs_hash == conf["defs_hash"]:
            return {"skipped": "unchanged"}
        rev = conf["rev"] + 1
        status, body = self._call("PUT", "/v1/relay/alerts", {"v": 1, "rev": rev, "alerts": alerts, "channels": channels})
        if status == 409:
            rev = body.get("error", {}).get("details", {}).get("rev", rev) + 1
            status, body = self._call("PUT", "/v1/relay/alerts", {"v": 1, "rev": rev, "alerts": alerts, "channels": channels})
        if status != 200:
            raise RuntimeError(f"同步提醒失败（{status}）")
        for item in channels:
            if "secret" in item:
                secrets[item["channel_id"]] = _hash(item["secret"])
        self.save(rev=rev, defs_hash=defs_hash, secrets=secrets)
        return {"synced": len(alerts), "rev": rev}

    def sync_subscriptions(self, codes: list[dict]) -> dict:
        if not self.enabled():
            return {"skipped": "disabled"}
        conf = self.config()
        digest = _hash(sorted(codes, key=lambda c: c["ts_code"]))
        if digest == conf["subs_hash"]:
            return {"skipped": "unchanged"}
        rev = conf["sub_rev"] + 1
        status, body = self._call("PUT", "/v1/subscriptions", {"v": 1, "rev": rev, "codes": codes})
        if status == 409:
            rev = body.get("error", {}).get("details", {}).get("rev", rev) + 1
            status, body = self._call("PUT", "/v1/subscriptions", {"v": 1, "rev": rev, "codes": codes})
        if status != 200:
            raise RuntimeError(f"同步订阅失败（{status}）")
        self.save(sub_rev=rev, subs_hash=digest)
        return {"synced": len(codes), "rev": rev}

    # ---- 拉取服务端触发的事件
    def pull_events(self) -> dict:
        if not self.enabled():
            return {"skipped": "disabled"}
        conf = self.config()
        status, body = self._call("GET", f"/v1/relay/events?after={conf['cursor']}")
        if status != 200:
            raise RuntimeError(f"拉取提醒事件失败（{status}）")
        seqs, created = [], 0
        # 服务端来的事件：外部渠道由服务端负责（在线只走事件流，没确认才外发），本机只写收件箱、弹桌面通知
        local_only = [c["channel_id"] for c in self.n.store.channels() if c["type"] in ("desktop", "inapp")]
        for item in body.get("items", []):
            event, payload = item["event"], item["event"]["payload"]
            made = self.n.publish("price.alert.fired", event["subject"], payload, event["dedupe_key"], occurred_at=event["occurred_at"],
                                  trade_date=event.get("trade_date"), source={"host": "server", "task_id": "relay.evaluate"}, routing={"channels": local_only}, dispatch=False)
            created += made["status"] == "created"
            rule_id = payload.get("rule_id")
            if rule_id and self.n.store.rule(rule_id):         # 本机的规则状态跟着服务端走，页面上才显示「已触发」
                self.n.store.put_rule_state(rule_id, state="fired", fired_at=event["occurred_at"], last_value={"price": payload.get("observed_price"), "trade_date": event.get("trade_date")})
            seqs.append(item["seq"])
        if seqs:
            self._call("POST", "/v1/relay/ack", {"seqs": seqs})
            self.save(cursor=max(seqs))
        self.save(healthy_at=self.n.clock.now_ms())
        self.n.dispatch()
        return {"events": len(seqs), "created": created}
