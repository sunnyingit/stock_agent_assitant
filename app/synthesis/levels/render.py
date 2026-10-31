"""价位文案：全部用模板拼，数字和后果都来自代码。"""

from __future__ import annotations

_DOWN_LEAD = {
    "close": "收盘跌破",
    "close_2d": "连续 2 日收盘跌破",
    "close_volume": "放量收盘跌破",
    "close_outflow": "收盘跌破",
}
_DOWN_TAIL = {"close_outflow": "，且大资金当日净流出"}
_UP_LEAD = {"close": "收盘突破", "close_volume": "放量收盘突破"}


def price_line(side: str, price: float, dist: float) -> str:
    if side == "down":
        return f"▼ {price:.2f} 再跌 {abs(dist) * 100:.1f}%"
    return f"▲ {price:.2f} 再涨 {abs(dist) * 100:.1f}%"


def condition_line(side: str, confirm: str, base_name: str, buffer: float) -> str:
    if side == "up":
        return f"{_UP_LEAD.get(confirm, '收盘突破')}{base_name}"
    below = f"下方 {buffer * 100:.0f}%" if buffer else ""
    return f"{_DOWN_LEAD.get(confirm, '收盘跌破')}{base_name}{below}{_DOWN_TAIL.get(confirm, '')}"


def consequence_line(text: str) -> str:
    return f"→ {text}" if text else ""
