"""基本面体检：取详情 → 行业环境 → 事实表 → 模板逐项解读。

逐项解读固定用代码规则（template），不调用大模型。
方向/一句话结论由基本面分析师（stream）完成后写入，再同步到详情页。
"""

from __future__ import annotations

from datetime import datetime

from app.fundamental_review import data as context_data
from app.fundamental_review import facts as fact_sheet
from app.fundamental_review import prompt, template
from app.reviews import core
from app.reviews.core import Unavailable  # 接口层和后台生成按 review.Unavailable 捕获
from app.services.verdict_view import summary_headline
from app.synthesis.calendar import now_cn
from app.synthesis.gateway import Gateway
from app.synthesis.types import digest

DISCLAIMER = "以上为基于公开数据的整理和解读，只描述在历史和同行中的位置，不构成投资建议"
KIND = "fund"
SUMMARY_KEY = "q11"


def headline_from_review(review: dict | None, *, allow_template: bool = False) -> dict | None:
    """第 11 题一句话。默认只要模型；allow_template 时规则模板也可作分析师结论。"""
    shown = summary_headline(review, SUMMARY_KEY, allow_template=allow_template)
    if shown is None:
        return None
    catalog = {row["id"]: row for row in (review or {}).get("facts") or [] if isinstance(row, dict) and row.get("id")}
    facts = []
    for fact_id in (review or {}).get("cited_fact_ids") or []:
        row = catalog.get(str(fact_id))
        if row is None:
            continue
        facts.append({"id": row["id"], "label": row.get("label") or row["id"], "text": row.get("text") or "", "value": row.get("value")})
    # 模板 q11 没有单独 cited_fact_ids 字段时，用题目 cites
    if not facts:
        row = next((item for item in (review or {}).get("items") or [] if item.get("key") == SUMMARY_KEY), None)
        for fact_id in (row or {}).get("cites") or []:
            item = catalog.get(str(fact_id))
            if item is None:
                continue
            facts.append({"id": item["id"], "label": item.get("label") or item["id"], "text": item.get("text") or "", "value": item.get("value")})
            if len(facts) >= 5:
                break
    shown["facts"] = facts
    return shown


def load_payload(stock) -> dict:
    """基本面详情（完整结果，带 inputs）。上市不足一年、没有可比同行、港股这类是定论；还在整理或取数失败是暂时的。"""
    from app.services.fundamental_detail import build_detail

    payload = build_detail(stock)
    state = (payload.get("meta") or {}).get("state")
    if state == "insufficient":
        raise Unavailable((payload.get("meta") or {}).get("status_text") or "数据不足，暂不做基本面体检")
    if state != "ok":
        raise RuntimeError((payload.get("meta") or {}).get("status_text") or "基本面详情暂时取不到")
    return payload


def build(
    stock,
    store=None,
    gateway: Gateway | None = None,
    now: datetime | None = None,
    payload: dict | None = None,
    context: dict | None = None,
    refresh: bool = False,
    model_tag: str = "",
) -> dict:
    """gateway / model_tag 保留签名兼容旧调用，但不再用于逐项解读。"""
    del gateway, model_tag
    now = now or now_cn()
    payload = payload or load_payload(stock)
    context = context if context is not None else context_data.load_context(stock)
    facts, d = fact_sheet.build(payload, context)
    text = {fact.id: fact.text for fact in facts}
    items = template.answers(d, text)
    public_facts = fact_sheet.public(facts)

    key = f"fund_review:v2:{stock.ts_code}:{digest([text, context['missing']])}:{prompt.PROMPT_VERSION}:template"
    hit = core.cached(store, key, stock.ts_code, KIND, refresh)
    if hit:
        return hit

    q11 = next((row for row in items if row.get("key") == SUMMARY_KEY and row.get("status") != "missing"), None)
    cited = list((q11 or {}).get("cites") or [])[:5]
    result = {
        "ts_code": stock.ts_code,
        "stock_name": stock.name,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "as_of": (payload.get("meta") or {}).get("as_of") or "",
        "source": "template",
        "headline": None,
        "items": core.render_items(items, template.QUESTIONS),
        "cited_fact_ids": cited,
        "profile": None,
        "facts": public_facts,
        "missing": context["missing"],
        "partial": bool(context.get("pending")),
        "disclaimer": DISCLAIMER,
        "cached": False,
        "debug": {"prompt_version": prompt.PROMPT_VERSION, "errors": [], "call_error": ""},
    }
    core.finish(store, key, result, stock.ts_code, KIND, use_model=False, call_error="")
    return result
