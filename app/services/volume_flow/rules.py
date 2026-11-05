"""量价资金的形态、位置信号和方向。阈值集中在这里，计算层只出数字。"""

from __future__ import annotations

RULE_VERSION = "volume_price-5"   # 5：形态要涨跌够 1%；资金按净流入率和自己过去比（z）确认，不再数天数；对立不再看「大单净卖」（那是常态）
DEFAULT_WINDOW = 5              # A 股短线主窗口；接口不传 window 时用这个
FLOW_SOURCE = "东方财富"
DISCLAIMER_TEXT = "以上为基于公开资金流数据的研究分析，不构成投资建议"

HIGH_VOL = 1.5
CONCENTRATION_LIMIT = 0.8
REL_QUIET = 0.8
REL_LOUD = 1.2
RATIO_EDGE = 1.2
FLOW_EDGE = 0.8
PATTERN_MIN_MOVE = 1.0  # 窗口涨跌不到 1% 不算「价涨」或「价跌」，量价形态不判
FLOW_Z_EDGE = 0.8       # 净流入率比自己平时高/低这么多个标准差，才算资金在确认或反对
FLOW_Z_STRONG = 1.5
STALL_RET = 1.0
STALL_VOL = 1.5
FOLLOW_MIN_MOVE = 1.0   # 窗口涨跌不到 1% 时不判断有没有人跟
YEAR_NEAR = 0.9
MARGIN_TOP = 0.95
STALE_DAYS = 5
PEAK_GAP = 5

_PATTERNS = {
    "up_vol_up": "价涨量增",
    "up_vol_down": "价涨量缩",
    "down_vol_up": "价跌量增",
    "down_vol_down": "价跌量缩",
    "none": "无明显形态",
}
_HINTS = (
    ("flow_concentration", "资金过度集中"),
    ("pv_top_divergence", "量价顶背离"),
    ("indicator_top_divergence", "指标顶背离"),
    ("high_volume_stall", "放量滞涨"),
    ("record_volume", "天量"),
    ("margin_surge", "融资快速增加"),
)


def price_pattern(ret, rel_vol) -> str:
    """形态只看窗口涨跌和相对量能。涨跌日量比不参与。"""
    if ret is None or rel_vol is None or abs(ret) < PATTERN_MIN_MOVE:
        return "none"
    if REL_QUIET <= rel_vol <= REL_LOUD:
        return "none"
    if ret > 0:
        return "up_vol_up" if rel_vol > REL_LOUD else "up_vol_down"
    return "down_vol_up" if rel_vol > REL_LOUD else "down_vol_down"


def flow_pattern(rows: list[dict]) -> str:
    """只用来展示。四档净额加起来恒为 0（有买就有卖），所以结果只可能是「大单买、中小单卖」或反过来，
    且大单净卖是常态（约六成窗口）。它不进方向和把握度的判断，强弱看 flow_state。"""
    nets = {row["size"]: row.get("net") or 0 for row in rows}
    big = nets.get("super_large", 0) + nets.get("large", 0)
    retail = nets.get("medium", 0) + nets.get("small", 0)
    if big > 0 and retail < 0:
        return "big_buy_retail_sell"
    if big < 0 and retail > 0:
        return "big_sell_retail_buy"
    if all(value > 0 for value in nets.values()):
        return "all_buy"
    if all(value < 0 for value in nets.values()):
        return "all_sell"
    return "mixed"


def flow_trend(days: list[dict]) -> str:
    if len(days) < 2:
        return "mixed"
    previous = days[-2].get("net") or 0
    latest = days[-1].get("net") or 0
    if previous > 0 and latest < 0:
        return "turning_out"
    if len(days) < 10:
        return "mixed"
    older = sum(row["net"] or 0 for row in days[:5])
    newer = sum(row["net"] or 0 for row in days[5:])
    if older > 0 and newer < 0:
        return "turning_out"
    if older > 0 and newer > 0:
        if newer > older:
            return "accelerating_in"
        if newer < older:
            return "slowing_in"
        return "steady_in"
    if older <= 0 and newer > 0:
        return "accelerating_in"
    if older < 0 and newer < 0 and newer < older:
        return "accelerating_out"
    return "mixed"


STATE_TEXT = {"in_strong": "比自己平时明显更流入", "in": "比自己平时偏流入", "normal": "和自己平时差不多", "out": "比自己平时偏流出", "out_strong": "比自己平时明显更流出"}


def flow_state(z) -> str:
    """大资金净流入率相对这只股票自己过去的强弱。z 取不到（历史太短）返回空串。"""
    if z is None:
        return ""
    if z >= FLOW_Z_STRONG:
        return "in_strong"
    if z >= FLOW_Z_EDGE:
        return "in"
    if z <= -FLOW_Z_STRONG:
        return "out_strong"
    if z <= -FLOW_Z_EDGE:
        return "out"
    return "normal"


def annotations(bars: list[dict], vol_base) -> list[dict]:
    if not vol_base:
        return []
    marks = []
    for bar in bars:
        volume = bar.get("volume") or 0
        if bar.get("chg") is not None and bar["chg"] < 0 and volume > HIGH_VOL * vol_base:
            month = int(bar["date"][5:7])
            day = int(bar["date"][8:10])
            marks.append({
                "date": bar["date"],
                "kind": "high_vol_down",
                "text": f"{month}/{day} 放量下跌",
            })
    return marks


def position_hints(raw: dict) -> list[dict]:
    close = raw.get("close")
    concentration = raw.get("concentration")
    five_ret = raw.get("five_ret")
    five_vol = raw.get("five_vol")
    base_vol = raw.get("base_vol")
    year_high = raw.get("year_high")
    percentile = raw.get("margin_percentile")
    flags = {
        "flow_concentration": concentration is not None and concentration > CONCENTRATION_LIMIT,
        "pv_top_divergence": bool(raw.get("has_prior_peak")) and _below(raw.get("volume"), raw.get("prev_high_volume")),
        "indicator_top_divergence": bool(raw.get("has_prior_peak")) and (
            _below(raw.get("macd"), raw.get("prev_macd")) or _below(raw.get("rsi"), raw.get("prev_rsi"))
        ),
        "high_volume_stall": (
            five_ret is not None and abs(five_ret) < STALL_RET
            and five_vol is not None and base_vol not in (None, 0) and five_vol > STALL_VOL * base_vol
            and close is not None and year_high not in (None, 0) and close > YEAR_NEAR * year_high
        ),
        "record_volume": _below(raw.get("volume_max"), raw.get("volume")),
        "margin_surge": percentile is not None and percentile >= MARGIN_TOP,
    }
    details = raw.get("details") or {}
    return [
        {"type": key, "hit": bool(flags[key]), "text": label, "detail_text": details.get(key) or ""}
        for key, label in _HINTS
    ]


def judge(metrics: dict, *, stale: bool) -> dict:
    if stale:
        return {
            "lean": "",
            "level": None,
            "certainty": "",
            "pattern": "",
            "flow_pattern": flow_pattern(metrics["flow"]["rows"]),
            "trend": flow_trend(metrics["daily"]["days"]),
            "annotations": annotations(metrics["price"]["bars"], metrics["price"].get("vol_base")),
        }
    pattern = price_pattern(metrics["price"].get("ret"), metrics["price"].get("rel_vol"))
    flow_kind = flow_pattern(metrics["flow"]["rows"])
    hints = position_hints(metrics["signals"])
    lean, level = _direction(pattern, metrics)
    state = flow_state(metrics["flow"].get("rate_z"))
    opposite = _opposite(lean, state)
    concentration = metrics["signals"].get("concentration")
    certainty = _certainty(pattern, opposite, concentration)
    return {
        "lean": lean,
        "level": level,
        "certainty": certainty,
        "pattern": pattern,
        "flow_pattern": flow_kind,
        "flow_state": state,
        "trend": flow_trend(metrics["daily"]["days"]),
        "annotations": annotations(metrics["price"]["bars"], metrics["price"].get("vol_base")),
        "hints": hints,
    }


def _direction(pattern: str, metrics: dict) -> tuple[str, int]:
    if pattern == "none":
        return "unclear", 0
    bullish = pattern.startswith("up_")
    level = 1
    ratio = metrics["price"].get("up_down_vol_ratio")
    if bullish and ratio is not None and ratio >= RATIO_EDGE:
        level += 1
    if not bullish and ratio is not None and ratio <= 1 / RATIO_EDGE:
        level += 1
    z = metrics["flow"].get("rate_z")          # 和自己过去比：按金额，不数天数
    if z is not None and ((bullish and z >= FLOW_Z_EDGE) or (not bullish and z <= -FLOW_Z_EDGE)):
        level += 1
    return ("bullish" if bullish else "bearish"), min(level, 3)


def _opposite(lean: str, state: str) -> bool:
    """资金和方向相反：看多但大资金比平时明显更流出，或看空但比平时明显更流入。"""
    return (lean == "bullish" and state in {"out", "out_strong"}) or (lean == "bearish" and state in {"in", "in_strong"})


def _certainty(pattern: str, opposite: bool, concentration) -> str | None:
    if pattern == "none":
        return None
    index = 1
    if pattern in {"up_vol_up", "down_vol_up"} and not opposite:
        index += 1
    if opposite or (concentration is not None and concentration > CONCENTRATION_LIMIT):
        index -= 1
    return ("low", "medium", "high")[min(2, max(0, index))]


def _below(value, high) -> bool:
    return value is not None and high is not None and value < high
