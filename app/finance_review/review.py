"""财报解读的编排：取详情 → 指标 → 事实表 → 模板答案 → 模型逐题解读（每题只给本题的事实子集）→ 逐项核对 → 硬阈值 → 合并输出。

失败不会更差：没有模型、调用失败、某一项没通过核对，都用模板答案顶上；硬阈值由代码执行，模型无权越过。
"""

from __future__ import annotations

from datetime import datetime

from app.finance_review import facts as fact_sheet
from app.finance_review import checks, outcomes, prompt, template
from app.reviews import attach, core
from app.reviews import validate as item_validate
from app.reviews.core import SKIPPED, Unavailable  # 接口层和后台生成按 review.Unavailable 捕获
from app.services.finance_detail import rules
from app.services.verdict_view import summary_headline
from app.synthesis.calendar import now_cn
from app.synthesis.gateway import Gateway
from app.synthesis.types import FactTable, digest

DISCLAIMER = "以上为基于公开财报的整理和解读，只描述这份财报带来的变化，不构成投资建议"
KIND = "fin"
DAYS_PREFIX = "距最近财报"


def load_payload(stock) -> dict:
    """财报详情（完整结果，带 inputs）。港股、金融业、没有财报是定论；还在整理或取数失败是暂时的。"""
    from app.services.finance_detail import build_detail

    payload = build_detail(stock)
    state = (payload.get("meta") or {}).get("state")
    if state == "insufficient":
        raise Unavailable((payload.get("meta") or {}).get("status_text") or "数据不足，暂不做财报解读")
    if state != "ok":
        raise RuntimeError((payload.get("meta") or {}).get("status_text") or "财报详情暂时取不到")
    return payload


def headline(review: dict | None, days_since) -> dict | None:
    """第 12 题由模型写成时的 headline（字段同 verdict）；顶部句子必须带"距最近财报 X 天"，由程序加在前面。"""
    shown = summary_headline(review, template.SUMMARY_KEY)
    if shown and days_since is not None:
        shown["text"] = f"{DAYS_PREFIX} {days_since} 天：{shown['text']}"
    return shown


def enforce(items: list[dict], base: list[dict], hard: dict) -> list[str]:
    """硬阈值：不能判偏正面的不能是 support，必须判需警惕的必须是 warn。只改模型回答的项，返回被改动的说明。"""
    template_status = {row["key"]: row["status"] for row in base}
    notes = []
    for row in items:
        rule = hard.get(row["key"])
        if not rule or row["source"] != "model":
            continue
        if rule["force_warn"] and row["status"] != "warn":
            notes.append(f"{row['key']}:{row['status']}→warn（硬阈值）")
            row["status"] = "warn"
        elif rule["forbid_support"] and row["status"] == "support":
            fallback = template_status.get(row["key"])
            row["status"] = fallback if fallback in ("neutral", "warn") else "neutral"
            notes.append(f"{row['key']}:support→{row['status']}（硬阈值）")
    return notes


def _carried(store, ts_code: str, report_end: str, qdigests: dict) -> dict[str, dict]:
    """上一次同一份财报、同一版提示词生成的解读里，事实子集没变的、由模型回答的题：原样沿用，不再问模型。"""
    entry = store.get(attach.latest_key(ts_code, KIND)) if store is not None else None
    prev = (entry or {}).get("result") or {}
    debug = prev.get("debug") or {}
    if not prev or debug.get("report_end") != report_end or debug.get("prompt_version") != prompt.PROMPT_VERSION or debug.get("call_error"):
        return {}
    old = debug.get("qdigests") or {}
    return {
        row["key"]: {name: row[name] for name in ("key", "status", "answer", "cites", "source")}
        for row in prev.get("items") or []
        if row["source"] == "model" and old.get(row["key"]) == qdigests.get(row["key"])
    }


def build(
    stock,
    store=None,
    gateway: Gateway | None = None,
    now: datetime | None = None,
    payload: dict | None = None,
    refresh: bool = False,
    model_tag: str = "",
) -> dict:
    now = now or now_cn()
    payload = payload or load_payload(stock)
    m = payload["inputs"]["metrics"]
    prior = store.previous_finance_signal(stock.ts_code, m["report_end"]) if store is not None else None   # 上期的预警，第 11 题跟踪用
    facts, d = fact_sheet.build(m, prior)
    text = {fact.id: fact.text for fact in facts}
    base_items = template.answers(d, text)
    skip = [row["key"] for row in base_items if row["status"] in SKIPPED]
    subsets = template.relevant(set(text), d["signals"])
    table = FactTable(facts=facts)

    use_model = gateway is not None and gateway.available
    stable = {fact_id: value for fact_id, value in text.items() if fact_id not in fact_sheet.VOLATILE}   # 每天都变的天数不进缓存键
    key = f"fin_review:v1:{stock.ts_code}:{m['report_end']}:{digest(stable)}:{prompt.PROMPT_VERSION}:{model_tag if use_model else 'template'}"
    as_of = (payload.get("meta") or {}).get("as_of") or ""
    hit = core.cached(store, key, stock.ts_code, KIND, refresh, as_of=as_of)
    if hit:
        return hit

    # 同一份财报的解读按题挂在快照上：只有事实子集变了的题才重新问模型（比如披露后第 5 个交易日定格，只重跑第 10、12 题）
    qdigests = {
        row["key"]: digest([text[fact] for fact in subsets.get(row["key"], []) if fact in text and fact not in fact_sheet.VOLATILE] + [row["status"]])
        for row in base_items
    }
    carried = _carried(store, stock.ts_code, m["report_end"], qdigests) if use_model and not refresh else {}
    items, errors, call_error, overridden, bounded = base_items, [], "", [], []
    if use_model:
        ask = [row for row in base_items if row["status"] not in SKIPPED and row["key"] not in carried]
        if ask:
            reference = {row["key"]: row["status"] for row in base_items if row["status"] not in SKIPPED}
            user = prompt.user(stock, rules.period_label(m), subsets, facts.labels, text, reference, skip + list(carried), d["signals"])
            result = gateway.run([("fin_review", prompt.SYSTEM, user, 0.2)])[0]
            if result.ok:
                allowed = {k: set(v) for k, v in subsets.items()}
                merged, errors = item_validate.merge_items(
                    result.data, table, ask, banned=prompt.BANNED, guarded=template.GUARDED,
                    overridden=overridden, allowed=allowed, extra=checks.make_extra(d["signals"], bool((m.get("cycle") or {}).get("high"))),
                )
                bounded = enforce(merged, ask, d["bounds"])
                for index, row in enumerate(merged):   # 天数由程序加在前面，模型自己写了就当没通过
                    if row["key"] == template.SUMMARY_KEY and row["source"] == "model" and DAYS_PREFIX in row["answer"]:
                        errors.append("文案:q12 自己写了距披露的天数")
                        merged[index] = next(b for b in ask if b["key"] == template.SUMMARY_KEY)
                fresh = {row["key"]: row for row in merged}
                items = [carried.get(row["key"]) or fresh.get(row["key"]) or row for row in base_items]
            else:
                call_error = result.error
                items = [carried.get(row["key"]) or row for row in base_items]
        else:
            items = [carried.get(row["key"]) or row for row in base_items]

    result = {
        "ts_code": stock.ts_code,
        "stock_name": stock.name,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "as_of": as_of,
        "source": core.source_of(items),
        "report": rules.period_label(m),
        "items": core.render_items(items, template.QUESTIONS),
        "facts": fact_sheet.public(facts),
        "missing": [],
        "disclaimer": DISCLAIMER,
        "cached": False,
        "debug": {
            "prompt_version": prompt.PROMPT_VERSION, "report_end": m["report_end"], "errors": errors, "call_error": call_error,
            "status_overridden": overridden, "bounded": bounded, "qdigests": qdigests, "carried": sorted(carried),
        },
    }
    core.finish(store, key, result, stock.ts_code, KIND, use_model, call_error)
    outcomes.record(store, stock.ts_code, payload, items, result["source"], prompt.PROMPT_VERSION)   # 回测：记下披露后第一时间的判断
    return result
