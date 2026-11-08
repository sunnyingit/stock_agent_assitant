"""组装基本面详情。方向由规则算出（给综合判断用），结论那一句用模板；AI 解读在 app/fundamental_review，由路由并进返回。"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from threading import Lock
from zoneinfo import ZoneInfo

from app import config
from app.tools import cache
from app.services.verdict_view import rule_fields, slim
from app.tools.stock import Stock, is_hk
from app.services.fundamental_detail.copy import write_text
from app.services.fundamental_detail.rules import (
    DISCLAIMER_TEXT,
    METRIC_TEXT,
    RULE_VERSION,
    break_gaps,
    certainty,
    clean_peers,
    counter_text,
    event_counts,
    history_label,
    holder_note,
    holders_level,
    holders_level_text,
    iso_date,
    judge,
    periods_behind,
    listed_under_one_year,
    meaningful,
    parse_ymd,
    peer_gap_text,
    peer_median,
    percentile,
    period_code,
    quality_level,
    quality_level_text,
    quality_note,
    quality_rows,
    quantiles,
    speak_multiple,
    status_line,
    support_text,
    valuation_level,
    valuation_level_text,
    weakest_item,
)

_ZONE = ZoneInfo("Asia/Shanghai")
_inflight: set[str] = set()
_inflight_lock = Lock()
_TABLE = (
    ("pe_ttm", "市盈率（TTM，扣非）", "times"),
    ("pb", "市净率", "times"),
    ("ps_ttm", "市销率（TTM）", "times"),
    ("roe", "ROE", "percent"),
    ("debt_to_assets", "资产负债率", "percent"),
    ("ocf_to_np", "经营现金流/净利润", "ratio"),
)
_EVENT_TYPES = {"buyback", "holder_reduce", "holder_increase", "unlock", "pledge", "holder_count", "incentive"}
_ALERT = {
    "buyback": "fund_buyback_due",
    "holder_reduce": "fund_reduce_due",
    "holder_increase": "fund_increase_due",
    "unlock": "fund_unlock_due",
    "pledge": "fund_pledge_due",
    "incentive": "fund_incentive_due",
}


def build_detail(stock: Stock, snapshot: dict | None = None, today: date | None = None) -> dict:
    today = today or datetime.now(_ZONE).date()
    if is_hk(stock) and snapshot is None:
        return _empty(stock, "insufficient", None, "港股暂不按这套同行口径比较")
    cache_key = f"fundamental_detail:{stock.ts_code}:{today.isoformat()}"
    live = snapshot is None
    if live:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        if not _claim(cache_key):
            return _shell(stock, "computing", today)
    try:
        data = snapshot if snapshot is not None else _load(stock)
        payload = _assemble(stock, data, today)
        if live and payload["meta"]["state"] != "computing":
            cache.set(cache_key, payload, config.CACHE_TTL["fundamental_detail"])
        return payload
    finally:
        if live:
            _release(cache_key)


def failed_payload(stock: Stock, message: str, today: date | None = None) -> dict:
    payload = _shell(stock, "failed", today or datetime.now(_ZONE).date())
    payload["meta"]["status_text"] = "数据暂时取不到"
    payload["error"] = message
    return payload


def _load(stock: Stock) -> dict:
    from app.services.fundamental_detail.load import load_snapshot

    return load_snapshot(stock)


def _assemble(stock: Stock, snapshot: dict, today: date) -> dict:
    price_day = parse_ymd(snapshot.get("price_date")) or today
    industry = dict(snapshot.get("industry") or {})
    report_end = snapshot.get("report_end")
    name = _display_name(stock, snapshot)
    if listed_under_one_year(snapshot.get("list_date"), price_day):
        return _empty(stock, "insufficient", price_day, "上市不足一年", report_end, industry, name)
    if industry.get("status") == "empty":
        return _empty(stock, "insufficient", price_day, "没有可比同行", report_end, industry, name)

    series = _series(snapshot)
    history = _history(series.get("history") or [])
    label = history_label([item["date"] for item in history])
    values = [item["value"] for item in history]
    raw_pctile = percentile(series.get("value"), values)
    pctile = None if raw_pctile is None else round(raw_pctile, 2)
    peers = clean_peers(series.get("peers") or [], stock.ts_code, series.get("value"), name)
    for row in peers:
        if row["ts_code"] == stock.ts_code:
            row["name"] = name
        row["value"] = _multiple(row["value"])
    mid = peer_median(peers)
    if mid is not None:
        mid = _multiple(mid)
    value = _multiple(series.get("value"))
    level = valuation_level(pctile, value, mid)
    bands = quantiles(values)
    metric = series.get("metric") or "pe_ttm_deducted"
    metric_text = series.get("metric_text") or METRIC_TEXT.get(metric) or "估值"
    speak = METRIC_TEXT.get(metric) or metric_text

    items = quality_rows(snapshot)
    red_line = int(snapshot.get("ocf_negative_years") or 0) >= 3
    q_level = quality_level(items, int(snapshot.get("ocf_negative_years") or 0))
    weak = weakest_item(items)
    q_note = quality_note(None if weak is None else weak["key"], red_line)

    events = _events(snapshot.get("holder_events") or [], price_day, snapshot.get("trade_dates") or [])
    h_level = holders_level(events)
    h_note = holder_note(h_level, events)
    lean, score = judge(level, q_level, h_level)
    certain, reasons = certainty(industry.get("peer_count"), report_end, price_day)
    risk = _risk(events)
    distorted = bool(snapshot.get("distorted"))
    text = write_text(_facts(
        metric, speak, metric_text, label, value, pctile, mid, level, q_level, h_level,
        items, distorted, snapshot.get("pe_reference"), bool(snapshot.get("cash_beats_profit")),
        q_note, red_line,
    ))
    public = [_public_event(item) for item in events]
    return {
        "ts_code": stock.ts_code,
        "stock_name": name,
        "meta": _meta(stock, "ok", price_day, report_end, industry, ""),
        "verdict": {
            **rule_fields(lean, text),
            # 下面三项只给综合判断读，对外响应里由 public() 去掉
            "lean": lean,
            "level": score,
            "certainty": certain,
        },
        "cells": [
            {"key": "valuation", "level": level, "level_text": valuation_level_text(level), "note_text": _valuation_note(label, pctile, distorted, metric)},
            {"key": "quality", "level": q_level, "level_text": quality_level_text(q_level), "note_text": q_note},
            {"key": "holders", "level": h_level, "level_text": holders_level_text(h_level), "note_text": h_note},
        ],
        "valuation": {
            "metric": metric,
            "metric_text": metric_text,
            "metric_reason": series.get("metric_reason") or None,
            "value": value,
            "pctile_5y": pctile,
            "peer_median": mid,
            "anchors": _anchors(metric, value, snapshot.get("close"), bands),
            "history": {
                "dates": [item["date"] for item in history],
                "values": [_multiple(item["value"]) for item in history],
                "p20": bands["p20"],
                "p50": bands["p50"],
                "p80": bands["p80"],
            },
            "peers": peers,
        },
        "price": _price_position(snapshot),
        "quality": {"items": items, "weakest": weak},
        "holder_events": public,
        "business": _public_business(snapshot.get("business")),
        "table": {"rows": _rows(snapshot, metric, value, mid, items)},
        "position_input": {
            "valuation_pctile_5y": pctile,
            "valuation_level": level,
            "risk_hit": risk,
        },
        "disclaimer_text": DISCLAIMER_TEXT,
        # 给 AI 体检和综合判断读的中间量，对外响应里由 public() 去掉
        "inputs": {
            "ocf_negative_years": int(snapshot.get("ocf_negative_years") or 0),
            "red_line": red_line,
            "distorted": distorted,
            "cash_beats_profit": bool(snapshot.get("cash_beats_profit")),
            "periods_behind": periods_behind(report_end, price_day),
            "certainty_reasons": reasons,
            "list_date": snapshot.get("list_date"),
            "business": snapshot.get("business"),
        },
    }


INTERNAL_FIELDS = ("inputs",)


def public(payload: dict) -> dict:
    """对外响应：verdict 只留和 headline 同一套的四个字段，中间量不对外。
    build_detail 的完整结果是给综合判断和 AI 体检读的（lean/level/certainty、inputs）。"""
    body = {key: value for key, value in payload.items() if key not in INTERNAL_FIELDS}
    verdict = payload.get("verdict")
    return {**body, "verdict": None if verdict is None else slim(verdict)}


ANCHOR_NOTE = "估值锚点 = 自身 5 年历史的 20%、50%、80% 分位倍数 × 当前扣非每股收益，只描述历史倍数对应的价格位置，不是目标价，不构成投资建议。"


def _price_position(snapshot: dict) -> dict:
    """当前股价在近 5 年收盘价里的位置。回答的是价格高不高，不是估值贵不贵。"""
    points = _history(snapshot.get("price_history") or [])
    values = [item["value"] for item in points if item.get("value")]
    close = snapshot.get("close")
    raw = percentile(close, values)
    bands = quantiles(values)
    return {
        "close": None if close is None else round(float(close), 2),
        "pctile_5y": None if raw is None else round(raw, 2),
        "low": None if not values else round(min(values), 2),
        "high": None if not values else round(max(values), 2),
        "history": {
            "dates": [item["date"] for item in points],
            "values": [None if item.get("value") is None else round(float(item["value"]), 2) for item in points],
            "p20": bands["p20"],
            "p50": bands["p50"],
            "p80": bands["p80"],
        },
    }


def _anchors(metric: str, value, close, bands: dict) -> dict | None:
    """估值锚点价格。开关关闭、不是扣非市盈率、扣非每股收益不为正、或历史分位线不全时都是 None。"""
    if not config.ENABLE_PRICE_ANCHORS or metric != "pe_ttm_deducted" or not value or not close:
        return None
    eps = float(close) / float(value)
    lines = {"low": bands.get("p20"), "mid": bands.get("p50"), "high": bands.get("p80")}
    if eps <= 0 or any(v is None for v in lines.values()):
        return None
    return {
        "eps_ttm_deducted": round(eps, 4),
        **{name: {"multiple": round(multiple, 2), "price": round(multiple * eps, 2)} for name, multiple in lines.items()},
        "note": ANCHOR_NOTE,
    }


def _series(snapshot: dict) -> dict:
    primary = {
        "metric": snapshot.get("metric"),
        "metric_text": snapshot.get("metric_text"),
        "metric_reason": snapshot.get("metric_reason"),
        "value": snapshot.get("value"),
        "history": snapshot.get("history") or [],
        "peers": snapshot.get("peers") or [],
    }
    if meaningful(primary["metric"], primary["value"]):
        return primary
    fallback = snapshot.get("fallback") or {}
    if meaningful(fallback.get("metric"), fallback.get("value")):
        return {
            "metric": fallback.get("metric"),
            "metric_text": fallback.get("metric_text"),
            "metric_reason": fallback.get("metric_reason"),
            "value": fallback.get("value"),
            "history": fallback.get("history") or [],
            "peers": fallback.get("peers") or [],
        }
    return primary


def _history(rows: list) -> list[dict]:
    points = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        day = iso_date(item.get("date"))
        if not day:
            continue
        points.append({"date": day, "value": _history_value(item.get("value"))})
    points.sort(key=lambda item: item["date"])
    return break_gaps(points)[-280:]


def _history_value(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0:
        return None
    return number


def _events(raw: list, price_day: date, trade_dates: list) -> list[dict]:
    rows = []
    for item in raw:
        kind = item.get("type")
        impact = item.get("impact") or "none"
        if kind not in _EVENT_TYPES or impact not in ("positive", "negative", "none"):
            continue
        copied = dict(item)
        copied["_price_day"] = price_day.isoformat() if price_day else None
        copied["_counts"] = event_counts(copied, price_day, trade_dates)
        rows.append(copied)
    directional = [item for item in rows if item.get("impact") != "none"]
    reference = [item for item in rows if item.get("impact") == "none"]
    directional.sort(key=lambda item: iso_date(item.get("date")) or "9999-99-99")
    reference.sort(key=lambda item: iso_date(item.get("date")) or "9999-99-99")
    return directional + reference


def _public_event(event: dict) -> dict:
    due = iso_date(event.get("date"))
    days_left = _days_left(event, due)
    return {
        "event_id": _event_id(event),
        "type": event.get("type"),
        "title": event.get("title") or event.get("type"),
        "impact": event.get("impact") or "none",
        "date": due,
        "days_left": days_left,
        "progress": _progress(event.get("progress")),
        "detail_text": event.get("detail_text") or "",
        "float_ratio": _ratio(event.get("float_ratio")),
        "source_url": event.get("source_url") or None,
        "alert": _alert(event, days_left),
    }


def _days_left(event: dict, due: str | None):
    if event.get("days_left") is not None:
        try:
            return int(event["days_left"])
        except (TypeError, ValueError):
            return None
    if due is None:
        return None
    price_day = parse_ymd(event.get("_price_day"))
    if price_day is None:
        return None
    return (parse_ymd(due) - price_day).days


def _progress(value):
    if not isinstance(value, dict):
        return None
    unit = value.get("unit")
    if unit not in ("amount", "float_share"):
        return None
    digits = 2 if unit == "amount" else 4
    return {"done": _ratio(value.get("done"), digits), "plan": _ratio(value.get("plan"), digits), "unit": unit}


def _alert(event: dict, days_left):
    if event.get("impact") in (None, "none") or days_left is None or days_left < 0:
        return None
    condition = _ALERT.get(event.get("type"))
    if not condition:
        return None
    return {"condition_id": condition, "subscribed": False, "alert_id": None}


def _event_id(event: dict) -> str:
    if event.get("event_id"):
        return str(event["event_id"])
    raw = f"{event.get('type')}|{event.get('title')}|{event.get('date') or ''}"
    return "fh_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:4]


def _public_business(raw) -> dict | None:
    """对外只留主营描述和产品线，给页面「靠什么赚钱」用。"""
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("main_business") or "").strip()
    items = []
    for item in (raw.get("items") or [])[:4]:
        if not isinstance(item, dict) or not str(item.get("item") or "").strip():
            continue
        items.append({
            "item": item.get("item"),
            "sales_yi": item.get("sales_yi"),
            "share": item.get("share"),
            "yoy": item.get("yoy"),
            "gross_margin": item.get("gross_margin"),
        })
    if not text and not items:
        return None
    return {"main_business": text, "period": raw.get("period") or "", "items": items}


def _rows(snapshot: dict, metric: str, value, mid, items: list[dict]) -> list[dict]:
    provided = snapshot.get("table") or {}
    quotes = snapshot.get("quotes") or {}
    by_key = {item["key"]: item for item in items}
    fallback_value = {
        "pe_ttm": snapshot.get("pe_reference") if snapshot.get("pe_reference") is not None else quotes.get("pe_ttm"),
        "pb": value if metric == "pb" else quotes.get("pb"),
        "ps_ttm": value if metric == "ps_ttm" else quotes.get("ps_ttm"),
        "roe": (by_key.get("profitability") or {}).get("value"),
        "debt_to_assets": (by_key.get("solvency") or {}).get("value"),
        "ocf_to_np": (by_key.get("cash") or {}).get("value"),
    }
    fallback_mid = {
        "pb": mid if metric == "pb" else None,
        "ps_ttm": mid if metric == "ps_ttm" else None,
    }
    rows = []
    for key, label, fmt in _TABLE:
        item = provided.get(key) or {}
        row_fmt = item.get("format") or fmt
        digits = 2 if row_fmt == "times" else 4
        rows.append({
            "key": key,
            "label": item.get("label") or label,
            "value": _num(item.get("value"), fallback_value.get(key), digits),
            "peer_median": _num(item.get("peer_median"), fallback_mid.get(key), digits),
            "format": row_fmt,
        })
    return rows


def _facts(metric, speak, metric_text, label, value, pctile, mid, level, q_level, h_level, items, distorted, pe_value, cash_beats, q_note, red_line) -> dict:
    counter = counter_text(
        items,
        distorted=distorted,
        pe_value=pe_value,
        metric=metric,
        cash_beats_profit=cash_beats,
    )
    if red_line:
        counter = q_note
    return {
        "metric_text": metric_text,
        "speak": speak,
        "history_label": label,
        "value": value,
        "value_text": None if value is None else speak_multiple(value),
        "pctile_5y": pctile,
        "peer_median": mid,
        "gap_text": peer_gap_text(value, mid),
        "valuation_level": level,
        "quality_level": q_level,
        "holders_level": h_level,
        "support": "" if red_line else support_text(items),
        "counter": counter,
        "caveat": counter,
    }


def _display_name(stock: Stock, snapshot: dict) -> str:
    for candidate in (snapshot.get("stock_name"), stock.name):
        text = str(candidate or "").strip()
        if text and text not in (stock.code, stock.ts_code):
            return text
    return str(stock.name or stock.code)


def _risk(events: list[dict]) -> bool:
    for event in events:
        flag = event.get("risk_hit")
        if flag is True or (isinstance(flag, str) and flag.strip()):
            return True
    return False


def _pct_note(label: str, pctile) -> str:
    if pctile is None:
        return "历史样本不足"
    return f"{label} {int(round(pctile * 100))}% 分位"


def _valuation_note(label: str, pctile, distorted: bool, metric: str) -> str:
    note = _pct_note(label, pctile)
    if distorted and metric != "pe_ttm_deducted":
        return f"{note}，利润处在低位，市盈率失真"
    return note


def _meta(stock: Stock, state: str, price_day: date | None, report_end, industry: dict, reason: str) -> dict:
    code = industry.get("code") or None
    name = industry.get("name") or stock.industry
    count = industry.get("peer_count")
    excluded = industry.get("peers_excluded")
    block = {
        "code": code,
        "name": name,
        "peer_count": None if count is None else int(count),
        "peers_excluded": None if excluded is None else int(excluded),
    }
    return {
        "state": state,
        "as_of": price_day.isoformat() if price_day else "",
        "updated_at": _now(),
        "price_date": price_day.isoformat() if price_day else None,
        "report_period": period_code(report_end),
        "status_text": status_line(price_day, report_end, reason),
        "industry": None if state == "computing" else block,
        "rule_version": RULE_VERSION,
    }


def _empty(stock, state: str, price_day, reason: str, report_end=None, industry=None, name=None) -> dict:
    day = price_day if isinstance(price_day, date) else parse_ymd(price_day)
    return {
        "ts_code": stock.ts_code,
        "stock_name": name or stock.name,
        "meta": _meta(stock, state, day, report_end, industry or {}, reason),
        "verdict": None,
        "cells": None,
        "valuation": None,
        "quality": None,
        "holder_events": None,
        "table": None,
        "position_input": None,
        "disclaimer_text": None if state == "computing" else DISCLAIMER_TEXT,
    }


def _shell(stock: Stock, state: str, today: date) -> dict:
    return {
        "ts_code": stock.ts_code,
        "stock_name": stock.name,
        "meta": {
            "state": state,
            "as_of": "",
            "updated_at": _now(),
            "price_date": None,
            "report_period": None,
            "status_text": "正在整理基本面" if state == "computing" else "",
            "industry": None,
            "rule_version": RULE_VERSION,
        },
        "verdict": None,
        "cells": None,
        "valuation": None,
        "quality": None,
        "holder_events": None,
        "table": None,
        "position_input": None,
        "disclaimer_text": None,
    }


def _multiple(value):
    number = _ratio(value, 4)
    if number is None:
        return None
    return round(number, 2 if abs(number) < 10 else 1)


def _num(primary, fallback, digits: int):
    value = primary if primary is not None else fallback
    return _ratio(value, digits)


def _ratio(value, digits: int = 4):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return round(number, digits)


def _now() -> str:
    return datetime.now(_ZONE).isoformat(timespec="seconds")


def _claim(key: str) -> bool:
    with _inflight_lock:
        if key in _inflight:
            return False
        _inflight.add(key)
        return True


def _release(key: str) -> None:
    with _inflight_lock:
        _inflight.discard(key)
