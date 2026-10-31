"""8 个问题的模板答案：模型失败、某一项没通过核对、或没有配置模型时用它。

只用事实表里的数字，每条都带引用。状态：support 偏正面、neutral 中性、warn 需警惕、na 不适用。
归属（q1 到 q3）和驱动（q4）是程序判定的结论，模型只能解释依据，不能改。
"""

from __future__ import annotations

from app.sector_review.facts import BEARISH, BULLISH, STAGE_TEXT

QUESTIONS = [
    ("q1", "主板块是什么", "本股主要属于哪个板块，凭什么算属于"),
    ("q2", "有没有次板块", "第二个板块是什么，为什么算，或为什么没有"),
    ("q3", "哪些只是挂名", "联动很强却不算属于的板块，为什么不算"),
    ("q4", "谁在驱动本股", "两个板块各自解释了本股走势的多少"),
    ("q5", "主板块现在怎么样", "主板块处在什么阶段，本股在里面是什么位置"),
    ("q6", "次板块现在怎么样", "次板块处在什么阶段，本股在里面是什么位置"),
    ("q7", "两个板块一致吗", "两个板块的阶段是同向还是对立"),
    ("q8", "一句话总结", "用白话讲清楚本股的板块归属和现在的板块环境"),
]
SUMMARY_KEY = "q8"
TITLES = {key: title for key, title, _ in QUESTIONS}
GUARDED = ("q4", "q5", "q6", "q7")   # 状态由事实决定：模型不能把偏正面改成需警惕，反过来也不行

RELEVANT = {
    "q1": ["sp.p.name", "sp.p.basis", "sp.p.biz", "sp.p.corr60", "sp.p.rank", "sp.biz.lines"],
    "q2": ["sp.s.name", "sp.s.basis", "sp.s.biz", "sp.s.corr60", "sp.s.rank", "sp.biz.lines", "sp.merged", "sp.p.name"],
    "q3": ["sp.hang", "sp.cand.count", "sp.merged", "sp.p.name", "sp.s.name"],
    "q4": ["sp.driver.type", "sp.driver.r2", "sp.driver.p_share", "sp.driver.s_share", "sp.driver.weights", "sp.p.name", "sp.s.name", "sp.p.beta60", "sp.s.beta60"],
    "q5": ["sp.p.name", "sp.p.stage", "sp.p.stage_days", "sp.p.role", "sp.p.ret_sector", "sp.p.ret_stock", "sp.p.ret_index", "sp.p.vs_index",
           "sp.p.stock_vs_sector", "sp.p.breadth", "sp.p.sw_rank", "sp.p.warn"],
    "q6": ["sp.s.name", "sp.s.stage", "sp.s.stage_days", "sp.s.role", "sp.s.ret_sector", "sp.s.ret_stock", "sp.s.ret_index", "sp.s.vs_index",
           "sp.s.stock_vs_sector", "sp.s.breadth", "sp.s.sw_rank", "sp.s.warn"],
    "q7": ["sp.consistency", "sp.p.name", "sp.p.stage", "sp.s.name", "sp.s.stage", "sp.driver.weights"],
}


def relevant(available: set[str]) -> dict[str, list[str]]:
    subsets = {key: [fact for fact in ids if fact in available] for key, ids in RELEVANT.items()}
    # 总结题可以引用任何一条
    subsets["q8"] = sorted(available)
    return subsets


def _item(key: str, status: str, answer: str, cites: list[str]) -> dict:
    return {"key": key, "status": status, "answer": answer, "cites": cites, "source": "template"}


def _na(key: str, why: str) -> dict:
    return _item(key, "na", f"不适用：{why}", [])


def _stage_status(stage: str | None) -> str:
    return "support" if stage in BULLISH else "warn" if stage in BEARISH else "neutral"


def answers(d: dict, text: dict[str, str]) -> list[dict]:
    has = text.__contains__
    primary, secondary, driver = d.get("primary"), d.get("secondary"), d.get("driver") or {}
    out = []

    # q1
    if primary is None:
        out.append(_item("q1", "warn", "没有找到本股明确属于的板块：候选板块里没有一个同时有业务占比或足够的联动与地位作证据，板块结论参考价值低。", []))
    else:
        cites = [c for c in ("sp.p.name", "sp.p.basis") if has(c)]
        out.append(_item("q1", "support", f"主板块是{text['sp.p.name']}：{text['sp.p.basis']}。", cites))

    # q2
    if secondary is not None:
        cites = [c for c in ("sp.s.name", "sp.s.basis") if has(c)]
        out.append(_item("q2", "support", f"次板块是{text['sp.s.name']}：{text['sp.s.basis']}。", cites))
    elif primary is None:
        out.append(_na("q2", "没有主板块"))
    else:
        merged = f"，{text['sp.merged']}的走势与主板块几乎相同，已并入" if has("sp.merged") else ""
        out.append(_item("q2", "neutral", f"没有发现和主板块不同类、又同样算属于的第二个板块{merged}，本股按单板块看。", [c for c in ("sp.merged", "sp.p.name") if has(c)]))

    # q3
    if has("sp.hang"):
        out.append(_item("q3", "neutral", f"这些板块和本股联动强，但业务占比不够、在板块里也不是核心成员，只算挂名：{text['sp.hang']}。", ["sp.hang"]))
    else:
        out.append(_item("q3", "neutral", "没有发现联动强却不算属于的板块。", [c for c in ("sp.cand.count",) if has(c)]))

    # q4
    kind = driver.get("type")
    if not has("sp.driver.type"):
        out.append(_na("q4", "样本不足，算不出驱动占比"))
    elif kind == "independent":
        cites = [c for c in ("sp.driver.type", "sp.driver.r2") if has(c)]
        out.append(_item("q4", "warn", f"{text['sp.driver.type']}：两个板块合计只解释了本股近 60 日收益的{text.get('sp.driver.r2', '很少一部分')}，板块结论参考价值低。", cites))
    elif kind == "dual":
        cites = [c for c in ("sp.driver.type", "sp.driver.p_share", "sp.driver.s_share") if has(c)]
        out.append(_item("q4", "neutral", f"{text['sp.driver.type']}：主板块解释{text['sp.driver.p_share']}，次板块解释{text['sp.driver.s_share']}。", cites))
    else:
        cites = [c for c in ("sp.driver.type", "sp.driver.p_share") if has(c)]
        out.append(_item("q4", "neutral", f"{text['sp.driver.type']}：主板块解释了本股近 60 日收益的{text.get('sp.driver.p_share', '大部分')}。", cites))

    # q5 / q6
    for key, tag, block, who in (("q5", "p", primary, "主板块"), ("q6", "s", secondary, "次板块")):
        if block is None:
            out.append(_na(key, f"没有{who}"))
            continue
        stage = (block.get("stage") or {}).get("current")
        parts = [f"{text[f'sp.{tag}.name'].split('（')[0]}处在{STAGE_TEXT.get(stage, '没有明确阶段')}阶段"]
        cites = [c for c in (f"sp.{tag}.name", f"sp.{tag}.stage") if has(c)]
        if has(f"sp.{tag}.role"):
            parts.append(f"本股是{text[f'sp.{tag}.role']}")
            cites.append(f"sp.{tag}.role")
        if has(f"sp.{tag}.stock_vs_sector"):
            parts.append(f"近 20 日相对板块 {text[f'sp.{tag}.stock_vs_sector']}")
            cites.append(f"sp.{tag}.stock_vs_sector")
        out.append(_item(key, _stage_status(stage), "，".join(parts) + "。", cites))

    # q7
    if secondary is None:
        out.append(_na("q7", "没有次板块"))
    elif d.get("divergent"):
        out.append(_item("q7", "warn", f"{text['sp.consistency']}：两个板块阶段对立，方向不取平均，请分别看。", ["sp.consistency"]))
    else:   # 同向：都偏多是 support，都偏空是 warn（一致地弱），否则 neutral
        stages = {(d["primary"].get("stage") or {}).get("current"), (d["secondary"].get("stage") or {}).get("current")}
        status = "support" if stages <= set(BULLISH) else "warn" if stages <= set(BEARISH) else "neutral"
        out.append(_item("q7", status, f"{text['sp.consistency']}。", ["sp.consistency"]))

    # q8
    out.append(_summary(d, text, out))
    return out


def _summary(d: dict, text: dict[str, str], items: list[dict]) -> dict:
    if d.get("primary") is None:
        return _item("q8", "neutral", "没有找到本股明确属于的板块，板块环境参考价值低。", [])
    by = {row["key"]: row for row in items}
    status = by["q5"]["status"]
    if d.get("divergent"):
        status = "neutral"
    elif d.get("secondary") is not None and by["q6"]["status"] != status and by["q6"]["status"] in ("support", "warn"):
        status = "neutral"
    name = text["sp.p.name"].split("（")[0]
    head = f"主板块{name}{('，次板块' + text['sp.s.name'].split('（')[0]) if d.get('secondary') else ''}"
    tail = f"，{text['sp.driver.type']}" if "sp.driver.type" in text else ""
    state = f"，主板块{text['sp.p.stage']}" if "sp.p.stage" in text else ""
    return _item("q8", status, f"{head}{tail}{state}。", [c for c in ("sp.p.name", "sp.s.name", "sp.driver.type", "sp.p.stage") if c in text])

