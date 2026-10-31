"""财报分析。流程、提示词和 JSON 结构在 configs/financial.json。"""

from __future__ import annotations

from typing import Any, Callable

from app.agents.display import debate_result, finish_display
from app.agents.engine import run_agent
from app.agents.financial.data import latest_change, to_rows
from app.tools.stock import Stock

FINE_PRINT = "以上为基于公开财报数据的研究分析，不构成投资建议"


def analyze_financials(stock: Stock, model: Any = None, fetch_financials: Callable[[], dict] | None = None) -> dict:
    return run_agent("financial", stock, model=model, fetch=fetch_financials)


def debate_financials(stock: Stock, model: Any = None, fetch_financials: Callable[[], dict] | None = None) -> dict:
    """给用户看的财报结论，以及后台保留的完整分析。"""
    prepared = None
    if fetch_financials is not None:
        prepared = fetch_financials()
        fetch_financials = lambda: prepared
    change = latest_change(prepared) if prepared else ""
    if prepared is None and model is None:
        change = _live_change(stock)
    result = run_agent(
        "financial",
        stock,
        model=model,
        fetch=fetch_financials,
        extra={"change": change},
    )
    rows = result.get("rows") or []
    facts = result.get("facts") or []
    bull = result.get("bull") or {}
    bear = result.get("bear") or {}
    display = _display(result.get("summary") or {}, rows, facts, bull, bear, change)
    return debate_result(display, facts, bull, bear, {
        "rows": rows,
        "periods_read": display["more"]["periods_read"],
        "periods_used": display["more"]["periods_used"],
    })


def _live_change(stock: Stock) -> str:
    try:
        from app.services.financial import get_financials

        return latest_change(to_rows(get_financials(stock)))
    except Exception:
        return ""


def _key_points(items: list, facts: list[dict]) -> list[dict]:
    valid = {fact.get("id") for fact in facts if fact.get("id")}
    cleaned = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        fact_id = str(item.get("fact_id") or "").strip()
        title = str(item.get("title") or "").strip()
        if valid and fact_id not in valid:
            continue
        if not title:
            continue
        cleaned.append({
            "lean": item.get("lean"),
            "title": title,
            "plain": str(item.get("plain") or "").strip(),
            "why_it_matters": str(item.get("why_it_matters") or "").strip(),
            "time": str(item.get("time") or "").strip(),
        })
    return cleaned


def _empty_watch(side: str) -> dict:
    if side == "偏多":
        better = "如果下一期收入和利润继续好于最近一期，偏多判断会更明显"
        worse = "如果下一期收入和利润明显差于最近一期，判断会转为偏空"
    else:
        better = "如果下一期收入和利润明显好于最近一期，判断会转为偏多"
        worse = "如果下一期收入和利润继续差于最近一期，偏空判断会更明显"
    return {
        "what": "下一期财报",
        "when": "尚未公告",
        "if_better": better,
        "if_worse": worse,
    }


def _display(summary: dict, rows: list, facts: list, bull: dict, bear: dict, change: str) -> dict:
    cited = {fact.get("row_id") for fact in facts if fact.get("row_id")}
    periods_read = len([row for row in rows if row.get("kind") == "财报"])
    periods_used = len([row for row in rows if row.get("id") in cited]) if cited else 0
    return finish_display(
        summary,
        bull,
        bear,
        change,
        label="财报面",
        fine_print=FINE_PRINT,
        points_key="key_points",
        points=_key_points(summary.get("key_points") or [], facts),
        more={
            "periods_read": periods_read,
            "periods_used": periods_used,
            "note": f"共看了 {periods_read} 期财报，采用了 {periods_used} 期",
        },
        missing_change="没有拿到最近一个交易日的涨跌",
        unmatched_change="最近的涨跌对不上这份财报",
        empty_watch=_empty_watch,
    )
