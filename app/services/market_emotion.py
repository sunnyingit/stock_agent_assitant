"""短线情绪：近 20 个交易日每天落在冰点、修复、发酵、高潮、退潮的哪一段。规则在 market_rules.emotion_stage。"""

from __future__ import annotations

from datetime import date

from app.services import market_rules as rules
from app.services import market_tape as tape

DAYS = 20


def stats(limit_rows: dict[str, list[dict]], pct_by_day: dict[str, dict[str, float]], days: list[str]) -> list[dict]:
    """days 升序。每天用当天的涨跌停名单，加上前一天涨停股今天的平均涨跌。"""
    out = []
    for index, day in enumerate(days):
        rows = limit_rows.get(day) or []
        if not rows:
            continue
        counts = {"U": 0, "D": 0, "Z": 0}
        height = 0
        for row in rows:
            kind = str(row.get("limit") or "")
            if kind in counts:
                counts[kind] += 1
            if kind == "U" and tape.num(row.get("limit_times")) is not None:
                height = max(height, int(tape.num(row.get("limit_times"))))
        follow = None
        if index > 0:
            prev = limit_rows.get(days[index - 1]) or []
            today = pct_by_day.get(day) or {}
            vals = [today[str(row.get("ts_code"))] for row in prev if str(row.get("limit") or "") == "U" and str(row.get("ts_code")) in today]
            follow = sum(vals) / len(vals) if vals else None
        tried = counts["U"] + counts["Z"]
        stat = {
            "date": tape.iso(day), "up": counts["U"], "down": counts["D"], "broken": counts["Z"], "height": height,
            "broken_rate": counts["Z"] / tried if tried else None, "follow": follow,
        }
        out.append(stat)
        stat["stage"] = rules.emotion_stage(out)
    return out


def build(today: date) -> dict:
    days = tape.open_dates(today, DAYS + 1)
    limit_rows, pct_by_day = {}, {}
    for day in days:
        rows = tape.cached(f"limit:{day}", lambda d=day: tape.pro().limit_list_d(trade_date=d))
        if not rows:
            continue
        limit_rows[day] = rows
        daily = tape.cached(f"daily:{day}", lambda d=day: tape.pro().daily(trade_date=d, fields="ts_code,close,pct_chg"))
        pct_by_day[day] = {str(row.get("ts_code") or ""): tape.num(row.get("pct_chg")) for row in daily if tape.num(row.get("pct_chg")) is not None}
    have = [day for day in days if day in limit_rows]
    rows = stats(limit_rows, pct_by_day, have)[-DAYS:]
    if not rows:
        return {"ok": False, "days": []}
    last = rows[-1]
    return {"ok": True, "stage": last["stage"], "days": rows, "last": last}
