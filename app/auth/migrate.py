"""给个人表补 user_id 列，旧行回填 default。"""

from __future__ import annotations

import sqlite3

LEGACY_USER = "default"


def table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_user_id_column(conn: sqlite3.Connection, table: str, backfill: str = LEGACY_USER) -> None:
    cols = table_cols(conn, table)
    if not cols:
        return
    if "user_id" not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT NOT NULL DEFAULT '{backfill}'")
    conn.execute(f"UPDATE {table} SET user_id=? WHERE user_id IS NULL OR user_id=''", (backfill,))


def _pk_cols(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})") if row[5]]


def rebuild_reasons(conn: sqlite3.Connection, backfill: str = LEGACY_USER) -> None:
    """reasons 原主键是 ts_code，多租户需 (user_id, ts_code)。"""
    if not table_cols(conn, "reasons"):
        return
    if _pk_cols(conn, "reasons") == ["user_id", "ts_code"]:
        return
    ensure_user_id_column(conn, "reasons", backfill)
    conn.executescript(
        f"""
        CREATE TABLE reasons_uid (
          user_id TEXT NOT NULL,
          ts_code TEXT NOT NULL,
          text TEXT NOT NULL,
          invalid TEXT NOT NULL DEFAULT '[]',
          written_at TEXT NOT NULL,
          updated_at REAL NOT NULL,
          PRIMARY KEY (user_id, ts_code)
        );
        INSERT OR IGNORE INTO reasons_uid (user_id, ts_code, text, invalid, written_at, updated_at)
        SELECT COALESCE(NULLIF(user_id,''), '{backfill}'), ts_code, text, invalid, written_at, updated_at FROM reasons;
        DROP TABLE reasons;
        ALTER TABLE reasons_uid RENAME TO reasons;
        """
    )


def rebuild_event_dedupe(conn: sqlite3.Connection, backfill: str = LEGACY_USER) -> None:
    """event.dedupe_key 全局唯一 → (user_id, dedupe_key)。"""
    if not table_cols(conn, "event"):
        return
    ensure_user_id_column(conn, "event", backfill)
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    if "event_user_dedupe" in indexes and "user_id" in table_cols(conn, "event"):
        # 可能仍带着旧的 dedupe_key UNIQUE；检查 sqlite_master sql
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='event'").fetchone()
        sql = (row[0] or "") if row else ""
        if "UNIQUE (user_id, dedupe_key)" in sql or "UNIQUE(user_id, dedupe_key)" in sql:
            return
    conn.executescript(
        f"""
        CREATE TABLE event_uid (
          event_id TEXT PRIMARY KEY, type TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL,
          occurred_at INTEGER NOT NULL, trade_date TEXT, severity TEXT NOT NULL, dedupe_key TEXT NOT NULL,
          source TEXT NOT NULL DEFAULT '{{}}', correlation_id TEXT, ttl_s INTEGER, schema_v INTEGER NOT NULL DEFAULT 1,
          payload TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '{backfill}',
          UNIQUE (user_id, dedupe_key)
        );
        INSERT OR IGNORE INTO event_uid (
          event_id, type, subject_kind, subject_id, occurred_at, trade_date, severity, dedupe_key,
          source, correlation_id, ttl_s, schema_v, payload, user_id
        )
        SELECT event_id, type, subject_kind, subject_id, occurred_at, trade_date, severity, dedupe_key,
          source, correlation_id, ttl_s, schema_v, payload, COALESCE(NULLIF(user_id,''), '{backfill}')
        FROM event;
        DROP TABLE event;
        ALTER TABLE event_uid RENAME TO event;
        CREATE INDEX IF NOT EXISTS event_by_subject ON event (user_id, subject_kind, subject_id, occurred_at DESC);
        CREATE INDEX IF NOT EXISTS event_by_type ON event (user_id, type, occurred_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS event_user_dedupe ON event (user_id, dedupe_key);
        """
    )


def rebuild_kv(conn: sqlite3.Connection, table: str = "kv", backfill: str = LEGACY_USER) -> None:
    """kv.key 全局主键 → (user_id, key)。"""
    if not table_cols(conn, table):
        return
    if _pk_cols(conn, table) == ["user_id", "key"]:
        return
    ensure_user_id_column(conn, table, backfill)
    conn.executescript(
        f"""
        CREATE TABLE {table}_uid (
          user_id TEXT NOT NULL,
          key TEXT NOT NULL,
          value TEXT NOT NULL,
          PRIMARY KEY (user_id, key)
        );
        INSERT OR IGNORE INTO {table}_uid (user_id, key, value)
        SELECT COALESCE(NULLIF(user_id,''), '{backfill}'), key, value FROM {table};
        DROP TABLE {table};
        ALTER TABLE {table}_uid RENAME TO {table};
        """
    )


def rebuild_watchlist(conn: sqlite3.Connection, backfill: str = LEGACY_USER) -> None:
    if not table_cols(conn, "watchlist"):
        return
    if _pk_cols(conn, "watchlist") == ["user_id", "ts_code"]:
        return
    ensure_user_id_column(conn, "watchlist", backfill)
    cols = table_cols(conn, "watchlist")
    sort = "sort_order" if "sort_order" in cols else "added_at"
    conn.executescript(
        f"""
        CREATE TABLE watchlist_uid (
          user_id TEXT NOT NULL,
          ts_code TEXT NOT NULL,
          name TEXT NOT NULL,
          added_at REAL NOT NULL,
          sort_order REAL NOT NULL DEFAULT 0,
          PRIMARY KEY (user_id, ts_code)
        );
        INSERT OR IGNORE INTO watchlist_uid (user_id, ts_code, name, added_at, sort_order)
        SELECT COALESCE(NULLIF(user_id,''), '{backfill}'), ts_code, name, added_at, {sort} FROM watchlist;
        DROP TABLE watchlist;
        ALTER TABLE watchlist_uid RENAME TO watchlist;
        """
    )


def rebuild_setting(conn: sqlite3.Connection, backfill: str = LEGACY_USER) -> None:
    if not table_cols(conn, "setting"):
        return
    if _pk_cols(conn, "setting") == ["user_id", "key"]:
        return
    ensure_user_id_column(conn, "setting", backfill)
    conn.executescript(
        f"""
        CREATE TABLE setting_uid (
          user_id TEXT NOT NULL,
          key TEXT NOT NULL,
          value TEXT NOT NULL,
          PRIMARY KEY (user_id, key)
        );
        INSERT OR IGNORE INTO setting_uid (user_id, key, value)
        SELECT COALESCE(NULLIF(user_id,''), '{backfill}'), key, value FROM setting;
        DROP TABLE setting;
        ALTER TABLE setting_uid RENAME TO setting;
        """
    )


def rebuild_channel(conn: sqlite3.Connection, backfill: str = LEGACY_USER) -> None:
    """channel.channel_id 主键 → (user_id, channel_id)，多用户可共用 ch_desktop。"""
    if not table_cols(conn, "channel"):
        return
    if _pk_cols(conn, "channel") == ["user_id", "channel_id"]:
        return
    ensure_user_id_column(conn, "channel", backfill)
    conn.executescript(
        f"""
        CREATE TABLE channel_uid (
          user_id TEXT NOT NULL,
          channel_id TEXT NOT NULL,
          type TEXT NOT NULL, name TEXT NOT NULL, secret_ref TEXT, config TEXT NOT NULL DEFAULT '{{}}',
          min_severity TEXT NOT NULL DEFAULT 'normal', quiet_hours TEXT NOT NULL DEFAULT '{{"from":"22:30","to":"08:00"}}',
          rate_limit TEXT NOT NULL DEFAULT '{{"per_min":10,"per_day":100}}', relay INTEGER NOT NULL DEFAULT 0,
          enabled INTEGER NOT NULL DEFAULT 1, verified_at INTEGER,
          PRIMARY KEY (user_id, channel_id)
        );
        INSERT OR IGNORE INTO channel_uid (
          user_id, channel_id, type, name, secret_ref, config, min_severity, quiet_hours, rate_limit, relay, enabled, verified_at
        )
        SELECT COALESCE(NULLIF(user_id,''), '{backfill}'), channel_id, type, name, secret_ref, config,
          min_severity, quiet_hours, rate_limit, relay, enabled, verified_at FROM channel;
        DROP TABLE channel;
        ALTER TABLE channel_uid RENAME TO channel;
        """
    )


def rebuild_notification(conn: sqlite3.Connection, backfill: str = LEGACY_USER) -> None:
    """notification.event_id 全局 UNIQUE → UNIQUE(user_id, event_id)。"""
    if not table_cols(conn, "notification"):
        return
    ensure_user_id_column(conn, "notification", backfill)
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='notification'").fetchone()
    sql = (row[0] or "") if row else ""
    if "UNIQUE (user_id, event_id)" in sql or "UNIQUE(user_id, event_id)" in sql:
        return
    conn.executescript(
        f"""
        CREATE TABLE notification_uid (
          notif_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, tone TEXT NOT NULL,
          link TEXT NOT NULL DEFAULT '{{}}', group_key TEXT, digest_id TEXT, severity TEXT NOT NULL DEFAULT 'normal',
          type TEXT NOT NULL DEFAULT '', subject_id TEXT NOT NULL DEFAULT '',
          created_at INTEGER NOT NULL, read_at INTEGER, archived_at INTEGER,
          user_id TEXT NOT NULL DEFAULT '{backfill}',
          UNIQUE (user_id, event_id)
        );
        INSERT OR IGNORE INTO notification_uid (
          notif_id, event_id, title, body, tone, link, group_key, digest_id, severity, type, subject_id,
          created_at, read_at, archived_at, user_id
        )
        SELECT notif_id, event_id, title, body, tone, link, group_key, digest_id, severity, type, subject_id,
          created_at, read_at, archived_at, COALESCE(NULLIF(user_id,''), '{backfill}')
        FROM notification;
        DROP TABLE notification;
        ALTER TABLE notification_uid RENAME TO notification;
        CREATE INDEX IF NOT EXISTS notif_unread ON notification (user_id, read_at, created_at DESC);
        """
    )


def rebuild_market_event(conn: sqlite3.Connection, backfill: str = LEGACY_USER) -> None:
    """market.event.key 全局 UNIQUE → UNIQUE(user_id, key)。"""
    if not table_cols(conn, "event"):
        return
    cols = table_cols(conn, "event")
    if "key" not in cols or "kind" not in cols:
        return  # notify.event 不是这张表
    ensure_user_id_column(conn, "event", backfill)
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='event'").fetchone()
    sql = (row[0] or "") if row else ""
    if "UNIQUE (user_id, key)" in sql or "UNIQUE(user_id, key)" in sql:
        return
    conn.executescript(
        f"""
        CREATE TABLE event_uid (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id TEXT NOT NULL DEFAULT '{backfill}',
          key TEXT NOT NULL,
          day TEXT NOT NULL,
          created_at REAL NOT NULL,
          kind TEXT NOT NULL,
          title TEXT NOT NULL,
          text TEXT NOT NULL,
          tab TEXT NOT NULL DEFAULT '',
          tone TEXT NOT NULL DEFAULT 'flat',
          read INTEGER NOT NULL DEFAULT 0,
          UNIQUE (user_id, key)
        );
        INSERT OR IGNORE INTO event_uid (id, user_id, key, day, created_at, kind, title, text, tab, tone, read)
        SELECT id, COALESCE(NULLIF(user_id,''), '{backfill}'), key, day, created_at, kind, title, text, tab, tone, read
        FROM event;
        DROP TABLE event;
        ALTER TABLE event_uid RENAME TO event;
        CREATE INDEX IF NOT EXISTS event_by_id ON event (user_id, id DESC);
        """
    )


def rebuild_jobs(conn: sqlite3.Connection, backfill: str = LEGACY_USER) -> None:
    """jobs.ts_code 主键 → (user_id, ts_code)。"""
    if not table_cols(conn, "jobs"):
        return
    if _pk_cols(conn, "jobs") == ["user_id", "ts_code"]:
        return
    ensure_user_id_column(conn, "jobs", backfill)
    conn.executescript(
        f"""
        CREATE TABLE jobs_uid (
          user_id TEXT NOT NULL,
          ts_code TEXT NOT NULL,
          due_at REAL NOT NULL,
          first_at REAL NOT NULL,
          reason TEXT NOT NULL DEFAULT '',
          attempts INTEGER NOT NULL DEFAULT 0,
          running INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (user_id, ts_code)
        );
        INSERT OR IGNORE INTO jobs_uid (user_id, ts_code, due_at, first_at, reason, attempts, running)
        SELECT COALESCE(NULLIF(user_id,''), '{backfill}'), ts_code, due_at, first_at, reason, attempts, running FROM jobs;
        DROP TABLE jobs;
        ALTER TABLE jobs_uid RENAME TO jobs;
        """
    )
