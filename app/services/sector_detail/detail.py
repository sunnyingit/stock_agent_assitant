"""组装板块详情。方向只看主板块，指定 sector_code 时其余块改看该板块。"""

from __future__ import annotations

from datetime import date, datetime
from threading import Lock
from zoneinfo import ZoneInfo

from app import config
from app.tools import cache
from app.tools.stock import Stock, is_hk
from app.services.sector_detail.copy import finish_text
from app.services.sector_detail.rules import (
    DISCLAIMER,
    RULE_VERSION,
    arrange,
    evaluate,
    pearson,
    relative_verdict,
)

_ZONE = ZoneInfo("Asia/Shanghai")
_inflight: set[str] = set()
_inflight_lock = Lock()


def build_detail(
    stock: Stock,
    sector_code: str | None = None,
    snapshot: dict | None = None,
    model=None,
    today: date | None = None,
) -> dict:
    today = today or datetime.now(_ZONE).date()
    sector_code = (sector_code or "").strip() or None
    if snapshot is None and is_hk(stock):
        return _insufficient(stock, today, "港股暂不按这套板块口径比较")
    live = snapshot is None
    snap_key = f"sector_snap:{stock.ts_code}:{today.isoformat()}"
    if live:
        snapshot = cache.get(snap_key)
        if snapshot is None:
            if not _claim(snap_key):
                return _computing(stock)
            try:
                from app.services.sector_detail.load import load_snapshot

                snapshot = load_snapshot(stock, today)
                if snapshot.get("sectors"):
                    cache.set(snap_key, snapshot, config.CACHE_TTL["sector_detail"])
                elif snapshot.get("listed_days", 0) >= 20:
                    cache.set(snap_key, snapshot, 120)
            finally:
                _release(snap_key)
    return _assemble(stock, snapshot or {}, sector_code, model, today)


def failed_payload(stock: Stock, message: str, today: date | None = None) -> dict:
    payload = _computing(stock)
    today = today or datetime.now(_ZONE).date()
    payload["meta"]["state"] = "failed"
    payload["meta"]["status_text"] = "数据暂时取不到"
    payload["meta"]["as_of"] = today.isoformat()
    payload["meta"]["error"] = message
    return payload


def _assemble(stock: Stock, data: dict, sector_code: str | None, model, today: date) -> dict:
    if int(data.get("listed_days") or 0) < 20:
        return _insufficient(stock, today, "上市不足 20 个交易日")
    sectors = list(data.get("sectors") or [])
    if not sectors:
        return _insufficient(stock, today, "没有可比的行业或概念板块")
    _ensure_corr(sectors)
    main, top, independent = arrange(sectors)
    by_code = {item.get("code"): item for item in sectors}
    unknown = bool(sector_code and sector_code not in by_code)
    if sector_code and sector_code in by_code:
        viewed = by_code[sector_code]
    elif main is not None:
        viewed = main
    else:
        viewed = top[0]
    viewing_main = main is not None and viewed.get("code") == main.get("code")
    if independent:
        viewing_main = False
    body = evaluate(viewed, stock.ts_code)
    headline = body if main is None or viewed is main else evaluate(main, stock.ts_code)
    if independent:
        warns = [item["text"] for item in body["sustain_items"] if item["status"] == "warn"]
        verdict_bits = relative_verdict(body.get("gaps", {}).get("stock_vs_index"), warns)
    else:
        verdict_bits = headline
    text, ai_status = finish_text(
        verdict_bits["tag_text"],
        verdict_bits.get("gaps"),
        verdict_bits.get("counter_text") or "还没有看到反向信号",
        independent,
        model,
    )
    as_of = data.get("as_of") or (body["lines"]["dates"][-1] if body["lines"]["dates"] else None)
    state, status = _state(data, as_of, today)
    source = data.get("source_text") or _default_source(sectors)
    if body.get("flow_rate") is None and "资金流" not in source:
        source += "板块资金流这次没有取到。"
    if unknown:
        source = f"{source}未找到指定板块，仍显示主板块。"
    payload = _shell(state, status, as_of)
    payload["meta"]["viewing_main"] = viewing_main
    payload["meta"]["source_text"] = source
    payload["verdict"] = {
        "lean": verdict_bits["lean"],
        "level": verdict_bits["level"],
        "certainty": verdict_bits["certainty"],
        "certainty_reasons": list(verdict_bits["certainty_reasons"]),
        "tag_text": verdict_bits["tag_text"],
        "ai_text": text,
        "ai_status": ai_status,
    }
    payload["sectors"] = {
        "main": None if main is None else _brief(main),
        "candidates": [
            {
                **_brief(item),
                "is_main": main is not None and item.get("code") == main.get("code"),
                "is_viewing": item.get("code") == viewed.get("code"),
            }
            for item in top
        ],
        "independent": independent,
    }
    payload["strength"] = {
        "lines": body["lines"],
        "round_start": body["round_start"],
        "rank": body["rank"],
        "flow_rate": body["flow_rate"],
        "turnover_share": body["turnover_share"],
        "chart_alt_text": _chart_alt(viewed.get("name") or "板块", body["lines"]),
    }
    payload["stage"] = {
        "current": body["stage"],
        "confirmed_days": body["confirmed_days"],
        "days_in_round": body["days_in_round"],
        "signals": body["signals"],
        "why_text": body["why_text"],
        "next_text": body["next_text"],
    }
    payload["role"] = {
        "value": body["role"],
        "reason_text": body["reason_text"],
        "role_text": body["role_text"],
        "self_rank": body["self_rank"],
        "member_count": body["member_count"],
        "top": body["top"],
    }
    payload["sustain"] = {"items": body["sustain_items"]}
    payload["watch_next"] = body["watch_next"]
    payload["position_hint"] = body["position_hint"]
    payload["disclaimer_text"] = DISCLAIMER
    return payload


def _ensure_corr(sectors: list[dict]) -> None:
    for item in sectors:
        if item.get("corr_20d") is not None:
            continue
        days = item.get("days") or []
        item["corr_20d"] = pearson(
            [day.get("stock") for day in days],
            [day.get("sector") for day in days],
        )


def _brief(sector: dict) -> dict:
    corr = sector.get("corr_20d")
    return {
        "code": sector.get("code"),
        "name": sector.get("name"),
        "type": sector.get("type"),
        "corr_20d": None if corr is None else round(float(corr), 2),
    }


def _state(data: dict, as_of: str | None, today: date) -> tuple[str, str]:
    if data.get("intraday") is True:
        stamp = data.get("status_date") or as_of or today.isoformat()
        clock = data.get("clock") or ""
        return "intraday", f"盘中 · {_md(stamp)} {clock}".strip()
    if data.get("intraday") is None:
        now = datetime.now(_ZONE)
        session = now.weekday() < 5 and (now.hour, now.minute) < (15, 0)
        if session and not _stale(as_of, today):
            return "intraday", f"盘中 · {now.strftime('%m-%d %H:%M')}"
    if _stale(as_of, today):
        return "stale", f"数据停在 {_md(as_of)}"
    return "ok", f"数据截至 {_md(as_of)} 收盘"


def _stale(as_of: str | None, today: date) -> bool:
    parsed = _parse(as_of)
    if parsed is None:
        return False
    return (today - parsed).days > 7


def _md(value: str | None) -> str:
    parsed = _parse(value)
    if parsed is None:
        return "--"
    return f"{parsed.month:02d}-{parsed.day:02d}"


def _parse(value: str | None) -> date | None:
    if not value:
        return None
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _chart_alt(name: str, lines: dict) -> str:
    return (
        f"{name}近 20 日累计：板块 {_signed(lines.get('sector'))}，"
        f"个股 {_signed(lines.get('stock'))}，沪深300 {_signed(lines.get('index'))}"
    )


def _signed(series) -> str:
    if not series:
        return "—"
    value = series[-1]
    if value is None:
        return "—"
    return f"{float(value) * 100:.1f}%"


def _default_source(sectors: list[dict]) -> str:
    concepts = any(item.get("type") == "concept" for item in sectors)
    text = (
        "板块走势按当前成分股等权计算，不用发布的指数点位。"
        "成分没有历史进出记录，概念板块的结论没有回测依据。"
        "行业板块在同级申万发布指数里排名，三级不和一级混比。相关性相差不到 0.05 时，优先用行业板块。"
    )
    if concepts:
        text += "概念板块沿用相关性最高的行业板块在同级里的排名。"
    else:
        text += "这次没有取到概念板块。"
    text += "板块资金流这次没有取到。"
    return text


def _shell(state: str, status: str, as_of: str | None) -> dict:
    return {
        "meta": {
            "state": state,
            "as_of": as_of,
            "updated_at": datetime.now(_ZONE).isoformat(timespec="seconds"),
            "rule_version": RULE_VERSION,
            "status_text": status,
            "viewing_main": None,
            "source_text": None,
            "error": None,
        },
        "verdict": None,
        "sectors": None,
        "strength": None,
        "stage": None,
        "role": None,
        "sustain": None,
        "watch_next": None,
        "position_hint": None,
        "disclaimer_text": None,
    }


def _computing(stock: Stock) -> dict:
    del stock
    return _shell("computing", "正在整理板块", None)


def _insufficient(stock: Stock, today: date, status: str) -> dict:
    del stock
    payload = _shell("insufficient", status, today.isoformat())
    return payload


def _claim(key: str) -> bool:
    with _inflight_lock:
        if key in _inflight:
            return False
        _inflight.add(key)
        return True


def _release(key: str) -> None:
    with _inflight_lock:
        _inflight.discard(key)
