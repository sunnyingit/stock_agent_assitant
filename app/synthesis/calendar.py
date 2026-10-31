"""交易时段与数据新鲜度。目前按工作日算，没有节假日表；接入交易日历时只改这里。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

_ZONE = ZoneInfo("Asia/Shanghai")


def now_cn() -> datetime:
    return datetime.now(_ZONE)


def _weekday(day: date) -> bool:
    return day.weekday() < 5


def previous_workday(day: date) -> date:
    cursor = day - timedelta(days=1)
    while not _weekday(cursor):
        cursor -= timedelta(days=1)
    return cursor


def session_of(now: datetime) -> str:
    """盘中（9:30–15:00 的工作日）返回 intraday，其余时间返回 close。"""
    if _weekday(now.date()) and (9, 30) <= (now.hour, now.minute) < (15, 0):
        return "intraday"
    return "close"


def last_close_date(now: datetime) -> date:
    """最近一个已经收盘的交易日。"""
    today = now.date()
    if _weekday(today) and (now.hour, now.minute) >= (15, 0):
        return today
    return previous_workday(today if _weekday(today) else today + timedelta(days=1))


def workday_gap(start: date, end: date) -> int:
    """start 之后到 end（含）经过多少个工作日；start 不早于 end 时为 0。"""
    gap = 0
    cursor = start
    while cursor < end:
        cursor += timedelta(days=1)
        if _weekday(cursor):
            gap += 1
    return gap


def parse_day(text) -> date | None:
    digits = "".join(ch for ch in str(text or "") if ch.isdigit())[:8]
    if len(digits) < 8:
        return None
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None
