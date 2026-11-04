"""财报解读：取详情 → 事实表 → 模板逐项解读。

逐项解读固定用代码规则（template），不调用大模型。
方向/一句话结论由财报分析师（stream）完成后写入，再同步到详情页。
"""

from __future__ import annotations

from datetime import datetime

from app.finance_review import facts as fact_sheet
from app.finance_review import outcomes, prompt, template
from app.reviews import core
from app.reviews.core import Unavailable  # 接口层和后台生成按 review.Unavailable 捕获
from app.services.finance_detail import rules
from app.services.verdict_view import summary_headline
from app.synthesis.calendar import now_cn
from app.synthesis.gateway import Gateway
from app.synthesis.types import digest

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


def headline(review: dict | None, days_since, *, allow_template: bool = False) -> dict | None:
    """第 12 题一句话。默认只要模型；allow_template 时规则模板也可作分析师结论。句首加上距披露天数。"""
    shown = summary_headline(review, template.SUMMARY_KEY, allow_template=allow_template)
    if shown is None:
        return None
    if days_since is not None:
        shown["text"] = f"{DAYS_PREFIX} {days_since} 天：{shown['text']}"
    catalog = {row["id"]: row for row in (review or {}).get("facts") or [] if isinstance(row, dict) and row.get("id")}
    facts = []
    for fact_id in (review or {}).get("cited_fact_ids") or []:
        row = catalog.get(str(fact_id))
        if row is None:
            continue
        facts.append({"id": row["id"], "label": row.get("label") or row["id"], "text": row.get("text") or "", "value": row.get("value")})
    shown["facts"] = facts
    return shown


def build(
    stock,
    store=None,
    gateway: Gateway | None = None,
    now: datetime | None = None,
    payload: dict | None = None,
    refresh: bool = False,
    model_tag: str = "",
) -> dict:
    """gateway / model_tag 保留签名兼容旧调用，但不再用于逐项解读。"""
    del gateway, model_tag
    now = now or now_cn()
    payload = payload or load_payload(stock)
    m = payload["inputs"]["metrics"]
    prior = store.previous_finance_signal(stock.ts_code, m["report_end"]) if store is not None else None
    facts, d = fact_sheet.build(m, prior)
    text = {fact.id: fact.text for fact in facts}
    items = template.answers(d, text)

    stable = {fact_id: value for fact_id, value in text.items() if fact_id not in fact_sheet.VOLATILE}
    key = f"fin_review:v2:{stock.ts_code}:{m['report_end']}:{digest(stable)}:{prompt.PROMPT_VERSION}:template"
    as_of = (payload.get("meta") or {}).get("as_of") or ""
    hit = core.cached(store, key, stock.ts_code, KIND, refresh, as_of=as_of)
    if hit:
        return hit

    q12 = next((row for row in items if row.get("key") == template.SUMMARY_KEY and row.get("status") != "missing"), None)
    cited = list((q12 or {}).get("cites") or [])[:5]
    result = {
        "ts_code": stock.ts_code,
        "stock_name": stock.name,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "as_of": as_of,
        "source": "template",
        "report": rules.period_label(m),
        "items": core.render_items(items, template.QUESTIONS),
        "signals": [],
        "cited_fact_ids": cited,
        "facts": fact_sheet.public(facts),
        "missing": [],
        "disclaimer": DISCLAIMER,
        "cached": False,
        "debug": {
            "prompt_version": prompt.PROMPT_VERSION,
            "report_end": m["report_end"],
            "errors": [],
            "call_error": "",
            "qdigests": {},
            "carried": [],
        },
    }
    core.finish(store, key, result, stock.ts_code, KIND, use_model=False, call_error="")
    outcomes.record(store, stock.ts_code, payload, items, result["source"], prompt.PROMPT_VERSION)
    return result
