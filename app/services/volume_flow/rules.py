"""量价资金的形态、位置信号和方向。阈值集中在这里，计算层只出数字。"""

from __future__ import annotations

RULE_VERSION = "volume_price-6"   # 6：path(含 turning_in) + 稳健 z + 分档加权；short 主窗 3 日
DEFAULT_WINDOW = 3              # short 主窗口；接口不传 window 时用这个
FLOW_SOURCE = "东方财富"
DISCLAIMER_TEXT = "以上为基于公开资金流数据的研究分析，不构成投资建议"
DIAGNOSIS_MAX_CHARS = 50        # 首次诊断 / 定时任务一句话上限；reply 不受此限

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
PATH_LOOKBACK_SHORT = 10
PATH_LOOKBACK_SWING = 20
STRUCT_ELG_W = 1.5
STRUCT_LG_W = 1.0
STRUCT_MD_ADJ = 0.25     # 中单同向/反向只调把握度，不翻方向

_PATTERNS = {
    "up_vol_up": "价涨量增",
    "up_vol_down": "价涨量缩",
    "down_vol_up": "价跌量增",
    "down_vol_down": "价跌量缩",
    "none": "无明显形态",
}
PATH_TEXT = {
    "turning_in": "资金回升",
    "turning_out": "资金转出",
    "accelerating_in": "流入加快",
    "accelerating_out": "流出加快",
    "slowing_in": "流入放缓",
    "slowing_out": "流出放缓",
    "steady_in": "持续流入",
    "steady_out": "持续流出",
    "mixed": "资金方向不一",
}
PRICE_FLOW_TEXT = {
    "align_up": "价资同向偏多",
    "align_down": "价资同向偏空",
    "diverge_up": "价涨资弱",
    "diverge_down": "价跌资强",
    "neutral": "价资对照不明",
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


def _path_value(row: dict):
    """优先用日净流入率；没有则用大资金净额。"""
    if row.get("net_rate") is not None:
        return float(row["net_rate"])
    if row.get("net") is not None:
        return float(row["net"])
    return None


def flow_trend(days: list[dict], lookback: int = PATH_LOOKBACK_SHORT) -> str:
    """Path：近 lookback 日大资金路径（V / 转出 / 加速 / 放缓）。"""
    series = [_path_value(row) for row in (days[-lookback:] if lookback else days)]
    values = [value for value in series if value is not None]
    if len(values) < 2:
        return "mixed"
    prev, latest = values[-2], values[-1]
    # 末日翻向：优先标转折（旧用例：连流入后末日转出）
    if prev > 0 and latest < 0:
        return "turning_out"
    if prev < 0 and latest > 0:
        return "turning_in"
    if len(values) < 6:
        return "mixed"
    mid = len(values) // 2
    older = sum(values[:mid])
    newer = sum(values[mid:])
    if older <= 0 < newer:
        return "turning_in"
    if older > 0 >= newer:
        return "turning_out"
    if older > 0 and newer > 0:
        if newer > older * 1.05:
            return "accelerating_in"
        if newer < older * 0.95:
            return "slowing_in"
        return "steady_in"
    if older < 0 and newer < 0:
        if newer < older * 1.05:   # 更负 = 流出加快
            return "accelerating_out"
        if newer > older * 0.95:   # 负得少 = 流出放缓
            return "slowing_out"
        return "steady_out"
    return "mixed"


def flow_path_detail(days: list[dict], lookback: int = PATH_LOOKBACK_SHORT) -> dict:
    """Path 附带前后半合计，供 facts / tools 引用。"""
    chosen = days[-lookback:] if lookback else days
    values = [_path_value(row) for row in chosen]
    usable = [value for value in values if value is not None]
    mid = max(1, len(usable) // 2) if usable else 0
    older = sum(usable[:mid]) if usable else None
    newer = sum(usable[mid:]) if usable else None
    label = flow_trend(days, lookback=lookback)
    return {
        "label": label,
        "text": PATH_TEXT.get(label, label),
        "lookback": lookback,
        "n": len(usable),
        "older": None if older is None else round(older, 4),
        "newer": None if newer is None else round(newer, 4),
    }


def flow_structure(elg, lg, md=None) -> dict:
    """分档加权：超大单 1.5、大单 1.0；中单只调把握度；小单不进。"""
    score = 0.0
    seen = False
    if elg is not None:
        score += STRUCT_ELG_W * float(elg)
        seen = True
    if lg is not None:
        score += STRUCT_LG_W * float(lg)
        seen = True
    if not seen:
        return {
            "direction": "flat",
            "score": None,
            "certainty_delta": 0.0,
            "medium": "none",
            "text": "分档资金不足",
        }
    direction = "in" if score > 0 else "out" if score < 0 else "flat"
    medium = "none"
    delta = 0.0
    if md is not None and direction != "flat" and md != 0:
        same = (md > 0 and direction == "in") or (md < 0 and direction == "out")
        medium = "same" if same else "against"
        delta = STRUCT_MD_ADJ if same else -STRUCT_MD_ADJ
    if direction == "in":
        head = "加权后大额偏流入"
    elif direction == "out":
        head = "加权后大额偏流出"
    else:
        head = "加权后大额接近平衡"
    if medium == "same":
        tail = "，中单同向略增把握"
    elif medium == "against":
        tail = "，中单反向略降把握"
    else:
        tail = ""
    return {
        "direction": direction,
        "score": round(score, 2),
        "certainty_delta": delta,
        "medium": medium,
        "text": head + tail,
    }


def price_flow_relation(ret, state: str) -> str:
    """价对照：只标配合/背离，不单独翻多空。"""
    if ret is None or not state:
        return "neutral"
    up = ret > 0
    down = ret < 0
    inflow = state in {"in", "in_strong"}
    outflow = state in {"out", "out_strong"}
    if up and inflow:
        return "align_up"
    if down and outflow:
        return "align_down"
    if up and outflow:
        return "diverge_up"
    if down and inflow:
        return "diverge_down"
    return "neutral"


def apply_structure_certainty(base: str | None, structure: dict) -> str | None:
    """Structure 只改把握度一档。"""
    if not base:
        return base
    order = ("low", "medium", "high")
    try:
        index = order.index(base)
    except ValueError:
        return base
    delta = structure.get("certainty_delta") or 0
    if delta > 0:
        index = min(2, index + 1)
    elif delta < 0:
        index = max(0, index - 1)
    return order[index]


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


def vs_self_user_text(z) -> str:
    """给用户看的力度说法：跟这只股票自己平时比，五档人话，不写 z / 标准差。"""
    state = flow_state(z)
    return STATE_TEXT.get(state, "") if state else ""


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
    path = flow_path_detail(metrics["daily"]["days"])
    rows = {row["size"]: row.get("net") for row in metrics["flow"]["rows"]}
    structure = flow_structure(rows.get("super_large"), rows.get("large"), rows.get("medium"))
    if stale:
        return {
            "lean": "",
            "level": None,
            "certainty": "",
            "pattern": "",
            "flow_pattern": flow_pattern(metrics["flow"]["rows"]),
            "trend": path["label"],
            "path": path,
            "structure": structure,
            "price_flow": "neutral",
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
    # 结构修正只在「加权资金方向与 lean 一致」时生效，避免习惯性净卖或背离时被中单同向抬高把握
    agree = (lean == "bullish" and structure.get("direction") == "in") or (
        lean == "bearish" and structure.get("direction") == "out"
    )
    if agree:
        certainty = apply_structure_certainty(certainty, structure)
    relation = price_flow_relation(metrics["price"].get("ret"), state)
    return {
        "lean": lean,
        "level": level,
        "certainty": certainty,
        "pattern": pattern,
        "flow_pattern": flow_kind,
        "flow_state": state,
        "trend": path["label"],
        "path": path,
        "structure": structure,
        "price_flow": relation,
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
