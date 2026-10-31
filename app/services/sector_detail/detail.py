"""组装板块详情：本股属于哪两个板块、各自处在什么阶段、两个板块各驱动本股多少，合成一个方向。

方向由规则算出（给综合判断用）；AI 解读在 app/sector_review，由路由并进返回。
完整结果带 inputs（两个板块各自的 lean / level / certainty）和 verdict 里的内部字段，public() 把它们去掉。
"""

from __future__ import annotations

from datetime import date, datetime
from threading import Lock
from zoneinfo import ZoneInfo

from app import config
from app.services.sector_detail.copy import write_text
from app.services.sector_detail.rules import DISCLAIMER, RULE_VERSION, combine, evaluate, relative_verdict
from app.services.verdict_view import rule_fields, slim
from app.tools import cache
from app.tools.stock import Stock, is_hk

_ZONE = ZoneInfo("Asia/Shanghai")
_inflight: set[str] = set()
_inflight_lock = Lock()
INTERNAL_FIELDS = ("inputs",)
DRIVER_TEXT = {"dual": "双驱动", "single": "单驱动", "independent": "走势独立", "unknown": "暂无法判断"}
ROLE_TEXT = {"primary": "主板块", "secondary": "次板块"}


def build_detail(stock: Stock, snapshot: dict | None = None, today: date | None = None) -> dict:
    today = today or datetime.now(_ZONE).date()
    if snapshot is None and is_hk(stock):
        return _insufficient(stock, today, "港股暂不按这套板块口径比较")
    if snapshot is None:
        snap_key = f"sector_snap:{stock.ts_code}:{today.isoformat()}"
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
    return _assemble(stock, snapshot or {}, today)


def failed_payload(stock: Stock, message: str, today: date | None = None) -> dict:
    payload = _computing(stock)
    today = today or datetime.now(_ZONE).date()
    payload["meta"]["state"] = "failed"
    payload["meta"]["status_text"] = "数据暂时取不到"
    payload["meta"]["as_of"] = today.isoformat()
    payload["meta"]["error"] = message
    return payload


def public(payload: dict) -> dict:
    """对外响应：verdict 只留和 headline 同一套的四个字段，中间量不对外。"""
    body = {key: value for key, value in payload.items() if key not in INTERNAL_FIELDS}
    verdict = payload.get("verdict")
    return {**body, "verdict": None if verdict is None else slim(verdict)}


def _assemble(stock: Stock, data: dict, today: date) -> dict:
    if int(data.get("listed_days") or 0) < 20:
        return _insufficient(stock, today, "上市不足 20 个交易日")
    affiliation = data.get("affiliation")
    if affiliation is None or not affiliation.get("candidates"):
        return _insufficient(stock, today, "没有可比的行业或概念板块")
    by_code = {item["code"]: item for item in data.get("sectors") or []}
    primary = by_code.get(affiliation.get("primary"))
    secondary = by_code.get(affiliation.get("secondary"))
    driver = affiliation.get("driver") or {}
    as_of = data.get("as_of")
    state, status = _state(data, as_of, today)
    payload = _shell(state, status, as_of)
    payload["meta"]["source_text"] = data.get("source_text")
    payload["meta"]["affiliation_version"] = affiliation.get("version")
    bodies = {}
    for role, sector in (("primary", primary), ("secondary", secondary)):
        if sector is not None:
            bodies[role] = evaluate(sector, stock.ts_code)
    weights = driver.get("weights") if secondary is not None else None
    independent = primary is None or driver.get("type") == "independent"
    if independent:
        gap = affiliation.get("stock_vs_index") if primary is None else bodies["primary"]["gaps"].get("stock_vs_index")
        warns = [] if primary is None else [item["text"] for item in bodies["primary"]["sustain_items"] if item["status"] == "warn"]
        bits = relative_verdict(gap, warns)
        text = write_text(bits["tag_text"], bits["gaps"], bits["counter_text"], True)
        merged = {"lean": bits["lean"], "level": bits["level"], "certainty": bits["certainty"], "certainty_reasons": bits["certainty_reasons"], "divergent": False}
    else:
        merged = combine(bodies["primary"], bodies.get("secondary"), weights)
        text = _text(sector_names={"primary": primary["name"], "secondary": None if secondary is None else secondary["name"]}, bodies=bodies, driver=driver, divergent=merged["divergent"])
    payload["verdict"] = {
        **rule_fields(merged["lean"], text),
        "lean": merged["lean"], "level": merged["level"], "certainty": merged["certainty"],
        "certainty_reasons": list(merged["certainty_reasons"]),
    }
    payload["sectors"] = {
        "primary": None if primary is None else _sector_block("primary", primary, bodies["primary"], driver),
        "secondary": None if secondary is None else _sector_block("secondary", secondary, bodies["secondary"], driver),
        "driver": _driver_block(driver, secondary is not None),
        "independent": independent,
        "divergence": _divergence(bodies, merged["divergent"]),
        "candidates": affiliation.get("candidates") or [],
        "merged": affiliation.get("merged") or [],
        "business_lines": affiliation.get("business_lines") or [],
    }
    payload["inputs"] = {
        role: {"lean": body["lean"], "level": body["level"], "certainty": body["certainty"], "stage": body["stage"], "role": body["role"]}
        for role, body in bodies.items()
    }
    payload["disclaimer_text"] = DISCLAIMER
    return payload


def _text(sector_names: dict, bodies: dict, driver: dict, divergent: bool) -> str:
    """规则版的一句话。单板块沿用「阶段，角色；差距。不过……」；两个板块各说阶段，再说驱动占比，分歧时点明。"""
    primary = bodies["primary"]
    if "secondary" not in bodies:
        return write_text(primary["tag_text"], primary["gaps"], primary["counter_text"], False)
    weights = driver.get("weights") or {}
    head = f"{sector_names['primary']}{_short(primary)}，{sector_names['secondary']}{_short(bodies['secondary'])}"
    share = f"两者对本股走势的解释约 {weights.get('primary', 0) * 100:.0f}% 和 {weights.get('secondary', 0) * 100:.0f}%"
    tail = "两个板块阶段对立，方向不取平均" if divergent else (primary["counter_text"] or "还没有看到反向信号")
    return f"{head}；{share}。不过{tail}。"


def _short(body: dict) -> str:
    return body["tag_text"].split("，")[0].replace("板块", "")


def _sector_block(role: str, sector: dict, body: dict, driver: dict) -> dict:
    weight = (driver.get("weights") or {}).get(role)
    corr = sector.get("corr_20d")
    return {
        "code": sector.get("code"),
        "name": sector.get("name"),
        "type": sector.get("type"),
        "role_in_stock": role,
        "role_text": ROLE_TEXT[role],
        "driver_weight": weight,
        "corr_20d": None if corr is None else round(float(corr), 2),
        "evidence": sector.get("evidence"),
        "tag_text": body["tag_text"],
        "strength": {
            "lines": body["lines"],
            "round_start": body["round_start"],
            "rank": body["rank"],
            "flow_rate": body["flow_rate"],
            "turnover_share": body["turnover_share"],
            "gaps": body["gaps"],
        },
        "stage": {
            "current": body["stage"],
            "confirmed_days": body["confirmed_days"],
            "days_in_round": body["days_in_round"],
            "signals": body["signals"],
            "why_text": body["why_text"],
            "next_text": body["next_text"],
        },
        "role": {
            "value": body["role"],
            "reason_text": body["reason_text"],
            "role_text": body["role_text"],
            "self_rank": body["self_rank"],
            "member_count": body["member_count"],
            "top": body["top"],
        },
        "sustain": {"items": body["sustain_items"]},
        "watch_next": body["watch_next"],
        "position_hint": body["position_hint"],
    }


def _driver_block(driver: dict, two: bool) -> dict:
    kind = driver.get("type") or "unknown"
    return {
        "type": kind,
        "type_text": DRIVER_TEXT.get(kind, DRIVER_TEXT["unknown"]),
        "r2": driver.get("r2"),
        "primary_share": driver.get("primary_share"),
        "secondary_share": driver.get("secondary_share") if two else None,
        "weights": driver.get("weights"),
        "days": driver.get("days"),
        "note": "两个板块指数对本股近 60 日收益的解释程度（R²），共同解释的部分平分",
    }


def _divergence(bodies: dict, active: bool) -> dict:
    if "secondary" not in bodies:
        return {"active": False, "text": ""}
    stages = {role: body["stage"] for role, body in bodies.items()}
    text = "两个板块处在对立的阶段，方向不取平均，请分别看" if active else ""
    return {"active": active, "stages": stages, "text": text}


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





def _shell(state: str, status: str, as_of: str | None) -> dict:
    return {
        "meta": {
            "state": state,
            "as_of": as_of,
            "updated_at": datetime.now(_ZONE).isoformat(timespec="seconds"),
            "rule_version": RULE_VERSION,
            "status_text": status,
            "affiliation_version": None,
            "source_text": None,
            "error": None,
        },
        "verdict": None,
        "sectors": None,
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
