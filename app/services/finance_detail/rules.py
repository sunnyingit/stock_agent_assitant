"""财报的规则版判断：三格（增长、含金量、预期）、规则版结论，以及模型不能越过的硬阈值。阈值在 config.py。"""

from __future__ import annotations

from app.services.finance_detail.config import thresholds

RULE_VERSION = "finance-2"
DISCLAIMER_TEXT = "数据来自定期报告，单季数据优先用交易所口径，没有时由累计数推算。只描述这份财报带来的变化，不构成投资建议。"
KIND_NAME = {"q1": "一季报", "h1": "半年报", "q3": "三季报", "annual": "年报"}
GROWTH_TEXT = {"up": "增长向上", "down": "增长向下", "mixed": "营收和利润分化", "loss": "亏损", "na": "暂无法比较"}
QUALITY_TEXT = {"good": "含金量较好", "ok": "含金量一般", "poor": "含金量偏弱", "na": "暂无法判断"}
EXPECT_TEXT = {"beat": "好于预期", "inline": "基本符合预期", "miss": "差于预期", "na": "暂无预期可比"}


def period_label(m: dict) -> str:
    end = m["report_end"]
    return f"{end[:4]} {KIND_NAME.get(m['kind'], '')}"


def profit_yoy(g: dict):
    """利润同比优先用扣非，没有扣非再用归母。"""
    return g["q_ded_yoy"] if g["q_ded_yoy"] is not None else g["q_np_yoy"]


def growth_level(m: dict) -> str:
    g = m["growth"]
    if g["loss_now"] or g["loss_prior"]:
        return "loss"
    rev, prof = g["q_rev_yoy"], profit_yoy(g)
    if rev is None or prof is None:
        return "na"
    if rev > 0 and prof > 0:
        return "up"
    if rev < 0 and prof < 0:
        return "down"
    return "mixed"


def quality_level(m: dict, th: dict) -> str:
    q, c = m["quality"], m["cash"]
    ratio, ocf = q["ded_to_np"], c["ocf_to_np_cum"]
    if ratio is None and ocf is None and not q["ded_negative_np_positive"]:
        return "na"
    if (
        q["ded_negative_np_positive"]
        or (ratio is not None and ratio < th["ded_to_np_support_min"])
        or (ocf is not None and ocf < th["ocf_to_np_support_min"])
        or c["ocf_cum_negative"]
        or c["ocf_negative_run"] >= th["ocf_negative_run"]
    ):
        return "poor"
    if (ratio is None or ratio >= th["ded_to_np_good"]) and (ocf is None or ocf >= th["ocf_to_np_good"]):
        return "good"
    return "ok"


def expectation_level(m: dict, th: dict) -> tuple[str, str]:
    """预期差：有业绩预告就看实际落在预告区间的哪一段；没有就看相对朴素预期的偏离。返回（档位, 依据）。"""
    expect = m.get("expect") or {}
    preview = expect.get("preview")
    if preview:
        return {"above": "beat", "within": "inline", "below": "miss"}[preview["position"]], "preview"
    naive = expect.get("naive")
    if naive:
        dev = naive["np_dev"] if naive["np_dev"] is not None else naive["rev_dev"]
        if dev is not None:
            return "beat" if dev > th["naive_pp"] else "miss" if dev < -th["naive_pp"] else "inline", "naive"
    return "na", ""


def cells(m: dict, th: dict | None = None) -> list[dict]:
    th = th or thresholds(m["industry_rule"])
    g_level, q_level = growth_level(m), quality_level(m, th)
    e_level, basis = expectation_level(m, th)
    return [
        {"key": "growth", "level": g_level, "level_text": GROWTH_TEXT[g_level], "note_text": _growth_note(m)},
        {"key": "quality", "level": q_level, "level_text": QUALITY_TEXT[q_level], "note_text": _quality_note(m)},
        {"key": "expectation", "level": e_level, "level_text": EXPECT_TEXT[e_level], "note_text": _expect_note(m, basis)},
    ]


_FORECAST_UP = ("预增", "略增", "扭亏", "续盈")
_FORECAST_DOWN = ("预减", "略减", "首亏", "续亏", "增亏")


def fresh_forecast(m: dict, th: dict) -> dict | None:
    """披露之后又发了新业绩预告（针对下一个报告期），而且是最近 30 天内发的：这时优先读预告。"""
    forecast, days = m["time"]["forecast"], m["time"]["forecast_days"]
    if not forecast or days is None or days > th["fresh_days"]:
        return None
    return forecast


def time_weight(m: dict, th: dict | None = None) -> float:
    """综合判断给财报这一维的时效权重：距最近披露 0～30 天正常，31～60 天减半，60 天以上且没有新预告只作背景（不参与方向）。
    处于预告窗口（刚发了新预告）按正常算，方向优先读预告。"""
    th = th or thresholds(m["industry_rule"])
    if fresh_forecast(m, th):
        return 1.0
    days = m["time"]["days_since"]
    if days is None or days <= th["fresh_days"]:
        return 1.0
    return 0.5 if days <= th["stale_days"] else 0.0


def verdict(m: dict, th: dict | None = None) -> tuple[str, int, str | None]:
    """规则版方向、强度、把握，只给综合判断读。处于预告窗口优先读预告；距披露超过 60 天且没有新预告，只作背景：方向不明。"""
    th = th or thresholds(m["industry_rule"])
    forecast = fresh_forecast(m, th)
    if forecast:
        kind = str(forecast.get("type") or "")
        if kind in _FORECAST_UP:
            return "bullish", 1, "low"
        if kind in _FORECAST_DOWN:
            return "bearish", 1, "low"
        return "neutral", 0, None
    g_level, q_level = growth_level(m), quality_level(m, th)
    score = {"up": 1, "down": -1}.get(g_level, 0) + {"good": 1, "poor": -1}.get(q_level, 0) + {"beat": 1, "miss": -1}.get(expectation_level(m, th)[0], 0)
    days = m["time"]["days_since"]
    if time_weight(m, th) == 0.0:
        return "neutral", 0, None
    lean = "bullish" if score >= 2 else "bearish" if score <= -2 else "neutral"
    level = min(3, abs(score)) if lean != "neutral" else 0
    d = m["data"]
    shaky = (
        g_level in ("loss", "na") or d["single_source"] == "diff" or d["restated"] or d["periods_behind"] > 1
        or not d["adj_ok"] or (days is not None and days > th["fresh_days"])
    )
    return lean, level, "low" if shaky else "medium"


def bounds(m: dict, th: dict | None = None) -> dict[str, dict]:
    """硬阈值：forbid_support 不能判偏正面，force_warn 必须判需警惕。模型无权越过。第 6 题没有硬阈值。"""
    th = th or thresholds(m["industry_rule"])
    g, q, c, b = m["growth"], m["quality"], m["cash"], m["balance"]
    rev, prof = g["q_rev_yoy"], profit_yoy(g)
    out = {key: {"forbid_support": False, "force_warn": False} for key in ("q2", "q4", "q5", "q7", "q8", "q9")}
    preview = (m.get("expect") or {}).get("preview")
    if preview:   # 只有公司自己发过业绩预告才有硬阈值；朴素预期不是承诺，只作参考
        out["q9"]["forbid_support"] = preview["actual"] < preview["mid"]
        out["q9"]["force_warn"] = preview["actual"] < preview["low"]
    out["q2"]["forbid_support"] = (rev is not None and rev < 0) or (prof is not None and prof < 0)
    out["q2"]["force_warn"] = bool(g["slowdown_two_quarters"])
    out["q4"]["forbid_support"] = q["ded_negative_np_positive"] or (q["ded_to_np"] is not None and q["ded_to_np"] < th["ded_to_np_support_min"])
    out["q4"]["force_warn"] = q["ded_negative_np_positive"]
    out["q5"]["forbid_support"] = q["nonrec_share"] is not None and q["nonrec_share"] > th["nonrec_share_max"]
    out["q5"]["force_warn"] = bool(q["ded_negative_np_positive"] and (q["market_driven"] or 0) >= 0.5)
    low_now = c["ocf_to_np_cum"] is not None and c["ocf_to_np_cum"] < th["ocf_to_np_support_min"]
    both_low = all(v is not None and v < th["ocf_to_np_support_min"] for v in (c["ocf_to_np_q"], c["ocf_to_np_prev_q"]))
    out["q7"]["forbid_support"] = low_now or c["ocf_cum_negative"]
    out["q7"]["force_warn"] = both_low or c["ocf_negative_run"] >= th["ocf_negative_run"]
    ar_high = b["ar_gap_pp"] is not None and b["ar_gap_pp"] >= th["ar_gap_pp"]
    inv_high = b["inv_gap_pp"] is not None and b["inv_gap_pp"] >= th["inv_gap_pp"]
    out["q8"]["forbid_support"] = ar_high
    out["q8"]["force_warn"] = ar_high and inv_high
    if m["industry_rule"] == "realestate":   # 房地产：看合同负债（预收）有没有塌
        liab = b["contract_liab_yoy"]
        out["q8"]["forbid_support"] = liab is not None and liab <= th["contract_liab_drop"]
        out["q8"]["force_warn"] = liab is not None and liab <= th["contract_liab_collapse"]
    if m["data"]["chronic_loss"]:   # 持续亏损：经营现金流占净利润没有意义，看现金还能撑多久
        left = runway(m)
        out["q7"]["forbid_support"] = left is not None and left < th["runway_support_q"]
        out["q7"]["force_warn"] = left is not None and left < th["runway_warn_q"]
    return out


def runway(m: dict):
    """按近 4 季平均每季的经营现金净流出，货币资金还能撑几个季度。没有净流出就没有这个数。"""
    burn, cash = m["cash"].get("burn"), m["balance"].get("cash_balance_for_runway")
    return None if not burn or burn <= 0 or cash is None else cash / burn


def pct(value, digits: int = 0, signed: bool = False) -> str:
    if value is None:
        return "—"
    text = f"{value * 100:+.{digits}f}%" if signed else f"{value * 100:.{digits}f}%"
    return text


def yi(value) -> str:
    return "—" if value is None else f"{value / 1e8:.2f} 亿元"


def _growth_note(m: dict) -> str:
    g = m["growth"]
    if g["loss_now"] or g["loss_prior"]:
        return f"单季归母 {yi(g['q_np'])}，较去年同期{'减亏' if (g['loss_change'] or 0) > 0 else '增亏'} {yi(abs(g['loss_change'] or 0))}"
    return f"单季营收 {pct(g['q_rev_yoy'], 0, True)}，归母 {pct(g['q_np_yoy'], 0, True)}，扣非 {pct(g['q_ded_yoy'], 0, True)}"


def _expect_note(m: dict, basis: str) -> str:
    expect = m.get("expect") or {}
    if basis == "preview":
        p = expect["preview"]
        return f"业绩预告区间 {yi(p['low'])} ～ {yi(p['high'])}，实际 {yi(p['actual'])}"
    if basis == "naive":
        n = expect["naive"]
        dev = n["np_dev"] if n["np_dev"] is not None else n["rev_dev"]
        return f"相对朴素预期（自身趋势外推，不是市场预期）{pct(dev, 0, True)}"
    return ""


def _quality_note(m: dict) -> str:
    q, c = m["quality"], m["cash"]
    return f"扣非/归母 {pct(q['ded_to_np'])}，经营现金流/净利润 {pct(c['ocf_to_np_cum'])}"
