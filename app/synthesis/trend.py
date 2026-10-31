"""走势与判断：近 60 个交易日的收盘价、上下两条价位线、每天的判断，以及判断记录。

收盘价用量价模块已对齐的前复权日线（最新一天等于实际收盘价）；每天的判断来自历史存档。
没有存档的日子 judgment 为 null（前端画成最浅的灰），和"不明"（深一点的灰）区分开。
"""

from __future__ import annotations

from app.synthesis import policy
from app.synthesis.calendar import parse_day, workday_gap

WINDOW = 60
_TICKS = 4


def bars_of(aligned: dict | None, window: int = WINDOW) -> list[dict]:
    rows = (aligned or {}).get("bars") or []
    return [{"date": row["date"], "close": row["close"]} for row in rows[-window:] if row.get("close") and row.get("date")]


def series_of(aligned: dict | None) -> dict:
    """算区间涨幅用的全量序列：收盘价和板块日涨跌（百分数）。"""
    data = aligned or {}
    return {
        "close": {row["date"]: row["close"] for row in data.get("bars") or [] if row.get("close") and row.get("date")},
        "sector": {row["date"]: row.get("chg") for row in (data.get("sector") or {}).get("rows") or [] if row.get("date")},
        "sector_name": (data.get("sector") or {}).get("name") or "",
    }


def _lean_text(lean: str | None) -> str:
    return policy.DIRECTION_TEXT.get(lean or "unclear", "不明")


def _records(points: list[dict]) -> list[dict]:
    """判断记录：第一次研判，以及之后每次方向或力度变了的那一天。"""
    items: list[dict] = []
    last = None
    for point in points:
        key = (point["lean"], point["level"])
        if last is None or key != last:
            level = policy.LEVEL_TEXT.get(point["level"] or 0, "")
            items.append({
                "date": point["date"],
                "kind": "first" if last is None else "change",
                "lean": point["lean"],
                "level": point["level"],
                "label": _lean_text(point["lean"]) + (f" · {level}" if level else ""),
                "text": "" if last is None else point.get("text") or "",
            })
        last = key
    return items


def _md(day: str) -> str:
    return day[5:10]


def _days_between(start: str, end: str, dates: list[str]) -> int:
    """start 到 end 之间隔了几个交易日。两个日期都在日线里就按日线数，否则按工作日估。"""
    if start in dates and end in dates:
        return dates.index(end) - dates.index(start)
    first, last = parse_day(start), parse_day(end)
    return workday_gap(first, last) if first and last else 0


def _stock_ret(series: dict, start: str, end: str):
    close = series.get("close") or {}
    if start not in close or end not in close or not close[start]:
        return None
    return round(close[end] / close[start] - 1, 4)


def _sector_ret(series: dict, start: str, end: str):
    """板块在 start 收盘之后到 end 收盘的涨幅：把每天的涨跌复利起来。"""
    rows = series.get("sector") or {}
    days = [day for day in sorted(rows) if start < day <= end and rows[day] is not None]
    if not days or end not in rows:
        return None
    acc = 1.0
    for day in days:
        acc *= 1 + float(rows[day]) / 100
    return round(acc - 1, 4)


def _segments(ordered: list[dict], today: str, dates: list[str], series: dict) -> list[dict]:
    """按方向分段：连续同一个方向算一段，力度变化不分段。"""
    groups: list[list[dict]] = []
    for point in ordered:
        if groups and groups[-1][0]["lean"] == point["lean"]:
            groups[-1].append(point)
        else:
            groups.append([point])
    rows = []
    for index, group in enumerate(groups):
        start = group[0]["date"]
        last = index == len(groups) - 1
        end = today if last else groups[index + 1][0]["date"]
        lean = group[0]["lean"]
        label = _lean_text(lean)
        if last:
            days = _days_between(start, end, dates) + 1
            text = f"{_md(start)} 起{label} · 第 {days} 天"
        else:
            days = _days_between(start, end, dates)
            text = f"{_md(start)} 起{label} · {days} 天"
        rows.append({
            "start": start, "end": end, "lean": lean, "label": label, "days": days, "current": last, "text": text,
            "ret": _stock_ret(series, start, end), "sector_ret": _sector_ret(series, start, end),
        })
    return rows


def _since_text(segment: dict, sector_name: str) -> str:
    if segment["ret"] is None:
        return ""
    text = f"{segment['label']}以来 {segment['ret'] * 100:+.1f}%"
    if segment["sector_ret"] is not None:
        text += f"，同期{sector_name or '板块'} {segment['sector_ret'] * 100:+.1f}%"
    return text


def build(trend_input: dict | None, levels: dict | None, current: dict, credibility: dict) -> dict | None:
    """trend_input: {"bars": [...], "history": [...], "date": 本次的交易日}；current: {"lean", "level", "text"}。"""
    bars = (trend_input or {}).get("bars") or []
    if not bars:
        return None
    history = (trend_input or {}).get("history") or []
    today = trend_input["date"]
    points: dict[str, dict] = {
        item["trade_date"]: {"date": item["trade_date"], "lean": item["lean"], "level": item["level"], "text": item.get("text", "")}
        for item in history
    }
    points[today] = {"date": today, "lean": current["lean"], "level": current["level"], "text": current.get("text", "")}
    ordered = [points[key] for key in sorted(points)]

    days = []
    for bar in bars:
        point = points.get(bar["date"])
        days.append({
            "date": bar["date"],
            "close": bar["close"],
            "judgment": point["lean"] if point else None,
            "level": point["level"] if point else None,
        })
    lines = {}
    for side, label in (("up", None), ("down", "转弱价位")):
        level = (levels or {}).get(side)
        lines[side] = None if not level else {
            "price": level["price"],
            "label": label or "突破" + (level.get("base_name") or "上方价"),
            "base_name": level.get("base_name") or "",
            "verified": level.get("verified", True),
        }
    values = [day["close"] for day in days] + [line["price"] for line in lines.values() if line]
    step = (len(days) - 1) / (_TICKS - 1) if len(days) > 1 else 0
    ticks = sorted({days[round(index * step)]["date"] for index in range(_TICKS)})
    records = _records(ordered)
    dates = [day["date"] for day in days]
    series = (trend_input or {}).get("series") or {}
    segments = _segments(ordered, today, dates, series)
    now_segment = segments[-1]
    current_run = {
        **{key: now_segment[key] for key in ("start", "lean", "label", "days", "ret", "sector_ret")},
        "start_in_window": now_segment["start"] in dates,
        # 高亮区间从第几根开始；起点比最后一根日线还新（盘中，今天的日线还没有）时等于根数
        "start_index": next((i for i, day in enumerate(dates) if day >= now_segment["start"]), len(dates)),
        "text": f"{_md(now_segment['start'])} 起{now_segment['label']}",
        "since_text": _since_text(now_segment, series.get("sector_name", "")),
    }
    if len(ordered) <= 1:
        note = f"之前没有记录；{credibility.get('total', len(policy.DIMENSIONS))} 个方面看完后会从这里开始累积"
    else:
        note = f"共 {len(ordered)} 天有判断记录，方向或力度变过 {len(records) - 1} 次"
    return {
        "window": len(days),
        "unit": "日线",
        "bars": days,
        "lines": lines,
        "x_ticks": ticks,
        "y_min": min(values),
        "y_max": max(values),
        "segments": segments,
        "current_run": current_run,
        # 本次的判断。盘中时 bars 里还没有今天这一根，前端要把它画在最后一根之后
        "today": {
            "date": today, "lean": current["lean"], "level": current["level"],
            "label": _lean_text(current["lean"]), "in_bars": today in dates,
        },
        "record": {"items": records[-10:], "total": len(ordered), "note": note},
    }
