"""把收件箱里的通知交给各页面的通知抽屉。页面自己算的条目（到价、改判、日历…）照旧，这里只补上收件箱里的新通知，
并在页面标已读时同步标收件箱。收件箱是唯一的事实来源，页面不另存一份。"""

from __future__ import annotations

import json
from datetime import date, datetime

from app.notify.store import NotifyStore
from app.scheduler import clock as clocks

PREFIX = "inbox:"
SYSTEM_TYPES = ("task.failed", "task.overdue", "task.completed")
# 事件类型 → 页面抽屉里的分类
KINDS = {
    "price.alert.fired": "alert", "price.move": "move", "limit.hit": "move", "price.extreme": "move",
    "judgment.changed": "change", "reason.status_changed": "change", "wish.condition_met": "change", "flow.streak": "move",
    "announcement.published": "ann", "news.item": "ann", "calendar.upcoming": "upcoming", "outcome.settled": "settle",
    "task.failed": "system", "task.overdue": "system", "task.completed": "system", "digest.summary": "system",
}


def _rows(store: NotifyStore, limit: int = 120) -> list[dict]:
    with store._conn() as conn:
        rows = conn.execute(
            "SELECT n.*, e.dedupe_key AS dedupe_key, e.payload AS payload, e.trade_date AS trade_date FROM notification n JOIN event e ON e.event_id=n.event_id"
            " WHERE n.archived_at IS NULL ORDER BY n.created_at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for row in rows:
        item = store._row(row)
        item["payload"] = json.loads(item["payload"]) if isinstance(item["payload"], str) else item["payload"]
        out.append(item)
    return out


def for_holdings(store: NotifyStore, codes: set[str]) -> list[dict]:
    """持仓页：自己关注的股票的通知，加上系统通知（任务出错）。"""
    return [i for i in _rows(store) if (i["type"] in SYSTEM_TYPES) or (i["subject_id"] in codes and i["type"] in KINDS)]


def for_market(store: NotifyStore) -> list[dict]:
    """市场页：指向市场页的通知，加上系统通知。generic.notice 里只要简报：市场的变化已经由页面自己的事件列表展示，不重复。"""
    out = []
    for i in _rows(store):
        if i["type"] in SYSTEM_TYPES or i["type"].startswith("market.") or (i["type"] == "generic.notice" and ":market:brief:" in i["dedupe_key"]):
            out.append(i)
    return out


def view(item: dict, today: date, zone=clocks.ZONE) -> dict:
    """统一的展示字段。页面按自己的字段名取；today 是页面认为的今天。"""
    created = datetime.fromtimestamp(item["created_at"] / 1000, zone)
    days = (today - created.date()).days
    group = "今天" if days <= 0 else "昨天" if days == 1 else "更早"
    return {
        "id": PREFIX + item["notif_id"], "kind": KINDS.get(item["type"], "other"), "group": group, "title": item["title"], "text": item["body"].split("\n")[0],
        "sub": "\n".join(item["body"].split("\n")[1:3]), "tone": item["tone"], "code": item["subject_id"] if item["type"] not in SYSTEM_TYPES else "",
        "day": created.date().isoformat(), "when": created.strftime("%H:%M" if group == "今天" else "%m-%d %H:%M"),
        "time": created.strftime("%H:%M" if group == "今天" else "%m-%d %H:%M"), "unread": item["read_at"] is None, "link": item["link"], "tab": (item["link"] or {}).get("tab", ""),
        "severity": item["severity"],
    }


def mark_read(store: NotifyStore, ids: list[str] | None, visible: list[dict], now_ms: int) -> int:
    """ids 里带 inbox: 前缀的是收件箱的；ids 为 None 表示「全部已读」，只标这个页面看得到的那些。"""
    if ids is None:
        targets = [i["notif_id"] for i in visible if i["read_at"] is None]
    else:
        targets = [i[len(PREFIX):] for i in ids if isinstance(i, str) and i.startswith(PREFIX)]
    return store.mark_read(targets, now_ms) if targets else 0
