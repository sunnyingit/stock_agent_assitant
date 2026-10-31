"""存档、缓存、任务队列和战绩，全部放在一个 SQLite 文件里（标准库，不加依赖）。

多实例部署时把这一层换成共享库即可：对外只有这些方法。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "synthesis.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_code TEXT NOT NULL,
  session TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  created_at REAL NOT NULL,
  facts_hash TEXT NOT NULL,
  mode TEXT NOT NULL,
  source TEXT NOT NULL,
  fallback_reason TEXT NOT NULL DEFAULT '',
  direction TEXT NOT NULL,
  level INTEGER NOT NULL,
  position TEXT NOT NULL,
  rule_direction TEXT NOT NULL,
  rule_level INTEGER NOT NULL,
  rule_position TEXT NOT NULL,
  diverged INTEGER NOT NULL DEFAULT 0,
  model TEXT NOT NULL DEFAULT '',
  prompt_version TEXT NOT NULL DEFAULT '',
  policy_version TEXT NOT NULL DEFAULT '',
  data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_by_stock ON runs (ts_code, id DESC);
CREATE INDEX IF NOT EXISTS runs_by_hash ON runs (ts_code, facts_hash);
CREATE TABLE IF NOT EXISTS kv_cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
  ts_code TEXT PRIMARY KEY,
  due_at REAL NOT NULL,
  first_at REAL NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  running INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS watchlist (
  ts_code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_code TEXT NOT NULL,
  side TEXT NOT NULL,
  price REAL NOT NULL,
  confirm TEXT NOT NULL DEFAULT 'close',
  base_name TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS alerts_by_stock ON alerts (ts_code);
CREATE TABLE IF NOT EXISTS finance_signals (
  ts_code TEXT NOT NULL,
  report_end TEXT NOT NULL,
  version TEXT NOT NULL,
  announced TEXT NOT NULL,
  recorded_at REAL NOT NULL,
  lean TEXT NOT NULL,
  source TEXT NOT NULL,
  statuses TEXT NOT NULL,
  tracked TEXT NOT NULL DEFAULT '{}',
  ret_5 REAL, excess_5 REAL, ret_20 REAL, excess_20 REAL,
  PRIMARY KEY (ts_code, report_end, version)
);
CREATE TABLE IF NOT EXISTS outcomes (
  run_id INTEGER NOT NULL,
  horizon INTEGER NOT NULL,
  ret REAL NOT NULL,
  settled_at REAL NOT NULL,
  PRIMARY KEY (run_id, horizon)
);
"""


class Store:
    def __init__(self, path: str | os.PathLike | None = None):
        self.path = str(path or os.environ.get("SYNTHESIS_DB") or _DEFAULT_PATH)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._memory = sqlite3.connect(":memory:", check_same_thread=False) if self.path == ":memory:" else None
        self._lock = threading.RLock()
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(finance_signals)")}
            if "tracked" not in columns:   # 早先建的库没有这一列
                conn.execute("ALTER TABLE finance_signals ADD COLUMN tracked TEXT NOT NULL DEFAULT '{}'")

    @contextmanager
    def _conn(self):
        with self._lock:
            conn = self._memory or sqlite3.connect(self.path, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                if self._memory is None:
                    conn.close()

    # ------------------------------------------------------------ 存档
    def save_run(self, run: dict) -> int:
        final = run["final"]
        rule = run["rule"]
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO runs (ts_code, session, trade_date, created_at, facts_hash, mode, source,
                   fallback_reason, direction, level, position, rule_direction, rule_level, rule_position,
                   diverged, model, prompt_version, policy_version, data)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run["ts_code"], run["session"], run["trade_date"], run.get("created_at") or time.time(),
                    run["facts_hash"], run["mode"], run["source"], run.get("fallback_reason") or "",
                    final["direction"], final["level"], final["position"],
                    rule["direction"], rule["level"], rule["position"],
                    1 if run.get("diverged") else 0, run.get("model") or "", run.get("prompt_version") or "",
                    run.get("policy_version") or "", json.dumps(run["data"], ensure_ascii=False, default=str),
                ),
            )
            return int(cursor.lastrowid)

    def _row(self, row) -> dict | None:
        if row is None:
            return None
        out = dict(row)
        out["data"] = json.loads(out["data"])
        return out

    def latest(self, ts_code: str, session: str | None = None) -> dict | None:
        sql = "SELECT * FROM runs WHERE ts_code=?" + (" AND session=?" if session else "") + " ORDER BY id DESC LIMIT 1"
        args = (ts_code, session) if session else (ts_code,)
        with self._conn() as conn:
            return self._row(conn.execute(sql, args).fetchone())

    def by_hash(self, ts_code: str, facts_hash: str, session: str, trade_date: str) -> dict | None:
        """同一个交易日内，事实没变才复用；换了一天就要重新存一条，否则每日判断会出现空洞。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE ts_code=? AND facts_hash=? AND session=? AND trade_date=? ORDER BY id DESC LIMIT 1",
                (ts_code, facts_hash, session, trade_date),
            ).fetchone()
            return self._row(row)

    def judgments(self, ts_code: str, limit: int = 400) -> list[dict]:
        """每个交易日页面上实际展示的判断，按日期升序。同一天优先取收盘后那一份，否则取最新的。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT trade_date, session, data FROM runs WHERE ts_code=? ORDER BY id DESC LIMIT ?", (ts_code, limit),
            ).fetchall()
        by_date: dict[str, dict] = {}
        for row in rows:
            held = by_date.get(row["trade_date"])
            if held is not None and not (held["session"] == "intraday" and row["session"] == "close"):
                continue
            data = json.loads(row["data"])
            state = data.get("final_state") or {}
            if not state:
                continue
            by_date[row["trade_date"]] = {
                "trade_date": row["trade_date"], "session": row["session"],
                "lean": state.get("direction") or "unclear", "level": state.get("level") or 0,
                "text": (data.get("shown") or {}).get("change_text", ""),
            }
        return [by_date[key] for key in sorted(by_date)]

    def recent(self, ts_code: str, limit: int = 10) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM runs WHERE ts_code=? ORDER BY id DESC LIMIT ?", (ts_code, limit)).fetchall()
            return [self._row(row) for row in rows]

    # ------------------------------------------------------------ 缓存（正反方论据）
    def get(self, key: str):
        with self._conn() as conn:
            row = conn.execute("SELECT value, expires_at FROM kv_cache WHERE key=?", (key,)).fetchone()
        if row is None or row["expires_at"] < time.time():
            return None
        return json.loads(row["value"])

    def set(self, key: str, value, ttl: float) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kv_cache (key, value, expires_at) VALUES (?,?,?)",
                (key, json.dumps(value, ensure_ascii=False, default=str), time.time() + ttl),
            )

    # ------------------------------------------------------------ 任务队列
    def enqueue(self, ts_code: str, delay: float, reason: str = "", max_wait: float = 120.0, now: float | None = None) -> None:
        """同一只股票短时间内多次触发会合并：每次触发把到期时间推后 delay，但最久不超过 max_wait。"""
        now = now if now is not None else time.time()
        with self._conn() as conn:
            row = conn.execute("SELECT first_at, running FROM jobs WHERE ts_code=?", (ts_code,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO jobs (ts_code, due_at, first_at, reason) VALUES (?,?,?,?)",
                    (ts_code, now + delay, now, reason),
                )
            elif not row["running"]:
                due = min(now + delay, row["first_at"] + max_wait)
                conn.execute("UPDATE jobs SET due_at=?, reason=? WHERE ts_code=?", (due, reason, ts_code))

    def claim_due(self, now: float | None = None) -> list[dict]:
        now = now if now is not None else time.time()
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM jobs WHERE running=0 AND due_at<=?", (now,)).fetchall()
            claimed = []
            for row in rows:
                conn.execute("UPDATE jobs SET running=1, attempts=attempts+1 WHERE ts_code=?", (row["ts_code"],))
                claimed.append({**dict(row), "attempts": row["attempts"] + 1})
            return claimed

    def finish(self, ts_code: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM jobs WHERE ts_code=?", (ts_code,))

    def retry(self, ts_code: str, delay: float, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        with self._conn() as conn:
            conn.execute("UPDATE jobs SET running=0, due_at=? WHERE ts_code=?", (now + delay, ts_code))

    def pending(self, ts_code: str) -> bool:
        with self._conn() as conn:
            return conn.execute("SELECT 1 FROM jobs WHERE ts_code=?", (ts_code,)).fetchone() is not None

    def release_stuck(self, older_than: float = 600.0, now: float | None = None) -> None:
        """进程重启后，把卡在"运行中"的任务放回队列。"""
        now = now if now is not None else time.time()
        with self._conn() as conn:
            conn.execute("UPDATE jobs SET running=0 WHERE running=1 AND due_at<?", (now - older_than,))

    # ------------------------------------------------------------ 自选
    def watch_add(self, ts_code: str, name: str, now: float | None = None) -> bool:
        """加入自选。已经在里面返回 False。"""
        with self._conn() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO watchlist (ts_code, name, added_at) VALUES (?,?,?)",
                (ts_code, name, now if now is not None else time.time()),
            )
            return cursor.rowcount > 0

    def watch_remove(self, ts_code: str) -> bool:
        with self._conn() as conn:
            return conn.execute("DELETE FROM watchlist WHERE ts_code=?", (ts_code,)).rowcount > 0

    def watch_list(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM watchlist ORDER BY added_at, ts_code").fetchall()]

    # ------------------------------------------------------------ 提醒
    def alert_add(self, ts_code: str, side: str, price: float, confirm: str = "close", base_name: str = "", now: float | None = None) -> dict:
        """同一只股票、同一个方向、同一个价只存一条：重复设置返回已有的那条。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM alerts WHERE ts_code=? AND side=? AND ABS(price-?)<0.005", (ts_code, side, price),
            ).fetchone()
            if row is None:
                cursor = conn.execute(
                    "INSERT INTO alerts (ts_code, side, price, confirm, base_name, created_at) VALUES (?,?,?,?,?,?)",
                    (ts_code, side, price, confirm, base_name, now if now is not None else time.time()),
                )
                row = conn.execute("SELECT * FROM alerts WHERE id=?", (cursor.lastrowid,)).fetchone()
            return dict(row)

    def alerts(self, ts_code: str | None = None) -> list[dict]:
        sql = "SELECT * FROM alerts" + (" WHERE ts_code=?" if ts_code else "") + " ORDER BY id"
        with self._conn() as conn:
            return [dict(row) for row in conn.execute(sql, (ts_code,) if ts_code else ()).fetchall()]

    def alert_remove(self, alert_id: int) -> bool:
        with self._conn() as conn:
            return conn.execute("DELETE FROM alerts WHERE id=?", (alert_id,)).rowcount > 0

    # ------------------------------------------------------------ 财报信号（回测用）
    def save_finance_signal(self, ts_code: str, report_end: str, version: str, announced: str, lean: str, source: str, statuses: dict, tracked: dict | None = None) -> bool:
        """每份财报、每个版本只记第一次：回测要看的是披露后第一时间的判断。返回是否新记。"""
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO finance_signals (ts_code, report_end, version, announced, recorded_at, lean, source, statuses, tracked)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (ts_code, report_end, version, announced, time.time(), lean, source, json.dumps(statuses, ensure_ascii=False), json.dumps(tracked or {}, ensure_ascii=False)),
            )
            return cursor.rowcount > 0

    def finance_signals(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM finance_signals ORDER BY announced, ts_code").fetchall()
        return [{**dict(row), "statuses": json.loads(row["statuses"]), "tracked": json.loads(row["tracked"] or "{}")} for row in rows]

    def previous_finance_signal(self, ts_code: str, before_end: str) -> dict | None:
        """这只股票上一个报告期的信号（最早记下的那条），第 11 题跟踪上期预警用。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM finance_signals WHERE ts_code=? AND report_end<? ORDER BY report_end DESC, recorded_at ASC LIMIT 1",
                (ts_code, before_end),
            ).fetchone()
        return None if row is None else {**dict(row), "statuses": json.loads(row["statuses"]), "tracked": json.loads(row["tracked"] or "{}")}

    def save_finance_outcome(self, ts_code: str, report_end: str, version: str, horizon: int, ret: float, excess) -> None:
        if horizon not in (5, 20):
            raise ValueError("horizon 只能是 5 或 20")
        with self._conn() as conn:
            conn.execute(
                f"UPDATE finance_signals SET ret_{horizon}=?, excess_{horizon}=? WHERE ts_code=? AND report_end=? AND version=?",
                (ret, excess, ts_code, report_end, version),
            )

    # ------------------------------------------------------------ 战绩
    def unsettled(self, horizon: int, limit: int = 500) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT r.* FROM runs r LEFT JOIN outcomes o ON o.run_id=r.id AND o.horizon=?
                   WHERE o.run_id IS NULL AND r.session='close' ORDER BY r.id LIMIT ?""",
                (horizon, limit),
            ).fetchall()
            return [self._row(row) for row in rows]

    def save_outcome(self, run_id: int, horizon: int, ret: float) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO outcomes (run_id, horizon, ret, settled_at) VALUES (?,?,?,?)",
                (run_id, horizon, ret, time.time()),
            )

    def stats(self, horizon: int) -> list[dict]:
        """按 来源 × 强度 × 是否分歧 统计命中率。命中：偏多后涨、偏空后跌；不明不统计。

        规则版在每次运行里都有结论，所以"规则"一行统计全部样本；"agent"一行只统计 Agent 通过校验的样本。
        分歧样本里谁更常对，看 diverged=1 的两行对比。
        """
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT r.source, r.level, r.direction, r.rule_level, r.rule_direction, r.diverged, o.ret
                   FROM runs r JOIN outcomes o ON o.run_id=r.id WHERE o.horizon=?""",
                (horizon,),
            ).fetchall()
        buckets: dict[tuple, dict] = {}

        def add(source: str, level: int, diverged: int, direction: str, ret: float) -> None:
            if direction not in ("bullish", "bearish"):
                return
            key = (source, level, diverged)
            bucket = buckets.setdefault(key, {"source": source, "level": level, "diverged": diverged, "n": 0, "hits": 0})
            bucket["n"] += 1
            bucket["hits"] += 1 if (ret > 0 if direction == "bullish" else ret < 0) else 0

        for row in rows:
            add("rule", row["rule_level"], row["diverged"], row["rule_direction"], row["ret"])
            if row["source"] == "agent":
                add("agent", row["level"], row["diverged"], row["direction"], row["ret"])
        out = []
        for bucket in buckets.values():
            bucket["hit_rate"] = round(bucket["hits"] / bucket["n"], 4)
            out.append(bucket)
        return sorted(out, key=lambda item: (item["source"], item["level"], item["diverged"]))
