"""把不会再变的行情落在磁盘上。

已经收盘的某一天（全市场日线、涨跌停、行业成分）下次直接读文件。
当天算好的市场页也存一份，重启后先把这份给页面，再在后台刷新。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parents[2] / "data" / "market_cache"
_LOCK = Lock()
_MEMBER_TTL = 7 * 24 * 3600


def _path(key: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in key)
    return _ROOT / (safe[:180] + ".json")


def age(key: str) -> float | None:
    path = _path(key)
    if not path.exists():
        return None
    return time.time() - path.stat().st_mtime


def load(key: str):
    path = _path(key)
    if not path.exists():
        return None
    elapsed = time.time() - path.stat().st_mtime
    if ":l1members:" in key and elapsed > _MEMBER_TTL:
        return None
    if ":discl:" in key and elapsed > 6 * 3600:
        return None
    if ":unlockday:" in key:
        tail = key.rsplit(":", 1)[-1]
        today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
        if tail >= today and elapsed > 6 * 3600:
            return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save(key: str, value) -> None:
    path = _path(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return
    tmp = path.with_suffix(".tmp")
    with _LOCK:
        try:
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        except OSError:
            return


def _restore(value):
    """JSON 只有字符串键。窗口天数、出手次数这种整数键读回来再转成整数。"""
    if isinstance(value, list):
        return [_restore(item) for item in value]
    if isinstance(value, dict):
        restored = {}
        for key, item in value.items():
            if isinstance(key, str) and key.isdigit():
                key = int(key)
            restored[key] = _restore(item)
        return restored
    return value


def load_bundle(day: str):
    payload = load(f"bundle:{day}")
    if not isinstance(payload, dict):
        return None
    return _restore(payload)


def save_bundle(day: str, payload: dict) -> None:
    save(f"bundle:{day}", payload)


def bundle_age(day: str) -> float | None:
    return age(f"bundle:{day}")


def drop(key: str) -> None:
    try:
        _path(key).unlink()
    except OSError:
        pass


def latest_bundle():
    if not _ROOT.exists():
        return None
    best = None
    for path in _ROOT.glob("bundle_*.json"):
        best = path if best is None or path.name > best.name else best
    if best is None:
        return None
    try:
        payload = json.loads(best.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None
