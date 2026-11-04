"""市场页通知：从快照变化里生成事件，按设置筛选，记下来，需要的话推到企业微信。

只推状态变化，数字小幅波动不推。没配 webhook 时只记下来给页面看，不发，也不报错。
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.services import market_changes as changes
from app.services.market_store import MarketStore, get_store

_ZONE = ZoneInfo("Asia/Shanghai")
DEFAULTS = {
    "level": True, "gate": True, "etf_streak": True, "etf_n": 5, "margin_turn": True,
    "team": True, "watch_sector": True, "brief_pre": True, "brief_close": True, "push": True,
}
ETF_N_RANGE = (3, 10)
# 变化类型 → 受哪个开关管。signal 是三个信号的档位变化，算在「结论」里。
KIND_SWITCH = {"level": "level", "gate": "gate", "signal": "level", "margin_turn": "margin_turn", "team": "team", "watch_sector": "watch_sector"}
TAB_OF = {"level": "fund", "gate": "fund"}


def settings(store: MarketStore | None = None) -> dict:
    return {**DEFAULTS, **(store or get_store()).settings()}


def clean_settings(values: dict) -> dict:
    out = {}
    for key, value in (values or {}).items():
        if key not in DEFAULTS:
            continue
        if key == "etf_n":
            number = int(value)
            if not ETF_N_RANGE[0] <= number <= ETF_N_RANGE[1]:
                raise ValueError(f"etf_n 要在 {ETF_N_RANGE[0]}–{ETF_N_RANGE[1]} 之间")
            out[key] = number
        else:
            out[key] = bool(value)
    return out


def record(summary: dict, parts: dict, store: MarketStore | None = None, now: datetime | None = None, push=None) -> list[dict]:
    """存今天的快照；和上一个交易日比，生成还没记过的事件。返回这次新增的事件。"""
    store = store or get_store()
    now = now or datetime.now(_ZONE)
    snap = changes.snapshot_of(summary, parts)
    store.save_snapshot(snap["day"], snap)
    snaps = store.snapshots()
    earlier = [item for item in snaps if item["day"] < snap["day"]]
    conf = settings(store)
    found = []
    if earlier:
        found += [item for item in changes.diff(earlier[-1], snap, snaps) if conf.get(KIND_SWITCH.get(item["kind"], ""), False)]
    streak = snap["etf"].get("streak") or 0
    if conf["etf_streak"] and abs(streak) == conf["etf_n"]:
        word = "净申购" if streak > 0 else "被赎回"
        found.append({"kind": "etf_streak", "title": f"宽基 ETF 连续 {abs(streak)} 天{word}", "tag": f"第 {abs(streak)} 天",
                      "detail": f"档位目前是{changes.level_text(snap)}，点开看影响了哪些板块。", "tone": "good" if streak > 0 else "warn", "tab": "fund"})
    created = []
    for item in found:
        key = f"{snap['day']}:{item['kind']}:{item['title']}"
        if store.add_event(key, snap["day"], item["kind"], item["title"], item["detail"], item.get("tab") or TAB_OF.get(item["kind"], ""), item["tone"], now.timestamp()):
            created.append(item)
    if created and conf["push"]:
        for item in created:
            (push or _push)(f"market:{snap['day']}:{item['kind']}:{item['title']}", "市场 · " + item["title"], item["detail"])
    return created


def _push(key: str, title: str, text: str) -> None:
    try:
        from app.notify.emit import emit

        emit(key, title, text)
    except Exception:
        pass


def brief(session: str, summary: dict, view: dict) -> tuple[str, str]:
    """盘前简报和收盘复盘的标题和正文。"""
    con = summary["conclusion"]
    head = f"{con['level']}{con['tilt']}" if con["tilt"] else con["level"]
    signals = "；".join(f"{item['n']}{item['name']} {item['label']}" for item in con["signals"])
    lines = [f"档位 {head}。{signals}。", f"怎么做：{con['action']}"]
    if session == "close":
        changed = [f"{item['title']}（{item['tag']}）" for item in view.get("items", [])[:4]]
        lines.append("今天的变化：" + ("；".join(changed) if changed else "没有值得说的变化") + "。")
    return ("市场 · 盘前简报" if session == "pre" else "市场 · 收盘复盘"), "\n".join(lines)


def notifications(store: MarketStore | None = None, upcoming: list[dict] | None = None, today: date | None = None) -> dict:
    store = store or get_store()
    today = today or datetime.now(_ZONE).date()
    groups: dict[str, list[dict]] = {"今天": [], "昨天": [], "更早": []}
    for row in store.events(60):
        day = date.fromisoformat(row["day"])
        group = "今天" if day == today else "昨天" if (today - day).days == 1 else "更早"
        groups[group].append({
            "id": row["id"], "kind": "判断变化", "title": row["title"], "text": row["text"], "tab": row["tab"], "tone": row["tone"],
            "time": datetime.fromtimestamp(row["created_at"], _ZONE).strftime("%H:%M" if group == "今天" else "%m-%d %H:%M"), "unread": not row["read"],
        })
    return {
        "unread": store.unread(),
        "groups": [{"title": title, "items": items} for title, items in groups.items() if items],
        "upcoming": upcoming or [],
        "channel": _channel(),
    }


def _channel() -> str:
    try:
        from app.notify.emit import webhook

        return "企业微信群机器人" if webhook() else "页面内通知（没配企业微信 webhook，不外发）"
    except Exception:
        return "页面内通知"
