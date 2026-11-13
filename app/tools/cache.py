"""进程内缓存，按 key 保存，到期后失效。"""

from __future__ import annotations

import time
from threading import Lock

_lock = Lock()
_store: dict[str, tuple[float, object]] = {}


def get(key: str):
    now = time.monotonic()
    with _lock:
        item = _store.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at <= now:
            _store.pop(key, None)
            return None
        return value


def set(key: str, value, ttl: float) -> None:
    with _lock:
        _store[key] = (time.monotonic() + ttl, value)


def delete(key: str) -> None:
    with _lock:
        _store.pop(key, None)


def delete_prefix(prefix: str) -> int:
    with _lock:
        keys = [key for key in _store if key.startswith(prefix)]
        for key in keys:
            _store.pop(key, None)
        return len(keys)


def clear() -> None:
    with _lock:
        _store.clear()
