"""价格提醒碰到设定价时，发一条企业微信。"""

from __future__ import annotations

from app.notify.emit import emit


def reached(alert: dict, price: float) -> bool:
    if alert["side"] == "down":
        return price <= alert["price"]
    return price >= alert["price"]


def check_prices(store, quotes: dict[str, dict], names: dict[str, str]) -> list[dict]:
    from app.notify.emit import webhook
    if not webhook():
        return []
    sent = []
    for alert in store.alerts():
        quote = quotes.get(alert["ts_code"]) or {}
        price = quote.get("price")
        if not price or not reached(alert, price):
            continue
        name = names.get(alert["ts_code"]) or alert["ts_code"]
        side = "跌到" if alert["side"] == "down" else "涨到"
        title = f"{name} {side} {alert['price']:.2f}"
        text = f"{name}（{alert['ts_code']}）现价 {price:.2f}，已{side}设定的 {alert['price']:.2f}。"
        sent.append(emit(f"price:{alert['id']}", title, text))
    return sent
