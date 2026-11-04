"""把 5 个维度详情接口的返回，转成统一的 Snapshot。

只读结构化字段，不读 text / tag_text 这类已经写好的文字：文字里的数字没法核对。
字段名来自各维度 build_detail 的返回结构；某个字段缺了，对应的事实就不出，不报错。
"""

from __future__ import annotations

from typing import Any, Callable

from app.synthesis import policy
from app.synthesis.types import (
    STATUS_INVALID,
    STATUS_OK,
    STATUS_PENDING,
    STATUS_STALE,
    STATUS_UNAVAILABLE,
    Snapshot,
)

_STATE = {
    "ok": STATUS_OK,
    "intraday": STATUS_OK,
    "no_news": STATUS_OK,
    "expired": STATUS_STALE,
    "stale": STATUS_STALE,
    "computing": STATUS_PENDING,
    "failed": STATUS_UNAVAILABLE,
    "insufficient": STATUS_UNAVAILABLE,
    "error": STATUS_UNAVAILABLE,
}
_LEAN = {"bullish": "bullish", "bearish": "bearish", "neutral": "neutral", "偏多": "bullish", "偏空": "bearish"}
_CERTAINTY = {"high": "高", "medium": "一般", "low": "低"}
_CATEGORY = {
    "earnings": "业绩", "operations": "经营", "capital": "资本", "governance": "风险与治理",
    "industry": "政策与行业", "macro": "宏观与市场", "opinion": "观点", "rumor": "传闻与澄清",
}
_IMPACT = {"positive": "利好", "negative": "利空", "neutral": "中性"}
_MAGNITUDE = {"large": "大", "medium": "中", "small": "小"}
_HOLDER_TYPE = {
    "buyback": "回购", "holder_reduce": "减持", "holder_increase": "增持", "unlock": "解禁",
    "pledge": "质押", "holder_count": "股东户数变化", "incentive": "股权激励",
}
_SECTOR_EVENT = {
    "start": "板块可能进入主升",
    "main": "板块可能进入高潮",
    "climax": "板块可能从高潮转入退潮",
    "diverge": "板块分歧可能演变成退潮",
    "ebb": "板块退潮可能结束或转入弱势",
    "weak": "板块弱势可能结束",
}


def _get(data: Any, *path, default=None):
    current = data
    for part in path:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, (list, tuple)) and isinstance(part, int) and -len(current) <= part < len(current):
            current = current[part]
        else:
            return default
        if current is None:
            return default
    return current


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _lean(value) -> str:
    return _LEAN.get(value, "unclear")


def view_text(snap: Snapshot) -> str:
    if snap.lean in ("bullish", "bearish"):
        text = f"{policy.DIRECTION_TEXT[snap.lean]} · {policy.LEVEL_TEXT.get(snap.level, '稍微')}"
    elif snap.lean == "neutral":
        text = "中性"
    else:
        text = "不明"
    if snap.certainty in _CERTAINTY and snap.lean in ("bullish", "bearish"):
        text += f" · 把握{_CERTAINTY[snap.certainty]}"
    return text


def _base(key: str, payload: dict | None, error: str = "") -> Snapshot:
    snap = Snapshot(key=key)
    if payload is None:
        snap.reason = error or "没有取到数据"
        return snap
    meta = payload.get("meta") or {}
    state = meta.get("state") or "ok"
    snap.status = _STATE.get(state, STATUS_UNAVAILABLE)
    snap.as_of = str(meta.get("as_of") or "")
    snap.rule_version = str(meta.get("rule_version") or "")
    snap.reason = str(meta.get("error") or meta.get("status_text") or "") if snap.status != STATUS_OK else ""
    verdict = payload.get("verdict") or {}
    if snap.status == STATUS_OK and verdict:
        snap.lean = _lean(verdict.get("lean"))
        level = verdict.get("level")
        snap.level = int(level) if isinstance(level, (int, float)) and snap.lean in ("bullish", "bearish") else 0
        snap.certainty = verdict.get("certainty") if verdict.get("certainty") in _CERTAINTY else None
    return snap


def _put(snap: Snapshot, fact_id: str, value) -> None:
    if value is not None:
        snap.facts[fact_id] = value


# ---------------------------------------------------------------- 量价资金
def volume_flow(payload: dict | None, error: str = "") -> Snapshot:
    snap = _base("volume_flow", payload, error)
    if payload is None or snap.status != STATUS_OK:
        return snap
    rows = _get(payload, "windows", "rows", default=[]) or []
    primary = next((row for row in rows if row.get("days") == policy.FLOW_WINDOW), {})
    row20 = next((row for row in rows if row.get("days") == 20), {})
    pattern = _get(payload, "price_volume", "pattern")
    _put(snap, "flow.view", view_text(snap))
    _put(snap, "flow.rel_vol", _number(primary.get("rel_vol")))
    _put(snap, "flow.pattern", pattern if pattern else None)
    _put(snap, "flow.up_down_vol", _number(_get(payload, "price_volume", "up_down_vol_ratio")))
    _put(snap, "flow.big_trend", _get(payload, "flow_daily", "trend") or None)
    _put(snap, "flow.net_rate", _number(primary.get("net_rate")))
    hits = [item.get("text") for item in _get(payload, "position_hints", "items", default=[]) if item.get("hit")]
    _put(snap, "flow.stall", "、".join(hits) if hits else "无")
    # 跨模块核对仍用 20 日，和 market.*_20d 同一口径；主窗口数字走 facts
    rel = _get(payload, "relative_strength") or {}
    win20 = next((w for w in (rel.get("windows") or []) if w.get("days") == 20), None)
    index_bars = ((win20 or rel).get("bars") if isinstance(win20 or rel, dict) else None) or rel.get("bars") or []
    index_bar = next((row for row in index_bars if row.get("kind") == "index"), {})
    snap.flags["window_ret"] = _number(row20.get("ret"))
    snap.flags["index_ret"] = _number(index_bar.get("ret"))
    snap.flags["stall_hits"] = len(hits)
    return snap


# ---------------------------------------------------------------- 新闻
def _event_text(event: dict) -> str:
    grade = event.get("source_grade") or "?"
    return " · ".join([
        _CATEGORY.get(event.get("category"), "其他") + "类",
        f"{grade} 级",
        _IMPACT.get(event.get("impact"), "中性"),
        "量级" + _MAGNITUDE.get(event.get("magnitude"), "小"),
    ])


def news(payload: dict | None, error: str = "") -> Snapshot:
    snap = _base("news", payload, error)
    if payload is None or snap.status != STATUS_OK:
        return snap
    events = [item for item in payload.get("key_events") or [] if isinstance(item, dict)]
    main = next((item for item in events if item.get("role") == "main"), None)
    counter = next((item for item in events if item.get("role") == "counter"), None)
    _put(snap, "news.view", view_text(snap))
    _put(snap, "news.main_event", _event_text(main) if main else "近期没有公司范围的有效事件")
    if main:
        _put(snap, "news.priced_in", _get(payload, "reaction", "priced_in"))
    if counter:
        _put(snap, "news.counter_event", _event_text(counter))
    notice = any(item.get("scope") == "company" and item.get("source_grade") == "A" for item in events)
    _put(snap, "news.company_notice", notice)
    snap.flags["main_impact"] = (main or {}).get("impact")
    snap.flags["main_published"] = (main or {}).get("published_at")
    snap.flags["main_session"] = (main or {}).get("publish_session")
    snap.flags["heat_risk"] = bool(_get(payload, "heat", "risk_hit"))
    return snap


# ---------------------------------------------------------------- 板块
def sector(payload: dict | None, error: str = "") -> Snapshot:
    snap = _base("sector", payload, error)
    if payload is None or snap.status != STATUS_OK:
        return snap
    primary = _get(payload, "sectors", "primary", default={}) or {}
    stage = _get(primary, "stage", "current")
    lines = _get(primary, "strength", "lines", default={}) or {}
    stock_line, sector_line, index_line = (lines.get(name) or [] for name in ("stock", "sector", "index"))
    _put(snap, "sector.view", view_text(snap))
    _put(snap, "sector.stage", stage)
    _put(snap, "sector.days", _number(_get(primary, "stage", "days_in_round")))
    _put(snap, "sector.role", _get(primary, "role", "value"))
    if sector_line and index_line:
        _put(snap, "sector.vs_index", round(sector_line[-1] - index_line[-1], 4))
    if stock_line and sector_line:
        _put(snap, "sector.stock_vs_sector", round(stock_line[-1] - sector_line[-1], 4))
    hint = primary.get("position_hint") or {}
    _put(snap, "sector.warn", bool(hint.get("climax") or hint.get("overdue")))
    _put(snap, "sector.driver", _get(payload, "sectors", "driver", "type"))
    _put(snap, "sector.secondary_stage", _get(payload, "sectors", "secondary", "stage", "current"))
    _put(snap, "sector.divergent", bool(_get(payload, "sectors", "divergence", "active", default=False)))
    snap.flags["stage"] = stage
    snap.flags["index_ret"] = _number(index_line[-1]) if len(index_line) >= 10 else None
    if stage in _SECTOR_EVENT:
        snap.events.append({"kind": "sector_stage", "text": _SECTOR_EVENT[stage]})
    return snap


# ---------------------------------------------------------------- 财报
_ACTIVE_DAYS = 60      # 距披露不超过这么多天，财报还算"新"
_UPCOMING_DAYS = 30    # 距下次披露不超过这么多天，算"即将披露"


def finance(payload: dict | None, error: str = "") -> Snapshot:
    snap = _base("finance", payload, error)
    if payload is None or snap.status != STATUS_OK:
        return snap
    cells = {item.get("key"): item.get("level") for item in _get(payload, "cells", default=[])}
    inputs = payload.get("inputs") or {}
    days_since = _number(_get(payload, "meta", "report", "days_since"))
    days_left = _number(_get(payload, "meta", "next_disclosure", "days_left"))
    if days_since is not None and days_since <= _ACTIVE_DAYS:
        phase = "active"
    elif days_left is not None and 0 <= days_left <= _UPCOMING_DAYS:
        phase = "upcoming"
    else:
        phase = "quiet"
    _put(snap, "fin.view", view_text(snap))
    _put(snap, "fin.phase", phase)
    _put(snap, "fin.growth", cells.get("growth"))
    _put(snap, "fin.quality", cells.get("quality"))
    _put(snap, "fin.days_since", days_since)
    _put(snap, "fin.days_to_next", days_left)
    _put(snap, "fin.warn_hits", _number(inputs.get("warn_hits")))
    red = bool(inputs.get("red_line"))
    _put(snap, "fin.red_line", red)
    snap.flags["red_line"] = red
    snap.flags["time_weight"] = inputs.get("time_weight", 1.0)   # 财报一季一出，离披露越远，参考价值越低
    if days_left is not None and 0 <= days_left <= _UPCOMING_DAYS:
        snap.events.append({"kind": "finance_disclosure", "text": f"下次财报披露还有 {int(days_left)} 天"})
    return snap


# ---------------------------------------------------------------- 基本面
def fundamental(payload: dict | None, error: str = "") -> Snapshot:
    snap = _base("fundamental", payload, error)
    if payload is None or snap.status != STATUS_OK:
        return snap
    cells = {item.get("key"): item.get("level") for item in _get(payload, "cells", default=[])}
    _put(snap, "fund.view", view_text(snap))
    _put(snap, "fund.valuation", cells.get("valuation"))
    _put(snap, "fund.pctile", _number(_get(payload, "valuation", "pctile_5y")))
    _put(snap, "fund.quality", cells.get("quality"))
    _put(snap, "fund.holders", cells.get("holders"))
    near = []
    for event in payload.get("holder_events") or []:
        days_left = _number(event.get("days_left"))
        if days_left is None or not 0 <= days_left <= policy.NEAR_EVENT_DAYS or event.get("impact") not in ("positive", "negative"):
            continue
        name = _HOLDER_TYPE.get(event.get("type"), "股东事件")
        text = f"{name}（{_IMPACT[event['impact']]}），{int(days_left)} 个交易日内"
        near.append((days_left, text))
    near.sort()
    _put(snap, "fund.holder_event", near[0][1] if near else "无")
    for _days, text in near[:2]:
        snap.events.append({"kind": "holder", "text": text})
    snap.flags["pctile"] = _number(_get(payload, "valuation", "pctile_5y"))
    return snap


ADAPTERS: dict[str, Callable[..., Snapshot]] = {
    "volume_flow": volume_flow,
    "news": news,
    "sector": sector,
    "finance": finance,
    "fundamental": fundamental,
}


def adapt(key: str, payload: dict | None, error: str = "") -> Snapshot:
    try:
        return ADAPTERS[key](payload, error)
    except Exception as exc:  # 适配器出错不能拖垮整次综合，按该维度不可用处理
        snap = Snapshot(key=key, status=STATUS_INVALID)
        snap.reason = f"字段解析失败: {exc}"
        return snap
