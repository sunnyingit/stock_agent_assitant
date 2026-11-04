"""11 个问题的模板答案。方向和顶部那句话由模型写；这些问题始终由规则按事实表回答。

状态：support 偏正面、neutral 中性、warn 需警惕、missing 数据缺失。
"""

from __future__ import annotations

QUESTIONS = [
    ("q1", "这家公司靠什么赚钱", "主营业务是什么，哪条业务线最重要"),
    ("q2", "估值贵不贵", "在自己 5 年历史里处在什么位置"),
    ("q3", "和同行比怎么样", "比同行中位数高还是低"),
    ("q4", "估值指标可信吗", "利润失真、换指标、历史样本够不够"),
    ("q5", "公司底子怎么样", "盈利、成长、负债、现金、分红在同行里的位置"),
    ("q6", "最弱的一项是什么", "有没有明显落后于同行的方面"),
    ("q7", "股东在做什么", "回购、增持、减持、激励的方向和规模"),
    ("q8", "近期有没有压力", "解禁、减持、质押有没有集中到来"),
    ("q9", "行业环境怎么样", "所属板块的阶段，近期有什么行业层面的消息"),
    ("q10", "数据靠谱吗", "财报新不新，同行够不够"),
    ("q11", "一句话总结", "用白话讲清楚现在这家公司的基本面处在什么位置"),
]
TITLES = {key: title for key, title, _ in QUESTIONS}
_PRESSURE = ("解禁", "减持", "质押")


def _missing(key: str, what: str) -> dict:
    return {"key": key, "status": "missing", "answer": f"数据缺失：{what}，这一项无法判断。", "cites": [], "source": "template"}


def _item(key: str, status: str, answer: str, cites: list[str]) -> dict:
    return {"key": key, "status": status, "answer": answer, "cites": cites, "source": "template"}


def answers(d: dict, text: dict[str, str]) -> list[dict]:
    has = text.__contains__
    out = []

    # q1 靠什么赚钱：只描述，不好坏
    if not has("fd.main_business") and not has("fd.biz_lines"):
        out.append(_missing("q1", "没有取到主营业务和业务构成"))
    else:
        parts, cites = [], []
        if has("fd.main_business"):
            parts.append(f"主营：{text['fd.main_business']}")
            cites.append("fd.main_business")
        if has("fd.biz_top"):
            parts.append(f"最大的业务线是{text['fd.biz_top']}")
            cites.append("fd.biz_top")
        if has("fd.concepts"):
            parts.append(f"判定属于的概念：{text['fd.concepts']}")
            cites.append("fd.concepts")
        if has("fd.chain"):
            parts.append(text["fd.chain"])
            cites.append("fd.chain")
        out.append(_item("q1", "neutral", "；".join(parts) + "。", cites))

    # q2 估值
    if d.get("pctile") is None or d.get("value") is None:
        out.append(_missing("q2", "缺少可比的历史估值"))
    else:
        level = d.get("valuation_level") or "fair"
        tail = {"cheap": "处在偏便宜的位置", "expensive": "处在偏贵的位置", "fair": "处在合理区间"}[level]
        out.append(_item(
            "q2", {"cheap": "support", "fair": "neutral", "expensive": "warn"}[level],
            f"{text['fd.metric']}当前 {text['fd.value']}，在自己{text.get('fd.history_span', '历史')}里是 {text['fd.pctile_5y']}，{tail}。",
            ["fd.metric", "fd.value", "fd.pctile_5y"],
        ))

    # q3 同行
    if d.get("vs_peer") is None:
        out.append(_missing("q3", "缺少同行中位数"))
    else:
        gap = d["vs_peer"]
        status = "support" if gap <= 0 else "warn" if gap > 0.2 else "neutral"
        out.append(_item(
            "q3", status,
            f"{text['fd.metric']}当前 {text['fd.value']}，{text['fd.vs_peer']}（{text['fd.peer_median']}，共 {text.get('fd.peer_count', '若干家')}同行）。",
            ["fd.value", "fd.peer_median", "fd.vs_peer"],
        ))

    # q4 指标可信度
    if not has("fd.distorted"):
        out.append(_missing("q4", "缺少估值指标的信息"))
    else:
        switched = has("fd.metric_reason") and text["fd.metric_reason"] != "没有换指标"
        if d.get("distorted") or switched:
            out.append(_item("q4", "warn", f"利润处在低位，市盈率容易失真，已换用{text['fd.metric']}；这个估值只能当参考。", ["fd.metric", "fd.distorted", "fd.metric_reason"]))
        elif d.get("peer_count") is not None and d["peer_count"] < 5:
            out.append(_item("q4", "neutral", f"指标没有失真，但同行只有 {text['fd.peer_count']}，同行比较的参考价值有限。", ["fd.metric", "fd.peer_count"]))
        else:
            out.append(_item("q4", "support", f"用{text['fd.metric']}，利润正常没有失真，历史样本和同行都够用。", ["fd.metric", "fd.distorted"]))

    # q5 底子
    scored = [(k, item) for k, item in (d.get("quality_items") or {}).items() if item.get("peer_pctile") is not None and item.get("counts", True)]
    if not scored or not d.get("quality_level"):
        out.append(_missing("q5", "缺少同行质量对比"))
    else:
        red = (d.get("ocf_negative_years") or 0) >= 3
        status = {"strong": "support", "weak": "warn"}.get(d["quality_level"], "neutral")
        names = {"profitability": "ROE", "growth": "三年成长", "solvency": "负债率", "cash": "现金比", "payout": "分红率"}
        bits = [f"{names[k]} {text.get(f'fd.q_{k}', '')}（同行分位 {text.get(f'fd.qp_{k}', '')}）" for k, _i in scored[:3]]
        lead = "经营现金流已连续多年为负，" if red else ""
        out.append(_item("q5", status, f"{lead}{'；'.join(bits)}，整体{'偏弱' if status == 'warn' else '较好' if status == 'support' else '中等'}。", [f"fd.qp_{k}" for k, _i in scored[:3]] + [f"fd.q_{k}" for k, _i in scored[:3]]))

    # q6 最弱
    if not has("fd.q_weakest"):
        out.append(_missing("q6", "缺少同行质量对比"))
    elif d.get("weakest"):
        out.append(_item("q6", "warn", f"{text['fd.q_weakest']}。", ["fd.q_weakest"]))
    else:
        out.append(_item("q6", "support", "五项指标在同行里都没有落到最后 20%，没有明显弱项。", ["fd.q_weakest"]))

    # q7 股东
    level = d.get("holders_level")
    events = d.get("holder_events") or []
    if level is None:
        out.append(_missing("q7", "缺少股东事件数据"))
    elif not events:
        out.append(_item("q7", "neutral", "近期没有回购、增减持、激励这类股东动作。", ["fd.hold_none"] if has("fd.hold_none") else []))
    else:
        status = {"positive": "support", "negative": "warn"}.get(level, "neutral")
        cites = [f"fd.hold_{i}" for i in range(1, min(len(events), 2) + 1)]
        out.append(_item("q7", status, f"规则版档位是{text.get('fd.holders_level', '')}。" + "；".join(text[c] for c in cites if has(c)) + "。", cites))

    # q8 压力
    pressure = [(i, event) for i, event in enumerate(events[:6], start=1) if event.get("impact") == "negative" and _is_pressure(event)]
    if level is None:
        out.append(_missing("q8", "缺少股东事件数据"))
    elif pressure:
        cites = [f"fd.hold_{i}" for i, _e in pressure[:2]]
        out.append(_item("q8", "warn", "；".join(text[c] for c in cites if has(c)) + "，这几件要留意。", cites))
    else:
        out.append(_item("q8", "support", "近期没有集中到来的解禁、减持或高比例质押。", ["fd.hold_none"] if has("fd.hold_none") else []))

    # q9 行业
    stage = d.get("sector_stage")
    news = d.get("industry_news")
    if stage is None and not news:
        out.append(_missing("q9", "缺少板块阶段和行业新闻"))
    else:
        parts, cites = [], []
        if stage:
            parts.append(f"所属板块目前处在“{text['fd.sector_stage']}”阶段")
            cites.append("fd.sector_stage")
        if news:
            parts.append(f"近期有 {len(news)} 条行业层面的新闻")
            cites.append("fd.industry_news_count")
        status = "support" if stage in ("start", "main") else "warn" if stage in ("climax", "ebb", "weak") else "neutral"
        out.append(_item("q9", status, "，".join(parts) + "。", cites))

    # q10 数据
    behind, count = d.get("periods_behind"), d.get("peer_count")
    if behind is None and count is None:
        out.append(_missing("q10", "缺少财报期和同行信息"))
    else:
        bad = (behind or 0) > 1 or (count is not None and count < 5)
        reasons = []
        if (behind or 0) > 1:
            reasons.append(f"财务数据落后 {behind} 期")
        if count is not None and count < 5:
            reasons.append(f"同行只有 {count} 家")
        cites = [c for c in ("fd.report_period", "fd.periods_behind", "fd.peer_count") if has(c)]
        out.append(_item(
            "q10", "warn" if bad else "support",
            "，".join(reasons) + "，结论只能当参考。" if bad else f"财报是{text.get('fd.report_period', '')}，同行 {text.get('fd.peer_count', '')}，数据可以用。", cites,
        ))

    # q11 总结
    judged = [row for row in out if row["key"] in ("q2", "q3", "q5", "q6", "q7", "q8") and row["status"] in ("support", "warn")]
    good, bad = sum(1 for row in judged if row["status"] == "support"), sum(1 for row in judged if row["status"] == "warn")
    status = "support" if good - bad >= 2 else "warn" if bad - good >= 2 else "neutral"
    lead = [row for row in out if row["key"] == "q2" and row["status"] != "missing"]
    sentence = (lead[0]["answer"].split("，", 1)[0] + "；" if lead else "") + {
        "support": "整体偏正面：估值、底子和股东行为多数是加分项。",
        "warn": "整体需要谨慎：估值、底子或股东行为里有多项是减分项。",
        "neutral": "整体中性：加分项和减分项大致相当，看不出明显的倾向。",
    }[status]
    out.append(_item("q11", status, sentence, lead[0]["cites"] if lead else []))
    return sorted(out, key=lambda row: int(row["key"][1:]))


def _is_pressure(event: dict) -> bool:
    return event.get("type") in ("unlock", "holder_reduce", "pledge")
