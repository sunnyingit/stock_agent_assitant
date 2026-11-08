"""市场页自己的存档：每日快照、通知事件、通知设置。标准库 sqlite，一个文件。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_DEFAULT_PATH = Path(os.environ.get("MARKET_DB") or Path(__file__).resolve().parents[2] / "data" / "market.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
  day TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL UNIQUE,
  day TEXT NOT NULL,
  created_at REAL NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  text TEXT NOT NULL,
  tab TEXT NOT NULL DEFAULT '',
  tone TEXT NOT NULL DEFAULT 'flat',
  read INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS event_by_id ON event (id DESC);
CREATE TABLE IF NOT EXISTS setting (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class MarketStore:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else _DEFAULT_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- 快照：每个交易日一条，当天重复写就覆盖，过了当天就定格
    def save_snapshot(self, day: str, data: dict, now: float | None = None) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO snapshot (day, created_at, data) VALUES (?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET created_at=excluded.created_at, data=excluded.data",
                (day, now or time.time(), json.dumps(data, ensure_ascii=False)),
            )

    def snapshots(self, limit: int = 120) -> list[dict]:
        """按日期升序，最多取最近 limit 条。"""
        with self._conn() as conn:
            rows = conn.execute("SELECT day, created_at, data FROM snapshot ORDER BY day DESC LIMIT ?", (limit,)).fetchall()
        return [{"day": row["day"], "created_at": row["created_at"], **json.loads(row["data"])} for row in reversed(rows)]

    # ---- 通知事件：key 唯一，同一件事只记一次
    def add_event(self, key: str, day: str, kind: str, title: str, text: str, tab: str = "", tone: str = "flat", now: float | None = None) -> bool:
        with self._lock, self._conn() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO event (key, day, created_at, kind, title, text, tab, tone) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key, day, now or time.time(), kind, title, text, tab, tone),
            )
            return cursor.rowcount == 1

    def events(self, limit: int = 60) -> list[dict]:
        with self._conn() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM event ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def unread(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM event WHERE read=0").fetchone()[0]

    def mark_read(self, ids: list[int] | None = None) -> None:
        with self._lock, self._conn() as conn:
            if ids is None:
                conn.execute("UPDATE event SET read=1")
            else:
                conn.executemany("UPDATE event SET read=1 WHERE id=?", [(int(item),) for item in ids])

    # ---- 设置
    def settings(self) -> dict:
        with self._conn() as conn:
            return {row["key"]: json.loads(row["value"]) for row in conn.execute("SELECT key, value FROM setting").fetchall()}

    def set_settings(self, values: dict) -> None:
        with self._lock, self._conn() as conn:
            conn.executemany(
                "INSERT INTO setting (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(key, json.dumps(value)) for key, value in values.items()],
            )


_store: MarketStore | None = None
_store_lock = threading.Lock()


def get_store() -> MarketStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = MarketStore()
        return _store


def set_store(store: MarketStore | None) -> None:
    global _store
    with _store_lock:
        _store = store
