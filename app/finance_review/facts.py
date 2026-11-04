"""财报事实表：代码算好所有数字，模型只引用编号（ff.<分组>.<名称>）。金额统一成"亿元"，同比写"+31.0%"，百分点差写"+2.0 个百分点"。

输入是 finance_detail 的指标字典。返回 (facts, derived)：facts 给模型看，derived 是原始数值和硬阈值，给模板答案用。
"""

from __future__ import annotations

from app.finance_review import signals as signal_picker
from app.finance_review import tracking
from app.finance_review.template import TOPIC
from app.reviews.core import FactSheet
from app.services.finance_detail import rules
from app.services.finance_detail.config import thresholds
from app.synthesis.types import Fact

GROUP_ORDER = ("报告期与时效", "增长", "利润桥", "利润含金量", "利润率", "现金流", "资产负债表信号", "估值影响", "预期", "市场反应与同行", "上期预警跟踪", "数据质量")
UNITS = {"pct": "%", "pp": "个百分点", "yi": "亿元", "days": "天", "count": "", "text": "", "share": "%"}
VOLATILE = ("ff.time.days_since", "ff.time.days_to_next")   # 每天都在变的事实：不进缓存键，否则同一份财报每天都要重新问一遍模型

_SINGLE = {"tushare": "交易所单季口径", "diff": "累计数相减"}
_ADJ = {"accelerating": "明显快于往年同季", "decelerating": "明显慢于往年同季", "normal": "与往年同季相当"}


def _yi(value) -> str:
    return "—" if value is None else f"{value / 1e8:.2f} 亿元"


def _pct(value, digits: int = 1) -> str:
    return "—" if value is None else f"{value * 100:+.{digits}f}%"


def _share(value, digits: int = 0) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def _pp(value) -> str:
    return "—" if value is None else f"{value * 100:+.1f} 个百分点"


def build(m: dict, prior: dict | None = None) -> tuple[FactSheet, dict]:
    facts = FactSheet()
    d: dict = {"m": m, "bounds": rules.bounds(m)}

    def put(fact_id: str, label: str, group: str, kind: str, value, text: str) -> None:
        if value is None and not text:
            return
        facts.labels[fact_id] = label
        facts.meta[fact_id] = (group, kind)
        facts.append(Fact(fact_id, value, text))

    _time(m, put)
    _growth(m, put)
    _bridge(m, put)
    _quality(m, put)
    _margin(m, put)
    _cash(m, put)
    _balance(m, put)
    _valuation(m, put)
    _expect(m, put)
    _reaction(m, put, d)
    _track(m, prior, put, d)
    _data(m, put)
    d["chronic_loss"] = m["data"]["chronic_loss"]
    d["signals"] = signal_picker.pick(m, thresholds(m["industry_rule"]), {fact.id for fact in facts})
    d["labels"] = facts.labels
    return facts, d


def public(facts: FactSheet) -> list[dict]:
    rows = []
    for fact in facts:
        group, kind = facts.meta.get(fact.id, ("其他", "text"))
        rows.append({
            "id": fact.id, "label": facts.labels.get(fact.id, fact.id), "group": group, "kind": kind, "unit": UNITS.get(kind, ""),
            "value": round(fact.value, 6) if isinstance(fact.value, float) else fact.value, "text": fact.text,
        })
    order = {name: index for index, name in enumerate(GROUP_ORDER)}
    rows.sort(key=lambda row: order.get(row["group"], len(order)))
    return rows


def _time(m, put) -> None:
    t, g = m["time"], "报告期与时效"
    put("ff.time.report", "最新报告期", g, "text", None, rules.period_label(m))
    put("ff.time.announced", "披露日", g, "text", None, t["announced"] or "未知")
    if t["days_since"] is not None:
        put("ff.time.days_since", "距最近一份财报披露的天数", g, "days", t["days_since"], f"{t['days_since']} 天")
    if t["next_date"]:
        put("ff.time.next", "下一期披露日", g, "text", None, f"{t['next_date']}（{t['next_source']}）")
    if t["days_to_next"] is not None:
        put("ff.time.days_to_next", "距下一期披露的天数", g, "days", t["days_to_next"], f"{t['days_to_next']} 天")
    forecast = t["forecast"]
    put("ff.time.forecast", "最新业绩预告", g, "text", None, "无" if not forecast else f"{forecast.get('type') or ''}，预告 {forecast.get('end_date')} 期")
    put("ff.time.express", "最新业绩快报", g, "text", None, "无" if not t["express"] else f"{t['express'].get('end_date')} 期快报")
    audit = t["audit"]
    put("ff.time.audit", "最近年报的审计意见", g, "text", None, "未取到" if not audit else f"{audit['result'] or '未知'}（{audit['end_date']} 年报）")
    put("ff.time.restated", "是否有更正重述", g, "text", None, "是" if m["data"]["restated"] else "否")


def _growth(m, put) -> None:
    gr, g = m["growth"], "增长"
    put("ff.growth.q_rev", "单季营收", g, "yi", gr["q_rev"], _yi(gr["q_rev"]))
    put("ff.growth.q_rev_yoy", "单季营收同比", g, "pct", gr["q_rev_yoy"], _pct(gr["q_rev_yoy"]))
    put("ff.growth.q_np", "单季归母净利润", g, "yi", gr["q_np"], _yi(gr["q_np"]))
    if gr["q_np_yoy"] is not None:
        put("ff.growth.q_np_yoy", "单季归母净利润同比", g, "pct", gr["q_np_yoy"], _pct(gr["q_np_yoy"]))
    put("ff.growth.q_ded", "单季扣非净利润", g, "yi", gr["q_ded"], _yi(gr["q_ded"]))
    if gr["q_ded_yoy"] is not None:
        put("ff.growth.q_ded_yoy", "单季扣非净利润同比", g, "pct", gr["q_ded_yoy"], _pct(gr["q_ded_yoy"]))
    put("ff.growth.cum_rev_yoy", "累计营收同比", g, "pct", gr["cum_rev_yoy"], _pct(gr["cum_rev_yoy"]))
    put("ff.growth.cum_np_yoy", "累计归母净利润同比", g, "pct", gr["cum_np_yoy"], _pct(gr["cum_np_yoy"]))
    if gr["loss_now"] or gr["loss_prior"]:
        change = gr["loss_change"] or 0
        if gr["loss_now"] and gr["loss_prior"]:
            word = "减亏" if change > 0 else "增亏"
        elif gr["loss_prior"]:
            word = "扭亏为盈"
        else:
            word = "由盈转亏"
        put("ff.growth.loss_change", "单季归母较去年同期的变化（亏损或扭亏时不算百分比）", g, "yi", change, f"{word} {_yi(abs(change))}")
    put("ff.growth.qoq_rev", "单季营收环比（未校正）", g, "pct", gr["qoq_rev"], _pct(gr["qoq_rev"]))
    if gr["adj_available"]:
        put("ff.growth.qoq_rev_adj", "季节性校正后的营收环比（本季环比减过去 3 年同季均值）", g, "pp", gr["qoq_rev_adj"], _pp(gr["qoq_rev_adj"]))
        put("ff.growth.adj_state", "校正环比的判断（偏离超过 ±10 个百分点才算异常）", g, "text", None, _ADJ[gr["adj_state"]])
    else:
        put("ff.growth.adj_state", "校正环比的判断", g, "text", None, "历史不足 3 年，只报原始环比，未校正")
    if gr["rev_accel_pp"] is not None:
        put("ff.growth.rev_accel", "营收同比较上一季的变化", g, "pp", gr["rev_accel_pp"], _pp(gr["rev_accel_pp"]))
    if gr.get("loss_rate") is not None:
        put("ff.growth.loss_rate", "单季亏损率（单季归母亏损 ÷ 营收）", g, "share", gr["loss_rate"], _share(gr["loss_rate"], 1))
    cons = (m.get("lines") or {}).get("consolidation")
    if cons:
        put("ff.growth.consolidation", "疑似并表：新出现的产品线", g, "text", cons["sales"],
            f"主营构成里新出现产品线「{cons['item']}」，收入 {_yi(cons['sales'])}，占营收增量的 {_share(cons['share_of_increment'])}，增长可能来自并表")
    if gr["scissors_pp"] is not None:
        put("ff.growth.scissors", "营收增速减归母增速（单季，剪刀差）", g, "pp", gr["scissors_pp"], _pp(gr["scissors_pp"]))
    series = "；".join(
        f"{row['end'][:4]}Q{(int(row['end'][4:6]) - 1) // 3 + 1} 营收 {_yi(row['revenue'])}（同比 {_pct(row['revenue_yoy'], 0)}）、归母 {_yi(row['net_profit'])}"
        for row in m["quarters"]
    )
    put("ff.growth.series", "近 8 个季度的单季序列", g, "text", None, series)


BRIDGE_LABELS = {
    "revenue": "收入增长贡献", "gross_margin": "毛利率变化", "expense": "四项费用率变化",
    "nonrec": "非经常损益变化", "tax_minority": "所得税与少数股东损益变化", "other": "其他项目（税金及附加、减值等，前几项加总后的余数）",
}


def _bridge(m, put) -> None:
    b, g = m.get("bridge"), "利润桥"
    if not b:
        return
    put("ff.bridge.delta", "单季归母净利润同比变化（本期减去年同期）", g, "yi", b["delta"], f"{_yi(b['delta'])}（{_yi(b['np_before'])} → {_yi(b['np_now'])}）")
    for key, label in BRIDGE_LABELS.items():
        if b["parts"].get(key) is not None:
            put(f"ff.bridge.{key}", f"利润桥：{label}", g, "yi", b["parts"][key], _signed_yi(b["parts"][key]))
    if b["top_drag"]:
        put("ff.bridge.top_drag", "利润桥里拖累最大的一项", g, "text", None, f"{BRIDGE_LABELS[b['top_drag']]} {_signed_yi(b['parts'][b['top_drag']])}")
    if b["top_boost"]:
        put("ff.bridge.top_boost", "利润桥里贡献最大的一项", g, "text", None, f"{BRIDGE_LABELS[b['top_boost']]} {_signed_yi(b['parts'][b['top_boost']])}")


def _signed_yi(value) -> str:
    return "—" if value is None else f"{value / 1e8:+.2f} 亿元"


def _quality(m, put) -> None:
    q, g = m["quality"], "利润含金量"
    put("ff.quality.np_cum", "累计归母净利润", g, "yi", q["np_cum"], _yi(q["np_cum"]))
    put("ff.quality.ded_cum", "累计扣非净利润", g, "yi", q["ded_cum"], _yi(q["ded_cum"]))
    if q["ded_to_np"] is not None:
        put("ff.quality.ded_to_np", "扣非净利润占归母净利润的比例", g, "share", q["ded_to_np"], _share(q["ded_to_np"]))
    if q["nonrec"] is not None:
        put("ff.quality.nonrec", "累计非经常损益（归母减扣非）", g, "yi", q["nonrec"], _yi(q["nonrec"]))
    if q["nonrec_share"] is not None:
        put("ff.quality.nonrec_share", "非经常损益占归母净利润的比例", g, "share", q["nonrec_share"], _share(q["nonrec_share"]))
    labels = {"invest": "投资收益", "fair_value": "公允价值变动收益", "forex": "汇兑收益", "subsidy": "其他收益（含政府补助）", "disposal": "非流动资产处置"}
    for key, label in labels.items():
        if q["comp"].get(key) is not None:
            put(f"ff.quality.{key}", f"累计{label}", g, "yi", q["comp"][key], _yi(q["comp"][key]))
    if q["fin_exp"] is not None and q["fin_exp_before"] is not None:
        put("ff.quality.fin_exp", "累计财务费用（本期 vs 去年同期）", g, "yi", q["fin_exp"], f"{_yi(q['fin_exp'])}，去年同期 {_yi(q['fin_exp_before'])}")
    if q["fin_exp_shift_to_np"] is not None:
        put("ff.quality.fin_exp_shift", "财务费用同比变动占归母净利润的比例", g, "share", q["fin_exp_shift_to_np"], _share(abs(q["fin_exp_shift_to_np"])))
    if q["impair_change"] is not None:
        put("ff.quality.impair_change", "资产减值损失较去年同期的变化", g, "yi", q["impair_change"], _yi(q["impair_change"]))


def _margin(m, put) -> None:
    mg, g = m["margin"], "利润率"
    put("ff.margin.gm", "单季毛利率", g, "share", mg["gm"], _share(mg["gm"], 1))
    put("ff.margin.gm_yoy", "单季毛利率同比变动", g, "pp", mg["gm_yoy_pp"], _pp(mg["gm_yoy_pp"]))
    put("ff.margin.gm_qoq", "单季毛利率环比变动", g, "pp", mg["gm_qoq_pp"], _pp(mg["gm_qoq_pp"]))
    put("ff.margin.nm", "单季净利率", g, "share", mg["nm"], _share(mg["nm"], 1))
    put("ff.margin.nm_yoy", "单季净利率同比变动", g, "pp", mg["nm_yoy_pp"], _pp(mg["nm_yoy_pp"]))
    names = {"sell": "销售", "admin": "管理", "rd": "研发", "fin": "财务"}
    for key, name in names.items():
        put(f"ff.margin.{key}_yoy", f"{name}费用率同比变动", g, "pp", mg[f"{key}_yoy_pp"], _pp(mg[f"{key}_yoy_pp"]))
    put("ff.margin.expense_yoy", "四项费用率合计同比变动", g, "pp", mg["expense_yoy_pp"], _pp(mg["expense_yoy_pp"]))
    cycle = m.get("cycle") or {}
    if cycle.get("gm_pctile") is not None:
        put("ff.margin.cycle", "单季毛利率在近 5 年里的分位", g, "share", cycle["gm_pctile"],
            f"处在近 5 年（{cycle['quarters']} 个季度）的 {_share(cycle['gm_pctile'])} 分位" + ("，利润可能处于周期高位" if cycle["high"] else ""))
    lines = m.get("lines") or {}
    if lines.get("gm_changes"):
        put("ff.margin.lines", "各产品线毛利率同比变动（主营构成，半年报和年报才有）", g, "text", None,
            "；".join(f"{row['item']}（占比 {_share(row['share'])}）{_pp(row['change_pp'])}" for row in lines["gm_changes"]))
    if mg["driver"]:
        put("ff.margin.driver", "毛利率变动和费用率变动里，影响更大的一项", g, "text", None, "毛利率变动" if mg["driver"] == "gross" else "费用率变动")


def _cash(m, put) -> None:
    c, g = m["cash"], "现金流"
    put("ff.cash.ocf_cum", "累计经营现金流净额", g, "yi", c["ocf_cum"], _yi(c["ocf_cum"]))
    if c["ocf_to_np_cum"] is not None:
        put("ff.cash.ocf_to_np", "累计经营现金流占归母净利润的比例", g, "share", c["ocf_to_np_cum"], _share(c["ocf_to_np_cum"]))
    put("ff.cash.ocf_q", "单季经营现金流净额", g, "yi", c["ocf_q"], _yi(c["ocf_q"]))
    if c["ocf_to_np_q"] is not None:
        put("ff.cash.ocf_to_np_q", "单季经营现金流占归母净利润的比例", g, "share", c["ocf_to_np_q"], _share(c["ocf_to_np_q"]))
    if c["ocf_to_np_prev_q"] is not None:
        put("ff.cash.ocf_to_np_prev_q", "上一季经营现金流占归母净利润的比例", g, "share", c["ocf_to_np_prev_q"], _share(c["ocf_to_np_prev_q"]))
    if c["ocf_to_np_4q"] is not None:
        put("ff.cash.ocf_to_np_4q", "近 4 季滚动经营现金流占归母净利润的比例", g, "share", c["ocf_to_np_4q"], _share(c["ocf_to_np_4q"]))
    if c.get("burn"):
        put("ff.cash.burn", "近 4 季平均每季经营现金净流出", g, "yi", c["burn"], _yi(c["burn"]))
        left = rules.runway(m)
        if left is not None:
            put("ff.cash.runway", "按这个速度货币资金还能撑几个季度", g, "count", left, f"货币资金 {_yi(m['balance']['cash_balance_for_runway'])}，约 {left:.1f} 个季度")
    put("ff.cash.neg_run", "单季经营现金流连续为负的季数", g, "count", c["ocf_negative_run"], f"{c['ocf_negative_run']} 季")
    if c["capex_yoy"] is not None:
        put("ff.cash.capex_yoy", "累计资本开支同比", g, "pct", c["capex_yoy"], _pct(c["capex_yoy"]))


def _balance(m, put) -> None:
    b, g = m["balance"], "资产负债表信号"
    put("ff.balance.ar_yoy", "应收款项同比增速", g, "pct", b["ar_yoy"], _pct(b["ar_yoy"]))
    put("ff.balance.rev_yoy", "累计营收同比增速（对照用）", g, "pct", b["rev_yoy"], _pct(b["rev_yoy"]))
    put("ff.balance.ar_gap", "应收增速减营收增速", g, "pp", b["ar_gap_pp"], _pp(b["ar_gap_pp"]))
    put("ff.balance.inv_yoy", "存货同比增速", g, "pct", b["inv_yoy"], _pct(b["inv_yoy"]))
    put("ff.balance.cost_yoy", "累计营业成本同比增速（对照用）", g, "pct", b["cost_yoy"], _pct(b["cost_yoy"]))
    put("ff.balance.inv_gap", "存货增速减成本增速", g, "pp", b["inv_gap_pp"], _pp(b["inv_gap_pp"]))
    put("ff.balance.contract_liab_yoy", "合同负债（预收）同比", g, "pct", b["contract_liab_yoy"], _pct(b["contract_liab_yoy"]))
    put("ff.balance.debt", "有息负债", g, "yi", b["debt"], _yi(b["debt"]))
    put("ff.balance.cash", "货币资金", g, "yi", b["cash"], _yi(b["cash"]))
    if b["goodwill_to_equity"] is not None:
        put("ff.balance.goodwill", "商誉占归母净资产的比例", g, "share", b["goodwill_to_equity"], _share(b["goodwill_to_equity"], 1))


_POSITION = {"above": "高于预告上限", "within": "落在预告区间内", "below": "低于预告下限"}
KIND_NAME = {"0331": "一季报", "0630": "半年报", "0930": "三季报", "1231": "年报"}


def period_name(end: str) -> str:
    return f"{end[:4]} {KIND_NAME.get(end[4:], '')}"


def _valuation(m, put) -> None:
    v, g = m.get("valuation"), "估值影响"
    if not v:
        return
    if not v["applicable"]:
        put("ff.valuation.note", "本期财报对估值的影响", g, "text", None, "扣非净利润为负，市盈率不可比，不给估值影响")
        return
    put("ff.valuation.ttm", "滚动 12 个月扣非净利润（上一季 → 本季）", g, "yi", v["ttm_now"], f"{_yi(v['ttm_before'])} → {_yi(v['ttm_now'])}")
    put("ff.valuation.change", "本期财报使滚动扣非净利润（也就是扣非每股收益，股本不变时）的变化", g, "pct", v["change"], _pct(v["change"]))
    put("ff.valuation.note", "本期财报对估值的影响", g, "text", None,
        f"股价不变时，市盈率相应{'下降' if v['change'] > 0 else '上升'}约 {abs(v['change']) * 100 / (1 + v['change']) :.1f}%；用自身历史倍数推出的估值锚点也相应变化 {_pct(v['change'])}（只报变化的比例，不出价格）")


def _expect(m, put) -> None:
    e, g = m.get("expect") or {}, "预期"
    p = e.get("preview")
    if p:
        put("ff.expect.preview", "公司对本报告期发过的业绩预告", g, "text", None, f"{p['type'] or '业绩预告'}，归母净利润区间 {_yi(p['low'])} ～ {_yi(p['high'])}（{p['ann_date']} 发布）")
        dev = f"{_pct(p['dev'])}（实际 {_yi(p['actual'])}，预告中值 {_yi(p['mid'])}）" if p["dev"] is not None else f"{_yi(p['dev_amount'])}（实际 {_yi(p['actual'])}，预告中值 {_yi(p['mid'])}）"
        put("ff.expect.preview_dev", "实际归母净利润相对预告中值", g, "pct" if p["dev"] is not None else "yi", p["dev"] if p["dev"] is not None else p["dev_amount"], dev)
        put("ff.expect.preview_position", "实际落在预告区间的哪一段", g, "text", None, _POSITION[p["position"]])
        if p["reason"]:
            put("ff.expect.reason", "预告里写的变动原因（公司原文）", g, "text", None, p["reason"][:90])
    else:
        put("ff.expect.preview", "公司对本报告期发过的业绩预告", g, "text", None, "没有发过业绩预告")
    n = e.get("naive")
    if n:
        put("ff.expect.naive_rev", "朴素预期的单季营收（去年同季 ×（1 + 之前四季平均同比））", g, "yi", n["rev"], f"{_yi(n['rev'])}；实际相对它 {_pct(n['rev_dev'])}")
        if n["np"] is not None:
            put("ff.expect.naive_np", "朴素预期的单季归母净利润", g, "yi", n["np"], f"{_yi(n['np'])}；实际相对它 {_pct(n['np_dev'])}")
        put("ff.expect.naive_note", "朴素预期的口径", g, "text", None, "基于公司自身趋势外推，不是市场预期")
    put("ff.expect.consensus", "券商一致预期", g, "text", None, "没有接入（数据接口每天只能调用 10 次，覆盖不了）")


def _track(m, prior, put, d) -> None:
    rows = tracking.compare(prior, m)
    d["track"] = rows
    d["prior_label"] = period_name(prior["report_end"]) if prior else ""
    if not rows:
        return
    g = "上期预警跟踪"
    for row in rows:
        put(f"ff.track.{row['key']}", f"上期需警惕的「{TOPIC[row['key']]}」跟踪", g, "text", row["after"],
            f"上期（{d['prior_label']}）{row['label']} {tracking.fmt(row['before'], row['kind'])} → 本期 {tracking.fmt(row['after'], row['kind'])}，{tracking.STATE_TEXT[row['state']]}")
    counts = {state: sum(1 for r in rows if r["state"] == state) for state in ("improved", "same", "worse")}
    put("ff.track.summary", "上期需警惕的项本期的变化", g, "text", None, f"上期需警惕 {len(rows)} 项：缓解 {counts['improved']} 项、持续 {counts['same']} 项、恶化 {counts['worse']} 项")


def _reaction(m, put, d) -> None:
    g = "市场反应与同行"
    th = thresholds(m["industry_rule"])
    tone = _tone(m, th)
    d["tone"], d["th"] = tone, th
    r = m.get("reaction")
    if r:
        put("ff.react.base", "反应的起算点（披露日前一个交易日的收盘；公告只给日期不给时刻，盘前盘后都覆盖）", g, "text", None, f"{r['base_date']} 收盘")
        put("ff.react.days_after", "已观察到的反应交易日数（披露日当天为第 1 日，最多算 5 个）", g, "days", min(r["days_after"], 5), f"{min(r['days_after'], 5)} 个交易日")
        put("ff.react.tone", "财报本身的方向", g, "text", None, tone["text"])
        name = r["sector_name"] or "所属行业"
        if r["pre5"]:
            put("ff.react.pre5", "披露前 5 个交易日的涨跌", g, "text", r["pre5"]["excess"], _move(r["pre5"], name))
        for k, row in r["after"].items():
            if row:
                put(f"ff.react.after{k}", f"披露日起第 {k} 个交易日，相对{name}指数的表现", g, "text", row["excess"] if row["excess"] is not None else row["stock"], _move(row, name))
        if r["volume_ratio"] is not None:
            put("ff.react.volume", "披露日成交额相对此前 20 日均值", g, "count", r["volume_ratio"], f"{r['volume_ratio']:.2f} 倍")
    peers = m.get("peers")
    if peers:
        labels = {"rev": "单季营收同比", "np": "单季归母净利润同比", "ded": "累计扣非净利润同比"}
        for name, row in peers["metrics"].items():
            put(f"ff.peer.{name}_pctile", f"本期{labels[name]}在同行里的分位（越高越好）", g, "share", row["pctile"],
                f"{round(row['pctile'] * 100)}% 分位（{row['count']} 家同行有数据；本公司 {_pct(row['own'])}，同行中位数 {_pct(row['median'])}）")


def _move(row: dict, sector_name: str) -> str:
    text = f"个股 {_pct(row['stock'])}"
    if row["sector"] is not None:
        text += f"，{sector_name}指数 {_pct(row['sector'])}，" + (f"跑赢行业 {_pp(row['excess'])[1:]}" if row["excess"] >= 0 else f"跑输行业 {_pp(-row['excess'])[1:]}")
    return text


def _tone(m: dict, th: dict) -> dict:
    """财报本身的方向：增长向上 +1、向下 -1，含金量好 +1、差 -1，合起来看正负。"""
    g_level, q_level = rules.growth_level(m), rules.quality_level(m, th)
    score = {"up": 1, "down": -1}.get(g_level, 0) + {"good": 1, "poor": -1}.get(q_level, 0)
    return {"score": score, "text": f"{rules.GROWTH_TEXT[g_level]}，{rules.QUALITY_TEXT[q_level]}"}


def _data(m, put) -> None:
    d, g = m["data"], "数据质量"
    put("ff.data.periods_behind", "财务数据落后的报告期数", g, "count", d["periods_behind"], f"{d['periods_behind']} 期")
    put("ff.data.single_source", "单季数的来源", g, "text", None, _SINGLE.get(d["single_source"], d["single_source"]))
    put("ff.data.history", "可用的单季历史", g, "count", d["history_quarters"], f"{d['history_quarters']} 个季度")
    if d["listed_years"] is not None:
        put("ff.data.listed_years", "上市年限", g, "count", d["listed_years"], f"{d['listed_years']} 年")
    put("ff.data.adj_ok", "季节性校正是否可用", g, "text", None, "可用" if d["adj_ok"] else "不可用（历史或上市年限不足 3 年）")
    put("ff.data.chronic_loss", "是否连续 4 个季度扣非为负", g, "text", None, "是（持续亏损，第 4 题不适用）" if d["chronic_loss"] else "否")
