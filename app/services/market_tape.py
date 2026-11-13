"""全市场资金和情绪切片。只接当前账号有权限的 Tushare 接口。

etf_share_size、rt_etf_k、limit_step、yc_cb 没有权限，不在这里。
南向 south_money 是存量，当日方向用港股通买卖额差，并写明口径。
"""

from __future__ import annotations

import math
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app import config
from app.tools import cache

PARTS = (
    "turnover", "etf", "margin", "margin_industry", "south", "shibor",
    "supply", "funds", "breadth", "limits", "index", "sw", "macro",
    "fx", "us", "hk", "ipo", "calendar", "unlock", "valuation", "pulse",
)
_ZONE = ZoneInfo("Asia/Shanghai")
_TTL = 6 * 3600

# 宽基名单与国家队 BROAD 对齐。大盘/中小盘/科创创业只用于市场资金里的分组展示。
# 份额用 fund_share，价格和成交用 fund_daily。净申购金额 = 份额变化 × 收盘价。
ETF_BOOK = (
    ("510300.SH", "华泰柏瑞沪深300ETF", "大盘"),
    ("510310.SH", "易方达沪深300ETF", "大盘"),
    ("510330.SH", "华夏沪深300ETF", "大盘"),
    ("159919.SZ", "嘉实沪深300ETF", "大盘"),
    ("510050.SH", "华夏上证50ETF", "大盘"),
    ("512050.SH", "华夏中证A500ETF", "大盘"),
    ("560610.SH", "中证A500ETF", "大盘"),
    ("510500.SH", "南方中证500ETF", "中小盘"),
    ("159922.SZ", "嘉实中证500ETF", "中小盘"),
    ("512500.SH", "华夏中证500ETF", "中小盘"),
    ("512100.SH", "南方中证1000ETF", "中小盘"),
    ("159845.SZ", "华夏中证1000ETF", "中小盘"),
    ("560010.SH", "广发中证1000ETF", "中小盘"),
    ("159915.SZ", "易方达创业板ETF", "科创创业"),
    ("159977.SZ", "天弘创业板ETF", "科创创业"),
    ("159952.SZ", "广发创业板ETF", "科创创业"),
    ("588000.SH", "华夏科创50ETF", "科创创业"),
    ("588080.SH", "易方达科创50ETF", "科创创业"),
)
_INDEXES = (
    ("000001.SH", "上证指数"),
    ("000300.SH", "沪深300"),
    ("000852.SH", "中证1000"),
    ("399006.SZ", "创业板指"),
)
_L1 = (
    "801010.SI", "801030.SI", "801040.SI", "801050.SI", "801080.SI",
    "801110.SI", "801120.SI", "801130.SI", "801140.SI", "801150.SI",
    "801160.SI", "801170.SI", "801180.SI", "801200.SI", "801210.SI",
    "801230.SI", "801710.SI", "801720.SI", "801730.SI", "801740.SI",
    "801750.SI", "801760.SI", "801770.SI", "801780.SI", "801790.SI",
    "801880.SI", "801890.SI", "801950.SI", "801960.SI", "801970.SI",
    "801980.SI",
)


def build(part: str, today: date | None = None) -> dict:
    if part not in PARTS:
        raise ValueError(f"未知切片 {part}")
    day = today or datetime.now(_ZONE).date()
    if part == "pulse":
        return _pulse(day)
    return {
        "turnover": _turnover,
        "etf": _etf,
        "margin": _margin,
        "margin_industry": _margin_industry,
        "south": _south,
        "shibor": _shibor,
        "supply": _supply,
        "funds": _funds,
        "breadth": _breadth,
        "limits": _limits,
        "index": _index,
        "sw": _sw,
        "macro": _macro,
        "fx": _fx,
        "us": _us,
        "hk": _hk,
        "ipo": _ipo,
        "calendar": _calendar,
        "unlock": _unlock,
        "valuation": _valuation,
    }[part](day)


# 三个模块共用 Modified Z-Score。参数只用日度序列，不用重叠滚动窗口。
# Mz = 0.6745 × (当前值 − 中位数) / MAD，等于 (当前值 − 中位数) / (1.4826 × MAD)。
BASELINE_DAYS = 60
MZ_K = 0.6745
TURN_HOT, TURN_COLD = 1.5, -1.0
TURN_LOG_FLOOR = math.log(1.05)     # 对数成交额至少偏离 5% 才认信号
TURN_FALLBACK = 0.10                 # 基线不足时退回 ±10%
ETF_Z = 1.5
PULSE_DAYS = 3                       # 宽基 / 两融主触发窗口（交易日）
ETF_FLOOR = 0.003                    # 3 日累计份额变化至少 0.3%
ETF_FALLBACK = 0.006                 # 基线不足时，3 日 >+0.6% / <−0.6%
ETF_SELL_MIN = 2                     # 净流出：脉冲窗口里至少这么多天在卖
SPLIT_RATE = 0.5                     # 单日份额变动超过一半，当成拆分合并，不进基线和信号
MARGIN_UP, MARGIN_DOWN = 1.5, -1.5
MARGIN_SOFT, MARGIN_FAST = -1.0, -2.0


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _baseline(daily: list[float]) -> tuple[float | None, float | None, bool]:
    """最近 60 个日度点的中位数和 MAD。样本不足或 MAD 为 0 时，基线不够。"""
    if len(daily) < BASELINE_DAYS:
        return (None, None, False)
    sample = daily[-BASELINE_DAYS:]
    med = _median(sample)
    mad = _median([abs(value - med) for value in sample])
    return med, mad, mad > 0


def modified_z(value: float | None, center: float | None, mad: float | None) -> float | None:
    if value is None or center is None or not mad:
        return None
    return MZ_K * (value - center) / mad


def _window(windows: dict, n: int):
    if not isinstance(windows, dict):
        return None
    if n in windows:
        return windows[n]
    return windows.get(str(n))


def judge_turnover_votes(sides: dict, last_two: tuple[str, str]) -> str:
    """1/3/5/30 日各有一档。30 日要和另外至少一档同向，最近两日也要同向。单独一天不改档。"""
    known = [sides.get(n) for n in (1, 3, 5, 30)]
    if not any(item and item != "没有" for item in known):
        return "没有"

    def agree(word: str) -> bool:
        if sides.get(30) != word:
            return False
        if word not in (sides.get(1), sides.get(3), sides.get(5)):
            return False
        return last_two[0] == word and last_two[1] == word

    if agree("冷清"):
        return "冷清"
    if agree("活跃"):
        return "活跃"
    return "正常"


def judge_etf_signal(mz_pulse, mz30, mz60, cum_pulse, sell_days: int, outflow_streak: bool, enough: bool) -> str:
    """净流入看 3/30/60 日偏离，且这 3 天里没有连续两天净流出。净流出还要 3 天里至少 2 天在卖。"""
    if cum_pulse is None:
        return "没有"
    if not enough:
        if cum_pulse > ETF_FALLBACK and not outflow_streak:
            return "净流入"
        if cum_pulse < -ETF_FALLBACK and sell_days >= ETF_SELL_MIN:
            return "净流出"
        return "基本没变"
    if abs(cum_pulse) < ETF_FLOOR:
        return "基本没变"
    longer_in = (mz30 is None or mz30 > ETF_Z) and (mz60 is None or mz60 > ETF_Z)
    if mz_pulse is not None and mz_pulse > ETF_Z and longer_in and not outflow_streak:
        return "净流入"
    longer_out = (mz30 is None or mz30 < -ETF_Z) and (mz60 is None or mz60 < -ETF_Z)
    if mz_pulse is not None and mz_pulse < -ETF_Z and sell_days >= ETF_SELL_MIN and longer_out:
        return "净流出"
    return "基本没变"


def judge_margin_signal(mz_pulse, mz30, mz60, enough: bool) -> str:
    """3 日是主触发。样本不足时不判升也不判降。30 日没有数时可以判升/降，但不标加速下降。"""
    if not enough or mz_pulse is None:
        return "持平"
    rising_30 = mz30 is None or mz30 > MARGIN_SOFT
    if mz_pulse < MARGIN_FAST and mz30 is not None and mz30 < MARGIN_SOFT:
        return "加速下降"
    if mz_pulse < MARGIN_DOWN or (mz_pulse < MARGIN_SOFT and mz30 is not None and mz30 < MARGIN_DOWN):
        return "在降"
    if mz_pulse > MARGIN_UP and rising_30:
        return "在升"
    return "持平"


def _turn_side(mean_log: float | None, med: float | None, mad: float | None, enough: bool, level_ratio: float | None) -> str:
    if mean_log is None:
        return "没有"
    if not enough:
        if level_ratio is None:
            return "没有"
        if level_ratio <= -TURN_FALLBACK:
            return "冷清"
        if level_ratio >= TURN_FALLBACK:
            return "活跃"
        return "正常"
    if abs(mean_log - med) < TURN_LOG_FLOOR:
        return "正常"
    score = modified_z(mean_log, med, mad)
    if score is None:
        return "正常"
    if score > TURN_HOT:
        return "活跃"
    if score < TURN_COLD:
        return "冷清"
    return "正常"


def _compound(rates: list[float]) -> float:
    value = 1.0
    for rate in rates:
        value *= 1 + rate
    return value - 1


def _rate_z(rates: list[float], n: int) -> tuple[float | None, float | None, bool]:
    """窗口累计变化，对照窗口之前 60 个日度变化率。n 日累计的波动按 √n 放大，避免用重叠窗口估波动。"""
    if len(rates) < n:
        return None, None, False
    cum = _compound(rates[-n:])
    prior = rates[-n - BASELINE_DAYS:-n]
    med, mad, enough = _baseline(prior)
    if not enough or med is None or not mad:
        return cum, None, False
    return cum, modified_z(cum, n * med, mad * math.sqrt(n)), True


SPANS = (1, 3, 5, 30, 60)


def _ago(days: list, n: int):
    """往前 n 个交易日；样本不够时用手里最早的一天，避免短样本把每个窗口都空着。"""
    if len(days) <= 1:
        return None
    return days[-1 - n] if len(days) > n else days[0]


def judge_pulse(turnover: str, etf: str, margin: str) -> str:
    """宽基定方向，成交定冷热，两融定退潮。成交正常且两融加速下降时，净流入也不算在进。"""
    if etf == "净流入" and turnover in ("正常", "平时") and margin == "加速下降":
        return "观望"
    if etf == "净流入" and turnover != "冷清":
        return "在进"
    if turnover == "冷清" and margin in ("在降", "加速下降"):
        return "偏弱"
    return "观望"


def _one_log_side(point: float, prior: list[float]) -> str:
    med, mad, enough = _baseline(prior)
    if enough:
        return _turn_side(point, med, mad, True, None)
    if not prior:
        return "没有"
    center = _median(prior)
    return _turn_side(point, center, None, False, math.exp(point - center) - 1)


def _turnover_sides(logs: list[float]) -> tuple[dict, dict, bool]:
    sides, scores = {}, {}
    short = len(logs) < BASELINE_DAYS + 30
    for n in (1, 3, 5, 30):
        if len(logs) <= n:
            sides[n] = "没有"
            scores[n] = None
            short = True
            continue
        window = logs[-n:]
        mean_log = sum(window) / len(window)
        prior = logs[:-n]
        med, mad, enough = _baseline(prior)
        if not enough:
            short = True
            center = _median(prior) if prior else None
            ratio = None if center is None else math.exp(mean_log - center) - 1
            sides[n] = _turn_side(mean_log, center, None, False, ratio)
            scores[n] = None
        else:
            sides[n] = _turn_side(mean_log, med, mad, True, None)
            scores[n] = modified_z(mean_log, med, mad)
    return sides, scores, short


def _prior_day_side(logs: list[float]) -> str:
    if len(logs) < 2:
        return "没有"
    return _one_log_side(logs[-2], logs[:-2])


def _share_rates(rows: list[dict]) -> list[float]:
    ordered = sorted([row for row in rows if _num(row.get("fd_share")) is not None], key=lambda row: _ymd(row.get("trade_date")))
    rates = []
    for prev, row in zip(ordered, ordered[1:]):
        if _day_gap(_ymd(prev.get("trade_date")), _ymd(row.get("trade_date"))) > 4:
            continue
        base = _num(prev.get("fd_share")) or 0
        if base <= 0:
            continue
        rate = ((_num(row.get("fd_share")) or 0) - base) / base
        if abs(rate) > SPLIT_RATE:
            continue
        rates.append(rate)
    return rates


def _judge_broad_etf(shares: dict[str, list[dict]], fallback_cum: float | None = None) -> dict:
    """每只 ETF 用自己近 60 天的日度份额变化率估常态，篮子取各只偏离的中位数。"""
    books = [rates for code, _name, _group in ETF_BOOK if len(rates := _share_rates(shares.get(code) or [])) >= PULSE_DAYS]
    if not books:
        label = judge_etf_signal(None, None, None, fallback_cum, 0, False, False)
        return {"label": label, "votes": [PULSE_DAYS] if label in ("净流入", "净流出") else [], "baseline": "不足", "mz": {}}
    scores = {1: [], 3: [], 5: [], 30: [], 60: []}
    cums = []
    ready = 0
    for rates in books:
        cum_pulse, mz_pulse, ok_pulse = _rate_z(rates, PULSE_DAYS)
        if cum_pulse is not None:
            cums.append(cum_pulse)
        if ok_pulse and mz_pulse is not None:
            ready += 1
            scores[PULSE_DAYS].append(mz_pulse)
        for n in (1, 5, 30, 60):
            _cum, mz, ok = _rate_z(rates, n)
            if ok and mz is not None:
                scores[n].append(mz)
    slots = []
    for offset in range(PULSE_DAYS, 0, -1):
        day_rates = [rates[-offset] for rates in books if len(rates) >= offset]
        slots.append(_median(day_rates) if day_rates else 0.0)
    sell_days = sum(1 for rate in slots if rate < 0)
    streak = any(slots[i] < 0 and slots[i + 1] < 0 for i in range(len(slots) - 1))
    enough = ready >= max(1, len(books) / 2) and bool(scores[PULSE_DAYS])
    mid = lambda items: _median(items) if items else None
    label = judge_etf_signal(mid(scores[PULSE_DAYS]), mid(scores[30]), mid(scores[60]), mid(cums), sell_days, streak, enough)
    if label == "净流入":
        votes = [n for n in (PULSE_DAYS, 30, 60) if scores[n] and mid(scores[n]) > ETF_Z] or [PULSE_DAYS]
    elif label == "净流出":
        votes = [n for n in (PULSE_DAYS, 30, 60) if scores[n] and mid(scores[n]) < -ETF_Z] or [PULSE_DAYS]
    else:
        votes = []
    return {"label": label, "votes": votes, "baseline": "够" if enough else "不足",
            "mz": {n: mid(scores[n]) for n in (1, 3, 5, 30, 60)}}


def _margin_rates(days: list[str], by_day: dict) -> tuple[list[float], list[float]]:
    rates, nets = [], []
    for prev, day in zip(days, days[1:]):
        if _day_gap(prev, day) > 4:
            continue
        base = by_day[prev]["rzye"]
        if not base:
            continue
        rates.append(by_day[day]["rzye"] / base - 1)
        nets.append(by_day[day]["rzmre"] - by_day[day]["rzche"])
    return rates, nets


def _judge_margin_book(days: list[str], by_day: dict) -> tuple[str, list[int], str, list[str]]:
    rates, nets = _margin_rates(days, by_day)
    got = {}
    for n in (1, 3, 5, 30, 60):
        _cum, mz, ok = _rate_z(rates, n)
        got[n] = (mz if ok else None, ok, _cum)
    mz_pulse, ok_pulse, cum_pulse = got[PULSE_DAYS][0], got[PULSE_DAYS][1], got[PULSE_DAYS][2]
    mz30, ok30 = got[30][0], got[30][1]
    mz60, ok60 = got[60][0], got[60][1]
    label = judge_margin_signal(mz_pulse if ok_pulse else None, mz30 if ok30 else None, mz60 if ok60 else None, ok_pulse)
    reversal = bool(ok_pulse and len(rates) >= 2 and cum_pulse and rates[-1] and (cum_pulse > 0) != (rates[-1] > 0))
    net_pulse = sum(nets[-PULSE_DAYS:]) if len(nets) >= PULSE_DAYS else None
    deep = bool(ok30 and mz30 is not None and mz30 < MARGIN_FAST and (not ok60 or mz60 is None or mz60 < MARGIN_FAST) and net_pulse is not None and net_pulse < 0)
    if label == "加速下降":
        votes = [PULSE_DAYS, 30]
    elif label in ("在降", "在升"):
        votes = [PULSE_DAYS] + ([30] if ok30 else [])
    else:
        votes = []
    marks = []
    if reversal:
        marks.append(f"两融 {PULSE_DAYS} 日方向和前一交易日相反，可能是噪音")
    if deep:
        marks.append("中期深度去杠杆")
    return label, votes, "够" if ok_pulse else "不足", marks, {n: got[n][0] for n in (1, 3, 5, 30, 60)}


def format_turnover(rows: list[dict], index_pct: dict[str, float] | None = None, index_close: dict[str, float] | None = None) -> dict:
    rows = [row for row in rows if _num(row.get("amount")) is not None]
    rows.sort(key=lambda row: _ymd(row.get("trade_date")))
    if len(rows) < 6:
        return _body([], "没有取到足够的全市场成交额")
    latest = rows[-1]
    amounts = [_num(row.get("amount")) for row in rows]
    amounts = [item for item in amounts if item and item > 0]
    logs = [math.log(item) for item in amounts]
    avg60 = _mean(rows[-60:], "amount")
    windows = {n: None if not avg60 else _mean(rows[-n:], "amount") / avg60 - 1 for n in SPANS}
    sides, scores, baseline_short = _turnover_sides(logs)
    label = judge_turnover_votes(sides, (sides.get(1, "没有"), _prior_day_side(logs)))
    votes = [n for n in (1, 3, 5, 30) if sides.get(n) == label and label in ("冷清", "活跃")]
    ratio5 = windows[5]
    year = rows[-240:]
    amounts = [_num(row.get("amount")) for row in year]
    amounts = [item for item in amounts if item is not None]
    pctile = _pctile(amounts, _num(latest.get("amount")))
    down = 0
    for row in rows[-5:]:
        pct = (index_pct or {}).get(_ymd(row.get("trade_date")))
        if pct is not None and pct < 0:
            down += 1
    facts = [
        {"id": "mkt.turnover", "text": f"{_iso(latest.get('trade_date'))} 沪深成交额 {_yi_plain(_num(latest.get('amount')))}，1 / 3 / 5 / 30 日对数成交额对照自身近 60 日，判定 {label}" + ("，基线不足" if baseline_short else "")},
        {"id": "mkt.turnover_windows", "text": "相对过去 60 日：" + "，".join(f"{n} 日 {_pct(windows[n])}" for n in SPANS)},
        {"id": "mkt.turnover_pctile", "text": f"成交额处在近一年 {'' if pctile is None else str(round(pctile * 100)) + '%'} 分位"},
    ]
    if down:
        facts.append({"id": "mkt.turnover_down", "text": f"近 5 天里 {down} 天指数下跌"})
    metrics = {
        "date": _iso(latest.get("trade_date")),
        "amount": _num(latest.get("amount")),
        "ratio5": ratio5,
        "avg60": avg60,
        "windows": windows,
        "mz": scores,
        "pctile": pctile,
        "label": label,
        "votes": votes,
        "baseline": "不足" if baseline_short else "够",
        "down_days": down,
        "series": [
            {
                "date": _iso(row.get("trade_date")), "value": _num(row.get("amount")),
                "up": (index_pct or {}).get(_ymd(row.get("trade_date")), 0) >= 0,
                "index": (index_close or {}).get(_ymd(row.get("trade_date"))),
                "index_pct": (index_pct or {}).get(_ymd(row.get("trade_date"))),
            }
            for row in rows[-60:]
        ],
    }
    return _body(facts, " ".join(item["text"] for item in facts) + "。冷清或活跃只说明热不热，不代表钱在进还是出。盘中同一时刻没有接。", metrics)


def format_etf(shares: dict[str, list[dict]], closes: dict[str, list[dict]]) -> dict:
    """shares[code] = [{trade_date, fd_share}]，单位万份。closes 用来把份额变化换成亿元。"""
    groups = {"大盘": 0.0, "中小盘": 0.0, "科创创业": 0.0}
    group_known = {"大盘": False, "中小盘": False, "科创创业": False}
    start_share = 0.0
    end_share = 0.0
    counted = 0
    day_net: dict[str, float] = {}
    fund_money: list[dict] = []
    for code, name, group in ETF_BOOK:
        rows = sorted(shares.get(code) or [], key=lambda row: _ymd(row.get("trade_date")))
        rows = [row for row in rows if _num(row.get("fd_share")) is not None]
        if len(rows) < 2:
            continue
        recent = rows[-(PULSE_DAYS + 1):]
        counted += 1
        start_share += _num(recent[0].get("fd_share")) or 0
        end_share += _num(recent[-1].get("fd_share")) or 0
        money_i = 0.0
        money_known = False
        for prev, row in zip(recent, recent[1:]):
            day = _ymd(row.get("trade_date"))
            delta = (_num(row.get("fd_share")) or 0) - (_num(prev.get("fd_share")) or 0)
            day_net[day] = day_net.get(day, 0.0) + delta
            price = _close_on(closes.get(code) or [], day)
            if price is not None:
                piece = delta * price / 10000
                groups[group] += piece
                group_known[group] = True
                money_i += piece
                money_known = True
        if money_known:
            fund_money.append({"code": code, "name": name, "group": group, "money": round(money_i, 2)})
    buy_days = sum(1 for day in sorted(day_net)[-PULSE_DAYS:] if day_net[day] > 0)
    if not counted or start_share <= 0:
        return _body([], "没有取到宽基 ETF 份额。etf_share_size 没有权限，这里用的是 fund_share")
    ratio = end_share / start_share - 1
    money = sum(groups.values())
    series = _etf_series(shares)
    windows = series.get("windows") or {n: None for n in SPANS}
    windows[PULSE_DAYS] = ratio
    judged = _judge_broad_etf(shares, ratio)
    label = judged["label"]
    bits = [
        f"宽基名单 {len(ETF_BOOK)} 只里取到 {counted} 只，份额变化 {_pct(ratio)}，判定 {label}" + ("，基线不足" if judged["baseline"] == "不足" else ""),
        f"近 {PULSE_DAYS} 个交易日里篮子净申购的天数 {buy_days}",
    ]
    if any(group_known.values()):
        bits.append(
            "净申购金额约 "
            f"{money:+.2f} 亿元（份额变化 × 收盘价）："
            f"大盘 {groups['大盘']:+.2f}，中小盘 {groups['中小盘']:+.2f}，科创创业 {groups['科创创业']:+.2f}"
        )
    text = "。".join(bits) + "。这是 fund_share 加收盘价的近似，不是交易所的份额规模接口。"
    top_in = sorted((row for row in fund_money if (row.get("money") or 0) > 0), key=lambda row: row["money"], reverse=True)[:3]
    metrics = {
        "ratio": ratio, "buy_days": buy_days, "label": label, "money": money,
        "groups": groups, "counted": counted, "votes": judged["votes"], "baseline": judged["baseline"],
        "mz": judged.get("mz") or {},
        "top_in": top_in,
        **series, "windows": windows,
    }
    return _body([{"id": "mkt.etf", "text": text}], text, metrics)


def _etf_series(shares: dict[str, list[dict]]) -> dict:
    """宽基合计总份额（亿份）。名单与国家队宽基一致；某天缺数据的不进当天合计，日变化只在两边都有的代码上算。"""
    by_day: dict[str, dict[str, float]] = {}
    codes = []
    for code, _name, _group in ETF_BOOK:
        rows = [row for row in shares.get(code) or [] if _num(row.get("fd_share")) is not None]
        if len(rows) < 2:
            continue
        codes.append(code)
        for row in rows:
            by_day.setdefault(_ymd(row.get("trade_date")), {})[code] = _num(row.get("fd_share"))
    min_need = max(3, (len(codes) + 1) // 2)
    days = sorted(day for day, slot in by_day.items() if len(slot) >= min_need)
    if len(days) < 2:
        return {"series": [], "total": None, "day_change": None, "ratio20": None, "pctile": None, "latest_date": ""}
    series = []
    totals = []
    day_changes = []
    for index, day in enumerate(days):
        slot = by_day[day]
        total = sum(slot.values()) / 1e4
        totals.append(total)
        gap = 0 if index == 0 else _day_gap(days[index - 1], day)
        if index == 0 or gap > 4:
            delta = 0.0
        else:
            prev = by_day[days[index - 1]]
            common = [code for code in slot if code in prev]
            if len(common) < min_need:
                delta = 0.0
            else:
                delta = (sum(slot[code] for code in common) - sum(prev[code] for code in common)) / 1e4
        day_changes.append(delta)
        series.append({"date": _iso(day), "value": total, "delta": delta})
    windows = {n: _growth(totals[-1], totals[-1 - n]) if len(totals) > n else None for n in SPANS}
    last_gap = _day_gap(days[-2], days[-1])
    return {
        "series": series[-60:],
        "total": totals[-1],
        "day_change": None if last_gap > 4 else day_changes[-1],
        "windows": windows,
        "pctile": _pctile(totals[-240:], totals[-1]),
        "latest_date": _iso(days[-1]),
        "items": [_etf_item(code, shares.get(code) or []) for code in codes],
    }


def _etf_item(code: str, rows: list[dict]) -> dict:
    rows = sorted([row for row in rows if _num(row.get("fd_share")) is not None], key=lambda row: _ymd(row.get("trade_date")))
    name = next((item[1] for item in ETF_BOOK if item[0] == code), code)
    last = _num(rows[-1].get("fd_share")) / 1e4
    prev = _num(rows[-2].get("fd_share")) / 1e4
    return {"code": code, "name": name, "share": last, "day_change": last - prev}


def format_margin(rows: list[dict], turnover: float | None = None) -> dict:
    by_day: dict[str, dict] = {}
    for row in rows:
        day = _ymd(row.get("trade_date"))
        if len(day) < 8:
            continue
        slot = by_day.setdefault(day, {"rzye": 0.0, "rzmre": 0.0, "rzche": 0.0, "ex": set()})
        exchange = str(row.get("exchange_id") or "")
        if exchange:
            slot["ex"].add(exchange)
        for key in ("rzye", "rzmre", "rzche"):
            value = _num(row.get(key))
            if value is not None:
                slot[key] += value
    days = sorted(day for day, slot in by_day.items() if len(slot["ex"]) >= 2 or not slot["ex"])
    if len(days) < 6:
        return _body([], "没有取到全市场两融")
    latest = by_day[days[-1]]
    prev = by_day[days[-2]]
    windows = {n: _growth(latest["rzye"], by_day[_ago(days, n)]["rzye"]) if _ago(days, n) else None for n in SPANS}
    ratio_day = windows[1]
    ratio5 = windows[5]
    ratio30 = windows[30]
    label, votes, baseline, marks, mz = _judge_margin_book(days, by_day)
    net = latest["rzmre"] - latest["rzche"]
    share = None if not turnover else latest["rzmre"] / (turnover * 1e8)
    text = (
        f"{_iso(days[-1])} 融资余额 {_yuan_yi(latest['rzye'])}，"
        + "，".join(f"{n} 日 {_pct(windows[n])}" for n in SPANS)
        + f"，判定 {label}" + ("，基线不足" if baseline == "不足" else "") + "。"
        f"当日融资买入 {_yuan_yi(latest['rzmre'])}，偿还 {_yuan_yi(latest['rzche'])}，净买入 {_yuan_yi(net)}"
    )
    if share is not None:
        text += f"，融资买入占成交 {_pct(share)}"
    text += "。"
    metrics = {
        "date": _iso(days[-1]),
        "balance": latest["rzye"] / 1e8,
        "ratio_day": ratio_day,
        "ratio5": ratio5,
        "ratio4": ratio30,
        "windows": windows,
        "mz": mz,
        "label": label,
        "votes": votes,
        "baseline": baseline,
        "marks": marks,
        "buy": latest["rzmre"] / 1e8,
        "repay": latest["rzche"] / 1e8,
        "net": net / 1e8,
        "share": share,
        "pctile": _pctile([by_day[day]["rzye"] for day in days[-240:]], latest["rzye"]),
        "series": [
            {"date": _iso(day), "value": by_day[day]["rzye"] / 1e8,
             "delta": (by_day[day]["rzmre"] - by_day[day]["rzche"]) / 1e8}
            for day in days[-60:]
        ],
    }
    return _body([{"id": "mkt.margin", "text": text}], text, metrics)


def format_margin_industry(members: dict[str, str], rows: list[dict]) -> dict:
    buckets: dict[str, dict] = {}
    for row in rows:
        name = members.get(str(row.get("ts_code") or ""))
        if not name:
            continue
        slot = buckets.setdefault(name, {"rzye": 0.0, "net": 0.0})
        rzye = _num(row.get("rzye"))
        buy = _num(row.get("rzmre"))
        repay = _num(row.get("rzche"))
        if rzye is not None:
            slot["rzye"] += rzye
        if buy is not None and repay is not None:
            slot["net"] += buy - repay
    if not buckets:
        return _body([], "没有取到行业两融")
    ranked = sorted(buckets.items(), key=lambda item: item[1]["net"], reverse=True)
    top = "，".join(f"{name} {_yuan_yi(slot['net'])}" for name, slot in ranked[:5])
    bottom = "，".join(f"{name} {_yuan_yi(slot['net'])}" for name, slot in ranked[-5:])
    text = f"按申万一级加总当日融资净买入。净买入最多：{top}。净买入最少：{bottom}。"
    rows = [{"name": name, "net": slot["net"] / 1e8} for name, slot in ranked]
    return _body([{"id": "mkt.margin_ind", "text": text}], text, {"rows": rows})


def format_south(hsgt: list[dict], ggt: list[dict]) -> dict:
    hsgt = sorted(hsgt, key=lambda row: _ymd(row.get("trade_date")))
    if len(hsgt) < 6:
        return _body([], "没有取到南向资金")
    latest = hsgt[-1]
    level = _num(latest.get("south_money"))
    prev = _num(hsgt[-2].get("south_money"))
    day_diff = None if level is None or prev is None else level - prev
    diffs = []
    for older, newer in zip(hsgt, hsgt[1:]):
        a, b = _num(older.get("south_money")), _num(newer.get("south_money"))
        if a is not None and b is not None:
            diffs.append(b - a)
    diff5 = sum(diffs[-5:]) if diffs else None
    ggt_rows = sorted(ggt, key=lambda row: _ymd(row.get("trade_date")))
    net = None
    if ggt_rows:
        buy = _num(ggt_rows[-1].get("buy_amount"))
        sell = _num(ggt_rows[-1].get("sell_amount"))
        if buy is not None and sell is not None:
            net = buy - sell
    text = (
        f"{_iso(latest.get('trade_date'))} south_money 存量 {_round(level)}，相对前一日差额 {_round(day_diff)}（接口单位，不是已经换算的亿元）。"
        f"近 5 日差额合计 {_round(diff5)}。这个字段近期几乎不动，不能直接写成当日净买入。"
    )
    if net is not None:
        text += (
            f" {_iso(ggt_rows[-1].get('trade_date'))} 港股通买卖额差 {net:+.2f}"
            "（buy_amount − sell_amount，单位沿用 ggt_daily）。"
        )
    text += " 北向不按日使用。"
    nets = [
        (_ymd(row.get("trade_date")), _num(row.get("buy_amount")) - _num(row.get("sell_amount")))
        for row in ggt_rows
        if _num(row.get("buy_amount")) is not None and _num(row.get("sell_amount")) is not None
    ]
    sums5 = [sum(item[1] for item in nets[index - 5:index]) for index in range(5, len(nets) + 1)]
    sums20 = [sum(item[1] for item in nets[index - 20:index]) for index in range(20, len(nets) + 1)]
    south_windows = {n: sum(item[1] for item in nets[-n:]) if len(nets) >= n else None for n in SPANS}
    return _body([{"id": "mkt.south", "text": text}], text, {
        "date": _iso(latest.get("trade_date")),
        "level": level,
        "day_diff": day_diff,
        "diff5": diff5,
        "ggt_date": _iso(ggt_rows[-1].get("trade_date")) if ggt_rows else "",
        "ggt_net": net,
        "net5": sums5[-1] if sums5 else None,
        "net20": sums20[-1] if sums20 else None,
        "windows": south_windows,
        "five_avg": (sum(sums5) / len(sums5)) if sums5 else None,
        "pctile20": _pctile(sums20[-240:], sums20[-1]) if sums20 else None,
        "series": [{"date": _iso(day), "value": value} for day, value in nets[-60:]],
    })


def format_shibor(rows: list[dict]) -> dict:
    rows = sorted([row for row in rows if _num(row.get("1w")) is not None], key=lambda row: _ymd(row.get("date")))
    if not rows:
        return _body([], "没有取到 Shibor")
    latest = rows[-1]
    avg = _mean(rows[-60:], "1w")
    now = _num(latest.get("1w"))
    side = "没有比较" if avg is None or now is None else ("低于" if now < avg else "高于")
    text = (
        f"{_iso(latest.get('date'))} Shibor 1周 {now}%，近 60 日均值 {None if avg is None else round(avg, 3)}%，{side}自身均值。"
        "这是 DR007 的近似，不是 DR007，也不是政策利率。"
    )
    return _body([{"id": "mkt.shibor", "text": text}], text, {
        "date": _iso(latest.get("date")),
        "rate": now,
        "avg": None if avg is None else round(avg, 3),
        "side": side,
        "label": "偏松" if side == "低于" else "偏紧" if side == "高于" else "没有",
    })


def format_supply(trades: list[dict], buybacks: list[dict]) -> dict:
    buy = sell = 0.0
    buy_known = sell_known = False
    for row in trades:
        vol = _num(row.get("change_vol"))
        price = _num(row.get("avg_price"))
        if vol is None or price is None:
            continue
        money = vol * price
        side = str(row.get("in_de") or "").upper()
        if side == "IN":
            buy += money
            buy_known = True
        elif side == "DE":
            sell += money
            sell_known = True
    latest_repo: dict[str, tuple[str, float]] = {}
    for row in buybacks:
        if "实施" not in str(row.get("proc") or ""):
            continue
        amount = _num(row.get("amount"))
        code = str(row.get("ts_code") or "")
        if amount is None or not code:
            continue
        stamp = _ymd(row.get("ann_date")) or _ymd(row.get("end_date"))
        prev = latest_repo.get(code)
        if prev is None or stamp >= prev[0]:
            latest_repo[code] = (stamp, amount)
    repo = sum(item[1] for item in latest_repo.values())
    if not buy_known and not sell_known and not repo:
        return _body([], "没有取到增减持和回购金额")
    # 回购公告是累计金额，不能加进这 28 天的增减持净额，否则会把「在拿走」说成「有人在接」。
    net = buy - sell
    label = "在拿走" if net < 0 else "有人在接" if net > 0 else "基本没变"
    text = (
        f"公告区间增持 {_yuan_yi(buy)}，减持 {_yuan_yi(sell)}，增持 − 减持 = {_yuan_yi(net)}，判定 {label}。"
        f"回购累计 {_yuan_yi(repo)}，按公司只留最新一条实施公告，是累计金额，不算进上面的净额。"
        "定增和再融资没有全市场日接口，没有算进去。"
    )
    return _body([{"id": "mkt.supply", "text": text}], text, {
        "buy": buy / 1e8, "sell": sell / 1e8, "repo": repo / 1e8, "net": net / 1e8, "label": label,
    })


def format_funds(rows: list[dict], today: date) -> dict:
    start = f"{today.year}{today.month:02d}01"
    recent_start = _ymd_offset(today, 70)
    picked = []
    recent = []
    for row in rows:
        kind = str(row.get("fund_type") or "")
        if kind not in ("股票型", "混合型"):
            continue
        issued = _ymd(row.get("issue_date") or row.get("found_date"))
        if len(issued) < 8:
            continue
        amount = _num(row.get("issue_amount"))
        item = (issued, amount, str(row.get("name") or ""))
        if issued >= start:
            picked.append(item)
        elif issued >= recent_start:
            recent.append(item)
    months = monthly_funds(rows, today)
    if not picked:
        if not recent:
            return _body([], "近两个月没有取到新发偏股基金")
        known = [item[1] for item in recent if item[1]]
        amount = f"{sum(known):.2f} 亿份" if known else "份额多数没公布"
        text = f"本月还没有新发偏股基金。近两个月 {len(recent)} 只，发行份额 {amount}。月频，只看方向，不参与短期判断。"
        return _body([{"id": "mkt.funds", "text": text}], text, {
            "count": len(recent), "amount": sum(known) if known else None, "scope": "近两个月", "months": months,
        })
    known = [item[1] for item in picked if item[1]]
    amount = f"{sum(known):.2f} 亿份" if known else "份额多数没公布"
    text = f"本月以来新发偏股基金 {len(picked)} 只，发行份额 {amount}。月频，只看方向，不参与短期判断。"
    return _body([{"id": "mkt.funds", "text": text}], text, {
        "count": len(picked), "amount": sum(known) if known else None, "scope": "本月", "months": months,
    })


def monthly_funds(rows: list[dict], today: date) -> list[dict]:
    """近 12 个月新发偏股基金（股票型 + 混合型）的只数和发行份额（亿份，没公布的不算）。"""
    keys = []
    year, month = today.year, today.month
    for _ in range(12):
        keys.append(f"{year}{month:02d}")
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    keys.reverse()
    slots = {key: {"count": 0, "amount": 0.0} for key in keys}
    for row in rows:
        if str(row.get("fund_type") or "") not in ("股票型", "混合型"):
            continue
        key = _ymd(row.get("issue_date") or row.get("found_date"))[:6]
        if key not in slots:
            continue
        slots[key]["count"] += 1
        amount = _num(row.get("issue_amount"))
        if amount:
            slots[key]["amount"] += amount
    return [{"month": f"{key[:4]}-{key[4:]}", **slots[key]} for key in keys]


def format_breadth(today_rows: list[dict], history: dict[str, list[float]]) -> dict:
    up = down = flat = 0
    highs = lows = 0
    for row in today_rows:
        pct = _num(row.get("pct_chg"))
        if pct is None:
            continue
        if pct > 0:
            up += 1
        elif pct < 0:
            down += 1
        else:
            flat += 1
        code = str(row.get("ts_code") or "")
        close = _num(row.get("close"))
        past = history.get(code) or []
        if close is None or len(past) < 20:
            continue
        if close >= max(past):
            highs += 1
        if close <= min(past):
            lows += 1
    if not (up or down):
        return _body([], "没有取到涨跌家数")
    text = f"上涨 {up} 家，下跌 {down} 家，平盘 {flat} 家。近 25 个交易日新高 {highs} 家，新低 {lows} 家。"
    return _body([{"id": "mkt.breadth", "text": text}], text, {"up": up, "down": down, "flat": flat, "highs": highs, "lows": lows})


def board_limit(ts_code: str) -> int | None:
    """涨跌停幅度（%）：主板 10，创业板和科创板 20。北交所 30，返回 None：它不进涨跌停数，也不进接力。ST 本来就不在名单里。"""
    code = str(ts_code or "")
    if code.endswith(".BJ"):
        return None
    return 20 if code[:3] in ("688", "689", "300", "301") else 10


def scaled_pct(pct: float, ts_code: str) -> float | None:
    """把涨跌幅折成「按 10cm 算」：20cm 的涨 10% 只算涨停的一半，和主板的 5% 一样。"""
    limit = board_limit(ts_code)
    return None if limit is None else pct * 10 / limit


def format_limits(rows: list[dict], today_pct: dict[str, float] | None = None, prev_rows: list[dict] | None = None) -> dict:
    """rows 是当天涨跌停名单；prev_rows 是前一交易日的名单，今天的涨跌幅 today_pct 用来算昨涨停今日表现。
    北交所（30cm）不算；20cm 的涨跌幅折成 10cm 再平均，免得创业板、科创板把赚钱效应拉高。"""
    rows = [row for row in rows if board_limit(row.get("ts_code")) is not None]
    counts = {"U": 0, "D": 0, "Z": 0}
    height = 0
    for row in rows:
        kind = str(row.get("limit") or "")
        if kind in counts:
            counts[kind] += 1
        times = _num(row.get("limit_times"))
        if kind == "U" and times is not None:
            height = max(height, int(times))
    if not any(counts.values()):
        return _body([], "没有取到涨跌停")
    follow = None
    follow_count = 0
    if today_pct and prev_rows:
        vals = [scaled_pct(today_pct[code], code) for code in (str(row.get("ts_code") or "") for row in prev_rows if str(row.get("limit") or "") == "U")
                if code in today_pct and board_limit(code) is not None]
        if vals:
            follow = sum(vals) / len(vals)
            follow_count = len(vals)
    tried = counts["U"] + counts["Z"]
    broken_rate = counts["Z"] / tried if tried else None
    text = f"涨停 {counts['U']}，跌停 {counts['D']}，炸板 {counts['Z']}，最高连板 {height}。不含 ST、北交所。"
    if follow is not None:
        text += f"昨天涨停的 {follow_count} 只，今天平均涨跌 {follow:+.2f}%（20cm 的折成 10cm）。"
    text += "连板天梯接口没有权限，高度来自 limit_times。"
    return _body([{"id": "mkt.limits", "text": text}], text, {
        "up": counts["U"], "down": counts["D"], "broken": counts["Z"], "height": height,
        "broken_rate": broken_rate, "follow": follow, "follow_count": follow_count,
    })


def format_index(bars: dict[str, list[dict]], basics: dict[str, dict]) -> dict:
    bits = []
    series = []
    for code, name in _INDEXES:
        rows = sorted(bars.get(code) or [], key=lambda row: _ymd(row.get("trade_date")))
        rows = [row for row in rows if _num(row.get("close")) is not None]
        if not rows:
            continue
        last = rows[-1]
        ret20 = _window_return(rows, 20)
        ret3, ret5 = _window_return(rows, 3), _window_return(rows, 5)
        pe = _num((basics.get(code) or {}).get("pe_ttm"))
        closes = [_num(row.get("close")) for row in rows]
        ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else None
        ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
        close = closes[-1]
        # 前低 = 今天之前 20 个交易日的最低收盘价；今天收在它下面就算破位
        prior = closes[-21:-1] if len(closes) >= 21 else []
        low = min(prior) if prior else None
        bits.append(f"{name} {_iso(last.get('trade_date'))} {close}，近 20 日 {_pct(ret20)}，PE(TTM) {pe}")
        series.append({
            "name": name, "code": code, "date": _iso(last.get("trade_date")),
            "close": close, "pct": _num(last.get("pct_chg")), "ret20": ret20, "ret3": ret3, "ret5": ret5, "pe": pe,
            "ma60": ma60, "above_ma60": None if ma60 is None else close >= ma60,
            "above_ma20": None if ma20 is None else close >= ma20,
            "prior_low": low, "broke_low": None if low is None else close < low,
        })
    if not bits:
        return _body([], "没有取到指数")
    text = "；".join(bits) + "。"
    return _body([{"id": "mkt.index", "text": text}], text, {"rows": series})


def format_sw(rows: list[dict]) -> dict:
    usable = []
    for row in rows:
        code = str(row.get("ts_code") or "")
        if code not in _L1:
            continue
        pct = _num(row.get("pct_change"))
        if pct is None:
            continue
        usable.append((str(row.get("name") or code), pct))
    if not usable:
        return _body([], "没有取到申万行业日线")
    usable.sort(key=lambda item: item[1], reverse=True)
    lead = "，".join(f"{name} {pct:+.2f}%" for name, pct in usable[:5])
    lag = "，".join(f"{name} {pct:+.2f}%" for name, pct in usable[-5:])
    text = f"申万一级涨幅前五：{lead}。后五：{lag}。"
    return _body([{"id": "mkt.sw", "text": text}], text, {
        "lead": [{"name": name, "pct": pct} for name, pct in usable[:5]],
        "lag": [{"name": name, "pct": pct} for name, pct in usable[-5:]],
    })


def format_macro(money: list[dict], ppi: list[dict], finance: list[dict]) -> dict:
    bits = []
    metrics = {}
    by_month: dict[str, dict] = {}
    for row in money:
        by_month.setdefault(str(row.get("month") or ""), {}).update(m1=_num(row.get("m1_yoy")), m2=_num(row.get("m2_yoy")))
    for row in ppi:
        by_month.setdefault(str(row.get("month") or ""), {}).update(ppi=_num(row.get("ppi_yoy")))
    metrics["history"] = [{"month": _month(key), **by_month[key]} for key in sorted(by_month) if key][-6:]
    if money:
        row = sorted(money, key=lambda item: str(item.get("month") or ""))[-1]
        bits.append(f"{_month(row.get('month'))} M1 同比 {_num(row.get('m1_yoy'))}%，M2 同比 {_num(row.get('m2_yoy'))}%")
        metrics.update({"month": _month(row.get("month")), "m1_yoy": _num(row.get("m1_yoy")), "m2_yoy": _num(row.get("m2_yoy"))})
    if ppi:
        row = sorted(ppi, key=lambda item: str(item.get("month") or ""))[-1]
        bits.append(f"PPI 同比 {_num(row.get('ppi_yoy'))}%")
        metrics["ppi_yoy"] = _num(row.get("ppi_yoy"))
    if finance:
        row = sorted(finance, key=lambda item: str(item.get("month") or ""))[-1]
        bits.append(f"社融当月增量 {_num(row.get('inc_month'))} 亿元")
        metrics["sf_month"] = _num(row.get("inc_month"))
    if not bits:
        return _body([], "没有取到宏观月频")
    text = "；".join(bits) + "。这是接口里最新的月份，月频，不参与短期资金判断。中国 10 年国债收益率没有权限。"
    return _body([{"id": "mkt.macro", "text": text}], text, metrics)


def format_fx(rows: list[dict]) -> dict:
    rows = sorted(rows, key=lambda row: _ymd(row.get("trade_date")))
    if not rows:
        return _body([], "没有取到汇率")
    last = rows[-1]
    close = _num(last.get("bid_close"))
    base = _num(rows[-6].get("bid_close")) if len(rows) > 5 else None
    text = f"{_iso(last.get('trade_date'))} 美元兑离岸人民币收盘 {close}。"
    return _body([{"id": "mkt.fx", "text": text}], text, {
        "date": _iso(last.get("trade_date")), "close": close, "change5": _growth(close, base),
    })


def format_us(rows: list[dict]) -> dict:
    rows = [row for row in rows if _num(row.get("y10")) is not None]
    rows.sort(key=lambda row: _ymd(row.get("date")))
    if not rows:
        return _body([], "没有取到美债收益率")
    last = rows[-1]
    y10 = _num(last.get("y10"))
    base = _num(rows[-6].get("y10")) if len(rows) > 5 else None
    text = f"{_iso(last.get('date'))} 美国 10 年国债收益率 {y10}%。"
    return _body([{"id": "mkt.us", "text": text}], text, {
        "date": _iso(last.get("date")), "y10": y10, "change5": None if base is None or y10 is None else y10 - base,
    })


def format_hk(rows: list[dict]) -> dict:
    rows = sorted(rows, key=lambda row: _ymd(row.get("trade_date")))
    if not rows:
        return _body([], "没有取到恒生科技")
    last = rows[-1]
    close = _num(last.get("close"))
    pct = _num(last.get("pct_chg"))
    base = _num(rows[-6].get("close")) if len(rows) > 5 else None
    text = f"{_iso(last.get('trade_date'))} 恒生科技 {close}，涨跌 {pct}%。代码是 HKTECH。"
    return _body([{"id": "mkt.hk", "text": text}], text, {
        "date": _iso(last.get("trade_date")), "close": close, "pct": pct, "change5": _growth(close, base),
    })


def format_ipo(rows: list[dict]) -> dict:
    if not rows:
        return _body([{"id": "mkt.ipo", "text": "这个窗口没有新股"}], "这个窗口没有新股", {"count": 0, "days": []})
    bits = []
    for row in rows[:8]:
        bits.append(f"{row.get('name') or row.get('ts_code')} {_iso(row.get('ipo_date'))} 发行价 {_num(row.get('price'))}")
    text = f"新股 {len(rows)} 只：" + "；".join(bits) + "。"
    days: dict[str, int] = {}
    for row in rows:
        day = _iso(row.get("ipo_date"))
        if day:
            days[day] = days.get(day, 0) + 1
    return _body([{"id": "mkt.ipo", "text": text}], text, {
        "count": len(rows), "days": [{"date": day, "count": days[day]} for day in sorted(days)],
    })


_US_KEYWORDS = ("利率决议", "美联储", "联邦公开市场", "非农", "CPI", "PPI", "失业率", "PMI", "GDP", "PCE")


def format_calendar(events: list[dict], opens: list[dict]) -> dict:
    picked = []
    for row in events:
        country = str(row.get("country") or "")
        if country not in ("中国", "美国"):
            continue
        event = str(row.get("event") or "")
        if not (event.startswith("中国") or event.startswith("美国")):
            continue
        if event.startswith("美国") and not any(word in event for word in _US_KEYWORDS):
            continue
        event = event.replace("(年度)", "").replace("(%)", "")
        picked.append((0 if event.startswith("中国") else 1, f"{_iso(row.get('date'))} {event}"))
    picked.sort()
    chosen = [item[1] for item in sorted(picked, key=lambda item: item[1])[:8]]
    ordered = sorted(opens, key=lambda row: _ymd(row.get("cal_date")))
    days = [f"{_iso(row.get('cal_date'))} {'开市' if str(row.get('is_open')) == '1' else '休市'}" for row in ordered[:6]]
    text = "宏观：" + ("；".join(chosen) if chosen else "没有筛到中国或美国事件") + "。交易日：" + "，".join(days) + "。"
    return _body([{"id": "mkt.calendar", "text": text}], text, {
        "events": [{"date": item.split(" ", 1)[0], "name": item.split(" ", 1)[-1]} for item in chosen],
        "days": [{"date": _iso(row.get("cal_date")), "open": str(row.get("is_open")) == "1"} for row in ordered[:6]],
    })


def format_unlock(rows: list[dict], capped: bool = False) -> dict:
    if not rows:
        return _body([{"id": "mkt.unlock", "text": "这个窗口没有解禁"}], "这个窗口没有解禁", {"capped": capped})
    total = 0.0
    ranked = []
    for row in rows:
        shares = _num(row.get("float_share"))
        if shares is None:
            continue
        total += shares
        ranked.append((shares, str(row.get("ts_code") or ""), _iso(row.get("float_date"))))
    ranked.sort(reverse=True)
    top = "，".join(f"{code} {day} {shares / 1e8:.2f} 亿股" for shares, code, day in ranked[:5])
    text = f"窗口内解禁 {len(ranked)} 条，合计 {total / 1e8:.2f} 亿股。较大的几条：{top}。"
    if capped:
        text += " 有一天达到单次 6000 条上限，这一天后面的没有算进来。"
    return _body([{"id": "mkt.unlock", "text": text}], text, {
        "count": len(ranked),
        "total": total / 1e8,
        "capped": capped,
        "top": [{"code": code, "date": day, "shares": shares / 1e8} for shares, code, day in ranked[:5]],
    })


def _pulse(today: date) -> dict:
    turn = _turnover(today)
    etf = _etf(today)
    margin = _margin(today)
    # 标签取各自 metrics 里判好的，不要从说明文字里再抓：成交额的文字里固定写着「冷清」「活跃」
    labels = {key: (doc.get("metrics") or {}).get("label") or "没有" for key, doc in (("turnover", turn), ("etf", etf), ("margin", margin))}
    lean = judge_pulse(labels["turnover"], labels["etf"], labels["margin"])
    text = f"短期判定 {lean}。成交 {labels['turnover']}，宽基 {labels['etf']}，两融 {labels['margin']}。在进要宽基净流入并且成交不冷清；成交正常且两融加速下降时仍是观望。"
    return _doc("pulse", [{"id": "mkt.pulse", "text": text}], text + " " + " ".join(doc.get("text") or "" for doc in (turn, etf, margin)))


def _turnover(today: date) -> dict:
    start = _ymd_offset(today, 400)
    end = _ymd(today)
    rows = []
    index_pct = {}
    for market in ("SH_MARKET", "SZ_MARKET"):
        # 键带 end：同一自然日里盘后补齐今日行时，旧缓存能被 _cached_through 丢掉重取
        frame = _cached_through(f"turnover:{market}:{start}:{end}", end, lambda m=market: _pro().daily_info(
            ts_code=m, start_date=start, end_date=end, fields="trade_date,amount",
        ))
        for row in frame:
            rows.append(row)
    totals: dict[str, float] = {}
    for row in rows:
        amount = _num(row.get("amount"))
        day = _ymd(row.get("trade_date"))
        if amount is None or len(day) < 8:
            continue
        totals[day] = totals.get(day, 0.0) + amount
    merged = [{"trade_date": day, "amount": amount} for day, amount in totals.items()]
    if merged:
        latest = max(merged, key=lambda row: row["trade_date"])
        cache.set(f"market_tape:turnover:total:{end}", {"latest": latest["amount"]}, _TTL)
    idx = _cached_through(f"indexpx:{start}:{end}", end, lambda: _pro().index_daily(
        ts_code="000001.SH", start_date=start, end_date=end, fields="trade_date,pct_chg,close",
    ))
    index_close = {}
    for row in idx:
        day = _ymd(row.get("trade_date"))
        if _num(row.get("pct_chg")) is not None:
            index_pct[day] = _num(row.get("pct_chg")) or 0
        if _num(row.get("close")) is not None:
            index_close[day] = _num(row.get("close"))
    body = format_turnover(merged, index_pct, index_close)
    return _from("turnover", body)


def invalidate_today_series(today: date | None = None) -> None:
    """盘后重算前丢掉滚到今天的成交/指数/份额/ETF 日线缓存（含旧版不带 end 的键）。"""
    day = today or datetime.now(_ZONE).date()
    start = _ymd_offset(day, 400)
    end = _ymd(day)
    for market in ("SH_MARKET", "SZ_MARKET"):
        cache.delete(f"market_tape:turnover:{market}:{start}:{end}")
        cache.delete(f"market_tape:turnover:{market}:{start}")
    cache.delete(f"market_tape:indexpx:{start}:{end}")
    cache.delete(f"market_tape:indexpx:{start}")
    cache.delete(f"market_tape:turnover:total:{end}")
    cache.delete(f"market_tape:turnover:total:{_ymd(day)}")
    cache.delete(f"market_tape:holdidx:{start}:{end}")
    cache.delete(f"market_tape:holdidx:{start}")
    # 国家队 / 板块共用的 ETF 份额与日线：按前缀清，避免漏票
    cache.delete_prefix("market_tape:fdshare:")
    cache.delete_prefix("market_tape:fdbar:")


def _etf(today: date) -> dict:
    codes = [code for code, _name, _group in ETF_BOOK]
    shares = dict(zip(codes, _parallel(codes, lambda code: fund_share_rows(code, today))))
    closes = dict(zip(codes, _parallel(codes, lambda code: fund_bar_rows(code, today))))
    body = format_etf(shares, closes)
    return _from("etf", body)


def _margin(today: date) -> dict:
    start = _ymd_offset(today, 400)
    rows = _cached(f"margin:{start}", lambda: _pro().margin(start_date=start, end_date=_ymd(today)))
    turn = _turnover(today)
    latest_amount = None
    # turnover text starts with a date; amount is recovered from cached totals via a second cheap read
    totals = _cached_value(f"turnover:total:{_ymd(today)}")
    if isinstance(totals, dict):
        latest_amount = totals.get("latest")
    body = format_margin(rows, latest_amount)
    _ = turn
    return _from("margin", body)


def _margin_industry(today: date) -> dict:
    members = _member_map()
    rows: list[dict] = []
    used = _ymd(today)
    for trade_day in reversed(_open_dates(today, 5) or [used]):
        rows = _cached(f"margin_detail:{trade_day}", lambda d=trade_day: _pro().margin_detail(
            trade_date=d, fields="ts_code,rzye,rzmre,rzche",
        ))
        if rows:
            used = trade_day
            break
    body = format_margin_industry(members, rows)
    text = body["text"] if not rows else f"{_iso(used)} " + body["text"]
    facts = [{"id": "mkt.margin_ind", "text": text}] if rows else body["facts"]
    return _doc("margin_industry", facts, text, body.get("metrics"))


def _member_map() -> dict[str, str]:
    cached = cache.get("market_tape:l1members")
    if isinstance(cached, dict):
        return cached
    mapping = {}
    loaded = _parallel(list(_L1), lambda code: _cached(f"l1members:{code}", lambda c=code: _pro().index_member_all(l1_code=c, is_new="Y")))
    for code, rows in zip(_L1, loaded):
        name = ""
        for row in rows:
            name = str(row.get("l1_name") or name)
            stock = str(row.get("ts_code") or "")
            if stock:
                mapping[stock] = name or code
    if mapping:
        cache.set("market_tape:l1members", mapping, 24 * 3600)
    return mapping


def _south(today: date) -> dict:
    start = _ymd_offset(today, 400)
    hsgt = _cached(f"hsgt:{start}", lambda: _pro().moneyflow_hsgt(start_date=start, end_date=_ymd(today)))
    ggt = _cached(f"ggt:{start}", lambda: _pro().ggt_daily(start_date=start, end_date=_ymd(today)))
    body = format_south(hsgt, ggt)
    return _from("south", body)


def _shibor(today: date) -> dict:
    start = _ymd_offset(today, 120)
    rows = _cached(f"shibor:{start}", lambda: _pro().shibor(start_date=start, end_date=_ymd(today)))
    body = format_shibor(rows)
    return _from("shibor", body)


def _supply(today: date) -> dict:
    start = _ymd_offset(today, 28)
    wide = _ymd_offset(today, 84)
    trades = _cached(f"holder:{start}", lambda: _pro().stk_holdertrade(start_date=start, end_date=_ymd(today)))
    repos = _cached(f"repo:{start}", lambda: _pro().repurchase(start_date=start, end_date=_ymd(today)))
    body = format_supply(trades, repos)
    wide_trades = _cached(f"holder:{wide}", lambda: _pro().stk_holdertrade(start_date=wide, end_date=_ymd(today)))
    body["metrics"]["weeks"] = weekly_supply(wide_trades, today)
    return _from("supply", body)


def weekly_supply(trades: list[dict], today: date) -> list[dict]:
    """近 12 周每周增持、减持金额（亿元）。回购是累计口径，不能拆到周，这里不放。"""
    weeks = []
    for back in range(11, -1, -1):
        end = today - timedelta(days=back * 7)
        begin = end - timedelta(days=6)
        weeks.append([_ymd(begin), _ymd(end), 0.0, 0.0])
    for row in trades:
        day = _ymd(row.get("ann_date"))
        vol = _num(row.get("change_vol"))
        price = _num(row.get("avg_price"))
        if len(day) < 8 or vol is None or price is None:
            continue
        side = str(row.get("in_de") or "").upper()
        if side not in ("IN", "DE"):
            continue
        for week in weeks:
            if week[0] <= day <= week[1]:
                week[2 if side == "IN" else 3] += vol * price / 1e8
                break
    return [
        {"label": f"W{index + 1}", "start": _iso(week[0]), "end": _iso(week[1]), "buy": week[2], "sell": week[3]}
        for index, week in enumerate(weeks)
    ]


def _funds(today: date) -> dict:
    rows = _cached(f"fund_basic:{today.year}{today.month:02d}", lambda: _pro().fund_basic(
        market="O", fields="ts_code,name,fund_type,issue_date,found_date,issue_amount",
    ))
    body = format_funds(rows, today)
    return _from("funds", body)


def _data_days(today: date, count: int = 4) -> list[str]:
    """最近几个交易日，新的在前。当天数据没发布时，调用方往前找第一个有数据的日子。"""
    return list(reversed(_open_dates(today, count) or [_ymd(today)]))


def _daily_rows(day: str) -> list[dict]:
    return _cached(f"daily:{day}", lambda d=day: _pro().daily(trade_date=d, fields="ts_code,close,pct_chg"))


def advance_decline_series(today: date, n: int = 30) -> list[dict]:
    """近 n 个交易日每天上涨 / 下跌家数（全 A 日线 pct_chg）。"""
    days = _open_dates(today, n)
    loaded = _parallel(days, _daily_rows)
    out = []
    for day, rows in zip(days, loaded):
        up = down = 0
        for row in rows or []:
            pct = _num(row.get("pct_chg"))
            if pct is None:
                continue
            if pct > 0:
                up += 1
            elif pct < 0:
                down += 1
        if up or down:
            out.append({"date": _iso(day), "up": up, "down": down})
    return out


def _breadth(today: date) -> dict:
    days = _data_days(today)
    day = days[0]
    today_rows: list[dict] = []
    for day in days:
        today_rows = _daily_rows(day)
        if today_rows:
            break
    history: dict[str, list[float]] = {}
    dates = [item for item in _open_dates(today, 26) if item < day][-24:]
    for rows in _parallel(dates, _daily_rows):
        for row in rows:
            close = _num(row.get("close"))
            code = str(row.get("ts_code") or "")
            if close is None or not code:
                continue
            history.setdefault(code, []).append(close)
    body = format_breadth(today_rows, history)
    body["metrics"]["date"] = _iso(day)
    body["metrics"]["series"] = advance_decline_series(today, 30)
    return _from("breadth", body)


def _limits(today: date) -> dict:
    days = _data_days(today)
    for index, day in enumerate(days):
        rows = _cached(f"limit:{day}", lambda d=day: _pro().limit_list_d(trade_date=d))
        if not rows:
            continue
        prev_rows: list[dict] = []
        today_pct: dict[str, float] = {}
        if index + 1 < len(days):
            prev = days[index + 1]
            prev_rows = _cached(f"limit:{prev}", lambda d=prev: _pro().limit_list_d(trade_date=d))
            daily = _cached(f"daily:{day}", lambda d=day: _pro().daily(trade_date=d, fields="ts_code,close,pct_chg"))
            today_pct = {str(row.get("ts_code") or ""): _num(row.get("pct_chg")) for row in daily if _num(row.get("pct_chg")) is not None}
        body = format_limits(rows, today_pct, prev_rows)
        body["metrics"]["date"] = _iso(day)
        return _from("limits", body)
    return _from("limits", format_limits([]))


def _index(today: date) -> dict:
    start = _ymd_offset(today, 200)
    bars = {}
    basics = {}
    for code, _name in _INDEXES:
        bars[code] = _cached(f"idx:{code}:{start}", lambda c=code: _pro().index_daily(
            ts_code=c, start_date=start, end_date=_ymd(today), fields="trade_date,close,pct_chg",
        ))
        basic_rows = _cached(f"idxb:{code}:{start}", lambda c=code: _pro().index_dailybasic(
            ts_code=c, start_date=start, end_date=_ymd(today), fields="trade_date,pe_ttm,pb",
        ))
        if basic_rows:
            basics[code] = sorted(basic_rows, key=lambda row: _ymd(row.get("trade_date")))[-1]
    body = format_index(bars, basics)
    return _from("index", body)


def _sw(today: date) -> dict:
    for day in _data_days(today):
        rows = _cached(f"sw:{day}", lambda d=day: _pro().sw_daily(trade_date=d))
        if rows:
            body = format_sw(rows)
            body["metrics"]["date"] = _iso(day)
            return _from("sw", body)
    return _from("sw", format_sw([]))


def _macro(today: date) -> dict:
    month = today.month - 8
    year = today.year
    while month <= 0:
        month += 12
        year -= 1
    start = f"{year}{month:02d}"
    end = f"{today.year}{today.month:02d}"
    money = _cached(f"cnm:{start}", lambda: _pro().cn_m(start_m=start, end_m=end))
    ppi = _cached(f"ppi:{start}", lambda: _pro().cn_ppi(start_m=start, end_m=end))
    finance = _cached(f"sf:{start}", lambda: _pro().sf_month(start_m=start, end_m=end))
    body = format_macro(money, ppi, finance)
    return _from("macro", body)


def _fx(today: date) -> dict:
    start = _ymd_offset(today, 15)
    rows = _cached(f"fx:{start}", lambda: _pro().fx_daily(ts_code="USDCNH.FXCM", start_date=start, end_date=_ymd(today)))
    body = format_fx(rows)
    return _from("fx", body)


def _us(today: date) -> dict:
    start = _ymd_offset(today, 15)
    rows = _cached(f"ust:{start}", lambda: _pro().us_tycr(start_date=start, end_date=_ymd(today)))
    body = format_us(rows)
    return _from("us", body)


def _hk(today: date) -> dict:
    start = _ymd_offset(today, 15)
    rows = _cached(f"hktech:{start}", lambda: _pro().index_global(ts_code="HKTECH", start_date=start, end_date=_ymd(today)))
    body = format_hk(rows)
    return _from("hk", body)


def _ipo(today: date) -> dict:
    end = today + timedelta(days=14)
    rows = _cached(f"ipo:{_ymd(today)}", lambda: _pro().new_share(start_date=_ymd(today), end_date=_ymd(end)))
    body = format_ipo(rows)
    return _doc("ipo", body["facts"], body["text"], body.get("metrics"))


def _calendar(today: date) -> dict:
    end = today + timedelta(days=14)
    events = _cached(f"eco:{_ymd(today)}", lambda: _pro().eco_cal(start_date=_ymd(today), end_date=_ymd(end)))
    opens = _cached(f"cal:{_ymd(today)}", lambda: _pro().trade_cal(exchange="SSE", start_date=_ymd(today), end_date=_ymd(end)))
    body = format_calendar(events, opens)
    return _from("calendar", body)


def format_valuation(rows: list[dict]) -> dict:
    """沪深 300 盈利收益率（1/PE_TTM）在近 3 年里的分位。没有国债收益率权限，所以这不是 ERP。"""
    rows = sorted([row for row in rows if _num(row.get("pe_ttm"))], key=lambda row: _ymd(row.get("trade_date")))
    if len(rows) < 60:
        return _body([], "没有取到足够的沪深 300 估值")
    yields = [1 / _num(row.get("pe_ttm")) for row in rows]
    now = yields[-1]
    pctile = _pctile(yields, now)
    text = (
        f"{_iso(rows[-1].get('trade_date'))} 沪深 300 PE(TTM) {_num(rows[-1].get('pe_ttm')):.2f}，"
        f"盈利收益率 {now * 100:.2f}%，处在近 {len(rows)} 个交易日的 {round(pctile * 100)}% 分位。"
        "中国 10 年国债收益率没有权限，所以这里不是 ERP，只是盈利收益率自身的分位。"
    )
    return _body([{"id": "mkt.valuation", "text": text}], text, {
        "date": _iso(rows[-1].get("trade_date")), "pe": _num(rows[-1].get("pe_ttm")),
        "earn_yield": now, "pctile": pctile, "days": len(rows),
    })


def _valuation(today: date) -> dict:
    start = _ymd_offset(today, 1100)
    rows = _cached(f"idxb300:{start}", lambda: _pro().index_dailybasic(
        ts_code="000300.SH", start_date=start, end_date=_ymd(today), fields="trade_date,pe_ttm",
    ))
    return _from("valuation", format_valuation(rows))


def unlock_rows(today: date) -> list[dict]:
    rows, _capped = _unlock_fetch(today)
    return rows


def _unlock_fetch(today: date) -> tuple[list[dict], bool]:
    """按天取。一次拉 10 天会顶到 6000 条上限，后面的解禁就丢了。"""
    rows = []
    seen = set()
    capped = False
    days = [_ymd(today + timedelta(days=offset)) for offset in range(11)]
    loaded = _parallel(days, lambda day: _cached(f"unlockday:{day}", lambda d=day: _pro().share_float(start_date=d, end_date=d)))
    for part in loaded:
        if len(part) >= 6000:
            capped = True
        for row in part:
            key = (row.get("ts_code"), _ymd(row.get("float_date")), row.get("float_share"), row.get("holder_name"))
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    return rows, capped


def _unlock(today: date) -> dict:
    rows, capped = _unlock_fetch(today)
    return _from("unlock", format_unlock(rows, capped))


def _open_dates(today: date, count: int) -> list[str]:
    start = _ymd_offset(today, count * 2 + 10)
    rows = _cached(f"calopen:{start}", lambda: _pro().trade_cal(exchange="SSE", start_date=start, end_date=_ymd(today), is_open="1"))
    days = sorted(_ymd(row.get("cal_date")) for row in rows if len(_ymd(row.get("cal_date"))) >= 8)
    return days[-count:]


_DAY_KEYS = {"daily", "limit", "margin_detail", "sw", "indflow"}
_FLIGHT_LOCK = threading.Lock()
_FLIGHT: dict[str, threading.Event] = {}


def _keep_on_disk(name: str) -> bool:
    """已经收盘的某一天，以及申万成分，落盘后不再打接口。含今天的滚动窗口不落盘。"""
    if name.startswith("l1members:") or name.startswith("discl:"):
        return True
    head, _, tail = name.rpartition(":")
    if len(tail) != 8 or not tail.isdigit():
        return False
    if head == "unlockday":
        return True
    return head in _DAY_KEYS and tail < datetime.now(_ZONE).strftime("%Y%m%d")


def _memory_ttl(name: str) -> float:
    head, _, tail = name.rpartition(":")
    today = datetime.now(_ZONE).strftime("%Y%m%d")
    if head == "unlockday" and tail >= today:
        return _TTL
    if name.startswith("l1members:"):
        return 24 * 3600
    if name.startswith("discl:"):
        return _TTL
    # 滚到「今天」的序列盘后还会补行，短一点，避免上午的昨日截止缓存挂到晚上
    rolling = ("turnover:", "indexpx:", "fdshare:", "fdbar:", "holdidx:")
    if tail == today and name.startswith(rolling):
        return 10 * 60
    return 7 * 24 * 3600


def _latest_trade_ymd(rows: list[dict]) -> str:
    days = [_ymd(row.get("trade_date")) for row in rows or []]
    days = [day for day in days if len(day) >= 8]
    return max(days) if days else ""


def _need_today_bar(end: str) -> bool:
    """收盘后才强求序列里有 end 这一天；盘中用到昨日收盘是正常的。"""
    now = datetime.now(_ZONE)
    today = now.strftime("%Y%m%d")
    if end < today:
        return True
    if end > today:
        return False
    if now.weekday() >= 5:
        return False
    return now.hour > 15 or (now.hour == 15 and now.minute >= 5)


def _cached_through(name: str, end: str, fetch) -> list[dict]:
    """日频序列：收盘后若最新日落后于 end，丢掉缓存再取一次。"""
    rows = _cached(name, fetch)
    if not _need_today_bar(end):
        return rows
    if _latest_trade_ymd(rows) >= end:
        return rows
    cache.delete(f"market_tape:{name}")
    return _cached(name, fetch)


def _memory_or_disk(name: str, key: str):
    from app.services import market_disk

    cached = cache.get(key)
    if isinstance(cached, list):
        return cached
    if not _keep_on_disk(name):
        return None
    stored = market_disk.load(key)
    if isinstance(stored, list) and (stored or name.startswith("discl:")):
        cache.set(key, stored, _memory_ttl(name))
        return stored
    return None


def _cached(name: str, fetch) -> list[dict]:
    from app.services import market_disk

    key = f"market_tape:{name}"
    hit = _memory_or_disk(name, key)
    if hit is not None:
        return hit
    with _FLIGHT_LOCK:
        hit = _memory_or_disk(name, key)
        if hit is not None:
            return hit
        event = _FLIGHT.get(key)
        if event is None:
            event = threading.Event()
            _FLIGHT[key] = event
            owner = True
        else:
            owner = False
    if not owner:
        event.wait(180)
        return _memory_or_disk(name, key) or []
    try:
        try:
            rows = _records(fetch)
        except Exception:
            rows = []
        if rows or name.startswith("discl:"):
            cache.set(key, rows, _memory_ttl(name))
            if _keep_on_disk(name):
                market_disk.save(key, rows)
        return rows
    finally:
        event.set()
        with _FLIGHT_LOCK:
            _FLIGHT.pop(key, None)


def _parallel(items, fetch):
    """同一类请求一起打。缓存里已有的不会再出去。"""
    if not items:
        return []
    from concurrent.futures import ThreadPoolExecutor

    if len(items) == 1:
        return [fetch(items[0])]
    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(fetch, items))


def _share_expect_ymd(today: date) -> str:
    """fund_share 正常 T+1：收盘后只强求追到上一交易日，不要求当天份额。"""
    opens = _open_dates(today, 6)
    if not opens:
        return _ymd(today)
    today_s = _ymd(today)
    if today_s in opens:
        idx = opens.index(today_s)
        return opens[idx - 1] if idx > 0 else opens[0]
    return opens[-1]


def fund_share_rows(code: str, today: date, days: int = 400) -> list[dict]:
    """宽基、国家队、板块共用同一段份额，避免同一只 ETF 按不同窗口各取一次。"""
    start = _ymd_offset(today, days)
    end = _ymd(today)
    expect = _share_expect_ymd(today)
    return _cached_through(
        f"fdshare:{code}:{start}:{end}",
        expect,
        lambda c=code, s=start, e=end: _pro().fund_share(ts_code=c, start_date=s, end_date=e),
    )


def fund_bar_rows(code: str, today: date, days: int = 400) -> list[dict]:
    start = _ymd_offset(today, days)
    end = _ymd(today)
    return _cached_through(
        f"fdbar:{code}:{start}:{end}",
        end,
        lambda c=code, s=start, e=end: _pro().fund_daily(
            ts_code=c, start_date=s, end_date=e, fields="ts_code,trade_date,close,pct_chg,vol,amount",
        ),
    )


def _cached_value(name: str):
    return cache.get(f"market_tape:{name}")


def _records(fetch) -> list[dict]:
    from app.services.sector_detail.tsutil import call, records

    return records(call(fetch))


def _pro():
    from app.tools.tushare_client import get_pro

    return get_pro()


def _close_on(rows: list[dict], day: str):
    for row in rows:
        if _ymd(row.get("trade_date")) == day:
            return _num(row.get("close"))
    return None


def _window_return(rows: list[dict], n: int):
    if len(rows) <= n:
        return None
    last = _num(rows[-1].get("close"))
    base = _num(rows[-1 - n].get("close"))
    return _growth(last, base)


def _doc(part: str, facts: list[dict], text: str, metrics: dict | None = None) -> dict:
    return {
        "part": part,
        "facts": facts,
        "text": text,
        "metrics": metrics or {},
        "unavailable": [] if facts else ["没有取到"],
    }


def _from(part: str, body: dict) -> dict:
    return _doc(part, body["facts"], body["text"], body.get("metrics"))


def _body(facts: list[dict], text: str, metrics: dict | None = None) -> dict:
    return {"facts": facts, "text": text, "metrics": metrics or {}}


def _mean(rows: list[dict], key: str):
    values = [_num(row.get(key)) for row in rows]
    values = [item for item in values if item is not None]
    if not values:
        return None
    return sum(values) / len(values)


def _growth(now, base):
    if now is None or not base:
        return None
    return now / base - 1


def _pctile(values: list[float], point) -> float | None:
    if point is None or not values:
        return None
    below = sum(1 for item in values if item <= point)
    return below / len(values)


def _round(value) -> str:
    if value is None:
        return "没有"
    return f"{value:.2f}"


def _pct(value) -> str:
    if value is None:
        return "没有"
    return f"{value * 100:+.2f}%"


def _yi_plain(value) -> str:
    if value is None:
        return "没有"
    if value >= 10000:
        return f"{value / 10000:.2f} 万亿元"
    return f"{value:.0f} 亿元"


def _yuan_yi(value) -> str:
    if value is None:
        return "没有"
    return f"{value / 1e8:+.2f} 亿元"


def _num(value):
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _ymd(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:8]


def _month(value) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(text) < 6:
        return str(value or "")
    return f"{text[:4]}年{int(text[4:6])}月"


def _iso(value) -> str:
    text = _ymd(value)
    if len(text) < 8:
        return ""
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


def _day_gap(earlier: str, later: str) -> int:
    return (datetime.strptime(later, "%Y%m%d") - datetime.strptime(earlier, "%Y%m%d")).days


def _ymd_offset(day: date, days: int) -> str:
    return (day - timedelta(days=days)).strftime("%Y%m%d")


# 其他市场模块复用的取数小工具
cached = _cached
pro = _pro
open_dates = _open_dates
num = _num
ymd = _ymd
iso = _iso
growth = _growth
pctile = _pctile
