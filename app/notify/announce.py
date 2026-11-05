"""公告规则的评估（stream 模式）：每条新公告最多触发一次，靠 item_id 去重，靠 rule_state.cursor 记水位。设计 6.6。

默认规则按关注层：持仓 P0 立即、P1 单条、P2 只进收件箱；想买 P0、P1；自选只有 P0。
用户也可以自己建 announcement 规则（min_importance、categories、keywords_any、keywords_none）。
"""

from __future__ import annotations

from datetime import date

from app.notify.pipeline import Notifier
from app.scheduler import clock as clocks

TIER_SEVERITY = {
    "hold": {"P0": "urgent", "P1": "normal", "P2": "info"},
    "wish": {"P0": "urgent", "P1": "normal"},
    "other": {"P0": "urgent"},
}
RANK = {"P0": 0, "P1": 1, "P2": 2}
DAILY_NORMAL_CAP = 5
COLLAPSE_MS = 10 * 60_000


def _key(item: dict) -> str:
    return f"{item['published_at']}|{item['item_id']}"


def ensure_default_rules(notifier: Notifier, subjects: list[dict], names: dict[str, str]) -> dict:
    """每只关注的股票一条系统规则（记水位用）；不再关注的删掉。"""
    store, now = notifier.store, notifier.clock.now_ms()
    want = {f"default-ann-{s['ts_code']}": s for s in subjects}
    for rule_id, subject in want.items():
        rule = store.rule(rule_id)
        if rule is None or rule["params"].get("tier") != subject["tier"]:
            store.put_rule({"rule_id": rule_id, "type": "announcement", "subject_kind": "stock", "subject_id": subject["ts_code"], "mode": "stream", "origin": "system",
                            "params": {"tier": subject["tier"], "name": names.get(subject["ts_code"]) or subject["ts_code"]}, "rearm": {"kind": "never"}}, now)
    removed = 0
    for rule in store.rules("announcement", origin="system"):
        if rule["rule_id"] not in want:
            store.delete_rule(rule["rule_id"])
            removed += 1
    return {"rules": len(want), "removed": removed}


def user_match(params: dict, item: dict) -> bool:
    if RANK[item["importance"]] > RANK.get(params.get("min_importance", "P1"), 1):
        return False
    if params.get("categories") and item["category"] not in params["categories"]:
        return False
    if params.get("topics") and item["topic"] not in params["topics"]:
        return False
    title = item["title"]
    if params.get("keywords_any") and not any(word in title for word in params["keywords_any"]):
        return False
    if any(word in title for word in params.get("keywords_none") or []):
        return False
    return True


def _severity(rule: dict, item: dict) -> str | None:
    params = rule["params"]
    if rule["origin"] == "system":
        return TIER_SEVERITY.get(params.get("tier", "other"), {}).get(item["importance"])
    if not user_match(params, item):
        return None
    return {"P0": "urgent", "P1": "normal", "P2": "info"}[item["importance"]]


def evaluate(notifier: Notifier, ts_code: str, name: str, items: list[dict], now_ms: int | None = None) -> dict:
    """对这只股票的所有 announcement 规则评估一遍最新条目。返回 {new, events}。"""
    store = notifier.store
    now = now_ms or notifier.clock.now_ms()
    day = clocks.day_of(now).isoformat()
    out = {"new": 0, "events": 0, "initialized": 0}
    ordered = sorted(items, key=_key)
    for rule in store.rules("announcement", subject_id=ts_code, enabled_only=True):
        state = store.rule_state(rule["rule_id"])
        if state["cursor"] is None:                    # 第一次：记下水位，不把历史公告当新消息推一遍
            store.put_rule_state(rule["rule_id"], cursor=_key(ordered[-1]) if ordered else "", last_eval_at=now)
            out["initialized"] += 1
            continue
        fresh = [item for item in ordered if _key(item) > state["cursor"]]
        if not fresh:
            store.put_rule_state(rule["rule_id"], last_eval_at=now)
            continue
        out["new"] += len(fresh)
        buckets: dict[str, list[dict]] = {}
        for item in fresh:
            severity = _severity(rule, item)
            if severity:
                buckets.setdefault(severity, []).append(item)
        for severity, group in buckets.items():
            group.sort(key=lambda item: (RANK[item["importance"]], item["published_at"]))
            lead = group[0]
            if severity == "normal" and _normal_today(store, ts_code, day) >= DAILY_NORMAL_CAP:
                severity = "info"                        # 每只股票每天 normal 最多 5 条，超出的只进收件箱
            payload = {"ts_code": ts_code, "name": name or rule["params"].get("name") or ts_code, **{k: lead[k] for k in (
                "item_id", "title", "url", "source", "source_grade", "published_at", "category", "topic", "importance", "direction_hint", "matched")},
                "extra": len(group) - 1, "others": [g["title"] for g in group[1:3]]}
            made = notifier.publish("announcement.published", {"kind": "stock", "id": ts_code}, payload, f"ann:{lead['item_id']}", severity=severity,
                                    trade_date=day, source={"host": "client", "task_id": "news.watch"}, routing=rule.get("routing") or None, dispatch=False)
            out["events"] += made["status"] == "created"
        store.put_rule_state(rule["rule_id"], cursor=_key(ordered[-1]), last_eval_at=now)
    return out


def _normal_today(store, ts_code: str, day: str) -> int:
    since = clocks.at_time(date.fromisoformat(day), "00:00")
    return sum(1 for event in store.events("announcement.published", ts_code, since, 100) if event["severity"] == "normal")
