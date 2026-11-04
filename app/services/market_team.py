"""国家队：看几只大盘 ETF 在大跌日有没有大额申购，只看份额，不猜是谁。

出手日：上证指数当天跌超 1%，且这 9 只 ETF 合计份额当天的增加，超过前 60 个交易日日变化均值加 2 倍标准差。
买进去的钱还在吗：从最近一次出手日起，新增份额还剩多少；撤走超过 3 成才改成「可能在卖」。
季报持有人没有接口，盘中异动要实时 ETF 行情，这一期都不做，页面上明说。
公告走东方财富新闻搜索，只做确认线索：标题里要有官方主体和明确动作，专家解读不算。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from statistics import mean, pstdev

from app.services import market_tape as tape

# 国家队通道 9 只，和宽基观察池（ETF_BOOK）不是一套
TEAM_BOOK = (
    ("510300.SH", "沪深300ETF 华泰柏瑞"),
    ("510310.SH", "沪深300ETF 易方达"),
    ("159919.SZ", "沪深300ETF 嘉实"),
    ("510330.SH", "沪深300ETF 华夏"),
    ("510050.SH", "上证50ETF 华夏"),
    ("510500.SH", "中证500ETF 南方"),
    ("512050.SH", "A500ETF 华夏"),
    ("588000.SH", "科创50ETF 华夏"),
    ("159995.SZ", "芯片ETF 华夏"),
)
DROP = -1.0          # 大盘跌超 1%
SIGMA = 2.0          # 份额增加超过近 60 天波动的 2 倍
LOOKBACK = 60
GONE = 0.30          # 撤走超过 3 成算可能在卖
RECENT = 5           # 出手日在最近 5 个交易日内，算「在出手」
AFTER = 20

OFFICIAL = {
    "汇金": ("中央汇金", "汇金"),
    "央行": ("央行", "中国人民银行", "人民银行"),
    "证监会": ("证监会", "金融监管总局"),
    "国务院": ("国务院",),
}
ACTIONS = ("增持", "降准", "降息", "下调", "回购增持再贷款", "再贷款", "设立", "出台", "印发", "宣布", "稳定")
STRONG = ("降准", "降息", "下调存款准备金率", "下调政策利率", "下调贷款市场报价利率")
QUERIES = ("中央汇金 增持", "央行 降准 降息", "央行 回购增持再贷款", "证监会 资本市场 新政")


def detect_actions(totals: list[tuple[str, float]], pct: dict[str, float]) -> list[dict]:
    """totals 是 (日期, 9 只合计份额·亿份)，按日期升序。"""
    deltas = [totals[i][1] - totals[i - 1][1] for i in range(1, len(totals))]
    found = []
    for i in range(LOOKBACK, len(deltas)):
        base = deltas[i - LOOKBACK:i]
        limit = mean(base) + SIGMA * pstdev(base)
        day = totals[i + 1][0]
        move = pct.get(day)
        if deltas[i] > 0 and deltas[i] > limit and move is not None and move <= DROP:
            found.append({"day": day, "delta": deltas[i], "index_pct": move, "limit": limit, "pos": i + 1})
    return found


def holding(totals: list[tuple[str, float]], action: dict | None) -> dict | None:
    if not action:
        return None
    before = totals[action["pos"] - 1][1]
    bought = action["delta"]
    now = totals[-1][1] - before
    gone = max(0.0, 1 - now / bought) if bought > 0 else None
    return {"bought": bought, "now": now, "gone": gone, "line": bought * (1 - GONE), "since": action["day"]}


def label_of(actions: list[dict], total_days: int, hold: dict | None) -> str:
    if actions and total_days - 1 - actions[-1]["pos"] < RECENT:
        return "在出手"
    if hold and hold["gone"] is not None and hold["gone"] > GONE and total_days - 1 - actions[-1]["pos"] <= 60:
        return "可能在卖"
    return "没出手"


def history(actions: list[dict], index_days: list[str], pct: dict[str, float]) -> list[dict]:
    """每次出手后 20 个交易日上证指数的涨跌；还没满 20 天的写进行到第几天。"""
    rows = []
    for action in actions:
        if action["day"] not in index_days:
            continue
        pos = index_days.index(action["day"])
        after = index_days[pos + 1:pos + 1 + AFTER]
        if len(after) < AFTER:
            rows.append({"day": tape.iso(action["day"]), "delta": action["delta"], "index_pct": action["index_pct"], "ret20": None, "progress": len(after)})
            continue
        acc = 1.0
        for day in after:
            acc *= 1 + (pct.get(day) or 0) / 100
        rows.append({"day": tape.iso(action["day"]), "delta": action["delta"], "index_pct": action["index_pct"], "ret20": acc - 1, "progress": None})
    return rows


def build(today: date) -> dict:
    start = tape._ymd_offset(today, 400)
    shares: dict[str, list[dict]] = {}
    for code, _name in TEAM_BOOK:
        shares[code] = tape.cached(f"fdshare:{code}:{start}", lambda c=code: tape.pro().fund_share(
            ts_code=c, start_date=start, end_date=tape._ymd(today),
        ))
    idx = tape.cached(f"indexpct:{start}", lambda: tape.pro().index_daily(
        ts_code="000001.SH", start_date=start, end_date=tape._ymd(today), fields="trade_date,pct_chg",
    ))
    pct = {tape.ymd(row.get("trade_date")): tape.num(row.get("pct_chg")) for row in idx if tape.num(row.get("pct_chg")) is not None}
    return summarize(shares, pct)


def summarize(shares: dict[str, list[dict]], pct: dict[str, float]) -> dict:
    by_day: dict[str, dict[str, float]] = {}
    codes = []
    for code, _name in TEAM_BOOK:
        rows = [row for row in shares.get(code) or [] if tape.num(row.get("fd_share")) is not None]
        if len(rows) < 2:
            continue
        codes.append(code)
        for row in rows:
            by_day.setdefault(tape.ymd(row.get("trade_date")), {})[code] = tape.num(row.get("fd_share"))
    days = sorted(day for day, slot in by_day.items() if len(slot) == len(codes))
    if len(days) < LOOKBACK + 2:
        return {"ok": False, "text": "取到的宽基 ETF 份额不够长，判不了出手日"}
    totals = [(day, sum(by_day[day].values()) / 1e4) for day in days]
    actions = detect_actions(totals, pct)
    last = actions[-1] if actions else None
    hold = holding(totals, last)
    label = label_of(actions, len(totals), hold)
    index_days = sorted(pct)
    counts = {n: sum(1 for item in actions if len(totals) - 1 - item["pos"] < n) for n in (5, 20, 60)}
    series = []
    for i in range(max(1, len(totals) - 60), len(totals)):
        day = totals[i][0]
        series.append({
            "date": tape.iso(day), "value": totals[i][1], "delta": totals[i][1] - totals[i - 1][1],
            "index_pct": pct.get(day), "action": any(item["day"] == day for item in actions),
        })
    return {
        "ok": True, "label": label, "codes": codes, "counted": len(codes), "total": totals[-1][1], "latest": tape.iso(totals[-1][0]),
        "actions": [{**item, "day": tape.iso(item["day"])} for item in actions[-6:]],
        "last": None if not last else {**last, "day": tape.iso(last["day"]), "since": len(totals) - 1 - last["pos"]},
        "hold": hold and {**hold, "since": tape.iso(hold["since"])},
        "counts": counts, "series": series,
        "history": history(actions, index_days, pct),
        "missing": [name for code, name in TEAM_BOOK if code not in codes],
    }


def official_items(items: list[dict], now: datetime, days: int = 20) -> list[dict]:
    """从新闻搜索结果里留下官方主体 + 明确动作的标题，近 days 天。"""
    out, seen = [], set()
    for item in items:
        title = str(item.get("title") or "")
        stamp = str(item.get("time") or "")[:10]
        if not title or title in seen or len(stamp) < 10:
            continue
        try:
            when = datetime.strptime(stamp, "%Y-%m-%d")
        except ValueError:
            continue
        if now - when > timedelta(days=days):
            continue
        who = next((name for name, words in OFFICIAL.items() if any(word in title[:24] for word in words)), "")
        if not who or not any(word in title for word in ACTIONS):
            continue
        strong = (
            any(word in title for word in STRONG)
            or ("汇金" in title and "增持" in title)
            or ("再贷款" in title and any(word in title for word in ("加力", "下调", "扩容")))
        )
        seen.add(title)
        out.append({"date": stamp, "who": who, "title": title, "url": item.get("url") or "", "strong": strong})
    out.sort(key=lambda row: row["date"], reverse=True)
    return out


def announcements(now: datetime | None = None) -> dict:
    from app.services.news import fetch_search_news

    now = now or datetime.now()

    def one(keyword: str) -> list[dict]:
        try:
            return fetch_search_news(keyword, 10)
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=4) as pool:
        batches = list(pool.map(one, QUERIES))
    rows = official_items([item for batch in batches for item in batch], now)
    return {"items": rows[:8], "ok": any(batches)}


def policy_hit(items: list[dict], now: datetime, days: int = 7) -> dict | None:
    """近 days 天有降准降息或汇金增持的官方标题，政策分给 5。"""
    for item in items:
        if not item.get("strong"):
            continue
        if now - datetime.strptime(item["date"], "%Y-%m-%d") <= timedelta(days=days):
            return item
    return None
