"""企业微信群机器人。只负责把一段文字发出去。"""

from __future__ import annotations

import json
import urllib.request


def send(webhook: str, content: str, timeout: float = 5) -> None:
    body = json.dumps({"msgtype": "markdown", "markdown": {"content": content}}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(webhook, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("errcode") not in (0, None):
        raise RuntimeError(payload.get("errmsg") or "企业微信没有发出去")
