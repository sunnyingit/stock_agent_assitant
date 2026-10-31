"""综合判断测试用的合成数据：一只上涨中的股票，5 个维度的详情，以及可编排输出的假模型。"""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.volume_flow import build_detail as volume_flow_detail
from app.tools.stock import Stock

NOW = datetime(2026, 10, 29, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))   # 周四，收盘后
TODAY = NOW.date()


def make_stock() -> Stock:
    return Stock(ts_code="600001.SH", code="600001", name="测试股份", quote_id="", em_code="", industry="白酒")


def workdays_back(end: date, count: int) -> list[str]:
    days, cursor = [], end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return list(reversed(days))


def _closes() -> list[float]:
    """前 240 天缓慢爬升到 19；之后 20 天冲高到 25；再 20 天回落到 18；最后 20 天每天涨 1%。"""
    closes = [18 + 1.0 * i / 240 for i in range(240)]
    closes += [19 + 6.0 * (i + 1) / 20 for i in range(20)]
    closes += [25 - 7.0 * (i + 1) / 20 for i in range(20)]
    for _ in range(20):
        closes.append(closes[-1] * 1.01)
    return closes


def make_aligned(closes=None, last_volume=2_000_000.0, big_buy=2e6) -> dict:
    closes = closes or _closes()
    dates = workdays_back(TODAY, len(closes))
    bars, flow, sector, index = [], [], [], []
    for i, close in enumerate(closes):
        chg = 0.0 if i == 0 else (close / closes[i - 1] - 1) * 100
        recent = i >= len(closes) - 20
        volume = last_volume if recent else 1_000_000.0
        bars.append({
            "date": dates[i], "close": round(close, 4), "volume": volume,
            "amount": close * volume, "chg": round(chg, 4), "turnover": 1.0,
        })
        flow.append({
            "date": dates[i],
            "super_large": big_buy if recent else 1e6, "large": big_buy / 2 if recent else 5e5,
            "medium": -5e5 if recent else -1e5, "small": -1e5,
        })
        sector.append({"date": dates[i], "chg": 0.2, "net": 100.0, "rate": 2.0})
        index.append({"date": dates[i], "chg": 0.1})
    return {
        "name": "测试股份", "as_of": dates[-1], "bars": bars, "flow": flow,
        "sector": {"name": "白酒Ⅱ", "rows": sector}, "index": {"name": "沪深300", "rows": index},
        "margin": [], "margin_percentile": None,
    }


def volume_payload(aligned: dict | None = None, stock=None) -> dict:
    return volume_flow_detail(stock or make_stock(), 20, aligned=aligned or make_aligned(), today=TODAY)


def news_payload(impact="positive", priced="not_yet", state="ok", lean="bullish", level=2) -> dict:
    event = {
        "role": "main", "category": "earnings", "source_grade": "A", "impact": impact, "magnitude": "large",
        "scope": "company", "published_at": "2026-10-27T18:30:00+08:00", "publish_session": "post", "basis": "fact",
    }
    return {
        "meta": {"state": state, "as_of": TODAY.isoformat(), "rule_version": "news-1"},
        "verdict": {"lean": lean, "level": level, "certainty": "high"},
        "key_events": [event], "reaction": {"priced_in": priced}, "heat": {"risk_hit": False},
    }


def sector_payload(stage="main", lean="bullish", level=2, state="ok") -> dict:
    return {
        "meta": {"state": state, "as_of": TODAY.isoformat(), "rule_version": "sector-2"},
        "verdict": {"lean": lean, "level": level, "certainty": "medium"},
        "sectors": {
            "primary": {
                "stage": {"current": stage, "days_in_round": 6},
                "role": {"value": "leader"},
                "strength": {"lines": {"dates": [], "stock": [0.05, 0.12], "sector": [0.03, 0.08], "index": [0.01, 0.02]}},
                "position_hint": {"climax": False, "overdue": False},
            },
            "secondary": None,
            "driver": {"type": "single"},
            "divergence": {"active": False},
        },
    }


def finance_payload(red=False, lean="bullish", level=1, days_left=40, time_weight=1.0) -> dict:
    return {
        "meta": {
            "state": "ok", "as_of": TODAY.isoformat(), "rule_version": "finance-2",
            "report": {"days_since": 12}, "next_disclosure": {"days_left": days_left},
        },
        "verdict": {"lean": lean, "level": level, "certainty": "medium"},
        "cells": [{"key": "growth", "level": "up"}, {"key": "quality", "level": "good"}, {"key": "expectation", "level": "na"}],
        "inputs": {"warn_hits": 0, "red_line": red, "time_weight": time_weight},
    }


def fundamental_payload(lean="neutral", level=0) -> dict:
    return {
        "meta": {"state": "ok", "as_of": TODAY.isoformat(), "rule_version": "fundamental-1"},
        "verdict": {"lean": lean, "level": level, "certainty": "medium"},
        "cells": [
            {"key": "valuation", "level": "fair"},
            {"key": "quality", "level": "medium"},
            {"key": "holders", "level": "quiet"},
        ],
        "valuation": {"pctile_5y": 0.55},
        "holder_events": [{"type": "unlock", "impact": "negative", "days_left": 12}],
    }


def loaders(aligned=None, **overrides) -> dict:
    """默认是"多个维度偏多"的场景；overrides 里放 {维度: 载荷 或 抛异常的可调用}。"""
    aligned = aligned or make_aligned()
    payloads = {
        "volume_flow": lambda: volume_payload(aligned),
        "news": news_payload,
        "sector": sector_payload,
        "finance": finance_payload,
        "fundamental": fundamental_payload,
    }
    for key, value in overrides.items():
        payloads[key] = value if callable(value) else (lambda payload=value: payload)
    return payloads


class ScriptedModel:
    """按角色返回预设输出的假模型。chief 默认采信所有论据，方向跟随分差。"""

    def __init__(self, bull=None, bear=None, chief=None, fail=False):
        self.bull = bull if bull is not None else [
            {"claim": "量价形态为价涨量增", "cites": ["flow.pattern"], "weight": 2},
            {"claim": "板块处于主升阶段", "cites": ["sector.stage"], "weight": 2},
        ]
        self.bear = bear if bear is not None else [
            {"claim": "未来 20 个交易日内有解禁", "cites": ["fund.holder_event"], "weight": 1},
        ]
        self.chief = chief
        self.fail = fail
        self.calls = []

    def complete(self, system, user):
        if self.fail:
            raise RuntimeError("模型不可用")
        payload = json.loads(user)
        self.calls.append(system.split("】")[0])
        if "【角色：正方】" in system:
            return {"points": self.bull}
        if "【角色：反方】" in system:
            return {"points": self.bear}
        return self._chief(payload)

    def _chief(self, payload):
        arguments = payload.get("arguments") or []
        spread = sum(item["weight"] * (1 if item["ref"].startswith("bull") else -1) for item in arguments)
        direction = "偏多" if spread >= 2 else "偏空" if spread <= -2 else "不明"
        candidates = payload.get("candidates") or []
        down = next((item for item in candidates if item["dir"] == "down"), None)
        up = next((item for item in candidates if item["dir"] == "up"), None)
        levels = {}
        if down:
            levels["down"] = {"base": down["id"], "buffer": "0%", "confirm": "close", "why": "量价形态仍在", "cites": ["flow.pattern"]}
        if up:
            levels["up"] = {"base": up["id"], "confirm": "close", "meaning": "stronger", "why": "板块处于主升", "cites": ["sector.stage"]}
        output = {
            "direction": direction, "position": "适中",
            "headline": "量价和板块都在推",
            "direction_text": "量价资金和板块支持，解禁是不支持的一面",
            "direction_cites": ["flow.pattern", "sector.stage"],
            "position_text": "收盘价离一年高点还有一段距离",
            "position_cites": ["market.from_year_high"],
            "verdicts": [{"ref": item["ref"], "adopt": True, "weight": item["weight"], "reason": ""} for item in arguments],
            "conflict_note": {"text": "", "cites": []},
            "postmortem": {"text": "如果解禁后抛压超预期，判断可能出错", "cites": ["fund.holder_event"]},
            "levels": levels,
            "watch_ids": [item["id"] for item in (payload.get("events") or [])[:1]],
        }
        if self.chief:
            output = self.chief(output, payload) or output
        return output
