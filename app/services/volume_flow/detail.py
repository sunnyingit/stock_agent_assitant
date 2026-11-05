"""组装量价资金详情的 8 个区块。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app import config
from app.tools import cache
from app.tools.stock import Stock, is_hk
from app.services.verdict_view import rule_fields, slim
from app.services.volume_flow.align import load_aligned
from app.services.volume_flow.copy import write_text
from app.services.volume_flow.follow import follow as follow_of, refine
from app.services.volume_flow.metrics import compute
from app.services.volume_flow.rules import (
    DEFAULT_WINDOW,
    DISCLAIMER_TEXT,
    FLOW_SOURCE,
    RULE_VERSION,
    STALE_DAYS,
    judge,
    position_hints,
)

FORMULA_TEXT = "相对量能 = 窗口日均成交量 ÷ 窗口之前 60 日均量（两段不重叠）；净流入率 = 大资金（超大单 + 大单）净流入 ÷ 成交额；强弱 = 净流入率和这只股票自己过去同长度窗口比，高于平时几个标准差。"


def build_detail(stock: Stock, window: int = DEFAULT_WINDOW, aligned: dict | None = None, today: date | None = None) -> dict:
    """不调用大模型：文字用模板，AI 解读由 app/flow_review 另外生成，路由层并进返回。"""
    if is_hk(stock):
        raise RuntimeError(f"{stock.ts_code} 是港股，暂无与 A 股相同的资金流数据")
    window = _window(window)
    today = today or date.today()
    cache_key = f"volume_flow:detail:{stock.ts_code}:{window}:{today.isoformat()}"
    if aligned is None:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    data = aligned if aligned is not None else load_aligned(stock)
    if not data.get("bars"):
        raise RuntimeError(f"没有查到 {stock.ts_code} 的日线数据")
    metrics = compute(data, window)
    stale = _stale(data.get("as_of") or "", today)
    verdict = judge(metrics, stale=stale)
    hints = verdict.get("hints") or position_hints(metrics["signals"])
    follow = None if stale else follow_of(metrics)
    verdict = refine(verdict, follow, metrics)
    text = write_text(metrics, verdict, follow)
    payload = {
        "ts_code": stock.ts_code,
        "stock_name": stock.name,
        "meta": _meta(data, window, stale, today),
        "verdict": {
            **rule_fields(verdict.get("lean") or "", text),
            # 下面三项只给综合判断读，对外响应里由 public() 去掉
            "lean": verdict.get("lean") or "",
            "level": verdict.get("level"),
            "certainty": verdict.get("certainty"),
        },
        "follow": _follow_public(follow),
        "disclaimer_text": DISCLAIMER_TEXT,
        "price_volume": {
            "bars": [_bar(row) for row in metrics["price"]["bars"]],
            "vol_base": metrics["price"]["vol_base"],
            "annotations": verdict.get("annotations") or [],
            "pattern": verdict.get("pattern") or "none",
            "up_down_vol_ratio": metrics["price"]["up_down_vol_ratio"],
            "turnover": _ratio(metrics["price"]["turnover"]),
            "turnover_pctile": _ratio(metrics["price"]["turnover_pctile"]),
            # 图单独用近 20 日，主窗口数字仍在 bars
            "chart": {
                "bars": [_bar(row) for row in (metrics.get("price_chart") or {}).get("bars") or []],
                "vol_base": (metrics.get("price_chart") or {}).get("vol_base"),
                "days": (metrics.get("price_chart") or {}).get("days") or 0,
            },
        },
        "flow_by_size": {
            "rows": metrics["flow"]["rows"],
            "concentration": metrics["flow"]["concentration"],
            "pattern": verdict.get("flow_pattern") or "",
            # 净流入率和自己过去的同长度窗口比：z 是高于平时几个标准差，state 是据此分的五档
            "rate": _ratio(metrics["flow"].get("rate")),
            "rate_z": metrics["flow"].get("rate_z"),
            "rate_pctile": metrics["flow"].get("rate_pctile"),
            "state": verdict.get("flow_state") or "",
        },
        "flow_daily": {
            "days": metrics["daily"]["days"],
            "history": (metrics.get("history") or {}).get("days") or [],
            "trend": verdict.get("trend") or "",
        },
        "relative_strength": {
            **_relative_public(metrics["relative"]),
            # 3 / 5 / 20：用来区分「本股更强」还是「板块大盘一起涨」
            "windows": [
                {"days": row["days"], **_relative_public(row)}
                for row in metrics.get("relative_windows") or []
            ],
        },
        "position_hints": {
            "items": hints,
        },
        "windows": {
            "rows": [_window_row(row) for row in metrics["windows"]],
            "formula_text": FORMULA_TEXT,
        },
    }
    if aligned is None:
        cache.set(cache_key, payload, config.CACHE_TTL["volume_flow"])
    return payload


def public(payload: dict) -> dict:
    """对外响应：verdict 只留和 headline 同一套的四个字段，位置提示也不对外。
    build_detail 的完整结果是给综合判断和 AI 体检读的（lean/level/certainty、position_hints）。"""
    body = {key: value for key, value in payload.items() if key != "position_hints"}
    return {**body, "verdict": slim(payload["verdict"])}


def _follow_public(block: dict | None) -> dict | None:
    """有没有人跟：结论一句话在 verdict.text 里，这里只给档位和各项证据。"""
    if block is None:
        return None
    items = [{key: item[key] for key in ("key", "name", "status", "text")} for item in block["items"]]
    return {"level": block["level"], "label": block["label"], "move": block["move"], "items": items}


def _meta(data: dict, window: int, stale: bool, today: date) -> dict:
    as_of = data.get("as_of") or ""
    stamp = as_of[5:10] if len(as_of) >= 10 else as_of
    if stale:
        state = "expired"
        status = f"数据停在 {stamp} · 已过期"
    elif as_of < today.isoformat():
        state = "ok"
        status = f"昨收 · {stamp}"
    else:
        state = "ok"
        status = f"收盘 · {stamp}"
    return {
        "state": state,
        "as_of": as_of,
        "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "status_text": status,
        "window": window,
        "sources": {"price_volume": "交易所行情", "flow": FLOW_SOURCE},
        "rule_version": RULE_VERSION,
    }


def _ratio(value):
    if value is None:
        return None
    return round(float(value) / 100, 6)


def _bar(row: dict) -> dict:
    return {**row, "chg": _ratio(row.get("chg"))}


def _ret_bar(row: dict) -> dict:
    return {**row, "ret": _ratio(row.get("ret"))}


def _relative_public(block: dict) -> dict:
    """相对强弱一块：主窗口和多窗口共用同一套字段。"""
    return {
        "bars": [_ret_bar(row) for row in (block["stock"], block["sector"], block["index"])],
        "sector_flow": block.get("sector_flow") or "",
        "net_rate_vs_sector": _ratio(block.get("net_rate_vs_sector")),
    }


def _window_row(row: dict) -> dict:
    return {**row, "ret": _ratio(row.get("ret")), "net_rate": _ratio(row.get("net_rate"))}


def _stale(as_of: str, today: date) -> bool:
    try:
        start = datetime.strptime(as_of, "%Y-%m-%d").date()
    except ValueError:
        return True
    if today <= start:
        return False
    gap = 0
    cursor = start
    while cursor < today:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            gap += 1
    return gap > STALE_DAYS


def _window(value: int) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        return DEFAULT_WINDOW
    if days < 1:
        return DEFAULT_WINDOW
    return min(days, 60)
