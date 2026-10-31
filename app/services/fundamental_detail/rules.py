"""基本面三格和方向。阈值写在这里，改口径时升 rule_version。"""

from __future__ import annotations

from datetime import date, timedelta
from statistics import median

RULE_VERSION = "fundamental-3"
DISCLAIMER_TEXT = "只描述在历史和同行中的位置，不给目标价。研究分析，不构成投资建议。"

METRIC_TEXT = {
    "pe_ttm_deducted": "市盈率（TTM，扣非）",
    "pb": "市净率",
    "ps_ttm": "市销率",
    "ev_ebitda": "EV/EBITDA",
}
QUALITY_KEYS = ("profitability", "growth", "solvency", "cash", "payout")
QUALITY_TEXT = {
    "profitability": "ROE",
    "growth": "3 年",
    "solvency": "负债率",
    "cash": "现金比（年报）",
    "payout": "分红率",
}
_HIGHER_BETTER = {
    "profitability": True,
    "growth": True,
    "solvency": False,
    "cash": True,
    "payout": True,
}
_VAL_LEVEL = {"cheap": "偏便宜", "fair": "合理", "expensive": "偏贵"}
_QUAL_LEVEL = {"strong": "较好", "medium": "中等", "weak": "偏弱"}
_HOLD_LEVEL = {"positive": "偏多", "offset": "相抵", "negative": "偏空", "quiet": "没有动作"}
_QUALITY_NOTE = {
    "profitability": "盈利能力偏弱",
    "growth": "成长性偏弱",
    "solvency": "负债水平偏高",
    "cash": "现金创造偏弱",
    "payout": "分红偏弱",
}
_WEAK_TEXT = {
    "profitability": "盈利能力处于行业后 {n}%，回报偏弱",
    "growth": "成长性处于行业后 {n}%",
    "solvency": "负债水平处于行业后 {n}%",
    "cash": "现金创造处于行业后 {n}%，利润没有充分变成现金",
    "payout": "分红处于行业后 {n}%",
}
_HOLDER_PHRASE = {
    "buyback": "回购在做",
    "holder_increase": "增持在做",
    "incentive": "激励在做",
    "holder_reduce": "减持将至",
    "unlock": "解禁将至",
    "pledge": "质押偏高",
}
_PHRASE_ORDER = ("buyback", "holder_increase", "incentive", "holder_reduce", "unlock", "pledge")
_PERIOD_SLOT = {"0331": 0, "0630": 1, "0930": 2, "1231": 3}
_OUTLIER_CAP = 200
_PB_WORDS = ("银行", "保险", "证券", "房地产", "多元金融")
_PS_WORDS = ("软件", "互联网", "游戏", "广告")
_SUPPORT = {
    "profitability": "盈利也偏弱",
    "growth": "成长也偏弱",
    "solvency": "负债也偏高",
    "cash": "现金也偏弱",
    "payout": "分红也偏弱",
}
_WEAK_PRIORITY = {"growth": 0, "profitability": 1, "cash": 2, "solvency": 3, "payout": 4}
_CLOSE_PCTILE = 0.05
_DISTORTED_REASON = "利润处在低位，市盈率失真"


def parse_ymd(value) -> date | None:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) < 8:
        return None
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None


def iso_date(value) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value.isoformat()
    parsed = parse_ymd(value)
    if parsed is None:
        text = str(value)
        return text if len(text) >= 10 and text[4] == "-" else None
    return parsed.isoformat()


def period_code(end) -> str | None:
    parsed = parse_ymd(end)
    if parsed is None:
        return None
    md = f"{parsed.month:02d}{parsed.day:02d}"
    label = {"0331": "Q1", "0630": "H1", "0930": "Q3", "1231": "FY"}.get(md)
    if label is None:
        return None
    return f"{parsed.year}{label}"


def period_cn(end) -> str | None:
    parsed = parse_ymd(end)
    if parsed is None:
        return None
    md = f"{parsed.month:02d}{parsed.day:02d}"
    label = {"0331": "一季报", "0630": "中报", "0930": "三季报", "1231": "年报"}.get(md)
    if label is None:
        return None
    return f"{parsed.year} {label}"


def status_line(price_day: date | None, report_end, reason: str = "") -> str:
    if price_day is None:
        return reason or ""
    head = f"{price_day.month:02d}-{price_day.day:02d} 收盘"
    if reason:
        return f"{head} · {reason}"
    report = period_cn(report_end)
    if not report:
        return f"{head} · 财务数据缺失"
    return f"{head} · 财务至 {report}"


def _period_index(end: str) -> int | None:
    parsed = parse_ymd(end)
    if parsed is None:
        return None
    slot = _PERIOD_SLOT.get(f"{parsed.month:02d}{parsed.day:02d}")
    if slot is None:
        return None
    return parsed.year * 4 + slot


def expected_report_end(price_day: date) -> str:
    """披露截止日已过的最新报告期。"""
    year = price_day.year
    candidates = [f"{year - 1}0930"]
    if price_day >= date(year, 4, 30):
        candidates.append(f"{year - 1}1231")
        candidates.append(f"{year}0331")
    if price_day >= date(year, 8, 31):
        candidates.append(f"{year}0630")
    if price_day >= date(year, 10, 31):
        candidates.append(f"{year}0930")
    return max(candidates, key=lambda item: _period_index(item) or -1)


def periods_behind(report_end, price_day: date) -> int:
    expected = _period_index(expected_report_end(price_day))
    actual = _period_index(report_end) if report_end else None
    if expected is None or actual is None:
        return 99
    return max(0, expected - actual)


def listed_under_one_year(list_date, price_day: date | None) -> bool:
    listed = parse_ymd(list_date)
    if listed is None or price_day is None:
        return False
    return (price_day - listed).days < 365


def history_label(dates: list) -> str:
    parsed = [item for item in (parse_ymd(value) for value in dates) if item is not None]
    if len(parsed) < 2:
        return "这段历史"
    span = (max(parsed) - min(parsed)).days
    if span >= 1400:
        return "近 5 年"
    if span >= 700:
        return "近 2 年"
    return "这段历史"


def percentile(value, series) -> float | None:
    nums = [float(item) for item in series if _positive(item)]
    if value is None or not nums:
        return None
    current = float(value)
    hit = sum(1 for item in nums if item <= current)
    return hit / len(nums)


def peer_pctile(value, peers, higher_better: bool) -> float | None:
    nums = [float(item) for item in peers if item is not None]
    if value is None or not nums:
        return None
    current = float(value)
    sample = nums + [current]
    if higher_better:
        hit = sum(1 for item in sample if item <= current)
    else:
        hit = sum(1 for item in sample if item >= current)
    return hit / len(sample)


def quantiles(values) -> dict:
    nums = sorted(float(item) for item in values if item is not None)
    if not nums:
        return {"p20": None, "p50": None, "p80": None}
    return {"p20": _quantile(nums, 0.2), "p50": _quantile(nums, 0.5), "p80": _quantile(nums, 0.8)}


def _quantile(nums: list[float], q: float) -> float:
    if len(nums) == 1:
        return round(nums[0], 2)
    pos = (len(nums) - 1) * q
    low = int(pos)
    high = min(low + 1, len(nums) - 1)
    weight = pos - low
    return round(nums[low] * (1 - weight) + nums[high] * weight, 2)


def meaningful(metric: str | None, value) -> bool:
    if metric not in METRIC_TEXT or not _positive(value):
        return False
    return float(value) <= _OUTLIER_CAP


def clean_peers(peers: list[dict], stock_code: str, value, self_name: str | None = None) -> list[dict]:
    rows = []
    seen = set()
    for item in peers or []:
        code = str(item.get("ts_code") or "")
        number = item.get("value")
        if not code or code in seen or not _positive(number) or float(number) > _OUTLIER_CAP:
            continue
        seen.add(code)
        rows.append({
            "ts_code": code,
            "name": _peer_name(item.get("name"), code),
            "value": float(number),
            "is_self": bool(item.get("is_self")) or code == stock_code,
        })
    if stock_code and stock_code not in seen and _positive(value) and float(value) <= _OUTLIER_CAP:
        rows.append({
            "ts_code": stock_code,
            "name": self_name or stock_code,
            "value": float(value),
            "is_self": True,
        })
    rows.sort(key=lambda item: (item["value"], item["ts_code"]))
    return rows[:120]


def peer_median(rows: list[dict]):
    """同行中位数不含本公司。只有自己一条时才退回这条。"""
    nums = [item["value"] for item in rows if item.get("value") is not None and not item.get("is_self")]
    if not nums:
        nums = [item["value"] for item in rows if item.get("value") is not None]
    if not nums:
        return None
    return float(median(nums))


def valuation_level(pctile, value, peer_mid) -> str:
    if pctile is None:
        return "fair"
    if pctile < 0.30 and peer_mid is not None and value is not None and float(value) <= float(peer_mid):
        return "cheap"
    if (
        pctile > 0.70
        and peer_mid is not None
        and float(peer_mid) > 0
        and value is not None
        and float(value) > float(peer_mid) * 1.2
    ):
        return "expensive"
    return "fair"


def quality_rows(snapshot: dict) -> list[dict]:
    quality = snapshot.get("quality") or {}
    peers = snapshot.get("peer_quality") or {}
    notes = snapshot.get("quality_notes") or {}
    rows = []
    for key in QUALITY_KEYS:
        value = _num(quality.get(key))
        samples = peers.get(key) or []
        pct = peer_pctile(value, samples, _HIGHER_BETTER[key])
        note = notes.get(key) or {}
        if note.get("as_percent") and value is not None:
            shown = f"{value * 100:.1f}%"
        else:
            shown = _quality_text(key, value)
        reference = "仅供参考" in str(note.get("note_text") or "")
        rows.append({
            "key": key,
            "metric_text": note.get("metric_text") or QUALITY_TEXT[key],
            "value": None if value is None else round(value, 4),
            "value_text": shown,
            "peer_pctile": None if pct is None else round(pct, 2),
            "note_text": note.get("note_text"),
            "counts": False if reference else bool(note.get("counts", True)),
        })
    return rows


def weakest_item(rows: list[dict]) -> dict | None:
    """分位不超过 0.20 才算弱项。和最低分相差不超过 5 个点时，优先成长、盈利、现金、负债，最后才是分红。"""
    pool = [item for item in rows if _scored(item) and item["peer_pctile"] <= 0.20]
    if not pool:
        return None
    floor = min(item["peer_pctile"] for item in pool)
    near = [item for item in rows if _scored(item) and item["peer_pctile"] <= floor + _CLOSE_PCTILE]
    near.sort(key=lambda item: (_WEAK_PRIORITY.get(item["key"], 9), item["peer_pctile"]))
    picked = near[0]
    n = max(1, int(round(picked["peer_pctile"] * 100)))
    return {"key": picked["key"], "text": _WEAK_TEXT[picked["key"]].format(n=n)}


def quality_level(rows: list[dict], ocf_negative_years: int) -> str:
    if ocf_negative_years >= 3:
        return "weak"
    scores = [item["peer_pctile"] for item in rows if _scored(item)]
    if not scores:
        return "medium"
    average = sum(scores) / len(scores)
    if average >= 0.6:
        return "strong"
    if average <= 0.4:
        return "weak"
    return "medium"


def quality_note(level_key: str | None, red_line: bool) -> str:
    if red_line:
        return "经营现金流连续为负"
    if level_key:
        return _QUALITY_NOTE[level_key]
    return "五项居中"


def event_counts(event: dict, price_day: date | None, trade_dates: list[str] | None) -> bool:
    if event.get("impact") not in ("positive", "negative"):
        return False
    if "counts" in event:
        return bool(event.get("counts"))
    if event.get("ongoing"):
        return True
    if price_day is None:
        return False
    due = parse_ymd(event.get("date"))
    if due is None or due < price_day:
        return False
    horizon = _horizon(price_day, trade_dates or [])
    return due <= horizon


def holders_level(events: list[dict]) -> str:
    positive = 0.0
    negative = 0.0
    for event in events:
        if not event.get("_counts"):
            continue
        weight = _weight(event)
        if event.get("impact") == "positive":
            positive += weight
        elif event.get("impact") == "negative":
            negative += weight
    if positive == 0 and negative == 0:
        return "quiet"
    if positive > 0 and negative == 0:
        return "positive"
    if negative > 0 and positive == 0:
        return "negative"
    small, large = sorted((positive, negative))
    if small * 2 >= large:
        return "offset"
    return "positive" if positive > negative else "negative"


def holder_note(level: str, events: list[dict]) -> str:
    kinds = []
    for kind in _PHRASE_ORDER:
        if any(event.get("_counts") and event.get("type") == kind for event in events):
            kinds.append(_HOLDER_PHRASE[kind])
    if kinds:
        return "，".join(kinds[:2])
    if level == "quiet":
        return "近期没有股东动作"
    if level == "offset":
        return "多空规模接近"
    if level == "positive":
        return "股东行为偏多"
    return "股东行为偏空"


def judge(valuation: str, quality: str, holders: str) -> tuple[str, int]:
    if holders == "positive" and valuation != "expensive":
        lean = "bullish"
    elif holders == "negative" and valuation != "cheap":
        lean = "bearish"
    elif valuation == "cheap" and quality != "weak":
        lean = "bullish"
    elif valuation == "expensive" and quality != "strong":
        lean = "bearish"
    else:
        lean = "neutral"
    if lean == "neutral":
        return "neutral", 0
    target = 1 if lean == "bullish" else -1
    signs = (_sign_valuation(valuation), _sign_quality(quality), _sign_holders(holders))
    if all(item == target for item in signs):
        return lean, 2
    return lean, 1


def certainty(peer_count, report_end, price_day: date | None) -> tuple[str, list[str]]:
    reasons = []
    count = _num(peer_count)
    if count is not None and count < 5:
        reasons.append("同行少于5家")
    if price_day is not None and periods_behind(report_end, price_day) > 1:
        reasons.append("财务数据超过一个报告期未更新")
    if reasons:
        return "low", reasons
    return "medium", []


def valuation_level_text(level: str) -> str:
    return _VAL_LEVEL[level]


def quality_level_text(level: str) -> str:
    return _QUAL_LEVEL[level]


def holders_level_text(level: str) -> str:
    return _HOLD_LEVEL[level]


def _horizon(price_day: date, trade_dates: list[str]) -> date:
    future = []
    for value in trade_dates:
        parsed = parse_ymd(value)
        if parsed is not None and parsed > price_day:
            future.append(parsed)
    future.sort()
    if len(future) >= 20:
        return future[19]
    if future:
        return future[-1]
    return price_day + timedelta(days=28)


def _weight(event: dict) -> float:
    if event.get("weight") is not None and _num(event.get("weight")) is not None:
        return abs(float(event["weight"])) or 0.01
    ratio = _num(event.get("float_ratio"))
    if ratio is None:
        return 0.01
    return abs(ratio) or 0.01


def _sign_valuation(level: str) -> int:
    return {"cheap": 1, "fair": 0, "expensive": -1}[level]


def _sign_quality(level: str) -> int:
    return {"strong": 1, "medium": 0, "weak": -1}[level]


def _sign_holders(level: str) -> int:
    return {"positive": 1, "offset": 0, "quiet": 0, "negative": -1}[level]


def _quality_text(key: str, value) -> str | None:
    if value is None:
        return None
    if key == "profitability":
        return f"{value * 100:.1f}%"
    if key == "growth":
        return f"{round(value * 100):+d}%/年"
    if key == "solvency":
        return f"{value * 100:.0f}%"
    if key == "cash":
        return f"{value:.2f}"
    return f"{value * 100:.0f}%"


def five_year_start(price_day: date) -> date:
    try:
        return price_day.replace(year=price_day.year - 5)
    except ValueError:
        return price_day.replace(year=price_day.year - 5, day=28)


def weekly_points(samples: list[tuple[date, float | None]], start: date, end: date) -> list[dict]:
    """每个有交易的周留最后一根正数，含超过 200 倍的市盈率。
    这一周没有正数时 value 为 null。更长的缺口由 break_gaps 补 null，图上断开。
    """
    buckets: dict[tuple[int, int], dict] = {}
    for day, value in samples:
        if day < start or day > end:
            continue
        number = float(value) if _positive(value) else None
        key = day.isocalendar()[:2]
        slot = buckets.get(key)
        if slot is None:
            buckets[key] = {"day": day, "value": number}
            continue
        if number is not None and (slot["value"] is None or day >= slot["day"]):
            slot["day"] = day
            slot["value"] = number
        elif number is None and slot["value"] is None and day >= slot["day"]:
            slot["day"] = day
    return [
        {"date": buckets[key]["day"].isoformat(), "value": buckets[key]["value"]}
        for key in sorted(buckets)
    ]


def break_gaps(points: list[dict], limit: int = 21) -> list[dict]:
    """相邻两点隔了超过三周时，按周补 null。没有读数的周不和前后连成直线。"""
    if len(points) < 2:
        return list(points)
    out = [points[0]]
    for item in points[1:]:
        prev_day = parse_ymd(out[-1].get("date"))
        this_day = parse_ymd(item.get("date"))
        if prev_day is not None and this_day is not None and (this_day - prev_day).days > limit:
            cursor = prev_day + timedelta(days=7)
            while (this_day - cursor).days > limit:
                out.append({"date": cursor.isoformat(), "value": None})
                cursor += timedelta(days=7)
        out.append(item)
    return out


def earnings_distorted(roe, profit_yoy) -> bool:
    """利润很薄时市盈率会失真。ROE 低于 5%，或扣非利润同比下降超过 50%。"""
    if roe is not None and float(roe) < 0.05:
        return True
    return profit_yoy is not None and float(profit_yoy) < -0.5


def choose_metric(industry_name, pe, pb, ps, deducted, roe, profit_yoy) -> tuple[str, float | None, str, str | None]:
    """返回指标、数值、短名称、换指标原因。原因单独给悬停，不写进主指标名称。"""
    text = industry_name or ""
    distorted = earnings_distorted(roe, profit_yoy)

    def pack(metric: str, value, label: str | None = None, switched: bool = False):
        return metric, float(value), label or METRIC_TEXT[metric], _DISTORTED_REASON if switched else None

    if any(word in text for word in _PB_WORDS) and _positive(pb) and float(pb) <= _OUTLIER_CAP:
        return pack("pb", pb, switched=distorted)
    if any(word in text for word in _PS_WORDS) and _positive(ps) and float(ps) <= _OUTLIER_CAP:
        return pack("ps_ttm", ps, switched=distorted)
    if distorted:
        if _positive(pb) and float(pb) <= _OUTLIER_CAP:
            return pack("pb", pb, switched=True)
        if _positive(ps) and float(ps) <= _OUTLIER_CAP:
            return pack("ps_ttm", ps, switched=True)
    if _positive(deducted) and float(deducted) <= _OUTLIER_CAP:
        return pack("pe_ttm_deducted", deducted)
    if _positive(pe) and float(pe) <= _OUTLIER_CAP:
        return "pe_ttm_deducted", float(pe), "市盈率（TTM）", None
    if _positive(pb) and float(pb) <= _OUTLIER_CAP:
        return pack("pb", pb)
    if _positive(ps) and float(ps) <= _OUTLIER_CAP:
        return pack("ps_ttm", ps)
    return "pe_ttm_deducted", None, METRIC_TEXT["pe_ttm_deducted"], None


def industry_level(l3_others: int) -> str:
    """三级行业至少 5 家同行才用，否则退回二级。"""
    if l3_others >= 5:
        return "l3"
    return "l2"


def pledge_fraction(raw) -> float | None:
    """pledge_stat.pledge_ratio 是百分数。0.80 表示占总股本 0.80%，不是 80%。"""
    number = _num(raw)
    if number is None or number < 0:
        return None
    return number / 100


def pledge_view(company_ratio, controller_ratio) -> dict | None:
    """质押是存量，不进未来 20 天的股东行为。超过 50% 时 risk_hit 为 true。"""
    company = _num(company_ratio)
    controller = _num(controller_ratio)
    if controller is not None and controller <= 0:
        controller = None
    if company is not None and company <= 0:
        company = None
    if controller is None and company is None:
        return None
    shown = controller if controller is not None else company
    if controller is not None:
        detail = f"控股股东所持股份 {_pledge_pct(controller)}% 已质押"
    else:
        detail = f"质押占总股本 {_pledge_pct(company)}%"
    return {
        "impact": "none",
        "float_ratio": None,
        "detail_text": detail,
        "risk_hit": shown is not None and shown >= 0.5,
        "counts": False,
    }


def is_st_name(name) -> bool:
    text = str(name or "").strip().upper().replace(" ", "")
    return text.startswith(("*ST", "S*ST", "SST", "ST"))


def peer_gap_text(value, mid) -> str:
    if value is None or mid in (None, 0):
        return ""
    try:
        ratio = float(value) / float(mid) - 1
    except (TypeError, ValueError, ZeroDivisionError):
        return ""
    shown = speak_multiple(mid)
    if abs(ratio) <= 0.05:
        return "接近同行"
    if ratio > 0.5:
        return f"远高于同行的 {shown} 倍"
    if ratio > 0:
        adverb = "只" if ratio <= 0.25 else ""
        return f"{adverb}比同行高 {round(ratio * 100)}%"
    if ratio < -0.5:
        return f"远低于同行的 {shown} 倍"
    return f"比同行低 {round(abs(ratio) * 100)}%"


def speak_multiple(value) -> str:
    number = float(value)
    if abs(number) >= 100:
        return f"{number:.0f}"
    if abs(number) >= 10:
        text = f"{number:.1f}"
    else:
        text = f"{number:.2f}"
    return text.rstrip("0").rstrip(".")


def support_text(items: list[dict]) -> str:
    weak = weakest_item(items)
    if weak is None:
        return ""
    return _SUPPORT.get(weak["key"], "")


def counter_text(items: list[dict], *, distorted: bool, pe_value, metric: str, cash_beats_profit: bool) -> str:
    parts = []
    if distorted and metric != "pe_ttm_deducted":
        if pe_value is not None:
            parts.append(f"利润处在低位，市盈率 {speak_multiple(pe_value)} 倍失真")
        else:
            parts.append("利润处在低位，市盈率失真")
    solvency = _item(items, "solvency")
    solvency_pct = None if solvency is None else solvency.get("peer_pctile")
    if solvency and solvency.get("value") is not None and (solvency_pct is None or solvency_pct > 0.2):
        parts.append(f"负债率 {solvency['value'] * 100:.0f}%")
    cash = _item(items, "cash")
    if _scored(cash):
        if cash_beats_profit:
            parts.append("现金流好于利润")
        elif cash.get("peer_pctile") >= 0.6:
            parts.append("现金流好于同行")
    return "，".join(parts) or "这只是历史和同行中的位置"


def _pledge_pct(number: float) -> str:
    pct = number * 100
    if pct >= 10:
        return f"{pct:.0f}"
    if pct >= 1:
        return f"{pct:.1f}".rstrip("0").rstrip(".")
    return f"{pct:.2f}"


def _scored(item: dict | None) -> bool:
    if not item or item.get("counts") is False:
        return False
    if "仅供参考" in str(item.get("note_text") or ""):
        return False
    return item.get("peer_pctile") is not None


def _item(items: list[dict], key: str) -> dict | None:
    for item in items:
        if item.get("key") == key:
            return item
    return None


def _peer_name(name, code: str) -> str:
    text = str(name or "").strip()
    symbol = code.split(".")[0]
    if not text or text in (code, symbol):
        return code
    return text


def _positive(value) -> bool:
    number = _num(value)
    return number is not None and number > 0


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number
