"""新闻第三、四层：量级、权重、市场反应和方向由代码计算。系数尚未回测。"""

from __future__ import annotations

import hashlib
import re

_CATEGORIES = {"业绩", "经营", "资本", "风险与治理", "政策与行业", "宏观与市场", "观点", "传闻与澄清"}
_DIRECTIONS = {"利好": 1, "利空": -1, "中性": 0}
_BASIS = {"事实": 1.0, "计划": 0.8, "预测": 0.6, "传闻": 0.3}
_SCOPE = {"公司": 1.0, "行业": 0.6, "市场": 0.3}
_MAGNITUDE = {"大": 1.5, "中": 1.0, "小": 0.5}
_GRADE_RANK = {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1}
_CERTAINTY = ["低", "一般", "高"]
_PRE_MOVE = 5.0
_VOLUME_HOT = 1.5
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_AMOUNT = re.compile(r"(-?\d+(?:\.\d+)?)\s*(万亿|亿|万元|万|元)?")


def score_news(
    facts: list[dict],
    articles: list[dict],
    *,
    bars: list[dict] | None = None,
    sector: list[dict] | None = None,
    heat_count: int = 0,
    as_of: str = "",
) -> dict:
    by_article = {row.get("id"): row for row in articles or [] if isinstance(row, dict)}
    events = []
    for fact in facts or []:
        if isinstance(fact, dict) and fact.get("id"):
            events.append(_event(
                fact,
                by_article.get(fact.get("article_id")) or {},
                bars or [],
                sector or [],
                as_of,
            ))
    signed = round(sum(item["signed_weight"] for item in events), 4)
    verdict = _verdict(events, signed, heat_count)
    return {"events": events, "score": signed, "verdict": verdict}


def _event(fact: dict, article: dict, bars: list[dict], sector: list[dict], as_of: str = "") -> dict:
    text = f"{article.get('title') or ''} {article.get('summary') or ''}"
    category = fact.get("category") if fact.get("category") in _CATEGORIES else ""
    direction = fact.get("direction") if fact.get("direction") in _DIRECTIONS else "中性"
    basis = _basis(fact.get("basis"), article.get("source_grade") or "E", category)
    scope = fact.get("scope") if fact.get("scope") in _SCOPE else _scope_from_article(article)
    magnitude = _magnitude(fact, text, category)
    weight = _MAGNITUDE[magnitude] * _BASIS[basis] * _SCOPE[scope] * _decay(fact, article, bars, as_of)
    reaction = _reaction(direction, fact, article, bars, sector)
    signed = round(weight * _DIRECTIONS[direction], 4)
    return {
        "fact_id": fact.get("id"),
        "event_id": _fingerprint(category, fact.get("event"), article.get("time") or fact.get("time")),
        "category": category,
        "scope": scope,
        "basis": basis,
        "direction": direction,
        "magnitude": magnitude,
        "persistence": fact.get("persistence") or "",
        "source_grade": article.get("source_grade") or "",
        "source": article.get("source") or "",
        "weight": round(weight, 4),
        "signed_weight": signed,
        "reaction": reaction,
        "market_reaction": "；".join(reaction.get("adjustments") or []),
    }


def _basis(value, grade: str, category: str) -> str:
    basis = value if value in _BASIS else "传闻"
    if category == "观点":
        basis = "预测" if basis == "事实" else basis
    if grade == "E":
        return "传闻"
    if grade == "D" and basis == "事实":
        return "预测"
    return basis


def _scope_from_article(article: dict) -> str:
    if article.get("relevance") == "中":
        return "行业"
    if article.get("relevance") == "高":
        return "公司"
    return "市场"


def _magnitude(fact: dict, text: str, category: str) -> str:
    amount = _amount_in_text(fact.get("amount"), text)
    base = _amount_in_text(fact.get("base"), text)
    if amount is not None and base not in (None, 0):
        ratio = abs(amount / base)
        if ratio >= 0.3:
            return "大"
        if ratio >= 0.1:
            return "中"
        return "小"
    claimed = fact.get("magnitude") if fact.get("magnitude") in _MAGNITUDE else ""
    if claimed in {"大", "中"} and _numbers_in_text(fact.get("numbers") or [], text):
        return claimed
    if category in {"风险与治理"} and claimed == "大" and _numbers_in_text(fact.get("numbers") or [], text):
        return "大"
    return "小"


def _amount_in_text(value, text: str):
    raw = str(value or "").strip()
    if not raw:
        return None
    matched = _AMOUNT.search(raw.replace(",", ""))
    if not matched:
        return None
    number = matched.group(1)
    if number not in text.replace(",", "") and _plain_number(number) not in text.replace(",", ""):
        return None
    amount = float(matched.group(1))
    unit = matched.group(2) or ""
    if unit == "万亿":
        return amount * 1e12
    if unit == "亿":
        return amount * 1e8
    if unit in {"万", "万元"}:
        return amount * 1e4
    return amount


def _plain_number(number: str) -> str:
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    return number


def _numbers_in_text(numbers: list, text: str) -> bool:
    compact = text.replace(",", "")
    for number in numbers:
        token = _plain_number(str(number).strip())
        if token and token in compact:
            return True
    found = _NUMBER.findall(str(numbers))
    return any(token in compact for token in found)


def _decay(fact: dict, article: dict, bars: list[dict], as_of: str = "") -> float:
    moment = str(article.get("time") or fact.get("time") or "")
    day = "".join(ch for ch in moment if ch.isdigit())[:8]
    dates = [str(row.get("trade_date") or "") for row in bars if row.get("trade_date")]
    as_of_day = "".join(ch for ch in str(as_of or "") if ch.isdigit())[:8]
    if not day or not dates:
        return 1.0
    last = dates[-1]
    # 日线还停在上一个交易日时，今天也算过了一档。
    extra = bool(as_of_day and as_of_day > last and day < as_of_day)
    if day >= last and not extra:
        return 1.0
    passed = len([trade_date for trade_date in dates if day < trade_date <= last])
    if extra:
        passed += 1
    if passed <= 0:
        return 1.0
    return round(0.8 ** passed, 4)


def _reaction(direction: str, fact: dict, article: dict, bars: list[dict], sector: list[dict]) -> dict:
    if direction not in {"利好", "利空"} or not bars:
        return {"adjustments": []}
    dates = [str(row.get("trade_date")) for row in bars]
    day = _reaction_day(article, fact, dates)
    if day not in dates:
        return {"adjustments": [], "pending": True}   # 反应日还没有行情（比如收盘后发布的新闻）：市场确实还没来得及反应
    index = dates.index(day)
    sector_map = {str(row.get("trade_date")): row.get("pct_change") for row in sector or []}
    excesses = []
    for cursor in range(max(0, index - 5), index):
        excess = _excess(bars[cursor], sector_map)
        if excess is not None:
            excesses.append(excess)
    day_excess = _excess(bars[index], sector_map)
    volumes = [float(bars[cursor]["vol"]) for cursor in range(max(0, index - 5), index) if _number(bars[cursor].get("vol"))]
    volume_ratio = None
    if volumes and _number(bars[index].get("vol")):
        volume_ratio = round(float(bars[index]["vol"]) / (sum(volumes) / len(volumes)), 2)
    adjustments = []
    pre_excess = round(sum(excesses), 2) if len(excesses) >= 5 else None
    if direction == "利好" and pre_excess is not None and pre_excess >= _PRE_MOVE:
        adjustments.append("市场可能已经反应")
    if direction == "利好" and day_excess is not None and day_excess < 0:
        if volume_ratio is not None and volume_ratio >= _VOLUME_HOT:
            adjustments.append("利好当日放量下跌")
        else:
            adjustments.append("利好当日下跌")
    if direction == "利空" and day_excess is not None and day_excess > 0:
        adjustments.append("利空未被市场认可")
    return {
        "pre_excess": pre_excess,
        "day_excess": None if day_excess is None else round(day_excess, 2),
        "volume_ratio": volume_ratio,
        "adjustments": adjustments,
    }


def _reaction_day(article: dict, fact: dict, dates: list[str]) -> str:
    moment = str(article.get("time") or fact.get("time") or "")
    day = "".join(ch for ch in moment if ch.isdigit())[:8]
    if article.get("session") == "盘后":
        later = [trade_date for trade_date in dates if trade_date > day]
        return later[0] if later else ""
    if day in dates:
        return day
    later = [trade_date for trade_date in dates if trade_date >= day]
    return later[0] if later else ""


def _excess(bar: dict, sector_map: dict):
    stock = bar.get("pct_chg")
    sector = sector_map.get(str(bar.get("trade_date")))
    if not _number(stock) or not _number(sector):
        return None
    return float(stock) - float(sector)


def _number(value) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _fingerprint(category: str, event, when) -> str:
    day = "".join(ch for ch in str(when or "") if ch.isdigit())[:8]
    text = re.sub(r"\s+", "", str(event or ""))[:80]
    raw = f"{category}|{text}|{day}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _verdict(events: list[dict], signed: float, heat_count: int) -> dict:
    effective = [item for item in events if item["direction"] in {"利好", "利空"} and item["category"]]
    adjustments = [note for item in effective for note in item["reaction"].get("adjustments") or []]
    if abs(signed) < 0.3:
        side = _tie_break(effective)
        level = 1
        certainty = "低"
    else:
        side = "偏多" if signed > 0 else "偏空"
        level = 3 if abs(signed) > 2 else 2 if abs(signed) > 1 else 1
        certainty = _certainty(effective, heat_count)
    level = max(1, level - len(adjustments))
    notes = []
    if len(effective) < 2:
        notes.append("有效事件少于2条")
    if heat_count >= 5 and not any(item["source_grade"] in {"A", "B"} for item in effective):
        notes.append("热度高但缺少公告或官方事实")
    notes.extend(adjustments)
    return {
        "lean": side,
        "lean_level": level,
        "certainty": certainty,
        "certainty_note": "；".join(dict.fromkeys(notes)),
        "score": signed,
    }


def _tie_break(events: list[dict]) -> str:
    bull = [item for item in events if item["direction"] == "利好"]
    bear = [item for item in events if item["direction"] == "利空"]
    if not bull and not bear:
        return "偏空"
    bull_rank = max((_rank(item) for item in bull), default=(-1, -1))
    bear_rank = max((_rank(item) for item in bear), default=(-1, -1))
    return "偏多" if bull_rank > bear_rank else "偏空"


def _rank(item: dict) -> tuple[int, int]:
    earnings = 1 if item["category"] == "业绩" else 0
    return earnings, _GRADE_RANK.get(item["source_grade"], 0)


def _certainty(events: list[dict], heat_count: int) -> str:
    index = 1
    consistent = _reaction_consistent(events)
    if any(item["source_grade"] == "A" and item["basis"] == "事实" for item in events) and consistent:
        index += 1
    if events and all(item["basis"] in {"预测", "传闻"} or item["category"] == "观点" for item in events):
        index -= 1
    sources = {item.get("source") or item["fact_id"] for item in events}
    if len(sources) <= 1:
        index -= 1
    if heat_count >= 5 and not any(item["source_grade"] in {"A", "B"} for item in events):
        index = min(index, 0)
    if len(events) < 2:
        index = min(index, 0)
    return _CERTAINTY[min(2, max(0, index))]


def _reaction_consistent(events: list[dict]) -> bool:
    checked = False
    for item in events:
        excess = item["reaction"].get("day_excess")
        if excess is None:
            continue
        checked = True
        if item["direction"] == "利好" and excess <= 0:
            return False
        if item["direction"] == "利空" and excess >= 0:
            return False
    return checked
