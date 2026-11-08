"""资金流 Facts Tools：薄封装 volume_flow 算法，供模型 tool-calling 与 facts 共用。

算法与权重写死在服务端；模型只可传白名单枚举（horizon / window / lookback / days）。
ts_code / as_of 由会话注入，模型不传代码。
"""

from __future__ import annotations

from typing import Any

from app.services.volume_flow import metrics as flow_metrics
from app.services.volume_flow.rules import (
    DEFAULT_WINDOW,
    DIAGNOSIS_MAX_CHARS,
    PATH_LOOKBACK_SHORT,
    PATH_LOOKBACK_SWING,
    PATH_TEXT,
    PRICE_FLOW_TEXT,
    RULE_VERSION,
    STATE_TEXT,
    flow_path_detail,
    flow_state,
    flow_structure,
    price_flow_relation,
)

WINDOWS = frozenset({3, 5, 10, 20})
LOOKBACKS = frozenset({10, 20})
DAYS_OPTS = frozenset({30, 60, 120})
BASE_WINDOWS = frozenset({3, 5, 10})
HORIZONS = {
    "short": {"window": 3, "lookback": PATH_LOOKBACK_SHORT},
    "swing": {"window": 20, "lookback": PATH_LOOKBACK_SWING},
}

# 诊断摘要 ≤50 字；reply 场景不卡此上限（由调用方 mode 区分）
OUTPUT_MODES = frozenset({"diagnosis", "reply"})


class ToolError(ValueError):
    """非法枚举或缺少数据。"""


def resolve_horizon(horizon: str | None = None, window: int | None = None, lookback: int | None = None) -> dict:
    """horizon=short → window=3；也可显式传 window/lookback（须在白名单）。"""
    hz = (horizon or "short").strip().lower()
    if hz not in HORIZONS:
        raise ToolError(f"horizon 只能是 short/swing，收到 {horizon!r}")
    defaults = HORIZONS[hz]
    win = defaults["window"] if window is None else int(window)
    lb = defaults["lookback"] if lookback is None else int(lookback)
    if win not in WINDOWS:
        raise ToolError(f"window 只能是 {sorted(WINDOWS)}，收到 {window!r}")
    if lb not in LOOKBACKS:
        raise ToolError(f"lookback 只能是 {sorted(LOOKBACKS)}，收到 {lookback!r}")
    return {"horizon": hz, "window": win, "lookback": lb}


def _flow_map(aligned: dict) -> dict:
    return {row["date"]: row for row in aligned.get("flow") or [] if row.get("date")}


def _wrap(payload: dict) -> dict:
    return {**payload, "rule_version": RULE_VERSION}


def get_flow_daily(aligned: dict, days: int | None = None) -> dict:
    n = 60 if days is None else int(days)
    if n not in DAYS_OPTS:
        raise ToolError(f"days 只能是 {sorted(DAYS_OPTS)}，收到 {days!r}")
    bars = aligned.get("bars") or []
    chosen = bars[-n:]
    daily = flow_metrics._daily(chosen, _flow_map(aligned))
    return _wrap({"days": daily["days"], "in_days": daily["in_days"], "out_days": daily["out_days"], "n": len(daily["days"])})


def compute_flow_path(aligned: dict, lookback: int | None = None, horizon: str | None = None) -> dict:
    resolved = resolve_horizon(horizon=horizon, lookback=lookback)
    bars = aligned.get("bars") or []
    daily = flow_metrics._daily(bars[-(resolved["lookback"] + 5):] or bars, _flow_map(aligned))
    path = flow_path_detail(daily["days"], lookback=resolved["lookback"])
    return _wrap({**path, "horizon": resolved["horizon"]})


def compute_flow_level(aligned: dict, window: int | None = None, horizon: str | None = None) -> dict:
    resolved = resolve_horizon(horizon=horizon, window=window)
    bars = aligned.get("bars") or []
    ctx = flow_metrics._rate_context(bars, _flow_map(aligned), resolved["window"])
    state = flow_state(ctx.get("rate_z"))
    return _wrap({
        "horizon": resolved["horizon"],
        "window": resolved["window"],
        "rate": ctx.get("rate"),
        "z": ctx.get("rate_z"),
        "pctile": ctx.get("rate_pctile"),
        "n": ctx.get("rate_n"),
        "method": ctx.get("rate_z_method") or "",
        "state": state,
        "state_text": STATE_TEXT.get(state, ""),
    })


def compute_flow_structure(aligned: dict, window: int | None = None, horizon: str | None = None) -> dict:
    resolved = resolve_horizon(horizon=horizon, window=window)
    bars = (aligned.get("bars") or [])[-resolved["window"]:]
    flow = _flow_map(aligned)
    totals = {size: 0.0 for size in ("super_large", "large", "medium", "small")}
    seen = {size: False for size in totals}
    for bar in bars:
        row = flow.get(bar["date"]) or {}
        for size in totals:
            if row.get(size) is None:
                continue
            totals[size] += float(row[size])
            seen[size] = True
    elg = totals["super_large"] if seen["super_large"] else None
    lg = totals["large"] if seen["large"] else None
    md = totals["medium"] if seen["medium"] else None
    structure = flow_structure(elg, lg, md)
    return _wrap({
        "horizon": resolved["horizon"],
        "window": resolved["window"],
        "tiers": {
            "super_large": None if elg is None else round(elg, 2),
            "large": None if lg is None else round(lg, 2),
            "medium": None if md is None else round(md, 2),
            "small": None if not seen["small"] else round(totals["small"], 2),
        },
        **structure,
    })


def compare_price_flow(aligned: dict, window: int | None = None, horizon: str | None = None) -> dict:
    resolved = resolve_horizon(horizon=horizon, window=window)
    level = compute_flow_level(aligned, window=resolved["window"], horizon=resolved["horizon"])
    bars = (aligned.get("bars") or [])[-resolved["window"]:]
    ret = flow_metrics._cumulative([row.get("chg") for row in bars])
    label = price_flow_relation(ret, level.get("state") or "")
    return _wrap({
        "horizon": resolved["horizon"],
        "window": resolved["window"],
        "ret": None if ret is None else round(ret, 2),
        "state": level.get("state") or "",
        "label": label,
        "text": PRICE_FLOW_TEXT.get(label, label),
    })


def summarize_flow_windows(aligned: dict, windows: list[int] | None = None) -> dict:
    chosen = list(windows) if windows else [3, 5, 10, 20]
    if any(days not in WINDOWS for days in chosen):
        raise ToolError(f"windows 子集只能来自 {sorted(WINDOWS)}")
    rows = []
    for days in chosen:
        level = compute_flow_level(aligned, window=days)
        rows.append({
            "window": days,
            "rate": level.get("rate"),
            "z": level.get("z"),
            "state": level.get("state") or "",
            "state_text": level.get("state_text") or "",
        })
    pace = "mixed"
    by = {row["window"]: row for row in rows}
    short, mid = by.get(3), by.get(5) or by.get(10)
    if short and mid and short.get("z") is not None and mid.get("z") is not None:
        if short["z"] > mid["z"] + 0.3:
            pace = "accelerating"
        elif short["z"] < mid["z"] - 0.3:
            pace = "slowing"
        else:
            pace = "steady"
    return _wrap({"rows": rows, "pace": pace})


def get_tier_breakdown(aligned: dict, window: int | None = None, horizon: str | None = None) -> dict:
    structure = compute_flow_structure(aligned, window=window, horizon=horizon)
    return _wrap({
        "window": structure["window"],
        "tiers": structure["tiers"],
        "note": "四档净额合计约等于 0；主方向看超大单/大单加权，中单只调把握，小单不进结论",
        "direction": structure["direction"],
        "text": structure["text"],
    })


def get_sector_flow_context(aligned: dict, window: int | None = None, horizon: str | None = None) -> dict:
    resolved = resolve_horizon(horizon=horizon, window=window)
    bars = (aligned.get("bars") or [])[-resolved["window"]:]
    flow = _flow_map(aligned)
    sector = {row["date"]: row for row in ((aligned.get("sector") or {}).get("rows") or []) if row.get("date")}
    index = {row["date"]: row for row in ((aligned.get("index") or {}).get("rows") or []) if row.get("date")}
    relative = flow_metrics._relative(bars, flow, sector, index, aligned)
    return _wrap({
        "window": resolved["window"],
        "horizon": resolved["horizon"],
        "stock_ret": (relative.get("stock") or {}).get("ret"),
        "sector_name": (relative.get("sector") or {}).get("name") or "",
        "sector_ret": (relative.get("sector") or {}).get("ret"),
        "index_name": (relative.get("index") or {}).get("name") or "",
        "index_ret": (relative.get("index") or {}).get("ret"),
        "sector_flow": relative.get("sector_flow") or "",
        "net_rate_vs_sector": relative.get("net_rate_vs_sector"),
    })


def get_last_day_focus(aligned: dict, base_window: int | None = None) -> dict:
    base = DEFAULT_WINDOW if base_window is None else int(base_window)
    if base not in BASE_WINDOWS:
        raise ToolError(f"base_window 只能是 {sorted(BASE_WINDOWS)}，收到 {base_window!r}")
    bars = aligned.get("bars") or []
    if not bars:
        raise ToolError("没有日线")
    flow = _flow_map(aligned)
    last = bars[-1]
    row = flow.get(last["date"]) or {}
    elg, lg = row.get("super_large"), row.get("large")
    last_net = None if elg is None and lg is None else (elg or 0) + (lg or 0)
    window_bars = bars[-base:]
    window_net = 0.0
    seen = False
    for bar in window_bars:
        item = flow.get(bar["date"]) or {}
        a, b = item.get("super_large"), item.get("large")
        if a is None and b is None:
            continue
        window_net += (a or 0) + (b or 0)
        seen = True
    share = None
    if last_net is not None and seen and window_net != 0:
        share = round(last_net / window_net, 4)
    dominant = share is not None and abs(share) >= 0.6
    return _wrap({
        "base_window": base,
        "date": last.get("date"),
        "last_net": None if last_net is None else round(last_net, 2),
        "window_net": None if not seen else round(window_net, 2),
        "share": share,
        "single_day_dominant": dominant,
        "text": "最近一日占短窗份额偏高，勿一日定乾坤" if dominant else "最近一日未单独主导短窗",
    })


def diagnosis_headline(path: dict, level: dict, structure: dict, price: dict, *, mode: str = "diagnosis") -> str:
    """页头一句话。diagnosis ≤50 字；reply 可更长。"""
    if mode not in OUTPUT_MODES:
        raise ToolError(f"mode 只能是 diagnosis/reply，收到 {mode!r}")
    bits = [
        path.get("text") or PATH_TEXT.get(path.get("label") or "", ""),
        level.get("state_text") or "",
        (structure.get("text") or "").replace("加权后", ""),
        price.get("text") or "",
    ]
    text = "；".join(bit for bit in bits if bit)
    if mode == "diagnosis":
        return text[:DIAGNOSIS_MAX_CHARS]
    return text


# 给模型网关用的 JSON Schema 摘要（实现仍走上面的函数）
TOOL_SPECS: list[dict[str, Any]] = [
    {"name": "get_flow_daily", "params": {"days": "30|60|120"}},
    {"name": "compute_flow_path", "params": {"horizon": "short|swing", "lookback": "10|20"}},
    {"name": "compute_flow_level", "params": {"horizon": "short|swing", "window": "3|5|10|20"}},
    {"name": "compute_flow_structure", "params": {"horizon": "short|swing", "window": "3|5|10|20"}},
    {"name": "compare_price_flow", "params": {"horizon": "short|swing", "window": "3|5|10|20"}},
    {"name": "summarize_flow_windows", "params": {"windows": "subset of 3,5,10,20"}},
    {"name": "get_tier_breakdown", "params": {"horizon": "short|swing", "window": "3|5|10|20"}},
    {"name": "get_sector_flow_context", "params": {"horizon": "short|swing", "window": "3|5|10|20"}},
    {"name": "get_last_day_focus", "params": {"base_window": "3|5|10"}},
]

DISPATCH = {
    "get_flow_daily": get_flow_daily,
    "compute_flow_path": compute_flow_path,
    "compute_flow_level": compute_flow_level,
    "compute_flow_structure": compute_flow_structure,
    "compare_price_flow": compare_price_flow,
    "summarize_flow_windows": summarize_flow_windows,
    "get_tier_breakdown": get_tier_breakdown,
    "get_sector_flow_context": get_sector_flow_context,
    "get_last_day_focus": get_last_day_focus,
}


def call_tool(name: str, aligned: dict, **kwargs) -> dict:
    fn = DISPATCH.get(name)
    if fn is None:
        raise ToolError(f"未知 tool: {name}")
    return fn(aligned, **kwargs)
