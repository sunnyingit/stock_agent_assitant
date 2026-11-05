"""ETF 详情：份额每日变化 + 价格、每日明细、同指数对比。只放市场页跟踪的 ETF，不给任意代码开口子。"""

from __future__ import annotations

from datetime import date
from statistics import pstdev

from app.services import market_sector, market_tape as tape, market_team

RANGES = (20, 60, 250)
TABLE_DAYS = 8
FLAG = 1.5   # 份额日变化超过近 60 天波动的 1.5 倍，才标「涨日减 / 跌日增」


def known() -> dict[str, str]:
    names = {code: name for code, name, _ in tape.ETF_BOOK}
    names.update({code: name for code, name in market_team.TEAM_BOOK})
    names.update({code: name for name, code, _ in market_sector.SECTORS})
    return names


def _basics(code: str) -> dict:
    rows = tape.cached("fdbasic:E", lambda: tape.pro().fund_basic(market="E", status="L"))
    for row in rows:
        if row.get("ts_code") == code:
            return row
    return {}


def _benchmark(code: str, rows: list[dict]) -> str:
    for row in rows:
        if row.get("ts_code") == code:
            return str(row.get("benchmark") or "")
    return ""


def _share_rows(code: str, start: str, today: date) -> list[dict]:
    return tape.cached(f"fdshare:{code}:{start}", lambda: tape.pro().fund_share(ts_code=code, start_date=start, end_date=tape._ymd(today)))


def series_of(shares: list[dict], bars: list[dict]) -> list[dict]:
    """按份额日期对齐收盘价和涨跌幅，份额单位换成亿份。"""
    by_day = {tape.ymd(row.get("trade_date")): row for row in bars}
    rows = sorted(
        [row for row in shares if tape.num(row.get("fd_share")) is not None],
        key=lambda row: tape.ymd(row.get("trade_date")),
    )
    out = []
    for index, row in enumerate(rows):
        day = tape.ymd(row.get("trade_date"))
        bar = by_day.get(day) or {}
        share = tape.num(row.get("fd_share")) / 1e4
        out.append({
            "date": tape.iso(day), "share": share,
            "delta": 0.0 if index == 0 else share - tape.num(rows[index - 1].get("fd_share")) / 1e4,
            "close": tape.num(bar.get("close")), "pct": tape.num(bar.get("pct_chg")),
        })
    return out


def flag(points: list[dict], index: int) -> str:
    base = [point["delta"] for point in points[max(1, index - 60):index]]
    if len(base) < 20:
        return ""
    sigma = pstdev(base) * FLAG
    point = points[index]
    if point["pct"] is None:
        return ""
    if point["pct"] > 0 and point["delta"] < -sigma:
        return "涨日减"
    if point["pct"] < 0 and point["delta"] > sigma:
        return "跌日增"
    return ""


def detail(code: str, days: int = 60, today: date | None = None) -> dict:
    names = known()
    if code not in names:
        raise KeyError(code)
    today = today or date.today()
    days = days if days in RANGES else 60
    shares = tape.fund_share_rows(code, today)
    bars = tape.fund_bar_rows(code, today)
    points = series_of(shares, bars)
    if len(points) < 2:
        return {"code": code, "name": names[code], "ok": False}
    for index in range(len(points)):
        points[index]["flag"] = flag(points, index)
    last = points[-1]
    net5 = sum((p["delta"] * p["close"]) for p in points[-5:] if p["close"] is not None)
    nav_rows = tape.cached(f"fdnav:{code}", lambda: tape.pro().fund_nav(ts_code=code, start_date=tape._ymd_offset(today, 15), end_date=tape._ymd(today)))
    nav_rows = sorted(nav_rows, key=lambda row: tape.ymd(row.get("nav_date")))
    nav = tape.num(nav_rows[-1].get("unit_nav")) if nav_rows else None
    nav_day = tape.iso(nav_rows[-1].get("nav_date")) if nav_rows else ""
    close_on_nav = next((p["close"] for p in points if p["date"] == nav_day), None)
    basics = _basics(code)
    all_basics = tape.cached("fdbasic:E", lambda: tape.pro().fund_basic(market="E", status="L"))
    benchmark = str(basics.get("benchmark") or "")
    same = []
    for other, name in known().items():
        if other == code or not benchmark or _benchmark(other, all_basics) != benchmark:
            continue
        pts = series_of(_share_rows(other, start, today), [])
        if len(pts) > 20:
            same.append({"code": other, "name": name, "change20": pts[-1]["share"] - pts[-21]["share"]})
    since = None
    action = (market_team.build(today).get("last") if code in dict(market_team.TEAM_BOOK) else None)
    if action:
        base = next((p for p in points if p["date"] == action["day"]), None)
        index = points.index(base) if base else None
        if index:
            since = {"day": action["day"], "change": last["share"] - points[index - 1]["share"]}
    return {
        "ok": True, "code": code, "name": basics.get("name") or names[code], "benchmark": benchmark, "days": days, "ranges": list(RANGES),
        "share": last["share"], "size": None if last["close"] is None else last["share"] * last["close"],
        "nav": nav, "nav_date": nav_day,
        "premium": None if not nav or close_on_nav is None else close_on_nav / nav - 1,
        "day_change": last["delta"], "net5": net5,
        "change20": points[-1]["share"] - points[-21]["share"] if len(points) > 20 else None,
        "since_action": since, "latest": last["date"],
        "series": points[-days:], "table": list(reversed(points[-TABLE_DAYS:])), "same_index": same,
        "holders": {"available": False, "text": "季报持有人没有接口，这一块不填示意数据"},
        "note": "份额来自交易所每日基金规模（场内份额），T+1 更新；单日变化不下结论。涨跌幅是场内价格涨跌，净值日期比价格晚一两天，溢价用同一天的价格算。",
    }
