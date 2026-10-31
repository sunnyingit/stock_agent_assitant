"""所属板块和公司概况。"""

from __future__ import annotations

from app import config
from app.tools import cache
from app.tools.http import get_json
from app.tools.stock import Stock, is_hk

_PROFILE_FIELDS = {
    "gsmc": "company_name",
    "sshy": "industry_name",
    "sszjhhy": "csrc_industry",
    "ssjys": "exchange",
    "qy": "area",
    "dsz": "chairman",
    "gyrs": "employees",
    "zczb": "registered_capital",
    "gswz": "website",
    "gsjj": "introduction",
}


def get_industry(stock: Stock, current_price: float | None = None) -> dict:
    cache_key = f"industry:{stock.ts_code}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    if is_hk(stock):
        result = {
            "industry": stock.industry,
            "csrc_industry": None,
            "price": current_price if current_price is not None else stock.price,
            "profile": {"company_name": stock.name, "exchange": "港交所"},
        }
        cache.set(cache_key, result, config.CACHE_TTL["industry"])
        return result

    payload = get_json(config.SURVEY_URL, {"code": stock.em_code})
    profile_raw = (payload or {}).get("jbzl") or {}
    profile = {
        target: profile_raw.get(source)
        for source, target in _PROFILE_FIELDS.items()
        if profile_raw.get(source) not in (None, "", "--")
    }
    result = {
        "industry": stock.industry or profile.get("industry_name"),
        "csrc_industry": profile.get("csrc_industry"),
        "price": current_price if current_price is not None else stock.price,
        "profile": profile,
    }
    cache.set(cache_key, result, config.CACHE_TTL["industry"])
    return result
