"""短线情绪：近 20 个交易日每天落在冰点、修复、发酵、高潮、退潮的哪一段。规则在 market_rules.emotion_stage。

阶段要看前一天落在哪，所以多取 WARMUP 天先把状态走起来，页面上只给最近 20 天。"""

from __future__ import annotations

from datetime import date

from app.services import market_rules as rules
from app.services import market_tape as tape

DAYS = 20
WARMUP = 10


THEME_TOP = 3
THEME_MIN = 3        # 昨天主线里至少 3 只涨停股，今天的表现才有代表性


def _industry(row: dict) -> str:
    return str(row.get("industry") or "").strip() or "其他"


def _themes(rows: list[dict]) -> list[dict]:
    """当天涨停（不含北交所）最集中的几个行业。"""
    counts: dict[str, int] = {}
    for row in rows:
        counts[_industry(row)] = counts.get(_industry(row), 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:THEME_TOP]
    return [{"name": name, "n": n} for name, n in ranked]


def stats(limit_rows: dict[str, list[dict]], pct_by_day: dict[str, dict[str, float]], days: list[str]) -> list[dict]:
    """days 升序。每天用当天的涨跌停名单，加上前一天涨停股今天的平均涨跌。
    北交所（30cm）不算；20cm 的涨跌幅折成 10cm 再平均。主线：当天涨停最集中的行业，以及昨天的主线今天的表现。"""
    out = []
    for index, day in enumerate(days):
        rows = [row for row in limit_rows.get(day) or [] if tape.board_limit(row.get("ts_code")) is not None]
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
        ups = [row for row in rows if str(row.get("limit") or "") == "U"]
        themes = _themes(ups)
        follow = board = theme_follow = None
        board_n = theme_n = 0
        if index > 0:
            prev = [row for row in limit_rows.get(days[index - 1]) or [] if tape.board_limit(row.get("ts_code")) is not None]
            today = pct_by_day.get(day) or {}
            held = [row for row in prev if str(row.get("limit") or "") == "U" and str(row.get("ts_code")) in today]
            scaled = [(row, tape.scaled_pct(today[str(row.get("ts_code"))], str(row.get("ts_code")))) for row in held]
            follow = sum(value for _row, value in scaled) / len(scaled) if scaled else None
            # 昨天已经连板的（高标）今天怎么样：只做说明，不单独判阶段
            high = [value for row, value in scaled if (tape.num(row.get("limit_times")) or 1) >= 2]
            board, board_n = (sum(high) / len(high) if high else None), len(high)
            # 昨天的主线（涨停最集中的几个行业）今天的表现：主线亏钱，接力就断了
            yesterday_themes = {item["name"] for item in _themes([row for row in prev if str(row.get("limit") or "") == "U"])}
            main = [value for row, value in scaled if _industry(row) in yesterday_themes]
            theme_follow, theme_n = (sum(main) / len(main) if len(main) >= THEME_MIN else None), len(main)
        tried = counts["U"] + counts["Z"]
        height_prev = out[-1]["height"] if out else None
        stat = {
            "date": tape.iso(day), "up": counts["U"], "down": counts["D"], "broken": counts["Z"], "height": height,
            "broken_rate": counts["Z"] / tried if tried else None, "follow": follow, "follow_board": board, "board_n": board_n,
            "height_prev": height_prev,
            "height_drop": height_prev is not None and height_prev >= rules.BREAK_PEAK and height_prev - height >= rules.BREAK_DROP,
            "themes": themes, "theme_share": (sum(item["n"] for item in themes) / counts["U"]) if counts["U"] else None,
            "theme_follow": theme_follow, "theme_n": theme_n,
        }
        out.append(stat)
        stat["stage"] = rules.emotion_stage(out)
    return out


def _limit_rows(day: str) -> list[dict]:
    return tape.cached(f"limit:{day}", lambda d=day: tape.pro().limit_list_d(trade_date=d))


def _pct_map(day: str) -> dict[str, float]:
    daily = tape.cached(f"daily:{day}", lambda d=day: tape.pro().daily(trade_date=d, fields="ts_code,close,pct_chg"))
    return {str(row.get("ts_code") or ""): tape.num(row.get("pct_chg")) for row in daily if tape.num(row.get("pct_chg")) is not None}


def build(today: date) -> dict:
    days = tape.open_dates(today, DAYS + WARMUP + 1)
    loaded = tape._parallel(days, _limit_rows)
    limit_rows = {day: rows for day, rows in zip(days, loaded) if rows}
    have = [day for day in days if day in limit_rows]
    pct_loaded = tape._parallel(have, _pct_map)
    pct_by_day = dict(zip(have, pct_loaded))
    rows = stats(limit_rows, pct_by_day, have)[-DAYS:]
    if not rows:
        return {"ok": False, "days": []}
    last = rows[-1]
    return {"ok": True, "stage": last["stage"], "days": rows, "last": last}
