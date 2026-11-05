"""渠道适配器：统一接口 send(notification, channel) -> {provider_msg_id}，失败抛带类别的异常。设计 7.4。

新增渠道 = 写一个适配器，在 ADAPTERS 里登记。
desktop 是「拉取式」：不由后端发，而是 Electron 来取、弹出来、再确认（见 api 的 /notify/desktop/...）。
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass

from app.notify import wecom


class DeliveryError(Exception):
    """klass: transient（超时、5xx、限流，可重试）或 permanent（密钥错、地址错）。"""

    def __init__(self, message: str, klass: str = "transient"):
        super().__init__(message)
        self.klass = klass


def resolve_secret(ref: str | None) -> str:
    """密钥不进库：库里只存引用。env:NAME 读环境变量或 .env；以后可以加 keychain:NAME。"""
    if not ref:
        return ""
    kind, _, name = ref.partition(":")
    if kind == "literal":              # 只有服务端转发在内存里用：解密后的密钥，不落库、不经接口
        return name
    if kind == "env":
        import os

        import app.config  # noqa: F401  读入 .env
        if name == "WECOM_WEBHOOK_URL":
            from app.notify.emit import webhook
            return webhook()
        return os.environ.get(name, "").strip()
    if kind == "keychain":
        try:
            import keyring
            return keyring.get_password("stock-agent", name) or ""
        except Exception:
            return ""
    return ""


def _post_json(url: str, body: dict, timeout: float = 6) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise DeliveryError(f"HTTP {exc.code}", "permanent" if exc.code in (400, 401, 403, 404) else "transient") from exc
    except Exception as exc:
        raise DeliveryError(str(exc)) from exc
    try:
        return json.loads(raw)
    except ValueError:
        return {}


@dataclass
class Adapter:
    pull: bool = False             # 拉取式：后端不主动发

    def send(self, notification: dict, channel: dict) -> dict:
        raise NotImplementedError


class Inapp(Adapter):
    def send(self, notification, channel):
        return {"provider_msg_id": notification["notif_id"]}


class Desktop(Adapter):
    def __init__(self):
        super().__init__(pull=True)


class WeComWebhook(Adapter):
    def send(self, notification, channel):
        url = resolve_secret(channel.get("secret_ref"))
        if not url:
            raise DeliveryError("没有配置企业微信 webhook", "permanent")
        content = f"**{notification['title']}**\n{notification['body']}"
        try:
            wecom.send(url, content)
        except RuntimeError as exc:
            raise DeliveryError(str(exc), "permanent") from exc
        except Exception as exc:
            raise DeliveryError(str(exc)) from exc
        return {"provider_msg_id": ""}


class Bark(Adapter):
    """Bark（iOS 推送）。secret_ref 指向设备 key，config.server 可换自建服务。"""

    def send(self, notification, channel):
        key = resolve_secret(channel.get("secret_ref"))
        if not key:
            raise DeliveryError("没有配置 Bark key", "permanent")
        server = (channel.get("config") or {}).get("server", "https://api.day.app").rstrip("/")
        out = _post_json(f"{server}/{key}", {"title": notification["title"], "body": notification["body"], "group": "stock-agent"})
        if out.get("code") not in (200, None):
            raise DeliveryError(str(out.get("message") or "Bark 没有发出去"), "permanent")
        return {"provider_msg_id": ""}


class ServerChan(Adapter):
    """Server酱：https://sctapi.ftqq.com/<sendkey>.send"""

    def send(self, notification, channel):
        key = resolve_secret(channel.get("secret_ref"))
        if not key:
            raise DeliveryError("没有配置 Server酱 SendKey", "permanent")
        body = urllib.parse.urlencode({"title": notification["title"], "desp": notification["body"]}).encode()
        request = urllib.request.Request(f"https://sctapi.ftqq.com/{key}.send", data=body)
        try:
            with urllib.request.urlopen(request, timeout=6) as response:
                out = json.loads(response.read().decode("utf-8", "replace"))
        except Exception as exc:
            raise DeliveryError(str(exc)) from exc
        if out.get("code") not in (0, None):
            raise DeliveryError(str(out.get("message") or "Server酱没有发出去"), "permanent")
        return {"provider_msg_id": str((out.get("data") or {}).get("pushid") or "")}


ADAPTERS: dict[str, Adapter] = {"inapp": Inapp(), "desktop": Desktop(), "wecom_webhook": WeComWebhook(), "bark": Bark(), "serverchan": ServerChan()}
