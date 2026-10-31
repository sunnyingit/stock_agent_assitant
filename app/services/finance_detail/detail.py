"""组装财报详情。代码只算事实和规则版结论（给综合判断用）；AI 解读在 app/finance_review，由路由并进返回。

返回结构和资金流、基本面一致：verdict（规则）、cells、meta，headline / review 由路由加。
"""

from __future__ import annotations

from datetime import date, datetime
from threading import Lock
from zoneinfo import ZoneInfo

from app import config
from app.services.finance_detail import metrics as metrics_mod
from app.services.finance_detail import rules
from app.services.finance_detail.config import THRESHOLD_VERSION, thresholds
from app.services.finance_detail.copy import write_text
from app.services.verdict_view import rule_fields, slim
from app.tools import cache
from app.tools.stock import Stock, is_hk

_ZONE = ZoneInfo("Asia/Shanghai")
_inflight: set[str] = set()
_inflight_lock = Lock()
INTERNAL_FIELDS = ("inputs",)


def build_detail(stock: Stock, snapshot: dict | None = None, today: date | None = None) -> dict:
    today = today or datetime.now(_ZONE).date()
    if is_hk(stock) and snapshot is None:
        return _empty(stock, "insufficient", today, "港股暂不按这套财报口径")
    cache_key = f"finance_detail:{stock.ts_code}:{today.isoformat()}"
    live = snapshot is None
    if live:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        if not _claim(cache_key):
            return _empty(stock, "computing", today, "")
    try:
        data = snapshot if snapshot is not None else _load(stock, today)
        payload = _assemble(stock, data, today)
        if live and payload["meta"]["state"] != "computing":
            cache.set(cache_key, payload, config.CACHE_TTL["finance_detail"])
        return payload
    finally:
        if live:
            _release(cache_key)


def failed_payload(stock: Stock, message: str, today: date | None = None) -> dict:
    payload = _empty(stock, "failed", today or datetime.now(_ZONE).date(), "数据暂时取不到")
    payload["error"] = message
    return payload


def public(payload: dict) -> dict:
    """对外响应：verdict 只留和 headline 同一套的四个字段，中间量不对外。
    build_detail 的完整结果是给综合判断和 AI 体检读的（lean/level/certainty、inputs）。"""
    body = {key: value for key, value in payload.items() if key not in INTERNAL_FIELDS}
    verdict = payload.get("verdict")
    return {**body, "verdict": None if verdict is None else slim(verdict)}


def _load(stock: Stock, today: date) -> dict:
    from app.services.finance_detail.load import load_snapshot

    return load_snapshot(stock, today)


def _assemble(stock: Stock, snapshot: dict, today: date) -> dict:
    m = metrics_mod.compute(snapshot, today)
    if m is None:
        return _empty(stock, "insufficient", today, "没有取到已披露的财报")
    if m["industry_rule"] == "finance":
        return _empty(stock, "insufficient", today, "金融业报表科目不同，暂不适用这套财报口径", snapshot.get("stock_name"))
    th = thresholds(m["industry_rule"])
    lean, level, certain = rules.verdict(m, th)
    t = m["time"]
    hard = rules.bounds(m, th)
    return {
        "ts_code": stock.ts_code,
        "stock_name": snapshot.get("stock_name") or stock.name,
        "meta": {
            "state": "ok",
            "as_of": today.isoformat(),
            "updated_at": _now(),
            "report": {"period": m["report_end"], "label": rules.period_label(m), "announced": t["announced"], "days_since": t["days_since"]},
            "next_disclosure": {"date": t["next_date"], "days_left": t["days_to_next"], "source": t["next_source"]},
            "status_text": _status(m),
            "industry_rule": m["industry_rule"],
            "rule_version": rules.RULE_VERSION,
            "threshold_version": THRESHOLD_VERSION,
        },
        "verdict": {
            **rule_fields(lean, write_text(m)),
            # 下面三项只给综合判断读，对外响应里由 public() 去掉
            "lean": lean,
            "level": level,
            "certainty": certain,
        },
        "cells": rules.cells(m, th),
        "quarters": m["quarters"],
        "disclaimer_text": rules.DISCLAIMER_TEXT,
        # 给 AI 体检和综合判断读的完整指标，对外响应里由 public() 去掉
        "inputs": {
            "metrics": m,
            "thresholds": th,
            "warn_hits": sum(1 for row in hard.values() if row["force_warn"]),   # 命中"必须判需警惕"的硬阈值数
            "red_line": bool(t["audit"] and not t["audit"]["standard"]),         # 年报审计意见不是标准无保留
            "time_weight": rules.time_weight(m, th),                             # 综合判断按距披露的天数调这一维的权重
        },
    }


def _status(m: dict) -> str:
    t = m["time"]
    stamp = (t["announced"] or "")[5:10]
    return f"{rules.period_label(m)} · {stamp} 披露" + (f" · 距今 {t['days_since']} 天" if t["days_since"] is not None else "")


def _empty(stock: Stock, state: str, today: date, reason: str, name: str | None = None) -> dict:
    return {
        "ts_code": stock.ts_code,
        "stock_name": name or stock.name,
        "meta": {
            "state": state,
            "as_of": today.isoformat() if state != "computing" else "",
            "updated_at": _now(),
            "report": None,
            "next_disclosure": None,
            "status_text": "正在整理财报" if state == "computing" else reason,
            "industry_rule": None,
            "rule_version": rules.RULE_VERSION,
            "threshold_version": THRESHOLD_VERSION,
        },
        "verdict": None,
        "cells": None,
        "quarters": None,
        "disclaimer_text": None if state == "computing" else rules.DISCLAIMER_TEXT,
    }


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
