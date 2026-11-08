"""自选列表（侧栏"诊断"）：每只股票一行 —— 现价涨跌，加一行最值得看的状态。

状态按优先级只取一条：离提醒价很近或已到价 → 判断刚变过 → 判断已过期 → 方向和位置。
列表按"需要你看的"排在前面：提醒、变化、正常、过期。
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from app.synthesis import policy
from app.synthesis.calendar import last_close_date, parse_day, workday_gap
from app.synthesis.store import Store
from app.tools.stock import Stock, stock_from_code

ALERT_NEAR = 0.05       # 离提醒价不到 5% 才在列表里提示
CHANGE_FRESH_DAYS = 2   # 判断变化只在最近这几个交易日内提示
_RANK = {"alert": 0, "change": 1, "state": 2, "stale": 3, "empty": 3}


def stock_of(row: dict) -> Stock:
    code, _, suffix = row["ts_code"].partition(".")
    return stock_from_code(code, suffix or None, row["name"])


def shown_page(record: dict, mode: str) -> dict:
    data = record["data"]
    return data["display_agent"] if (mode == "agent" and data.get("display_agent")) else data["display_rule"]


def _alert_line(alerts: list[dict], price: float | None) -> tuple[dict | None, dict | None]:
    """返回 (要展示的那条提醒, 它的距离信息)。已到价的优先，其次是离得最近的。"""
    if not price or not alerts:
        return None, None
    best, best_key = None, None
    for alert in alerts:
        reached = price <= alert["price"] if alert["side"] == "down" else price >= alert["price"]
        gap = abs(price - alert["price"]) / price
        key = (0 if reached else 1, gap)
        if best_key is None or key < best_key:
            best, best_key = alert, key
    reached = best_key[0] == 0
    gap = best_key[1]
    if not reached and gap > ALERT_NEAR:
        return None, None
    return best, {"reached": reached, "gap": round(gap, 4)}


def _change_text(points: list[dict], expected) -> str:
    """最近一个交易日的判断，和前一个有记录的交易日比，方向变了就说一句。"""
    if len(points) < 2:
        return ""
    previous, last = points[-2], points[-1]
    day = parse_day(last["trade_date"])
    if day is None or workday_gap(day, expected) > CHANGE_FRESH_DAYS:
        return ""
    if previous["lean"] == last["lean"]:
        return ""
    return f"从{policy.DIRECTION_TEXT[previous['lean']]}变为{policy.DIRECTION_TEXT[last['lean']]}"


def _subtitle(alert, near, change: str, stale: bool, empty: bool, direction: str, position: str) -> dict:
    if alert is not None:
        text = f"已到提醒价 {alert['price']:.2f}" if near["reached"] else f"离提醒价 {alert['price']:.2f} 差 {near['gap'] * 100:.1f}%"
        return {"kind": "alert", "text": text, "tone": "warn", "alert_id": alert["id"]}
    if empty:
        return {"kind": "empty", "text": "还没有判断", "tone": "muted"}
    if stale:
        return {"kind": "stale", "text": "判断已过期", "tone": "muted"}
    if change:
        return {"kind": "change", "text": change, "tone": "warn"}
    return {"kind": "state", "text": f"{direction} · {position}", "tone": "muted"}


def build(
    store: Store,
    quote_fn: Callable[[list[Stock]], dict],
    mode: str,
    now: datetime,
    only: list[str] | None = None,
) -> list[dict]:
    rows = [row for row in store.watch_list() if only is None or row["ts_code"] in only]
    stocks = [stock_of(row) for row in rows]
    quotes = quote_fn(stocks) if stocks else {}
    expected = last_close_date(now)
    items = []
    for order, (row, stock) in enumerate(zip(rows, stocks)):
        code = row["ts_code"]
        quote = quotes.get(code) or {}
        price = quote.get("price")
        record = store.latest(code)
        page = shown_page(record, mode) if record else None
        stale = bool(record) and record["trade_date"] < expected.isoformat()
        alert, near = _alert_line(store.alerts(code), price)
        change = _change_text(store.judgments(code), expected) if record else ""
        direction = policy.DIRECTION_TEXT.get((page or {}).get("direction", {}).get("lean"), "不明")
        position = (page or {}).get("position", {}).get("label", "适中")
        subtitle = _subtitle(alert, near, change, stale, record is None, direction, position)
        items.append({
            "ts_code": code,
            "name": row["name"],
            "price": price,
            "change": quote.get("change"),
            "change_pct": quote.get("change_pct"),
            "subtitle": subtitle,
            "direction": None if not page else {
                "lean": page["direction"]["lean"], "level": page["direction"]["level"], "label": direction,
            },
            "position": None if not page else {"value": page["position"]["value"], "label": position},
            "judged_at": (page or {}).get("generated_at"),
            "stale": stale,
            "computing": store.pending(code),
            "alert": None if alert is None else {
                "id": alert["id"], "side": alert["side"], "price": alert["price"],
                "gap": near["gap"], "reached": near["reached"],
            },
            "_order": (_RANK[subtitle["kind"]], order),
        })
    items.sort(key=lambda item: item.pop("_order"))
    return items
