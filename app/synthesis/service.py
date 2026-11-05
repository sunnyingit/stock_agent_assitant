"""综合判断的编排：取数 → 体检 → 规则版 → 事实表 → 价位 → Agent → 校验与回退 → 合并输出 → 存档。

失败不会更差：Agent 超时、出错、不通过校验时，用规则版结论。
SYNTHESIS_MODE：rule 只用规则版；shadow 后台跑 Agent 并存档，页面仍用规则版；agent 页面用 Agent（失败时回退）。
"""

from __future__ import annotations

import os
import time
from datetime import datetime

from app import config
from app.synthesis import decider, display, policy, rules, trend
from app.synthesis.calendar import last_close_date, now_cn, session_of
from app.synthesis.decider import stabilize
from app.synthesis.gateway import Gateway
from app.synthesis.levels import Evaluator, build_candidates, resolve_levels
from app.synthesis.producer import adapters, health
from app.synthesis.producer.collect import collect
from app.synthesis.producer.facts import build_fact_table
from app.synthesis.producer.market import market_facts
from app.synthesis.store import Store
from app.synthesis.types import STATUS_PENDING

MODES = ("rule", "shadow", "agent")
_TRANSIENT = {"no_model", "model_error"}   # 这类失败下次应当重试，而不是复用


def current_mode() -> str:
    mode = os.environ.get("SYNTHESIS_MODE", "rule").strip().lower()
    return mode if mode in MODES else "rule"


def _state(final: dict) -> dict:
    return {
        "direction": final["direction"],
        "level": final["level"],
        "raw_direction": final.get("raw_direction", final["direction"]),
        "adopted": [{"claim": item["claim"], "weight": item["weight"]} for item in final.get("adopted") or []],
    }


def _previous(store: Store, ts_code: str, key: str) -> dict | None:
    for run in store.recent(ts_code, 5):
        state = run["data"].get(key)
        if state:
            return state
    return None


def _reusable(record: dict | None, mode: str, model_tag: str) -> bool:
    if not record or record["policy_version"] != policy.POLICY_VERSION:
        return False
    if mode == "rule":
        authored = ((record.get("data") or {}).get("brief") or {}).get("authored")
        if authored != "agent" and config.LLM_API_KEY:
            return False
        return True
    if record["prompt_version"] != decider.PROMPT_VERSION or record["model"] != model_tag:
        return False
    return record["fallback_reason"] not in _TRANSIENT


def _current_price(aligned: dict | None):
    bars = (aligned or {}).get("bars") or []
    return bars[-1].get("close") if bars else None


def load_table(
    stock,
    store: Store,
    now: datetime | None = None,
    loaders=None,
    aligned: dict | None = None,
    build_volume_flow=None,
    timeout: float | None = None,
):
    """取五维详情并装成事实表。看多、看空、主分析师共用这一份。"""
    now = now or now_cn()
    bundle = collect(stock, now, loaders, aligned, timeout)
    snaps = [adapters.adapt(key, bundle.payloads.get(key), bundle.errors.get(key, "")) for key in policy.DIMENSIONS]
    for snap in snaps:
        if snap.key in bundle.timed_out:
            snap.status, snap.reason = STATUS_PENDING, "还在整理"
    market = market_facts(bundle.aligned)
    hygiene = health.inspect(snaps, market, now)
    fresh = health.fresh(snaps, now)
    rule = rules.synthesize(snaps, market, fresh)
    price = _current_price(bundle.aligned)
    evaluator = Evaluator(stock, bundle.aligned, snaps, now, fresh, build_volume_flow)
    candidates = build_candidates(bundle.aligned, snaps, evaluator) if price else []
    context = {
        "done": rule["done"],
        "total": rule["total"],
        "fresh": rule["fresh"],
        "majority": rule["majority"],
        "dim_lean": {snap.key: snap.lean for snap in snaps if snap.done},
        # 给主分析师看：每个已完成的维度的方向、强度、把握度（顺序和 headlines 一致）
        "dim_view": [
            {"dimension": snap.key, "label": policy.DIMENSION_LABEL.get(snap.key, snap.key), "direction": policy.DIRECTION_TEXT.get(snap.lean, "不明"),
             "strength": policy.LEVEL_TEXT.get(snap.level or 0, ""), "certainty": snap.certainty or ""}
            for key in ("sector", "volume_flow", "news", "finance", "fundamental") for snap in snaps if snap.key == key and snap.done
        ],
    }
    table = build_fact_table(snaps, market, rule, candidates, context)
    _attach_review_facts(table, bundle.payloads, bundle.aligned, stock, now)
    from app.synthesis.headlines import dimension_headlines

    table.headlines = dimension_headlines(bundle.payloads, stock.ts_code, store)
    return bundle, snaps, market, hygiene, fresh, rule, price, evaluator, candidates, table, now


def compute(
    stock,
    store: Store,
    gateway: Gateway | None = None,
    mode: str | None = None,
    now: datetime | None = None,
    loaders=None,
    aligned: dict | None = None,
    force: bool = False,
    build_volume_flow=None,
    timeout: float | None = None,
) -> dict:
    mode = mode or current_mode()
    bundle, snaps, market, hygiene, fresh, rule, price, evaluator, candidates, table, now = load_table(
        stock, store, now, loaders, aligned, build_volume_flow, timeout,
    )
    session = session_of(now)
    trade_date = (now.date() if session == "intraday" else last_close_date(now)).isoformat()
    digest = table.digest()
    pending = [snap.key for snap in snaps if snap.status == STATUS_PENDING]

    use_agent = mode != "rule"
    if gateway is None:
        gateway = Gateway.from_config()
    model_tag = config.LLM_MODEL if gateway.available else ""

    if not force and not pending:
        hit = store.by_hash(stock.ts_code, digest, session, trade_date)
        if _reusable(hit, mode, model_tag):
            return _reply(hit["data"], mode, hit["id"], reused=True)

    decision = None
    sides = None
    if use_agent:
        previous_agent = _previous(store, stock.ts_code, "agent_state")
        decision = decider.decide(table, gateway, session, previous_agent, store, model_tag)
        sides = (decision or {}).get("sides")
    elif gateway.available:
        from app.synthesis.decider import debate

        sides = debate.run_sides(gateway, table, store, model_tag)
    agent_ok = decision is not None and decision["source"] == "agent"
    fallback_reason = "" if decision is None or agent_ok else decision["reason"]

    credibility = rules.credibility(rule, inconsistent=fallback_reason == "inconsistent")
    rule_state = display.rule_final(rule, snaps, market)
    previous_shown = _previous(store, stock.ts_code, "final_state")
    rule_state["change_reason"] = stabilize.change_reason(previous_shown, rule_state["direction"], rule_state["level"], [])

    trend_input = {
        "bars": trend.bars_of(bundle.aligned),
        "series": trend.series_of(bundle.aligned),
        "history": store.judgments(stock.ts_code),
        "date": trade_date,
    }
    levels_rule = resolve_levels(None, candidates, price, evaluator, snaps)
    page_rule = display.assemble(
        stock, rule_state, levels_rule, rule, snaps, credibility, table.events, session, now,
        table=table, fallback_reason=fallback_reason, trend_input=trend_input,
    )
    page_agent = None
    agent = None
    agent_state = None
    levels_agent = None
    if agent_ok:
        agent = display.agent_final(decision["final"])
        levels_agent = resolve_levels(decision["final"]["levels"], candidates, price, evaluator, snaps)
        page_agent = display.assemble(
            stock, agent, levels_agent, rule, snaps, credibility, table.events, session, now,
            sides=decision["sides"], table=table, trend_input=trend_input,
        )
        agent_state = _state(decision["final"])

    shown_page = page_agent if (mode == "agent" and agent_ok) else page_rule
    shown_final = agent if (mode == "agent" and agent_ok) else rule_state
    decided = decision["final"] if agent_ok else rule_state
    diverged = agent_ok and (decided["direction"], decided["position"]) != (rule["direction"], rule["position"])
    data = {
        "display_rule": page_rule,
        "display_agent": page_agent,
        "final_state": _state(shown_final),
        "shown": {"change_text": shown_final.get("change_reason") or ""},
        "agent_state": agent_state,
        "table": table.as_dict(),
        "table_hash": digest,
        "rule": rule,
        "hygiene": hygiene,
        "pending": pending,
        "levels_rule": levels_rule,
        "levels_agent": levels_agent,
        "decision": _archive_decision(decision),
        "calls": gateway.calls if gateway is not None else [],
        "diverged": diverged,
        "fallback_reason": fallback_reason,
        "brief": _brief(
            bundle.payloads, bundle.aligned, stock, now, shown_page,
            "model" if shown_page is page_agent else "rule", sides, table,
        ),
    }
    run = {
        "ts_code": stock.ts_code, "session": session, "trade_date": trade_date, "facts_hash": digest,
        "mode": mode, "source": "agent" if agent_ok else "rule", "fallback_reason": fallback_reason,
        "final": {"direction": decided["direction"], "level": decided["level"], "position": decided["position"]},
        "rule": rule, "diverged": diverged, "model": model_tag,
        "prompt_version": decider.PROMPT_VERSION if use_agent else "", "policy_version": policy.POLICY_VERSION,
        "created_at": time.time(), "data": data,
    }
    run_id = store.save_run(run)
    reply = _reply(data, mode, run_id, reused=False)
    reply["pending"] = pending
    return reply


def _attach_review_facts(table, payloads, aligned, stock, now) -> None:
    """体检事实表并进给 Agent 看的那张表，这样看多、看空引用的是 fr.* / ff.* / fd.* / sp.*。"""
    from app.synthesis.brief import review_rows
    from app.synthesis.types import Fact

    have = {item.id for item in table.facts}
    for row in review_rows(payloads, aligned, stock, now):
        if row["id"] in have:
            continue
        table.facts.append(Fact(row["id"], row.get("value"), row.get("text") or ""))
        have.add(row["id"])


def _brief(payloads, aligned, stock, now, page: dict, source: str, sides, table) -> dict | None:
    try:
        from app.synthesis.brief import compose

        extra = [{"id": item.id, "label": item.id, "text": item.text, "value": item.value} for item in table.facts]
        return compose(payloads, aligned, stock, now, page, source, sides, extra)
    except Exception:
        return None


def _archive_decision(decision: dict | None) -> dict | None:
    if decision is None:
        return None
    return {
        "source": decision["source"], "reason": decision["reason"], "digest": decision["digest"],
        "sides": decision["sides"], "runs": decision["runs"], "final": decision["final"],
    }


def _reply(data: dict, mode: str, run_id: int, reused: bool) -> dict:
    page = data["display_agent"] if (mode == "agent" and data.get("display_agent")) else data["display_rule"]
    return {
        "display": page,
        "internal": {
            "run_id": run_id,
            "reused": reused,
            "mode": mode,
            "source": "agent" if (mode == "agent" and data.get("display_agent")) else "rule",
            "fallback_reason": data.get("fallback_reason") or "",
            "facts_hash": data.get("table_hash"),
            "diverged": data.get("diverged"),
            "rule": data["rule"],
            "hygiene": data["hygiene"],
        },
    }
