"""顶部那一句：按已经算好的数字用模板拼，带"距最近财报 X 天"。AI 解读在 app/finance_review。"""

from __future__ import annotations

from app.services.finance_detail.rules import growth_level, pct, period_label, yi


def write_text(m: dict) -> str:
    g, q, c = m["growth"], m["quality"], m["cash"]
    days = m["time"]["days_since"]
    head = f"{period_label(m)}，距最近财报 {days} 天" if days is not None else period_label(m)
    if growth_level(m) == "loss":
        move = f"单季归母 {yi(g['q_np'])}，较去年同期{'减亏' if (g['loss_change'] or 0) > 0 else '增亏'} {yi(abs(g['loss_change'] or 0))}"
    else:
        move = f"单季营收同比 {pct(g['q_rev_yoy'], 0, True)}、归母同比 {pct(g['q_np_yoy'], 0, True)}、扣非同比 {pct(g['q_ded_yoy'], 0, True)}"
    cash = f"经营现金流/净利润 {pct(c['ocf_to_np_cum'])}" if c["ocf_to_np_cum"] is not None else "经营现金流缺少可比数据"
    ded = f"扣非/归母 {pct(q['ded_to_np'])}" if q["ded_to_np"] is not None else "扣非缺少可比数据"
    return f"{head}：{move}；{ded}，{cash}。"
