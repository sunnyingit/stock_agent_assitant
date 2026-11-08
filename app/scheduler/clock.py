"""时间约定：时刻用整数毫秒时间戳（UTC）；交易日用 YYYY-MM-DD，按北京时间。测试里换一个假时钟就行。"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Shanghai")


class Clock:
    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeClock(Clock):
    """测试用：时间只在调用 advance / set 时才动。"""

    def __init__(self, at: datetime | str | int = "2026-11-05 08:00"):
        self._ms = at if isinstance(at, int) else to_ms(at)

    def now_ms(self) -> int:
        return self._ms

    def sleep(self, seconds: float) -> None:
        self._ms += int(seconds * 1000)

    def advance(self, seconds: float = 0, minutes: float = 0, hours: float = 0, days: float = 0) -> None:
        self._ms += int((seconds + minutes * 60 + hours * 3600 + days * 86400) * 1000)

    def set(self, at: datetime | str | int) -> None:
        self._ms = at if isinstance(at, int) else to_ms(at)


def to_ms(value: datetime | str) -> int:
    """'2026-11-05 21:00'（按北京时间）或 datetime → 毫秒。"""
    if isinstance(value, str):
        text = value.strip().replace("T", " ")
        value = datetime.strptime(text, "%Y-%m-%d %H:%M" if len(text) <= 16 else "%Y-%m-%d %H:%M:%S")
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZONE)
    return int(value.timestamp() * 1000)


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, ZONE)


def day_of(ms: int) -> date:
    return from_ms(ms).date()


def at_time(day: date, hhmm: str) -> int:
    """某一天的 HH:MM（北京时间）→ 毫秒。"""
    hour, minute = (int(part) for part in hhmm.split(":"))
    return to_ms(datetime(day.year, day.month, day.day, hour, minute))


def daterange(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)
