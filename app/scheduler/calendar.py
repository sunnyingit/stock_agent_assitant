"""交易日历和交易时段。

数据来自 tushare 的 trade_cal，按年落盘到 data/calendar.json；取不到（或这一年还没公布）时退回「周一到周五」，
并标记 approx，页面和日志里能看出来。测试里传一个假的 loader。
"""

from __future__ import annotations

import json
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from app.paths import data_dir

_PATH = data_dir() / "calendar.json"
# 上午 09:30–11:30，下午 13:00–15:00（北京时间）
SESSIONS = {"am": ("09:30", "11:30"), "pm": ("13:00", "15:00")}


def _minutes(hhmm: str) -> int:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return hour * 60 + minute


class TradingCalendar:
    def __init__(self, loader: Callable[[int], set[str]] | None = None, path: Path | None = None):
        self._loader = loader or default_loader
        self._path = path or _PATH
        self._years: dict[int, set[str]] = {}
        self._approx: set[int] = set()
        self._lock = threading.Lock()

    # ---- 年度数据
    def _load(self, year: int) -> set[str] | None:
        with self._lock:
            if year in self._years:
                return self._years[year] or None
            stored = self._read().get(str(year))
            if stored:
                self._years[year] = set(stored)
                return self._years[year]
            try:
                days = set(self._loader(year))
            except Exception:
                days = set()
            # 一年至少有 200 个交易日才算完整；还没公布的年份不落盘，下次再取
            if len(days) >= 200:
                self._years[year] = days
                self._write(year, sorted(days))
                return days
            self._years[year] = set()
            self._approx.add(year)
            return None

    def _read(self) -> dict:
        try:
            return json.loads(self._path.read_text())
        except Exception:
            return {}

    def _write(self, year: int, days: list[str]) -> None:
        try:
            data = self._read()
            data[str(year)] = days
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(data, ensure_ascii=False))
        except Exception:
            pass

    # ---- 查询
    def is_trading_day(self, day: date) -> bool:
        days = self._load(day.year)
        if days is None:
            return day.weekday() < 5
        return day.isoformat() in days

    def approximate(self, day: date) -> bool:
        """这一天是按「工作日」估的（日历没取到）。"""
        self._load(day.year)
        return day.year in self._approx

    def next_trading_day(self, day: date) -> date:
        cur = day + timedelta(days=1)
        while not self.is_trading_day(cur):
            cur += timedelta(days=1)
        return cur

    def prev_trading_day(self, day: date) -> date:
        cur = day - timedelta(days=1)
        while not self.is_trading_day(cur):
            cur -= timedelta(days=1)
        return cur

    def matches(self, day: date, rule: str) -> bool:
        """触发器里的 days：trading | weekday | any。"""
        if rule == "trading":
            return self.is_trading_day(day)
        if rule == "weekday":
            return day.weekday() < 5
        return True

    def session_of(self, minute_of_day: int) -> str | None:
        for name, (start, end) in SESSIONS.items():
            if _minutes(start) <= minute_of_day < _minutes(end):
                return name
        return None

    def session_slots(self, day: date, every_min: int, sessions: tuple[str, ...] = ("am", "pm")) -> list[int]:
        """某个交易日里所有触发时刻，用「当天几点几分」的分钟数表示。每个时段从开始时刻起每 every_min 分钟一次，到时段结束前为止。"""
        if not self.is_trading_day(day):
            return []
        out = []
        for name in sessions:
            start, end = _minutes(SESSIONS[name][0]), _minutes(SESSIONS[name][1])
            out.extend(range(start, end, every_min))
        return out


def default_loader(year: int) -> set[str]:
    from app.services import market_tape as tape

    rows = tape._records(lambda: tape.pro().trade_cal(exchange="SSE", start_date=f"{year}0101", end_date=f"{year}1231", is_open="1"))
    return {tape.iso(row.get("cal_date")) for row in rows if tape.iso(row.get("cal_date"))}
