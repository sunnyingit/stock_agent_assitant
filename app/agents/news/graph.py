"""新闻分析。采集和打分由代码完成，模型只抽取事件和写结论。"""

from __future__ import annotations

from typing import Any, Callable

from app.agents.display import debate_result, finish_display
from app.agents.engine import run_agent
from app.agents.news.clean import clean_news
from app.agents.news.score import score_news
from app.tools.stock import Stock

FINE_PRINT = "以上为基于公开新闻的研究分析，不构成投资建议"
_EVENT_FIELDS = (
    "event_id", "category", "scope", "basis", "direction", "magnitude",
    "weight", "source_grade", "market_reaction",
)


def analyze_news(stock: Stock, model: Any = None, fetch_news: Callable[[], dict] | None = None) -> dict:
    holder: dict = {}
    result = run_agent(
        "news",
        stock,
        model=model,
        fetch=_fetch(stock, fetch_news),
        extra=_live_extra(stock, model, fetch_news),
        after_extract=_after_extract(stock, holder, model is None and fetch_news is None),
    )
    return _stamp(result, holder)


def debate_news(stock: Stock, model: Any = None, fetch_news: Callable[[], dict] | None = None) -> dict:
    """给用户看的新闻结论，以及后台保留的完整分析。"""
    live = model is None and fetch_news is None
    holder: dict = {}
    stats: dict = {}
    result = run_agent(
        "news",
        stock,
        model=model,
        fetch=_fetch(stock, fetch_news, stats) if fetch_news is not None else None,
        extra=_live_extra(stock, model, fetch_news),
        after_extract=_after_extract(stock, holder, live),
    )
    result = _stamp(result, holder)
    articles = result.get("articles") or []
    facts = result.get("facts") or []
    bull = result.get("bull") or {}
    bear = result.get("bear") or {}
    change = ""
    if live:
        change = _today_change(stock)
    display = _display(result.get("summary") or {}, articles, facts, bull, bear, change, stats)
    payload = debate_result(display, facts, bull, bear, {
        "articles": articles,
        "news_read": display["more"]["news_read"],
        "news_used": display["more"]["news_used"],
        "cleaning": stats,
    })
    verdict = (result.get("framework") or {}).get("verdict") or {}
    if verdict:
        payload["internal"]["verdict"]["score"] = verdict.get("score")
        payload["internal"]["framework"] = result.get("framework")
    return payload


def _fetch(stock: Stock, fetch_news: Callable[[], dict] | None, stats: dict | None = None):
    if fetch_news is None and stats is None:
        return None

    def fetch():
        if fetch_news is None:
            from app.services.news import get_news

            payload = get_news(stock)
        else:
            payload = fetch_news()
        payload = dict(payload or {})
        items, info = clean_news(payload.get("items") or [], stock)
        payload["items"] = items
        if stats is not None:
            stats.clear()
            stats.update(info)
        return payload

    return fetch


def _live_extra(stock: Stock, model, fetch_news) -> dict:
    if model is not None or fetch_news is not None:
        return {"change": ""}
    return {"change": _today_change(stock)}


def _after_extract(stock: Stock, holder: dict, live: bool):
    def after(state: dict) -> None:
        bars, sector = _market(stock) if live else ([], [])
        articles = state.get("material") if isinstance(state.get("material"), list) else []
        heat = 0
        for row in articles:
            try:
                heat = max(heat, int(row.get("heat_count") or 0))
            except (TypeError, ValueError):
                continue
        if not heat:
            heat = sum(1 for row in articles if row.get("source_grade") == "E")
        scored = score_news(state.get("facts") or [], articles, bars=bars, sector=sector, heat_count=heat)
        by_id = {item["fact_id"]: item for item in scored["events"]}
        for fact in state.get("facts") or []:
            item = by_id.get(fact.get("id"))
            if not item:
                continue
            for field in _EVENT_FIELDS:
                fact[field] = item.get(field)
        holder.clear()
        holder.update(scored)
        after.chief_extra = {"score": scored["verdict"]}

    after.chief_extra = {}
    return after


def _market(stock: Stock) -> tuple[list, list]:
    bars: list = []
    sector: list = []
    try:
        from app.services.series import get_bars

        bars = get_bars(stock, 30).get("items") or []
    except Exception:
        bars = []
    try:
        from app.services.series import get_sector

        sector = get_sector(stock, 30).get("items") or []
    except Exception:
        sector = []
    return bars, sector


def _stamp(result: dict, holder: dict) -> dict:
    verdict = holder.get("verdict")
    if not verdict or not result.get("facts"):
        return result
    summary = dict(result.get("summary") or {})
    current = dict(summary.get("verdict") or {})
    current["lean"] = verdict["lean"]
    current["lean_level"] = verdict["lean_level"]
    current["certainty"] = verdict["certainty"]
    current["certainty_note"] = verdict.get("certainty_note") or current.get("certainty_note") or ""
    summary["verdict"] = current
    stamped = dict(result)
    stamped["summary"] = summary
    stamped["framework"] = {
        "score": holder.get("score"),
        "verdict": verdict,
        "events": holder.get("events") or [],
    }
    return stamped


def _today_change(stock: Stock) -> str:
    try:
        from app.services.basic import get_basic

        pct = get_basic(stock).get("change_pct")
    except Exception:
        return ""
    if pct is None:
        return ""
    return f"{pct:+.2f}%"


def _key_news(items: list, articles: list[dict], facts: list[dict]) -> list[dict]:
    by_url = {row.get("url"): row for row in articles if row.get("url")}
    by_title = {row.get("title"): row for row in articles if row.get("title")}
    by_article = {}
    for fact in facts or []:
        if fact.get("article_id"):
            by_article.setdefault(fact["article_id"], fact)
    cleaned = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        article = by_url.get(url) or by_title.get(title)
        if articles and article is None:
            continue
        if article:
            title = title or article.get("title") or ""
            url = article.get("url") or url
        if not title:
            continue
        fact = by_article.get((article or {}).get("id")) or {}
        direction = {"利好": "偏多", "利空": "偏空"}.get(fact.get("direction") or "")
        cleaned.append({
            "lean": direction or item.get("lean"),
            "title": title,
            "plain": str(item.get("plain") or "").strip(),
            "why_it_matters": str(item.get("why_it_matters") or "").strip(),
            "source": str(item.get("source") or (article or {}).get("source") or "").strip(),
            "source_grade": fact.get("source_grade") or (article or {}).get("source_grade") or "",
            "magnitude": fact.get("magnitude") or "",
            "market_reaction": fact.get("market_reaction") or "",
            "time": str(item.get("time") or (article or {}).get("time") or "").strip(),
            "url": url,
        })
    return cleaned


def _empty_watch(side: str) -> dict:
    if side == "偏多":
        better = "如果后续消息继续好于现在的预期，偏多判断会更明显"
        worse = "如果后续消息明显差于预期，判断会转为偏空"
    else:
        better = "如果后续消息明显好于预期，判断会转为偏多"
        worse = "如果后续消息落在现在的预期或更差，偏空判断会更明显"
    return {
        "what": "后续公告和新闻",
        "when": "尚未确定",
        "if_better": better,
        "if_worse": worse,
    }


def _display(summary: dict, articles: list, facts: list, bull: dict, bear: dict, change: str, stats: dict) -> dict:
    cited = {fact.get("article_id") for fact in facts if fact.get("article_id")}
    news_read = stats.get("read") or len(articles)
    news_used = len(cited) if cited else len(articles)
    dropped = max(news_read - news_used, 0)
    return finish_display(
        summary,
        bull,
        bear,
        change,
        label="新闻面",
        fine_print=FINE_PRINT,
        points_key="key_news",
        points=_key_news(summary.get("key_news") or [], articles, facts),
        more={
            "news_read": news_read,
            "news_used": news_used,
            "note": f"共读了 {news_read} 条新闻，去掉重复和无关的 {dropped} 条",
        },
        missing_change="没有拿到今天的涨跌数据",
        unmatched_change="没有新闻对上今天的涨跌",
        empty_watch=_empty_watch,
        why_limit=90,
    )
