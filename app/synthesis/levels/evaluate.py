"""代入重算：把收盘价设在价位外侧，只重算和价格有关的部分，再跑一遍规则版综合。

和价格有关的部分是量价维度（形态、量能、大资金）和市场口径（距一年高点 → 位置）。
其余维度保持原样。结论（方向、强度、位置）变了，这个价位才算"有后果"。
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from app.synthesis import policy, rules
from app.synthesis.producer import adapters
from app.synthesis.producer.market import market_facts
from app.synthesis.types import Snapshot

_DOWN_TEXT = {
    ("bullish", "bearish"): "这轮上涨可能结束了",
    ("bullish", "unclear"): "偏多的判断可能失效",
    ("unclear", "bearish"): "判断可能转为偏空",
    ("bearish", "bearish"): "偏空的判断可能更明显",
}
_UP_TEXT = {
    ("bearish", "unclear"): "偏空的判断可能减弱",
    ("bearish", "bullish"): "判断可能转为偏多",
    ("unclear", "bullish"): "判断可能转为偏多",
}
MEANING_TEXT = {
    "stronger": "涨势更强",
    "position_high": "位置变成偏高",
    "priced_in": "利好基本兑现",
}


def simulate_aligned(aligned: dict, price: float, direction: str, confirm: str) -> dict:
    bars = [dict(row) for row in aligned.get("bars") or []]
    flow = [dict(row) for row in aligned.get("flow") or []]
    outside = price * (1 - policy.SIMULATE_OUTSIDE) if direction == "down" else price * (1 + policy.SIMULATE_OUTSIDE)
    days = 2 if confirm == "close_2d" else 1
    start = len(bars) - days
    for index in range(start, len(bars)):
        before = float(bars[index - 1]["close"])
        bars[index]["chg"] = (outside / before - 1) * 100
        bars[index]["close"] = outside
    if confirm == "close_volume":
        history = [row["volume"] for row in bars[-61:-1] if row.get("volume")]
        if history:
            bars[-1]["volume"] = sum(history) / len(history) * policy.SIMULATED_VOLUME_MULTIPLE
    if confirm == "close_outflow" and flow:
        for row in flow:
            if row.get("date") == bars[-1].get("date"):
                for size in ("super_large", "large"):
                    row[size] = -max(abs(row.get(size) or 0), 1e7)
    return {**aligned, "bars": bars, "flow": flow}


def describe(base: dict, after: dict, direction: str) -> dict:
    changed = (base["direction"], base["level"], base["position"]) != (after["direction"], after["level"], after["position"])
    table = _DOWN_TEXT if direction == "down" else _UP_TEXT
    text = table.get((base["direction"], after["direction"]), "")
    if not text and changed:
        if after["position"] != base["position"]:
            text = "位置变成" + policy.POSITION_TEXT[after["position"]]
        elif after["level"] != base["level"]:
            stronger = after["level"] > base["level"]
            text = "判断可能更明显" if stronger else "判断可能减弱"
    return {
        "changed": changed,
        "text": text,
        "from": [base["direction"], base["level"], base["position"]],
        "to": [after["direction"], after["level"], after["position"]],
    }


class Evaluator:
    def __init__(
        self,
        stock,
        aligned: dict | None,
        snaps: list[Snapshot],
        now: datetime,
        fresh: bool,
        build_volume_flow: Callable[[dict], dict] | None = None,
    ):
        self.stock = stock
        self.aligned = aligned
        self.snaps = snaps
        self.now = now
        self.fresh = fresh
        self._build = build_volume_flow or self._default_build
        self.base = rules.synthesize(snaps, market_facts(aligned), fresh)

    def _default_build(self, aligned: dict) -> dict:
        from app.services.volume_flow import build_detail

        return build_detail(self.stock, policy.FLOW_WINDOW, aligned=aligned, today=self.now.date())

    def evaluate(self, price: float, direction: str, confirm: str) -> dict:
        if not self.aligned:
            return {"changed": False, "text": "", "from": None, "to": None}
        simulated = simulate_aligned(self.aligned, price, direction, confirm)
        try:
            flow = adapters.adapt("volume_flow", self._build(simulated))
        except Exception as exc:
            return {"changed": False, "text": "", "from": None, "to": None, "error": str(exc)}
        snaps = [flow if snap.key == "volume_flow" and flow.done else snap for snap in self.snaps]
        after = rules.synthesize(snaps, market_facts(simulated), self.fresh)
        return describe(self.base, after, direction)
