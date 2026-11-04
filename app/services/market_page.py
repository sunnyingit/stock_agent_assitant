"""市场页：把市场切片收成设计稿上的结论、资金、好不好做、宏观、日历、板块和自选。

首屏摘要和右侧依据各 tab 都从同一份切片包里算，切片包 5 分钟内只取一次。
国家队没有单独的日频接口，这里不编造申购金额；打开这一页也不给自选排队诊断。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.services import market_rules as rules
from app.services import market_changes, market_emotion, market_intraday, market_notify, market_sector, market_team
from app.services.market_store import get_store
from app.services.market_tape import build, judge_pulse
from app.tools import cache

_ZONE = ZoneInfo("Asia/Shanghai")
_BUNDLE_TTL = 300
_BUNDLE_LOCK = threading.Lock()
PARTS = (
    "turnover", "etf", "margin", "south", "shibor", "supply", "funds", "breadth", "limits", "index", "sw",
    "macro", "valuation", "fx", "us", "hk", "ipo", "calendar", "unlock", "margin_industry",
)
TABS = ("fund", "team", "sector", "mood", "cal", "macro")
_WEEKDAY = "一二三四五六日"


# ---------------------------------------------------------------- 文案小工具
def _pct(value, digits: int = 1) -> str:
    if value is None:
        return "没有"
    return f"{value * 100:+.{digits}f}%"


def _yi(value, signed: bool = False) -> str:
    if value is None:
        return "没有"
    body = f"{abs(value) / 10000:.2f} 万亿" if abs(value) >= 10000 else f"{abs(value):.{0 if abs(value) >= 100 else 1}f} 亿"
    if not signed:
        return body if value >= 0 else "−" + body
    return ("+" if value > 0 else "−" if value < 0 else "") + body


def _plain(value, digits: int = 2) -> str:
    return "没有" if value is None else f"{value:.{digits}f}"


def _delta(value, digits: int = 1) -> str:
    return "没有" if value is None else f"{value:+.{digits}f}".replace("-", "−")


def _word(pctile) -> str:
    if pctile is None:
        return ""
    return "很低" if pctile < 0.2 else "偏低" if pctile < 0.4 else "中等" if pctile < 0.6 else "偏高" if pctile < 0.8 else "很高"


def _pctile_info(label: str, pctile) -> dict | None:
    if pctile is None:
        return None
    return {"label": label, "value": pctile, "word": _word(pctile), "text": f"{round(pctile * 100)}% 分位"}


def _age(day: str, today: date, previous: str = "") -> str:
    if not day:
        return ""
    if day == today.isoformat():
        return "今日"
    if previous and day == previous:
        return "昨日"
    return day[5:]


def _previous_open(parts: dict, today: date) -> str:
    return parts.get("_prev") or ""


def _stat(label: str, value: str, on: bool = False) -> dict:
    return {"label": label, "value": value, "on": on}


# ---------------------------------------------------------------- 取数
def _fetch(name: str, day: date) -> tuple[str, dict]:
    try:
        return name, build(name, day)
    except Exception:
        return name, {"metrics": {}, "text": "", "facts": [], "unavailable": ["没有取到"]}


def bundle(today: date | None = None) -> dict:
    """所有切片的 metrics，外加板块三层。5 分钟内共用。"""
    day = today or datetime.now(_ZONE).date()
    key = f"market_page:bundle:{day.isoformat()}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    with _BUNDLE_LOCK:
        cached = cache.get(key)
        if cached is not None:
            return cached
        with ThreadPoolExecutor(max_workers=4) as pool:
            docs = dict(pool.map(lambda name: _fetch(name, day), PARTS))
            sector_future = pool.submit(_sector, day)
            extra = {
                "team": pool.submit(_safe, lambda: market_team.build(day), {"ok": False}),
                "announce": pool.submit(_safe, market_team.announcements, {"items": [], "ok": False}),
                "emotion": pool.submit(_safe, lambda: market_emotion.build(day), {"ok": False, "days": []}),
                "catalysts": pool.submit(_safe, market_sector.catalysts, {}),
            }
            result = {name: doc.get("metrics") or {} for name, doc in docs.items()}
            result.update({name: future.result() for name, future in extra.items()})
            result["_text"] = {name: doc.get("text") or "" for name, doc in docs.items()}
            result["sectors"] = sector_future.result()
        result["watch"] = _watch(result["sectors"], day)
        result["_day"] = day.isoformat()
        result["_prev"] = _prev_trade_day(day)
        cache.set(key, result, _BUNDLE_TTL)
        return result


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


def _prev_trade_day(day: date) -> str:
    from app.services import market_tape

    try:
        before = [item for item in market_tape.open_dates(day, 3) if item < day.strftime("%Y%m%d")]
    except Exception:
        return ""
    return market_tape.iso(before[-1]) if before else ""


def _sector(day: date) -> dict:
    try:
        return market_sector.build(day)
    except Exception:
        return {"rows": [], "date": "", "windows": list(market_sector.WINDOWS), "conclusion_days": market_sector.CONCLUSION_DAYS}


def warm() -> None:
    """后台预热：页面第一次打开前把切片包取好。"""
    threading.Thread(target=lambda: bundle(), daemon=True).start()


def _watch(sectors: dict, day: date) -> list[dict]:
    try:
        from app.services import market_tape
        from app.synthesis import watchlist, worker
        from app.synthesis.service import current_mode

        store = worker.get_store()
        mode = current_mode()
        verdict_by_l1 = {row["industry"]: row["verdict"] for row in sectors.get("rows") or [] if row.get("industry")}
        sector_by_l1 = {row["industry"]: row["name"] for row in sectors.get("rows") or [] if row.get("industry")}
        items = []
        for row in store.watch_list():
            code = row["ts_code"]
            l1 = _industry_of(code, market_tape)
            record = store.latest(code)
            page = watchlist.shown_page(record, mode) if record else None
            lean = ((page or {}).get("direction") or {}).get("lean")
            verdict = verdict_by_l1.get(l1)
            action, reason = rules.watch_action(verdict, lean)
            items.append({
                "ts_code": code, "name": row["name"], "industry": l1 or "—",
                "sector": sector_by_l1.get(l1, ""), "verdict": verdict,
                "direction": {"bullish": "偏多", "bearish": "偏空", "unclear": "不明"}.get(lean, "还没有判断"),
                "action": action, "reason": reason,
            })
        return items
    except Exception:
        return []


def _industry_of(code: str, tape) -> str:
    rows = tape.cached(f"member_of:{code}", lambda: tape.pro().index_member_all(ts_code=code, is_new="Y"))
    for row in rows:
        if row.get("l1_name"):
            return str(row["l1_name"])
    return ""


# ---------------------------------------------------------------- 结论
def conclusion(turnover: str, etf: str, margin: str) -> dict:
    """短期「钱」的结论只看成交、宽基份额和两融。南向、流动性和增减持不改这一档。"""
    lean = judge_pulse(turnover, etf, margin)
    gate = "通过" if lean == "在进" else "不过"
    toward_in = []
    if etf != "净流入":
        toward_in.append("宽基 ETF 近 5 日净流入，而且至少 4 天在申购")
    if turnover == "冷清":
        toward_in.append("成交回到平时，不再冷清")
    toward_weak_met = []
    toward_weak_open = []
    if margin in ("在降", "加速下降"):
        toward_weak_met.append(f"两融{margin}")
    else:
        toward_weak_open.append("两融 4 周下降超过 2%")
    if turnover == "冷清":
        toward_weak_met.append("成交冷清")
    else:
        toward_weak_open.append("成交转为冷清")
    if lean == "在进":
        summary = "宽基在净流入，成交也不冷清，短期可以看成有钱进来。"
    elif lean == "偏弱" and etf == "净流入":
        summary = "成交冷清、借钱炒股的在还钱，但宽基 ETF 在净流入：有钱往 ETF 里去，要等成交回到平时才算「在进」。"
    elif lean == "偏弱":
        summary = "成交冷清，借钱炒股的余额还在降，没有新钱推动。"
    else:
        summary = "成交、宽基和两融没有一起指向进场或撤出。"
    return {
        "label": lean,
        "gate": gate,
        "gate_text": "只减不加" if lean == "偏弱" else ("可以看增量" if lean == "在进" else "先看，不加码"),
        "summary": summary,
        "toward_in": toward_in,
        "toward_weak_met": toward_weak_met,
        "toward_weak_open": toward_weak_open,
    }


def _trend(indexes: list[dict]) -> tuple[str, str]:
    known = [row for row in indexes if row.get("above_ma60") is not None]
    if not known:
        return "没有", "指数日线不够 60 天"
    below = [row["name"] for row in known if not row["above_ma60"]]
    above = [row["name"] for row in known if row["above_ma60"]]
    broke = [row["name"] for row in known if row.get("broke_low")]
    if len(above) * 4 >= len(known) * 3:
        word = "向上"
    elif len(above) * 4 <= len(known):
        word = "向下"
    else:
        word = "震荡"
    bits = []
    if above:
        bits.append("、".join(above) + "在 60 日线上")
    if below:
        bits.append("、".join(below) + "在 60 日线下")
    if broke:
        bits.append("、".join(broke) + "已跌破前低")
    return word, "；".join(bits)


def _money_signal(parts: dict) -> dict:
    turn, etf, margin = parts["turnover"], parts["etf"], parts["margin"]
    head = conclusion(turn.get("label") or "没有", etf.get("label") or "没有", margin.get("label") or "没有")
    tier = rules.money_tier(head["label"])
    tone = {"顺": "good", "平": "warn", "逆": "bad"}[tier]
    return {"head": head, "tier": tier, "tone": tone}


def _mood_card(parts: dict) -> dict:
    indexes = (parts["index"] or {}).get("rows") or []
    breadth, limits = parts["breadth"], parts["limits"]
    signal = rules.mood_signal(indexes, breadth, limits)
    tier = signal["tier"]
    trend_word, trend_text = _trend(indexes)
    up, down = breadth.get("up"), breadth.get("down")
    share = up / (up + down) if up is not None and down is not None and (up + down) else None
    wide = "没有" if share is None else "有" if share >= 0.55 else "没有" if share <= 0.40 else "分化"
    follow, broken = limits.get("follow"), limits.get("broken_rate")
    if follow is None or broken is None:
        chase = "没有"
    else:
        chase = "能" if follow > 0 and broken < 0.30 else "难" if follow < -0.5 or broken > 0.40 else "一般"
    qa = [
        {"q": "大盘趋势怎么样？", "a": trend_word, "detail": trend_text or "没有取到指数", "tone": _tone3(trend_word, "向上", "向下")},
        {"q": "多数股票在涨吗？", "a": wide, "detail": (
            f"今天 {breadth.get('up', '—')} 涨、{breadth.get('down', '—')} 跌"
            + ("" if share is None else f"，上涨占 {round(share * 100)}%")
            + f"；近 25 个交易日新高 {breadth.get('highs', '—')}，新低 {breadth.get('lows', '—')}"
        ), "tone": _tone3(wide, "有", "没有")},
        {"q": "短线追涨能赚钱吗？", "a": chase, "detail": (
            f"涨停 {limits.get('up', '—')} / 跌停 {limits.get('down', '—')}，最高 {limits.get('height', '—')} 板，炸板率 "
            + ("没有" if broken is None else f"{round(broken * 100)}%")
            + ("" if follow is None else f"；昨天涨停的 {limits.get('follow_count', 0)} 只今天平均 {follow:+.2f}%")
        ), "tone": _tone3(chase, "能", "难")},
    ]
    emotion = parts.get("emotion") or {}
    if emotion.get("ok"):
        last = emotion["last"]
        follow = last.get("follow")
        qa.append({
            "q": "短线情绪走到哪了？", "a": emotion["stage"], "tone": {"发酵": "good", "高潮": "warn", "退潮": "bad", "冰点": "bad"}.get(emotion["stage"], "flat"),
            "detail": f"涨停 {last['up']} / 跌停 {last['down']}，最高 {last['height']} 板" + ("" if follow is None else f"，昨涨停今日 {follow:+.2f}%") + "；按近 3 天平均判",
        })
    means = {
        "顺": "环境对做多友好，但仍要挑有新钱的板块。",
        "平": "涨跌互有，选股比选时点重要。",
        "逆": "现在买进去，大概率要先扛一段；钱只往有新钱的板块去。",
    }.get(tier, "没有足够的数据判断。")
    when = {
        "顺": "多数指数跌回 60 日线下、或上涨家数不足四成 → 「好不好做」转平",
        "平": "多数指数站上 60 日线、上涨占五成五以上、追涨开始有收益 → 转顺",
        "逆": "多数指数站回 60 日线、上涨家数过半 → 「好不好做」转平",
    }.get(tier, "")
    return {
        "tier": tier, "label": signal["label"], "tone": {"顺": "good", "平": "warn", "逆": "bad"}.get(tier, "flat"),
        "reasons": signal["reasons"], "summary": "；".join(signal["reasons"]) or "没有足够的数据",
        "qa": qa, "means": means, "when": when, "date": breadth.get("date") or limits.get("date") or "",
        "stage": emotion.get("stage") if emotion.get("ok") else None,
        "stages": [{"date": row["date"], "stage": row["stage"], "up": row["up"], "down": row["down"]} for row in emotion.get("days") or []],
        "indexes": [
            {"name": row["name"], "close": row.get("close"), "pct": row.get("pct"), "ret20": row.get("ret20"),
             "ma20": row.get("above_ma20"), "ma60": row.get("above_ma60"), "broke_low": row.get("broke_low")}
            for row in indexes
        ],
        "scope": "全 A 不含 ST · 指数日线 · 涨停按 limit_list_d · 收盘后更新",
    }


def _tone3(word: str, good: str, bad: str) -> str:
    return "good" if word == good else "bad" if word == bad else "flat"


def _better_worse(parts: dict, mood: dict) -> tuple[list[dict], list[dict]]:
    turn, etf, margin = parts["turnover"], parts["etf"], parts["margin"]
    indexes = (parts["index"] or {}).get("rows") or []
    better, worse = [], []
    etf_met = etf.get("label") == "净流入"
    better.append({"text": "宽基 ETF 近 5 天净流入超过 1%，且至少 4 天在买", "met": etf_met,
                   "now": f"现在 {_pct(etf.get('ratio'), 2)}，{etf.get('buy_days', '—')} 天在买"})
    if turn.get("label") == "冷清":
        better.append({"text": "成交回到平时水平，不再冷清", "met": False, "now": f"现在近 5 日 {_pct(turn.get('ratio5'))}"})
    margin_down = margin.get("label") in ("在降", "加速下降")
    better.append({"text": "借钱炒股的人不再减少", "met": not margin_down, "now": f"现在 4 周 {_pct(margin.get('ratio4'))}"})
    broken = [row["name"] for row in indexes if row.get("broke_low")]
    worse.append({"text": "借钱炒股的 4 周减少超过 5%", "met": margin.get("label") == "加速下降", "now": f"现在 4 周 {_pct(margin.get('ratio4'))}"})
    worse.append({"text": "指数跌破前低", "met": bool(broken), "now": "现在 " + ("、".join(broken) + "已破" if broken else "都没破")})
    worse.append({"text": "成交转为冷清", "met": turn.get("label") == "冷清", "now": f"现在近 5 日 {_pct(turn.get('ratio5'))}"})
    return better, worse


# ---------------------------------------------------------------- 宏观
def _macro_card(parts: dict) -> dict:
    macro, valuation = parts["macro"], parts["valuation"]
    history = macro.get("history") or []
    liquidity, liquidity_text = rules.liquidity_score(history)
    profit, profit_text = rules.profit_score(history)
    value, value_text = rules.value_score(valuation.get("pctile"))
    hit = market_team.policy_hit((parts.get("announce") or {}).get("items") or [], datetime.now())
    policy = 5 if hit else 3
    policy_text = (f"{hit['date']} {hit['title']}" if hit else
                   ("近 7 天没有筛到降准降息或汇金增持的官方标题" if (parts.get("announce") or {}).get("ok") else "公告新闻没取到，暂按 3 分"))
    scores = {"policy": policy, "liquidity": liquidity, "profit": profit, "value": value}
    direction = rules.macro_direction(scores)
    names = {"liquidity": "钱活不活", "profit": "企业赚钱", "value": "估值"}
    strong = [names[key] for key in ("profit", "value") if scores[key] >= 5]
    weak = [names[key] for key in ("liquidity", "profit", "value") if scores[key] <= 1]
    head = ("、".join(strong) + "有支撑" if strong else "") + ("，但" if strong and weak else "") + ("、".join(weak) + "偏弱" if weak else "")
    fx, us, hk = parts["fx"], parts["us"], parts["hk"]
    external = [
        {"name": "人民币", "word": rules.trend_word(fx.get("change5"), "走弱", "走强"), "detail": f"美元兑离岸人民币 {_plain(fx.get('close'), 4)}"},
        {"name": "美债 10Y", "word": rules.trend_word(us.get("change5"), "上行", "下行", 0.02), "detail": f"收益率 {_plain(us.get('y10'))}%"},
        {"name": "恒生科技", "word": rules.trend_word(hk.get("change5"), "走强", "走弱", 0.005), "detail": f"{_plain(hk.get('close'))}，近 5 日 {_pct(hk.get('change5'))}"},
    ]
    month = macro.get("month") or ""
    rows = [
        {"key": "policy", "name": "政策", "text": policy_text, "age": "近 7 天", "score": policy, "weight": 0.35,
         "url": (hit or {}).get("url", "")},
        {"key": "liquidity", "name": "流动性", "text": liquidity_text, "age": "上月", "score": liquidity, "weight": 0.25},
        {"key": "profit", "name": "盈利", "text": profit_text, "age": "上月", "score": profit, "weight": 0.20},
        {"key": "value", "name": "估值", "text": value_text, "age": "近 3 年", "score": value, "weight": 0.20},
    ]
    return {
        "tier": direction["tier"], "label": {"顺": "顺", "平": "平", "逆": "逆"}[direction["tier"]],
        "tone": {"顺": "good", "平": "warn", "逆": "bad"}[direction["tier"]],
        "score": direction["score"], "headline": head or "各项没有明显偏向",
        "rows": rows, "external": external, "month": month,
        "formula": "政策 {p} × 35% + 流动性 {a} × 25% + 盈利 {b} × 20% + 估值 {c} × 20% = {t:.2f}".format(
            p=policy, a=liquidity, b=profit, c=value, t=direction["score"]),
        "next": "下次发布：下月的 PPI 和 M1 / M2（通常次月上中旬）",
        "scope": "1 / 3 / 5 分按规则速查；方向分是位置不是预测；政策看新闻搜索里的官方标题，可能有遗漏；估值用沪深 300 盈利收益率的 3 年分位，不是 ERP",
        "metrics": {
            "m1": macro.get("m1_yoy"), "m2": macro.get("m2_yoy"), "ppi": macro.get("ppi_yoy"), "sf": macro.get("sf_month"),
            "pe": valuation.get("pe"), "earn_yield": valuation.get("earn_yield"), "value_pctile": valuation.get("pctile"),
        },
    }


# ---------------------------------------------------------------- 日历
def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def _calendar_card(parts: dict, watch: list[dict], today: date) -> dict:
    window_end = today + timedelta(days=14)
    rows: list[dict] = []
    for item in (parts["calendar"] or {}).get("events") or []:
        tag = "数据" if "中国" in item["name"][:2] else "外部"
        rows.append({"date": item["date"], "text": item["name"], "tag": tag})
    for item in (parts["ipo"] or {}).get("days") or []:
        rows.append({"date": item["date"], "text": f"{item['count']} 只新股申购", "tag": "IPO"})
    for month in (today.month, today.month % 12 + 1):
        year = today.year + (1 if month < today.month else 0)
        delivery = _third_friday(year, month)
        if today <= delivery <= window_end:
            rows.append({"date": delivery.isoformat(), "text": "股指期货交割（遇休市顺延）", "tag": "交割"})
    unlock = parts["unlock"] or {}
    if unlock.get("count"):
        rows.append({
            "date": today.isoformat(),
            "text": f"未来 10 天解禁 {unlock['count']} 条，合计 {unlock['total']:.2f} 亿股" + ("（单次最多 6000 条，没算完）" if unlock.get("capped") else ""),
            "tag": "解禁",
        })
    mine = _my_unlocks(watch, today) + _my_disclosures(watch, today)
    rows.extend(mine)
    rows.sort(key=lambda row: (row["date"], row["tag"] != "涉及自选"))
    for row in rows:
        day = date.fromisoformat(row["date"])
        row["weekday"] = "周" + _WEEKDAY[day.weekday()]
        row["day"] = row["date"][5:]
    dense = len(rows) >= 6
    return {
        "label": "事件偏密" if dense else "事件正常", "tone": "warn" if dense else "flat",
        "rows": rows[:8], "mine_count": len(mine), "unlock": unlock,
        "scope": "解禁按股数，没按市值；交割日按每月第三个周五，遇休市顺延；只提示，不改档位",
    }


_PERIOD_NAME = {"0331": "一季报", "0630": "半年报", "0930": "三季报", "1231": "年报"}


def _my_disclosures(watch: list[dict], today: date) -> list[dict]:
    """自选股未来两周的定期报告披露日（预约日期）。"""
    if not watch:
        return []
    from app.services import market_tape

    periods = [f"{today.year - 1}1231", f"{today.year}0331", f"{today.year}0630", f"{today.year}0930", f"{today.year}1231"]
    end = (today + timedelta(days=14)).isoformat()
    out = []
    for row in watch:
        for period in periods:
            try:
                rows = market_tape.cached(f"discl:{row['ts_code']}:{period}", lambda c=row["ts_code"], p=period: market_tape.pro().disclosure_date(end_date=p, ts_code=c))
            except Exception:
                continue
            for item in rows:
                when = market_tape.iso(item.get("actual_date") or item.get("pre_date"))
                if when and today.isoformat() <= when <= end:
                    out.append({"date": when, "text": f"{row['name']} {_PERIOD_NAME.get(period[4:], '定期报告')}披露", "tag": "涉及自选"})
    return out


def _my_unlocks(watch: list[dict], today: date) -> list[dict]:
    if not watch:
        return []
    from app.services import market_tape

    names = {row["ts_code"]: row["name"] for row in watch}
    out = []
    try:
        rows = market_tape.unlock_rows(today)
    except Exception:
        return []
    merged: dict[tuple[str, str], float] = {}
    for row in rows:
        code = str(row.get("ts_code") or "")
        if code not in names:
            continue
        key = (code, market_tape.iso(row.get("float_date")))
        merged[key] = merged.get(key, 0.0) + (market_tape.num(row.get("float_ratio")) or 0.0)
    for (code, day), ratio in merged.items():
        out.append({"date": day, "text": f"{names[code]} 解禁，占总股本 {ratio:.2f}%", "tag": "涉及自选"})
    return out


# ---------------------------------------------------------------- 板块
def _sector_rows(sectors: dict, catalysts: dict | None = None) -> list[dict]:
    rows = []
    for row in sectors.get("rows") or []:
        flow = row["flow"].get(sectors.get("conclusion_days", 10))
        rows.append({
            "name": row["name"], "code": row["code"], "industry": row["industry"],
            "flow": flow, "flow_text": _yi(flow, True), "flows": {str(key): value for key, value in row["flow"].items()},
            "heat": row["heat"], "heat_text": "—" if row["heat"] is None else f"{row['heat']:.1f}×",
            "lever": row["lever"], "lever_word": row["lever_word"],
            "verdict": row["verdict"], "tone": row["tone"],
            "catalyst": (catalysts or {}).get(row["name"]),
        })
    return rows


def _sector_headline(rows: list[dict]) -> str:
    good = [row["name"] for row in rows if row["verdict"] == "三层同向"]
    money_in = [row["name"] for row in rows if row["flow"] is not None and row["flow"] > 0]
    money_out = [row["name"] for row in rows if row["flow"] is not None and row["flow"] <= 0]
    hot_no_money = [row["name"] for row in rows if row["verdict"] == "只热不增"]
    bits = []
    if good:
        bits.append("、".join(good) + "三层同向")
    if money_in:
        bits.append("钱在往 " + "、".join(money_in) + " 走")
    if hot_no_money:
        bits.append("、".join(hot_no_money) + "只热不增")
    if money_out:
        bits.append("、".join(money_out) + "在流出")
    return "；".join(bits) or "没有取到板块 ETF"


# ---------------------------------------------------------------- 国家队
_TEAM_TIER = {"在出手": "顺", "没出手": "平", "可能在卖": "逆"}
_TEAM_TONE = {"顺": "good", "平": "flat", "逆": "bad"}


def _team_signal(team: dict) -> dict:
    if not team.get("ok"):
        return {"key": "team", "n": "②", "name": "国家队", "tier": None, "label": "没取到", "tone": "flat", "text": team.get("text") or "份额数据没取到，不参与总档位"}
    tier = _TEAM_TIER[team["label"]]
    return {"key": "team", "n": "②", "name": "国家队", "tier": tier, "label": team["label"], "tone": _TEAM_TONE[tier], "text": _team_headline(team)}


def _team_headline(team: dict) -> str:
    last, hold = team.get("last"), team.get("hold")
    if team["label"] == "在出手":
        return f"{last['day'][5:]} 大盘跌 {abs(last['index_pct']):.1f}%，大盘 ETF 份额一天多了 {last['delta']:.0f} 亿份"
    if team["label"] == "可能在卖":
        return f"出手后新增份额已撤走 {hold['gone'] * 100:.0f}%，超过 3 成"
    if last:
        return f"最近没出手，{last['day'][5:]} 出过一次手（{last['since']} 个交易日前）"
    return "近一年没有出现符合条件的大额申购"


def _team_card(team: dict, announce: dict) -> dict:
    if not team.get("ok"):
        return {"pending": True, "text": team.get("text") or "份额数据没取到，这一项暂时判不了。"}
    last, hold, counts = team.get("last"), team.get("hold"), team["counts"]
    tier = _TEAM_TIER[team["label"]]
    if last:
        first = (f"{last['day'][5:]} 出过一次手：那天上证跌 {abs(last['index_pct']):.1f}%，9 只 ETF 份额一天多了 {last['delta']:.0f} 亿份，"
                 f"明显高于平时" + (f"；之后 {last['since']} 个交易日没有再出现。" if last["since"] > 0 else "。"))
    else:
        first = "近一年没有出现「大盘跌超 1%、份额明显放大」的日子。"
    gone = None if not hold else hold["gone"]
    rows = [
        {"q": "最近有没有出手托底？", "a": f"近 20 天 {counts[20]} 次", "tone": "good" if counts[5] else "flat", "detail": first},
        {"q": "之前买进去的钱还在吗？", "a": "没有出手记录" if not hold else ("大部分还在" if gone <= 0.15 else "有一部分撤走" if gone <= 0.30 else "撤走超过 3 成"),
         "tone": "flat" if not hold or gone <= 0.30 else "bad",
         "detail": "没有可追踪的出手日" if not hold else f"出手日 {hold['bought']:.0f} 亿份，从那天起累计新增 {hold['now']:.0f} 亿份；低于 {hold['line']:.0f} 亿份才算撤走超过 3 成"},
        {"q": "有没有在悄悄卖？", "a": "有迹象" if team["label"] == "可能在卖" else "暂时没有迹象", "tone": "bad" if team["label"] == "可能在卖" else "good",
         "detail": "新增份额撤走超过 3 成才算有卖出迹象；只看份额，不确认是谁"},
    ]
    means = {
        "在出手": "有人在大跌里接盘，但这是跌出来的托底，不代表马上反转。",
        "可能在卖": "之前托底的份额在撤，下方支撑变弱。",
        "没出手": "有人托底但不追涨：大跌时它可能再出手，但别指望它推着市场往上走。",
    }[team["label"]]
    return {
        "pending": False, "tier": tier, "label": team["label"], "tone": _TEAM_TONE[tier], "summary": _team_headline(team),
        "qa": rows, "means": means, "when": "再遇到大跌日大额申购 → 「在出手」；新增份额撤走超过 3 成 → 「可能在卖」",
        "scope": "看的是 9 只大盘 ETF 的大额申购，这类资金通常来自国家队，不确认具体是谁；份额晚一天公布", "date": team.get("latest", ""),
        "announce": announce.get("items", [])[:1],
    }


def _team_tab(parts: dict, day: date) -> dict:
    team, announce = parts.get("team") or {}, parts.get("announce") or {}
    if not team.get("ok"):
        return {"title": "国家队", "label": "没取到", "tone": "flat", "pending": True, "sections": [],
                "summary": team.get("text") or "份额数据没取到，这一项暂时判不了。", "footnote": ""}
    card = _team_card(team, announce)
    last, hold, counts = team["last"], team["hold"], team["counts"]
    previous = _previous_open(parts, day)
    act = _block(
        "act", "最近有没有出手", "大跌时有没有人托底", f"近 20 天 {counts[20]} 次", _age(team["latest"], day, previous), team["label"], card["tone"],
        [_stat("近 5 天", f"{counts[5]} 次"), _stat("近 20 天", f"{counts[20]} 次", True), _stat("近 60 天", f"{counts[60]} 次")],
        "上证当天跌超 1%，9 只 ETF 合计份额当天的增加，超过前 60 个交易日日变化均值加 2 倍标准差，算一次出手；只看份额，T+1 次日晚确认。",
        story=card["qa"][0]["detail"], open=True,
        chart={"kind": "team", "unit": "亿份", "points": team["series"], "legend": ["出手日", "份额增", "份额减"]},
        listing={"title": f"看的是哪几只 ETF（国家队通道 {team['counted']} 只，和「宽基观察池」不是一套）",
                 "rows": [{"left": f"{name}", "mid": code[:6], "right": "", "code": code} for code, name in market_team.TEAM_BOOK if code in team["codes"]]},
    )
    holdblock = _block(
        "hold", "买进去的钱还在吗", "有没有在悄悄卖", "没有出手记录" if not hold else f"累计新增 {hold['now']:.0f} 亿份", _age(team["latest"], day, previous),
        card["qa"][1]["a"], card["qa"][1]["tone"],
        [_stat("出手日", "—" if not hold else f"{hold['bought']:.0f} 亿份"), _stat("现在", "—" if not hold else f"{hold['now']:.0f} 亿份", True),
         _stat("撤走", "—" if not hold or hold["gone"] is None else f"{hold['gone'] * 100:.0f}%")],
        "从出手日起累计新增份额，撤走超过 3 成才改成「可能在卖」；累计里包含出手后别人的申购，所以可能比出手那天还多。",
    )
    live = _block(
        "live", "今天盘中", "有没有异动", "未接入", "今日", "未接入", "flat", [],
        "盘中成交放大、价格比净值溢价，需要实时 ETF 行情；这一期没接，晚上的份额数据才算数。",
    )
    items = [{"left": f"{row['who']}：{row['title']}", "mid": row["date"][5:], "right": "强信号" if row["strong"] else "", "url": row["url"]} for row in announce.get("items") or []]
    ann = _block(
        "ann", "官方说了什么", "汇金、央行、证监会", f"近 20 天 {len(items)} 条" if items else "没有筛到", "近 20 天", "只做确认", "flat",
        [], "新闻搜索里标题带官方主体和明确动作的才算；公告只用来确认，不单独改状态。搜索可能有遗漏。",
        listing={"title": "标题（来自东方财富新闻搜索）", "rows": items} if items else None,
    )
    quarter = _block(
        "qtr", "季报核对", "事后确认有没有卖过", "没有接口", "上季", "不判断", "flat", [],
        "季报持有人和倒挂核对没有稳定接口，这一块不填示意数据。2026 年 5 月起年报、中报也不再列实名。",
    )
    hist = team.get("history") or []
    history_block = _block(
        "hist", "历次出手后 20 个交易日", "上证指数的表现", f"{len(hist)} 次", "近一年", "样本少", "flat", [],
        "只有这一年的几次可比样本，不能用来推测会涨到哪、什么时候涨。",
        listing={"title": "出手日 · 当天份额增加 · 之后 20 日上证", "rows": [
            {"left": row["day"], "mid": f"+{row['delta']:.0f} 亿份", "right": ("进行中 · 第 %d 天" % row["progress"]) if row["ret20"] is None else _pct(row["ret20"])}
            for row in reversed(hist)
        ]} if hist else None,
    )
    return {
        "title": "国家队", "label": team["label"], "tone": card["tone"], "age": _age(team["latest"], day, previous), "summary": _team_headline(team),
        "means": card["means"], "when": card["when"],
        "sections": [
            {"title": "三件事看懂国家队", "hint": "第一块是判断用的，点标题可收起", "blocks": [act, holdblock, live]},
            {"title": "辅助确认 · 公告和季报", "hint": "", "blocks": [ann, quarter]},
            {"title": "历次出手", "hint": "", "blocks": [history_block]},
        ],
        "footnote": "我们不看什么：席位、托管行、龙虎榜、盘中「尾盘拉权重」都在猜「是谁」，猜不准，也不影响判断，所以不进结论。只看「钱进没进、出没出」，不猜「谁在买卖」。",
    }


# ---------------------------------------------------------------- 摘要
def build_summary(today: date | None = None, record: bool = True) -> dict:
    day = today or datetime.now(_ZONE).date()
    parts = bundle(day)
    summary = summarize(parts, day)
    if record:
        try:
            market_notify.record(summary, parts)
        except Exception:
            pass   # 存档失败不能让页面打不开
    return summary


def refresh(today: date | None = None) -> None:
    """让下一次取数重新算（定时任务用）。"""
    day = today or datetime.now(_ZONE).date()
    cache.set(f"market_page:bundle:{day.isoformat()}", None, 0)


def changes_view(mode: str, since_ts: float | None = None, today: date | None = None) -> dict:
    """变了什么：mode 取 last / today / week。"""
    day = today or datetime.now(_ZONE).date()
    build_summary(day)
    snaps = get_store().snapshots()
    out = market_changes.view(mode, snaps, since_ts, market_notify.settings()["etf_n"])
    if mode == "today":
        out["items"] = out["items"] + _intraday_items(market_intraday.build())
    return out


def _intraday_items(data: dict) -> list[dict]:
    items = []
    market = data.get("market")
    if data.get("live") and market and market.get("vs_yesterday") is not None:
        rate = market["vs_yesterday"]
        items.append({"kind": "intraday", "title": f"成交比昨天同一时间{'多' if rate > 0 else '少'} {abs(rate) * 100:.0f}%", "tag": "盘中",
                      "detail": "市场" + ("更热了" if rate > 0 else "更冷了") + "；盘中数据只提醒，不改结论", "tone": "good" if rate > 0 else "warn", "tab": "fund"})
    etf = data.get("etf")
    if data.get("live") and etf and etf.get("alert"):
        items.append({"kind": "intraday", "title": "大盘 ETF 盘中成交放大", "tag": "盘中预警", "detail": "大盘跌超 1%，9 只 ETF 成交是前几天同一时刻的 3 倍以上；要等晚上的份额数据才算数", "tone": "good", "tab": "team"})
    return items


def history_view() -> list[dict]:
    return market_changes.history(get_store().snapshots())


def notify_payload(today: date | None = None) -> dict:
    day = today or datetime.now(_ZONE).date()
    parts = bundle(day)
    card = _calendar_card(parts, parts["watch"], day)
    upcoming = [
        {"tag": "提醒", "title": row["text"], "when": row["date"][5:], "weekday": row["weekday"], "tab": "cal"}
        for row in card["rows"] if row["tag"] == "涉及自选" and row["date"] >= day.isoformat()
    ]
    return market_notify.notifications(upcoming=upcoming, today=day)


def summarize(parts: dict, day: date) -> dict:
    money = _money_signal(parts)
    mood = _mood_card(parts)
    macro = _macro_card(parts)
    sector_rows = _sector_rows(parts["sectors"], parts.get("catalysts"))
    good_sectors = [row["name"] for row in sector_rows if row["verdict"] in ("三层同向", "有钱没热度")]
    signals = [
        {"key": "money", "n": "①", "name": "钱", "tier": money["tier"], "label": rules.MONEY_WORD[money["tier"]],
         "tone": money["tone"], "text": money["head"]["summary"]},
        _team_signal(parts.get("team") or {}),
        {"key": "mood", "n": "③", "name": "好不好做", "tier": mood["tier"], "label": mood["label"], "tone": mood["tone"],
         "text": mood["summary"]},
    ]
    total = rules.overall(signals)
    better, worse = _better_worse(parts, mood)
    indexes = (parts["index"] or {}).get("rows") or []
    previous = _previous_open(parts, day)
    return {
        "generated_at": datetime.now(_ZONE).strftime("%Y-%m-%d %H:%M"),
        "today": day.isoformat(),
        "indexes": [{"name": row["name"], "close": row.get("close"), "pct": row.get("pct"), "date": row.get("date")} for row in indexes],
        "dates": {
            "quote": indexes[0]["date"] if indexes else "",
            "money": (parts["margin"] or {}).get("date") or (parts["turnover"] or {}).get("date") or "",
            "macro": (parts["macro"] or {}).get("month") or "",
        },
        "conclusion": {
            "level": total["level"], "tilt": total["tilt"], "count": total["count"],
            "gate": money["head"]["gate"], "gate_text": money["head"]["gate_text"],
            "headline": "、".join(item["text"] for item in signals if item["tier"] and item["tier"] != "平") or "成交、宽基、两融和指数没有一起指向一边",
            "action": rules.action_text(total["level"], total["tilt"], good_sectors),
            "signals": signals, "better": better, "worse": worse,
            "note": "按框架规则得出，不构成投资建议。" + ("" if (parts.get("team") or {}).get("ok") else "国家队没取到，总档位只用已经取到的信号。"),
        },
        "money": _money_card(parts, money),
        "team": _team_card(parts.get("team") or {}, parts.get("announce") or {}),
        "mood": mood,
        "macro": macro,
        "calendar": _calendar_card(parts, parts["watch"], day),
        "sectors": {
            "rows": sector_rows, "date": parts["sectors"].get("date"), "headline": _sector_headline(sector_rows),
            "scope": "增量 = 代表 ETF 近 2 周份额净增 × 收盘价；热度 = 换手 ÷ 自身一年均值，1.5 倍以上算热；"
                     "杠杆 = 对应申万一级行业两融余额 4 周变化，±2% 内算走平；每个板块取一只代表 ETF，红利没有对应行业",
        },
        "watch": {"rows": parts["watch"], "scope": "动作词由「板块结论 × 个股判断」按规则生成，不是买卖指令；只读已有判断，不在这里批量发起诊断"},
        "ages": {"turnover": _age((parts["turnover"] or {}).get("date", ""), day, previous), "etf": _age((parts["etf"] or {}).get("latest_date", ""), day, previous)},
    }


def _money_card(parts: dict, money: dict) -> dict:
    turn, etf, margin, supply = parts["turnover"], parts["etf"], parts["margin"], parts["supply"]
    etf_word = {"净流入": "有", "净流出": "没有，在流出", "基本没变": "基本没有"}.get(etf.get("label"), "没有")
    taking = margin.get("label") in ("在降", "加速下降") or supply.get("label") == "在拿走"
    return {
        "tier": money["tier"], "label": rules.MONEY_WORD[money["tier"]], "tone": money["tone"],
        "summary": money["head"]["summary"],
        "qa": [
            {"q": "最近交易活跃吗？", "a": {"冷清": "比平时冷清", "活跃": "比平时活跃", "平时": "和平时差不多"}.get(turn.get("label"), "没有"),
             "tone": {"冷清": "bad", "活跃": "good"}.get(turn.get("label"), "flat"),
             "detail": f"近 5 天成交比平时（60 天平均）{_pct(turn.get('ratio5'))}，今天 {_yi(turn.get('amount'))}"},
            {"q": "有新钱进场吗？", "a": etf_word, "tone": {"净流入": "good", "净流出": "bad"}.get(etf.get("label"), "flat"),
             "detail": f"宽基 ETF 近 5 天份额 {_pct(etf.get('ratio'), 2)}，净申购约 {_yi(etf.get('money'), True)}，{etf.get('buy_days', '—')} 天在买"},
            {"q": "有人在拿钱走吗？", "a": "有" if taking else "暂时没有", "tone": "bad" if taking else "good",
             "detail": f"借钱炒股的 4 周{_pct(margin.get('ratio4'))}；大股东近 4 周增持 + 回购 − 减持 {_yi(supply.get('net'), True)}"},
        ],
        "means": money["head"]["summary"],
        "when": "宽基 ETF 5 天净买入超过 1%（且至少 4 天在买），同时成交不再冷清 → 「在进」",
        "scope": "成交额只说明热不热；宽基份额变化乘收盘价；回购按公司只留最新一条实施公告",
        "date": margin.get("date") or turn.get("date") or "",
    }


# ---------------------------------------------------------------- 依据 tab
def build_tab(name: str, today: date | None = None) -> dict:
    if name not in TABS:
        raise ValueError(f"未知 tab {name}")
    day = today or datetime.now(_ZONE).date()
    return tab_of(name, bundle(day), day)


def tab_of(name: str, parts: dict, day: date) -> dict:
    if name == "fund":
        return _fund_tab(parts, day)
    if name == "team":
        return _team_tab(parts, day)
    if name == "sector":
        return _sector_tab(parts)
    if name == "mood":
        return _mood_tab(parts)
    if name == "cal":
        return _cal_tab(parts, day)
    return _macro_tab(parts)


def _block(key, title, sub, big, age, judge, tone, stats, rule, **extra) -> dict:
    return {"key": key, "title": title, "sub": sub, "big": big, "age": age, "judge": judge, "tone": tone,
            "stats": stats, "rule": rule, **extra}


def _fund_tab(parts: dict, day: date) -> dict:
    previous = _previous_open(parts, day)
    turn, etf, margin = parts["turnover"], parts["etf"], parts["margin"]
    south, shibor, supply, funds = parts["south"], parts["shibor"], parts["supply"], parts["funds"]
    money = _money_signal(parts)
    groups = etf.get("groups") or {}
    core = [
        _block(
            "turnover", "成交额", "市场热不热", _yi(turn.get("amount")), _age(turn.get("date", ""), day, previous),
            turn.get("label") or "没有", {"冷清": "bad", "活跃": "good"}.get(turn.get("label"), "flat"),
            [_stat("5 日 vs 平时", _pct(turn.get("ratio5")), True), _stat("20 日 vs 平时", _pct(turn.get("ratio20"))),
             _stat("一年分位", "—" if turn.get("pctile") is None else f"{round(turn['pctile'] * 100)}%")],
            "近 5 日均额相对过去 60 日，低于 −5% 算冷清，高于 +5% 算活跃。成交额只说明热不热，不代表钱在进还是出。盘中同一时刻的对比这一期没接。",
            story=(f"近 5 天里 {turn['down_days']} 天指数下跌，成交" + ("一直比平时少" if (turn.get("ratio5") or 0) < 0 else "比平时多") + "。") if turn.get("down_days") else "",
            pctile=_pctile_info("成交额处在一年里", turn.get("pctile")),
            chart={"kind": "bars", "unit": "亿元", "points": turn.get("series") or [], "legend": ["上涨日", "下跌日"]}, open=True,
        ),
        _block(
            "etf", "宽基 ETF 份额", "有没有新钱买入", f"{_plain(etf.get('total'), 0)} 亿份", _age(etf.get("latest_date", ""), day, previous),
            etf.get("label") or "没有", {"净流入": "good", "净流出": "bad"}.get(etf.get("label"), "flat"),
            [_stat("大盘", _yi(groups.get("大盘"), True)), _stat("中小盘", _yi(groups.get("中小盘"), True)),
             _stat("科创创业", _yi(groups.get("科创创业"), True), True)],
            "9 只宽基 ETF 份额 5 天合计变化，超过 +1% 且至少 4 天在买算净流入，低于 −1% 算净流出；按份额算，不受涨跌影响。金额是份额变化乘收盘价。",
            story=f"最近一天份额 {_delta(etf.get('day_change'))} 亿份，5 日 {_pct(etf.get('ratio'), 2)}，20 日 {_pct(etf.get('ratio20'))}；净申购 {etf.get('buy_days', '—')} 天。",
            pctile=_pctile_info("宽基 ETF 总份额处在一年里", etf.get("pctile")),
            chart={"kind": "area", "unit": "亿份", "points": etf.get("series") or [], "legend": ["当天净买入", "当天净卖出"]},
            items=etf.get("items") or [],
        ),
        _block(
            "margin", "两融余额", "借钱炒股的人多不多", _yi(margin.get("balance")), _age(margin.get("date", ""), day, previous),
            margin.get("label") or "没有", {"在降": "bad", "加速下降": "bad", "在升": "good"}.get(margin.get("label"), "flat"),
            [_stat("当日", _pct(margin.get("ratio_day"))), _stat("5 日", _pct(margin.get("ratio5"))), _stat("约 4 周", _pct(margin.get("ratio4")), True)],
            "4 周变化 ±2% 内算持平，低于 −2% 算在降，低于 −5% 算加速下降。",
            story=f"融资买入占成交 {_pct(margin.get('share'))}；当日融资净买入 {_yi(margin.get('net'), True)}。",
            pctile=_pctile_info("两融余额处在一年里", margin.get("pctile")),
            chart={"kind": "area", "unit": "亿元", "points": margin.get("series") or [], "legend": ["当天净借入", "当天净还款"]},
        ),
    ]
    where = [
        _block(
            "south", "南向资金", "A 股的钱有没有去港股", "没有" if south.get("ggt_net") is None else _delta(south["ggt_net"], 1), _age(south.get("ggt_date", ""), day, previous),
            "不改档位", "flat",
            [_stat("近 5 日", _delta(south.get("net5"))), _stat("近 20 日", _delta(south.get("net20")), True), _stat("5 日均值", _delta(south.get("five_avg")))],
            "大数字是港股通买入额 − 卖出额，单位沿用 ggt_daily 接口。北向资金已不按日披露，所以不看。不改上面的档位。",
            pctile=_pctile_info("近 20 日南下规模处在一年里", south.get("pctile20")),
            chart={"kind": "bars", "unit": "", "points": south.get("series") or [], "legend": ["当天净买入港股", "当天净卖出"], "signed": True},
        ),
        _block(
            "shibor", "银行间的钱", "钱贵不贵（Shibor 1 周）", "没有" if shibor.get("rate") is None else f"{shibor['rate']:.3f}%", _age(shibor.get("date", ""), day, previous),
            shibor.get("label") or "没有", {"偏松": "good", "偏紧": "bad"}.get(shibor.get("label"), "flat"),
            [_stat("1 周", "—" if shibor.get("rate") is None else f"{shibor['rate']:.3f}%", True), _stat("60 日均值", "—" if shibor.get("avg") is None else f"{shibor['avg']:.3f}%"), _stat("比较", shibor.get("side") or "—")],
            "这是 Shibor 1 周，当作 DR007 的近似，不是 DR007，也不是政策利率。央行净投放没接。",
        ),
    ]
    slow = [
        _block(
            "supply", "大股东增减持和回购", "增持 + 回购 − 减持", _yi(supply.get("net"), True), "近 28 天",
            supply.get("label") or "没有", {"在拿走": "bad", "有人在接": "good"}.get(supply.get("label"), "flat"),
            [_stat("增持", _yi(supply.get("buy")), True), _stat("减持", _yi(supply.get("sell"))), _stat("回购累计", _yi(supply.get("repo")))],
            "回购按公司只保留最新一条实施公告，是累计金额，不是这 28 天的增量，所以图里只画增持和减持。定增和再融资没有全市场日接口。",
            chart={"kind": "weeks", "unit": "亿元", "points": supply.get("weeks") or [], "legend": ["增持", "减持"]},
        ),
        _block(
            "funds", "新发基金", "老百姓有没有买基金入市", "没有" if funds.get("count") is None else f"{funds['count']} 只", "月频",
            "只看方向", "flat",
            [_stat("发行份额", "多数没公布" if funds.get("amount") is None else f"{funds['amount']:.2f} 亿份", True), _stat("范围", funds.get("scope") or "—"), _stat("只数", "—" if funds.get("count") is None else str(funds["count"]))],
            "月度数据，只看方向，不参与短期判断。新开户数没有接口，不放。",
            chart={"kind": "months", "unit": "只", "points": funds.get("months") or [], "legend": ["新发偏股基金只数"]},
        ),
    ]
    return {
        "title": "钱有没有进来", "label": rules.MONEY_WORD[money["tier"]], "tone": money["tone"],
        "age": _age(margin.get("date", ""), day, previous), "summary": money["head"]["summary"],
        "means": f"{money['head']['summary']}等到宽基 ETF 5 天净买入超过 1%、成交不再冷清，才会改成「在进」。",
        "sections": [
            {"title": "三个数看懂资金", "hint": "第一块是判断用的，点标题可收起", "blocks": core},
            {"title": "钱去哪了 · 钱贵不贵", "hint": "", "blocks": where},
            {"title": "变化慢一些的 · 周度 / 月度", "hint": "", "blocks": slow},
        ],
        "footnote": "数据没按时更新时（节假日、交易所延迟），对应的数会标日期并沿用上一次。",
    }


def _mood_tab(parts: dict) -> dict:
    mood = _mood_card(parts)
    breadth, limits = parts["breadth"], parts["limits"]
    return {
        "title": "好不好做", "label": mood["label"], "tone": mood["tone"], "age": "今日", "summary": mood["summary"],
        "means": mood["means"], "when": mood["when"], "indexes": mood["indexes"],
        "sections": [{"title": "三件事看懂好不好做", "hint": "", "blocks": [
            _block("trend", "大盘趋势", "指数在均线上还是下", mood["qa"][0]["a"], "今日", mood["qa"][0]["a"], mood["qa"][0]["tone"], [],
                   "看 60 日线定方向，前低 = 今天之前 20 个交易日的最低收盘价，今天收在它下面算破位。", story=mood["qa"][0]["detail"], open=True),
            _block("breadth", "多数股票", "涨的多还是跌的多", f"{breadth.get('up', '—')} / {breadth.get('down', '—')}", "今日", mood["qa"][1]["a"], mood["qa"][1]["tone"],
                   [_stat("上涨", str(breadth.get("up", "—")), True), _stat("下跌", str(breadth.get("down", "—"))), _stat("新高 / 新低", f"{breadth.get('highs', '—')} / {breadth.get('lows', '—')}")],
                   "全 A 不含 ST；新高、新低按近 25 个交易日。", story=mood["qa"][1]["detail"]),
            _block("limits", "短线情绪", "追涨能不能赚钱", f"涨停 {limits.get('up', '—')}", "今日", mood["qa"][2]["a"], mood["qa"][2]["tone"],
                   [_stat("跌停", str(limits.get("down", "—"))), _stat("炸板", str(limits.get("broken", "—"))), _stat("最高连板", str(limits.get("height", "—")), True)],
                   "不含 ST。连板高度来自 limit_times。", story=mood["qa"][2]["detail"]),
        ] + ([_block("stage", "情绪阶段", "近 20 个交易日每天所处阶段", mood["stage"], "今日", mood["stage"], mood["qa"][3]["tone"], [],
                     "冰点 → 修复 → 发酵 → 高潮 → 退潮，一圈通常几周。按近 3 天涨停数、跌停数和昨涨停今日平均涨跌判，阈值是起始值，会按行情校准。",
                     story=mood["qa"][3]["detail"], chart={"kind": "stages", "points": mood["stages"]})] if mood.get("stage") else [])}],
        "footnote": "「好不好做」看的是买进去容不容易赚钱，不看钱从哪来（那是「资金」和「国家队」）。",
    }


def _sector_tab(parts: dict) -> dict:
    rows = _sector_rows(parts["sectors"], parts.get("catalysts"))
    return {
        "title": "板块", "label": "", "tone": "flat", "age": "昨日", "summary": _sector_headline(rows),
        "rows": rows, "windows": parts["sectors"].get("windows"), "conclusion_days": parts["sectors"].get("conclusion_days"),
        "sw": (parts["sw"] or {}), "margin_industry": (parts["margin_industry"] or {}).get("rows") or [],
        "footnote": "板块只回答「钱往哪去」，不改大盘档位。每个板块取一只代表 ETF，板块结论用近 2 周（10 日）；份额 T+1。",
    }


def _cal_tab(parts: dict, day: date) -> dict:
    card = _calendar_card(parts, parts["watch"], day)
    return {
        "title": "日历", "label": card["label"], "tone": card["tone"], "age": "今日",
        "summary": f"未来两周{card['label']}" + (f"，有 {card['mine_count']} 件涉及你的自选" if card["mine_count"] else ""),
        "rows": card["rows"], "unlock": card["unlock"], "days": (parts["calendar"] or {}).get("days") or [],
        "footnote": card["scope"],
    }


def _macro_tab(parts: dict) -> dict:
    card = _macro_card(parts)
    return {
        "title": "宏观", "label": card["label"], "tone": card["tone"], "age": card["month"],
        "summary": card["headline"], "card": card, "formula": card["formula"],
        "footnote": "两个容易误读的地方：估值便宜但钱没来，便宜决定能放多少，钱决定什么时候加；盈利好但货币没活，等 M1 拐头。",
    }


def display_amount(value) -> str:
    return _yi(value)


def display_pct(value) -> str:
    return _pct(value, 2)
