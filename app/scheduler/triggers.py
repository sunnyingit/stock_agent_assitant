"""触发器：校验，以及「到现在为止应该触发过的周期」。

只回答「哪些周期应当存在」，不管有没有跑过、错过了怎么办（那是 planner 的事）。种类见设计文档 3.2：
daily、session_interval、cron、data_ready、event、manual。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from app.scheduler import clock
from app.scheduler.calendar import TradingCalendar

DAYS = ("trading", "weekday", "any")
KINDS = ("daily", "session_interval", "cron", "data_ready", "event", "manual")
DEFAULT_WINDOW_MIN = 120
_HHMM = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class Period:
    key: str              # task_run.period_key
    scheduled_for: int    # 应触发的时刻（毫秒）
    window_ms: int        # 这段时间内触发都算准时；过了就是错过
    trade_date: str       # 这个周期属于哪个交易日（或日历日）
    slot: bool = False    # 是不是 session_interval 的一个时刻：错过的不补


def validate(trigger: dict, min_interval_min: int = 0) -> dict:
    """返回规范化后的触发器；不合法抛 ValueError（消息可以直接给用户看）。"""
    if not isinstance(trigger, dict) or trigger.get("kind") not in KINDS:
        raise ValueError("触发器 kind 必须是 " + "、".join(KINDS))
    kind = trigger["kind"]
    out = {"kind": kind}
    if kind in ("daily", "cron", "data_ready", "session_interval"):
        days = trigger.get("days", "trading")
        if days not in DAYS:
            raise ValueError("days 必须是 " + "、".join(DAYS))
        out["days"] = days
    if kind == "daily":
        if not _HHMM.match(str(trigger.get("at") or "")):
            raise ValueError("daily 需要 at，格式 HH:MM")
        out["at"] = _pad(trigger["at"])
        out["window_min"] = _window(trigger.get("window_min", DEFAULT_WINDOW_MIN))
    elif kind == "session_interval":
        every = trigger.get("every_min")
        if not isinstance(every, int) or every < 1 or every > 120:
            raise ValueError("every_min 要在 1 到 120 之间")
        if every < min_interval_min:
            raise ValueError(f"这个任务的间隔不能小于 {min_interval_min} 分钟")
        sessions = tuple(trigger.get("sessions") or ("am", "pm"))
        if not sessions or any(item not in ("am", "pm") for item in sessions):
            raise ValueError("sessions 只能是 am、pm")
        out["every_min"], out["sessions"] = every, list(sessions)
    elif kind == "cron":
        parse_cron(str(trigger.get("expr") or ""))
        out["expr"] = " ".join(str(trigger["expr"]).split())
        out["window_min"] = _window(trigger.get("window_min", 60))
    elif kind == "data_ready":
        if not trigger.get("gate"):
            raise ValueError("data_ready 需要 gate")
        out["gate"] = str(trigger["gate"])
        deadline = trigger.get("deadline", "23:59")
        if not _HHMM.match(str(deadline)):
            raise ValueError("deadline 格式 HH:MM")
        out["deadline"] = _pad(deadline)
    elif kind == "event":
        out["topic"] = str(trigger.get("topic") or "")
        out["debounce_s"] = max(0, int(trigger.get("debounce_s", 30)))
        out["max_wait_s"] = max(out["debounce_s"], int(trigger.get("max_wait_s", 120)))
    return out


def _pad(hhmm: str) -> str:
    hour, minute = hhmm.split(":")
    return f"{int(hour):02d}:{minute}"


def _window(value) -> int:
    minutes = int(value)
    if not 1 <= minutes <= 24 * 60:
        raise ValueError("window_min 要在 1 到 1440 之间")
    return minutes


def candidates(trigger: dict, now_ms: int, calendar: TradingCalendar, lookback_days: int = 3, horizon_ms: int | None = None) -> list[Period]:
    """到现在为止，应当触发过的周期，按时刻升序。horizon_ms：更早的周期不再考虑（分钟级的 cron 否则一次要查几千个）。"""
    kind = trigger["kind"]
    today = clock.day_of(now_ms)
    first = today - timedelta(days=lookback_days)
    out: list[Period] = []
    if kind in ("daily", "data_ready"):
        label = "trading_day" if trigger.get("days", "trading") == "trading" else "day"
        for day in clock.daterange(first, today):
            if not calendar.matches(day, trigger.get("days", "trading")):
                continue
            if kind == "daily":
                start, window = clock.at_time(day, trigger["at"]), trigger.get("window_min", DEFAULT_WINDOW_MIN) * 60_000
            else:
                start = clock.at_time(day, "00:00")
                window = max(60_000, clock.at_time(day, trigger["deadline"]) - start)
            if start <= now_ms:
                out.append(Period(f"{label}:{day.isoformat()}", start, window, day.isoformat()))
    elif kind == "session_interval":
        if calendar.is_trading_day(today) or trigger.get("days") == "any":
            slots = calendar.session_slots(today, trigger["every_min"], tuple(trigger.get("sessions") or ("am", "pm")))
            due = [minute for minute in slots if clock.at_time(today, f"{minute // 60}:{minute % 60:02d}") <= now_ms]
            if due:
                minute = due[-1]
                start = clock.at_time(today, f"{minute // 60}:{minute % 60:02d}")
                out.append(Period(f"min:{today.isoformat()}T{minute // 60:02d}:{minute % 60:02d}", start, trigger["every_min"] * 60_000, today.isoformat(), slot=True))
    elif kind == "cron":
        fields = parse_cron(trigger["expr"])
        for day in clock.daterange(first, today):
            if not calendar.matches(day, trigger.get("days", "trading")) or not _cron_day(fields, day):
                continue
            for hour in sorted(fields["hour"]):
                for minute in sorted(fields["minute"]):
                    start = clock.at_time(day, f"{hour}:{minute:02d}")
                    if start <= now_ms:
                        out.append(Period(f"cron:{day.isoformat()}T{hour:02d}:{minute:02d}", start, trigger.get("window_min", 60) * 60_000, day.isoformat()))
    if horizon_ms is not None:
        out = [period for period in out if now_ms - period.scheduled_for <= horizon_ms]
    return sorted(out, key=lambda period: period.scheduled_for)


def window_ms(trigger: dict, scheduled_for: int | None = None) -> int | None:
    """这种触发器下，错过多久之内还算准时。事件和手动触发没有时限。"""
    kind = trigger["kind"]
    if kind == "daily":
        return trigger.get("window_min", DEFAULT_WINDOW_MIN) * 60_000
    if kind == "cron":
        return trigger.get("window_min", 60) * 60_000
    if kind == "session_interval":
        return trigger["every_min"] * 60_000
    if kind == "data_ready" and scheduled_for is not None:
        day = clock.day_of(scheduled_for)
        return max(60_000, clock.at_time(day, trigger["deadline"]) - scheduled_for)
    return None


def period_date(period_key: str) -> str:
    """周期键里的日期：'trading_day:2026-11-05'、'min:2026-11-05T14:30'、'cron:2026-11-05T08:40' → '2026-11-05'；没有返回空串。"""
    if ":" in period_key:
        tail = period_key.split(":", 1)[1]
        if len(tail) >= 10 and tail[4] == "-" and tail[7] == "-":
            return tail[:10]
    return ""


def next_fire(trigger: dict, now_ms: int, calendar: TradingCalendar, horizon_days: int = 14) -> int | None:
    """下一次触发时刻（毫秒）；事件和手动触发没有。给界面和「干跑」用。"""
    return next(iter(next_fires(trigger, now_ms, calendar, 1, horizon_days)), None)


def next_fires(trigger: dict, now_ms: int, calendar: TradingCalendar, count: int = 5, horizon_days: int = 21) -> list[int]:
    kind = trigger["kind"]
    if kind in ("event", "manual"):
        return []
    today = clock.day_of(now_ms)
    out: list[int] = []
    for day in clock.daterange(today, today + timedelta(days=horizon_days)):
        if not calendar.matches(day, trigger.get("days", "trading")):
            continue
        if kind == "daily":
            times = [clock.at_time(day, trigger["at"])]
        elif kind == "data_ready":
            times = [clock.at_time(day, "00:00")]
        elif kind == "session_interval":
            times = [clock.at_time(day, f"{m // 60}:{m % 60:02d}") for m in calendar.session_slots(day, trigger["every_min"], tuple(trigger.get("sessions") or ("am", "pm")))]
        else:
            fields = parse_cron(trigger["expr"])
            times = [clock.at_time(day, f"{h}:{m:02d}") for h in sorted(fields["hour"]) for m in sorted(fields["minute"])] if _cron_day(fields, day) else []
        out.extend(stamp for stamp in times if stamp > now_ms)
        if len(out) >= count:
            break
    return sorted(out)[:count]


# ---------------------------------------------------------------- cron
_RANGES = {"minute": (0, 59), "hour": (0, 23), "dom": (1, 31), "month": (1, 12), "dow": (0, 7)}


def parse_cron(expr: str) -> dict:
    """五段：分 时 日 月 周。支持 * 、a-b、a,b、*/n、a-b/n；周 0 和 7 都是星期日。"""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError("cron 要写成五段：分 时 日 月 周")
    out = {}
    for name, part in zip(("minute", "hour", "dom", "month", "dow"), parts):
        out[name] = _expand(part, *_RANGES[name], name)
        out[name + "_star"] = part == "*" or part.startswith("*/")
    if 7 in out["dow"]:
        out["dow"] = (out["dow"] - {7}) | {0}
    return out


def _expand(part: str, low: int, high: int, name: str) -> set[int]:
    values: set[int] = set()
    for chunk in part.split(","):
        step = 1
        if "/" in chunk:
            chunk, raw = chunk.split("/", 1)
            if not raw.isdigit() or int(raw) < 1:
                raise ValueError(f"cron 的 {name} 步长不对：{part}")
            step = int(raw)
        if chunk in ("*", ""):
            start, end = low, high
        elif "-" in chunk:
            a, b = chunk.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise ValueError(f"cron 的 {name} 写法不对：{part}")
            start, end = int(a), int(b)
        elif chunk.isdigit():
            start = end = int(chunk)
        else:
            raise ValueError(f"cron 的 {name} 写法不对：{part}")
        if start < low or end > high or start > end:
            raise ValueError(f"cron 的 {name} 超出范围 {low}–{high}：{part}")
        values.update(range(start, end + 1, step))
    return values


def _cron_day(fields: dict, day: date) -> bool:
    if day.month not in fields["month"]:
        return False
    dow = (day.weekday() + 1) % 7          # cron：0 = 星期日
    dom_ok, dow_ok = day.day in fields["dom"], dow in fields["dow"]
    if fields["dom_star"] and fields["dow_star"]:
        return True
    if fields["dom_star"]:
        return dow_ok
    if fields["dow_star"]:
        return dom_ok
    return dom_ok or dow_ok                  # 日和周都写了：满足任一（和 vixie cron 一致）
