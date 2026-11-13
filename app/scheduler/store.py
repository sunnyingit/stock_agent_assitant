"""调度记录的存储：SQLite，表结构见设计文档 3.4。写法兼容 Postgres（服务端以后换库）。

一个库一把锁，和现有的几个 SQLite 存储一样；标准库，不加依赖。
"""

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

_DEFAULT_PATH = Path(os.environ.get("SCHED_DB") or (data_dir() / "scheduler.db"))
_RESULT_DIR = data_dir() / "results"
INLINE_LIMIT = 8 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_run (
  run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, period_key TEXT NOT NULL, trigger TEXT NOT NULL,
  scheduled_for INTEGER NOT NULL, state TEXT NOT NULL, counts TEXT NOT NULL DEFAULT '{}',
  started_at INTEGER, finished_at INTEGER, note TEXT NOT NULL DEFAULT '',
  config TEXT NOT NULL DEFAULT '{}', code_version TEXT NOT NULL DEFAULT '',
  UNIQUE (task_id, period_key)
);
CREATE TABLE IF NOT EXISTS job (
  job_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT NOT NULL, unit_key TEXT NOT NULL, period_key TEXT NOT NULL,
  queue TEXT NOT NULL, state TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 50,
  attempt INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
  due_at INTEGER NOT NULL, expire_at INTEGER, lease_owner TEXT, lease_until INTEGER, coalesce_key TEXT,
  input TEXT NOT NULL DEFAULT '{}', output TEXT NOT NULL DEFAULT '{}', error TEXT,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  UNIQUE (task_id, unit_key, period_key)
);
CREATE INDEX IF NOT EXISTS job_claim ON job (queue, state, due_at, priority);
CREATE INDEX IF NOT EXISTS job_by_run ON job (run_id);
CREATE TABLE IF NOT EXISTS job_step (
  job_id TEXT NOT NULL, step TEXT NOT NULL, state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
  output_ref TEXT, tokens INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL,
  PRIMARY KEY (job_id, step)
);
CREATE TABLE IF NOT EXISTS job_attempt (
  job_id TEXT NOT NULL, attempt INTEGER NOT NULL, lease_owner TEXT,
  started_at INTEGER NOT NULL, ended_at INTEGER, state TEXT NOT NULL, error TEXT,
  tokens INTEGER NOT NULL DEFAULT 0, calls INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (job_id, attempt)
);
CREATE TABLE IF NOT EXISTS result (
  ref TEXT PRIMARY KEY, owner_job TEXT, step TEXT, kind TEXT NOT NULL, content_type TEXT NOT NULL,
  size INTEGER NOT NULL, sha256 TEXT NOT NULL, inline TEXT, path TEXT,
  created_at INTEGER NOT NULL, expires_at INTEGER
);
CREATE TABLE IF NOT EXISTS budget_ledger (
  resource TEXT NOT NULL, window TEXT NOT NULL, used TEXT NOT NULL DEFAULT '{}', PRIMARY KEY (resource, window)
);
CREATE TABLE IF NOT EXISTS task_override (
  task_id TEXT PRIMARY KEY, enabled INTEGER, trigger TEXT, params TEXT, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sched_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL);
"""

CLAIMABLE = ("pending", "retrying", "not_ready")
TERMINAL = ("succeeded", "dead", "skipped", "cancelled", "expired")


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class SchedStore:
    def __init__(self, path: str | os.PathLike | None = None, result_dir: Path | None = None):
        self.path = str(path or os.environ.get("SCHEDULER_DB") or _DEFAULT_PATH)
        self.result_dir = result_dir or _RESULT_DIR
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

    @staticmethod
    def _row(row) -> dict | None:
        if row is None:
            return None
        out = dict(row)
        for key in ("counts", "config", "input", "output", "error", "used", "trigger", "params"):
            if key in out and isinstance(out[key], str) and out[key][:1] in "{[":
                try:
                    out[key] = json.loads(out[key])
                except ValueError:
                    pass
        return out

    # ------------------------------------------------------------ 覆盖项
    def override(self, task_id: str) -> dict | None:
        with self._conn() as conn:
            return self._row(conn.execute("SELECT * FROM task_override WHERE task_id=?", (task_id,)).fetchone())

    def set_override(self, task_id: str, now: int, enabled: bool | None = None, trigger: dict | None = None, params: dict | None = None) -> dict:
        current = self.override(task_id) or {}
        merged_enabled = current.get("enabled") if enabled is None else int(enabled)
        merged_trigger = current.get("trigger") if trigger is None else trigger
        merged_params = current.get("params") if params is None else params
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO task_override (task_id, enabled, trigger, params, updated_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(task_id) DO UPDATE SET enabled=excluded.enabled, trigger=excluded.trigger,"
                " params=excluded.params, updated_at=excluded.updated_at",
                (task_id, merged_enabled, None if merged_trigger is None else _dumps(merged_trigger),
                 None if merged_params is None else _dumps(merged_params), now),
            )
        return self.override(task_id)

    def clear_override(self, task_id: str) -> bool:
        with self._conn() as conn:
            return conn.execute("DELETE FROM task_override WHERE task_id=?", (task_id,)).rowcount > 0

    # ------------------------------------------------------------ 运行
    def create_run(self, task_id: str, period_key: str, trigger: str, scheduled_for: int, state: str, config: dict,
                   code_version: str = "", note: str = "", now: int = 0) -> dict | None:
        """(task_id, period_key) 唯一；已经有就返回 None（重复 tick 无害）。"""
        run_id = ulid(now or None)
        with self._conn() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO task_run (run_id, task_id, period_key, trigger, scheduled_for, state, config, code_version, note)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, task_id, period_key, trigger, scheduled_for, state, _dumps(config), code_version, note),
            )
            if cursor.rowcount == 0:
                return None
        return self.run(run_id)

    def run(self, run_id: str) -> dict | None:
        with self._conn() as conn:
            return self._row(conn.execute("SELECT * FROM task_run WHERE run_id=?", (run_id,)).fetchone())

    def run_by_period(self, task_id: str, period_key: str) -> dict | None:
        with self._conn() as conn:
            return self._row(conn.execute("SELECT * FROM task_run WHERE task_id=? AND period_key=?", (task_id, period_key)).fetchone())

    def update_run(self, run_id: str, **fields) -> None:
        if not fields:
            return
        sets, args = [], []
        for key, value in fields.items():
            sets.append(f"{key}=?")
            args.append(_dumps(value) if isinstance(value, (dict, list)) else value)
        with self._conn() as conn:
            conn.execute(f"UPDATE task_run SET {', '.join(sets)} WHERE run_id=?", (*args, run_id))

    def runs(self, task_id: str | None = None, states: tuple | None = None, active: bool = False, limit: int = 50,
             since: int | None = None, until: int | None = None) -> list[dict]:
        sql, args = "SELECT * FROM task_run WHERE 1=1", []
        if task_id:
            sql += " AND task_id=?"
            args.append(task_id)
        if states:
            sql += f" AND state IN ({','.join('?' * len(states))})"
            args.extend(states)
        if active:
            sql += " AND state IN ('planned','waiting','running')"
        if since is not None:
            sql += " AND scheduled_for>=?"
            args.append(since)
        if until is not None:
            sql += " AND scheduled_for<=?"
            args.append(until)
        sql += " ORDER BY scheduled_for DESC, run_id DESC LIMIT ?"
        args.append(limit)
        with self._conn() as conn:
            return [self._row(row) for row in conn.execute(sql, args).fetchall()]

    def last_run(self, task_id: str) -> dict | None:
        rows = self.runs(task_id, limit=1)
        return rows[0] if rows else None

    # ------------------------------------------------------------ 单元
    def add_jobs(self, run_id: str, task_id: str, period_key: str, units: list[dict], queue: str, due_at: int,
                 max_attempts: int, now: int, expire_at: int | None = None, coalesce_key: str | None = None) -> int:
        """批量插入；(task_id, unit_key, period_key) 唯一，已有的忽略。返回新增个数。"""
        added = 0
        with self._conn() as conn:
            for unit in units:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO job (job_id, run_id, task_id, unit_key, period_key, queue, state, priority, max_attempts,"
                    " due_at, expire_at, coalesce_key, input, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ulid(now), run_id, task_id, unit["unit_key"], period_key, queue, "pending", unit.get("priority", 50), max_attempts,
                     due_at, expire_at, coalesce_key, _dumps(unit.get("input") or {}), now, now),
                )
                added += cursor.rowcount
        return added

    def job(self, job_id: str) -> dict | None:
        with self._conn() as conn:
            return self._row(conn.execute("SELECT * FROM job WHERE job_id=?", (job_id,)).fetchone())

    def jobs_of(self, run_id: str, state: str | None = None, unit_key: str | None = None, limit: int = 500) -> list[dict]:
        sql, args = "SELECT * FROM job WHERE run_id=?", [run_id]
        if state:
            sql += " AND state=?"
            args.append(state)
        if unit_key:
            sql += " AND unit_key=?"
            args.append(unit_key)
        sql += " ORDER BY priority DESC, created_at LIMIT ?"
        args.append(limit)
        with self._conn() as conn:
            return [self._row(row) for row in conn.execute(sql, args).fetchall()]

    def job_counts(self, run_id: str) -> dict:
        with self._conn() as conn:
            rows = conn.execute("SELECT state, COUNT(*) AS n FROM job WHERE run_id=? GROUP BY state", (run_id,)).fetchall()
        counts = {row["state"]: row["n"] for row in rows}
        counts["jobs"] = sum(counts.values())
        return counts

    def claim(self, queue: str, now: int, owner: str, lease_ms: int, limit: int = 1) -> list[dict]:
        """领到期的单元。优先级高的先，同优先级先到期的先。过了 expire_at 的当场标成 expired，不领。"""
        out = []
        with self._conn() as conn:
            conn.execute(
                "UPDATE job SET state='expired', updated_at=?, error=? WHERE queue=? AND state IN ('pending','retrying','not_ready')"
                " AND expire_at IS NOT NULL AND expire_at<=?",
                (now, _dumps({"class": "expired", "code": "EXPIRED", "message": "过了有效期没有执行"}), queue, now),
            )
            rows = conn.execute(
                "SELECT job_id FROM job WHERE queue=? AND state IN ('pending','retrying','not_ready') AND due_at<=?"
                " ORDER BY priority DESC, due_at, created_at LIMIT ?",
                (queue, now, limit),
            ).fetchall()
            for row in rows:
                cursor = conn.execute(
                    "UPDATE job SET state='leased', lease_owner=?, lease_until=?, updated_at=? WHERE job_id=? AND state IN ('pending','retrying','not_ready')",
                    (owner, now + lease_ms, now, row["job_id"]),
                )
                if cursor.rowcount:
                    out.append(self._row(conn.execute("SELECT * FROM job WHERE job_id=?", (row["job_id"],)).fetchone()))
        return out

    def update_job(self, job_id: str, now: int, **fields) -> None:
        sets, args = ["updated_at=?"], [now]
        for key, value in fields.items():
            sets.append(f"{key}=?")
            args.append(_dumps(value) if isinstance(value, (dict, list)) else value)
        with self._conn() as conn:
            conn.execute(f"UPDATE job SET {', '.join(sets)} WHERE job_id=?", (*args, job_id))

    def reclaim(self, now: int) -> int:
        """租约过期还没收尾的单元（进程被杀、电脑休眠）回到待办。尝试次数不加，但留一条 abandoned 的尝试记录。"""
        with self._conn() as conn:
            rows = conn.execute("SELECT job_id, attempt FROM job WHERE state IN ('leased','running') AND lease_until<=?", (now,)).fetchall()
            for row in rows:
                conn.execute("UPDATE job_attempt SET state='abandoned', ended_at=? WHERE job_id=? AND attempt=? AND state='running'",
                             (now, row["job_id"], row["attempt"]))
                conn.execute("UPDATE job SET state='pending', lease_owner=NULL, lease_until=NULL, updated_at=? WHERE job_id=?", (now, row["job_id"]))
            return len(rows)

    def cancel_pending(self, run_id: str, now: int, state: str = "cancelled") -> int:
        with self._conn() as conn:
            return conn.execute(
                "UPDATE job SET state=?, updated_at=? WHERE run_id=? AND state IN ('pending','retrying','not_ready')", (state, now, run_id),
            ).rowcount

    def coalesce(self, task_id: str, key: str, now: int, debounce_ms: int, max_wait_ms: int) -> dict | None:
        """事件触发的合并：同一个键、还没开始的单元，把到期时间往后推（最长不超过首次时间加 max_wait）。返回被合并的单元。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM job WHERE task_id=? AND coalesce_key=? AND state='pending' ORDER BY created_at DESC LIMIT 1", (task_id, key),
            ).fetchone()
            if row is None:
                return None
            due = min(now + debounce_ms, row["created_at"] + max_wait_ms)
            conn.execute("UPDATE job SET due_at=MAX(due_at, ?), updated_at=? WHERE job_id=?", (due, now, row["job_id"]))
            return self._row(conn.execute("SELECT * FROM job WHERE job_id=?", (row["job_id"],)).fetchone())

    # ------------------------------------------------------------ 尝试和步骤
    def begin_attempt(self, job_id: str, attempt: int, owner: str, now: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO job_attempt (job_id, attempt, lease_owner, started_at, state) VALUES (?,?,?,?, 'running')",
                (job_id, attempt, owner, now),
            )

    def end_attempt(self, job_id: str, attempt: int, now: int, state: str, error: dict | None = None, tokens: int = 0, calls: int = 0) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE job_attempt SET ended_at=?, state=?, error=?, tokens=?, calls=? WHERE job_id=? AND attempt=?",
                (now, state, None if error is None else _dumps(error), tokens, calls, job_id, attempt),
            )

    def attempts(self, job_id: str) -> list[dict]:
        with self._conn() as conn:
            return [self._row(row) for row in conn.execute("SELECT * FROM job_attempt WHERE job_id=? ORDER BY attempt", (job_id,)).fetchall()]

    def step(self, job_id: str, step: str) -> dict | None:
        with self._conn() as conn:
            return self._row(conn.execute("SELECT * FROM job_step WHERE job_id=? AND step=?", (job_id, step)).fetchone())

    def steps(self, job_id: str) -> list[dict]:
        with self._conn() as conn:
            return [self._row(row) for row in conn.execute("SELECT * FROM job_step WHERE job_id=? ORDER BY updated_at", (job_id,)).fetchall()]

    def put_step(self, job_id: str, step: str, state: str, now: int, attempt: int = 0, output_ref: str | None = None, tokens: int = 0) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO job_step (job_id, step, state, attempt, output_ref, tokens, updated_at) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(job_id, step) DO UPDATE SET state=excluded.state, attempt=excluded.attempt,"
                " output_ref=COALESCE(excluded.output_ref, job_step.output_ref), tokens=excluded.tokens, updated_at=excluded.updated_at",
                (job_id, step, state, attempt, output_ref, tokens, now),
            )

    def reset_steps(self, job_id: str, from_step: str | None, now: int) -> None:
        """手动重试：从某一步起往后的步骤作废（没指定就全部作废）。"""
        order = [row["step"] for row in self.steps(job_id)]
        drop = order if from_step is None or from_step not in order else order[order.index(from_step):]
        with self._conn() as conn:
            for name in drop:
                conn.execute("DELETE FROM job_step WHERE job_id=? AND step=?", (job_id, name))

    # ------------------------------------------------------------ 结果
    def put_result(self, content, now: int, owner_job: str | None = None, step: str | None = None, kind: str | None = None,
                   ttl_days: int | None = 30) -> str:
        """小的内联，大的落文件。返回引用 'res:<ulid>'。"""
        if isinstance(content, (dict, list)):
            body, kind, content_type = _dumps(content), kind or "json", "application/json"
        elif isinstance(content, bytes):
            body, kind, content_type = content.decode("utf-8", "replace"), kind or "blob", "application/octet-stream"
        else:
            body, kind, content_type = str(content), kind or "text", "text/plain; charset=utf-8"
        data = body.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        ref = "res:" + ulid(now)
        inline, path = body, None
        if len(data) > INLINE_LIMIT:
            inline = None
            folder = self.result_dir / (digest[:2])
            folder.mkdir(parents=True, exist_ok=True)
            path = str(folder / digest)
            Path(path).write_bytes(data)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO result (ref, owner_job, step, kind, content_type, size, sha256, inline, path, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ref, owner_job, step, kind, content_type, len(data), digest, inline, path, now, None if ttl_days is None else now + ttl_days * 86400_000),
            )
        return ref

    def result(self, ref: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM result WHERE ref=?", (ref,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        if out["inline"] is None and out["path"]:
            try:
                out["inline"] = Path(out["path"]).read_text(encoding="utf-8")
            except OSError:
                out["inline"] = None
        out["content"] = json.loads(out["inline"]) if out["kind"] == "json" and out["inline"] else out["inline"]
        return out

    # ------------------------------------------------------------ 预算和状态
    def add_usage(self, resource: str, window: str, **amounts: int) -> dict:
        with self._conn() as conn:
            row = conn.execute("SELECT used FROM budget_ledger WHERE resource=? AND window=?", (resource, window)).fetchone()
            used = json.loads(row["used"]) if row else {}
            for key, value in amounts.items():
                used[key] = used.get(key, 0) + value
            conn.execute(
                "INSERT INTO budget_ledger (resource, window, used) VALUES (?,?,?) ON CONFLICT(resource, window) DO UPDATE SET used=excluded.used",
                (resource, window, _dumps(used)),
            )
        return used

    def usage(self, resource: str, window: str) -> dict:
        with self._conn() as conn:
            row = conn.execute("SELECT used FROM budget_ledger WHERE resource=? AND window=?", (resource, window)).fetchone()
        return json.loads(row["used"]) if row else {}

    def state(self, key: str, default=None):
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM sched_state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_state(self, key: str, value, now: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO sched_state (key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, _dumps(value), now),
            )

    # ------------------------------------------------------------ 清理
    def purge(self, now: int, keep_days: int = 60) -> dict:
        cutoff = now - keep_days * 86400_000
        with self._conn() as conn:
            old = [row["run_id"] for row in conn.execute(
                "SELECT run_id FROM task_run WHERE scheduled_for<? AND state NOT IN ('planned','waiting','running')", (cutoff,)).fetchall()]
            for run_id in old:
                ids = [row["job_id"] for row in conn.execute("SELECT job_id FROM job WHERE run_id=?", (run_id,)).fetchall()]
                for job_id in ids:
                    conn.execute("DELETE FROM job_step WHERE job_id=?", (job_id,))
                    conn.execute("DELETE FROM job_attempt WHERE job_id=?", (job_id,))
                conn.execute("DELETE FROM job WHERE run_id=?", (run_id,))
                conn.execute("DELETE FROM task_run WHERE run_id=?", (run_id,))
            expired = conn.execute("SELECT ref, path FROM result WHERE expires_at IS NOT NULL AND expires_at<?", (now,)).fetchall()
            for row in expired:
                if row["path"]:
                    try:
                        Path(row["path"]).unlink()
                    except OSError:
                        pass
                conn.execute("DELETE FROM result WHERE ref=?", (row["ref"],))
        return {"runs": len(old), "results": len(expired)}
