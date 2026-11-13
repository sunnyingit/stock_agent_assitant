"""服务端库 data/server.db：设备、订阅、离线转发、产物、就绪标记。设计 4.1、4.4、8.1。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from app.scheduler.ids import ulid

from app.paths import data_dir

_PATH = Path(os.environ.get("SERVER_DB") or (data_dir() / "server.db"))
DEVICE_TTL_MS = 30 * 86400_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS device (
  device_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, created_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL, last_stream_at INTEGER
);
CREATE TABLE IF NOT EXISTS subscription (
  device_id TEXT NOT NULL, ts_code TEXT NOT NULL, tier TEXT NOT NULL DEFAULT 'other', updated_at INTEGER NOT NULL,
  PRIMARY KEY (device_id, ts_code)
);
CREATE TABLE IF NOT EXISTS device_rev (device_id TEXT NOT NULL, scope TEXT NOT NULL, rev INTEGER NOT NULL, PRIMARY KEY (device_id, scope));
CREATE TABLE IF NOT EXISTS relay_alert (
  device_id TEXT NOT NULL, alert_id TEXT NOT NULL, ts_code TEXT NOT NULL, side TEXT NOT NULL, price REAL NOT NULL,
  confirm TEXT NOT NULL, rearm TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'armed', fire_seq INTEGER NOT NULL DEFAULT 0,
  fired_at INTEGER, rearm_at INTEGER, last_value TEXT, label TEXT NOT NULL DEFAULT '', channels TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (device_id, alert_id)
);
CREATE TABLE IF NOT EXISTS relay_channel (
  device_id TEXT NOT NULL, channel_id TEXT NOT NULL, type TEXT NOT NULL, secret_enc BLOB NOT NULL, only_offline INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (device_id, channel_id)
);
CREATE TABLE IF NOT EXISTS relay_event (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL, event TEXT NOT NULL, created_at INTEGER NOT NULL,
  acked_at INTEGER, pushed_at INTEGER, push_attempts INTEGER NOT NULL DEFAULT 0, pushed_to TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS relay_event_dev ON relay_event (device_id, seq);
CREATE TABLE IF NOT EXISTS artifact (
  kind TEXT NOT NULL, subject TEXT NOT NULL, as_of TEXT NOT NULL, session TEXT NOT NULL, schema TEXT NOT NULL,
  policy_version TEXT NOT NULL DEFAULT '', facts_hash TEXT NOT NULL, producer TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL, expires_at INTEGER, payload TEXT NOT NULL,
  PRIMARY KEY (kind, subject, as_of, session, schema)
);
CREATE INDEX IF NOT EXISTS artifact_by_day ON artifact (kind, as_of);
CREATE TABLE IF NOT EXISTS ready_marker (date TEXT NOT NULL, gate TEXT NOT NULL, ready_at INTEGER, checked_at INTEGER NOT NULL, PRIMARY KEY (date, gate));
CREATE TABLE IF NOT EXISTS system_event (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT NOT NULL UNIQUE, event TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS demand (ts_code TEXT PRIMARY KEY, hits INTEGER NOT NULL DEFAULT 0, last_at INTEGER NOT NULL);
"""


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_of(value) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class ServerStore:
    def __init__(self, path: str | os.PathLike | None = None):
        self._memory = str(path) == ":memory:"
        self.path = ":memory:" if self._memory else str(path or os.environ.get("SERVER_DB") or _PATH)
        if not self._memory:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._shared = sqlite3.connect(":memory:", check_same_thread=False) if self._memory else None
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        with self._lock:
            conn = self._shared or sqlite3.connect(self.path, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                if self._shared is None:
                    conn.close()

    @staticmethod
    def _row(row):
        return None if row is None else dict(row)

    # ------------------------------------------------------------ 设备
    def add_device(self, token_hash: str, now: int) -> str:
        device_id = ulid(now)
        with self._conn() as conn:
            conn.execute("INSERT INTO device (device_id, token_hash, created_at, last_seen_at) VALUES (?,?,?,?)", (device_id, token_hash, now, now))
        return device_id

    def device(self, device_id: str) -> dict | None:
        with self._conn() as conn:
            return self._row(conn.execute("SELECT * FROM device WHERE device_id=?", (device_id,)).fetchone())

    def touch_device(self, device_id: str, now: int, stream: bool = False) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE device SET last_seen_at=?" + (", last_stream_at=?" if stream else "") + " WHERE device_id=?",
                         (now, now, device_id) if stream else (now, device_id))

    def delete_device(self, device_id: str) -> None:
        """撤销设备 = 删除它的全部数据。"""
        with self._conn() as conn:
            for table in ("subscription", "device_rev", "relay_alert", "relay_channel", "relay_event", "device"):
                conn.execute(f"DELETE FROM {table} WHERE device_id=?", (device_id,))

    def online(self, device_id: str, now: int, within_ms: int = 45_000) -> bool:
        device = self.device(device_id)
        return bool(device and device["last_stream_at"] and now - device["last_stream_at"] <= within_ms)

    def stale_devices(self, now: int) -> list[str]:
        with self._conn() as conn:
            return [r["device_id"] for r in conn.execute("SELECT device_id FROM device WHERE last_seen_at<?", (now - DEVICE_TTL_MS,)).fetchall()]

    def rev(self, device_id: str, scope: str) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT rev FROM device_rev WHERE device_id=? AND scope=?", (device_id, scope)).fetchone()
        return row["rev"] if row else 0

    def set_rev(self, conn, device_id: str, scope: str, rev: int) -> None:
        conn.execute("INSERT INTO device_rev (device_id, scope, rev) VALUES (?,?,?) ON CONFLICT(device_id, scope) DO UPDATE SET rev=excluded.rev", (device_id, scope, rev))

    # ------------------------------------------------------------ 订阅
    def put_subscriptions(self, device_id: str, codes: list[dict], now: int) -> int:
        with self._conn() as conn:
            conn.execute("DELETE FROM subscription WHERE device_id=?", (device_id,))
            for item in codes:
                conn.execute("INSERT OR REPLACE INTO subscription (device_id, ts_code, tier, updated_at) VALUES (?,?,?,?)",
                             (device_id, item["ts_code"], item.get("tier") if item.get("tier") in ("hold", "other") else "other", now))
        return len(codes)

    def subscription_union(self, now: int) -> list[dict]:
        """并集，按（有设备持有，订阅设备数，最近被请求次数）排序。设备 30 天没心跳的订阅作废。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT s.ts_code AS ts_code, MAX(s.tier='hold') AS held, COUNT(DISTINCT s.device_id) AS devices, COALESCE(d.hits, 0) AS hits"
                " FROM subscription s JOIN device v ON v.device_id=s.device_id AND v.last_seen_at>=? LEFT JOIN demand d ON d.ts_code=s.ts_code"
                " GROUP BY s.ts_code ORDER BY held DESC, devices DESC, hits DESC, s.ts_code", (now - DEVICE_TTL_MS,)).fetchall()
        return [{"ts_code": r["ts_code"], "held": bool(r["held"]), "devices": r["devices"], "hits": r["hits"]} for r in rows]

    def bump_demand(self, ts_code: str, now: int) -> None:
        with self._conn() as conn:
            conn.execute("INSERT INTO demand (ts_code, hits, last_at) VALUES (?,1,?) ON CONFLICT(ts_code) DO UPDATE SET hits=hits+1, last_at=excluded.last_at", (ts_code, now))

    # ------------------------------------------------------------ 离线转发
    def put_relay(self, device_id: str, rev: int, alerts: list[dict], channels: list[dict], encrypt) -> tuple[bool, int]:
        """全量同步 + 乐观版本。rev 必须大于已存的；否则返回 (False, 当前 rev)。定义没变的提醒保留触发状态。"""
        with self._conn() as conn:
            current = self.rev(device_id, "relay")
            if rev <= current:
                return False, current
            old = {r["alert_id"]: dict(r) for r in conn.execute("SELECT * FROM relay_alert WHERE device_id=?", (device_id,)).fetchall()}
            conn.execute("DELETE FROM relay_alert WHERE device_id=?", (device_id,))
            for alert in alerts:
                prev = old.get(alert["alert_id"])
                same = prev and (prev["ts_code"], prev["side"], prev["price"], prev["confirm"]) == (alert["ts_code"], alert["side"], float(alert["price"]), alert["confirm"])
                conn.execute(
                    "INSERT INTO relay_alert (device_id, alert_id, ts_code, side, price, confirm, rearm, state, fire_seq, fired_at, rearm_at, last_value, label, channels)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (device_id, alert["alert_id"], alert["ts_code"], alert["side"], float(alert["price"]), alert["confirm"], canonical(alert.get("rearm") or {"kind": "hysteresis", "pct": 1.0}),
                     prev["state"] if same else "armed", prev["fire_seq"] if same else (prev["fire_seq"] if prev else 0), prev["fired_at"] if same else None,
                     prev["rearm_at"] if same else None, prev["last_value"] if same else None, alert.get("label", ""), canonical(alert.get("channels") or [])),
                )
            kept = {c["channel_id"] for c in channels}
            existing = {r["channel_id"]: r["secret_enc"] for r in conn.execute("SELECT channel_id, secret_enc FROM relay_channel WHERE device_id=?", (device_id,)).fetchall()}
            conn.execute("DELETE FROM relay_channel WHERE device_id=?", (device_id,))
            for channel in channels:
                secret = encrypt(channel["secret"]) if channel.get("secret") else existing.get(channel["channel_id"])
                if secret is None:
                    continue
                conn.execute("INSERT INTO relay_channel (device_id, channel_id, type, secret_enc, only_offline) VALUES (?,?,?,?,?)",
                             (device_id, channel["channel_id"], channel["type"], secret, int(channel.get("only_offline", True))))
            self.set_rev(conn, device_id, "relay", rev)
        return True, rev

    def relay_alerts(self, device_id: str | None = None, confirm: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM relay_alert WHERE 1=1", []
        if device_id:
            sql += " AND device_id=?"
            args.append(device_id)
        if confirm:
            sql += " AND confirm=?"
            args.append(confirm)
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["rearm"], item["channels"] = json.loads(item["rearm"]), json.loads(item["channels"])
            item["last_value"] = json.loads(item["last_value"]) if item["last_value"] else None
            out.append(item)
        return out

    def update_relay_alert(self, device_id: str, alert_id: str, **fields) -> None:
        fields.pop("last_eval_at", None)
        if isinstance(fields.get("last_value"), (dict, list)):
            fields["last_value"] = canonical(fields["last_value"])
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._conn() as conn:
            conn.execute(f"UPDATE relay_alert SET {sets} WHERE device_id=? AND alert_id=?", (*fields.values(), device_id, alert_id))

    def relay_channels(self, device_id: str) -> dict[str, dict]:
        with self._conn() as conn:
            return {r["channel_id"]: dict(r) for r in conn.execute("SELECT * FROM relay_channel WHERE device_id=?", (device_id,)).fetchall()}

    def add_relay_event(self, device_id: str, event: dict, now: int) -> int | None:
        """同一个 dedupe_key 对同一设备只存一次。"""
        with self._conn() as conn:
            for row in conn.execute("SELECT event FROM relay_event WHERE device_id=? ORDER BY seq DESC LIMIT 200", (device_id,)).fetchall():
                if json.loads(row["event"]).get("dedupe_key") == event["dedupe_key"]:
                    return None
            return conn.execute("INSERT INTO relay_event (device_id, event, created_at) VALUES (?,?,?)", (device_id, canonical(event), now)).lastrowid

    def relay_events(self, device_id: str, after: int = 0, limit: int = 200) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM relay_event WHERE device_id=? AND seq>? ORDER BY seq LIMIT ?", (device_id, after, limit)).fetchall()
        return [{"seq": r["seq"], "event": json.loads(r["event"]), "created_at": r["created_at"], "acked_at": r["acked_at"], "pushed_at": r["pushed_at"]} for r in rows]

    def ack_events(self, device_id: str, seqs: list[int], now: int) -> int:
        with self._conn() as conn:
            return sum(conn.execute("UPDATE relay_event SET acked_at=? WHERE device_id=? AND seq=? AND acked_at IS NULL", (now, device_id, s)).rowcount for s in seqs)

    def unpushed_events(self, limit: int = 100) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM relay_event WHERE pushed_at IS NULL AND push_attempts<3 ORDER BY seq LIMIT ?", (limit,)).fetchall()
        return [{"seq": r["seq"], "device_id": r["device_id"], "event": json.loads(r["event"]), "created_at": r["created_at"], "acked_at": r["acked_at"],
                 "push_attempts": r["push_attempts"], "pushed_to": json.loads(r["pushed_to"])} for r in rows]

    def mark_channel_pushed(self, seq: int, channel_id: str) -> None:
        with self._conn() as conn:
            row = conn.execute("SELECT pushed_to FROM relay_event WHERE seq=?", (seq,)).fetchone()
            done = json.loads(row["pushed_to"]) if row else []
            if channel_id not in done:
                conn.execute("UPDATE relay_event SET pushed_to=? WHERE seq=?", (json.dumps(done + [channel_id]), seq))

    def mark_pushed(self, seq: int, now: int, done: bool) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE relay_event SET push_attempts=push_attempts+1" + (", pushed_at=?" if done else "") + " WHERE seq=?", (now, seq) if done else (seq,))

    # ------------------------------------------------------------ 产物
    def put_artifact(self, kind: str, subject: str, as_of: str, session: str, schema: str, payload: dict, now: int,
                     policy_version: str = "", producer: dict | None = None, ttl_days: int = 30) -> dict:
        envelope_hash = hash_of(payload)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO artifact (kind, subject, as_of, session, schema, policy_version, facts_hash, producer, created_at, expires_at, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(kind, subject, as_of, session, schema) DO UPDATE SET policy_version=excluded.policy_version, facts_hash=excluded.facts_hash,"
                " producer=excluded.producer, created_at=excluded.created_at, expires_at=excluded.expires_at, payload=excluded.payload",
                (kind, subject, as_of, session, schema, policy_version, envelope_hash, canonical(producer or {"host": "server"}), now, now + ttl_days * 86400_000, canonical(payload)),
            )
        return self.artifact(kind, subject, as_of, session, schema)

    @staticmethod
    def _artifact(row) -> dict | None:
        if row is None:
            return None
        return {"v": 1, "kind": row["kind"], "subject": row["subject"], "as_of": row["as_of"], "session": row["session"], "schema": row["schema"],
                "policy_version": row["policy_version"], "facts_hash": row["facts_hash"], "producer": json.loads(row["producer"]),
                "created_at": row["created_at"], "expires_at": row["expires_at"], "payload": json.loads(row["payload"])}

    def artifact(self, kind: str, subject: str, as_of: str | None = None, session: str | None = None, schema: str | None = None) -> dict | None:
        sql, args = "SELECT * FROM artifact WHERE kind=? AND subject=?", [kind, subject]
        for column, value in (("as_of", as_of), ("session", session), ("schema", schema)):
            if value:
                sql += f" AND {column}=?"
                args.append(value)
        sql += " ORDER BY as_of DESC, created_at DESC LIMIT 1"
        with self._conn() as conn:
            return self._artifact(conn.execute(sql, args).fetchone())

    def manifest(self, kind: str, as_of: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT subject, session, schema, facts_hash, created_at FROM artifact WHERE kind=? AND as_of=? ORDER BY subject", (kind, as_of)).fetchall()
        return [dict(r) for r in rows]

    def has_fresh(self, kind: str, subject: str, as_of: str, session: str, schema: str) -> dict | None:
        return self.artifact(kind, subject, as_of, session, schema)

    # ------------------------------------------------------------ 就绪标记
    def set_ready(self, date: str, gate: str, ready: bool, now: int) -> None:
        with self._conn() as conn:
            prev = conn.execute("SELECT ready_at FROM ready_marker WHERE date=? AND gate=?", (date, gate)).fetchone()
            ready_at = (prev["ready_at"] if prev and prev["ready_at"] else now) if ready else None
            conn.execute("INSERT INTO ready_marker (date, gate, ready_at, checked_at) VALUES (?,?,?,?) ON CONFLICT(date, gate) DO UPDATE SET ready_at=excluded.ready_at, checked_at=excluded.checked_at",
                         (date, gate, ready_at, now))

    def ready(self, date: str) -> dict:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM ready_marker WHERE date=?", (date,)).fetchall()
        return {r["gate"]: ({"ready": True, "at": r["ready_at"]} if r["ready_at"] else {"ready": False, "checked_at": r["checked_at"]}) for r in rows}

    # ------------------------------------------------------------ 系统事件流（服务端任务的回执，所有设备共用，不含任何用户数据）
    def add_system_event(self, event: dict, now: int) -> int | None:
        with self._conn() as conn:
            cursor = conn.execute("INSERT OR IGNORE INTO system_event (dedupe_key, event, created_at) VALUES (?,?,?)", (event["dedupe_key"], canonical(event), now))
            return cursor.lastrowid if cursor.rowcount else None

    def system_events(self, after: int = 0, since: int = 0, limit: int = 200) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT seq, event, created_at FROM system_event WHERE seq>? AND created_at>=? ORDER BY seq LIMIT ?", (after, since, limit)).fetchall()
        return [{"seq": r["seq"], "event": json.loads(r["event"]), "created_at": r["created_at"]} for r in rows]

    # ------------------------------------------------------------ 清理
    def purge(self, now: int) -> dict:
        with self._conn() as conn:
            artifacts = conn.execute("DELETE FROM artifact WHERE expires_at IS NOT NULL AND expires_at<?", (now,)).rowcount
            conn.execute("DELETE FROM system_event WHERE created_at<?", (now - 7 * 86400_000,))
            events = conn.execute("DELETE FROM relay_event WHERE created_at<? AND (acked_at IS NOT NULL OR pushed_at IS NOT NULL)", (now - 7 * 86400_000,)).rowcount
        return {"artifacts": artifacts, "relay_events": events, "stale_devices": len(self.stale_devices(now))}
