"""摘要：把一段时间里没有单独打扰用户的 normal 事件，合成一条 digest.summary。设计 7.2 第 4 条。"""

from __future__ import annotations

from app.notify.pipeline import Notifier
from app.scheduler import clock as clocks


def build(notifier: Notifier, label: str, since_ms: int | None = None) -> dict:
    """摘要开着才有。吸收收件箱里还没被摘要吸收的 normal 通知（按股票分组），本身也是一个事件，走同样的投递。"""
    store, now = notifier.store, notifier.clock.now_ms()
    if not store.settings().get("digest", {}).get("enabled"):
        return {"status": "disabled"}
    since = since_ms if since_ms is not None else now - 16 * 3600_000
    pending = [n for n in store.inbox(limit=500) if n["severity"] == "normal" and n["digest_id"] is None and n["created_at"] >= since and n["type"] != "digest.summary"]
    if not pending:
        return {"status": "empty"}
    groups: dict[str, list[dict]] = {}
    for item in pending:
        groups.setdefault(item["subject_id"] or "其他", []).append(item)
    lines = [f"{subject}：" + "；".join(i["title"] for i in items[:3]) + (f"…还有 {len(items) - 3} 条" if len(items) > 3 else "") for subject, items in groups.items()]
    counts: dict[str, int] = {}
    for item in pending:
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    made = notifier.publish("digest.summary", {"kind": "system", "id": "digest"}, {"period": label, "period_label": label, "items": lines, "counts": counts},
                            f"{clocks.day_of(now).isoformat()}:{label}", source={"host": "client", "task_id": "notify.digest"})
    if made["status"] == "created":
        with store._conn() as conn:
            for item in pending:
                conn.execute("UPDATE notification SET digest_id=? WHERE notif_id=?", (made["notification"]["notif_id"], item["notif_id"]))
    return {"status": made["status"], "absorbed": len(pending)}
