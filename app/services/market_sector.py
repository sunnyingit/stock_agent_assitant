"""板块三层：钱（ETF 份额净增 × 收盘价）、热度（换手 ÷ 自身一年均值）、杠杆（行业两融 4 周变化）。

每个板块固定取一只代表 ETF。杠杆取对应的申万一级行业，红利没有对应行业，杠杆这一层空着，不拿别的行业凑。
"""

from __future__ import annotations

from datetime import date

from app.services import market_rules as rules
from app.services import market_tape as tape

# 名称、代表 ETF、对应的申万一级行业代码
SECTORS = (
    ("银行", "512800.SH", "801780.SI"),
    ("红利", "510880.SH", None),
    ("白酒", "512690.SH", "801120.SI"),
    ("芯片", "159995.SZ", "801080.SI"),
    ("机器人", "562500.SH", "801890.SI"),
    ("光伏", "515790.SH", "801730.SI"),
)
WINDOWS = (3, 5, 10, 20)
CONCLUSION_DAYS = 10  # 板块结论用近 2 周
L1_NAMES = {
    "801780.SI": "银行", "801120.SI": "食品饮料", "801080.SI": "电子",
    "801890.SI": "机械设备", "801730.SI": "电力设备",
}


def flows(shares: list[dict], closes: list[dict]) -> dict[int, float | None]:
    """各窗口的净申购金额（亿元）：每天的份额变化（万份）× 当天收盘价 ÷ 1 万，再累加。"""
    rows = sorted(
        [row for row in shares if tape.num(row.get("fd_share")) is not None],
        key=lambda row: tape.ymd(row.get("trade_date")),
    )
    price = {tape.ymd(row.get("trade_date")): tape.num(row.get("close")) for row in closes}
    daily: list[float] = []
    for prev, row in zip(rows, rows[1:]):
        close = price.get(tape.ymd(row.get("trade_date")))
        if close is None:
            continue
        daily.append((tape.num(row.get("fd_share")) - tape.num(prev.get("fd_share"))) * close / 10000)
    return {n: (sum(daily[-n:]) if len(daily) >= n else None) for n in WINDOWS}


def heat(shares: list[dict], closes: list[dict]) -> float | None:
    """今天换手率 ÷ 近一年平均换手率。换手率 = 成交量（手）× 100 ÷ 份额（万份 × 1 万）。"""
    held = sorted(
        [(tape.ymd(row.get("trade_date")), tape.num(row.get("fd_share"))) for row in shares if tape.num(row.get("fd_share"))],
    )
    if not held:
        return None
    rates = []
    pointer = 0
    current = held[0][1]
    for row in sorted(closes, key=lambda item: tape.ymd(item.get("trade_date"))):
        day = tape.ymd(row.get("trade_date"))
        while pointer < len(held) and held[pointer][0] <= day:
            current = held[pointer][1]
            pointer += 1
        vol = tape.num(row.get("vol"))
        if vol is None or not current:
            continue
        rates.append(vol * 100 / (current * 10000))
    if len(rates) < 60:
        return None
    base = sum(rates[-250:]) / len(rates[-250:])
    return rates[-1] / base if base else None


def lever(today: date) -> dict[str, float | None]:
    """行业两融余额 4 周（20 个交易日）变化。按申万一级成分股加总融资余额。"""
    days = tape.open_dates(today, 22)
    out: dict[str, float | None] = {code: None for code in L1_NAMES}
    if len(days) < 21:
        return out
    now_rows = _margin_rows(days[-1]) or _margin_rows(days[-2])
    then_rows = _margin_rows(days[-21])
    if not now_rows or not then_rows:
        return out
    for code in L1_NAMES:
        members = _members(code)
        if not members:
            continue
        now = sum(tape.num(row.get("rzye")) or 0 for row in now_rows if row.get("ts_code") in members)
        then = sum(tape.num(row.get("rzye")) or 0 for row in then_rows if row.get("ts_code") in members)
        out[code] = tape.growth(now, then) if then else None
    return out


def _margin_rows(day: str) -> list[dict]:
    return tape.cached(f"margin_detail:{day}", lambda: tape.pro().margin_detail(trade_date=day, fields="ts_code,rzye,rzmre,rzche"))


def _members(code: str) -> set[str]:
    rows = tape.cached(f"l1members:{code}", lambda: tape.pro().index_member_all(l1_code=code, is_new="Y"))
    return {str(row.get("ts_code")) for row in rows if row.get("ts_code")}


def build(today: date) -> dict:
    start = tape._ymd_offset(today, 400)
    levers = lever(today)
    rows = []
    latest_share_day = ""
    for name, etf, l1 in SECTORS:
        shares = tape.cached(f"fdshare:{etf}:{start}", lambda c=etf: tape.pro().fund_share(ts_code=c, start_date=start, end_date=tape._ymd(today)))
        closes = tape.cached(f"fdbar:{etf}:{start}", lambda c=etf: tape.pro().fund_daily(
            ts_code=c, start_date=start, end_date=tape._ymd(today), fields="ts_code,trade_date,close,vol,amount",
        ))
        flow = flows(shares, closes)
        ratio = heat(shares, closes)
        move = levers.get(l1) if l1 else None
        layers = rules.sector_layers(flow.get(CONCLUSION_DAYS), ratio, move)
        for row in shares:
            latest_share_day = max(latest_share_day, tape.ymd(row.get("trade_date")))
        rows.append({
            "name": name, "code": etf, "industry": L1_NAMES.get(l1, ""),
            "flow": flow, "heat": ratio, "lever": move,
            "lever_word": layers["lever_word"], "verdict": layers["verdict"], "tone": layers["tone"],
        })
    return {"rows": rows, "date": tape.iso(latest_share_day), "windows": list(WINDOWS), "conclusion_days": CONCLUSION_DAYS}


def catalysts(now=None) -> dict[str, dict]:
    """每个板块最近 3 天的一条新闻线索。东方财富搜索，标题里要带板块名；没有就空着，不凑。"""
    from datetime import datetime, timedelta

    from app.services.news import fetch_search_news

    now = now or datetime.now()
    out: dict[str, dict] = {}
    for name, _code, _l1 in SECTORS:
        try:
            items = fetch_search_news(f"{name}板块", 8)
        except Exception:
            continue
        for item in sorted(items, key=lambda row: str(row.get("time") or ""), reverse=True):
            stamp = str(item.get("time") or "")[:16]
            title = str(item.get("title") or "")
            try:
                when = datetime.strptime(stamp[:10], "%Y-%m-%d")
            except ValueError:
                continue
            if name in title and now - when <= timedelta(days=3):
                out[name] = {"title": title, "time": stamp, "url": item.get("url") or ""}
                break
    return out
