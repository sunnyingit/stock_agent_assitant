"""各业务的取数函数。JSON 里的 input.fetcher 只写这里的名字。"""

from __future__ import annotations

from app.agents.llm import ModelError
from app.tools.stock import Stock


def fetch_news(stock: Stock) -> dict:
    from app.agents.news.clean import clean_news
    from app.services.news import get_news

    payload = dict(get_news(stock))
    items, _info = clean_news(payload.get("items") or [], stock)
    payload["items"] = items
    return payload


def fetch_financial(stock: Stock) -> dict:
    from app.agents.financial.data import to_rows
    from app.services.financial import get_financials

    return to_rows(get_financials(stock))


FETCHERS = {
    "news": fetch_news,
    "financial": fetch_financial,
}


def resolve_fetch(name: str, stock: Stock):
    fetcher = FETCHERS.get(name or "")
    if fetcher is None:
        raise ModelError(f"未注册的数据源: {name}")
    return lambda: fetcher(stock)
