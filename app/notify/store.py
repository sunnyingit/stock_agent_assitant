"""通知库 data/notify.db：事件、规则、规则状态、收件箱、渠道、投递、设置。设计 5.4、6.1、7.3–7.5。

和旧的 sent 表（Outbox）放在同一个文件里但互不相干。一个库一把锁。按 user_id 隔离。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from app.auth.migrate import (
    LEGACY_USER,
    ensure_user_id_column,
    rebuild_channel,
    rebuild_event_dedupe,
    rebuild_notification,
    rebuild_setting,
    table_cols,
)
from app.scheduler.ids import ulid

from app.paths import data_dir

_PATH = Path(os.environ.get("NOTIFY_DB") or (data_dir() / "notify.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS event (
  event_id TEXT PRIMARY KEY, type TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL,
  occurred_at INTEGER NOT NULL, trade_date TEXT, severity TEXT NOT NULL, dedupe_key TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '{}', correlation_id TEXT, ttl_s INTEGER, schema_v INTEGER NOT NULL DEFAULT 1,
  payload TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE (user_id, dedupe_key)
);
CREATE TABLE IF NOT EXISTS rule (
  rule_id TEXT PRIMARY KEY, type TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL,
  params TEXT NOT NULL DEFAULT '{}', mode TEXT NOT NULL DEFAULT 'edge',
  rearm TEXT NOT NULL DEFAULT '{"kind":"hysteresis","pct":1.0}', cooldown_s INTEGER NOT NULL DEFAULT 0,
  routing TEXT NOT NULL DEFAULT '{}', origin TEXT NOT NULL DEFAULT 'user', origin_ref TEXT,
  enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, v INTEGER NOT NULL DEFAULT 1,
  user_id TEXT NOT NULL DEFAULT 'default'
);
CREATE TABLE IF NOT EXISTS rule_state (
  rule_id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'armed', last_value TEXT, last_eval_at INTEGER,
  fired_at INTEGER, rearm_at INTEGER, fire_seq INTEGER NOT NULL DEFAULT 0, cursor TEXT,
  user_id TEXT NOT NULL DEFAULT 'default'
);
CREATE TABLE IF NOT EXISTS notification (
  notif_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, tone TEXT NOT NULL,
  link TEXT NOT NULL DEFAULT '{}', group_key TEXT, digest_id TEXT, severity TEXT NOT NULL DEFAULT 'normal',
  type TEXT NOT NULL DEFAULT '', subject_id TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL, read_at INTEGER, archived_at INTEGER,
  user_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE (user_id, event_id)
);
CREATE TABLE IF NOT EXISTS channel (
  user_id TEXT NOT NULL DEFAULT 'default',
  channel_id TEXT NOT NULL, type TEXT NOT NULL, name TEXT NOT NULL, secret_ref TEXT, config TEXT NOT NULL DEFAULT '{}',
  min_severity TEXT NOT NULL DEFAULT 'normal', quiet_hours TEXT NOT NULL DEFAULT '{"from":"22:30","to":"08:00"}',
  rate_limit TEXT NOT NULL DEFAULT '{"per_min":10,"per_day":100}', relay INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1, verified_at INTEGER,
  PRIMARY KEY (user_id, channel_id)
);
CREATE TABLE IF NOT EXISTS delivery (
  delivery_id TEXT PRIMARY KEY, notif_id TEXT NOT NULL, channel_id TEXT NOT NULL, state TEXT NOT NULL,
  deliver_after INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at INTEGER, last_error TEXT,
  provider_msg_id TEXT, suppressed_reason TEXT, sent_at INTEGER, expires_at INTEGER,
  user_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE (channel_id, notif_id)
);
CREATE TABLE IF NOT EXISTS setting (
  user_id TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  PRIMARY KEY (user_id, key)
);
"""

# 索引在 migrate 补齐 user_id 之后再建，避免旧库缺列时 CREATE INDEX 失败
_INDEXES = """
CREATE INDEX IF NOT EXISTS event_by_subject ON event (user_id, subject_kind, subject_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS event_by_type ON event (user_id, type, occurred_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS event_user_dedupe ON event (user_id, dedupe_key);
CREATE INDEX IF NOT EXISTS rule_by_type ON rule (user_id, type, enabled);
CREATE INDEX IF NOT EXISTS notif_unread ON notification (user_id, read_at, created_at DESC);
CREATE INDEX IF NOT EXISTS delivery_due ON delivery (user_id, state, deliver_after);
"""

JSON_COLS = {"source", "payload", "params", "rearm", "routing", "link", "config", "quiet_hours", "rate_limit"}
DEFAULT_SETTINGS = {
    "enabled": True,
    "quiet_hours": {"from": "22:30", "to": "08:00"},
    "digest": {"enabled": False, "times": ["08:35", "21:30"]},
    "collapse_s": 600,
    "relay": {"enabled": False, "url": "", "device_id": "", "rev": 0, "sub_rev": 0, "cursor": 0, "sys_cursor": 0, "defs_hash": "", "subs_hash": "", "secrets": {}, "healthy_at": 0},
}


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class NotifyStore:
    def __init__(self, path: str | os.PathLike | None = None, user_id: str = LEGACY_USER):
        self._memory = str(path) == ":memory:"
        self.path = ":memory:" if self._memory else str(path or os.environ.get("NOTIFY_DB") or _PATH)
        self.user_id = (user_id or LEGACY_USER).strip() or LEGACY_USER
        if not self._memory:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._shared = sqlite3.connect(":memory:", check_same_thread=False) if self._memory else None
        if self._shared is not None:
            self._shared.row_factory = sqlite3.Row
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)
            conn.executescript(_INDEXES)

    def for_user(self, user_id: str) -> "NotifyStore":
        other = NotifyStore.__new__(NotifyStore)
        other.path = self.path
        other.user_id = (user_id or LEGACY_USER).strip() or LEGACY_USER
        other._memory = self._memory
        other._lock = self._lock
        other._shared = self._shared
        return other

    def _migrate(self, conn: sqlite3.Connection) -> None:
        for table in ("notification", "rule", "rule_state", "channel", "delivery"):
            ensure_user_id_column(conn, table)
        if table_cols(conn, "sent"):
            ensure_user_id_column(conn, "sent")
        rebuild_event_dedupe(conn)
        rebuild_notification(conn)
        rebuild_channel(conn)
        rebuild_setting(conn)

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
    def _row(row) -> dict | None:
        if row is None:
            return None
        out = dict(row)
        for key in JSON_COLS & out.keys():
            if isinstance(out[key], str):
                try:
                    out[key] = json.loads(out[key])
                except ValueError:
                    pass
        return out

    # ------------------------------------------------------------ 事件
    def add_event(self, event: dict) -> dict | None:
        """dedupe_key 唯一：重复返回 None。"""
        with self._conn() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO event (event_id, type, subject_kind, subject_id, occurred_at, trade_date, severity, dedupe_key,"
                " source, correlation_id, ttl_s, schema_v, payload, user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event["event_id"], event["type"], event["subject"]["kind"], event["subject"]["id"], event["occurred_at"], event.get("trade_date"),
                 event["severity"], event["dedupe_key"], _dumps(event.get("source") or {}), event.get("correlation_id"), event.get("ttl_s"),
                 event.get("v", 1), _dumps(event["payload"]), self.user_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.event(event["event_id"])

    def event(self, event_id: str) -> dict | None:
        with self._conn() as conn:
            return self._event(self._row(conn.execute(
                "SELECT * FROM event WHERE event_id=? AND user_id=?", (event_id, self.user_id)
            ).fetchone()))

    @staticmethod
    def _event(row: dict | None) -> dict | None:
        if row is None:
            return None
        row["subject"] = {"kind": row.pop("subject_kind"), "id": row.pop("subject_id")}
        row["v"] = row.pop("schema_v")
        return row

    def events(self, type_: str | None = None, subject_id: str | None = None, since: int | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
        sql, args = "SELECT * FROM event WHERE user_id=?", [self.user_id]
        for column, value in (("type", type_), ("subject_id", subject_id)):
            if value:
                sql += f" AND {column}=?"
                args.append(value)
        if since is not None:
            sql += " AND occurred_at>=?"
            args.append(since)
        sql += " ORDER BY occurred_at DESC, event_id DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self._conn() as conn:
            return [self._event(self._row(row)) for row in conn.execute(sql, args).fetchall()]

    # ------------------------------------------------------------ 收件箱
    def add_notification(self, event: dict, title: str, body: str, tone: str, link: dict, group_key: str, now: int) -> dict | None:
        notif_id = ulid(now)
        with self._conn() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO notification (notif_id, event_id, title, body, tone, link, group_key, severity, type, subject_id, created_at, user_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (notif_id, event["event_id"], title, body, tone, _dumps(link), group_key, event["severity"], event["type"], event["subject"]["id"], now, self.user_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.notification(notif_id)

    def notification(self, notif_id: str) -> dict | None:
        with self._conn() as conn:
            return self._row(conn.execute(
                "SELECT * FROM notification WHERE notif_id=? AND user_id=?", (notif_id, self.user_id)
            ).fetchone())

    def inbox(self, unread_only: bool = False, limit: int = 50, offset: int = 0, group: str | None = None, include_archived: bool = False) -> list[dict]:
        sql, args = "SELECT * FROM notification WHERE user_id=?", [self.user_id]
        if unread_only:
            sql += " AND read_at IS NULL"
        if group:
            sql += " AND group_key=?"
            args.append(group)
        if not include_archived:
            sql += " AND archived_at IS NULL"
        sql += " ORDER BY created_at DESC, notif_id DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self._conn() as conn:
            return [self._row(row) for row in conn.execute(sql, args).fetchall()]

    def unread_count(self) -> int:
        with self._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM notification WHERE user_id=? AND read_at IS NULL AND archived_at IS NULL",
                (self.user_id,),
            ).fetchone()[0]

    def mark_read(self, ids: list[str] | None, now: int) -> int:
        with self._conn() as conn:
            if ids is None:
                return conn.execute(
                    "UPDATE notification SET read_at=? WHERE user_id=? AND read_at IS NULL", (now, self.user_id)
                ).rowcount
            return sum(
                conn.execute(
                    "UPDATE notification SET read_at=? WHERE notif_id=? AND user_id=? AND read_at IS NULL",
                    (now, i, self.user_id),
                ).rowcount
                for i in ids
            )

    def archive(self, ids: list[str], now: int) -> int:
        with self._conn() as conn:
            return sum(
                conn.execute(
                    "UPDATE notification SET archived_at=? WHERE notif_id=? AND user_id=?", (now, i, self.user_id)
                ).rowcount
                for i in ids
            )

    def recent_similar(self, type_: str, subject_id: str, since: int, exclude_event: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM notification WHERE user_id=? AND type=? AND subject_id=? AND created_at>=? AND event_id<>? AND digest_id IS NULL LIMIT 1",
                (self.user_id, type_, subject_id, since, exclude_event),
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------ 渠道
    def put_channel(self, channel: dict) -> dict:
        existing = self.channel(channel["channel_id"]) or {}
        merged = {"min_severity": "normal", "quiet_hours": {"from": "22:30", "to": "08:00"}, "rate_limit": {"per_min": 10, "per_day": 100},
                  "config": {}, "relay": 0, "enabled": 1, "secret_ref": None, "verified_at": None, **existing, **channel}
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO channel (user_id, channel_id, type, name, secret_ref, config, min_severity, quiet_hours, rate_limit, relay, enabled, verified_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id, channel_id) DO UPDATE SET type=excluded.type, name=excluded.name,"
                " secret_ref=excluded.secret_ref, config=excluded.config, min_severity=excluded.min_severity, quiet_hours=excluded.quiet_hours,"
                " rate_limit=excluded.rate_limit, relay=excluded.relay, enabled=excluded.enabled, verified_at=excluded.verified_at",
                (self.user_id, merged["channel_id"], merged["type"], merged["name"], merged["secret_ref"], _dumps(merged["config"]), merged["min_severity"],
                 _dumps(merged["quiet_hours"]), _dumps(merged["rate_limit"]), int(bool(merged["relay"])), int(bool(merged["enabled"])), merged["verified_at"]),
            )
        return self.channel(merged["channel_id"])

    def channel(self, channel_id: str) -> dict | None:
        with self._conn() as conn:
            return self._row(conn.execute(
                "SELECT * FROM channel WHERE user_id=? AND channel_id=?", (self.user_id, channel_id)
            ).fetchone())

    def channels(self, enabled_only: bool = False) -> list[dict]:
        sql, args = "SELECT * FROM channel WHERE user_id=?", [self.user_id]
        if enabled_only:
            sql += " AND enabled=1"
        sql += " ORDER BY channel_id"
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._row(row) for row in rows]

    def delete_channel(self, channel_id: str) -> bool:
        with self._conn() as conn:
            return conn.execute(
                "DELETE FROM channel WHERE user_id=? AND channel_id=?", (self.user_id, channel_id)
            ).rowcount > 0

    # ------------------------------------------------------------ 投递
    def add_delivery(self, notif_id: str, channel_id: str, state: str, deliver_after: int, expires_at: int | None, reason: str | None = None) -> dict | None:
        delivery_id = ulid(deliver_after)
        with self._conn() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO delivery (delivery_id, notif_id, channel_id, state, deliver_after, next_attempt_at, suppressed_reason, expires_at, user_id)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (delivery_id, notif_id, channel_id, state, deliver_after, deliver_after if state == "pending" else None, reason, expires_at, self.user_id),
            )
            if cursor.rowcount == 0:
                return None
            return self._row(conn.execute(
                "SELECT * FROM delivery WHERE delivery_id=? AND user_id=?", (delivery_id, self.user_id)
            ).fetchone())

    def delivery(self, delivery_id: str) -> dict | None:
        with self._conn() as conn:
            return self._row(conn.execute(
                "SELECT * FROM delivery WHERE delivery_id=? AND user_id=?", (delivery_id, self.user_id)
            ).fetchone())

    def deliveries(self, notif_id: str | None = None, channel_id: str | None = None, state: str | None = None, limit: int = 200) -> list[dict]:
        sql, args = "SELECT * FROM delivery WHERE user_id=?", [self.user_id]
        for column, value in (("notif_id", notif_id), ("channel_id", channel_id), ("state", state)):
            if value:
                sql += f" AND {column}=?"
                args.append(value)
        sql += " ORDER BY deliver_after DESC LIMIT ?"
        args.append(limit)
        with self._conn() as conn:
            return [self._row(row) for row in conn.execute(sql, args).fetchall()]

    def due_deliveries(self, now: int, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM delivery WHERE user_id=? AND state='pending' AND deliver_after<=? AND COALESCE(next_attempt_at, deliver_after)<=?"
                " ORDER BY deliver_after LIMIT ?",
                (self.user_id, now, now, limit),
            ).fetchall()
        return [self._row(row) for row in rows]

    def update_delivery(self, delivery_id: str, **fields) -> None:
        sets = ", ".join(f"{key}=?" for key in fields)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE delivery SET {sets} WHERE delivery_id=? AND user_id=?",
                (*fields.values(), delivery_id, self.user_id),
            )

    def sent_count(self, channel_id: str, since: int) -> int:
        with self._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM delivery WHERE user_id=? AND channel_id=? AND state='sent' AND sent_at>=?",
                (self.user_id, channel_id, since),
            ).fetchone()[0]

    # ------------------------------------------------------------ 规则
    def put_rule(self, rule: dict, now: int) -> dict:
        rule_id = rule.get("rule_id") or "rule_" + ulid(now)
        existing = self.rule(rule_id)
        merged = {"mode": "edge", "rearm": {"kind": "hysteresis", "pct": 1.0}, "cooldown_s": 0, "routing": {}, "origin": "user",
                  "origin_ref": None, "enabled": 1, "params": {}, "v": 1, **(existing or {}), **rule, "rule_id": rule_id}
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO rule (rule_id, type, subject_kind, subject_id, params, mode, rearm, cooldown_s, routing, origin, origin_ref, enabled, created_at, updated_at, v, user_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(rule_id) DO UPDATE SET type=excluded.type, subject_kind=excluded.subject_kind,"
                " subject_id=excluded.subject_id, params=excluded.params, mode=excluded.mode, rearm=excluded.rearm, cooldown_s=excluded.cooldown_s,"
                " routing=excluded.routing, origin=excluded.origin, origin_ref=excluded.origin_ref, enabled=excluded.enabled, updated_at=excluded.updated_at,"
                " user_id=excluded.user_id",
                (rule_id, merged["type"], merged["subject_kind"], merged["subject_id"], _dumps(merged["params"]), merged["mode"], _dumps(merged["rearm"]),
                 merged["cooldown_s"], _dumps(merged["routing"]), merged["origin"], merged["origin_ref"], int(bool(merged["enabled"])),
                 (existing or {}).get("created_at", now), now, merged["v"], self.user_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO rule_state (rule_id, user_id) VALUES (?,?)", (rule_id, self.user_id)
            )
        return self.rule(rule_id)

    def rule(self, rule_id: str) -> dict | None:
        with self._conn() as conn:
            return self._row(conn.execute(
                "SELECT * FROM rule WHERE rule_id=? AND user_id=?", (rule_id, self.user_id)
            ).fetchone())

    def rules(self, type_: str | None = None, subject_id: str | None = None, origin: str | None = None, enabled_only: bool = False) -> list[dict]:
        sql, args = "SELECT * FROM rule WHERE user_id=?", [self.user_id]
        for column, value in (("type", type_), ("subject_id", subject_id), ("origin", origin)):
            if value:
                sql += f" AND {column}=?"
                args.append(value)
        if enabled_only:
            sql += " AND enabled=1"
        with self._conn() as conn:
            return [self._row(row) for row in conn.execute(sql + " ORDER BY created_at, rule_id", args).fetchall()]

    def delete_rule(self, rule_id: str) -> bool:
        with self._conn() as conn:
            conn.execute("DELETE FROM rule_state WHERE rule_id=? AND user_id=?", (rule_id, self.user_id))
            return conn.execute("DELETE FROM rule WHERE rule_id=? AND user_id=?", (rule_id, self.user_id)).rowcount > 0

    def rule_state(self, rule_id: str) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM rule_state WHERE rule_id=? AND user_id=?", (rule_id, self.user_id)
            ).fetchone()
        out = self._row(row) or {"rule_id": rule_id, "state": "armed", "last_value": None, "last_eval_at": None, "fired_at": None, "rearm_at": None, "fire_seq": 0, "cursor": None}
        if isinstance(out.get("last_value"), str):
            try:
                out["last_value"] = json.loads(out["last_value"])
            except ValueError:
                pass
        return out

    def put_rule_state(self, rule_id: str, **fields) -> None:
        if isinstance(fields.get("last_value"), (dict, list)):
            fields["last_value"] = _dumps(fields["last_value"])
        with self._conn() as conn:
            conn.execute("INSERT OR IGNORE INTO rule_state (rule_id, user_id) VALUES (?,?)", (rule_id, self.user_id))
            if fields:
                conn.execute(
                    f"UPDATE rule_state SET {', '.join(f'{k}=?' for k in fields)} WHERE rule_id=? AND user_id=?",
                    (*fields.values(), rule_id, self.user_id),
                )

    # ------------------------------------------------------------ 设置
    def settings(self) -> dict:
        out = json.loads(json.dumps(DEFAULT_SETTINGS))
        with self._conn() as conn:
            for row in conn.execute("SELECT key, value FROM setting WHERE user_id=?", (self.user_id,)).fetchall():
                out[row["key"]] = json.loads(row["value"])
        return out

    def set_settings(self, values: dict) -> dict:
        with self._conn() as conn:
            for key, value in values.items():
                if key in DEFAULT_SETTINGS:
                    conn.execute(
                        "INSERT INTO setting (user_id, key, value) VALUES (?,?,?) ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
                        (self.user_id, key, _dumps(value)),
                    )
        return self.settings()

    # ------------------------------------------------------------ 清理
    def purge(self, now: int) -> dict:
        event_cut, inbox_cut = now - 180 * 86400_000, now - 90 * 86400_000
        with self._conn() as conn:
            deliveries = conn.execute(
                "DELETE FROM delivery WHERE user_id=? AND state IN ('sent','dead','suppressed') AND COALESCE(sent_at, deliver_after)<?",
                (self.user_id, inbox_cut),
            ).rowcount
            notifs = conn.execute(
                "DELETE FROM notification WHERE user_id=? AND created_at<?", (self.user_id, inbox_cut)
            ).rowcount
            events = conn.execute(
                "DELETE FROM event WHERE user_id=? AND occurred_at<? AND event_id NOT IN"
                " (SELECT event_id FROM notification WHERE user_id=?)",
                (self.user_id, event_cut, self.user_id),
            ).rowcount
        return {"events": events, "notifications": notifs, "deliveries": deliveries}


_lock = threading.Lock()
_store: NotifyStore | None = None


def get_base_store() -> NotifyStore:
    global _store
    with _lock:
        if _store is None:
            _store = NotifyStore(user_id=LEGACY_USER)
        return _store


def get_store(user_id: str | None = None) -> NotifyStore:
    base = get_base_store()
    if user_id is not None:
        return base.for_user(user_id)
    from app.auth.user import current_user_id

    return base.for_user(current_user_id() or LEGACY_USER)


def set_store(store: NotifyStore | None) -> None:
    global _store
    with _lock:
        _store = store
