"""财报解读的模板答案：模型失败、某一项没通过核对、或没有配置模型时用它；也是硬阈值的执行者（模型的状态越界，以这里为准）。

只用事实表里的数字，每条都带引用。状态：support 偏正面、neutral 中性、warn 需警惕、missing 数据缺失、na 不适用。
问题编号沿用设计稿（第 1、9、10、11 题随后续阶段上线，现在没有）。
"""

from __future__ import annotations

from app.services.finance_detail.rules import expectation_level, profit_yoy
from app.services.finance_detail.rules import runway as rules_runway

QUESTIONS = [
    ("q1", "这份财报最大的变化是什么", "代码挑出的最强信号，和它的另一面"),
    ("q2", "增长在加速还是减速", "单季同比判方向，校正环比判拐点"),
    ("q3", "收入和利润走得一致吗", "不一致时看原因出在哪里"),
    ("q4", "利润里有多少是主业赚的", "扣非占归母的比例"),
    ("q5", "有没有一次性因素扭曲本期", "非经常损益、财务费用等的影响"),
    ("q6", "利润率在改善还是恶化", "主因是毛利还是费用"),
    ("q7", "现金流跟上利润了吗", "经营现金流和净利润的比例"),
    ("q8", "资产负债表有没有预警", "应收、存货的增速是否跑在营收、成本前面"),
    ("q9", "和预期比怎么样", "业绩预告、朴素预期（券商一致预期暂未接入）"),
    ("q10", "市场反应和财报一致吗", "披露后股价相对行业怎么走，是公司问题还是行业问题"),
    ("q11", "上期的风险点缓解了吗", "上期判为需警惕的项，本期是缓解、持续还是恶化"),
    ("q12", "一句话总结", "这份财报带来的最大变化是什么"),
]
SUMMARY_KEY = "q12"
TOPIC = {"q2": "增长", "q3": "收入和利润不一致", "q4": "主业含金量", "q5": "一次性因素", "q6": "利润率", "q7": "现金流", "q8": "资产负债表", "q9": "预期差", "q10": "市场反应与财报背离", "q11": "上期风险点没有缓解"}
JUDGED = ("q2", "q3", "q4", "q5", "q6", "q7", "q8", "q9", "q10", "q11")
# 状态由事实按固定标准决定：模型的状态和参考状态正反相反时，以参考状态为准
GUARDED = JUDGED
# 每题的事实子集：只把子集送进提示词，核对时也只在子集内检查引用
RELEVANT = {
    "q2": ["ff.growth.q_rev", "ff.growth.q_rev_yoy", "ff.growth.q_np_yoy", "ff.growth.q_ded_yoy", "ff.growth.cum_rev_yoy", "ff.growth.cum_np_yoy",
           "ff.growth.loss_change", "ff.growth.loss_rate", "ff.growth.consolidation", "ff.growth.qoq_rev", "ff.growth.qoq_rev_adj", "ff.growth.adj_state", "ff.growth.rev_accel", "ff.growth.series"],
    "q3": ["ff.growth.q_rev_yoy", "ff.growth.q_np_yoy", "ff.growth.q_ded_yoy", "ff.growth.scissors", "ff.growth.loss_change", "ff.margin.gm_yoy",
           "ff.margin.expense_yoy", "ff.margin.driver", "ff.growth.consolidation", "ff.quality.nonrec", "ff.quality.fin_exp", "ff.bridge.delta", "ff.bridge.revenue",
           "ff.bridge.gross_margin", "ff.bridge.expense", "ff.bridge.nonrec", "ff.bridge.tax_minority", "ff.bridge.other", "ff.bridge.top_drag", "ff.bridge.top_boost"],
    "q4": ["ff.quality.np_cum", "ff.quality.ded_cum", "ff.quality.ded_to_np", "ff.quality.nonrec", "ff.quality.nonrec_share"],
    "q5": ["ff.quality.nonrec", "ff.quality.nonrec_share", "ff.quality.invest", "ff.quality.fair_value", "ff.quality.forex", "ff.quality.subsidy",
           "ff.quality.disposal", "ff.quality.fin_exp", "ff.quality.fin_exp_shift", "ff.quality.impair_change", "ff.quality.np_cum", "ff.time.restated",
           "ff.bridge.delta", "ff.bridge.nonrec", "ff.bridge.expense", "ff.bridge.top_drag"],
    "q6": ["ff.margin.gm", "ff.margin.gm_yoy", "ff.margin.gm_qoq", "ff.margin.nm", "ff.margin.nm_yoy", "ff.margin.sell_yoy", "ff.margin.admin_yoy",
           "ff.margin.rd_yoy", "ff.margin.fin_yoy", "ff.margin.expense_yoy", "ff.margin.driver", "ff.margin.cycle", "ff.margin.lines", "ff.bridge.gross_margin", "ff.bridge.expense",
           "ff.bridge.revenue", "ff.bridge.top_drag", "ff.bridge.top_boost"],
    "q7": ["ff.cash.ocf_cum", "ff.cash.ocf_to_np", "ff.cash.ocf_q", "ff.cash.ocf_to_np_q", "ff.cash.ocf_to_np_prev_q", "ff.cash.ocf_to_np_4q",
           "ff.cash.neg_run", "ff.cash.capex_yoy", "ff.quality.ded_to_np", "ff.cash.burn", "ff.cash.runway"],
    "q8": ["ff.balance.ar_yoy", "ff.balance.rev_yoy", "ff.balance.ar_gap", "ff.balance.inv_yoy", "ff.balance.cost_yoy", "ff.balance.inv_gap",
           "ff.balance.contract_liab_yoy", "ff.balance.debt", "ff.balance.cash", "ff.balance.goodwill"],
}
RELEVANT["q9"] = ["ff.expect.preview", "ff.expect.preview_dev", "ff.expect.preview_position", "ff.expect.reason", "ff.expect.naive_rev",
                  "ff.expect.naive_np", "ff.expect.naive_note", "ff.expect.consensus", "ff.growth.q_rev_yoy", "ff.growth.q_np_yoy"]
RELEVANT["q11"] = ["ff.track.summary"] + [f"ff.track.{k}" for k in ("q2", "q3", "q4", "q5", "q6", "q7", "q8", "q10")]
# 第 1 题：增长、利润桥、含金量三组事实，再加代码挑出的正负信号候选
RELEVANT["q1"] = sorted(set(RELEVANT["q2"]) | set(RELEVANT["q4"]) | {f for f in RELEVANT["q3"] if f.startswith("ff.bridge.")} | {"ff.margin.cycle"})
RELEVANT["q9"] += ["ff.valuation.ttm", "ff.valuation.change", "ff.valuation.note"]
RELEVANT["q10"] = ["ff.react.base", "ff.react.days_after", "ff.react.tone", "ff.react.pre5", "ff.react.after1", "ff.react.after3", "ff.react.after5",
                   "ff.react.volume", "ff.peer.rev_pctile", "ff.peer.np_pctile", "ff.peer.ded_pctile", "ff.growth.q_rev_yoy", "ff.growth.q_np_yoy", "ff.growth.q_ded_yoy"]
# 第 12 题看全局：各题最要紧的事实，加上时效和数据质量
RELEVANT["q12"] = sorted({fact for key in JUDGED for fact in RELEVANT[key]} | {
    "ff.time.report", "ff.time.audit", "ff.time.forecast", "ff.data.single_source", "ff.data.adj_ok", "ff.data.periods_behind",
})


def relevant(available: set[str], signals: dict | None = None) -> dict[str, list[str]]:
    """每题的事实子集。第 1、12 题再加上正负信号候选指向的事实（核对"必须引用"时要用）。"""
    subsets = {key: [fact for fact in ids if fact in available] for key, ids in RELEVANT.items()}
    extra = [fact for side in ("positive", "negative") for fact in (signals or {}).get(side, []) if fact in available]
    for key in ("q1", "q12"):
        subsets[key] = sorted(set(subsets[key]) | set(extra))
    return subsets


def _item(key: str, status: str, answer: str, cites: list[str]) -> dict:
    return {"key": key, "status": status, "answer": answer, "cites": cites, "source": "template"}


def _cites(text: dict, *ids: str) -> list[str]:
    return [fact for fact in ids if fact in text]


def answers(d: dict, text: dict[str, str]) -> list[dict]:
    m, hard = d["m"], d["bounds"]
    g, q, mg, c, b = m["growth"], m["quality"], m["margin"], m["cash"], m["balance"]
    t = lambda fact: text.get(fact, "—")   # noqa: E731
    out = []

    # q1 最大的变化：代码挑出的最强信号，和它的另一面
    sig, names = d.get("signals") or {}, d.get("labels") or {}
    top, side = sig.get("top"), sig.get("top_side")
    if not top:
        out.append(_item("q1", "missing", "数据缺失：没有挑出明显的正面或负面信号。", []))
    else:
        other = (sig.get("negative" if side == "positive" else "positive") or [None])[0]
        said = f"这份财报最大的变化：{names.get(top, top)} {t(top)}（{'正面' if side == 'positive' else '负面'}）"
        if other:
            said += f"；另一面：{names.get(other, other)} {t(other)}（{'负面' if side == 'positive' else '正面'}）"
        high = ["ff.margin.cycle"] if (m.get("cycle") or {}).get("high") and "ff.margin.cycle" in text else []
        out.append(_item("q1", "support" if side == "positive" else "warn", said + "。" + ("毛利率处在近 5 年高位，利润可能处于周期高位。" if high else ""), [top] + ([other] if other else []) + high))

    # q2 增长
    rev, prof = g["q_rev_yoy"], profit_yoy(g)
    if g["loss_now"] or g["loss_prior"]:
        change = g["loss_change"] or 0
        status = "neutral" if change > 0 else "warn"
        rate = f"，单季亏损率 {t('ff.growth.loss_rate')}" if "ff.growth.loss_rate" in text else ""
        out.append(_item("q2", status, f"公司处在亏损中，单季归母较去年同期{'减亏' if change > 0 else '增亏'} {t('ff.growth.loss_change').split(' ', 1)[-1]}{rate}，不算增速百分比。", _cites(text, "ff.growth.loss_change", "ff.growth.loss_rate", "ff.growth.q_np")))
    elif rev is None or prof is None:
        out.append(_item("q2", "missing", "数据缺失：缺少去年同期的单季数据，这一项无法判断。", []))
    else:
        if hard["q2"]["force_warn"]:
            status = "warn"
        elif hard["q2"]["forbid_support"]:
            status = "warn" if rev < 0 and prof < 0 else "neutral"
        else:
            status = "support"
        trend = "加速" if (g["rev_accel_pp"] or 0) > 0.05 else "放缓" if (g["rev_accel_pp"] or 0) < -0.05 else "基本稳定"
        out.append(_item("q2", status, f"单季营收同比 {t('ff.growth.q_rev_yoy')}，扣非同比 {t('ff.growth.q_ded_yoy')}，增速较上一季{trend}；校正环比判断：{t('ff.growth.adj_state')}。",
                         _cites(text, "ff.growth.q_rev_yoy", "ff.growth.q_ded_yoy", "ff.growth.rev_accel", "ff.growth.adj_state")))

    # q3 收入和利润
    gap = g["scissors_pp"]
    if gap is None:
        out.append(_item("q3", "missing", "数据缺失：亏损或缺少基期，营收和利润的增速差无法计算。", []))
    else:
        opposite = (g["q_rev_yoy"] or 0) * (g["q_np_yoy"] or 0) < 0
        status = "warn" if opposite or abs(gap) >= 0.20 else "support" if abs(gap) < 0.10 else "neutral"
        if "ff.bridge.top_drag" in text:
            driver = f"利润桥里拖累最大的是{t('ff.bridge.top_drag')}"
            if "ff.bridge.top_boost" in text:
                driver += f"，贡献最大的是{t('ff.bridge.top_boost')}"
        else:
            driver = f"利润率变化的主因是{t('ff.margin.driver')}" if "ff.margin.driver" in text else "利润率变化缺少对比"
        out.append(_item("q3", status, f"单季营收同比 {t('ff.growth.q_rev_yoy')}，归母同比 {t('ff.growth.q_np_yoy')}，增速差 {t('ff.growth.scissors')}；{driver}。",
                         _cites(text, "ff.growth.q_rev_yoy", "ff.growth.q_np_yoy", "ff.growth.scissors", "ff.bridge.top_drag", "ff.bridge.top_boost", "ff.margin.driver")))

    # q4 主业含金量
    if d["chronic_loss"]:
        out.append(_item("q4", "na", "公司连续 4 个季度扣非为负，这一项不适用。", []))
    elif q["ded_to_np"] is None and not q["ded_negative_np_positive"]:
        out.append(_item("q4", "missing", "数据缺失：缺少扣非净利润或归母为负，这一项无法判断。", []))
    else:
        ratio = q["ded_to_np"]
        if hard["q4"]["force_warn"] or (ratio is not None and ratio < 0.5):
            status = "warn"
        elif hard["q4"]["forbid_support"]:
            status = "neutral"
        else:
            status = "support"
        shown = f"扣非占归母 {t('ff.quality.ded_to_np')}" if ratio is not None else "归母为正但扣非为负"
        out.append(_item("q4", status, f"{shown}，累计归母 {t('ff.quality.np_cum')}、扣非 {t('ff.quality.ded_cum')}。", _cites(text, "ff.quality.ded_to_np", "ff.quality.np_cum", "ff.quality.ded_cum")))

    # q5 一次性因素
    share, shift = q["nonrec_share"], q["fin_exp_shift_to_np"]
    if share is None and shift is None:
        out.append(_item("q5", "missing", "数据缺失：缺少非经常损益和财务费用的对比。", []))
    else:
        big_shift = shift is not None and abs(shift) >= 0.30
        if hard["q5"]["force_warn"] or (share is not None and share > 0.50):
            status = "warn"
        elif hard["q5"]["forbid_support"] or big_shift:
            status = "neutral"
        else:
            status = "support"
        bits = []
        if share is not None:
            bits.append(f"非经常损益占归母 {t('ff.quality.nonrec_share')}")
        if "ff.quality.fin_exp" in text:
            bits.append(f"财务费用 {t('ff.quality.fin_exp')}")
        if "ff.bridge.nonrec" in text:
            bits.append(f"利润桥里非经常损益变化 {t('ff.bridge.nonrec')}")
        out.append(_item("q5", status, "，".join(bits) + ("；财务费用同比变动幅度很大，汇兑或衍生品可能影响了利润" if big_shift else "") + "。",
                         _cites(text, "ff.quality.nonrec_share", "ff.quality.fin_exp", "ff.quality.fin_exp_shift", "ff.bridge.nonrec")))

    # q6 利润率
    gm, nm = mg["gm_yoy_pp"], mg["nm_yoy_pp"]
    if gm is None and nm is None:
        out.append(_item("q6", "missing", "数据缺失：缺少去年同期的利润率。", []))
    else:
        worse = (gm is not None and gm <= -0.02) or (nm is not None and nm <= -0.02)
        better = (gm is not None and gm >= 0.01) and (nm is None or nm >= 0)
        status = "warn" if worse else "support" if better else "neutral"
        bridge = f"；利润桥里毛利率变化 {t('ff.bridge.gross_margin')}、费用率变化 {t('ff.bridge.expense')}" if "ff.bridge.gross_margin" in text else ""
        out.append(_item("q6", status, f"单季毛利率 {t('ff.margin.gm')}，同比 {t('ff.margin.gm_yoy')}；净利率同比 {t('ff.margin.nm_yoy')}；费用率合计同比 {t('ff.margin.expense_yoy')}{bridge}。",
                         _cites(text, "ff.margin.gm", "ff.margin.gm_yoy", "ff.margin.nm_yoy", "ff.margin.expense_yoy", "ff.bridge.gross_margin", "ff.bridge.expense")))

    # q7 现金流（持续亏损的公司改看现金消耗和剩余可用季数）
    ratio = c["ocf_to_np_cum"]
    if d["chronic_loss"]:
        left = rules_runway(m)
        if left is None:
            out.append(_item("q7", "neutral", "公司持续亏损，但经营现金流没有净流出，现金没有被消耗。", _cites(text, "ff.cash.ocf_cum")))
        else:
            status = "warn" if hard["q7"]["force_warn"] else "neutral" if hard["q7"]["forbid_support"] else "support"
            out.append(_item("q7", status, f"公司持续亏损，近 4 季平均每季经营现金净流出 {t('ff.cash.burn')}，{t('ff.cash.runway')}。", _cites(text, "ff.cash.burn", "ff.cash.runway")))
    elif ratio is None and not c["ocf_cum_negative"]:
        out.append(_item("q7", "missing", "数据缺失：缺少经营现金流或归母为负，这一项无法判断。", []))
    else:
        if hard["q7"]["force_warn"]:
            status = "warn"
        elif hard["q7"]["forbid_support"]:
            status = "neutral"
        else:
            status = "support" if ratio is not None and ratio >= 0.80 else "neutral"
        shown = f"累计经营现金流占归母 {t('ff.cash.ocf_to_np')}" if ratio is not None else f"累计经营现金流 {t('ff.cash.ocf_cum')}"
        out.append(_item("q7", status, f"{shown}，单季经营现金流 {t('ff.cash.ocf_q')}。", _cites(text, "ff.cash.ocf_to_np", "ff.cash.ocf_cum", "ff.cash.ocf_q", "ff.cash.neg_run")))

    # q8 资产负债表（房地产改看合同负债）
    if m["industry_rule"] == "realestate" and b["contract_liab_yoy"] is not None:
        status = "warn" if hard["q8"]["force_warn"] else "neutral" if hard["q8"]["forbid_support"] else "support"
        out.append(_item("q8", status, f"房地产公司主要看合同负债（预收）：同比 {t('ff.balance.contract_liab_yoy')}。", _cites(text, "ff.balance.contract_liab_yoy")))
    elif b["ar_gap_pp"] is None and b["inv_gap_pp"] is None:
        out.append(_item("q8", "missing", "数据缺失：缺少去年同期的资产负债表。", []))
    else:
        if hard["q8"]["force_warn"]:
            status = "warn"
        elif hard["q8"]["forbid_support"] or (b["goodwill_to_equity"] or 0) > 0.30:
            status = "neutral"
        else:
            status = "support"
        out.append(_item("q8", status, f"应收同比 {t('ff.balance.ar_yoy')}（营收 {t('ff.balance.rev_yoy')}），存货同比 {t('ff.balance.inv_yoy')}（成本 {t('ff.balance.cost_yoy')}）。",
                         _cites(text, "ff.balance.ar_yoy", "ff.balance.rev_yoy", "ff.balance.inv_yoy", "ff.balance.cost_yoy")))

    # q9 预期差：有业绩预告就看预告，没有就只对照朴素预期并注明
    level, basis = expectation_level(m, d["th"])
    if level == "na":
        out.append(_item("q9", "missing", "数据缺失：没有业绩预告，也没有足够的历史可以外推朴素预期。", []))
    else:
        status = {"beat": "support", "inline": "neutral", "miss": "warn"}[level]
        if hard["q9"]["force_warn"]:
            status = "warn"
        elif hard["q9"]["forbid_support"] and status == "support":
            status = "neutral"
        if basis == "preview":
            said = f"公司业绩预告：{t('ff.expect.preview')}；实际相对预告中值 {t('ff.expect.preview_dev')}，{t('ff.expect.preview_position')}。"
            cites = _cites(text, "ff.expect.preview", "ff.expect.preview_dev", "ff.expect.preview_position", "ff.expect.reason")
        else:
            said = f"没有业绩预告，只能对照朴素预期（{t('ff.expect.naive_note')}）：单季营收 {t('ff.expect.naive_rev')}"
            said += f"；归母 {t('ff.expect.naive_np')}" if "ff.expect.naive_np" in text else ""
            said += "。券商一致预期暂未接入。"
            cites = _cites(text, "ff.expect.naive_rev", "ff.expect.naive_np", "ff.expect.naive_note", "ff.expect.consensus")
        out.append(_item("q9", status, said, cites))

    # q10 市场反应
    out.append(_reaction_answer(d, text, t))

    # q11 上期预警跟踪：第一次分析、或上期没有需警惕的项，不适用
    track = d.get("track") or []
    if not track:
        out.append(_item("q11", "na", "上期没有需警惕的项，或这是第一次分析，这一项不适用。", []))
    else:
        counts = {state: sum(1 for r in track if r["state"] == state) for state in ("improved", "same", "worse")}
        status = "warn" if counts["worse"] > counts["improved"] else "support" if counts["improved"] > counts["worse"] else "neutral"
        shown = [f"ff.track.{r['key']}" for r in track[:2]]
        out.append(_item("q11", status, t("ff.track.summary") + "；" + "；".join(t(fact) for fact in shown) + "。", ["ff.track.summary"] + shown))

    # q12 总结（"距最近财报 X 天"由程序加在前面，这里和模型都不写）
    judged = [row for row in out if row["key"] in JUDGED and row["status"] in ("support", "warn")]
    good, bad = sum(1 for row in judged if row["status"] == "support"), sum(1 for row in judged if row["status"] == "warn")
    status = "support" if good - bad >= 2 else "warn" if bad - good >= 2 else "neutral"
    flagged = [row for row in out if row["key"] in JUDGED and row["status"] == "warn"]
    signals, labels = d.get("signals") or {}, d.get("labels") or {}
    pos, neg = (signals.get("positive") or [])[:1], (signals.get("negative") or [])[:1]
    sides = "".join(f"{name}：{labels.get(fact, fact)} {t(fact)}。" for name, picked in (("正面信号", pos), ("负面信号", neg)) for fact in picked)
    tail = f"需要留意的有 {len(flagged)} 项：{'、'.join(TOPIC[row['key']] for row in flagged)}。" if flagged else "没有触发需警惕的项。"
    high = ["ff.margin.cycle"] if (m.get("cycle") or {}).get("high") and "ff.margin.cycle" in text else []
    out.append(_item("q12", status, sides + ("利润可能处于周期高位。" if high else "") + tail, pos + neg + high))
    return out


def _reaction_answer(d: dict, text: dict, t) -> dict:
    r = d["m"].get("reaction")
    shown = [(k, row) for k, row in sorted(((r or {}).get("after") or {}).items()) if row]
    if not shown:
        return {"key": "q10", "status": "missing", "answer": "数据缺失：披露后的行情还没形成（至少要过 1 个交易日），这一项无法判断。", "cites": [], "source": "template"}
    k, row = shown[-1]
    excess = row["excess"] if row["excess"] is not None else row["stock"]
    threshold = d["th"]["reaction_pp"]
    tone = d["tone"]["score"]
    if (tone > 0 and excess >= threshold) or (tone < 0 and excess <= -threshold):
        status, verdict = "support", "股价反应和财报方向一致"
    elif (tone > 0 and excess <= -threshold) or (tone < 0 and excess >= threshold):
        status, verdict = "warn", "股价反应和财报方向相反，可能已提前反应，或市场在看别的因素"
    else:
        status, verdict = "neutral", "股价反应不明显，或财报本身方向分化"
    # 公司还是行业：行业同期涨跌比个股的超额还大且同向，主要是行业因素
    cause = ""
    if row["sector"] is not None and row["stock"] * row["sector"] > 0 and abs(row["sector"]) >= abs(excess):
        cause = "；同期行业指数的涨跌幅度更大，主要是行业因素"
    elif row["sector"] is not None:
        cause = "；超额部分更多来自公司自身"
    peer = f"；单季营收同比在同行里是 {t('ff.peer.rev_pctile')}" if "ff.peer.rev_pctile" in text else ""
    cites = [fact for fact in ("ff.react.tone", f"ff.react.after{k}", "ff.peer.rev_pctile") if fact in text]
    return {"key": "q10", "status": status, "answer": f"财报方向：{t('ff.react.tone')}。披露后第 {k} 个交易日，{t(f'ff.react.after{k}')}，{verdict}{cause}{peer}。", "cites": cites, "source": "template"}
