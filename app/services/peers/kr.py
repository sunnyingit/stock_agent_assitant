"""韩股同类近期走势。"""

from __future__ import annotations

from app import config
from app.services.peers.mapping import match_peers
from app.services.peers.quotes import trend_of
from app.tools import cache
from app.tools.stock import Stock


def get_kr_peers(stock: Stock, current_price: float | None = None) -> dict:
    cache_key = f"peers:kr:{stock.ts_code}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    _us_peers, kr_peers = match_peers(stock.industry)
    result = {
        "industry": stock.industry,
        "price": current_price if current_price is not None else stock.price,
        "peers": [trend_of(quote_id, name) for quote_id, name in kr_peers],
    }
    cache.set(cache_key, result, config.CACHE_TTL["peers"])
    return result
