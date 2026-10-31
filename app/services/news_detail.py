"""新闻详情。采集、去重、时间窗口和权重计算由代码完成；每条新闻的类别、范围、依据、方向、金额和量级由模型判断，
模型不可用时退回关键词规则。"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from threading import Lock
from zoneinfo import ZoneInfo

from app import config
from app.agents.news.clean import annotate, clean_news
from app.agents.news.score import score_news
from app.services.verdict_view import STATUS_OF_LEAN, model_fields, rule_fields, slim
from app.tools import cache
from app.tools.stock import Stock

RULE_VERSION = "news-1"
_LEAN = {"偏多": "bullish", "偏空": "bearish"}
_CERTAINTY = {"高": "high", "一般": "medium", "低": "low"}
_IMPACT = {"利好": "positive", "利空": "negative", "中性": "neutral"}
_BASIS_EN = {"事实": "fact", "计划": "plan", "预测": "forecast", "传闻": "rumor"}
_CATEGORY_EN = {
    "业绩": "earnings",
    "经营": "operations",
    "资本": "capital",
    "风险与治理": "governance",
    "政策与行业": "industry",
    "宏观与市场": "macro",
    "观点": "opinion",
    "传闻与澄清": "rumor",
}
_SCOPE_EN = {"公司": "company", "行业": "industry", "市场": "market"}
_MAGNITUDE_EN = {"大": "large", "中": "medium", "小": "small"}
_SESSION_EN = {"盘前": "pre", "盘中": "intra", "盘后": "post"}
_GRADE_RANK = {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1}
_GRADE_LABEL = {"A": "A 公告", "B": "B 官方", "C": "C 媒体", "D": "D 研报", "E": "E 热榜"}
_COMMENTARY = ("为何", "为什么", "怎么看", "前瞻", "机会", "点评", "解读", "曝光", "出炉", "密集")
_inflight: set[str] = set()
_inflight_lock = Lock()


def build_detail(
    stock: Stock,
    days: int = 3,
    model=None,
    articles: list[dict] | None = None,
    bars: list[dict] | None = None,
    sector: list[dict] | None = None,
    today: date | None = None,
) -> dict:
    days = _days(days)
    today = today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    cache_key = f"news_detail:{stock.ts_code}:{days}:{today.isoformat()}"
    live = articles is None and bars is None and sector is None
    if live and model is None:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        if not _claim(cache_key):
            return _shell(stock, days, "computing", today)
    try:
        payload = _assemble(stock, days, model, articles, bars, sector, today)
    finally:
        if live and model is None:
            _release(cache_key)
    if live and model is None and payload["meta"]["state"] != "computing":
        cache.set(cache_key, payload, config.CACHE_TTL["news_detail"])
    return payload


def _assemble(stock, days, model, articles, bars, sector, today: date) -> dict:
    market_bars, market_sector = _market(stock, bars, sector)
    if articles is None and bars is None:
        market_bars = _with_today(market_bars, today, _realtime(stock))
    trade_dates = [str(row.get("trade_date") or "") for row in market_bars if row.get("trade_date")]
    found = articles if articles is not None else _load_news(stock)
    stamp = datetime(today.year, today.month, today.day, 16, 0)
    live = articles is None and bars is None and sector is None
    # 清洗也交给模型：是否相关、是否重复、是否例行都由它判断；代码只补属性，不丢弃
    prepared, _ = annotate(found, stock, today=stamp, trade_dates=trade_dates, window_days=days)
    for index, article in enumerate(prepared, start=1):
        article.setdefault("id", f"N{index}")
    judged = _judge(prepared, model, live, stock)
    if judged is not None:
        kept, facts, copy, stats = judged
        ready = True
    else:   # 模型不可用或没有给出可用的结果：退回规则清洗和关键词抽取
        kept, stats = clean_news(found, stock, today=stamp, trade_dates=trade_dates, window_days=days)
        for index, article in enumerate(kept, start=1):
            article.setdefault("id", f"N{index}")
        facts = [_fact_from_article(article, index) for index, article in enumerate(kept, start=1)]
        copy, ready = {}, False
    as_of = today.strftime("%Y%m%d")
    heat = stats.get("heat") or 0
    scored = score_news(facts, kept, bars=market_bars, sector=market_sector, heat_count=heat, as_of=as_of)
    events = _attach(scored["events"], facts, kept)
    _enrich_markers(events, market_bars)
    _fill_buyback(events, market_bars)
    company_facts = [
        fact for fact in facts
        if fact.get("scope") == "公司" and _article_in_window(fact, kept)
    ]
    company_ids = {fact.get("article_id") for fact in company_facts}
    company_articles = [article for article in kept if article.get("id") in company_ids]
    company_scored = score_news(
        company_facts,
        company_articles,
        bars=market_bars,
        sector=market_sector,
        heat_count=heat,
        as_of=as_of,
    ) if company_facts else {"events": [], "score": 0, "verdict": {}}
    return _payload(
        stock, days, today, stats, company_scored, events, market_bars, market_sector, copy, ready,
    )


def _payload(stock, days, today, stats, scored, events, bars, sector, copy, ready: bool) -> dict:
    effective = [
        item for item in events
        if item.get("in_window") and item.get("scope") == "公司" and item.get("direction") in {"利好", "利空"}
    ]
    current = [item for item in events if item.get("in_window") and item.get("scope") == "公司"]
    if not effective:
        scored = {"score": 0, "verdict": {"lean": "", "lean_level": 0, "certainty": "", "certainty_note": ""}}
    # no_news 只表示窗口里没有公司自己的事件；有事件但方向都是中性，是"有消息、看不出方向"，状态仍是 ok
    state = "ok" if current else "no_news"
    key_events, others = _pick(events, scored["verdict"])
    primary = _primary(events, scored["verdict"])
    meta = _meta(days, today, stats, len(events), bars, ready, state)
    if meta["state"] == "computing":
        return _shell(stock, days, "computing", today)
    verdict, model_text = _verdict(scored, events, key_events, others, copy, stock, days, effective)
    return {
        "ts_code": stock.ts_code,
        "stock_name": stock.name,
        "meta": meta,
        "verdict": verdict,
        # 模型写成了结论才有；方向仍由代码打分决定，模型只写这句话
        "headline": model_fields(STATUS_OF_LEAN[verdict["lean"]][0], model_text),
        "reaction": _reaction_block(primary, key_events, bars, sector, today),
        "timeline": _timeline(bars, sector, events, key_events),
        "key_events": [_event_row(item, ready) for item in key_events],
        "other_news": {"total": len(others), "items": [_other(item) for item in others]},
        "heat": _heat(events, stats),
        "watch_next": _watch(verdict["lean"], any(item.get("source_grade") == "A" for item in current)),
        "disclaimer_text": _disclaimer(events),
    }


def public(payload: dict) -> dict:
    """对外响应：verdict 只留和 headline 同一套的四个字段；lean/level/certainty 只给综合判断读。"""
    verdict = payload.get("verdict")
    return {**payload, "verdict": None if verdict is None else slim(verdict)}


def _shell(stock: Stock, days: int, state: str, today: date) -> dict:
    return {
        "ts_code": stock.ts_code,
        "stock_name": stock.name,
        "meta": {
            "state": state,
            "as_of": "",
            "updated_at": _now(),
            "status_text": "正在整理新闻",
            "window_days": days,
            "counts": {"found": 0, "kept": 0, "events": 0},
            "rule_version": RULE_VERSION,
            "model_version": "",
        },
        "verdict": None,
        "headline": None,
        "reaction": None,
        "timeline": None,
        "key_events": None,
        "other_news": None,
        "heat": None,
        "watch_next": None,
        "disclaimer_text": None,
    }


def _meta(days, today, stats, event_count, bars, ready: bool, state: str = "ok") -> dict:
    as_of = today.isoformat() if today else ""
    if not as_of and bars:
        as_of = _iso(bars[-1].get("trade_date"))
    stamp = _now()
    clock = stamp[5:16].replace("T", " ")
    kept = stats.get("kept") or 0
    in_window = stats.get("in_window")
    if in_window is None:
        in_window = kept
    return {
        "state": state,
        "as_of": as_of,
        "updated_at": stamp,
        "status_text": f"近 {days} 天 · {in_window} 条有效 · {clock}",
        "window_days": days,
        "counts": {
            "found": stats.get("read") or 0,
            "kept": kept,
            "events": event_count,
            "in_window": in_window,
        },
        "dropped": stats.get("dropped") or [],
        "rule_version": RULE_VERSION,
        "model_version": config.LLM_MODEL if ready else "template",
    }


def _verdict(scored, events, key_events, others, copy, stock, days, effective) -> tuple[dict, str | None]:
    """规则结论（四个字段 + 给综合判断读的 lean/level/certainty），和模型写的那句话。"""
    raw = scored["verdict"]
    if not effective:
        lean, level, certainty = "unclear", 0, None
    else:
        lean = _LEAN.get(raw.get("lean"), "unclear")
        level = raw.get("lean_level") or 1
        certainty = _CERTAINTY.get(raw.get("certainty"), "low")
        if not _official_fact(effective) or _only_forecast(effective):
            certainty = "low"
    template, model_text = _copy(events, key_events, others, copy, stock, days)
    return {**rule_fields(lean, template), "lean": lean, "level": level, "certainty": certainty}, model_text


def _official_fact(events) -> bool:
    return any(item.get("source_grade") in {"A", "B"} and item.get("basis") == "事实" for item in events)


def _only_forecast(events) -> bool:
    return bool(events) and all(
        item.get("basis") in {"预测", "传闻"} or item.get("category") == "观点" for item in events
    )


def _reaction_block(primary, key_events, bars, sector, today: date | None = None) -> dict:
    reaction = (primary or {}).get("reaction") or {}
    priced = _priced(primary)
    pre = reaction.get("pre_excess")
    today_chg = None
    if bars and _number(bars[-1].get("pct_chg")):
        today_chg = round(float(bars[-1]["pct_chg"]) / 100, 6)
    event_id, text = _today_link(key_events, bars)
    intraday = bool(bars and bars[-1].get("intraday"))
    latest = _iso(bars[-1].get("trade_date")) if bars else ""
    if (
        bars
        and not intraday
        and today
        and latest
        and latest < today.isoformat()
        and _number(bars[-1].get("pct_chg"))
    ):
        prefix = f"昨日 {float(bars[-1]['pct_chg']):+.2f}%"
        text = prefix if text == "没有对应新闻" else f"{prefix}，{text}"
    return {
        "priced_in": priced,
        "pre_excess_5d": None if pre is None else round(float(pre) / 100, 6),
        "today": {"chg": today_chg, "event_id": event_id, "text": text},
    }


def _priced(event) -> str:
    """事件是否已被股价反应：not_yet / partly / mostly；算不出来是 unknown。
    算不出来：没有主事件、事件本身是中性、或者前后的行情数据不够。
    反应日还没有行情（收盘后发布的新闻）不算"算不出来"：市场确实还没反应，仍是 not_yet。"""
    if not event or event.get("direction") not in {"利好", "利空"}:
        return "unknown"
    reaction = event.get("reaction") or {}
    if reaction.get("pending"):
        return "not_yet"
    pre = reaction.get("pre_excess")
    day = reaction.get("day_excess")
    if pre is None and day is None:
        return "unknown"
    positive = event.get("direction") == "利好"
    pre_agrees = pre is not None and ((positive and pre >= 5) or (not positive and pre <= -5))
    day_agrees = day is not None and ((positive and day > 0) or (not positive and day < 0))
    if pre_agrees and day_agrees:
        return "mostly"
    if pre_agrees or day_agrees:
        return "partly"
    return "not_yet"


def _today_link(key_events, bars) -> tuple[str | None, str]:
    if not bars:
        return None, "没有对应新闻"
    latest = str(bars[-1].get("trade_date") or "")
    change = bars[-1].get("pct_chg")
    if not _number(change) or float(change) == 0:
        return None, "没有对应新闻"
    positive = float(change) > 0
    for item in key_events:
        if item.get("marker_date") != latest:
            continue
        if (item.get("direction") == "利好") != positive:
            continue
        return item["event_id"], f"由事件 {item['rank']} 带动"
    return None, "没有对应新闻"


def _timeline(bars, sector, events, key_events) -> dict:
    chosen = bars[-20:]
    sector_map = {str(row.get("trade_date")): row.get("pct_change") for row in sector or []}
    rows = []
    for bar in chosen:
        excess = _excess(bar, sector_map)
        rows.append({
            "date": _iso(bar.get("trade_date")),
            "close": bar.get("close"),
            "excess_vs_sector": None if excess is None else round(excess / 100, 6),
            "is_intraday": bool(bar.get("intraday")),
        })
    ranks = {item["event_id"]: item["rank"] for item in key_events}
    markers = []
    for item in events:
        if not item.get("marker_date"):
            continue
        markers.append({
            "event_id": item["event_id"],
            "date": _iso(item["marker_date"]),
            "impact": _impact(item.get("direction")),
            "weight": item.get("weight"),
            "is_key": item["event_id"] in ranks,
            "rank": ranks.get(item["event_id"]),
        })
    alt = ""
    if rows:
        alt = (
            f"近{len(rows)}个交易日，收盘价从{_px(rows[0]['close'])}到{_px(rows[-1]['close'])}，"
            f"标出{len(markers)}个事件"
        )
    return {"bars": rows, "markers": markers, "chart_alt_text": alt}


def _event_row(item: dict, ready: bool = False) -> dict:
    reaction = item.get("reaction") or {}
    day = reaction.get("day_excess")
    summary = item.get("summary_text") if ready else None
    return {
        "rank": item["rank"],
        "event_id": item["event_id"],
        "title": item.get("title") or "",
        "url": item.get("url") or "",
        "summary": summary or None,
        "impact": _impact(item.get("direction")),
        "role": item["role"],
        "category": _enum(item.get("category"), _CATEGORY_EN),
        "source_grade": item.get("source_grade") or "",
        "magnitude": _enum(item.get("magnitude"), _MAGNITUDE_EN),
        "magnitude_text": item.get("magnitude_text") or "",
        "basis": _enum(item.get("basis"), _BASIS_EN),
        "persistence": _persistence(item.get("persistence")),
        "scope": _enum(item.get("scope"), _SCOPE_EN),
        "surprise": item.get("surprise") or "na",
        "published_at": _published(item.get("time")),
        "publish_session": _enum(item.get("session"), _SESSION_EN),
        "in_window": bool(item.get("in_window")),
        "weight": item.get("weight"),
        "reaction": {
            "priced_in": _priced(item),
            "day_excess": None if day is None else round(float(day) / 100, 6),
            "text": _reaction_text(item),
        },
        "evidence": item.get("evidence") or [],
        "sources_count": int(item.get("sources_count") or 1),
    }


def _other(item: dict) -> dict:
    reaction = item.get("reaction") or {}
    day = reaction.get("day_excess")
    row = {
        "event_id": item["event_id"],
        "title": item.get("title") or "",
        "url": item.get("url") or "",
        "source_grade": item.get("source_grade") or "",
        "published_at": _published(item.get("time")),
        "impact": _impact(item.get("direction")),
        "scope": _enum(item.get("scope"), _SCOPE_EN),
        "category": _enum(item.get("category"), _CATEGORY_EN),
        "in_window": bool(item.get("in_window")),
        "magnitude_text": item.get("magnitude_text") or "",
        "sources_count": int(item.get("sources_count") or 1),
        "is_rumor": item.get("basis") == "传闻" or item.get("source_grade") == "E",
    }
    if reaction:
        row["reaction"] = {
            "priced_in": _priced(item),
            "day_excess": None if day is None else round(float(day) / 100, 6),
            "text": _reaction_text(item),
        }
        if item.get("evidence"):
            row["evidence"] = item.get("evidence")
    return row


def _heat(events, stats) -> dict:
    grades = {item.get("source_grade") for item in events}
    hot = (stats.get("heat") or 0) >= 5 and not ({"A", "B"} & grades)
    return {"news_count_pctile_60d": None, "risk_hit": hot}


def _watch(lean: str, has_notice: bool = False) -> list[dict]:
    """has_notice：窗口里已经有公司的 A 级公告。方向不明时，等的是"新的"公告，不是已经出了的那条。"""
    if lean == "bullish":
        text, then_text = "出现 A 级利空公告", "方向转为偏空"
    elif lean == "bearish":
        text, then_text = "出现 A 级利好公告", "方向转为偏多"
    elif has_notice:
        text, then_text = "出现新的 A 级公告", "方向按新公告的利好或利空确定"
    else:
        text, then_text = "出现 A 级公告", "方向按公告的利好或利空确定"
    return [{
        "condition_id": "grade_a_opposite",
        "text": text,
        "then_text": then_text,
        "subscribed": False,
        "alert_id": None,
    }]


def _disclaimer(events) -> str:
    labels = []
    for grade in ("A", "B", "C", "D", "E"):
        if any(item.get("source_grade") == grade for item in events):
            labels.append(_GRADE_LABEL[grade])
    source = " · ".join(labels) if labels else "公开新闻"
    return f"来源：{source}。以上为基于公开新闻的研究分析，不构成投资建议。"


def _pick(events, verdict) -> tuple[list[dict], list[dict]]:
    lean = verdict.get("lean")
    main = "利好" if lean == "偏多" else "利空"
    opposite = "利空" if main == "利好" else "利好"
    pool = [item for item in events if item.get("scope") == "公司" and item.get("in_window")]
    ordered = sorted(pool, key=lambda item: item.get("weight") or 0, reverse=True)
    primary = [item for item in ordered if item.get("direction") == main]
    counter = next((item for item in ordered if item.get("direction") == opposite), None)
    chosen = []
    for item in primary:
        if len(chosen) >= (2 if counter else 3):
            break
        chosen.append(item)
    if counter and len(chosen) < 3:
        chosen.append(counter)
    chosen.sort(key=lambda item: item.get("weight") or 0, reverse=True)
    for rank, item in enumerate(chosen, start=1):
        item["rank"] = rank
        item["role"] = "counter" if item.get("direction") == opposite else "main"
    chosen_ids = {item["event_id"] for item in chosen}
    others = [item for item in events if item["event_id"] not in chosen_ids]
    others.sort(key=lambda item: str(item.get("time") or ""), reverse=True)
    return chosen, others


def _primary(events, verdict) -> dict | None:
    if verdict.get("lean") not in {"偏多", "偏空"}:
        return None
    direction = "利好" if verdict.get("lean") == "偏多" else "利空"
    side = [
        item for item in events
        if item.get("in_window") and item.get("scope") == "公司" and item.get("direction") == direction
    ]
    if not side:
        return None
    return max(side, key=lambda item: item.get("weight") or 0)


def _attach(scored_events, facts, articles) -> list[dict]:
    by_fact = {fact.get("id"): fact for fact in facts}
    by_article = {article.get("id"): article for article in articles}
    rows = []
    for event in scored_events:
        fact = by_fact.get(event.get("fact_id")) or {}
        article = by_article.get(fact.get("article_id")) or {}
        text = f"{article.get('title') or ''} {article.get('summary') or ''}"
        row = dict(event)
        row.update({
            "title": article.get("title") or fact.get("event") or "",
            "url": article.get("url") or "",
            "summary_text": (fact.get("summary") or article.get("summary") or article.get("title") or "")[:40],
            "time": article.get("time") or "",
            "session": article.get("session") or "",
            "source": article.get("source_name") or article.get("source") or "",
            "sources_count": article.get("sources_count") or 1,
            "in_window": bool(article.get("in_window")),
            "marker_date": _marker_day(article, fact, [str(item.get("trade_date") or "") for item in []]),
            "magnitude_text": _magnitude_text(fact, text),
            "surprise": _surprise(event, fact, text),
            "evidence": _evidence(fact, text) + _counter_rows(event),
        })
        rows.append(row)
    return rows


def _marker_day(article, fact, dates) -> str:
    del dates
    moment = str(article.get("time") or fact.get("time") or "")
    day = "".join(ch for ch in moment if ch.isdigit())[:8]
    return day


def _enrich_markers(events, bars):
    dates = [str(row.get("trade_date") or "") for row in bars]
    for item in events:
        day = item.get("marker_date") or ""
        if item.get("session") == "盘后":
            later = [trade_date for trade_date in dates if trade_date > day]
            item["marker_date"] = later[0] if later else (dates[-1] if dates else "")
        elif day and day not in dates:
            later = [trade_date for trade_date in dates if trade_date >= day]
            item["marker_date"] = later[0] if later else day


def _magnitude_text(fact, text: str) -> str:
    amount = _amount(fact.get("amount"), text)
    base = _amount(fact.get("base"), text)
    if amount is None or base in (None, 0):
        return {"大": "量级大", "中": "量级中等", "小": "量级小"}.get(fact.get("magnitude") or "", "")
    ratio = abs(amount / base) * 100
    if "营收" in text or "收入" in text:
        return f"占营收 {round(ratio, 1)}%"
    if "市值" in text:
        return f"占流通市值 {round(ratio, 1)}%"
    return f"占对比基数 {round(ratio, 1)}%"


def _surprise(event, fact, text: str):
    if event.get("category") != "业绩" or "预期" not in text:
        return "na"
    amount = _amount(fact.get("amount"), text)
    base = _amount(fact.get("base"), text)
    if amount is None or base in (None, 0):
        return "na"
    ratio = amount / base - 1
    if ratio >= 0.1:
        return "beat"
    if ratio <= -0.1:
        return "miss"
    return "inline"


def _evidence(fact, text: str) -> list[dict]:
    rows = []
    compact = text.replace(",", "")
    for field in ("amount", "base"):
        raw = str(fact.get(field) or "").strip()
        if not raw:
            continue
        number = re.search(r"-?\d+(?:\.\d+)?", raw.replace(",", ""))
        if not number or number.group(0) not in compact:
            continue
        quote = _quote(compact, number.group(0))
        rows.append({"field": field, "value": raw, "quote": quote})
    return rows


def _quote(text: str, token: str) -> str:
    index = text.find(token)
    if index < 0:
        return token[:30]
    start = max(0, index - 8)
    return text[start:start + 30]


def _amount(value, text: str):
    raw = str(value or "").replace(",", "")
    matched = re.search(r"(-?\d+(?:\.\d+)?)\s*(万亿|亿|万元|万|元)?", raw)
    if not matched or matched.group(1) not in text.replace(",", ""):
        return None
    number = float(matched.group(1))
    unit = matched.group(2) or ""
    scale = {"": 1, "元": 1, "万": 1e4, "万元": 1e4, "亿": 1e8, "万亿": 1e12}
    return number * scale.get(unit, 1)


MAX_ARTICLES = 20   # 一次最多交给模型的新闻条数：窗口内的优先，再看来源等级和时间；超出的记为 over_limit
EXTRACT_VERSION = "news-extract-3"   # 改提示词要升版本，旧缓存随之失效
EXTRACT_TTL = 24 * 3600   # 缓存键里带了新闻内容：同一批新闻直接沿用上一次的判断（方向不会来回变），有新新闻键就变，自然重判
_SKIP_REASONS = {"无关": "irrelevant", "行情播报": "market_recap", "例行披露": "routine", "重复": "duplicate"}


def _judge(articles, model, live: bool, stock: Stock):
    """整批新闻交给模型：合并同一件事的重复报道、挑出无关或例行的、给每个事件打标签，并写一句结论。
    返回（保留的新闻，事件，{ai_text}，清洗统计）；模型不可用、调用失败、或结果完全不可用时返回 None（由规则兜底）。
    去掉的每一条都带原因：irrelevant 无关 / market_recap 行情播报 / routine 例行披露 / duplicate 重复并入 / not_extracted 模型没处理 / over_limit 超出条数。
    权重和方向分数仍由 score_news 按模型给的标签计算。"""
    writer = _writer(model, live)
    if not writer or not articles:
        return None
    ranked = sorted(
        articles,
        key=lambda row: (bool(row.get("in_window")), _GRADE_RANK.get(row.get("source_grade"), 0), str(row.get("time") or "")),
        reverse=True,
    )
    sent, overflow = ranked[:MAX_ARTICLES], ranked[MAX_ARTICLES:]
    user = _user(sent, stock)
    key = None if model is not None else f"news_extract:{stock.ts_code}:{EXTRACT_VERSION}:{hashlib.md5(user.encode()).hexdigest()}"
    data = cache.get(key) if key else None
    if data is None:
        try:
            from app.agents.llm import complete_json

            data = complete_json(writer, _SYSTEM, user, 0)
        except Exception:
            return None
        if key and isinstance(data, dict):
            cache.set(key, data, EXTRACT_TTL)
    if not isinstance(data, dict):
        return None
    facts = _accepted_facts(data.get("events"), sent)
    primary = {str(fact["article_id"]): fact for fact in facts}
    merged = {other: str(fact["article_id"]) for fact in facts for other in fact.get("also_in") or []}
    skipped = _accepted_skips(data.get("skipped"), sent, set(primary) | set(merged))
    if not facts and not skipped:
        return None
    by_id = {str(article.get("id")): article for article in sent}
    kept = []
    for article_id, fact in primary.items():
        row = by_id[article_id]
        row["sources_count"] = 1 + len(fact.get("also_in") or [])
        kept.append(row)
    kept.sort(key=lambda row: str(row.get("time") or ""), reverse=True)
    dropped = []
    for article in sent:
        article_id = str(article.get("id"))
        if article_id in primary:
            continue
        if article_id in merged:
            dropped.append({**_dropped_row(article, "duplicate"), "into": by_id[merged[article_id]].get("title") or ""})
        else:
            dropped.append(_dropped_row(article, skipped.get(article_id, "not_extracted")))
    dropped.extend(_dropped_row(article, "over_limit") for article in overflow)
    stats = {
        "read": len(articles),
        "kept": len(kept),
        "in_window": sum(1 for row in kept if row.get("in_window")),
        "heat": articles[0].get("heat_count") or 0,
        "dropped": dropped,
    }
    return kept, facts, _checked_sentence(data, sent), stats


def _dropped_row(article, reason: str) -> dict:
    return {"title": article.get("title") or "", "reason": reason, "time": article.get("time") or ""}


def _accepted_skips(rows, articles, taken: set) -> dict:
    """模型说"这条不要"：编号要存在、没有同时被抽成事件或并入别的事件，原因要在约定的几种里。返回 {编号: 原因代码}。"""
    ids = {str(article.get("id")) for article in articles}
    skipped = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        key = str(row.get("article_id") or "")
        reason = _SKIP_REASONS.get(str(row.get("reason") or "").strip())
        if key in ids and key not in taken and key not in skipped and reason:
            skipped[key] = reason
    return skipped


def _accepted_facts(rows, articles) -> list[dict]:
    """模型给的事件逐条核对：新闻编号要存在、一条新闻只留一个事件；金额和数字要出现在正文里。
    类别、方向这些标签原样交给 score_news（不在取值范围里的，它会按"无效"处理）。"""
    by_id = {str(article.get("id")): article for article in articles}
    facts, seen = [], set()
    primaries = {str(row.get("article_id") or "") for row in rows or [] if isinstance(row, dict)}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        key = str(row.get("article_id") or "")
        article = by_id.get(key)
        if article is None or key in seen:
            continue
        seen.add(key)
        also = []
        for other in row.get("also_in") or []:   # 同一件事的其他报道：编号要存在，且自己不是别的事件的主稿
            other = str(other)
            if other in by_id and other != key and other not in primaries and other not in seen and other not in also:
                also.append(other)
        seen.update(also)
        text = f"{article.get('title') or ''} {article.get('summary') or ''}".replace(",", "")
        event = str(row.get("event") or article.get("title") or "").strip()
        facts.append({
            "id": f"F{key}",
            "article_id": article.get("id"),
            "event": event[:80],
            "summary": event[:40],
            "category": str(row.get("category") or ""),
            "scope": str(row.get("scope") or ""),
            "basis": str(row.get("basis") or ""),
            "direction": str(row.get("direction") or ""),
            "persistence": str(row.get("persistence") or ""),
            "magnitude": str(row.get("magnitude") or ""),
            "amount": _in_text(row.get("amount"), text),
            "base": _in_text(row.get("base"), text),
            "numbers": [item for item in (str(n) for n in row.get("numbers") or []) if item.replace(",", "") in text],
            "reason": str(row.get("reason") or "")[:60],
            "also_in": also,
        })
    return facts


_MONEY = re.compile(r"\d[\d.]*\s*(?:(?:万亿|亿|万)?元|万亿|亿)(?![户股人手笔家])")
_DATE = re.compile(r"\d\s*[年月日]")


def _in_text(value, text: str) -> str:
    """金额和基数只认带金额单位的数字（亿、万、元），而且要出现在正文里；日期、户数、股数这类对不上口径的当没有。
    （模型曾把"2026年6月30日"当基数，算出占比 246781.8%。）"""
    raw = str(value or "").strip()
    money = _MONEY.search(raw.replace(",", ""))
    if not money or _DATE.search(raw):
        return ""
    number = _NUMBER.search(money.group(0))
    return raw if number and number.group(0) in text else ""


_BANNED = ("买入", "卖出", "建仓", "加仓", "减仓", "目标价", "建议")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _checked_sentence(data, articles) -> dict:
    if not isinstance(data, dict):
        return {}
    text = str(data.get("ai_text") or "").strip()
    cited = [str(item) for item in data.get("cited_article_ids") or []]
    by_id = {str(article.get("id")): article for article in articles}
    if not text or len(text) > 90 or "不过" not in text or any(word in text for word in _BANNED):
        return {}
    if not cited or any(item not in by_id for item in cited):
        return {}
    source = " ".join(f"{by_id[item].get('title') or ''} {by_id[item].get('summary') or ''}" for item in cited)
    if any(number not in source for number in _NUMBER.findall(text)):
        return {}
    return {"ai_text": text}


def _fact_from_article(article: dict, index: int) -> dict:
    text = f"{article.get('title') or ''} {article.get('summary') or ''}"
    return {
        "id": f"F{article.get('id') or index}",
        "event": article.get("title") or "",
        "article_id": article.get("id"),
        "category": _category(text, article),
        "scope": "行业" if article.get("relevance") == "中" else "公司",
        "basis": _basis(article),
        "direction": _direction(article.get("title") or "") if _direction(article.get("title") or "") != "中性" else _direction(text),
        "persistence": "one_off",
        "magnitude": "小",
        "amount": _first_amount(text),
        "base": _second_amount(text),
        "summary": (article.get("summary") or article.get("title") or "")[:40],
    }


def _direction(text: str) -> str:
    positive = ("回购", "预增", "增长", "上调", "中标", "超预期", "利好", "增持", "提价", "涨价")
    negative = ("减持", "预减", "下降", "下调", "处罚", "立案", "亏损", "利空", "诉讼", "降价")
    if any(word in text for word in positive) and not any(word in text for word in negative):
        return "利好"
    if any(word in text for word in negative) and not any(word in text for word in positive):
        return "利空"
    return "中性"


def _category(text: str, article: dict) -> str:
    if any(word in text for word in ("处罚", "立案", "诉讼", "问询")):
        return "风险与治理"
    if any(word in text for word in ("回购", "减持", "增持", "定增")):
        return "资本"
    if any(word in text for word in ("合同", "订单", "中标", "提价", "涨价", "降价")):
        return "经营"
    if any(word in text for word in ("业绩", "财报", "净利", "营收", "预告", "快报", "三季报", "季报")):
        return "业绩"
    if article.get("relevance") == "中":
        return "政策与行业"
    return "观点"


def _basis(article: dict) -> str:
    grade = article.get("source_grade") or "E"
    text = f"{article.get('title') or ''} {article.get('summary') or ''}"
    if grade == "E":
        return "传闻"
    if grade == "A":
        return "事实"
    if grade == "D" or any(word in text for word in _COMMENTARY):
        return "预测"
    return "事实"


def _first_amount(text: str) -> str:
    matched = re.search(r"\d+(?:\.\d+)?\s*(?:万亿|亿元|亿|万元|万)", text)
    return matched.group(0) if matched else ""


def _second_amount(text: str) -> str:
    found = re.findall(r"\d+(?:\.\d+)?\s*(?:万亿|亿元|亿|万元|万)", text)
    return found[1] if len(found) > 1 else ""


def _copy(events, key_events, others, model_copy, stock, days) -> tuple[str, str | None]:
    """返回（规则模板写的一句话，模型写的一句话或 None）。"""
    template = _template(key_events, others, stock, days)
    return template, (model_copy or {}).get("ai_text") or None


def _template(key_events, others, stock, days) -> str:
    name = stock.name or "该公司"
    if not key_events:
        recent = [item for item in others if item.get("scope") == "公司" and item.get("in_window")]
        stale = [item for item in others if item.get("scope") == "公司" and not item.get("in_window")]
        if recent:
            latest = _latest_company(recent)
            text = (
                f"近 {days} 天有{name}的{_event_kind(latest.get('title') or '')}，"
                f"{_impact_phrase(latest.get('direction'))}。不过这还不足以单独定方向。"
            )
            return text[:90]
        if stale:
            latest = _latest_company(stale)
            text = (
                f"近 {days} 天没有{name}的新公告。"
                f"不过最近一条是 {_md(latest.get('time'))} 的{_event_kind(latest.get('title') or '')}，"
                f"{_impact_phrase(latest.get('direction'))}。"
            )
            return text[:90]
        industry = next((item for item in others if item.get("scope") == "行业"), None)
        if industry:
            text = (
                f"近 {days} 天主要是行业层面的消息（{_event_kind(industry.get('title') or '')}），"
                f"没有{name}自己的公告。不过这还不是公司公告。"
            )
        else:
            text = f"近 {days} 天没有{name}自己的公告。不过还要等公司自己的公告。"
        return text[:90]
    lead = key_events[0]
    counter = next((item for item in key_events if item.get("role") == "counter"), None)
    official = any(item.get("source_grade") in {"A", "B"} for item in key_events)
    kind = _event_kind(lead.get("title") or "")
    if official:
        body = f"近 {days} 天有{name}的{kind}。"
    else:
        kind_name = "经营" if lead.get("category") == "经营" else "公司"
        body = f"近 {days} 天主要是{name}的{kind_name}消息（{kind}），没有{name}自己的公告。"
    if counter:
        tail = f"不过同时有{_clause(counter.get('title'))}的报道。"
    else:
        tail = "不过还没有看到明确的反方报道。"
    return (body + tail)[:90]


def _latest_company(events: list[dict]) -> dict:
    def rank(item: dict) -> tuple:
        kind = _event_kind(item.get("title") or "")
        specific = 0 if kind in {"董事会决议", "公司公告"} else 1
        return (str(item.get("time") or ""), specific)

    return max(events, key=rank)


def _event_kind(title: str) -> str:
    if "财务总监" in title:
        return "财务总监变更"
    if "回购" in title and "进展" in title:
        return "回购进展"
    if "回购" in title:
        return "回购"
    if "减持" in title:
        return "减持"
    if "增持" in title:
        return "增持"
    if "提价" in title or "涨价" in title:
        return "提价"
    if any(word in title for word in ("预告", "三季报", "业绩")):
        return "业绩预告" if "预告" in title or "三季报" in title else "业绩"
    if "立案" in title or "处罚" in title:
        return "风险公告"
    if "董事会" in title:
        return "董事会决议"
    return "公司公告"


def _impact_phrase(direction: str | None) -> str:
    if direction == "利好":
        return "影响偏多"
    if direction == "利空":
        return "影响偏空"
    return "影响中性"


def _md(value) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) < 8:
        return ""
    return f"{int(digits[4:6])}-{int(digits[6:8])}"


def _clause(title: str) -> str:
    text = re.split(r"[丨|？?，,。]", title or "")[0].strip()
    return text[:12] or "这条消息"


def _market(stock, bars, sector):
    if bars is not None or sector is not None:
        return _score_rows(bars or []), _score_rows(sector or [], change_key="pct_change")
    try:
        from app.services.volume_flow.align import load_aligned

        aligned = load_aligned(stock)
    except Exception:
        return [], []
    chosen = (aligned.get("bars") or [])[-40:]
    sector_rows = ((aligned.get("sector") or {}).get("rows") or [])[-40:]
    return _from_aligned(chosen), _from_aligned(sector_rows, change_key="pct_change")


def _from_aligned(rows, change_key="pct_chg") -> list[dict]:
    converted = []
    for row in rows:
        converted.append({
            "trade_date": str(row.get("date") or "").replace("-", ""),
            "close": row.get("close"),
            "pct_chg": row.get("chg"),
            "pct_change": row.get("chg"),
            "vol": row.get("volume"),
        })
    if change_key == "pct_change":
        return converted
    return converted


def _score_rows(rows, change_key="pct_chg") -> list[dict]:
    converted = []
    for row in rows:
        trade_date = str(row.get("trade_date") or row.get("date") or "")
        trade_date = "".join(ch for ch in trade_date if ch.isdigit())[:8]
        change = row.get(change_key)
        if change is None:
            change = row.get("chg")
        converted.append({
            "trade_date": trade_date,
            "close": row.get("close"),
            "pct_chg": change,
            "pct_change": change,
            "vol": row.get("vol") if row.get("vol") is not None else row.get("volume"),
        })
    return converted


def _load_news(stock: Stock) -> list[dict]:
    from app.services.news import fetch_announcements, get_news

    items = list((get_news(stock) or {}).get("items") or [])
    try:
        items.extend(fetch_announcements(stock))
    except Exception:
        pass
    return items


def _with_today(bars: list[dict], today: date, quote: dict | None) -> list[dict]:
    if not quote or not bars or today.weekday() >= 5:
        return bars
    today_digits = today.strftime("%Y%m%d")
    if str(bars[-1].get("trade_date") or "") >= today_digits:
        return bars
    return [*bars, {
        "trade_date": today_digits,
        "close": quote["price"],
        "pct_chg": quote["pct"],
        "pct_change": quote["pct"],
        "vol": None,
        "intraday": True,
    }]


def _realtime(stock: Stock) -> dict | None:
    symbol = _tencent_symbol(stock)
    if not symbol:
        return None
    try:
        import requests

        session = requests.Session()
        session.trust_env = False
        response = session.get(f"http://qt.gtimg.cn/q={symbol}", timeout=(2, 4))
        response.raise_for_status()
        text = response.content.decode("gbk", "replace")
    except Exception:
        return None
    parts = text.split("~")
    if len(parts) < 5:
        return None
    try:
        price = float(parts[3])
        prev = float(parts[4])
    except ValueError:
        return None
    if price <= 0 or prev <= 0:
        return None
    return {"price": price, "prev": prev, "pct": round((price / prev - 1) * 100, 4)}


def _tencent_symbol(stock: Stock) -> str:
    market = (stock.ts_code or "").split(".")[-1].upper()
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(market, "")
    if not prefix or not stock.code:
        return ""
    return prefix + stock.code


def _writer(model, live: bool):
    """model=False 关闭；传入 model 就用它；否则只有真实请求（不是注入数据的测试）且配了密钥才调用。"""
    if model is False:
        return None
    if model is not None:
        return model
    if not live or not config.LLM_API_KEY:
        return None
    try:
        from app.agents.llm import get_chat_model

        return get_chat_model()
    except Exception:
        return None


def _excess(bar, sector_map):
    stock = bar.get("pct_chg")
    sector = sector_map.get(str(bar.get("trade_date")))
    if not _number(stock) or not _number(sector):
        return None
    return float(stock) - float(sector)


def _reaction_text(item: dict) -> str:
    reaction = item.get("reaction") or {}
    day = reaction.get("day_excess")
    base = "当日涨跌还没法对应" if day is None else f"当日 {day:+.2f}%"
    note = next((text for text in reaction.get("adjustments") or [] if "下跌" in text or "未被" in text), "")
    if note:
        return f"{base}，{note}"
    return base


def _counter_rows(event: dict) -> list[dict]:
    reaction = event.get("reaction") or {}
    notes = [text for text in reaction.get("adjustments") or [] if "下跌" in text or "未被" in text]
    if not notes:
        return []
    day = reaction.get("day_excess")
    if day is not None and float(day) < 0:
        value = f"当日跑输板块 {abs(float(day)):.2f}%"
    elif day is not None and float(day) > 0:
        value = f"当日跑赢板块 {float(day):.2f}%"
    else:
        value = notes[0]
    return [{"field": "counter", "value": value, "quote": notes[0]}]


def _article_in_window(fact: dict, articles: list[dict]) -> bool:
    article = next((item for item in articles if item.get("id") == fact.get("article_id")), None)
    return bool(article and article.get("in_window"))


def _fill_buyback(events: list[dict], bars: list[dict]) -> None:
    closes = {str(row.get("trade_date") or ""): row.get("close") for row in bars or []}
    for item in events:
        title = item.get("title") or ""
        if "回购" not in title or "进展" not in title:
            continue
        parsed = _parse_buyback(_notice_plain(item.get("url") or ""))
        if not parsed:
            continue
        close = closes.get(item.get("marker_date") or "")
        item["magnitude_text"] = _buyback_line(parsed, close)


def _notice_plain(url: str) -> str:
    matched = re.search(r"(AN\d+)", url or "")
    if not matched:
        return ""
    art = matched.group(1)
    key = f"news:ann-body:{art}"
    cached = cache.get(key)
    if isinstance(cached, str):
        return cached
    text = ""
    try:
        from app.tools.http import get_json

        payload = get_json(
            "https://np-cnotice-stock.eastmoney.com/api/content/ann",
            {"art_code": art, "client_source": "web", "page_index": 1},
            headers={"Referer": "https://data.eastmoney.com/"},
        )
        text = str(((payload or {}).get("data") or {}).get("notice_content") or "")
    except Exception:
        text = ""
    plain = re.sub(r"<[^>]+>", "", text)
    if plain:
        cache.set(key, plain, config.CACHE_TTL["news"])
    return plain


def _parse_buyback(text: str) -> dict | None:
    plain = re.sub(r"\s+", "", text or "")
    amount = re.search(r"成交总金额为([\d,.]+)元", plain)
    if not amount:
        return None
    shares = re.search(r"回购公司A股股份([\d,]+)股", plain)
    ratio = re.search(r"占公司总股本的([\d.]+)%", plain)
    return {
        "amount": float(amount.group(1).replace(",", "")),
        "shares": float(shares.group(1).replace(",", "")) if shares else None,
        "ratio": float(ratio.group(1)) if ratio else None,
    }


def _buyback_line(parsed: dict, close) -> str:
    yi = parsed["amount"] / 1e8
    if parsed.get("shares") and parsed.get("ratio") and _number(close) and float(close) > 0:
        total = parsed["shares"] / (parsed["ratio"] / 100)
        pct = parsed["amount"] / (float(close) * total) * 100
        return f"已回购 {yi:.2f} 亿，占市值 {pct:.2f}%"
    if parsed.get("ratio") is not None:
        return f"已回购 {yi:.2f} 亿，占总股本 {parsed['ratio']:.2f}%"
    return f"已回购 {yi:.2f} 亿"


def _px(value) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


_SYSTEM = (
    "你是新闻事件抽取员。给你一只股票和一批新闻，由你完成筛选、合并和判断。只根据给定新闻，不要补充新闻里没有的信息，只输出 JSON："
    "{\"events\": [{\"article_id\": \"\", \"also_in\": [], \"event\": \"\", \"category\": \"\", \"scope\": \"\", \"basis\": \"\", "
    "\"direction\": \"\", \"persistence\": \"\", \"magnitude\": \"\", \"amount\": \"\", \"base\": \"\", "
    "\"numbers\": [], \"reason\": \"\"}], \"skipped\": [{\"article_id\": \"\", \"reason\": \"\"}], "
    "\"ai_text\": \"\", \"cited_article_ids\": []}。\n"
    "【筛选与合并】每条新闻要么进 events，要么进 skipped，要么作为重复报道写进某个事件的 also_in，不能漏掉。\n"
    "- 报道同一件事的新闻（同一份公告被多家转载、标题略有不同的重复稿）合并成一个事件：article_id 选最权威的一条（公告 > 官方 > 媒体 > 研报 > 热榜），其余的编号写进 also_in。\n"
    "- skipped 的 reason 只能是：无关（和这只股票、它所在行业都无关，包括综述、汇总、晚间新闻精选、只是顺带提到这家公司的稿子）、"
    "行情播报（只讲股价涨跌、资金流向，没有新信息）、例行披露（纯程序性文件，如核查意见、法律意见书、会议通知，没有实质信息）。\n"
    "- 判断是否相关要看内容，不要只看标题有没有公司名：标题只写“AI龙头”“该公司”，但内容讲的就是这家公司的，要抽取，不要当无关。\n"
    "【events】一个事件一个对象，article_id 必须是输入里的 id。event 用一句话写清发生了什么。\n"
    "category 只能是：业绩、经营、资本、风险与治理、政策与行业、宏观与市场、观点、传闻与澄清。"
    "scope 只能是：公司（这条新闻的主角就是这家公司）、行业、市场。"
    "basis 只能是：事实（公告或官方披露）、计划、预测、传闻；研报、评级、目标价写预测，category 写观点；没有公告印证的传闻写传闻。\n"
    "direction 只能是：利好、利空、中性，指对公司未来盈利或估值的影响方向，不是股价已经涨跌。"
    "先看数字的方向：净利润、营收同比增长是利好，同比减少或下滑是利空；几项指标方向相反或幅度很小，写中性。"
    "董事会决议、薪酬方案、股东户数这类没有新信息的，写中性，magnitude 写小。\n"
    "amount：这条新闻讲的核心金额，必须带单位，如“30亿元”。base：和 amount 同口径、可以直接比较的基数，如上年同期、公司营收、总市值，同样必须是带单位的金额。"
    "找不到同口径的基数就留空，不要用日期、户数、股数或别的数字凑。numbers 只写正文里出现的数字，正文没有的不要补。"
    "magnitude 写大、中、小：有可比的 amount 和 base 时按占比判断，没有就写小。persistence 只能是：一次性、可持续。reason 用一句话说明为什么是这个方向。\n"
    "示例（只展示写法，别照搬）：\n"
    "- “公司拟回购不超过30亿元股份” → category 资本，scope 公司，basis 计划，direction 利好，amount “30亿元”，base 留空，magnitude 小。\n"
    "- “前三季度净利润644.67亿元，同比减少0.25%；营收同比增长1.96%” → category 业绩，scope 公司，basis 事实，direction 中性（营收增、净利微降，方向相反），amount “644.67亿元”，base 留空，magnitude 小。\n"
    "- “董事会审议通过高管薪酬兑现方案” → category 风险与治理，scope 公司，basis 事实，direction 中性，magnitude 小。\n"
    "【ai_text】面向普通投资者的一句话结论，不超过 90 个字：先说整体是偏多、偏空还是看不出方向，再写最要紧的依据。必须包含“不过”：有反方报道或不确定的地方就写出来；"
    "没有就写“不过目前没有看到明确的反方报道”，不要为了凑转折编造细节。只能使用新闻里出现的数字，不要自己计算，不要出现买入、卖出、建仓等建议。"
    "cited_article_ids 填 ai_text 用到的新闻 id，必须来自输入。"
)


def _user(articles, stock: Stock) -> str:
    import json
    brief = [{
        "id": article.get("id"),
        "title": article.get("title"),
        "summary": str(article.get("summary") or "")[:400],
        "source": article.get("source_name") or article.get("source"),
        "source_grade": article.get("source_grade"),
        "time": article.get("time"),
    } for article in articles]
    return json.dumps({"stock": {"name": stock.name, "code": stock.code}, "articles": brief}, ensure_ascii=False)


def _published(value):
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) >= 19 and text[10] == " ":
        text = text[:19]
    for fmt, size in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            parsed = datetime.strptime(text[:size], fmt)
            return parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
        except ValueError:
            continue
    return None


def _enum(value, mapping: dict) -> str:
    text = str(value or "")
    if text in mapping:
        return mapping[text]
    if text in mapping.values():
        return text
    return ""


def _impact(direction) -> str:
    return _IMPACT.get(direction or "", "neutral")


def _persistence(value) -> str:
    text = str(value or "")
    if text in {"lasting", "持续", "长期"} or "持续" in text or "长期" in text:
        return "lasting"
    return "one_off"


def _iso(value) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())[:8]
    if len(digits) < 8:
        return ""
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _number(value) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _days(value) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        return 3
    if days < 1:
        return 3
    return min(days, 20)


def _claim(key: str) -> bool:
    with _inflight_lock:
        if key in _inflight:
            return False
        _inflight.add(key)
        return True


def _release(key: str) -> None:
    with _inflight_lock:
        _inflight.discard(key)
