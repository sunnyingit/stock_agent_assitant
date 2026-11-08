"""综合判断给页面的第一眼：headline、今天涨跌、两边结论和风险、要盯的价位。

事实的 value / text 只从各维度体检事实表（review.facts 的同一套）原样抄出。
模型句子不另造数字。今天这根涨跌由代码归类，不进 headline.facts。
"""

from __future__ import annotations

import re

from app.synthesis.calendar import now_cn

SIDE_ORDER = ("fundamental", "finance", "volume_flow", "news", "sector")
SIDE_LABEL = {
    "fundamental": "基本面",
    "finance": "财报",
    "volume_flow": "资金流",
    "news": "新闻",
    "sector": "板块",
}
PICKS = {
    "fundamental": ("fd.value", "fd.pctile_5y", "fd.peer_median"),
    "finance": ("ff.growth.q_rev_yoy", "ff.growth.q_ded_yoy", "ff.margin.cycle", "ff.margin.gm"),
    "volume_flow": ("fr.big_net_20d", "fr.net_rate_20d", "fr.trend", "fr.ret_20d"),
    "news": ("news.main",),
    "sector": ("sp.p.stage", "sp.p.stage_days", "sp.p.vs_index", "sp.p.stock_vs_sector"),
}
_STATUS = {"bullish": ("support", "偏多"), "bearish": ("warn", "偏空")}
_TODAY_MIN = 0.01
_DAYS = re.compile(r"还有\s*(\d+)\s*个交易日")


def compose(payloads: dict, aligned: dict | None, stock, now, page: dict, source: str = "rule", sides: dict | None = None, extra_facts: list | None = None) -> dict:
    """用这次已经取到的详情，加上页面上的方向、价位、事件，装出第一眼。

    bull / bear 用看多、看空 Agent 校验通过后的论据和风险。事实的 value、text 按引用编号从事实表原样抄。
    """
    catalog = _catalog(payloads or {}, aligned, stock, now)
    for row in extra_facts or []:
        if row.get("id"):
            catalog.setdefault(row["id"], row)
    leans = _leans(payloads or {})
    levels = _levels(page, _close(aligned))
    events = _events(page)
    direction = ((page.get("direction") or {}).get("lean")) or "unclear"
    bull = _agent_case((sides or {}).get("bull"), catalog)
    bear = _agent_case((sides or {}).get("bear"), catalog)
    return {
        "headline": {
            "source": source,
            "status": _STATUS.get(direction, ("neutral", "不明"))[0],
            "status_text": _STATUS.get(direction, ("neutral", "不明"))[1],
            "text": _headline_text(page),
            "facts": _headline_facts(direction, bull, bear),
        },
        "today": _today(aligned, payloads.get("sector"), payloads.get("news")),
        "sides": _sides(leans),
        "bull": bull,
        "bear": bear,
        "watch": _watch(levels, events),
        "authored": "agent" if sides else "none",
    }


def review_rows(payloads: dict, aligned, stock, now) -> list[dict]:
    return list(_catalog(payloads or {}, aligned, stock, now).values())


def compose_live(stock, page: dict, now=None, source: str = "rule") -> dict:
    """存档里还没有这套结构时，用当前详情现装一份。"""
    from app.synthesis.producer.collect import collect

    now = now or now_cn()
    bundle = collect(stock, now)
    return compose(bundle.payloads, bundle.aligned, stock, now, page, source)


def _catalog(payloads: dict, aligned, stock, now) -> dict:
    rows: dict[str, dict] = {}
    _take(rows, _flow_facts, payloads.get("volume_flow"), aligned, stock, now)
    _take(rows, _finance_facts, payloads.get("finance"))
    _take(rows, _fund_facts, payloads.get("fundamental"))
    _take(rows, _sector_facts, payloads.get("sector"))
    for row in _news_facts(payloads.get("news")):
        rows[row["id"]] = row
    return rows


def _take(rows: dict, builder, *args) -> None:
    try:
        for row in builder(*args) or []:
            if row.get("id"):
                rows[row["id"]] = row
    except Exception:
        return


def _flow_facts(payload, aligned, stock, now):
    if not payload or not aligned:
        return []
    from app.flow_review import facts as sheet

    built, _derived = sheet.build(payload, aligned, {"missing": []}, stock, now)
    return sheet.public(built)


def _finance_facts(payload):
    metrics = ((payload or {}).get("inputs") or {}).get("metrics")
    if not metrics:
        return []
    from app.finance_review import facts as sheet

    built, _derived = sheet.build(metrics)
    return sheet.public(built)


def _fund_facts(payload):
    if not payload or (payload.get("meta") or {}).get("state") != "ok":
        return []
    from app.fundamental_review import facts as sheet

    built, _derived = sheet.build(payload)
    return sheet.public(built)


def _sector_facts(payload):
    if not payload or not (payload.get("sectors") or {}).get("primary"):
        return []
    from app.sector_review import facts as sheet

    built, _derived = sheet.build(payload)
    return sheet.public(built)


def _news_facts(payload):
    main = next((item for item in (payload or {}).get("key_events") or [] if item.get("role") == "main"), None)
    if not main:
        return []
    title = str(main.get("title") or "").strip() or "主要事件"
    return [{"id": "news.main", "label": "主要事件", "text": title, "value": main.get("impact") or ""}]


def _leans(payloads: dict) -> dict:
    leans = {}
    for key in SIDE_ORDER:
        verdict = (payloads.get(key) or {}).get("verdict") or {}
        lean = verdict.get("lean") or ""
        leans[key] = lean if lean in ("bullish", "bearish") else "unclear"
    return leans


def _pick(leans: dict, want: str, catalog: dict, limit: int = 3) -> list[dict]:
    chosen = []
    for key in SIDE_ORDER:
        if leans.get(key) != want:
            continue
        for fact_id in PICKS[key]:
            row = catalog.get(fact_id)
            if row is None:
                continue
            chosen.append(_slim(row))
            break
        if len(chosen) >= limit:
            break
    return chosen


def _agent_case(side: dict | None, catalog: dict) -> dict:
    """一边 Agent 的结论和风险。没有跑成，或被校验丢掉，就留空，不用维度方向去凑。"""
    if not side or side.get("failed"):
        return {"text": "", "facts": [], "risk": None}
    points = side.get("points") or []
    risk = side.get("risk") if isinstance(side.get("risk"), dict) else None
    risk_out = None
    if risk and str(risk.get("text") or "").strip():
        risk_out = {"text": str(risk["text"]).strip(), "facts": _cited([risk], catalog)}
    return {
        "text": "；".join(item["claim"] for item in points if item.get("claim")),
        "facts": _cited(points, catalog),
        "risk": risk_out,
    }


def _cited(rows: list[dict], catalog: dict) -> list[dict]:
    chosen, seen = [], set()
    for row in rows:
        for fact_id in row.get("cites") or []:
            if fact_id in seen or fact_id not in catalog:
                continue
            seen.add(fact_id)
            chosen.append(_slim(catalog[fact_id]))
    return chosen


def _headline_facts(direction: str, bull: dict, bear: dict) -> list[dict]:
    if direction == "bullish":
        return bull["facts"][:5]
    if direction == "bearish":
        return bear["facts"][:5]
    return []


def _slim(row: dict) -> dict:
    return {"id": row["id"], "label": row.get("label") or row["id"], "text": row.get("text") or "", "value": row.get("value")}


def _case(own: list[dict], other: list[dict], level: dict | None, name: str, ending: str) -> dict:
    if not own and not other and not level:
        return {"text": "", "facts": [], "risk": None}
    risk_bits = [_phrase(other[:2])]
    if level and level.get("price") is not None:
        cond = level.get("condition") or "收盘价碰到"
        risk_bits.append(f"{cond} {level['price']:.2f}，{name}就{ending}")
    risk_text = "。".join(bit for bit in risk_bits if bit)
    return {
        "text": _phrase(own),
        "facts": own,
        "risk": None if not risk_text else {"text": risk_text, "facts": other[:2]},
    }


def _phrase(facts: list[dict]) -> str:
    return "；".join(f"{item['label']} {item['text']}" for item in facts if item.get("text"))


def _headline_text(page: dict) -> str:
    raw = str(page.get("headline") or "")
    if "：" in raw:
        return raw.split("：", 1)[1]
    return raw


def _sides(leans: dict) -> list[dict]:
    rows = []
    for key in SIDE_ORDER:
        status, text = _STATUS.get(leans.get(key), ("neutral", "不明"))
        rows.append({"key": key, "label": SIDE_LABEL[key], "status": status, "status_text": text})
    return rows


def _today(aligned, sector, news) -> dict | None:
    bars = (aligned or {}).get("bars") or []
    if not bars or bars[-1].get("chg") is None:
        return None
    ret = float(bars[-1]["chg"]) / 100
    if abs(ret) < _TODAY_MIN:
        return None
    sector_ret, index_ret = _sector_day(sector)
    hit = _same_day_news(news, bars[-1].get("date"))
    kind = _kind(ret, sector_ret, index_ret, hit is not None)
    return {
        "kind": kind,
        "ret": round(ret, 4),
        "sector_ret": None if sector_ret is None else round(sector_ret, 4),
        "index_ret": None if index_ret is None else round(index_ret, 4),
        "text": _today_text(kind, ret, sector_ret, index_ret, hit),
    }


def _sector_day(payload) -> tuple[float | None, float | None]:
    lines = (((payload or {}).get("sectors") or {}).get("primary") or {}).get("strength", {}).get("lines") or {}
    return _daily(lines.get("sector") or []), _daily(lines.get("index") or [])


def _daily(series: list) -> float | None:
    if len(series) < 2 or series[-1] is None or series[-2] is None:
        return None
    prev = 1 + float(series[-2])
    if prev == 0:
        return None
    return (1 + float(series[-1])) / prev - 1


def _same_day_news(payload, day) -> dict | None:
    if not day:
        return None
    for item in (payload or {}).get("key_events") or []:
        published = str(item.get("published_at") or "")[:10]
        if published == day and item.get("scope") == "company" and item.get("impact") in ("positive", "negative"):
            return item
    return None


def _kind(ret: float, sector_ret, index_ret, has_news: bool) -> str:
    if has_news:
        return "news"
    if sector_ret is not None and ret * sector_ret > 0 and (index_ret is None or abs(ret - sector_ret) <= abs(ret - index_ret)):
        return "sector"
    if index_ret is not None and ret * index_ret > 0 and (sector_ret is None or abs(ret - index_ret) < abs(ret - sector_ret)):
        return "index"
    return "stock"


def _today_text(kind: str, ret: float, sector_ret, index_ret, news: dict | None) -> str:
    head = f"今天 {ret * 100:+.1f}%"
    if kind == "news":
        title = str((news or {}).get("title") or "公司消息")
        return f"{head}，对应消息：{title}"
    bits = []
    if sector_ret is not None:
        bits.append(f"板块 {sector_ret * 100:+.1f}%")
    if index_ret is not None:
        bits.append(f"大盘 {index_ret * 100:+.1f}%")
    tail = {"sector": "属于跟板块", "index": "属于跟大盘"}.get(kind, "属于自己的事，没有对上新闻")
    mid = "、".join(bits)
    return f"{head}，{mid}，没有对应新闻，{tail}" if mid else f"{head}，没有对应新闻，{tail}"


def _close(aligned) -> float | None:
    bars = (aligned or {}).get("bars") or []
    close = bars[-1].get("close") if bars else None
    return float(close) if close else None


def _levels(page: dict, close: float | None) -> dict:
    out = {}
    for side in ("down", "up"):
        level = (page.get("levels") or {}).get(side)
        if not level or level.get("price") is None:
            continue
        price = float(level["price"])
        out[side] = {
            "side": side,
            "price": round(price, 2),
            "name": level.get("line") or "",
            "dist": None if not close else round(price / close - 1, 4),
            "condition": level.get("condition") or "",
            "consequence": level.get("consequence") or "",
        }
    return out


def _events(page: dict) -> list[dict]:
    rows = []
    for item in page.get("watch") or []:
        text = str(item.get("text") or "")
        matched = _DAYS.search(text)
        if "财报" in text or "披露" in text:
            dim = "finance"
        elif "板块" in text:
            dim = "sector"
        else:
            dim = "fundamental"
        rows.append({
            "id": item.get("id"),
            "dim": dim,
            "days_left": int(matched.group(1)) if matched else None,
            "text": text,
        })
    return rows


def _watch(levels: dict, events: list[dict]) -> dict:
    bits = []
    down, up = levels.get("down"), levels.get("up")
    if down:
        bits.append(f"{down['condition'] or '收盘价跌破'} {down['price']:.2f}")
    if up:
        bits.append(f"{up['condition'] or '收盘价站上'} {up['price']:.2f}")
    if events:
        bits.append(events[0]["text"])
    text = ""
    if bits:
        text = "，或".join(bits[:2]) + "，这个判断就要更新" if len(bits) > 1 else bits[0] + "，这个判断就要更新"
    return {"text": text, "levels": [levels[side] for side in ("down", "up") if side in levels], "events": events}
