"""上期预警跟踪（设计稿事实 10）：上期判为"需警惕"的每道题，记下当时最能代表它的那个指标；本期再算一次，看是缓解、持续还是恶化。

记录随信号一起存（store.finance_signals.tracked），所以第一次分析、或上期没有预警时，第 11 题返回"不适用"。
"""

from __future__ import annotations

from app.services.finance_detail.rules import pct

# 题号 → (指标名, 取值, 越高越好吗(1)/越低越好吗(-1), 变化小于这个数算"持续", 格式)
TRACKED = {
    "q2": ("单季营收同比", lambda m: m["growth"]["q_rev_yoy"], 1, 0.03, "pct"),
    "q3": ("营收增速与归母增速的差距（绝对值）", lambda m: None if m["growth"]["scissors_pp"] is None else abs(m["growth"]["scissors_pp"]), -1, 0.05, "pp"),
    "q4": ("扣非净利润占归母的比例", lambda m: m["quality"]["ded_to_np"], 1, 0.05, "share"),
    "q5": ("非经常损益占归母的比例", lambda m: m["quality"]["nonrec_share"], -1, 0.05, "share"),
    "q6": ("毛利率同比变动", lambda m: m["margin"]["gm_yoy_pp"], 1, 0.005, "pp"),
    "q7": ("累计经营现金流占归母的比例", lambda m: m["cash"]["ocf_to_np_cum"], 1, 0.05, "share"),
    "q8": ("应收增速减营收增速", lambda m: m["balance"]["ar_gap_pp"], -1, 0.03, "pp"),
    "q10": ("披露后相对行业的超额收益", lambda m: _latest_excess(m), 1, 0.01, "pct"),
}
STATE_TEXT = {"improved": "缓解", "same": "持续", "worse": "恶化"}


def _latest_excess(m: dict):
    shown = [(k, row) for k, row in sorted(((m.get("reaction") or {}).get("after") or {}).items()) if row]
    if not shown:
        return None
    row = shown[-1][1]
    return row["excess"] if row["excess"] is not None else row["stock"]


def fmt(value, kind: str) -> str:
    if kind == "pp":
        return f"{value * 100:+.1f} 个百分点"
    return pct(value, 0 if kind == "share" else 1, signed=kind == "pct")


def to_record(m: dict, statuses: dict) -> dict:
    """需警惕的题，记下当时的指标值。"""
    out = {}
    for key, status in statuses.items():
        if status == "warn" and key in TRACKED:
            value = TRACKED[key][1](m)
            if value is not None:
                out[key] = {"label": TRACKED[key][0], "value": value}
    return out


def compare(prior: dict | None, m: dict) -> list[dict]:
    """prior 是上一期的 {report_end, tracked}。返回每道上期预警题的本期值和变化方向。"""
    rows = []
    for key, old in ((prior or {}).get("tracked") or {}).items():
        label, getter, direction, tolerance, kind = TRACKED[key]
        now = getter(m)
        if now is None:
            continue
        change = (now - old["value"]) * direction
        state = "improved" if change > tolerance else "worse" if change < -tolerance else "same"
        rows.append({"key": key, "label": label, "before": old["value"], "after": now, "kind": kind, "state": state})
    return rows
