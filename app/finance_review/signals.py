"""正负信号候选：代码按规则预先选出这份财报最强的 3 个正面信号和 3 个负面信号，附在第 12 题的提示词里。

第 12 题的核对规则：两边候选都不为空时，回答必须各引用至少一个正面、一个负面信号。每个信号指向一条事实编号，分数是"超出阈值有多远"，只用来排序。
"""

from __future__ import annotations

LIMIT = 3


def pick(m: dict, th: dict, available: set[str]) -> dict[str, list[str]]:
    """返回 {"positive": [事实编号], "negative": [事实编号]}，按强弱排序，事实表里没有的编号不选。"""
    g, q, mg, c, b = m["growth"], m["quality"], m["margin"], m["cash"], m["balance"]
    positive: list[tuple[float, str]] = []
    negative: list[tuple[float, str]] = []

    def add(side: list, score: float, fact_id: str) -> None:
        if fact_id in available:
            side.append((score, fact_id))

    def growth_signal(value, fact_id: str) -> None:
        if value is None:
            return
        add(positive if value > 0 else negative, abs(value) / 0.20, fact_id)

    growth_signal(g["q_rev_yoy"], "ff.growth.q_rev_yoy")
    growth_signal(g["q_ded_yoy"], "ff.growth.q_ded_yoy")
    growth_signal(g["q_np_yoy"], "ff.growth.q_np_yoy")
    if g["rev_accel_pp"] is not None and abs(g["rev_accel_pp"]) >= th["accel_pp"]:
        add(positive if g["rev_accel_pp"] > 0 else negative, abs(g["rev_accel_pp"]) / 0.10, "ff.growth.rev_accel")
    if g["scissors_pp"] is not None and abs(g["scissors_pp"]) >= th["scissors_warn_pp"]:
        add(negative, abs(g["scissors_pp"]) / th["scissors_warn_pp"], "ff.growth.scissors")
    if g["loss_change"] is not None and (g["loss_now"] or g["loss_prior"]):
        add(positive if g["loss_change"] > 0 else negative, 1.0, "ff.growth.loss_change")

    ratio = q["ded_to_np"]
    if ratio is not None:
        if ratio >= th["ded_to_np_good"]:
            add(positive, 0.5 + (ratio - th["ded_to_np_good"]) / 0.2, "ff.quality.ded_to_np")
        elif ratio < th["ded_to_np_support_min"]:
            add(negative, 0.5 + (th["ded_to_np_support_min"] - ratio) / 0.2, "ff.quality.ded_to_np")
    if q["nonrec_share"] is not None and q["nonrec_share"] > th["nonrec_share_max"]:
        add(negative, 0.5 + (q["nonrec_share"] - th["nonrec_share_max"]) / 0.3, "ff.quality.nonrec_share")
    shift = q["fin_exp_shift_to_np"]
    if shift is not None and shift >= th["fin_exp_shift_to_np"]:    # 财务费用同比上升，吃掉利润
        add(negative, shift / th["fin_exp_shift_to_np"], "ff.quality.fin_exp_shift")

    ocf = c["ocf_to_np_cum"]
    if c["ocf_cum_negative"]:
        add(negative, 1.5, "ff.cash.ocf_cum")
    elif ocf is not None:
        if ocf >= th["ocf_to_np_good"]:
            add(positive, 0.5 + (ocf - th["ocf_to_np_good"]) / 0.5, "ff.cash.ocf_to_np")
        elif ocf < th["ocf_to_np_support_min"]:
            add(negative, 0.5 + (th["ocf_to_np_support_min"] - ocf) / 0.5, "ff.cash.ocf_to_np")
    if c["ocf_negative_run"] >= th["ocf_negative_run"]:
        add(negative, 1.5, "ff.cash.neg_run")

    for value, fact_id in ((mg["gm_yoy_pp"], "ff.margin.gm_yoy"), (mg["nm_yoy_pp"], "ff.margin.nm_yoy")):
        if value is None:
            continue
        if value >= th["margin_pp_good"]:
            add(positive, value / 0.02, fact_id)
        elif value <= -th["margin_pp_warn"]:
            add(negative, abs(value) / th["margin_pp_warn"], fact_id)

    if b["ar_gap_pp"] is not None and b["ar_gap_pp"] >= th["ar_gap_pp"]:
        add(negative, b["ar_gap_pp"] / th["ar_gap_pp"], "ff.balance.ar_gap")
    if b["inv_gap_pp"] is not None and b["inv_gap_pp"] >= th["inv_gap_pp"]:
        add(negative, b["inv_gap_pp"] / th["inv_gap_pp"], "ff.balance.inv_gap")
    if b["goodwill_to_equity"] is not None and b["goodwill_to_equity"] > th["goodwill_to_equity"]:
        add(negative, 1.0, "ff.balance.goodwill")
    if b["contract_liab_yoy"] is not None and b["contract_liab_yoy"] > 0.20:
        add(positive, b["contract_liab_yoy"] / 0.5, "ff.balance.contract_liab_yoy")

    reaction = m.get("reaction") or {}
    shown = [(k, row) for k, row in sorted((reaction.get("after") or {}).items()) if row]
    if shown:   # 取已经有的最长一档（披露后第 5、3、1 个交易日）
        k, row = shown[-1]
        excess = row["excess"] if row["excess"] is not None else row["stock"]
        if abs(excess) >= th["reaction_pp"]:
            add(positive if excess > 0 else negative, abs(excess) / th["reaction_pp"], f"ff.react.after{k}")

    everything = [(score, fact_id, "positive") for score, fact_id in positive] + [(score, fact_id, "negative") for score, fact_id in negative]
    strongest = max(everything, default=None)
    return {
        "positive": _top(positive), "negative": _top(negative),
        "top": strongest[1] if strongest else None, "top_side": strongest[2] if strongest else None,   # 第 1 题"最大的变化"取这一条
    }


def _top(side: list[tuple[float, str]]) -> list[str]:
    out: list[str] = []
    for _score, fact_id in sorted(side, key=lambda row: -row[0]):
        if fact_id not in out:
            out.append(fact_id)
        if len(out) == LIMIT:
            break
    return out

