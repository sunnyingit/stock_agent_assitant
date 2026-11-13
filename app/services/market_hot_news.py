"""市场页「今日热闻」：百度财经热搜 + 财经快讯热榜，按规则加权去重取 Top N。

热度只说明「大家在看什么」，不是投资建议。单路失败不拖垮整页。
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

from app.services import baidu_board as board
from app.services import news

_ZONE = ZoneInfo("Asia/Shanghai")
_TITLE_NOISE = re.compile(r"[\s\W_]+", re.UNICODE)
_NEWS_SOURCES = (
    ("cls", "财联社", 18.0),
    ("wallstreetcn", "华尔街见闻", 16.0),
)
_POOL_LIMIT = 30  # 每路先取这么多再加权


def _norm_title(title: str) -> str:
    text = str(title or "").strip().lower()
    text = _TITLE_NOISE.sub("", text)
    return text


def _similar(a: str, b: str) -> bool:
    """标题规范化后相同，或较短一方被较长一方包含且长度够（中文标题按字数）。"""
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < 4:
        return False
    return short in long and len(short) / len(long) >= 0.5


def _rank_boost(rank: int | None, size: int = 30) -> float:
    if not rank or rank < 1:
        return 0.0
    return max(0.0, (size - rank + 1) / size) * 25.0


def _score_baidu(item: dict, max_hot: int) -> float:
    hot = item.get("hot")
    if hot and max_hot > 0:
        score = float(hot) / max_hot * 70.0
    else:
        score = _rank_boost(item.get("rank"))
    score += _rank_boost(item.get("rank"))
    tag = str(item.get("tag") or "")
    if tag == "热":
        score += 8.0
    elif tag == "新":
        score += 6.0
    if item.get("pinned"):
        score += 12.0
    return score


def _parse_time(time_text: str | None) -> datetime | None:
    if not time_text:
        return None
    text = str(time_text).strip()
    for fmt, width in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(text[:width], fmt).replace(tzinfo=_ZONE)
        except ValueError:
            continue
    return None


def _freshness_boost(time_text: str | None, now: datetime) -> float:
    when = _parse_time(time_text)
    if when is None:
        return 0.0
    hours = max(0.0, (now - when).total_seconds() / 3600.0)
    if hours <= 6:
        return 12.0
    if hours <= 24:
        return 8.0
    if hours <= 72:
        return 3.0
    return 0.0


def _score_news(item: dict, base: float, now: datetime) -> float:
    return base + _rank_boost(item.get("rank"), size=20) + _freshness_boost(item.get("time"), now)


def _baidu_rows() -> tuple[list[dict], dict]:
    raw = board.fetch_board("finance", _POOL_LIMIT)
    max_hot = max((int(row["hot"]) for row in raw["items"] if row.get("hot")), default=0)
    rows = []
    for row in raw["items"]:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        rows.append({
            "title": title,
            "url": row.get("url") or "",
            "source": "baidu_board",
            "source_name": "百度财经热搜",
            "hot": row.get("hot"),
            "hot_text": row.get("hot_text") or "",
            "tag": row.get("tag") or "",
            "rank_hint": row.get("rank"),
            "weight": round(_score_baidu(row, max_hot), 2),
        })
    meta = {
        "ok": True,
        "board": raw.get("board"),
        "updated_at": raw.get("updated_at") or raw.get("fetched_at") or "",
        "stale": bool(raw.get("stale")),
        "count": len(rows),
    }
    return rows, meta


def _news_rows(source_id: str, source_name: str, base: float, now: datetime) -> tuple[list[dict], dict]:
    items = news.fetch_hot_news(source_id, _POOL_LIMIT)
    rows = []
    for item in items:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        rows.append({
            "title": title,
            "url": item.get("url") or "",
            "source": source_id,
            "source_name": source_name,
            "hot": None,
            "hot_text": "",
            "tag": "",
            "rank_hint": item.get("rank"),
            "time": item.get("time"),
            "weight": round(_score_news(item, base, now), 2),
        })
    return rows, {"ok": True, "source": source_id, "count": len(rows)}


def _dedupe(rows: list[dict]) -> list[dict]:
    ordered = sorted(rows, key=lambda r: r.get("weight") or 0, reverse=True)
    kept: list[dict] = []
    norms: list[str] = []
    for row in ordered:
        key = _norm_title(row["title"])
        if not key:
            continue
        if any(_similar(key, old) for old in norms):
            continue
        kept.append(row)
        norms.append(key)
    return kept


def build(limit: int = 10, now: datetime | None = None) -> dict:
    """合成今日热闻 Top N。"""
    limit = max(1, min(int(limit), 30))
    now = now or datetime.now(_ZONE)
    pool: list[dict] = []
    sources: dict[str, dict] = {}
    errors: list[str] = []

    def take_baidu():
        return "baidu", _baidu_rows()

    def take_news(source_id: str, source_name: str, base: float):
        return source_id, _news_rows(source_id, source_name, base, now)

    jobs = [take_baidu]
    for source_id, source_name, base in _NEWS_SOURCES:
        jobs.append(lambda s=source_id, n=source_name, b=base: take_news(s, n, b))

    with ThreadPoolExecutor(max_workers=3) as pool_exec:
        futures = [pool_exec.submit(job) for job in jobs]
        for fut in as_completed(futures):
            try:
                key, (rows, meta) = fut.result()
                pool.extend(rows)
                sources[key] = meta
            except Exception as exc:  # noqa: BLE001 - 单路失败继续
                errors.append(str(exc).strip() or type(exc).__name__)

    items = _dedupe(pool)[:limit]
    return {
        "status": "ok",
        "note": "按热度与名次规则汇总，热度不等于投资建议。",
        "limit": limit,
        "count": len(items),
        "partial": bool(errors) or len(sources) < 1 + len(_NEWS_SOURCES),
        "errors": errors,
        "sources": sources,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "items": items,
    }
