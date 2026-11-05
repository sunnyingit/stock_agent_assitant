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
from app.services import market_disk
from app.services.market_tape import build, judge_pulse, judge_turnover
from app.tools import cache

_ZONE = ZoneInfo("Asia/Shanghai")
_BUNDLE_TTL = 300
_BUNDLE_LOCK = threading.Lock()
_REFRESH_LOCK = threading.Lock()
_REFRESHING: set[str] = set()
_GENERATION = 0
# 首屏用得到的。南向、Shibor、新发基金、行业两融只在点开右侧时再取。
CORE = (
    "turnover", "etf", "margin", "supply", "breadth", "limits", "index", "sw",
    "macro", "valuation", "fx", "us", "hk", "ipo", "calendar", "unlock",
)
LAZY = ("south", "shibor", "funds", "margin_industry")
PARTS = CORE + LAZY
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


_SPANS = (1, 3, 5, 30, 60)


def _stored(bucket, n: int):
    if not isinstance(bucket, dict):
        return None
    if n in bucket:
        return bucket[n]
    return bucket.get(str(n))


def _etf_marks(etf: dict) -> list[int]:
    label, windows = etf.get("label"), etf.get("windows")
    marks = []
    for n in (5, 30, 60):
        value = _stored(windows, n)
        if value is None:
            continue
        if label == "净流入" and value >= 0.01:
            marks.append(n)
        elif label == "净流出" and value <= -0.01:
            marks.append(n)
    return marks


def _margin_marks(margin: dict) -> list[int]:
    label, windows = margin.get("label"), margin.get("windows")
    day30, day60 = _stored(windows, 30), _stored(windows, 60)
    if label == "加速下降":
        return [n for n, value in ((30, day30), (60, day60)) if value is not None and value < -0.05]
    if label == "在降" and day30 is not None and day30 < -0.02:
        return [30]
    if label == "在升" and day30 is not None and day30 > 0.02:
        return [30, 60] if day60 is not None and day60 > 0 else [30]
    return []


def _span_stats(bucket, fmt, mark=()) -> list[dict]:
    """1 / 3 / 5 / 30 / 60 个交易日，同一排。mark 里的档位是这次判断用上的。"""
    marks = {mark} if isinstance(mark, int) else set(mark or [])
    return [_stat(f"{n} 日", fmt(_stored(bucket, n)), n in marks) for n in _SPANS]


# ---------------------------------------------------------------- 取数
def _fetch(name: str, day: date) -> tuple[str, dict]:
    try:
        return name, build(name, day)
    except Exception:
        return name, {"metrics": {}, "text": "", "facts": [], "unavailable": ["没有取到"]}


def _bundle_key(day: date) -> str:
    return f"market_page:bundle:{day.isoformat()}"


def _store_bundle(payload: dict, day: date, generation: int) -> dict:
    with _REFRESH_LOCK:
        if generation != _GENERATION:
            return payload
    cache.set(_bundle_key(day), payload, _BUNDLE_TTL)
    market_disk.save_bundle(day.isoformat(), payload)
    return payload


def bundle(today: date | None = None) -> dict:
    """首屏切片。内存或磁盘里有今天的结果就直接给；过了 5 分钟在后台重算。没有今天的就先给上一份。"""
    day = today or datetime.now(_ZONE).date()
    key = _bundle_key(day)
    cached = cache.get(key)
    if cached is not None:
        return cached
    disk = market_disk.load_bundle(day.isoformat())
    age = market_disk.bundle_age(day.isoformat())
    if disk is not None and age is not None and age < _BUNDLE_TTL:
        cache.set(key, disk, _BUNDLE_TTL)
        return disk
    if disk is not None:
        _kick(day)
        return disk
    with _BUNDLE_LOCK:
        cached = cache.get(key)
        if cached is not None:
            return cached
        older = market_disk.latest_bundle()
        if older and older.get("_day") != day.isoformat():
            _kick(day)
            return older
        with _REFRESH_LOCK:
            generation = _GENERATION
        return _store_bundle(_compute(day), day, generation)


def _warm_members() -> None:
    from app.services import market_tape

    market_tape._member_map()


def _compute(day: date) -> dict:
    with ThreadPoolExecutor(max_workers=4) as pool:
        part_futures = [pool.submit(_fetch, name, day) for name in CORE]
        sector_future = pool.submit(_sector, day)
        member_future = pool.submit(_safe, _warm_members, None)
        extra = {
            "team": pool.submit(_safe, lambda: market_team.build(day), {"ok": False}),
            "announce": pool.submit(_safe, market_team.announcements, {"items": [], "ok": False}),
            "emotion": pool.submit(_safe, lambda: market_emotion.build(day), {"ok": False, "days": []}),
            "catalysts": pool.submit(_safe, market_sector.catalysts, {}),
        }
        docs = dict(future.result() for future in part_futures)
        result = {name: doc.get("metrics") or {} for name, doc in docs.items()}
        result.update({name: future.result() for name, future in extra.items()})
        result["_text"] = {name: doc.get("text") or "" for name, doc in docs.items()}
        result["sectors"] = sector_future.result()
        member_future.result()
    result["watch"] = _watch(result["sectors"], day)
    result["personal"] = _personal(result["watch"], day)
    result["_day"] = day.isoformat()
    result["_prev"] = _prev_trade_day(day)
    return result


def _kick(day: date) -> None:
    token = day.isoformat()
    with _REFRESH_LOCK:
        if token in _REFRESHING:
            return
        _REFRESHING.add(token)
        generation = _GENERATION

    def run() -> None:
        try:
            payload = _compute(day)
            if _store_bundle(payload, day, generation) is payload and generation == _GENERATION:
                try:
                    market_notify.record(summarize(payload, day), payload)
                except Exception:
                    pass
        finally:
            with _REFRESH_LOCK:
                _REFRESHING.discard(token)

    threading.Thread(target=run, daemon=True).start()


def ensure(parts: dict, names: tuple[str, ...], day: date) -> None:
    """右侧 tab 才用到的切片。补进当前这份结果，刷新进行中时不写回磁盘。"""
    missing = [name for name in names if name not in parts]
    if not missing:
        return
    with ThreadPoolExecutor(max_workers=4) as pool:
        docs = dict(pool.map(lambda name: _fetch(name, day), missing))
    text = parts.setdefault("_text", {})
    for name, doc in docs.items():
        parts[name] = doc.get("metrics") or {}
        text[name] = doc.get("text") or ""
    token = parts.get("_day") or day.isoformat()
    with _REFRESH_LOCK:
        refreshing = token in _REFRESHING
    if refreshing:
        return
    cache.set(f"market_page:bundle:{token}", parts, _BUNDLE_TTL)
    market_disk.save_bundle(token, parts)


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
        by_name = {row["name"]: row for row in sectors.get("rows") or []}
        items = []
        for row in store.watch_list():
            code = row["ts_code"]
            info = _industry_info(code, market_tape)
            l1 = info.get("l1_name", "")
            sector_name = market_sector.sector_for(l1, info.get("l2_name", ""), info.get("l3_name", ""))
            sector = by_name.get(sector_name or "")
            record = store.latest(code)
            page = watchlist.shown_page(record, mode) if record else None
            lean = ((page or {}).get("direction") or {}).get("lean")
            verdict = sector["verdict"] if sector else None
            action, reason = rules.watch_action(verdict, lean)
            items.append({
                "ts_code": code, "name": row["name"], "industry": l1 or "—",
                "sector": sector_name or "", "verdict": verdict, "phase": sector["phase"] if sector else None,
                "direction": {"bullish": "偏多", "bearish": "偏空", "unclear": "不明"}.get(lean, "还没有判断"),
                "action": action, "reason": reason,
            })
        return items
    except Exception:
        return []


def _industry_info(code: str, tape) -> dict:
    """这只股票的申万一二三级行业名。和持仓页共用同一份缓存。"""
    try:
        rows = tape.cached(f"holdind:{code}", lambda: tape.pro().index_member_all(ts_code=code, is_new="Y"))
    except Exception:
        rows = []
    row = rows[0] if rows else {}
    return {key: str(row.get(key) or "") for key in ("l1_name", "l2_name", "l3_name")}


# ---------------------------------------------------------------- 结论
def conclusion(turnover: str, etf: str, margin: str) -> dict:
    """短期「钱」的结论只看成交、宽基份额和两融。南向、流动性和增减持不改这一档。"""
    lean = judge_pulse(turnover, etf, margin)
    gate = "通过" if lean == "在进" else "不过"
    toward_in = []
    if etf != "净流入":
        toward_in.append("宽基 5 日超过 +1% 且至少 4 天在买，同时 30 日和 60 日也超过 +1%")
    if turnover == "冷清":
        toward_in.append("1 / 3 / 5 / 30 日成交里，冷清不到三档")
    toward_weak_met = []
    toward_weak_open = []
    if margin in ("在降", "加速下降"):
        toward_weak_met.append(f"两融{margin}")
    else:
        toward_weak_open.append("两融 30 日下降超过 2%")
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
    vote = rules.trend_vote(indexes)
    if not vote:
        return "没有", "指数日线不够 60 天"
    known = [row for row in indexes if row.get("above_ma60") is not None]
    below = [row["name"] for row in known if not row["above_ma60"]]
    above = [row["name"] for row in known if row["above_ma60"]]
    broke = [row["name"] for row in known if row.get("broke_low")]
    bits = []
    if vote["kind"] == "急跌":
        bits.append("、".join(vote["plunged"]) + "短期急跌（3 日跌超 4% 或 5 日跌超 6%）")
    if above:
        bits.append("、".join(above) + "在 60 日线上")
    if below:
        bits.append("、".join(below) + "在 60 日线下")
    if vote["kind"] == "收复":
        bits.append(f"但 {vote['above20']} 个已收复 20 日线，是在反弹，先按震荡看")
    if broke:
        bits.append("、".join(broke) + "已跌破前低")
    return vote["word"], "；".join(bits)


def _volume(turn: dict) -> dict | None:
    """今天成交额跟前 20 个交易日均值比，再看近一年分位。"""
    series = [point["value"] for point in (turn or {}).get("series") or [] if point.get("value") is not None]
    if len(series) < 21:
        return None
    return rules.volume_state(series[-1], series[-21:-1], turn.get("pctile"))


def _theme_view(last: dict) -> dict | None:
    """涨停最集中在哪几个行业，以及昨天的主线今天赚不赚钱。"""
    themes = last.get("themes") or []
    if not themes:
        return None
    share, follow = last.get("theme_share"), last.get("theme_follow")
    if follow is not None and follow <= rules.THEME_BREAK:
        word = "主线在退"
    elif share is not None and share >= 0.35:
        word = "有主线"
    elif share is not None and share <= 0.20:
        word = "没主线"
    else:
        word = "一般"
    detail = "今天涨停最集中：" + "、".join(f"{item['name']} {item['n']} 家" for item in themes)
    if share is not None:
        detail += f"（前三合计占 {round(share * 100)}%）"
    if follow is not None:
        detail += f"；昨天的主线今天平均 {follow:+.2f}%（{last.get('theme_n', 0)} 只，20cm 折成 10cm）"
    return {"word": word, "detail": detail, "tone": {"有主线": "good", "主线在退": "bad"}.get(word, "flat")}


def _mood_notes(indexes: list[dict], breadth: dict, limits: dict, emotion: dict, volume: dict | None = None) -> list[str]:
    """几种 A 股常见的状态，只说「现在是什么状态、要看什么」，不给买卖指令。"""
    notes = []
    vote = rules.trend_vote(indexes) or {}
    up, down = breadth.get("up"), breadth.get("down")
    share = up / (up + down) if up is not None and down is not None and (up + down) else None
    last = (emotion or {}).get("last") or {}
    shanghai = next((row for row in indexes if row.get("name") == "上证指数"), None) or {}
    move = shanghai.get("pct")
    heavy = bool(volume) and volume["word"] in ("放量", "天量")
    light = bool(volume) and volume["word"] in ("缩量", "地量")
    if vote.get("kind") == "急跌":
        notes.append("急剧下跌：急跌后的第一天常有反抽，但这时追涨没有安全垫；要看第二天昨天涨停的票能不能转正。")
    if share is not None and share >= rules.THRUST and vote.get("word") in ("向下", "震荡") and vote.get("above", 0) * 4 < vote.get("known", 4) * 3:
        tail = "放量反抽，资金参与度比缩量反弹高；" if heavy else "缩量反弹，持续性要打问号；" if light else ""
        notes.append(f"普涨 {round(share * 100)}% 但指数还没站回 60 日线：多是下跌或急跌之后的反抽日。{tail}A 股常说「反抽不等于反转」，要看下一天能不能站稳、涨停股有没有接力。")
    if move is not None and move <= -1.0 and volume:
        if heavy:
            notes.append("放量下跌：分歧大，抛压还没衰竭。")
        elif light:
            notes.append("缩量下跌：抛压在减弱，但缩量不等于见底，要等放量企稳。")
    if volume and volume["word"] == "地量":
        notes.append("成交额处在近一年最低的 10%：常被说「地量见地价」，但地量可以持续，要等放量确认。")
    if volume and volume["word"] == "天量":
        notes.append("成交额处在近一年最高的 10%：天量之后常有分歧，要看后面能不能守住。")
    if last.get("down") is not None and last["down"] >= rules.PANIC_DOWN:
        notes.append(f"跌停 {last['down']} 家：恐慌日。常被当作情绪低点，但真正的修复要等昨涨停重新赚钱。")
    if (emotion or {}).get("stage") == "冰点":
        extra = "，且放量，恐慌还在释放" if heavy else "，且缩量，抛压在衰竭" if light else ""
        notes.append(f"冰点：亏钱效应集中{extra}。是不是低点，要看下一天昨涨停是否转正、跌停是否明显减少；转正之前只能算还在冰点。")
    if last.get("height_drop"):
        notes.append(f"高标断板：最高连板从 {last['height_prev']} 板掉到 {last['height']} 板，常被当作退潮的信号。")
    theme = last.get("theme_follow")
    if theme is not None and theme <= rules.THEME_BREAK:
        notes.append(f"主线大面：昨天涨停最集中的行业今天平均 {theme:+.2f}%，接力常在这时断掉。")
    return notes


def _money_signal(parts: dict) -> dict:
    turn, etf, margin = parts["turnover"], parts["etf"], parts["margin"]
    labels = (turn.get("label") or "没有", etf.get("label") or "没有", margin.get("label") or "没有")
    if "没有" in labels:
        head = {
            "label": "没有", "gate": "不过", "gate_text": "数据没齐",
            "summary": "成交、宽基或两融有一项没取到，这一档先不算进总判断。",
            "toward_in": [], "toward_weak_met": [], "toward_weak_open": [],
        }
        return {"head": head, "tier": None, "tone": "flat"}
    head = conclusion(*labels)
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
    known = [row for row in indexes if row.get("above_ma60") is not None]
    above_n = sum(1 for row in known if row["above_ma60"])
    qa = [
        {"q": "大盘趋势怎么样？", "a": trend_word, "detail": trend_text or "没有取到指数", "tone": _tone3(trend_word, "向上", "向下"),
         "facts": [{"k": "60 日线上", "v": f"{above_n}/{len(known)}" if known else "没有", "tone": _tone3(trend_word, "向上", "向下")}]},
        {"q": "多数股票在涨吗？", "a": wide, "detail": (
            f"今天 {breadth.get('up', '—')} 涨、{breadth.get('down', '—')} 跌"
            + ("" if share is None else f"，上涨占 {round(share * 100)}%")
            + f"；近 25 个交易日新高 {breadth.get('highs', '—')}，新低 {breadth.get('lows', '—')}"
        ), "tone": _tone3(wide, "有", "没有"),
         "facts": ([{"k": "上涨", "v": f"{round(share * 100)}%", "tone": _tone3(wide, "有", "没有")}] if share is not None else [])
         + [{"k": "新高/新低", "v": f"{breadth.get('highs', '—')}/{breadth.get('lows', '—')}", "tone": "flat"}]},
        {"q": "短线追涨能赚钱吗？", "a": chase, "detail": (
            f"涨停 {limits.get('up', '—')} / 跌停 {limits.get('down', '—')}，最高 {limits.get('height', '—')} 板，炸板率 "
            + ("没有" if broken is None else f"{round(broken * 100)}%")
            + ("" if follow is None else f"；昨天涨停的 {limits.get('follow_count', 0)} 只今天平均 {follow:+.2f}%")
        ), "tone": _tone3(chase, "能", "难"),
         "facts": ([{"k": "昨涨停", "v": f"{follow:+.2f}%".replace("-", "−"), "tone": "good" if follow > 0 else "bad" if follow < 0 else "flat"}] if follow is not None else [])
         + ([{"k": "炸板", "v": f"{round(broken * 100)}%", "tone": "bad" if broken > 0.40 else "flat"}] if broken is not None else [])},
    ]
    emotion = parts.get("emotion") or {}
    if emotion.get("ok"):
        last = emotion["last"]
        follow = last.get("follow")
        qa.append({
            "q": "短线情绪走到哪了？", "a": emotion["stage"], "tone": {"发酵": "good", "高潮": "warn", "退潮": "bad", "冰点": "bad"}.get(emotion["stage"], "flat"),
            "facts": [{"k": "涨停", "v": str(last.get("up", "—")), "tone": "good"}, {"k": "跌停", "v": str(last.get("down", "—")), "tone": "bad" if last.get("down") else "flat"}],
            "detail": f"涨停 {last['up']} / 跌停 {last['down']}，最高 {last['height']} 板" + ("" if follow is None else f"，昨涨停今日 {follow:+.2f}%")
            + ("" if last.get("follow_board") is None or last.get("board_n", 0) < 3 else f"（其中昨天连板的 {last['board_n']} 只 {last['follow_board']:+.2f}%）")
            + "；按当天的数字判，再看前一天在哪一段",
        })
    volume = _volume(parts.get("turnover") or {})
    if volume:
        qa.append({
            "q": "成交量怎么样？", "a": volume["word"], "tone": "flat",
            "facts": [{"k": "今天", "v": _yi(volume["amount"]), "tone": "flat"}, {"k": "较前 20 日", "v": f"{round(volume['ratio'] * 100)}%", "tone": "flat"}],
            "detail": f"今天 {_yi(volume['amount'])}，是前 20 个交易日均值（{_yi(volume['base'])}）的 {round(volume['ratio'] * 100)}%"
                      + ("" if volume["pctile"] is None else f"；处在近一年的 {round(volume['pctile'] * 100)}% 分位"),
        })
    theme_view = _theme_view(emotion["last"]) if emotion.get("ok") else None
    if theme_view:
        top = (emotion.get("last") or {}).get("themes") or []
        theme_facts = []
        if top:
            theme_facts.append({"k": top[0]["name"], "v": f"{top[0]['n']} 家", "tone": theme_view["tone"]})
        share = (emotion.get("last") or {}).get("theme_share")
        if share is not None:
            theme_facts.append({"k": "前三占比", "v": f"{round(share * 100)}%", "tone": theme_view["tone"]})
        qa.append({"q": "涨停集中在哪几个行业？", "a": theme_view["word"], "tone": theme_view["tone"], "facts": theme_facts, "detail": theme_view["detail"]})
    means = {
        "顺": "环境对做多友好，但仍要挑有新钱的板块。",
        "平": "涨跌互有，选股比选时点重要。",
        "逆": "现在买进去，大概率要先扛一段；钱只往有新钱的板块去。",
    }.get(tier, "没有足够的数据判断。")
    notes = _mood_notes(indexes, breadth, limits, emotion, volume)
    if notes:
        means = means + " " + " ".join(notes)
    when = {
        "顺": "多数指数跌回 60 日线下、或上涨家数不足四成 → 「好不好做」转平",
        "平": "多数指数站上 60 日线、上涨占五成五以上、追涨开始有收益 → 转顺",
        "逆": "多数指数站回 60 日线、上涨家数过半 → 「好不好做」转平",
    }.get(tier, "")
    return {
        "tier": tier, "label": signal["label"], "tone": {"顺": "good", "平": "warn", "逆": "bad"}.get(tier, "flat"),
        "reasons": signal["reasons"], "summary": "；".join(signal["reasons"]) or "没有足够的数据",
        "qa": qa, "means": means, "when": when, "date": breadth.get("date") or limits.get("date") or "",
        "stage": emotion.get("stage") if emotion.get("ok") else None, "volume": volume, "theme": theme_view,
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
    margin_now = _pct(_stored(margin.get("windows"), 30) if margin.get("windows") else margin.get("ratio4"))
    margin5 = _stored(margin.get("windows"), 5)
    better.append({"short": "宽基 5 日", "text": "宽基 ETF 近 5 天净流入超过 1%，且至少 4 天在买", "met": etf_met,
                   "now": f"现在 {_pct(etf.get('ratio'), 2)}，{etf.get('buy_days', '—')} 天在买"})
    if turn.get("label") == "冷清":
        better.append({"short": "成交回暖", "text": "成交回到平时水平，不再冷清", "met": False, "now": f"现在近 5 日 {_pct(turn.get('ratio5'))}"})
    better.append({"short": "两融止跌", "text": "两融近 5 日不再减少，不低于 −2% 算止跌", "met": margin5 is not None and margin5 >= -0.02,
                   "now": "现在没有" if margin5 is None else f"现在 5 日 {_pct(margin5)}"})
    broken = [row["name"] for row in indexes if row.get("broke_low")]
    worse.append({"short": "两融 30 日", "text": "借钱炒股的 30 日减少超过 5%", "met": margin.get("label") == "加速下降", "now": f"现在 30 日 {margin_now}"})
    worse.append({"short": "跌破前低", "text": "指数跌破前低", "met": bool(broken), "now": "现在 " + ("、".join(broken) + "已破" if broken else "都没破")})
    worse.append({"short": "成交冷清", "text": "成交转为冷清", "met": turn.get("label") == "冷清", "now": f"现在近 5 日 {_pct(turn.get('ratio5'))}"})
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
        {"key": "value", "name": "估值", "text": value_text, "age": "近 3 年" if (valuation.get("days") or 0) >= 700 else "样本内", "score": value, "weight": 0.20},
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
    unlock = parts.get("unlock") or {}
    if unlock.get("count"):
        rows.append({
            "date": today.isoformat(),
            "text": f"未来 10 天解禁 {unlock['count']} 条，合计 {unlock['total']:.2f} 亿股" + ("（单次最多 6000 条，没算完）" if unlock.get("capped") else ""),
            "tag": "解禁",
        })
    mine = list(parts["personal"]) if "personal" in parts else _personal(watch, today)
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


def _personal(watch: list[dict], today: date) -> list[dict]:
    return _my_unlocks(watch, today) + _my_disclosures(watch, today)


def _my_disclosures(watch: list[dict], today: date) -> list[dict]:
    """自选股未来两周的定期报告披露日（预约日期）。"""
    if not watch:
        return []
    from app.services import market_tape

    periods = [f"{today.year - 1}1231", f"{today.year}0331", f"{today.year}0630", f"{today.year}0930", f"{today.year}1231"]
    end = (today + timedelta(days=14)).isoformat()
    jobs = [(row, period) for row in watch for period in periods]

    def one(job: tuple[dict, str]) -> list[dict]:
        row, period = job
        try:
            rows = market_tape.cached(
                f"discl:{row['ts_code']}:{period}",
                lambda c=row["ts_code"], p=period: market_tape.pro().disclosure_date(end_date=p, ts_code=c),
            )
        except Exception:
            return []
        found = []
        for item in rows:
            when = market_tape.iso(item.get("actual_date") or item.get("pre_date"))
            if when and today.isoformat() <= when <= end:
                found.append({"date": when, "text": f"{row['name']} {_PERIOD_NAME.get(period[4:], '定期报告')}披露", "tag": "涉及自选"})
        return found

    out = []
    for found in market_tape._parallel(jobs, one):
        out.extend(found)
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
BUCKET_ORDER = [item[0] for item in rules.BUCKETS]


def _sector_rows(sectors: dict, catalysts: dict | None = None) -> list[dict]:
    """每个板块一行，按「钱 → 价 → 热度」的分组排序：同一组里钱进得越异常越靠前。"""
    rows = []
    for row in sectors.get("rows") or []:
        flow = row["flow"].get(sectors.get("conclusion_days", 5))
        phase = str(row.get("phase") or "")
        rows.append({
            "name": row["name"], "code": row["code"], "industry": row["industry"], "group": row.get("group", ""), "source": row.get("source", "etf"),
            "flow": flow, "flow_text": _yi(flow, True), "flows": {str(key): value for key, value in row["flow"].items()},
            "flow_rate": row.get("flow_rate"), "size": row.get("size"), "money_z": row.get("money_z"),
            "money_z3": row.get("money_z3"), "heat3": row.get("heat3"), "flow3": (row.get("flow") or {}).get(3),
            "ret5": row.get("ret5"), "ret20": row.get("ret20"), "ex5": row.get("ex5"), "ex20": row.get("ex20"),
            "heat": row["heat"], "heat_text": "—" if row["heat"] is None else f"{row['heat']:.1f}×", "position": row.get("position"),
            "lever": row["lever"], "lever_word": row["lever_word"],
            "verdict": row["verdict"], "tone": row["tone"], "phase": phase, "bucket": row.get("bucket", "flat"), "why": row.get("why", ""),
            "meaning": rules.MEANING.get(phase.split("·")[0], ""),
            "catalyst": (catalysts or {}).get(row["name"]),
        })
    rows.sort(key=lambda item: (BUCKET_ORDER.index(item["bucket"]) if item["bucket"] in BUCKET_ORDER else 99, -(item["money_z"] if item["money_z"] is not None else -99)))
    return rows


def _sector_buckets(rows: list[dict]) -> list[dict]:
    return [{"key": key, "title": title, "tone": tone, "names": [row["name"] for row in rows if row["bucket"] == key]} for key, title, tone in rules.BUCKETS
            if any(row["bucket"] == key for row in rows)]


def _sector_headline(rows: list[dict]) -> str:
    """一句话：钱往哪走、哪些别追。每组最多说三个。"""
    if not rows:
        return "没有取到板块数据"
    pick = lambda key, n=3: [row["name"] for row in rows if row["bucket"] == key][:n]   # noqa: E731
    bits = []
    for key, text in (("main", "有钱又热"), ("next", "钱先到、价还没热"), ("dip", "价在跌、钱却在进（抄底）"), ("hot_only", "涨着但没新钱，别追"), ("out", "钱在撤")):
        names = pick(key)
        if names:
            bits.append(f"{text}：{'、'.join(names)}")
    return "；".join(bits) or "钱进出都不明显，板块之间没有拉开差距"


# ---------------------------------------------------------------- 国家队
# 在出手发生在大跌里，说明市场正弱，托底不等于顺风，所以只展示不加分；托底的钱撤走才扣分
_TEAM_TIER = {"在出手": "平", "没出手": "平", "可能在卖": "逆"}
_TEAM_TONE = {"在出手": "good", "没出手": "flat", "可能在卖": "bad"}


def _team_signal(team: dict) -> dict:
    if not team.get("ok"):
        return {"key": "team", "n": "②", "name": "国家队", "tier": None, "label": "没取到", "tone": "flat", "text": team.get("text") or "份额数据没取到，不参与总档位"}
    return {"key": "team", "n": "②", "name": "国家队", "tier": _TEAM_TIER[team["label"]], "label": team["label"], "tone": _TEAM_TONE[team["label"]], "text": _team_headline(team)}


def _team_event(last: dict) -> str:
    """一次出手发生了什么，不带术语：上证怎么样、9 只 ETF 买了多少、买的是什么、在近 60 天里算不算猛。"""
    move = last["index_pct"]
    market = f"上证{'跌' if move < 0 else '涨'} {abs(move):.1f}%"
    if last.get("weak") == "连跌":
        market += f"，近 3 日累计跌 {abs(last['cum3']):.1f}%"
    elif last.get("weak") == "承接":
        market += f"，但刚承过压（近 3 日累计 {last['cum3']:+.1f}%，最弱一天 {last['worst3']:+.1f}%）"
    names, positive = last.get("names", 9), last.get("positive")
    buying = f"{names} 只大盘 ETF 里 {positive} 只当天在净申购" if positive is not None else f"{names} 只大盘 ETF 当天一起大额申购"
    rank = last.get("rank")
    size = "" if not rank else ("，是近 60 个交易日里最多的一次" if rank == 1 else f"，在近 60 个交易日里排第 {rank}")
    blue = "" if last.get("blue_share") is None else f"，其中大盘宽基占 {last['blue_share'] * 100:.0f}%"
    text = f"{market}；{buying}，合计净申购约 {last['delta']:.0f} 亿元{size}{blue}"
    if last.get("weak") == "极猛":
        text += "。上证跌得不算多，但这笔买入是近 60 天里最猛的，所以也算"
    if last.get("streak", 1) > 1:
        text += f"。这是连续出手的第 {last['streak']} 次"
    return text


def _team_sell_event(sell: dict) -> str:
    move = sell["index_pct"]
    return (f"近 3 个交易日里有 {sell['days']} 天，{sell['names']} 只大盘 ETF 里 {sell['negative']} 只在被净赎回；"
            f"{sell['day'][5:]} 合计净赎回约 {abs(sell['delta']):.0f} 亿元，大盘宽基占 {sell['blue_share'] * 100:.0f}%，"
            f"当天上证{'跌' if move < 0 else '涨'} {abs(move):.1f}%，不是恐慌割肉的样子，更像是有大资金在减仓")


def _team_headline(team: dict) -> str:
    last, hold, sell = team.get("last"), team.get("hold"), team.get("sell")
    if team["label"] == "在出手":
        return (f"出手了：{last['day'][5:]} {_team_event(last)}。出手后 5 个交易日内算「在出手」，"
                f"今天是第 {last['since'] + 1} 天，信号会逐天变弱")
    if team["label"] == "可能在卖":
        if sell and sell["since"] < 5:
            return f"可能在卖：{_team_sell_event(sell)}。只看份额，分不清是不是国家队"
        return f"之前托底的钱在撤：出手后新增的份额，9 只 ETF 中间已经还回去 {hold['gone'] * 100:.0f}%，超过 3 成"
    if last:
        return f"最近没出手。上一次是 {last['day'][5:]}（{last['since']} 个交易日前）"
    return "近一年没有出现「市场承压、大盘 ETF 一起大额买入」的日子"


def _signed(value: float, digits: int = 1, suffix: str = "%") -> str:
    return f"{value:+.{digits}f}{suffix}".replace("-", "−")


def _last_facts(last: dict | None) -> list[dict]:
    if not last:
        return []
    facts = [
        {"k": "出手日", "v": last["day"][5:], "tone": "flat"},
        {"k": "上证", "v": _signed(last["index_pct"]), "tone": "bad" if last["index_pct"] < 0 else "good"},
    ]
    if last.get("weak") == "承接" and last.get("cum3") is not None:
        facts.append({"k": "近 3 日", "v": _signed(last["cum3"]), "tone": "flat"})
        facts.append({"k": "最弱一天", "v": _signed(last["worst3"]), "tone": "bad"})
    elif last.get("weak") == "连跌" and last.get("cum3") is not None:
        facts.append({"k": "近 3 日", "v": _signed(last["cum3"]), "tone": "bad"})
    if last.get("positive") is not None:
        facts.append({"k": "当天在买", "v": f"{last['positive']}/{last.get('names', 9)}", "tone": "good"})
    facts.append({"k": "净申购", "v": f"{last['delta']:.0f} 亿", "tone": "good" if last["delta"] > 0 else "bad"})
    if last.get("blue_share") is not None:
        facts.append({"k": "宽基占比", "v": f"{last['blue_share'] * 100:.0f}%", "tone": "flat"})
    if last.get("rank"):
        facts.append({"k": "近 60 日", "v": "最多一次" if last["rank"] == 1 else f"第 {last['rank']}", "tone": "flat"})
    if last.get("signal") is not None:
        facts.append({"k": "异常度", "v": f"{last['signal']:.1f}", "tone": "flat"})
    if last.get("limit") is not None:
        facts.append({"k": "异常门槛", "v": f"{last['limit']:.1f}", "tone": "flat"})
    if last.get("money_limit") is not None:
        facts.append({"k": "金额门槛", "v": f"{last['money_limit']:.0f} 亿", "tone": "flat"})
    facts.append({"k": "距今", "v": f"{last['since']} 天", "tone": "good" if last["since"] < 5 else "flat"})
    if last.get("streak", 1) > 1:
        facts.append({"k": "连续", "v": f"第 {last['streak']} 次", "tone": "good"})
    return facts


def _sell_facts(sell: dict | None) -> list[dict]:
    if not sell:
        return []
    return [
        {"k": "减仓日", "v": sell["day"][5:], "tone": "bad"},
        {"k": "上证", "v": _signed(sell["index_pct"]), "tone": "flat"},
        {"k": "净赎回", "v": f"{abs(sell['delta']):.0f} 亿", "tone": "bad"},
        {"k": "在赎回", "v": f"{sell['negative']}/{sell['names']}", "tone": "bad"},
        {"k": "宽基占比", "v": f"{sell['blue_share'] * 100:.0f}%", "tone": "flat"},
        {"k": "近 3 日", "v": f"{sell['days']} 天", "tone": "bad"},
    ]


def _team_card(team: dict, announce: dict) -> dict:
    if not team.get("ok"):
        return {"pending": True, "text": team.get("text") or "份额数据没取到，这一项暂时判不了。"}
    last, hold, counts = team.get("last"), team.get("hold"), team["counts"]
    tone = _TEAM_TONE[team["label"]]
    if last:
        tech = ""
        if last.get("money_limit") is not None and last.get("signal") is not None and last.get("limit") is not None:
            tech = (f"（技术细节：各只申购跟自己平时比的异常度，中间值 {last['signal']:.1f}，超过 {last['limit']:.1f} 才算异常；"
                    f"合计净申购要超过 {last['money_limit']:.0f} 亿元）")
        first = (f"{last['day'][5:]} 出过一次手：{_team_event(last)}"
                 + (f"；之后 {last['since']} 个交易日没有再出现。" if last["since"] > 0 else "。")
                 + tech
                 + (f"。出手后 5 个交易日内算「在出手」，今天是第 {last['since'] + 1} 天，信号会逐天变弱" if team["label"] == "在出手" else ""))
    else:
        first = "近一年没有出现「市场承压、大盘 ETF 一起大额买入」的日子。"
    gone = None if not hold else hold["gone"]
    count_facts = [{"k": f"{n} 日", "v": f"{_stored(counts, n) or 0} 次", "tone": "good" if _stored(counts, n) else "flat"} for n in (1, 3, 5, 30, 60)]
    hold_facts = [] if not hold else [
        {"k": "出手日", "v": f"{hold['bought']:.0f} 亿", "tone": "flat"},
        {"k": "之后累计", "v": f"{hold['post']:+.0f} 亿".replace("-", "−"), "tone": "bad" if hold["post"] < 0 else "good" if hold["post"] > 0 else "flat"},
        {"k": "已还回去", "v": "没有" if hold["gone"] is None else f"{hold['gone'] * 100:.0f}%", "tone": "bad" if hold["sold"] else "flat"},
        {"k": "之后天数", "v": f"{hold['days']} 天", "tone": "flat"},
    ]
    rows = [
        {"q": "最近有没有出手托底？", "a": f"近 5 天 {_stored(counts, 5) or 0} 次", "tone": "good" if _stored(counts, 5) else "flat",
         "facts": count_facts + _last_facts(last), "detail": first},
        {"q": "之前买进去的钱还在吗？", "a": "没有出手记录" if not hold else ("撤走超过 3 成" if hold["sold"] else "有一部分撤走" if gone > 0.15 else "大部分还在"),
         "tone": "bad" if hold and hold["sold"] else "flat", "facts": hold_facts,
         "detail": "没有可追踪的出手日" if not hold else (
             f"出手日净申购 {hold['bought']:.0f} 亿元；之后 {hold['days']} 个交易日累计 {hold['post']:+.0f} 亿元。"
             f"9 只 ETF 把出手当天新增的份额还回去的比例，中间值是 {0 if hold['gone'] is None else hold['gone'] * 100:.0f}%，超过 30% 才算撤走"
         )},
        {"q": "有没有在悄悄卖？", "a": "有迹象" if team["label"] == "可能在卖" else "暂时没有迹象", "tone": "bad" if team["label"] == "可能在卖" else "good",
         "facts": _sell_facts(team.get("sell")) if team.get("sell") and team["sell"]["since"] < 5 else [],
         "detail": ((_team_sell_event(team["sell"]) + "。") if team.get("sell") and team["sell"]["since"] < 5 else
                    "两种迹象才算：出手后新增的份额被还回去超过 30%；或者大盘宽基被连续、广泛、大额地赎回（上证没大跌）。现在两种都没有。")
                   + "只看份额，不确认是谁"},
    ]
    means = {
        "在出手": "有人在市场承压时接盘，但这是跌出来的托底，不代表马上反转。",
        "可能在卖": "大盘宽基在被大额撤出，下方支撑变弱；但只看份额，分不清是国家队还是别的大资金。",
        "没出手": "有人托底但不追涨：大跌时它可能再出手，但别指望它推着市场往上走。",
    }[team["label"]]
    return {
        "pending": False, "tier": _TEAM_TIER[team["label"]], "label": team["label"], "tone": tone, "summary": _team_headline(team),
        "qa": rows, "means": means, "when": "市场承压（当天大跌，或前两天刚承压），同时大盘宽基 ETF 一起被大额买入 → 「在出手」；大盘宽基被连续大额赎回而上证没大跌，或出手后新增的份额被还回去超过 3 成 → 「可能在卖」",
        "scope": "看的是 9 只大盘 ETF 的大额净申购（份额变化 × 收盘价），这类资金通常来自国家队，不确认具体是谁；份额晚一天公布；出手只展示，不加总档位的分", "date": team.get("latest", ""),
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
        "act", "最近有没有出手", "大跌时有没有人托底", f"近 5 天 {_stored(counts, 5) or 0} 次", _age(team["latest"], day, previous), team["label"], card["tone"],
        _span_stats(counts, lambda value: f"{0 if value is None else value} 次", 5),
        "国家队出手的样子：市场承压时逆势、大额、广泛地买大盘宽基。四条同时满足才记一次出手：①市场承压——当天上证跌超 1%；或跌超 0.5% 且近 3 日累计跌超 2%；或前两天刚承压（近 3 日累计跌超 2%，或有一天跌超 1%），今天不管涨跌（护盘买进去，指数当天可能已经拉红）；②9 只 ETF 里至少 7 只当天净申购；③合计净申购和各只的申购都明显超过平时（近 60 天均值加 2 倍正常波动）；④这笔钱一半以上流向沪深 300、上证 50、中证 500、A500 这类大盘宽基，而不是科创 50、芯片这类热点。出手后 5 个交易日内显示「在出手」，信号逐天变弱。减仓看反方向：大盘宽基被广泛、大额地净赎回，上证没有大跌（恐慌割肉不算），近 3 个交易日里至少 2 天这样。只比相邻两个交易日，只看份额，不猜是谁。",
        story=card["qa"][0]["detail"], open=True,
        chart={"kind": "team", "unit": "亿元", "points": team["series"], "legend": ["出手日", "净申购", "净赎回"]},
        listing={"title": f"看的是哪几只 ETF（国家队通道 {team['counted']} 只，和「宽基观察池」不是一套）",
                 "rows": [{"left": f"{name}", "mid": code[:6], "right": "", "code": code} for code, name in market_team.TEAM_BOOK if code in team["codes"]]},
    )
    holdblock = _block(
        "hold", "买进去的钱还在吗", "有没有在悄悄卖", "没有出手记录" if not hold else f"已还回去 {0 if hold['gone'] is None else hold['gone'] * 100:.0f}%", _age(team["latest"], day, previous),
        card["qa"][1]["a"], card["qa"][1]["tone"],
        [_stat("出手日", "—" if not hold else f"{hold['bought']:.0f} 亿元"), _stat("之后累计", "—" if not hold else f"{hold['post']:+.0f} 亿元", True),
         _stat("已还回去", "—" if not hold or hold["gone"] is None else f"{hold['gone'] * 100:.0f}%")],
        "出手后，每只 ETF 把出手当天新增的份额还回去了多少（按各自的波动折算，大 ETF 不会单独把比例拉高或压低），取 9 只的中间值；超过 30% 才改成「可能在卖」。",
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
        listing={"title": "出手日 · 当天净申购 · 之后 20 日上证", "rows": [
            {"left": row["day"], "mid": f"+{row['delta']:.0f} 亿元", "right": ("进行中 · 第 %d 天" % row["progress"]) if row["ret20"] is None else _pct(row["ret20"])}
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
    if "personal" not in parts and parts.get("_day") == day.isoformat() and day.isoformat() not in _REFRESHING:
        parts["personal"] = _personal(parts.get("watch") or [], day)
        cache.set(_bundle_key(day), parts, _BUNDLE_TTL)
        market_disk.save_bundle(day.isoformat(), parts)
    summary = summarize(parts, day)
    # 后台正在用今天的数据重算时，这一份是旧结果，快照等重算完再记一次。
    if record and parts.get("_day") == day.isoformat() and day.isoformat() not in _REFRESHING:
        try:
            market_notify.record(summary, parts)
        except Exception:
            pass   # 存档失败不能让页面打不开
    return summary


def refresh(today: date | None = None) -> None:
    """让下一次取数重新算（定时任务用）。进行中的后台刷新不会把旧结果写回来。"""
    global _GENERATION
    day = today or datetime.now(_ZONE).date()
    with _REFRESH_LOCK:
        _GENERATION += 1
    cache.set(_bundle_key(day), None, 0)
    market_disk.drop(f"bundle:{day.isoformat()}")


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


def notify_payload(today: date | None = None, inbox: list[dict] | None = None) -> dict:
    day = today or datetime.now(_ZONE).date()
    parts = bundle(day)
    card = _calendar_card(parts, parts["watch"], day)
    upcoming = [
        {"tag": "提醒", "title": row["text"], "when": row["date"][5:], "weekday": row["weekday"], "tab": "cal"}
        for row in card["rows"] if row["tag"] == "涉及自选" and row["date"] >= day.isoformat()
    ]
    return market_notify.notifications(upcoming=upcoming, today=day, inbox=inbox)


def summarize(parts: dict, day: date) -> dict:
    money = _money_signal(parts)
    mood = _mood_card(parts)
    macro = _macro_card(parts)
    sector_rows = _sector_rows(parts["sectors"], parts.get("catalysts"))
    good_sectors = [row["name"] for row in sector_rows if row["bucket"] in ("main", "next")]
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
            "headline": "、".join(item["text"] for item in signals if (item["tier"] and item["tier"] != "平") or item.get("label") == "在出手") or "成交、宽基、两融和指数没有一起指向一边",
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
            "buckets": _sector_buckets(sector_rows), "coverage": parts["sectors"].get("coverage"),
            "scope": SECTOR_SCOPE,
        },
        "watch": {"rows": parts["watch"], "scope": "动作词由「板块结论 × 个股判断」按规则生成，不是买卖指令；只读已有判断，不在这里批量发起诊断"},
        "ages": {"turnover": _age((parts["turnover"] or {}).get("date", ""), day, previous), "etf": _age((parts["etf"] or {}).get("latest_date", ""), day, previous)},
    }


def _band(value, low: float, high: float, below: str, mid: str, above: str) -> str:
    if value is None:
        return "没有"
    if value < low:
        return below
    if value > high:
        return above
    return mid


def _span_words(bucket, days: tuple[int, ...], low: float, high: float, below: str, mid: str, above: str, digits: int = 1) -> str:
    bits = []
    for n in days:
        value = _stored(bucket, n)
        bits.append(f"{n} 日 {_pct(value, digits)}（{_band(value, low, high, below, mid, above)}）")
    return "，".join(bits)


def _span_facts(bucket, days: tuple[int, ...], low: float, high: float, below: str, mid: str, above: str, digits: int = 1) -> list[dict]:
    tone_of = {below: "bad", above: "good"}
    facts = []
    for n in days:
        value = _stored(bucket, n)
        word = _band(value, low, high, below, mid, above)
        facts.append({"k": f"{n} 日", "v": f"{_pct(value, digits)} {word}", "tone": tone_of.get(word, "flat")})
    return facts


def _span_facts(bucket, days: tuple[int, ...], low: float, high: float, below: str, mid: str, above: str, digits: int = 1) -> list[dict]:
    tone_of = {below: "bad", above: "good"}
    facts = []
    for n in days:
        value = _stored(bucket, n)
        word = _band(value, low, high, below, mid, above)
        facts.append({"k": f"{n} 日", "v": f"{_pct(value, digits)} {word}", "tone": tone_of.get(word, "flat")})
    return facts


def _money_card(parts: dict, money: dict) -> dict:
    turn, etf, margin, supply = parts["turnover"], parts["etf"], parts["margin"], parts["supply"]
    etf_word = {"净流入": "有", "净流出": "没有，在流出", "基本没变": "基本没有"}.get(etf.get("label"), "没有")
    taking = margin.get("label") in ("在降", "加速下降") or supply.get("label") == "在拿走"
    turn_facts = _span_facts(turn.get("windows"), (1, 3, 5, 30), -0.05, 0.05, "低于平时", "接近平时", "高于平时")
    turn_facts.append({"k": "今天", "v": _yi(turn.get("amount")), "tone": "flat"})
    etf_facts = _span_facts(etf.get("windows"), (1, 3, 5, 30, 60), -0.01, 0.01, "在减", "基本没变", "在增", 2)
    etf_facts.append({"k": "近 5 日在买", "v": f"{etf.get('buy_days', '—')} 天", "tone": "good" if (etf.get("buy_days") or 0) >= 4 else "flat"})
    etf_facts.append({"k": "净申购", "v": _yi(etf.get("money"), True), "tone": "good" if (etf.get("money") or 0) > 0 else "bad" if (etf.get("money") or 0) < 0 else "flat"})
    margin_facts = _span_facts(margin.get("windows"), (1, 3, 5, 30, 60), -0.02, 0.02, "在降", "持平", "在升")
    margin_facts.append({"k": "增减持 28 日", "v": _yi(supply.get("net"), True), "tone": "bad" if supply.get("label") == "在拿走" else "good" if supply.get("label") == "有人在接" else "flat"})
    return {
        "tier": money["tier"], "label": rules.MONEY_WORD[money["tier"]], "tone": money["tone"],
        "summary": money["head"]["summary"],
        "qa": [
            {"q": "最近交易活跃吗？", "a": {"冷清": "比平时冷清", "活跃": "比平时活跃", "平时": "和平时差不多"}.get(turn.get("label"), "没有"),
             "tone": {"冷清": "bad", "活跃": "good"}.get(turn.get("label"), "flat"),
             "facts": turn_facts,
             "detail": "1 日是今天，3 日是近 3 天，都相对 60 日均。四档里三档低于 −5% 才算冷清，三档高于 +5% 才算活跃。"},
            {"q": "有新钱进场吗？", "a": etf_word, "tone": {"净流入": "good", "净流出": "bad"}.get(etf.get("label"), "flat"),
             "facts": etf_facts,
             "detail": "1 日、3 日是最近份额变化，和 5 / 30 / 60 日一起看。算有新钱要 5 日超过 +1% 且至少 4 天在买，30 日和 60 日也都超过 +1%。"},
            {"q": "有人在拿钱走吗？", "a": "有" if taking else "暂时没有", "tone": "bad" if taking else "good",
             "facts": margin_facts,
             "detail": "1 日、3 日、5 日是近几天余额变化；加速下降要 30 日和 60 日都低于 −5%，30 日低于 −2% 算在降。回购是累计公告，不算进增减持。"},
        ],
        "means": money["head"]["summary"],
        "when": "1 / 3 / 5 / 30 日成交里冷清不到三档，并且宽基 5 日超过 +1%（至少 4 天在买）、30 日和 60 日也都超过 +1% → 「在进」",
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
        return _sector_tab(parts, day)
    if name == "mood":
        return _mood_tab(parts)
    if name == "cal":
        return _cal_tab(parts, day)
    return _macro_tab(parts)


def _block(key, title, sub, big, age, judge, tone, stats, rule, **extra) -> dict:
    return {"key": key, "title": title, "sub": sub, "big": big, "age": age, "judge": judge, "tone": tone,
            "stats": stats, "rule": rule, **extra}


def _fund_tab(parts: dict, day: date) -> dict:
    ensure(parts, ("south", "shibor", "funds"), day)
    previous = _previous_open(parts, day)
    turn, etf, margin = parts["turnover"], parts["etf"], parts["margin"]
    south, shibor, supply, funds = parts.get("south") or {}, parts.get("shibor") or {}, parts["supply"], parts.get("funds") or {}
    money = _money_signal(parts)
    groups = etf.get("groups") or {}
    core = [
        _block(
            "turnover", "成交额", "市场热不热", _yi(turn.get("amount")), _age(turn.get("date", ""), day, previous),
            turn.get("label") or "没有", {"冷清": "bad", "活跃": "good"}.get(turn.get("label"), "flat"),
            _span_stats(turn.get("windows"), _pct, [n for n in (1, 3, 5, 30) if judge_turnover(_stored(turn.get("windows"), n)) == turn.get("label")])[:-1] + [_stat("60 日均", _yi(turn.get("avg60")))],
            "1 / 3 / 5 / 30 日都是这段日均成交相对过去 60 日，±5% 分冷清和活跃。四档里要有三档同向，才把成交判成冷清或活跃；单独 5 日不够。成交额只说明热不热，不代表钱在进还是出。",
            story=(f"近 5 天里 {turn['down_days']} 天指数下跌，成交" + ("一直比平时少" if (turn.get("ratio5") or 0) < 0 else "比平时多") + "。") if turn.get("down_days") else "",
            pctile=_pctile_info("成交额处在一年里", turn.get("pctile")),
            chart={"kind": "bars", "unit": "亿元", "points": turn.get("series") or [], "legend": ["上涨日", "下跌日"]}, open=True,
        ),
        _block(
            "etf", "宽基 ETF 份额", "有没有新钱买入", f"{_plain(etf.get('total'), 0)} 亿份", _age(etf.get("latest_date", ""), day, previous),
            etf.get("label") or "没有", {"净流入": "good", "净流出": "bad"}.get(etf.get("label"), "flat"),
            _span_stats(etf.get("windows"), lambda value: _pct(value, 2), _etf_marks(etf)),
            "净流入要 5 日超过 +1% 且至少 4 天在买，同时 30 日和 60 日也都超过 +1%。净流出要 5 日和 30 日都低于 −1%。按份额算，不受涨跌影响。金额是份额变化乘收盘价。",
            story=f"最近一天份额 {_delta(etf.get('day_change'))} 亿份，5 日 {_pct(etf.get('ratio'), 2)}，净申购 {etf.get('buy_days', '—')} 天。大盘 {_yi(groups.get('大盘'), True)}，中小盘 {_yi(groups.get('中小盘'), True)}，科创创业 {_yi(groups.get('科创创业'), True)}。",
            pctile=_pctile_info("宽基 ETF 总份额处在一年里", etf.get("pctile")),
            chart={"kind": "area", "unit": "亿份", "points": etf.get("series") or [], "legend": ["当天净买入", "当天净卖出"]},
            items=etf.get("items") or [],
        ),
        _block(
            "margin", "两融余额", "借钱炒股的人多不多", _yi(margin.get("balance")), _age(margin.get("date", ""), day, previous),
            margin.get("label") or "没有", {"在降": "bad", "加速下降": "bad", "在升": "good"}.get(margin.get("label"), "flat"),
            _span_stats(margin.get("windows"), _pct, _margin_marks(margin)),
            "加速下降要 30 日和 60 日都低于 −5%。30 日低于 −2% 算在降，高于 +2% 且 60 日不降才算在升。1 / 3 / 5 日只展示，不单独改档。",
            story=f"融资买入占成交 {_pct(margin.get('share'))}；当日融资净买入 {_yi(margin.get('net'), True)}。",
            pctile=_pctile_info("两融余额处在一年里", margin.get("pctile")),
            chart={"kind": "area", "unit": "亿元", "points": margin.get("series") or [], "legend": ["当天净借入", "当天净还款"]},
        ),
    ]
    where = [
        _block(
            "south", "南向资金", "A 股的钱有没有去港股", "没有" if south.get("ggt_net") is None else _delta(south["ggt_net"], 1), _age(south.get("ggt_date", ""), day, previous),
            "不改档位", "flat",
            _span_stats(south.get("windows"), _delta, 5),
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
            "supply", "大股东增减持和回购", "增持 − 减持", _yi(supply.get("net"), True), "近 28 天",
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
        "sections": [{"title": "看懂好不好做", "hint": "", "blocks": [
            _block("trend", "大盘趋势", "指数在均线上还是下", mood["qa"][0]["a"], "今日", mood["qa"][0]["a"], mood["qa"][0]["tone"], [],
                   "看 60 日线定方向，前低 = 今天之前 20 个交易日的最低收盘价，今天收在它下面算破位。", story=mood["qa"][0]["detail"], open=True),
            _block("breadth", "多数股票", "涨的多还是跌的多", f"{breadth.get('up', '—')} / {breadth.get('down', '—')}", "今日", mood["qa"][1]["a"], mood["qa"][1]["tone"],
                   [_stat("上涨", str(breadth.get("up", "—")), True), _stat("下跌", str(breadth.get("down", "—"))), _stat("新高 / 新低", f"{breadth.get('highs', '—')} / {breadth.get('lows', '—')}")],
                   "全 A 不含 ST；新高、新低按近 25 个交易日。", story=mood["qa"][1]["detail"]),
            _block("limits", "短线情绪", "追涨能不能赚钱", f"涨停 {limits.get('up', '—')}", "今日", mood["qa"][2]["a"], mood["qa"][2]["tone"],
                   [_stat("跌停", str(limits.get("down", "—"))), _stat("炸板", str(limits.get("broken", "—"))), _stat("最高连板", str(limits.get("height", "—")), True)],
                   "不含 ST、北交所；20cm（创业板、科创板）的涨跌幅折成 10cm 再平均。连板高度来自 limit_times。", story=mood["qa"][2]["detail"]),
        ] + ([_block("volume", "成交量", "放量还是缩量", mood["volume"]["word"], "今日", mood["volume"]["word"], "flat",
                     [_stat("今天", _yi(mood["volume"]["amount"]), True), _stat("前 20 日均", _yi(mood["volume"]["base"])),
                      _stat("近一年分位", "—" if mood["volume"]["pctile"] is None else f"{round(mood['volume']['pctile'] * 100)}%")],
                     "今天成交额是前 20 个交易日均值的 1.2 倍以上算放量，0.8 倍以下算缩量；近一年分位在最低 10% 算地量，最高 10% 算天量。"
                     "放量、缩量要和涨跌一起看：放量下跌是抛压还没完，缩量下跌是抛压在减弱，放量反抽比缩量反弹可信。不单独改「好不好做」的档位。",
                     story=next((row["detail"] for row in mood["qa"] if row["q"] == "成交量怎么样？"), ""), chart={"kind": "bars", "unit": "亿元", "points": (parts.get("turnover") or {}).get("series") or [], "legend": ["上涨日", "下跌日"]})] if mood.get("volume") else [])
          + ([_block("theme", "涨停主线", "涨停集中在哪几个行业", mood["theme"]["word"], "今日", mood["theme"]["word"], mood["theme"]["tone"], [],
                     "看今天涨停最集中的前三个行业，占全部涨停的多少；再看昨天的主线今天平均涨跌。前三占 35% 以上算有主线，20% 以下算没主线；"
                     "昨天的主线今天平均亏过 1.5%，算主线在退，接力常在这时断掉。",
                     story=mood["theme"]["detail"])] if mood.get("theme") else [])
          + ([_block("stage", "情绪阶段", "近 20 个交易日每天所处阶段", mood["stage"], "今日", mood["stage"], mood["qa"][3]["tone"], [],
                     "冰点 → 修复 → 发酵 → 高潮 → 退潮，一圈通常几周。按当天的涨停数、跌停数、昨涨停今日平均涨跌（20cm 折成 10cm）、炸板率、最高连板和昨天主线今天的表现判，并且接着前一天走；"
                     "高标断板、主线大面也算退潮的信号。阈值按这一年的行情定。",
                     story=mood["qa"][3]["detail"], chart={"kind": "stages", "points": mood["stages"]})] if mood.get("stage") else [])}],
        "footnote": "「好不好做」看的是买进去容不容易赚钱，不看钱从哪来（那是「市场资金」和「国家队」）。",
    }


def _index_rows(parts: dict) -> list[dict]:
    rows = (parts.get("index") or {}).get("rows") or []
    return [
        {"name": row.get("name"), "close": row.get("close"), "pct": row.get("pct"),
         "ma60": row.get("above_ma60"), "broke_low": row.get("broke_low")}
        for row in rows
    ]


SECTOR_SCOPE = (
    f"覆盖申万 31 个一级行业：{len(market_sector.UNIVERSE)} 个有代表 ETF 的主题（钱 = ETF 份额变化 × 收盘价），"
    f"{len(market_sector.BASE)} 个没有合适 ETF 的行业按行业主力资金估算；"
    "钱按「近 5 日净流入比自己平时多几倍波动」判，不拿绝对金额比；价看近 5 日相对沪深 300；热度 = 近 5 日日均成交额 ÷ 近 60 日均值；"
    "杠杆 = 行业两融 30 日变化相对各行业中位数。份额晚一天公布。"
)


def _sector_tab(parts: dict, day: date) -> dict:
    rows = _sector_rows(parts["sectors"], parts.get("catalysts"))
    coverage = parts["sectors"].get("coverage") or {}
    return {
        "title": "板块", "label": "", "tone": "flat", "age": "昨日", "summary": _sector_headline(rows),
        "rows": rows, "buckets": _sector_buckets(rows), "indexes": _index_rows(parts),
        "windows": parts["sectors"].get("windows"), "conclusion_days": parts["sectors"].get("conclusion_days"),
        "coverage": coverage, "coverage_l1": [{"l1": name, **item} for name, item in market_sector.coverage().items()],
        "thresholds": {"money_z": rules.MONEY_Z, "price_ex": rules.PRICE_EX, "hot": rules.SECTOR_HOT, "cold": rules.SECTOR_COLD,
                       "pos_high": rules.POS_HIGH, "pos_low": rules.POS_LOW},
        "footnote": SECTOR_SCOPE + " 板块只回答「钱往哪去」，不改大盘档位，也不推荐具体的票。",
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
