"""通知发出去之后记一笔，同一件事同一天只发一次。"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from app.paths import data_dir

_PATH = Path(os.environ.get("NOTIFY_DB") or (data_dir() / "notify.db"))


class Outbox:
    def __init__(self, path: Path | None = None):
        self.path = path or _PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY, sent_at REAL NOT NULL)")

    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def already(self, key: str) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT 1 FROM sent WHERE key=?", (key,)).fetchone()
        return row is not None

    def mark(self, key: str) -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR IGNORE INTO sent (key, sent_at) VALUES (?, ?)", (key, time.time()))
