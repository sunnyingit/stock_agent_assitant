"""行业环境：所属板块的阶段，和近期行业层面的新闻。两样都取现成的维度结果（它们各自有缓存），取不到就记一条说明，不影响其他部分。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait

NEWS_LIMIT = 5
CONTEXT_TIMEOUT = 25   # 秒。板块、新闻冷启动要一两分钟，体检不等它们：没取完就先出结果，它们在后台继续算好缓存，下一轮再补上
_LABELS = {"sector": "板块阶段", "news": "行业新闻"}


def load_context(stock, timeout: float | None = None) -> dict:
    """返回 {"sector": {"stage"} 或 None, "news": [行业新闻] 或 None, "missing": [说明], "pending": [还没取完的部分]}。"""
    limit = CONTEXT_TIMEOUT if timeout is None else timeout
    pool = ThreadPoolExecutor(max_workers=2)
    jobs = {"sector": pool.submit(_sector, stock), "news": pool.submit(_news, stock)}
    done, _ = wait(jobs.values(), timeout=limit)
    pool.shutdown(wait=False)   # 没做完的线程继续跑，把各自维度的缓存填好
    values, missing, pending = {}, [], []
    for name, job in jobs.items():
        if job not in done:
            pending.append(name)
            missing.append(f"{_LABELS[name]}：数据还在整理，这次没用上")
            values[name] = None
            continue
        try:
            values[name] = job.result()
        except Exception as exc:
            values[name] = None
            missing.append(f"{_LABELS[name]}：{str(exc) or exc.__class__.__name__}")
    return {"sector": values["sector"], "news": values["news"], "missing": missing, "pending": pending}


def _sector(stock) -> dict | None:
    from app.services.sector_detail import build_detail

    payload = build_detail(stock)
    stage = (payload.get("stage") or {}).get("current")
    return {"stage": stage} if stage else None


def _news(stock) -> list[dict]:
    from app.services import news_detail

    payload = news_detail.build_detail(stock)
    if (payload.get("meta") or {}).get("state") == "computing":
        raise RuntimeError("新闻还在整理")
    rows = [
        row for row in [*(payload.get("key_events") or []), *((payload.get("other_news") or {}).get("items") or [])]
        if row.get("scope") == "industry" and row.get("in_window")
    ]
    rows.sort(key=lambda row: str(row.get("published_at") or ""), reverse=True)
    return [
        {"title": row.get("title") or "", "published_at": row.get("published_at"), "category": row.get("category"), "source_grade": row.get("source_grade")}
        for row in rows[:NEWS_LIMIT]
    ]
