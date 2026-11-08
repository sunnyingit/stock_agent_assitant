"""有事件就发企业微信。没配 webhook 时不发，也不报错。"""

from __future__ import annotations

import os

from app.notify.outbox import Outbox
from app.notify.wecom import send
from app.synthesis.calendar import now_cn

_outbox: Outbox | None = None


def webhook() -> str:
    import app.config  # 读入 .env 里的 WECOM_WEBHOOK_URL
    return os.environ.get("WECOM_WEBHOOK_URL", "").strip() or app.config.WECOM_WEBHOOK_URL


def outbox() -> Outbox:
    global _outbox
    if _outbox is None:
        _outbox = Outbox()
    return _outbox


def emit(key: str, title: str, text: str) -> dict:
    """key 同一天只发一次。返回 status：sent / duplicate / unconfigured / failed。"""
    day = now_cn().date().isoformat()
    full = f"{day}:{key}"
    box = outbox()
    if box.already(full):
        return {"status": "duplicate", "key": full}
    url = webhook()
    if not url:
        return {"status": "unconfigured", "key": full}
    content = f"**{title}**\n{text}"
    try:
        send(url, content)
    except Exception as exc:
        return {"status": "failed", "key": full, "error": str(exc)}
    box.mark(full)
    return {"status": "sent", "key": full}
