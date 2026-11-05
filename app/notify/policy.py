"""策略：一个事件该进收件箱吗、发到哪些渠道、什么时候发、要不要折叠。设计 7.1–7.2。纯函数，好测。"""

from __future__ import annotations

from datetime import timedelta

from app.notify.types import SEVERITY
from app.scheduler import clock as clocks


def _minutes(hhmm: str) -> int:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return hour * 60 + minute


def in_quiet(now_ms: int, quiet: dict) -> bool:
    if not quiet or not quiet.get("from") or not quiet.get("to"):
        return False
    local = clocks.from_ms(now_ms)
    minute = local.hour * 60 + local.minute
    start, end = _minutes(quiet["from"]), _minutes(quiet["to"])
    return start <= minute < end if start < end else (minute >= start or minute < end)


def quiet_end(now_ms: int, quiet: dict) -> int:
    """静默时段结束的时刻（毫秒）：当前时刻之后第一个 to。"""
    local = clocks.from_ms(now_ms)
    end = _minutes(quiet["to"])
    day = local.date()
    target = clocks.at_time(day, f"{end // 60}:{end % 60:02d}")
    if target <= now_ms:
        target = clocks.at_time(day + timedelta(days=1), f"{end // 60}:{end % 60:02d}")
    return target


def decide(event: dict, event_type, settings: dict, channels: list[dict], now_ms: int, routing: dict | None = None, collapsed: bool = False) -> dict:
    """返回 {"inbox", "deliver": [{channel_id, when, after}], "digest", "suppressed"}。

    inbox 永远为 True：收件箱是唯一的事实来源，策略只决定要不要打扰。
    """
    routing = routing or {}
    severity = routing.get("severity") or event["severity"]
    rank = SEVERITY[severity]
    out = {"inbox": True, "deliver": [], "digest": False, "suppressed": None, "severity": severity}
    if not settings.get("enabled", True):
        out["suppressed"] = "disabled"
        return out
    ttl = event.get("ttl_s") or event_type.ttl_s
    if ttl and now_ms - event["occurred_at"] > ttl * 1000:
        out["suppressed"] = "expired"
        return out
    if rank == 0:
        out["suppressed"] = "info_only"
        return out
    if rank == 1 and collapsed:
        out["suppressed"] = "collapsed"
        return out
    if rank == 1 and event["type"] != "digest.summary" and settings.get("digest", {}).get("enabled") and not routing.get("channels"):
        out["digest"] = True
        out["suppressed"] = "digest"
        return out
    wanted = routing.get("channels")
    for channel in channels:
        if not channel["enabled"] or (wanted is not None and channel["channel_id"] not in wanted):
            continue
        if SEVERITY[channel["min_severity"]] > rank:
            continue
        quiet = channel.get("quiet_hours") or settings.get("quiet_hours") or {}
        after = now_ms
        if rank < 2 and in_quiet(now_ms, quiet):
            after = quiet_end(now_ms, quiet)
        elif rank == 2 and quiet.get("urgent") is False and in_quiet(now_ms, quiet):
            after = quiet_end(now_ms, quiet)
        out["deliver"].append({"channel_id": channel["channel_id"], "when": "now" if after == now_ms else "after_quiet", "after": after})
    return out
