"""新闻。个股用东方财富，按关键词搜索用东方财富搜索，热榜用 NewsNow。"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from xml.etree import ElementTree

import requests

from app import config
from app.tools import cache
from app.tools.http import get_json
from app.tools.stock import Stock, is_hk

NEWS_API = "https://newsnow.busiyi.world/api/s"
EASTMONEY_NEWS = "https://emweb.securities.eastmoney.com/PC_HSF10/NewsBulletin/PageAjax"
HK_NEWS = "https://emweb.securities.eastmoney.com/PC_HKF10/CompanyNews/PageAjax"
GOOGLE_RSS = "https://news.google.com/rss/search"
GOOGLE_TIMEOUT = (2, 6)
SEARCH_API = "https://search-api-web.eastmoney.com/search/jsonp"
_TAG = re.compile(r"<[^>]+>")

SOURCES = {
    "cls": "财联社",
    "wallstreetcn": "华尔街见闻",
    "xueqiu": "雪球热榜",
    "weibo": "微博热搜",
    "zhihu": "知乎热榜",
    "baidu": "百度热搜",
    "toutiao": "今日头条",
    "douyin": "抖音热榜",
    "thepaper": "澎湃新闻",
    "36kr": "36氪",
    "ithome": "IT之家",
    "v2ex": "V2EX",
    "juejin": "掘金",
    "hackernews": "Hacker News",
    "eastmoney": "东方财富",
    "search": "东方财富搜索",
    "google": "Google News",
}


def _time_text(value) -> str | None:
    if not value:
        return None
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return str(value)
    if stamp > 10_000_000_000:
        stamp = stamp / 1000
    return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")


def _plain_text(value: str) -> str:
    text = html.unescape(_TAG.sub(" ", value or ""))
    return re.sub(r"\s+", " ", text).strip()


def fetch_hot_news(source_id: str, count: int = 15) -> list[dict]:
    cache_key = f"news:source:{source_id}:{count}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    payload = get_json(NEWS_API, {"id": source_id})
    items = []
    for rank, item in enumerate((payload.get("items") or [])[:count], start=1):
        items.append({
            "rank": rank,
            "source": source_id,
            "source_name": SOURCES.get(source_id, source_id),
            "title": item.get("title") or "",
            "url": item.get("url") or item.get("mobileUrl") or "",
            "time": _time_text(item.get("pubDate") or item.get("publish_time")),
        })
    cache.set(cache_key, items, config.CACHE_TTL["news"])
    return items


def fetch_stock_news(em_code: str, count: int = 15, market: str = "CN") -> list[dict]:
    """按股票代码拉东方财富个股新闻。返回的条目都属于这只股票。"""
    cache_key = f"news:eastmoney:{market}:{em_code}:{count}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    if market == "HK":
        url, block_name = HK_NEWS, "gsxw"
    else:
        url, block_name = EASTMONEY_NEWS, "gszx"
    payload = get_json(
        url,
        {"code": em_code},
        headers={"Referer": "https://emweb.securities.eastmoney.com/"},
    )
    block = (payload or {}).get(block_name) or {}
    rows = ((block.get("data") or {}).get("items")) or []
    items = []
    for rank, item in enumerate(rows[:count], start=1):
        source = item.get("source")
        if isinstance(source, dict):
            source = source.get("name") or ""
        link = item.get("url") or item.get("uniqueUrl") or ""
        if not link and item.get("code"):
            link = f"https://finance.eastmoney.com/a/{item.get('code')}.html"
        items.append({
            "rank": rank,
            "source": "eastmoney",
            "source_name": source or SOURCES["eastmoney"],
            "title": item.get("title") or "",
            "url": link,
            "time": _time_text(item.get("showDateTime") or item.get("updateTime")),
            "summary": _plain_text(item.get("summary") or ""),
        })
    cache.set(cache_key, items, config.CACHE_TTL["news"])
    return items


def fetch_announcements(stock: Stock, count: int = 30) -> list[dict]:
    """交易所公告。东方财富个股页的 gsgg，按股票代码拉取，记为 A 级。"""
    if is_hk(stock) or not stock.em_code:
        return []
    cache_key = f"news:ann:{stock.em_code}:{count}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    payload = get_json(
        EASTMONEY_NEWS,
        {"code": stock.em_code},
        headers={"Referer": "https://emweb.securities.eastmoney.com/"},
    )
    rows = (payload or {}).get("gsgg") or []
    if isinstance(rows, dict):
        rows = ((rows.get("data") or {}).get("items")) or []
    items = []
    for item in rows[:count]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        art = str(item.get("art_code") or "")
        raw = str(item.get("display_time") or item.get("notice_date") or "").strip()
        if len(raw) >= 19:
            raw = raw[:19]
        url = ""
        if art and stock.code:
            url = f"https://data.eastmoney.com/notices/detail/{stock.code}/{art}.html"
        items.append({
            "source": "exchange",
            "source_name": "交易所公告",
            "title": title,
            "url": url,
            "time": raw or None,
            "summary": "",
        })
    cache.set(cache_key, items, config.CACHE_TTL["news"])
    return items


def parse_google_rss(xml_text: str, keyword: str, max_results: int) -> list[dict]:
    root = ElementTree.fromstring(xml_text)
    items = []
    for rank, node in enumerate(root.findall(".//item")[:max_results], start=1):
        source_node = node.find("source")
        source_name = ""
        if source_node is not None and source_node.text:
            source_name = source_node.text.strip()
        items.append({
            "rank": rank,
            "source": "google",
            "source_name": source_name or SOURCES["google"],
            "title": (node.findtext("title") or "").strip(),
            "url": (node.findtext("link") or "").strip(),
            "time": (node.findtext("pubDate") or "").strip() or None,
            "summary": _plain_text(node.findtext("description") or ""),
            "keyword": keyword,
        })
    return items


def parse_search_news(payload: dict, keyword: str, max_results: int) -> list[dict]:
    result = (payload or {}).get("result") if isinstance(payload, dict) else None
    rows = result.get("cmsArticleWebOld") if isinstance(result, dict) else None
    items = []
    for rank, row in enumerate((rows or [])[:max_results], start=1):
        if not isinstance(row, dict):
            continue
        title = _plain_text(str(row.get("title") or ""))
        if not title:
            continue
        media = str(row.get("mediaName") or "").strip()
        items.append({
            "rank": rank,
            "source": "search",
            "source_name": media or SOURCES["search"],
            "title": title,
            "url": str(row.get("url") or "").strip(),
            "time": str(row.get("date") or "").strip() or None,
            "summary": _plain_text(str(row.get("content") or "")),
            "keyword": keyword,
        })
    return items


def fetch_search_news(keyword: str, max_results: int = 12) -> list[dict]:
    text = (keyword or "").strip()
    if not text:
        return []
    cache_key = f"news:search:{text}:{max_results}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    param = {
        "uid": "",
        "keyword": text,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "default",
                "sort": "default",
                "pageIndex": 1,
                "pageSize": max_results,
                "preTag": "",
                "postTag": "",
            }
        },
    }
    # 这个接口对 requests 的请求会回一份无关结果，用标准库才能搜到文章。
    body = json.dumps(param, ensure_ascii=False, separators=(",", ":"))
    url = SEARCH_API + "?cb=&param=" + urllib.parse.quote(body)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://so.eastmoney.com/"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        raw = response.read().decode("utf-8", "replace").strip()
    if raw.endswith(")") and "(" in raw:
        raw = raw[raw.find("(") + 1:-1]
    items = parse_search_news(json.loads(raw), text, max_results)
    cache.set(cache_key, items, config.CACHE_TTL["news"])
    return items


def fetch_google_news(keyword: str, language: str = "zh-CN", max_results: int = 12) -> list[dict]:
    cache_key = f"news:google:{keyword}:{language}:{max_results}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(
            GOOGLE_RSS,
            params={"q": keyword, "hl": language, "gl": "CN"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=GOOGLE_TIMEOUT,
        )
        response.raise_for_status()
        items = parse_google_rss(response.content.decode("utf-8", "replace"), keyword, max_results)
    except Exception:
        cache.set(cache_key, [], 60)
        raise
    cache.set(cache_key, items, config.CACHE_TTL["news"])
    return items


def _needles(stock: Stock) -> list[str]:
    needles = []
    if stock.name and stock.name != stock.code:
        needles.append(stock.name)
    if is_hk(stock):
        if stock.code:
            needles.append(stock.code)
        if stock.ts_code:
            needles.append(stock.ts_code)
        return needles
    if stock.code:
        needles.append(stock.code)
        needles.extend(f"{prefix}{stock.code}" for prefix in ("SH", "SZ", "BJ"))
    if stock.ts_code:
        needles.append(stock.ts_code)
    return needles


def search_query(stock: Stock, keyword: str) -> str:
    """用户要搜的原话。不拼股票名称。空字符串表示不搜索。"""
    del stock
    return (keyword or "").strip()


def _mentions(item: dict, keyword: str) -> bool:
    text = (keyword or "").strip()
    if not text:
        return True
    blob = " ".join(str(item.get(key) or "") for key in ("title", "summary"))
    return text in blob


def related_news(items: list[dict], stock: Stock) -> list[dict]:
    needles = _needles(stock)
    matched = []
    for item in items:
        blob = " ".join(str(item.get(key) or "") for key in ("title", "summary", "url"))
        if any(text in blob for text in needles):
            matched.append(item)
    return matched


def merge_news(
    google_items: list[dict],
    hot_items: list[dict],
    source_ids: list[str],
    stock: Stock,
    limit: int,
    direct_items: list[dict] | None = None,
    search_items: list[dict] | None = None,
) -> tuple[bool, list[dict]]:
    del source_ids, stock
    seen: set[str] = set()
    items: list[dict] = []

    def add(item: dict) -> None:
        title = (item.get("title") or "").strip()
        if not title or title in seen or len(items) >= limit:
            return
        seen.add(title)
        items.append(item)

    for item in search_items or []:
        add(item)
    for item in direct_items or []:
        add(item)
    for item in google_items:
        add(item)
    for item in hot_items:
        add(item)
    return bool(items), items[:limit]


def get_topic_news(keyword: str, limit: int = 24) -> dict:
    """无绑定股票：按主题搜东方财富 / Google，并筛热榜。"""
    keyword = (keyword or "").strip()
    if not keyword:
        raise ValueError("主题新闻需要 keyword")
    limit = max(1, min(int(limit or 24), 80))
    cache_key = f"news:topic:{keyword}:{limit}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    search_items: list[dict] = []
    google_items: list[dict] = []
    hot: list[dict] = []
    unavailable: list[str] = []
    source_ids = [sid for sid in list(SOURCES) if sid not in ("google", "eastmoney", "search")]
    with ThreadPoolExecutor(max_workers=min(6, 2 + len(source_ids))) as pool:
        futures = {
            pool.submit(fetch_search_news, keyword, min(max(limit, 12), 30)): "search",
            pool.submit(fetch_google_news, keyword, "zh-CN", min(max(limit, 12), 20)): "google",
        }
        for source_id in source_ids:
            futures[pool.submit(fetch_hot_news, source_id, max(limit, 15))] = source_id
        for future in as_completed(futures):
            source_id = futures[future]
            try:
                rows = future.result()
            except Exception:
                if source_id not in unavailable:
                    unavailable.append(source_id)
                continue
            if source_id == "search":
                search_items.extend(rows)
            elif source_id == "google":
                google_items.extend(rows)
            else:
                hot.extend(rows)
    hot = [item for item in hot if _mentions(item, keyword)]
    # merge_news 里 stock 未使用，传占位对象会更麻烦；直接本地去重
    seen: set[str] = set()
    items: list[dict] = []
    for item in list(search_items) + list(google_items) + list(hot):
        title = (item.get("title") or "").strip()
        if not title or title in seen or len(items) >= limit:
            continue
        seen.add(title)
        items.append(item)
    used = []
    if search_items:
        used.append({"id": "search", "name": SOURCES["search"], "count": sum(1 for i in items if i.get("source") == "search")})
    if google_items:
        used.append({"id": "google", "name": SOURCES["google"], "count": sum(1 for i in items if i.get("source") == "google")})
    for source_id in source_ids:
        count = sum(1 for i in items if i.get("source") == source_id)
        if count:
            used.append({"id": source_id, "name": SOURCES.get(source_id, source_id), "count": count})
    result = {
        "matched": bool(items),
        "count": len(items),
        "sources": used,
        "unavailable": unavailable,
        "items": items,
        "query": keyword,
        "keyword": keyword,
    }
    cache.set(cache_key, result, config.CACHE_TTL["news"] if items else 60)
    return result


def get_news(stock: Stock, limit: int = 24, sources: list[str] | None = None, keyword: str = "") -> dict:
    keyword = (keyword or "").strip()
    source_ids = [
        source_id for source_id in (sources or list(SOURCES))
        if source_id not in ("google", "eastmoney", "search")
    ]
    cache_key = f"news:related:{stock.ts_code}:{limit}:{','.join(source_ids)}:eastmoney:{keyword}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    hot: list[dict] = []
    google_items: list[dict] = []
    search_items: list[dict] = []
    direct_items: list[dict] = []
    unavailable: list[str] = []
    query = search_query(stock, keyword)
    workers = min(8, max(1, len(source_ids) + 2))
    futures = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        if stock.em_code and not keyword:
            market = "HK" if is_hk(stock) else "CN"
            futures[pool.submit(fetch_stock_news, stock.em_code, max(limit, 10), market)] = "eastmoney"
        if query:
            futures[pool.submit(fetch_search_news, query, min(max(limit, 12), 20))] = "search"
            futures[pool.submit(fetch_google_news, query, "zh-CN", min(max(limit, 12), 20))] = "google"
        for source_id in source_ids:
            futures[pool.submit(fetch_hot_news, source_id, max(limit, 15))] = source_id
        for future in as_completed(futures):
            source_id = futures[future]
            try:
                rows = future.result()
            except Exception:
                if source_id not in unavailable:
                    unavailable.append(source_id)
                continue
            if source_id == "google":
                google_items.extend(rows)
            elif source_id == "search":
                search_items.extend(rows)
            elif source_id == "eastmoney":
                direct_items.extend(rows)
            else:
                hot.extend(rows)

    if keyword:
        direct_items = [item for item in direct_items if _mentions(item, keyword)]
        hot = [item for item in hot if _mentions(item, keyword)]
    if not hot and not google_items and not direct_items and not search_items:
        if not keyword:
            raise RuntimeError("未获取到新闻")
        result = {
            "matched": False,
            "count": 0,
            "sources": [],
            "unavailable": unavailable,
            "items": [],
            "query": query,
            "keyword": keyword,
        }
        cache.set(cache_key, result, 60)
        return result

    matched, items = merge_news(google_items, hot, source_ids, stock, limit, direct_items, search_items)
    used = []
    if search_items:
        used.append({
            "id": "search",
            "name": SOURCES["search"],
            "count": sum(1 for item in items if item.get("source") == "search"),
        })
    if direct_items:
        used.append({
            "id": "eastmoney",
            "name": SOURCES["eastmoney"],
            "count": sum(1 for item in items if item.get("source") == "eastmoney"),
        })
    if google_items:
        used.append({"id": "google", "name": SOURCES["google"], "count": sum(1 for item in items if item.get("source") == "google")})
    for source_id in source_ids:
        count = sum(1 for item in items if item.get("source") == source_id)
        if count:
            used.append({"id": source_id, "name": SOURCES.get(source_id, source_id), "count": count})
    result = {
        "matched": matched,
        "count": len(items),
        "sources": used,
        "unavailable": unavailable,
        "items": items,
        "query": query,
        "keyword": keyword,
    }
    cache.set(cache_key, result, config.CACHE_TTL["news"])
    return result
