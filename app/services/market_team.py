"""国家队：看几只大盘 ETF 有没有被大额、广泛地买进或卖出，只看份额，不猜是谁。

A 股里国家队（汇金等）既会在市场承压时逆势护盘，也会在市场平稳或走强时减仓。散户抄底、追涨、恐慌赎回也会让 ETF 份额变化，
所以买和卖都要同时满足「买得广、量级远超平时、主要是大盘宽基」，再看市场背景。

出手（托底买入）：
  1. 市场承压。当天上证收跌且够明显：跌超 1%；或跌超 0.5% 且近 3 日累计跌超 2%；
     或者前两天刚承压（近 3 日累计跌超 2%，或最近 3 天里有一天跌超 1%），今天不管涨跌，国家队接着买。
     买得极猛（近 60 天最大、异常度 ≥ 3）时，跌超 0.5% 也算。
     不要求当天一定收跌：护盘买进去以后，指数当天可能已经拉红。
  2. 买得广：9 只里至少 3/4 当天净申购为正。
  3. 买得猛：9 只合计净申购超过前 60 日均值加 2 倍标准差；每只 ETF 的申购跟自己前 60 天比，取这些异常度的中位数，
     超过前 60 天这个中位数的中位数加 2 倍 MAD（不会被以前的暴增日抬高）。
  4. 买的是大盘宽基：沪深 300、上证 50、中证 500、A500 这几只贡献合计净申购的一半以上。
减仓（出货）：
  1. 买得广、买得猛、主要是大盘宽基的镜像：至少 3/4 的 ETF 当天净赎回，合计净赎回超过前 60 日均值减 2 倍标准差，
     异常度的中位数低于前 60 天的中位数减 2 倍 MAD，大盘宽基占净赎回的一半以上。
  2. 上证当天没有大跌（跌幅不超过 1%）：恐慌日的赎回是散户割肉，不算国家队。
  3. 近 3 个交易日里至少 2 天满足以上，才算一次减仓：单日赎回可能只是调仓，连续才是趋势。
只比较相邻两个交易日，缺份额的那只跳过，不把多天拼成一天。
最近 5 个交易日内出现过出手，算「在出手」，强度按离开出手日的天数线性衰减，第 5 天降到 0。
最近 5 个交易日内出现过减仓，或者出手后各只按自己的波动算累计回撤中位数超过 30%，算「可能在卖」；两者都有，以更近的为准。
季报持有人没有接口，盘中异动要实时 ETF 行情，这一期都不做，页面上明说。
公告走东方财富新闻搜索，只做确认线索：标题里要有官方主体和明确动作，专家解读不算。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from statistics import mean, median, pstdev

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
LOOKBACK = 60
MIN_BASE = 40        # 前 60 天里至少 40 天有数，才算得出这只的平时
MIN_NAMES = 5        # 至少 5 只有 z 值，才合成当天的中位数
MAD_K = 2.0          # 中位数加这么多倍 MAD
MONEY_K = 2.0        # 合计净申购超过前 60 日均值加这么多倍标准差
BREADTH = 0.75       # 至少这个比例的 ETF 当天净申购为正
BLUE_CHIPS = ("510300.SH", "510310.SH", "159919.SZ", "510330.SH", "510050.SH", "510500.SH", "512050.SH")
BLUE_SHARE = 0.5     # 大盘宽基要占合计净申购的一半以上
DROP_HARD = -1.0     # 当天跌超 1%：明显下跌；前两天有这样的一天，也算市场刚承压
DROP_SOFT = -0.5     # 跌超 0.5% 且……
SLUMP_3D = -2.0      # ……近 3 日累计跌超 2%：连续下跌；或者买得极猛（见下）
EXTREME_Z = 3.0      # 买得极猛：近 60 天最大的一次，且异常度不低于这个值
CONFIRM_DAYS, CONFIRM_MIN = 3, 2   # 减仓：近 3 个交易日里至少 2 天满足
CONTINUE_GAP = 3     # 出手日之间隔不超过 3 个交易日，算连续出手
GONE = 0.30          # 标准化累计回撤超过 3 成，算可能在卖
RECENT = 5           # 最近 5 个交易日内出现过，算「在出手」；第 5 天强度降到 0
SELL_WINDOW = 60
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


def daily_flows(calendar: list[str], shares: dict[str, dict[str, float]], closes: dict[str, dict[str, float]], codes: list[str]) -> list[tuple[str, float | None]]:
    """calendar 是上证交易日升序。返回 (日期, 9 只合计净申购·亿元)；这天或前一天有哪只缺份额、缺收盘价，就是 None。
    份额单位万份，× 元 = 万元，再 / 1e4 = 亿元。"""
    out: list[tuple[str, float | None]] = [(calendar[0], None)] if calendar else []
    for prev, day in zip(calendar, calendar[1:]):
        now, before, price = shares.get(day, {}), shares.get(prev, {}), closes.get(day, {})
        if all(code in now and code in before and code in price for code in codes):
            out.append((day, sum((now[code] - before[code]) * price[code] for code in codes) / 1e4))
        else:
            out.append((day, None))
    return out


def _mad(values: list[float]) -> float:
    mid = median(values)
    return median([abs(item - mid) for item in values])


def code_deltas(calendar: list[str], shares: dict[str, dict[str, float]], codes: list[str]) -> dict[str, dict[str, float]]:
    """每只 ETF 只在相邻两个交易日都有份额时记一天的变化。缺一天就跳过，不把多天拼成一天。"""
    out = {code: {} for code in codes}
    for prev, day in zip(calendar, calendar[1:]):
        before, now = shares.get(prev, {}), shares.get(day, {})
        for code in codes:
            if code in before and code in now:
                out[code][day] = now[code] - before[code]
    return out


def _composite(calendar: list[str], deltas: dict[str, dict[str, float]], index: int) -> tuple[float | None, dict[str, float], dict[str, float]]:
    """当天 9 只 z 值的中位数。z = (当日变化 − 前 60 日均值) / 前 60 日标准差。"""
    day = calendar[index]
    scores, stdevs, spikes = [], {}, {}
    for code, series in deltas.items():
        if day not in series:
            continue
        history = [series[past] for past in calendar[:index] if past in series][-LOOKBACK:]
        if len(history) < MIN_BASE:
            continue
        sigma = pstdev(history)
        if sigma == 0:
            continue
        scores.append((series[day] - mean(history)) / sigma)
        stdevs[code] = sigma
        spikes[code] = series[day]
    if len(scores) < MIN_NAMES:
        return None, {}, {}
    return median(scores), stdevs, spikes


def money_totals(calendar: list[str], money: dict[str, dict[str, float]]) -> list[tuple[float | None, dict[str, float]]]:
    """每个交易日：有数的 ETF 当天净申购金额（亿元）和合计。有数的不到 MIN_NAMES 只，合计记 None。"""
    out = []
    for day in calendar:
        have = {code: series[day] for code, series in money.items() if day in series}
        out.append((sum(have.values()) if len(have) >= MIN_NAMES else None, have))
    return out


def weak_kind(move: float, cum3: float, worst3: float, biggest: bool, signal: float) -> str | None:
    """市场有没有承压（出手的背景）。越弱要求的买入量越低。
    返回：大跌（当天跌超 1%）、连跌（跌超 0.5% 且 3 日累计跌超 2%）、承接（前两天刚承压，今天不管涨跌）、极猛（买得极猛，跌超 0.5%）。"""
    if move <= DROP_HARD:
        return "大跌"
    if move <= DROP_SOFT and cum3 <= SLUMP_3D:
        return "连跌"
    if cum3 <= SLUMP_3D or worst3 <= DROP_HARD:
        return "承接"
    if move <= DROP_SOFT and biggest and signal >= EXTREME_Z:
        return "极猛"
    return None


def _signals(calendar: list[str], deltas: dict[str, dict[str, float]]) -> list[tuple[str, float | None, dict[str, float], dict[str, float]]]:
    out = []
    for index in range(len(calendar)):
        signal, stdevs, spikes = _composite(calendar, deltas, index)
        out.append((calendar[index], signal, stdevs, spikes))
    return out


def detect_actions(calendar: list[str], deltas: dict[str, dict[str, float]], pct: dict[str, float],
                   money: dict[str, dict[str, float]], blue: tuple[str, ...] = BLUE_CHIPS) -> list[dict]:
    """市场承压时，买得广、买得猛（金额和异常度都超过平时）、主要买的是大盘宽基，算一次出手。当天不一定收跌。"""
    signals = _signals(calendar, deltas)
    totals = money_totals(calendar, money)
    found = []
    for index in range(1, len(calendar)):
        day, signal, stdevs, spikes = signals[index]
        move = pct.get(day)
        total, have = totals[index]
        if signal is None or signal <= 0 or move is None or total is None or total <= 0:
            continue
        window = range(max(0, index - LOOKBACK), index)
        prior = [signals[j][1] for j in window if signals[j][1] is not None]
        history = [totals[j][0] for j in window if totals[j][0] is not None]
        if len(prior) < MIN_BASE or len(history) < MIN_BASE:
            continue
        limit = median(prior) + MAD_K * _mad(prior)
        money_limit = mean(history) + MONEY_K * pstdev(history)
        if signal <= limit or total <= money_limit:
            continue
        positive = sum(1 for value in have.values() if value > 0)
        if positive < BREADTH * len(have):
            continue
        blue_share = sum(value for code, value in have.items() if code in blue) / total
        if blue_share < BLUE_SHARE:
            continue
        recent = [pct.get(past, 0.0) for past in calendar[max(0, index - 2):index + 1]]
        cum3, worst3 = sum(recent), min(recent)
        weak = weak_kind(move, cum3, worst3, total >= max(history), signal)
        if weak is None:
            continue
        found.append({
            "day": day, "signal": signal, "limit": limit, "index_pct": move, "pos": index, "stdevs": stdevs, "spikes": spikes,
            "delta": total, "money_limit": money_limit, "positive": positive, "names": len(have), "blue_share": blue_share,
            "cum3": cum3, "worst3": worst3, "weak": weak,
            # 给页面说人话用：这天的合计净申购在前 60 个交易日里排第几
            "rank": 1 + sum(1 for item in history if item >= total), "of": len(history),
        })
    for k, item in enumerate(found):   # 连续几天都在买，说一声「连续第 N 天」
        item["streak"] = found[k - 1]["streak"] + 1 if k and item["pos"] - found[k - 1]["pos"] <= CONTINUE_GAP else 1
    return found


def detect_sells(calendar: list[str], deltas: dict[str, dict[str, float]], pct: dict[str, float],
                 money: dict[str, dict[str, float]], blue: tuple[str, ...] = BLUE_CHIPS) -> list[dict]:
    """大盘宽基被广泛、大额净赎回，而且上证没有大跌（不是恐慌割肉），近 3 个交易日里至少 2 天这样，算一次减仓。"""
    signals = _signals(calendar, deltas)
    totals = money_totals(calendar, money)
    hit: dict[int, dict] = {}
    for index in range(1, len(calendar)):
        day, signal, stdevs, spikes = signals[index]
        move = pct.get(day)
        total, have = totals[index]
        if signal is None or signal >= 0 or move is None or total is None or total >= 0 or move <= DROP_HARD:
            continue
        window = range(max(0, index - LOOKBACK), index)
        prior = [signals[j][1] for j in window if signals[j][1] is not None]
        history = [totals[j][0] for j in window if totals[j][0] is not None]
        if len(prior) < MIN_BASE or len(history) < MIN_BASE:
            continue
        limit = median(prior) - MAD_K * _mad(prior)
        money_limit = mean(history) - MONEY_K * pstdev(history)
        if signal >= limit or total >= money_limit:
            continue
        negative = sum(1 for value in have.values() if value < 0)
        if negative < BREADTH * len(have):
            continue
        blue_share = sum(value for code, value in have.items() if code in blue) / total
        if blue_share < BLUE_SHARE:
            continue
        hit[index] = {
            "day": day, "signal": signal, "limit": limit, "index_pct": move, "pos": index, "delta": total, "money_limit": money_limit,
            "negative": negative, "names": len(have), "blue_share": blue_share, "of": len(history),
            "rank": 1 + sum(1 for item in history if item <= total),
        }
    found = []
    for index, item in sorted(hit.items()):
        days = sum(1 for j in range(index - CONFIRM_DAYS + 1, index + 1) if j in hit)
        if days >= CONFIRM_MIN:
            found.append({**item, "days": days})
    return found


def standardized_gone(calendar: list[str], shares: dict[str, dict[str, float]], action: dict) -> float | None:
    """各只用出手当天的标准差把累计变化标准化，再取回撤比例的中位数。"""
    prev = calendar[action["pos"] - 1]
    last = calendar[-1]
    gones = []
    for code, spike in action["spikes"].items():
        sigma = action["stdevs"].get(code) or 0
        if spike <= 0 or sigma <= 0:
            continue
        if code not in shares.get(prev, {}) or code not in shares.get(last, {}):
            continue
        cum = (shares[last][code] - shares[prev][code]) / sigma
        gones.append(max(0.0, 1 - cum / (spike / sigma)))
    return None if not gones else min(median(gones), 1.0)


def _hold(flows: list[tuple[str, float | None]], action: dict | None, gone: float | None, since: int | None) -> dict | None:
    """bought / post 仍是净申购金额，方便看规模。gone 是各只标准化回撤的中位数，卖不卖只看它。"""
    if not action:
        return None
    bought = action["delta"]
    days = 0 if since is None else since
    post = sum(value for _day, value in flows[action["pos"] + 1:] if value is not None)
    return {
        "bought": bought, "post": post, "now": bought + post, "gone": gone,
        "line": bought * (1 - GONE), "sold": gone is not None and gone > GONE and 0 < days <= SELL_WINDOW,
        "days": days, "since": action["day"],
    }


def strength_of(signal: float, since: int) -> float:
    """离开出手日越久越弱，第 5 个交易日降到 0。"""
    return signal * max(0.0, 1 - since / RECENT)


def label_of(since: int | None, hold: dict | None, sell_since: int | None = None) -> str:
    """近 5 天内有出手算「在出手」，有减仓或出手后回吐超 3 成算「可能在卖」；买卖都有，以更近的为准。"""
    buying = since is not None and since < RECENT
    selling = sell_since is not None and sell_since < RECENT
    if buying and (not selling or since < sell_since):
        return "在出手"
    if selling:
        return "可能在卖"
    if hold and hold["sold"] and hold["days"] <= SELL_WINDOW:
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
    codes = [code for code, _name in TEAM_BOOK]
    shares = dict(zip(codes, tape._parallel(codes, lambda code: tape.fund_share_rows(code, today))))
    closes = dict(zip(codes, tape._parallel(codes, lambda code: tape.fund_bar_rows(code, today))))
    idx = tape.cached(f"indexpct:{start}", lambda: tape.pro().index_daily(
        ts_code="000001.SH", start_date=start, end_date=tape._ymd(today), fields="trade_date,pct_chg",
    ))
    pct = {tape.ymd(row.get("trade_date")): tape.num(row.get("pct_chg")) for row in idx if tape.num(row.get("pct_chg")) is not None}
    return summarize(shares, closes, pct)


def summarize(shares: dict[str, list[dict]], closes: dict[str, list[dict]], pct: dict[str, float]) -> dict:
    sh: dict[str, dict[str, float]] = {}
    px: dict[str, dict[str, float]] = {}
    codes = []
    for code, _name in TEAM_BOOK:
        share_rows = [row for row in shares.get(code) or [] if tape.num(row.get("fd_share")) is not None]
        bar_rows = [row for row in closes.get(code) or [] if tape.num(row.get("close")) is not None]
        if len(share_rows) < 2 or len(bar_rows) < 2:
            continue
        codes.append(code)
        for row in share_rows:
            sh.setdefault(tape.ymd(row.get("trade_date")), {})[code] = tape.num(row.get("fd_share"))
        for row in bar_rows:
            px.setdefault(tape.ymd(row.get("trade_date")), {})[code] = tape.num(row.get("close"))
    covered = sorted(day for day in sh if day in pct)
    calendar = [day for day in sorted(pct) if covered and covered[0] <= day <= covered[-1]]
    flows = daily_flows(calendar, sh, px, codes) if codes else []
    deltas = code_deltas(calendar, sh, codes)
    money = {code: {day: change * px[day][code] / 1e4 for day, change in series.items() if code in px.get(day, {})} for code, series in deltas.items()}
    if max((len(series) for series in deltas.values()), default=0) < MIN_BASE:
        return {"ok": False, "text": "取到的宽基 ETF 份额不够长，判不了出手日"}
    actions = detect_actions(calendar, deltas, pct, money)
    sells = detect_sells(calendar, deltas, pct, money)
    last = actions[-1] if actions else None
    since = None if not last else len(calendar) - 1 - last["pos"]
    hold = _hold(flows, last, None if last is None else standardized_gone(calendar, sh, last), since)
    sell_last = sells[-1] if sells else None
    sell_since = None if not sell_last else len(calendar) - 1 - sell_last["pos"]
    label = label_of(since, hold, sell_since)
    counts = {n: sum(1 for item in actions if len(calendar) - 1 - item["pos"] < n) for n in (1, 3, 5, 30, 60)}
    series = []
    for i in range(max(1, len(flows) - 60), len(flows)):
        day, value = flows[i]
        series.append({
            "date": tape.iso(day), "value": sum(sh[day].values()) / 1e4 if day in sh else None, "delta": value or 0.0,
            "index_pct": pct.get(day), "action": any(item["day"] == day for item in actions),
        })
    return {
        "ok": True, "label": label, "codes": codes, "counted": len(codes), "latest": tape.iso(calendar[-1]) if calendar else "",
        "strength": 0.0 if since is None or label != "在出手" else strength_of(last["signal"], since),
        "total": sum(sh[calendar[-1]].values()) / 1e4 if calendar and calendar[-1] in sh else None,
        "actions": [{key: value for key, value in {**item, "day": tape.iso(item["day"])}.items() if key not in ("stdevs", "spikes")} for item in actions[-6:]],
        "last": None if not last else {key: value for key, value in {**last, "day": tape.iso(last["day"]), "since": since}.items() if key not in ("stdevs", "spikes")},
        "hold": hold and {**hold, "since": tape.iso(hold["since"])},
        "sell": None if not sell_last else {**sell_last, "day": tape.iso(sell_last["day"]), "since": sell_since},
        "sells": [{**item, "day": tape.iso(item["day"])} for item in sells[-6:]],
        "counts": counts, "series": series,
        "history": history(actions, calendar, pct),
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
