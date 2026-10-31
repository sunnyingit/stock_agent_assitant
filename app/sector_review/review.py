"""板块解读的编排：取详情 → 事实表 → 模板答案 → 模型逐题解读（每题只给本题的事实子集）→ 逐项核对 → 合并输出。

失败不会更差：没有模型、调用失败、某一项没通过核对，都用模板答案顶上；归属由程序判定，模型无权更改。
"""

from __future__ import annotations

from datetime import datetime

from app.reviews import core
from app.reviews import validate as item_validate
from app.reviews.core import SKIPPED, Unavailable  # 接口层和后台生成按 review.Unavailable 捕获
from app.sector_review import checks, outcomes, prompt, template
from app.sector_review import facts as fact_sheet
from app.synthesis.calendar import now_cn
from app.synthesis.gateway import Gateway
from app.synthesis.types import FactTable, digest

DISCLAIMER = "以上为基于公开行情和主营构成的整理和解读，只描述板块归属和状态，不推荐个股，不构成投资建议"
KIND = "sector"


def load_payload(stock) -> dict:
    """板块详情（完整结果）。港股、上市不足 20 个交易日、没有可比板块是定论；还在整理或取数失败是暂时的。"""
    from app.services.sector_detail import build_detail

    payload = build_detail(stock)
    state = (payload.get("meta") or {}).get("state")
    if state == "insufficient":
        raise Unavailable((payload.get("meta") or {}).get("status_text") or "数据不足，暂不做板块解读")
    if state not in ("ok", "intraday", "stale"):
        raise RuntimeError((payload.get("meta") or {}).get("status_text") or "板块详情暂时取不到")
    return payload


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
    outcomes.record(store, stock.ts_code, payload)   # 回测：记下这一天的归属判断（缓存命中时也要记）
    facts, d = fact_sheet.build(payload)
    text = {fact.id: fact.text for fact in facts}
    base_items = template.answers(d, text)
    skip = [row["key"] for row in base_items if row["status"] in SKIPPED]
    subsets = template.relevant(set(text))
    table = FactTable(facts=facts)

    use_model = gateway is not None and gateway.available
    key = f"sector_review:v1:{stock.ts_code}:{digest(text)}:{prompt.PROMPT_VERSION}:{model_tag if use_model else 'template'}"
    as_of = (payload.get("meta") or {}).get("as_of") or ""
    hit = core.cached(store, key, stock.ts_code, KIND, refresh, as_of=as_of)
    if hit:
        return hit

    items, errors, call_error, overridden = base_items, [], "", []
    if use_model:
        ask = [row for row in base_items if row["status"] not in SKIPPED]
        reference = {row["key"]: row["status"] for row in ask}
        user = prompt.user(stock, subsets, facts.labels, text, reference, skip)
        result = gateway.run([("sector_review", prompt.SYSTEM, user, 0.2)])[0]
        if result.ok:
            allowed = {k: set(v) for k, v in subsets.items()}
            hanging = [row["name"] for row in d.get("hang") or []]
            items, errors = item_validate.merge_items(
                result.data, table, base_items, banned=prompt.BANNED, guarded=template.GUARDED,
                overridden=overridden, allowed=allowed, extra=checks.make_extra(set(text), hanging),
            )
        else:
            call_error = result.error

    result = {
        "ts_code": stock.ts_code,
        "stock_name": stock.name,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "as_of": as_of,
        "source": core.source_of(items),
        "items": core.render_items(items, template.QUESTIONS),
        "facts": fact_sheet.public(facts),
        "missing": [],
        "disclaimer": DISCLAIMER,
        "cached": False,
        "debug": {"prompt_version": prompt.PROMPT_VERSION, "errors": errors, "call_error": call_error, "status_overridden": overridden},
    }
    core.finish(store, key, result, stock.ts_code, KIND, use_model, call_error)
    return result
