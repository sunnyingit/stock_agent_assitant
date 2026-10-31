"""价位菜单的默认选择：参考表（Agent 可以不照做，没有 Agent 时就用它）。

背后的道理：利好越扎实，越给空间、确认越严；利空或利好已兑现，就不给空间、越早提醒。
"""

from __future__ import annotations

from app.synthesis.types import Snapshot

_INFLOW = {"accelerating_in", "slowing_in", "steady_in"}
_OUTFLOW = {"turning_out", "accelerating_out"}


def _fact(snaps: dict[str, Snapshot], fact_id: str):
    for snap in snaps.values():
        if snap.done and fact_id in snap.facts:
            return snap.facts[fact_id]
    return None


def _flag(snaps: dict[str, Snapshot], key: str, flag: str):
    snap = snaps.get(key)
    return snap.flags.get(flag) if snap is not None and snap.done else None


def reference_row(snaps: list[Snapshot]) -> str:
    """返回参考表里的哪一行。"""
    by_key = {snap.key: snap for snap in snaps}
    impact = _flag(by_key, "news", "main_impact")
    priced = _fact(by_key, "news.priced_in")
    trend = _fact(by_key, "flow.big_trend")
    stage = _fact(by_key, "sector.stage")
    if impact == "negative" or stage in ("ebb", "weak", "diverge") or _flag(by_key, "finance", "red_line"):
        return "bearish_news_or_ebb"
    if impact == "positive" and priced in ("mostly",) and trend in _OUTFLOW:
        return "priced_in_outflow"
    if impact == "positive" and priced == "not_yet" and trend in _INFLOW:
        return "good_news_unpriced"
    if stage in ("start", "main", "climax"):
        return "sector_driven"
    if impact is None and trend in _INFLOW | _OUTFLOW:
        return "flow_driven"
    return "unclear"


# 每行：下方 (候选偏好顺序, 缓冲, 确认)，上方 (候选偏好顺序, 确认, 含义)
_ROWS = {
    "good_news_unpriced": (("L3", "L2", "L1"), 0.02, "close_2d", ("U1", "U2"), "close_volume", "stronger"),
    "priced_in_outflow": (("L2", "L1"), 0.0, "close_outflow", ("U1", "U2"), "close", "priced_in"),
    "bearish_news_or_ebb": (("L1", "L2"), 0.0, "close", ("U3", "U1"), "close", "position_high"),
    "flow_driven": (("L2", "L1"), 0.02, "close_outflow", ("U1", "U2"), "close_volume", "stronger"),
    "sector_driven": (("L1", "L2"), 0.02, "close", ("U1", "U2"), "close_volume", "stronger"),
    "unclear": (("L1", "L2", "L4"), 0.0, "close", ("U1", "U2"), "close", "stronger"),
}
FALLBACK_ROW = "unclear"


def default_selection(row: str, candidates: list[dict]) -> dict:
    prefer_down, buffer, down_confirm, prefer_up, up_confirm, meaning = _ROWS.get(row, _ROWS[FALLBACK_ROW])
    selection = {"row": row, "down": None, "up": None}
    present = {item["id"]: item for item in candidates}
    for cid in prefer_down:
        if cid in present and present[cid]["dir"] == "down":
            selection["down"] = {"base": cid, "buffer": buffer, "confirm": down_confirm, "why": "", "cites": []}
            break
    for cid in prefer_up:
        if cid in present and present[cid]["dir"] == "up":
            selection["up"] = {"base": cid, "confirm": up_confirm, "meaning": meaning, "why": "", "cites": []}
            break
    return selection
