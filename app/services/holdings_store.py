"""持仓页自己记的东西：成交流水、想买计划、买入理由、提醒规则、体检快照。放在 data/holdings.db（HOLDINGS_DB 可改）。

只存用户写下的；行情、AI 判断、板块都是现取，不落这里。持仓是从流水算出来的（见 holdings_calc.positions），
不单独存一份「当前持仓」，免得和流水对不上。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "holdings.db"
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_code TEXT NOT NULL,
  name TEXT NOT NULL,
  side TEXT NOT NULL,
  price REAL NOT NULL,
  qty INTEGER NOT NULL,
  trade_date TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  invalid TEXT NOT NULL DEFAULT '[]',
  context TEXT NOT NULL DEFAULT '{}',
  note TEXT NOT NULL DEFAULT '',
  wish_id INTEGER,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS trades_by_stock ON trades (ts_code, trade_date, id);
CREATE TABLE IF NOT EXISTS wishes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_code TEXT NOT NULL,
  name TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'manual',
  reason TEXT NOT NULL DEFAULT '',
  conditions TEXT NOT NULL DEFAULT '[]',
  match TEXT NOT NULL DEFAULT 'all',
  plan_pct REAL NOT NULL DEFAULT 0,
  invalid TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'open',
  created_at REAL NOT NULL,
  closed_at REAL
);
CREATE TABLE IF NOT EXISTS reasons (
  ts_code TEXT PRIMARY KEY,
  text TEXT NOT NULL,
  invalid TEXT NOT NULL DEFAULT '[]',
  written_at TEXT NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_code TEXT NOT NULL,
  kind TEXT NOT NULL,
  value REAL NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS checkups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at REAL NOT NULL,
  data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

SIDES = ("buy", "sell")
SOURCES = ("scan", "sold", "diagnose", "manual")
RULE_KINDS = ("flow_out",)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def valid_day(text: str) -> bool:
    if not isinstance(text, str) or not _DAY.match(text):
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


class HoldingsStore:
    def __init__(self, path: str | os.PathLike | None = None):
        self.path = str(path or os.environ.get("HOLDINGS_DB") or _DEFAULT_PATH)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._memory = sqlite3.connect(":memory:", check_same_thread=False) if self.path == ":memory:" else None
        self._lock = threading.RLock()
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

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

    # ------------------------------------------------------------ 成交流水
    @staticmethod
    def _trade(row) -> dict:
        return {
            "id": row["id"], "ts_code": row["ts_code"], "name": row["name"], "side": row["side"],
            "price": row["price"], "qty": row["qty"], "date": row["trade_date"], "reason": row["reason"],
            "invalid": json.loads(row["invalid"] or "[]"), "context": json.loads(row["context"] or "{}"),
            "note": row["note"], "wish_id": row["wish_id"],
        }

    def trades(self, ts_code: str | None = None) -> list[dict]:
        sql = "SELECT * FROM trades" + (" WHERE ts_code=?" if ts_code else "") + " ORDER BY trade_date, id"
        with self._conn() as conn:
            rows = conn.execute(sql, (ts_code,) if ts_code else ()).fetchall()
        return [self._trade(row) for row in rows]

    def trade(self, trade_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        return self._trade(row) if row else None

    def add_trade(self, ts_code: str, name: str, side: str, price: float, qty: int, day: str,
                  reason: str = "", invalid: list | None = None, context: dict | None = None,
                  wish_id: int | None = None, now: float | None = None) -> dict:
        if side not in SIDES:
            raise ValueError("方向只能是买入或卖出")
        if not price or price <= 0:
            raise ValueError("价格要大于 0")
        if not isinstance(qty, int) or qty <= 0:
            raise ValueError("数量要是大于 0 的整数")
        if not valid_day(day):
            raise ValueError("日期要写成 2026-11-02 这样")
        held = self._held_after(self.trades(ts_code), {"side": side, "qty": qty, "date": day, "id": 10 ** 12})
        if held < 0:
            raise ValueError("卖出的数量比当时手里有的多")
        with self._conn() as conn:
            cursor = conn.execute(
                "INSERT INTO trades (ts_code, name, side, price, qty, trade_date, reason, invalid, context, wish_id, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ts_code, name, side, float(price), qty, day, reason.strip(), _json(invalid or []), _json(context or {}),
                 wish_id, now if now is not None else time.time()),
            )
            trade_id = cursor.lastrowid
        return self.trade(trade_id)

    @staticmethod
    def _held_after(existing: list[dict], extra: dict) -> int:
        """把新的一笔放进去，按日期顺序走一遍，持仓有没有掉到负数。返回最后剩多少。"""
        rows = sorted(existing + [extra], key=lambda row: (row["date"], row["id"]))
        held = 0
        for row in rows:
            held += row["qty"] if row["side"] == "buy" else -row["qty"]
            if held < 0:
                return held
        return held

    def delete_trade(self, trade_id: int) -> bool:
        item = self.trade(trade_id)
        if item is None:
            return False
        rest = [row for row in self.trades(item["ts_code"]) if row["id"] != trade_id]
        held = 0
        for row in sorted(rest, key=lambda row: (row["date"], row["id"])):
            held += row["qty"] if row["side"] == "buy" else -row["qty"]
            if held < 0:
                raise ValueError("删掉这一笔之后，后面的卖出会比手里有的多；先删后面的卖出")
        with self._conn() as conn:
            conn.execute("DELETE FROM trades WHERE id=?", (trade_id,))
        return True

    def set_trade_note(self, trade_id: int, note: str) -> bool:
        with self._conn() as conn:
            cursor = conn.execute("UPDATE trades SET note=? WHERE id=?", (note.strip(), trade_id))
            return cursor.rowcount > 0

    # ------------------------------------------------------------ 想买
    @staticmethod
    def _wish(row) -> dict:
        return {
            "id": row["id"], "ts_code": row["ts_code"], "name": row["name"], "source": row["source"],
            "reason": row["reason"], "conditions": json.loads(row["conditions"] or "[]"), "match": row["match"],
            "plan_pct": row["plan_pct"], "invalid": json.loads(row["invalid"] or "[]"), "status": row["status"],
            "created_at": row["created_at"],
        }

    def wishes(self, status: str | None = "open") -> list[dict]:
        sql = "SELECT * FROM wishes" + (" WHERE status=?" if status else "") + " ORDER BY id"
        with self._conn() as conn:
            rows = conn.execute(sql, (status,) if status else ()).fetchall()
        return [self._wish(row) for row in rows]

    def wish(self, wish_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM wishes WHERE id=?", (wish_id,)).fetchone()
        return self._wish(row) if row else None

    def add_wish(self, ts_code: str, name: str, reason: str, conditions: list, match: str = "all",
                 plan_pct: float = 0.0, invalid: list | None = None, source: str = "manual", now: float | None = None) -> dict:
        if not reason.strip():
            raise ValueError("先写一句为什么想买")
        if match not in ("all", "any"):
            raise ValueError("条件只能选全部满足或任一满足")
        if not 0 <= plan_pct <= 100:
            raise ValueError("打算买的仓位要在 0 到 100 之间")
        if source not in SOURCES:
            source = "manual"
        if any(row["ts_code"] == ts_code for row in self.wishes("open")):
            raise ValueError("已经在想买里了")
        with self._conn() as conn:
            cursor = conn.execute(
                "INSERT INTO wishes (ts_code, name, source, reason, conditions, match, plan_pct, invalid, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (ts_code, name, source, reason.strip(), _json(conditions), match, float(plan_pct), _json(invalid or []),
                 now if now is not None else time.time()),
            )
            wish_id = cursor.lastrowid
        return self.wish(wish_id)

    def update_wish(self, wish_id: int, **fields) -> dict | None:
        allowed = {"reason": str, "conditions": list, "match": str, "plan_pct": float, "invalid": list}
        sets, args = [], []
        for key, kind in allowed.items():
            if key not in fields:
                continue
            value = fields[key]
            if key == "match" and value not in ("all", "any"):
                raise ValueError("条件只能选全部满足或任一满足")
            if key == "reason" and not str(value).strip():
                raise ValueError("先写一句为什么想买")
            sets.append(f"{key}=?")
            args.append(_json(value) if kind is list else (str(value).strip() if kind is str else float(value)))
        if sets:
            with self._conn() as conn:
                conn.execute(f"UPDATE wishes SET {', '.join(sets)} WHERE id=? AND status='open'", (*args, wish_id))
        return self.wish(wish_id)

    def close_wish(self, wish_id: int, status: str = "dropped", now: float | None = None) -> bool:
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE wishes SET status=?, closed_at=? WHERE id=? AND status='open'",
                (status, now if now is not None else time.time(), wish_id),
            )
            return cursor.rowcount > 0

    # ------------------------------------------------------------ 买入理由
    def reason(self, ts_code: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM reasons WHERE ts_code=?", (ts_code,)).fetchone()
        if not row:
            return None
        return {"ts_code": ts_code, "text": row["text"], "invalid": json.loads(row["invalid"] or "[]"), "written_at": row["written_at"]}

    def set_reason(self, ts_code: str, text: str, invalid: list | None = None, written_at: str | None = None,
                   now: float | None = None) -> dict:
        if not text.strip() and not invalid:
            raise ValueError("理由不能空着")
        stamp = now if now is not None else time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO reasons (ts_code, text, invalid, written_at, updated_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(ts_code) DO UPDATE SET text=excluded.text, invalid=excluded.invalid,"
                " written_at=excluded.written_at, updated_at=excluded.updated_at",
                (ts_code, text.strip(), _json(invalid or []), written_at or date.fromtimestamp(stamp).isoformat(), stamp),
            )
        return self.reason(ts_code)

    def drop_reason(self, ts_code: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM reasons WHERE ts_code=?", (ts_code,))

    # ------------------------------------------------------------ 提醒规则（价格提醒用综合判断那边的 alerts 表）
    def rules(self, ts_code: str | None = None) -> list[dict]:
        sql = "SELECT * FROM rules" + (" WHERE ts_code=?" if ts_code else "") + " ORDER BY id"
        with self._conn() as conn:
            rows = conn.execute(sql, (ts_code,) if ts_code else ()).fetchall()
        return [{"id": row["id"], "ts_code": row["ts_code"], "kind": row["kind"], "value": row["value"]} for row in rows]

    def add_rule(self, ts_code: str, kind: str, value: float, now: float | None = None) -> dict:
        if kind not in RULE_KINDS:
            raise ValueError("不认识的提醒类型")
        if value <= 0:
            raise ValueError("天数要大于 0")
        for row in self.rules(ts_code):
            if row["kind"] == kind and row["value"] == value:
                return row
        with self._conn() as conn:
            cursor = conn.execute(
                "INSERT INTO rules (ts_code, kind, value, created_at) VALUES (?,?,?,?)",
                (ts_code, kind, float(value), now if now is not None else time.time()),
            )
            rule_id = cursor.lastrowid
        return next(row for row in self.rules(ts_code) if row["id"] == rule_id)

    def remove_rule(self, rule_id: int) -> bool:
        with self._conn() as conn:
            return conn.execute("DELETE FROM rules WHERE id=?", (rule_id,)).rowcount > 0

    # ------------------------------------------------------------ 体检快照
    def save_checkup(self, data: dict, now: float | None = None) -> int:
        with self._conn() as conn:
            return conn.execute(
                "INSERT INTO checkups (created_at, data) VALUES (?,?)", (now if now is not None else time.time(), _json(data)),
            ).lastrowid

    def checkups(self, limit: int = 2) -> list[dict]:
        """最近几次体检，新的在前。"""
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM checkups ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": row["id"], "created_at": row["created_at"], "data": json.loads(row["data"])} for row in rows]

    # ------------------------------------------------------------ 设置、已读
    def get(self, key: str, default=None):
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value) -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, _json(value)))


_lock = threading.Lock()
_store: HoldingsStore | None = None


def get_store() -> HoldingsStore:
    global _store
    with _lock:
        if _store is None:
            _store = HoldingsStore()
        return _store


def set_store(store: HoldingsStore | None) -> None:
    global _store
    with _lock:
        _store = store
