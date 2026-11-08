"""基本面分析师追问的切片。数字在这里算好，模型只引用 text。

业绩预告和快报、未来三个月解禁、机构调研、主营构成、现金分红。
预告净利润单位是万元，快报金额是元，解禁股数是股、比例已经是百分数，分红是每股派现。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app import config
from app.tools import cache
from app.tools.stock import Stock, is_hk

PARTS = ("forecast", "express", "share_float", "survey", "mainbz", "dividend", "chain")
_ZONE = ZoneInfo("Asia/Shanghai")
_SKIP_BIZ = ("合计", "总计", "调整", "主营业务", "营业收入")
_LARGE_RATIO = 1.0


def build(stock: Stock, part: str, today: date | None = None) -> dict:
    if part not in PARTS:
        raise ValueError(f"未知切片 {part}")
    day = today or datetime.now(_ZONE).date()
    if is_hk(stock):
        return _doc(stock, part, [], "港股暂无与 A 股相同的业绩预告、解禁、调研和分红数据")
    return {
        "forecast": _forecast,
        "express": _express,
        "share_float": _share_float,
        "survey": _survey,
        "mainbz": _mainbz,
        "dividend": _dividend,
        "chain": _chain,
    }[part](stock, day)


def format_forecast(rows: list[dict], today: date) -> dict:
    picked = _latest_by_end(rows, today)[:2]
    if not picked:
        return _body([], "没有取到业绩预告")
    facts = []
    for row in picked:
        end = _iso(row.get("end_date"))
        kind = str(row.get("type") or "业绩预告").strip()
        low, high = _num(row.get("net_profit_min")), _num(row.get("net_profit_max"))
        span = _range(_wan_yi(low), _wan_yi(high))
        change = _range(_pct_points(row.get("p_change_min")), _pct_points(row.get("p_change_max")))
        bits = [f"{_iso(row.get('ann_date'))} 预告 {end}：{kind}"]
        if span:
            bits.append(f"归母净利润 {span}")
        if change:
            bits.append(f"变动 {change}")
        last = _num(row.get("last_parent_net"))
        if last is not None:
            bits.append(f"上年同期 {_wan_yi(last)}")
        reason = _clip(row.get("change_reason") or row.get("summary"))
        if reason:
            bits.append(reason)
        text = "，".join(bits)
        facts.append({"id": f"fd.forecast.{_ymd(row.get('end_date'))}", "text": text})
    return _body(facts, " ".join(item["text"] for item in facts) + " 这是公司自己的预告，不是一致预期。")


def format_express(rows: list[dict], today: date) -> dict:
    picked = _latest_by_end(rows, today)[:2]
    if not picked:
        return _body([], "没有取到业绩快报")
    facts = []
    for row in picked:
        profit = _num(row.get("n_income"))
        prior = _num(row.get("yoy_net_profit"))
        yoy = None if profit is None or not prior else (profit - prior) / abs(prior) * 100
        bits = [
            f"{_iso(row.get('ann_date'))} 快报 {_iso(row.get('end_date'))}",
            f"营收 {_yuan_yi(row.get('revenue'))}",
        ]
        sales_yoy = _rate(row.get("yoy_sales"))
        if sales_yoy:
            bits.append(f"营收同比 {sales_yoy}")
        bits.append(f"净利润 {_yuan_yi(profit)}")
        if yoy is not None:
            bits.append(f"净利润同比 {yoy:+.1f}%")
        dedu = _rate(row.get("yoy_dedu_np"))
        if dedu:
            bits.append(f"扣非同比 {dedu}")
        eps = _num(row.get("diluted_eps"))
        if eps is not None:
            bits.append(f"摊薄每股收益 {_money(eps)}")
        roe = _num(row.get("diluted_roe"))
        if roe is not None:
            bits.append(f"摊薄净资产收益率 {roe:.2f}%")
        text = "，".join(bits)
        facts.append({"id": f"fd.express.{_ymd(row.get('end_date'))}", "text": text})
    return _body(facts, " ".join(item["text"] for item in facts) + " 快报还不是正式财报。")


def format_share_float(rows: list[dict], today: date) -> dict:
    end = today + timedelta(days=92)
    events = []
    for row in rows:
        due = _as_date(row.get("float_date"))
        if due is None or due < today or due > end:
            continue
        ratio = _num(row.get("float_ratio"))
        events.append({
            "date": due,
            "holder": str(row.get("holder_name") or "限售股东").strip() or "限售股东",
            "kind": str(row.get("share_type") or "").strip(),
            "shares": _num(row.get("float_share")),
            "ratio": ratio,
        })
    events.sort(key=lambda item: (item["date"], -(item["ratio"] or 0)))
    if not events:
        text = "未来 3 个月没有限售解禁"
        return _body([{"id": "fd.share_float", "text": text}], text)
    ratios = [item["ratio"] for item in events if item["ratio"] is not None]
    bits = []
    for item in events[:8]:
        ratio = item["ratio"]
        mark = "，大额" if ratio is not None and ratio >= _LARGE_RATIO else ""
        piece = f"{item['date'].isoformat()} {item['holder']}"
        if item["kind"]:
            piece += f"（{item['kind']}）"
        piece += f"解禁 {_shares(item['shares'])}"
        if ratio is not None:
            piece += f"，占总股本 {ratio:.2f}%{mark}"
        bits.append(piece)
    large = [item for item in events if item["ratio"] is not None and item["ratio"] >= _LARGE_RATIO]
    total = sum(ratios)
    if large:
        tail = "未来 3 个月有大额解禁，单笔占总股本达到 1%。给出可以关注之前先看这些供给。"
    elif total >= _LARGE_RATIO:
        tail = f"单笔不到 1%，但合计占总股本 {total:.2f}%，仍是供给冲击。"
    else:
        tail = "这些解禁单笔占总股本都不到 1%。"
    text = "未来 3 个月解禁：" + "；".join(bits) + "。" + tail
    return _body([{"id": "fd.share_float", "text": text}], text)


def format_survey(rows: list[dict], today: date) -> dict:
    start = today - timedelta(days=90)
    grouped: dict[date, list[dict]] = {}
    for row in rows:
        day = _as_date(row.get("surv_date"))
        if day is None or day < start or day > today:
            continue
        grouped.setdefault(day, []).append(row)
    if not grouped:
        text = "近 90 日没有机构调研记录"
        return _body([{"id": "fd.survey", "text": text}], text)
    days = sorted(grouped, reverse=True)
    latest = grouped[days[0]]
    orgs = []
    kinds = []
    for row in latest:
        org = _clean(row.get("rece_org"))
        kind = _clean(row.get("org_type"))
        if org and org not in orgs:
            orgs.append(org)
        if kind and kind not in kinds:
            kinds.append(kind)
    staff = _clip(_first(latest, "comp_rece"), 40)
    mode = _clean(_first(latest, "rece_mode"))
    place = _clean(_first(latest, "rece_place"))
    bits = [f"近 90 日机构调研 {len(days)} 次", f"最近 {days[0].isoformat()}"]
    if mode:
        bits.append(f"方式 {mode}")
    if place:
        bits.append(f"地点 {place}")
    if staff:
        bits.append(f"接待人员 {staff}")
    if kinds:
        bits.append("机构类型 " + "、".join(kinds[:4]))
    if orgs:
        bits.append("参与 " + "；".join(_clip(name, 24) for name in orgs[:3]))
    if len(days) > 1:
        bits.append("更早还有 " + "、".join(day.isoformat() for day in days[1:4]))
    text = "，".join(bits) + "。这只说明有人在看，不是买卖信号。"
    return _body([{"id": "fd.survey", "text": text}], text)


def format_mainbz(product: list[dict], region: list[dict]) -> dict:
    facts = []
    series = []
    product_text, product_series = _biz_lines(product, "产品")
    region_text, _ = _biz_lines(region, "地区")
    if product_text:
        facts.append({"id": "fd.mainbz.product", "text": product_text})
        series = product_series
    if region_text:
        facts.append({"id": "fd.mainbz.region", "text": region_text})
    if not facts:
        return _body([], "没有取到主营业务构成")
    return {**_body(facts, " ".join(item["text"] for item in facts)), "series": series}


def format_dividend(rows: list[dict]) -> dict:
    events = _dividend_events(rows)
    if not events:
        text = "没有取到分红送股记录"
        return _body([{"id": "fd.dividend", "text": text}], text)
    latest = events[0]
    annual = [item for item in events if item["end"].endswith("1231")][:5]
    paid = [item for item in annual if item["cash"] > 0]
    bits = [f"最近一期 {_iso(latest['end'])} {_proc(latest['proc'])}，{_cash_text(latest)}"]
    if annual:
        bits.append(f"近 {len(annual)} 个年度里，有现金分红的是 {len(paid)} 个")
        if not paid:
            bits.append("这些年度没有现金回报")
    shown = [item for item in events if item["cash"] > 0][:4]
    if shown:
        bits.append("近期派现：" + "；".join(
            f"{_iso(item['end'])} {_proc(item['proc'])} {_cash_text(item)}" for item in shown
        ))
    text = "。".join(bits) + "。"
    return _body([{"id": "fd.dividend", "text": text}], text)


def _forecast(stock: Stock, today: date) -> dict:
    rows = _cached(stock, "forecast", lambda pro: pro.forecast(
        ts_code=stock.ts_code, start_date=f"{today.year - 1}0101",
        fields="ann_date,end_date,type,p_change_min,p_change_max,net_profit_min,net_profit_max,last_parent_net,summary,change_reason",
    ))
    body = format_forecast(rows, today)
    return _doc(stock, "forecast", body["facts"], body["text"])


def _express(stock: Stock, today: date) -> dict:
    rows = _cached(stock, "express", lambda pro: pro.express(
        ts_code=stock.ts_code, start_date=f"{today.year - 2}0101",
        fields="ann_date,end_date,revenue,n_income,yoy_sales,yoy_dedu_np,yoy_net_profit,diluted_eps,diluted_roe",
    ))
    body = format_express(rows, today)
    return _doc(stock, "express", body["facts"], body["text"])


def _share_float(stock: Stock, today: date) -> dict:
    start = today.strftime("%Y%m%d")
    end = (today + timedelta(days=92)).strftime("%Y%m%d")
    rows = _cached(stock, "share_float", lambda pro: pro.share_float(
        ts_code=stock.ts_code, start_date=start, end_date=end,
    ))
    body = format_share_float(rows, today)
    return _doc(stock, "share_float", body["facts"], body["text"])


def _survey(stock: Stock, today: date) -> dict:
    start = (today - timedelta(days=90)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    rows = _cached(stock, "survey", lambda pro: pro.stk_surv(
        ts_code=stock.ts_code, start_date=start, end_date=end,
    ))
    body = format_survey(rows, today)
    return _doc(stock, "survey", body["facts"], body["text"])


def _mainbz(stock: Stock, today: date) -> dict:
    product = _cached(stock, "mainbz_p", lambda pro: pro.fina_mainbz(
        ts_code=stock.ts_code, type="P", fields="end_date,bz_item,bz_sales,bz_profit,bz_cost",
    ))
    region = _cached(stock, "mainbz_d", lambda pro: pro.fina_mainbz(
        ts_code=stock.ts_code, type="D", fields="end_date,bz_item,bz_sales,bz_profit,bz_cost",
    ))
    body = format_mainbz(product, region)
    doc = _doc(stock, "mainbz", body["facts"], body["text"])
    doc["series"] = body.get("series") or []
    return doc


def _dividend(stock: Stock, today: date) -> dict:
    rows = _cached(stock, "dividend", lambda pro: pro.dividend(
        ts_code=stock.ts_code, fields="end_date,ann_date,div_proc,cash_div,cash_div_tax,stk_div",
    ))
    body = format_dividend(rows)
    return _doc(stock, "dividend", body["facts"], body["text"])


def _chain(stock: Stock, today: date) -> dict:
    from app.services import industry_chain

    return industry_chain.build(stock)


def _cached(stock: Stock, name: str, fetch) -> list[dict]:
    from app.services.sector_detail.tsutil import call, records
    from app.tools.tushare_client import get_pro

    day = datetime.now(_ZONE).date().strftime("%Y%m%d")
    key = f"fund_reply:{name}:{stock.ts_code}:{day}"
    cached = cache.get(key)
    if isinstance(cached, list):
        return cached
    frame = call(lambda: fetch(get_pro()))
    rows = records(frame)
    if rows or frame is not None:
        cache.set(key, rows, config.CACHE_TTL["fundamental_detail"])
    return rows


def _biz_lines(rows: list[dict], label: str) -> tuple[str, list[dict]]:
    period, items = _biz_items(rows)
    if not items:
        return "", []
    bits = []
    series = []
    for item in items:
        share = "" if item["share"] is None else f"，占比 {item['share']:.1f}%"
        margin = "" if item["margin"] is None else f"，毛利率 {item['margin']:.1f}%"
        bits.append(f"{item['name']} 收入 {item['sales']}，成本 {item['cost']}{share}{margin}")
        if item["share"] is not None:
            series.append({"label": item["name"], "value": round(item["share"], 1)})
    return f"{label}构成（{period}）：" + "；".join(bits), series


def _biz_items(rows: list[dict]) -> tuple[str, list[dict]]:
    by_period: dict[str, dict[str, dict]] = {}
    for row in rows:
        name = str(row.get("bz_item") or "").strip()
        sales = _num(row.get("bz_sales"))
        if not name or sales is None or sales <= 0 or any(word in name for word in _SKIP_BIZ):
            continue
        by_period.setdefault(_ymd(row.get("end_date")), {})[name] = row
    if not by_period:
        return "", []
    period = max(by_period)
    current = by_period[period]
    total = sum(_num(row.get("bz_sales")) or 0 for row in current.values())
    items = []
    for name, row in sorted(current.items(), key=lambda pair: -(_num(pair[1].get("bz_sales")) or 0))[:6]:
        sales = _num(row.get("bz_sales")) or 0
        profit = _num(row.get("bz_profit"))
        cost = _num(row.get("bz_cost"))
        items.append({
            "name": name,
            "sales": _yuan_yi(sales),
            "cost": "没有" if cost is None else _yuan_yi(cost),
            "share": None if total <= 0 else sales / total * 100,
            "margin": None if profit is None or sales <= 0 else profit / sales * 100,
        })
    return _iso(period), items


def _dividend_events(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        end = _ymd(row.get("end_date"))
        if len(end) < 8:
            continue
        grouped.setdefault(end, []).append(row)
    events = []
    for end, group in grouped.items():
        row = max(group, key=lambda item: (_proc_rank(item.get("div_proc")), _ymd(item.get("ann_date"))))
        pre, post = _cash_pair(row)
        cash = pre if pre is not None else post
        events.append({
            "end": end,
            "proc": str(row.get("div_proc") or "").strip(),
            "pre": pre,
            "post": post,
            "cash": 0.0 if cash is None else cash,
            "bonus": _num(row.get("stk_div")) or 0.0,
        })
    events.sort(key=lambda item: item["end"], reverse=True)
    return events


def _cash_pair(row: dict) -> tuple[float | None, float | None]:
    """税前用 cash_div_tax，税后用 cash_div。预案阶段经常只填一边。"""
    post = _positive(row.get("cash_div"))
    pre = _positive(row.get("cash_div_tax"))
    if pre is None and post is None:
        if _num(row.get("cash_div")) == 0 or _num(row.get("cash_div_tax")) == 0:
            return 0.0, 0.0
        return None, None
    return pre if pre is not None else post, post if post is not None else pre


def _cash_text(item: dict) -> str:
    bits = []
    if item["cash"] > 0:
        if item["pre"] is not None and item["post"] is not None and abs(item["pre"] - item["post"]) >= 0.005:
            bits.append(f"每股派现税前 {_money(item['pre'])}，税后 {_money(item['post'])}")
        else:
            bits.append(f"每股派现 {_money(item['cash'])}")
    else:
        bits.append("这一期没有现金分红")
    if item["bonus"] > 0:
        bits.append(f"每股送转 {_money(item['bonus'])}")
    return "，".join(bits)


def _latest_by_end(rows: list[dict], today: date) -> list[dict]:
    best: dict[str, dict] = {}
    limit = today.strftime("%Y%m%d")
    for row in rows:
        if not isinstance(row, dict):
            continue
        end = _ymd(row.get("end_date"))
        ann = _ymd(row.get("ann_date"))
        if len(end) < 8 or (len(ann) >= 8 and ann > limit):
            continue
        prev = best.get(end)
        if prev is None or ann >= _ymd(prev.get("ann_date")):
            best[end] = row
    return sorted(best.values(), key=lambda row: _ymd(row.get("ann_date")), reverse=True)


def _doc(stock: Stock, part: str, facts: list[dict], text: str) -> dict:
    return {
        "ts_code": stock.ts_code,
        "stock_name": stock.name,
        "part": part,
        "facts": facts,
        "text": text,
        "unavailable": [] if facts else ["没有取到"],
    }


def _body(facts: list[dict], text: str) -> dict:
    return {"facts": facts, "text": text}


def _proc_rank(value) -> int:
    text = str(value or "")
    if "实施" in text:
        return 3
    if "股东大会" in text:
        return 2
    if "预案" in text:
        return 1
    return 0


def _proc(value: str) -> str:
    if value == "实施":
        return "已实施"
    return value or "进度未知"


def _num(value):
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _positive(value):
    number = _num(value)
    if number is None or number <= 0:
        return None
    return number


def _ymd(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:8]


def _iso(value) -> str:
    text = _ymd(value)
    if len(text) < 8:
        return ""
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


def _as_date(value) -> date | None:
    text = _ymd(value)
    if len(text) < 8:
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def _wan_yi(value) -> str:
    if value is None:
        return ""
    return f"{value / 10000:.2f} 亿元"


def _yuan_yi(value) -> str:
    number = value if isinstance(value, (int, float)) and value == value else _num(value)
    if number is None:
        return "没有"
    return f"{number / 1e8:.2f} 亿元"


def _shares(value) -> str:
    if value is None:
        return "股数没有"
    wan = value / 10000
    if wan >= 10000:
        return f"{wan / 10000:.2f} 亿股"
    return f"{wan:.2f} 万股"


def _money(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{text} 元"


def _pct_points(value) -> str:
    number = _num(value)
    if number is None:
        return ""
    return f"{number:+.1f}%"


def _rate(value) -> str:
    """同比。绝对值大于 2 视为已经是百分数，否则视为小数。"""
    number = _num(value)
    if number is None:
        return ""
    if abs(number) <= 2:
        number *= 100
    return f"{number:+.1f}%"


def _range(left: str, right: str) -> str:
    if left and right and left != right:
        return f"{left} 到 {right}"
    return left or right


def _clip(value, limit: int = 60) -> str:
    text = _clean(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _clean(value) -> str:
    text = str(value or "").strip()
    if text in {"--", "-", "None", "nan"}:
        return ""
    return text


def _first(rows: list[dict], key: str):
    for row in rows:
        text = _clean(row.get(key))
        if text:
            return text
    return ""
