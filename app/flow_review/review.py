"""资金面体检：取数 → 事实表 → 异常检测 → 模板逐项解读。

逐项解读固定用代码规则（template），不调用大模型。
方向/一句话结论由资金流分析师（stream）完成后写入，再同步到详情页。
"""

from __future__ import annotations

from datetime import datetime

from app.flow_review import anomalies as anomaly_rules
from app.flow_review import data as extra_data
from app.flow_review import facts as fact_sheet
from app.flow_review import prompt, template
from app.reviews import core
from app.reviews.core import Unavailable  # 接口层和后台生成按 review.Unavailable 捕获
from app.synthesis.calendar import now_cn
from app.synthesis.gateway import Gateway
from app.synthesis.types import digest

DISCLAIMER = "以上为基于公开资金流数据的整理和解读，不构成投资建议"
KIND = "flow"


def load_inputs(stock, now: datetime, aligned: dict | None = None) -> tuple[dict, dict]:
    """量价详情和对齐数据。港股或没有日线时直接说明不可用。"""
    from app.services.volume_flow import build_detail
    from app.services.volume_flow.align import load_aligned
    from app.services.volume_flow.rules import DEFAULT_WINDOW

    try:
        aligned = aligned or load_aligned(stock)
        payload = build_detail(stock, DEFAULT_WINDOW, aligned=aligned, today=now.date())
    except Exception as exc:
        raise Unavailable(str(exc)) from exc
    return payload, aligned


def build(
    stock,
    store=None,
    gateway: Gateway | None = None,
    now: datetime | None = None,
    payload: dict | None = None,
    aligned: dict | None = None,
    extra: dict | None = None,
    refresh: bool = False,
    model_tag: str = "",
) -> dict:
    """gateway / model_tag 保留签名兼容旧调用，但不再用于逐项解读。"""
    del gateway, model_tag  # 逐项只用规则
    now = now or now_cn()
    if payload is None or aligned is None:
        payload, aligned = load_inputs(stock, now, aligned)
    extra = extra if extra is not None else extra_data.load_extra(stock)
    facts, d = fact_sheet.build(payload, aligned, extra, stock, now)
    if not facts:
        raise Unavailable("没有可用的日线数据")
    text = {fact.id: fact.text for fact in facts}
    rule_anomalies = anomaly_rules.detect(d, facts)
    items = template.answers(d, text, rule_anomalies)

    key = f"flow_review:v6:{stock.ts_code}:{digest([text, extra['missing'], rule_anomalies])}:{prompt.PROMPT_VERSION}:template"
    hit = core.cached(store, key, stock.ts_code, KIND, refresh)
    if hit:
        return hit

    rendered = core.render_items(items, template.QUESTIONS)
    fact_rows = fact_sheet.public(facts)
    signals = template.rule_signals(payload, items)
    q10 = next((row for row in items if row.get("key") == "q10" and row.get("status") != "missing"), None)
    cited = list((q10 or {}).get("cites") or [])[:5]
    result = {
        "ts_code": stock.ts_code,
        "stock_name": stock.name,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "as_of": aligned.get("as_of") or "",
        "source": "template",
        "items": rendered,
        "signals": signals,
        "cited_fact_ids": cited,
        "facts": fact_rows,
        "missing": extra["missing"],
        "disclaimer": DISCLAIMER,
        "cached": False,
        "debug": {"prompt_version": prompt.PROMPT_VERSION, "errors": [], "call_error": "", "repaired": [], "status_overridden": []},
    }
    core.finish(store, key, result, stock.ts_code, KIND, use_model=False, call_error="")
    return result
