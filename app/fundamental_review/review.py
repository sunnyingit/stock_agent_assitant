"""基本面体检的编排：取详情 → 行业环境 → 事实表 → 模板答案 → 模型逐项解读 + 公司画像 → 逐项校验 → 合并输出。

失败不会更差：没有模型、调用失败、某一项没通过校验，都用模板答案顶上；公司画像只有模型成功且通过格式检查才有。
"""

from __future__ import annotations

from datetime import datetime

from app.fundamental_review import data as context_data
from app.fundamental_review import facts as fact_sheet
from app.fundamental_review import prompt, template, validate
from app.reviews import core
from app.reviews import validate as item_validate
from app.reviews.core import Unavailable  # 接口层和后台生成按 review.Unavailable 捕获
from app.synthesis.calendar import now_cn
from app.synthesis.gateway import Gateway
from app.synthesis.types import FactTable, digest

DISCLAIMER = "以上为基于公开数据的整理和解读，只描述在历史和同行中的位置，不构成投资建议"
KIND = "fund"


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
    now = now or now_cn()
    payload = payload or load_payload(stock)
    context = context if context is not None else context_data.load_context(stock)
    facts, d = fact_sheet.build(payload, context)
    by_id = {fact.id: fact for fact in facts}
    text = {fact.id: fact.text for fact in facts}
    base_items = template.answers(d, text)
    must_missing = [row["key"] for row in base_items if row["status"] == "missing"]
    table = FactTable(facts=facts)

    use_model = gateway is not None and gateway.available
    key = f"fund_review:v1:{stock.ts_code}:{digest([text, context['missing']])}:{prompt.PROMPT_VERSION}:{model_tag if use_model else 'template'}"
    hit = core.cached(store, key, stock.ts_code, KIND, refresh)
    if hit:
        return hit

    items, errors, call_error, overridden, profile = base_items, [], "", [], None
    if use_model:
        reference = {row["key"]: row["status"] for row in base_items if row["status"] != "missing"}
        user = prompt.user(facts, facts.labels, must_missing, context["missing"], template.relevant(set(by_id)), reference, stock)
        result = gateway.run([("fund_review", prompt.SYSTEM, user, 0.2)])[0]
        if result.ok:
            items, errors = item_validate.merge_items(result.data, table, base_items, banned=prompt.BANNED, guarded=template.GUARDED, overridden=overridden)
            profile, profile_errors = validate.profile(result.data, stock.name)
            errors += profile_errors
        else:
            call_error = result.error

    source = core.source_of(items)
    rendered = core.render_items(items, template.QUESTIONS)
    result = {
        "ts_code": stock.ts_code,
        "stock_name": stock.name,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "as_of": (payload.get("meta") or {}).get("as_of") or "",
        "source": source,
        "items": rendered,
        "profile": profile,
        "facts": fact_sheet.public(facts),
        "missing": context["missing"],
        "partial": bool(context.get("pending")),   # 行业环境还没取全：过一会儿会自动再生成一次
        "disclaimer": DISCLAIMER,
        "cached": False,
        "debug": {"prompt_version": prompt.PROMPT_VERSION, "errors": errors, "call_error": call_error, "status_overridden": overridden},
    }
    core.finish(store, key, result, stock.ts_code, KIND, use_model, call_error)
    return result
