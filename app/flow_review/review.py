"""资金面体检的编排：取数 → 事实表 → 异常检测（内部：喂给模型和模板，不对外返回）→ 模板答案 → 模型逐项解读 → 逐项校验 → 合并输出。

失败不会更差：没有模型、调用失败、某一项没通过校验，都用模板答案顶上；异常检测不依赖模型。
"""

from __future__ import annotations

from datetime import datetime

from app.flow_review import anomalies as anomaly_rules
from app.flow_review import data as extra_data
from app.flow_review import facts as fact_sheet
from app.flow_review import prompt, template, validate
from app.reviews import core
from app.reviews.core import Unavailable  # 接口层和后台生成按 review.Unavailable 捕获
from app.synthesis.calendar import now_cn
from app.synthesis.gateway import Gateway
from app.synthesis.types import FactTable, digest

DISCLAIMER = "以上为基于公开资金流数据的整理和解读，不构成投资建议"
KIND = "flow"

def _repair(gateway: Gateway, table: FactTable, facts, items: list[dict], failed: dict) -> tuple[list[dict], list[str], list[str]]:
    """没通过校验的几项，把具体错误告诉模型，让它只重写这几项；再校验一次，仍不通过的才用模板。"""
    fixes = [{"key": key, "status": info["status"], "previous_answer": info["answer"], "errors": info["errors"]} for key, info in failed.items()]
    result = gateway.run([("flow_review_fix", prompt.FIX_SYSTEM, prompt.fix_user(facts, facts.labels, fixes), 0.0)])[0]
    if not result.ok:
        return items, [], [f"修正调用失败:{result.error}"]
    only = {"items": []}
    for row in result.data.get("items") or []:
        if isinstance(row, dict) and row.get("key") in failed:
            only["items"].append({**row, "status": failed[row["key"]]["status"]})   # 只改文字，状态保留重写前的
    fixed, errors = validate.merge_items(only, table, items)
    repaired = [row["key"] for row, old in zip(fixed, items) if row["source"] == "model" and old["source"] != "model"]
    return fixed, repaired, [f"修正后仍未通过:{item}" for item in errors]


def load_inputs(stock, now: datetime, aligned: dict | None = None) -> tuple[dict, dict]:
    """量价详情和对齐数据。港股或没有日线时直接说明不可用。"""
    from app.services.volume_flow import build_detail
    from app.services.volume_flow.align import load_aligned

    try:
        aligned = aligned or load_aligned(stock)
        payload = build_detail(stock, 20, aligned=aligned, today=now.date())
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
    now = now or now_cn()
    if payload is None or aligned is None:
        payload, aligned = load_inputs(stock, now, aligned)
    extra = extra if extra is not None else extra_data.load_extra(stock)
    facts, d = fact_sheet.build(payload, aligned, extra, stock, now)
    if not facts:
        raise Unavailable("没有可用的日线数据")
    by_id = {fact.id: fact for fact in facts}
    text = {fact.id: fact.text for fact in facts}
    rule_anomalies = anomaly_rules.detect(d, facts)
    base_items = template.answers(d, text, rule_anomalies)
    must_missing = [row["key"] for row in base_items if row["status"] == "missing"]
    table = FactTable(facts=facts)

    use_model = gateway is not None and gateway.available
    key = f"flow_review:v3:{stock.ts_code}:{digest([text, extra['missing'], rule_anomalies])}:{prompt.PROMPT_VERSION}:{model_tag if use_model else 'template'}"
    hit = core.cached(store, key, stock.ts_code, KIND, refresh)
    if hit:
        return hit

    items, errors, call_error, repaired, overridden = base_items, [], "", [], []
    if use_model:
        reference = {row["key"]: row["status"] for row in base_items if row["status"] != "missing"}
        user = prompt.user(
            facts, facts.labels, must_missing, extra["missing"], rule_anomalies,
            by_question=template.relevant(set(by_id)), reference=reference,
        )
        result = gateway.run([("flow_review", prompt.SYSTEM, user, 0.2)])[0]
        if result.ok:
            failed: dict = {}
            items, errors = validate.merge_items(result.data, table, base_items, failed, overridden)
            if failed:
                items, repaired, still = _repair(gateway, table, facts, items, failed)
                errors += still
        else:
            call_error = result.error

    source = core.source_of(items)
    rendered = core.render_items(items, template.QUESTIONS)
    fact_rows = fact_sheet.public(facts)
    result = {
        "ts_code": stock.ts_code,
        "stock_name": stock.name,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "as_of": aligned.get("as_of") or "",
        "source": source,
        "items": rendered,
        "facts": fact_rows,
        "missing": extra["missing"],
        "disclaimer": DISCLAIMER,
        "cached": False,
        "debug": {"prompt_version": prompt.PROMPT_VERSION, "errors": errors, "call_error": call_error, "repaired": repaired, "status_overridden": overridden},
    }
    core.finish(store, key, result, stock.ts_code, KIND, use_model, call_error)
    return result
