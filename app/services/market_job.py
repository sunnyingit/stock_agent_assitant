"""定时任务：交易日 08:30 盘前简报、15:35 收盘复盘。各算一遍、存快照、记事件、需要时推送。"""

from __future__ import annotations

import threading
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.services import market_changes, market_notify, market_page, market_tape
from app.services.market_store import get_store

_ZONE = ZoneInfo("Asia/Shanghai")
SLOTS = {"pre": (8, 30), "close": (15, 35)}
GRACE = 20   # 过点后 20 分钟内都算（服务刚起来也能补上）


def due(now: datetime, done: set[str]) -> str | None:
    for name, (hour, minute) in SLOTS.items():
        key = f"{now.date().isoformat()}:{name}"
        passed = now.hour * 60 + now.minute - (hour * 60 + minute)
        if key not in done and 0 <= passed <= GRACE:
            return name
    return None


def is_trading_day(day: date) -> bool:
    try:
        return market_tape.open_dates(day, 1)[-1] == day.strftime("%Y%m%d")
    except Exception:
        return day.weekday() < 5


def refresh_holdings() -> list[str]:
    """持仓和想买里的票，每个交易日盘前让综合判断重算一次，研判记录才连得起来。出错不影响简报。"""
    try:
        from app.services import holdings_calc
        from app.services.holdings_store import get_store
        from app.synthesis import worker

        store = get_store()
        held, _notes = holdings_calc.positions(store.trades())
        codes = sorted(set(held) | {wish["ts_code"] for wish in store.wishes()})
        for code in codes:
            worker.request(code, "holdings", delay=5.0)
        return codes
    except Exception:
        return []


def run_session(session: str, now: datetime | None = None, push=None) -> dict:
    """重算一遍，存快照和事件；简报开着就推一条。"""
    now = now or datetime.now(_ZONE)
    market_page.refresh(now.date())
    summary = market_page.build_summary(now.date())
    if session == "pre":
        refresh_holdings()
    conf = market_notify.settings()
    if conf["push"] and conf[f"brief_{session}"]:
        snaps = get_store().snapshots()
        view = market_changes.view("today", snaps) if session == "close" else {}
        title, text = market_notify.brief(session, summary, view)
        (push or market_notify._push)(f"market:brief:{now.date().isoformat()}:{session}", title, text)
    return summary


class MarketJob(threading.Thread):
    def __init__(self, interval: float = 30.0):
        super().__init__(daemon=True, name="market-job")
        self.interval = interval
        self.done: set[str] = set()
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick(datetime.now(_ZONE))
            except Exception:
                pass
            self._stop_event.wait(self.interval)

    def tick(self, now: datetime) -> str | None:
        name = due(now, self.done)
        if name is None or not is_trading_day(now.date()):
            return None
        self.done.add(f"{now.date().isoformat()}:{name}")
        run_session(name, now)
        return name


_job: MarketJob | None = None


def start() -> MarketJob:
    global _job
    if _job is None or not _job.is_alive():
        _job = MarketJob()
        _job.start()
    return _job
