"""新闻第一层：来源分级、去重、主体匹配、新旧和盘前盘中盘后。"""

from __future__ import annotations

import re
from datetime import datetime

from app.tools.stock import Stock

_GRADE_RANK = {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1}
_IMPORTANT = ("公告", "预告", "快报", "定期报告", "问询", "立案", "处罚", "诉讼", "澄清", "回购", "减持", "增持")
_MEDIA = (
    "财联社", "证券时报", "上海证券报", "中国证券报", "证券日报", "第一财经",
    "澎湃", "新华", "东方财富", "华尔街见闻", "界面", "每日经济",
)
_HOT = {"xueqiu", "weibo", "zhihu", "baidu", "toutiao", "douyin"}
_MEDIA_IDS = {"cls", "wallstreetcn", "thepaper", "eastmoney"}


_PASSING = ("只剩", "只剩下", "除外", "千元股")
_FUND = ("调仓", "持仓", "加仓", "减仓", "基金经理", "重仓", "加码")
_TAPE = ("走势", "飘红", "全线", "逆势", "领涨", "领跌", "大涨", "大跌", "上涨", "下跌")
_NOT_TAPE = ("预告", "出炉", "财报", "业绩", "提价", "涨价", "降价", "回购", "减持", "公告")
_NAME_TAIL = ("银行", "证券", "保险", "集团", "股份")
_TOKEN_STOP = (
    "白酒", "行业", "公司", "市场", "板块", "公告", "新闻", "最新", "曝光", "股份",
    "关于", "发布", "表示", "以及", "其他", "高端", "本周", "密集", "动向", "证券",
    "报道", "董事会", "股东会", "股东大会", "投资者", "有限公司",
)
_ART = re.compile(r"AN\d+")


def grade_source(item: dict) -> str:
    title = str(item.get("title") or "")
    source = f"{item.get('source_name') or ''} {item.get('source') or ''}"
    blob = title + source
    if item.get("source") == "exchange" or "交易所" in source:
        return "A"
    if any(word in blob for word in ("公告", "上交所", "深交所", "北交所", "巨潮", "证监会", "cninfo")):
        return "A"
    if any(word in source for word in ("统计局", "工信部", "海关总署", "行业协会")):
        return "B"
    if item.get("source") in _MEDIA_IDS or any(word in source for word in _MEDIA):
        return "C"
    if any(word in blob for word in ("研报", "目标价", "评级", "盈利预测")):
        return "D"
    if item.get("source") in _HOT:
        return "E"
    return "E"


def relevance(item: dict, stock: Stock) -> str:
    """公司名要当主角。个股新闻流里的行业稿、行情稿和顺带提及都要降级。"""
    title = str(item.get("title") or "")
    if _quote_page(item, stock) or _broadcast(title, stock):
        return "低"
    if any(word in title for word in _FUND) and not _company_subject(title, stock):
        return "低"
    if item.get("source") == "exchange":
        return "高"
    if _company_subject(title, stock):
        return "高"
    if _mentions(title, stock):
        return "低"
    industry = (stock.industry or "").strip()
    blob = f"{title} {str(item.get('summary') or '')[:80]}"
    if industry and industry in blob:
        return "中"
    return "低"


def _aliases(stock: Stock) -> list[str]:
    names = []
    if stock.name:
        names.append(stock.name)
        for prefix in ("贵州", "中国", "上海", "北京"):
            if stock.name.startswith(prefix) and len(stock.name) - len(prefix) >= 2:
                names.append(stock.name[len(prefix):])
    return names


def _hit(text: str, name: str) -> bool:
    index = text.find(name)
    if index < 0 or not name:
        return False
    after = text[index + len(name):index + len(name) + 2]
    return after not in _NAME_TAIL


def _mentions(text: str, stock: Stock) -> bool:
    if stock.code and stock.code in text:
        return True
    return any(_hit(text, name) for name in _aliases(stock))


def _company_subject(title: str, stock: Stock) -> bool:
    if not _mentions(title, stock):
        return False
    if any(word in title for word in _PASSING):
        return False
    if any(word in title for word in _FUND):
        head = title[:8]
        if not any(_hit(head, name) for name in _aliases(stock)):
            return False
    return True


def _quote_page(item: dict, stock: Stock) -> bool:
    url = str(item.get("url") or "").lower()
    if "xueqiu.com/s/" in url:
        return True
    title = str(item.get("title") or "").strip()
    if not str(item.get("time") or "").strip() and title in {stock.name, stock.code, stock.ts_code}:
        return True
    return False


def _broadcast(title: str, stock: Stock) -> bool:
    if _company_subject(title, stock):
        return False
    if any(word in title for word in _NOT_TAPE):
        return False
    return any(word in title for word in _TAPE)


def _topic_tokens(title: str, stock: Stock) -> set[str]:
    text = re.sub(r"[^\u4e00-\u9fff]", "", title)
    for name in _aliases(stock):
        text = text.replace(name, "")
    for word in _TOKEN_STOP:
        text = text.replace(word, "")
    tokens = set()
    for part in re.findall(r"[\u4e00-\u9fff]{3,}", text):
        for index in range(len(part) - 2):
            tokens.add(part[index:index + 3])
    return tokens


def _same_reprint(left: dict, right: dict, stock: Stock) -> bool:
    """只合并同一天的转载。不同日期的回购进展是不同事件。"""
    left_art = _art_code(left)
    right_art = _art_code(right)
    if left_art and right_art and left_art != right_art:
        return False
    if _publish_day(left) != _publish_day(right):
        return False
    tokens = _topic_tokens(str(left.get("title") or ""), stock) & _topic_tokens(str(right.get("title") or ""), stock)
    return bool(tokens)


def _cluster(rows: list[dict], stock: Stock, dropped: list[dict]) -> list[dict]:
    groups: list[dict] = []
    for row in rows:
        placed = False
        for group in groups:
            if _same_reprint(group["rows"][0], row, stock):
                dropped.append(_drop(row, "duplicate"))
                group["rows"][0] = _merge(group["rows"][0], row)
                placed = True
                break
        if not placed:
            groups.append({"rows": [row]})
    return [group["rows"][0] for group in groups]


def session_of(value) -> str:
    moment = _parse_time(value)
    if moment is None:
        return ""
    minutes = moment.hour * 60 + moment.minute
    if minutes < 9 * 60 + 30:
        return "盘前"
    if minutes <= 15 * 60:
        return "盘中"
    return "盘后"


def clean_news(
    items: list[dict],
    stock: Stock,
    *,
    today: datetime | None = None,
    trade_dates: list[str] | None = None,
    window_days: int = 3,
) -> tuple[list[dict], dict]:
    today = today or datetime.now()
    grouped: dict[str, dict] = {}
    dropped: list[dict] = []
    reprints: list[dict] = []
    dropped_relevance = 0
    dropped_old = 0
    heat = 0
    dates = trade_dates or []
    for item in items or []:
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            continue
        row = dict(item)
        row["source_grade"] = grade_source(row)
        row["relevance"] = relevance(row, stock)
        row["session"] = session_of(row.get("time"))
        row["sources_count"] = 1
        title = str(row.get("title") or "")
        if row["source_grade"] == "E" or _broadcast(title, stock):
            heat += 1
        if row["relevance"] == "低":
            dropped_relevance += 1
            reason = "market_recap" if _quote_page(row, stock) or _broadcast(title, stock) else "irrelevant"
            dropped.append(_drop(row, reason))
            continue
        if not _fresh(row, today, dates, window_days):
            if _mentions(title, stock) and _within_long(row, today, dates):
                reprints.append(row)
            else:
                dropped_old += 1
            continue
        key = _dedupe_key(row)
        current = grouped.get(key)
        if current is None:
            grouped[key] = row
            continue
        dropped.append(_drop(row, "duplicate"))
        grouped[key] = _merge(current, row)
    kept = _cluster(list(grouped.values()), stock, dropped)
    kept, absorbed = _absorb(kept, reprints, stock, dropped)
    dropped_old += len(reprints) - absorbed
    _mark_window(kept, dates, window_days, today)
    kept = sorted(kept, key=lambda row: str(row.get("time") or ""), reverse=True)
    for row in kept:
        row["heat_count"] = heat
    return kept, {
        "read": len(items or []),
        "kept": len(kept),
        "in_window": sum(1 for row in kept if row.get("in_window")),
        "heat": heat,
        "dropped_relevance": dropped_relevance,
        "dropped_old": dropped_old,
        "dropped_duplicate": max(len(items or []) - dropped_relevance - dropped_old - len(kept), 0),
        "dropped": dropped,
    }


def annotate(
    items: list[dict],
    stock: Stock,
    *,
    today: datetime | None = None,
    trade_dates: list[str] | None = None,
    window_days: int = 3,
) -> tuple[list[dict], dict]:
    """只给每条新闻补上属性（来源等级、盘前盘中盘后、是否在窗口内），不丢弃、不合并。
    是否相关、是否重复、是否例行，交给模型判断；clean_news 只在模型不可用时兜底。"""
    today = today or datetime.now()
    rows = []
    for item in items or []:
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            continue
        row = dict(item)
        row["source_grade"] = grade_source(row)
        row["session"] = session_of(row.get("time"))
        row["sources_count"] = 1
        rows.append(row)
    _mark_window(rows, trade_dates or [], window_days, today)
    heat = sum(1 for row in rows if row["source_grade"] == "E")
    for row in rows:
        row["heat_count"] = heat
    return rows, {"read": len(items or []), "heat": heat}


def _merge(left: dict, right: dict) -> dict:
    better = right if _GRADE_RANK[right["source_grade"]] > _GRADE_RANK[left["source_grade"]] else left
    if _GRADE_RANK[right["source_grade"]] == _GRADE_RANK[left["source_grade"]]:
        if right.get("source") == "exchange" and left.get("source") != "exchange":
            better = right
        elif left.get("source") == "exchange":
            better = left
    other = left if better is right else right
    merged = dict(better)
    merged["time"] = _earlier(left.get("time"), right.get("time"))
    merged["session"] = session_of(merged["time"])
    merged["sources_count"] = int(left.get("sources_count") or 1) + int(right.get("sources_count") or 1)
    if not merged.get("summary"):
        merged["summary"] = other.get("summary") or ""
    return merged


def _earlier(left, right) -> str:
    first = _parse_time(left)
    second = _parse_time(right)
    if first and second:
        return str(left if first <= second else right)
    return str(left or right or "")


def _fresh(item: dict, today: datetime, trade_dates: list[str], window_days: int = 3) -> bool:
    moment = _parse_time(item.get("time"))
    if moment is None:
        return True
    day = moment.strftime("%Y%m%d")
    if trade_dates:
        if day > trade_dates[-1]:
            return True
        recent = set(trade_dates[-max(int(window_days or 3), 1):])
        longer = set(trade_dates[-20:])
        if day in recent:
            return True
        return bool(_long_window(item) and day in longer)
    age = (today.date() - moment.date()).days
    if age <= 5:
        return True
    return bool(_long_window(item) and age <= 30)


def _long_window(item: dict) -> bool:
    """重要公告，以及还没落地的计划，可以回到 20 个交易日。"""
    title = str(item.get("title") or "")
    if item.get("source_grade") == "A" or any(word in title for word in _IMPORTANT):
        return True
    pending = any(word in title for word in ("拟", "计划", "预告", "将于"))
    done = any(word in title for word in ("完成", "已实施", "已回购"))
    return pending and not done


def _dedupe_key(row: dict) -> str:
    title = re.sub(r"\W+", "", str(row.get("title") or "").lower())[:40]
    art = _art_code(row)
    if row.get("source") == "exchange" and art:
        return art
    if row.get("source") == "exchange":
        return f"{title}|{_publish_day(row)}"
    return title


def _art_code(row: dict) -> str:
    matched = _ART.search(str(row.get("url") or ""))
    return matched.group(0) if matched else ""


def _publish_day(row: dict) -> str:
    moment = _parse_time(row.get("time"))
    return moment.strftime("%Y%m%d") if moment else ""


def _drop(row: dict, reason: str) -> dict:
    return {"title": row.get("title") or "", "reason": reason, "time": row.get("time") or ""}


def _within_long(item: dict, today: datetime, trade_dates: list[str]) -> bool:
    moment = _parse_time(item.get("time"))
    if moment is None:
        return False
    day = moment.strftime("%Y%m%d")
    if trade_dates:
        return day in set(trade_dates[-20:]) or day > trade_dates[-1]
    return (today.date() - moment.date()).days <= 30


def _absorb(kept: list[dict], reprints: list[dict], stock: Stock, dropped: list[dict]) -> tuple[list[dict], int]:
    """窗口外的媒体稿如果只是同一天公告的转载，并进那条公告。"""
    absorbed = 0
    for row in reprints:
        host = next((item for item in kept if _same_reprint(item, row, stock)), None)
        if host is None:
            continue
        dropped.append(_drop(row, "duplicate"))
        kept[kept.index(host)] = _merge(host, row)
        absorbed += 1
    return kept, absorbed


def _mark_window(rows: list[dict], trade_dates: list[str], window_days: int, today: datetime) -> None:
    recent = set(trade_dates[-max(int(window_days or 3), 1):]) if trade_dates else set()
    last = trade_dates[-1] if trade_dates else ""
    for row in rows:
        moment = _parse_time(row.get("time"))
        if moment is None:
            row["in_window"] = False
            continue
        day = moment.strftime("%Y%m%d")
        if trade_dates:
            row["in_window"] = day in recent or day > last
        else:
            row["in_window"] = (today.date() - moment.date()).days <= 5


def _parse_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text[:19] if " " in fmt else text[:10], fmt)
        except ValueError:
            continue
    matched = re.match(r"(\d{2})-(\d{2})(?:\s+(\d{2}):(\d{2}))?", text)
    if not matched:
        return None
    now = datetime.now()
    hour = int(matched.group(3) or 0)
    minute = int(matched.group(4) or 0)
    try:
        return datetime(now.year, int(matched.group(1)), int(matched.group(2)), hour, minute)
    except ValueError:
        return None
