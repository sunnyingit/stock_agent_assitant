"""板块快照：先判断本股属于哪两个板块（discover.py / affiliation.py），再只为选中的板块取完整成分走势。

成分是当前名单（is_new），没有进出日期。概念板块因此不能当成已经回测过。
板块的曲线用成分股等权，不用发布的指数点位；归属判断用的是发布的指数日线。
行业板块的涨幅排名对照申万同级发布指数。概念板块沿用本股所属申万行业的排名。
宽度是站上 20 日线的比例，当日上涨家数另记。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from app import config
from app.services.sector_detail import affiliation as aff
from app.services.sector_detail import discover
from app.services.sector_detail.rules import compound, pearson
from app.services.sector_detail.tsutil import call as _call
from app.services.sector_detail.tsutil import iso as _iso
from app.services.sector_detail.tsutil import num as _num
from app.services.sector_detail.tsutil import records as _records
from app.services.sector_detail.tsutil import ret as _ret
from app.services.sector_detail.tsutil import si as _si
from app.tools import cache
from app.tools.stock import Stock

_MEMBER_CAP = 100
_L1 = (
    "801010.SI", "801030.SI", "801040.SI", "801050.SI", "801080.SI",
    "801110.SI", "801120.SI", "801130.SI", "801140.SI", "801150.SI",
    "801160.SI", "801170.SI", "801180.SI", "801200.SI", "801210.SI",
    "801230.SI", "801710.SI", "801720.SI", "801730.SI", "801740.SI",
    "801750.SI", "801760.SI", "801770.SI", "801780.SI", "801790.SI",
    "801880.SI", "801890.SI", "801950.SI", "801960.SI", "801970.SI",
    "801980.SI",
)


def load_snapshot(stock: Stock, today: date) -> dict:
    from app.tools.tushare_client import get_pro

    pro = get_pro()
    start, end = _window(today)
    stock_bars = _bars(pro, stock.ts_code, start, end, required=True)
    if not stock_bars:
        raise RuntimeError(f"没有取到 {stock.ts_code} 的行情")
    dates = sorted(stock_bars)[-260:]
    listed_days = _listed_days(stock_bars, today)
    empty = {"as_of": dates[-1] if dates else None, "listed_days": listed_days, "sectors": [], "affiliation": None, "source_text": _source(False)}
    if not dates or listed_days < 20:
        return empty
    stock_rets = {day: bar["ret"] for day, bar in stock_bars.items() if bar.get("ret") is not None}
    side = ThreadPoolExecutor(max_workers=2)   # 同级排名要的全行业走势最慢，和归属判断同时取
    try:
        sw_ahead, l1_ahead = side.submit(_sw_returns, pro, dates), side.submit(_l1_bars, pro, start, end)
        return _snapshot(pro, stock, stock_bars, dates, listed_days, stock_rets, start, end, sw_ahead, l1_ahead, empty)
    finally:
        side.shutdown(wait=False)


def _snapshot(pro, stock, stock_bars, dates, listed_days, stock_rets, start, end, sw_ahead, l1_ahead, empty) -> dict:
    found = discover.gather(pro, stock, stock_rets, dates, start, end)
    index_map = _index_bars(pro, start, end)
    chosen = [(role, cand) for role, cand in (("primary", found["primary"]), ("secondary", found["secondary"])) if cand is not None]
    affiliation = {
        "version": aff.VERSION,
        "primary": found["primary"]["code"] if found["primary"] else None,
        "secondary": found["secondary"]["code"] if found["secondary"] else None,
        "driver": found["driver"],
        "merged": found["merged"],
        "business_lines": found["lines"],
        "candidates": [
            discover.public_candidate(cand, next((role for role, picked in chosen if picked is cand), None))
            for cand in sorted(found["candidates"], key=lambda item: item["score"], reverse=True)
        ],
        "stock_vs_index": _gap(stock_rets, index_map, dates),
    }
    if not chosen:
        return {**empty, "affiliation": affiliation, "source_text": _source(False)}
    boards = [_board(role, cand, found["roster"][cand["code"]], found["caps"], stock.ts_code, found["industry"]) for role, cand in chosen]
    panel = _panel(pro, boards, stock.ts_code, stock_bars, start, end)
    levels = _sw_levels(pro)
    market = _market_amounts(pro, start, end)
    sectors = []
    for board in boards:
        built = _sector_row(board, panel, dates, index_map, stock.ts_code, market)
        if built is not None:
            built["role_in_stock"] = board["role"]
            built["evidence"] = next(item for item in affiliation["candidates"] if item["code"] == board["code"])
            sectors.append(built)
    sw_panel, l1 = sw_ahead.result(), l1_ahead.result()
    _rank_same_level(sectors, levels, sw_panel, l1)
    _copy_industry_rank(sectors, found["industry"], dates, levels, sw_panel, l1)
    return {
        "as_of": dates[-1],
        "listed_days": listed_days,
        "sectors": sectors,
        "affiliation": affiliation,
        "source_text": _source(any(item["type"] != "industry" for item in sectors), any(item.get("truncated") for item in sectors)),
    }


def _gap(stock_rets: dict, index_map: dict, dates: list[str]) -> float | None:
    window = dates[-20:]
    mine = compound([stock_rets.get(day) for day in window])
    base = compound([index_map.get(day) for day in window])
    return None if mine is None or base is None else mine - base


def _board(role: str, cand: dict, members: list[dict], caps: dict, ts_code: str, row: dict) -> dict:
    """选中的板块 → 取走势用的成分名单。成分超过上限时留市值最大的，本股一定在里面。"""
    ordered = sorted(members, key=lambda item: caps.get(item["ts_code"], 0.0), reverse=True)
    kept = ordered[:_MEMBER_CAP]
    if ts_code not in {item["ts_code"] for item in kept}:
        mine = next((item for item in members if item["ts_code"] == ts_code), None)
        if mine is not None:
            kept = kept[:-1] + [mine]
    board = {"code": cand["code"], "name": cand["name"], "type": cand["type"], "role": role, "members": kept, "truncated": len(members) > len(kept)}
    if cand["kind"] == "sw":
        board.update({"sw_level": cand["level"], "l1_code": cand.get("l1_code") or _si(row.get("l1_code")), "l1_name": cand.get("l1_name") or ""})
    return board


def _panel(pro, boards: list[dict], ts_code: str, stock_bars: dict, start: str, end: str) -> dict:
    codes = sorted({item["ts_code"] for board in boards for item in board["members"]} - {ts_code})
    panel = {ts_code: stock_bars}
    with ThreadPoolExecutor(max_workers=discover.WORKERS) as pool:
        for code, bars in zip(codes, pool.map(lambda item: _bars(pro, item, start, end, required=False), codes)):
            panel[code] = bars
    return panel


def _copy_industry_rank(sectors: list[dict], row: dict, dates: list[str], levels: dict, sw_panel: dict, l1_bars: dict) -> None:
    """概念板块没有同级发布指数可排，沿用本股所属申万行业的排名。选中的板块里有行业就用它，否则单独算一份。"""
    donor = next((item for item in sectors if item.get("type") == "industry"), None)
    if donor is None:
        code = _si(row.get("l3_code")) or _si(row.get("l2_code"))
        if not code:
            return
        donor = {
            "code": code, "sw_level": "l3" if row.get("l3_code") else "l2", "l1_code": _si(row.get("l1_code")),
            "l1_name": str(row.get("l1_name") or ""), "name": str(row.get("l3_name") or row.get("l2_name") or code),
            "days": [{"date": day} for day in dates[-260:]],
        }
        _rank_one(donor, levels, sw_panel, l1_bars)
    fields = ("rank", "rank_total", "rank_level", "rank_level_text", "rank_basis_name", "rank_basis_text")
    ranks = {day.get("date"): {field: day.get(field) for field in fields} for day in donor.get("days") or []}
    for sector in sectors:
        if sector.get("type") == "industry":
            continue
        for day in sector.get("days") or []:
            copied = ranks.get(day.get("date"))
            if copied:
                day.update(copied)


def _source(concepts: bool, truncated: bool = False) -> str:
    text = (
        "先按业务占比、交易联动、板块内地位判断本股属于哪两个板块，只挂名的不算。"
        "板块走势按当前成分股等权计算，不用发布的指数点位；成分没有历史进出记录，概念板块的结论没有回测依据。"
        "行业板块在同级申万发布指数里排名，三级不和一级混比。"
    )
    if concepts:
        text += "概念板块沿用本股所属申万行业在同级里的排名。"
    text += "板块资金流这次没有取到。"
    if truncated:
        text += "成分过多的板块只保留了市值最大的部分。"
    return text


def _window(today: date) -> tuple[str, str]:
    start = today - timedelta(days=420)
    return start.strftime("%Y%m%d"), today.strftime("%Y%m%d")


def _listed_days(bars: dict, today: date) -> int:
    if not bars:
        return 0
    first = date.fromisoformat(min(bars))
    if first <= today - timedelta(days=400):
        return max(len(bars), 20)
    return len(bars)




















def _sector_row(board: dict, panel: dict, dates: list[str], index_map: dict, ts_code: str, market: dict) -> dict | None:
    members = board["members"]
    flags = {item["ts_code"]: [] for item in members}
    days = []
    shares: list[float | None] = []
    for index, day in enumerate(dates):
        rets = []
        up = 0
        counted = 0
        above = 0
        ma_counted = 0
        limit_up = 0
        touched = 0
        broken = 0
        amount = 0.0
        stock_bar = (panel.get(ts_code) or {}).get(day)
        for item in members:
            code = item["ts_code"]
            bar = (panel.get(code) or {}).get(day)
            if not bar or bar.get("ret") is None:
                flags[code].append(False)
                continue
            counted += 1
            ret = bar["ret"]
            rets.append(ret)
            if ret > 0:
                up += 1
            standing = _above_ma20(panel, code, dates, index)
            if standing is not None:
                ma_counted += 1
                if standing:
                    above += 1
            hit = _limit(code, item.get("name"), ret)
            flags[code].append(hit)
            if hit:
                limit_up += 1
            touch = hit or _touched(code, item.get("name"), bar.get("high"), bar.get("pre_close"))
            if touch:
                touched += 1
                if not hit:
                    broken += 1
            if bar.get("amount"):
                amount += bar["amount"]
        if counted < 3:
            sector_ret = None
            up_ratio = None
        else:
            sector_ret = sum(rets) / len(rets)
            up_ratio = up / counted
        breadth = None if ma_counted < 3 else above / ma_counted
        market_amount = market.get(day)
        share = None if not amount or not market_amount else amount / market_amount
        shares.append(share)
        index_ret = index_map.get(day)
        days.append({
            "date": day,
            "stock": None if not stock_bar else stock_bar.get("ret"),
            "sector": sector_ret,
            "index": index_ret,
            "breadth": None if breadth is None else round(breadth, 4),
            "up_ratio_today": None if up_ratio is None else round(up_ratio, 4),
            "limit_up": limit_up,
            "max_streak": 0,
            "broken_rate": None if touched < 3 else round(broken / touched, 4),
            "rank": None,
            "rank_total": None,
        })
    _fill_streaks(days, flags)
    paired = [
        (day, share)
        for day, share in zip(days, shares)
        if day["sector"] is not None and day["stock"] is not None
    ]
    if len(paired) < 10:
        return None
    days = [day for day, _share in paired]
    shares = [share for _day, share in paired]
    corr = pearson([day["stock"] for day in days], [day["sector"] for day in days])
    return {
        "code": board["code"],
        "name": board["name"],
        "type": board["type"],
        "corr_20d": corr,
        "days": days,
        "members": _member_returns(members, panel, dates),
        "flow_rate": None,
        "turnover_share": _share_block(shares),
        "catalyst": None,
        "truncated": bool(board.get("truncated")),
    }




def _above_ma20(panel: dict, code: str, dates: list[str], index: int) -> bool | None:
    if index < 19:
        return None
    closes = []
    for day in dates[index - 19:index + 1]:
        bar = (panel.get(code) or {}).get(day)
        close = None if not bar else _close_of(bar)
        if close is None:
            return None
        closes.append(close)
    average = sum(closes) / len(closes)
    return closes[-1] > average


def _close_of(bar: dict) -> float | None:
    pre = bar.get("pre_close")
    ret = bar.get("ret")
    if pre is None or ret is None:
        return None
    return float(pre) * (1.0 + float(ret))


def _fill_streaks(days: list[dict], flags: dict[str, list[bool]]) -> None:
    running = {code: 0 for code in flags}
    for index, day in enumerate(days):
        best = 0
        for code, series in flags.items():
            if index < len(series) and series[index]:
                running[code] += 1
            else:
                running[code] = 0
            best = max(best, running[code])
        day["max_streak"] = best




_LEVEL_TEXT = {"l1": "申万一级", "l2": "申万二级", "l3": "申万三级"}


def _sw_levels(pro) -> dict[str, str]:
    key = "sector_sw_levels"
    cached = cache.get(key)
    if cached is not None:
        return cached
    found = {}
    for level, label in (("L1", "l1"), ("L2", "l2"), ("L3", "l3")):
        frame = _call(lambda current=level: pro.index_classify(level=current, src="SW2021"))
        for row in _records(frame):
            code = _si(row.get("index_code"))
            if code:
                found[code] = label
    if found:
        cache.set(key, found, config.CACHE_TTL["sector_detail"])
    return found


def _sw_returns(pro, dates: list[str]) -> dict[str, dict[str, float]]:
    window = dates[-180:]
    if not window:
        return {}
    key = f"sector_sw_ret:{window[0]}:{window[-1]}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    spans = []
    cursor = date.fromisoformat(window[0])
    last = date.fromisoformat(window[-1])
    while cursor <= last:
        chunk_end = min(last, cursor + timedelta(days=12))
        spans.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    with ThreadPoolExecutor(max_workers=discover.WORKERS) as pool:
        frames = list(pool.map(lambda span: _retry(lambda: pro.sw_daily(
            start_date=span[0].strftime("%Y%m%d"), end_date=span[1].strftime("%Y%m%d"),
        )), spans))
    panel: dict[str, dict[str, float]] = {}
    for frame in frames:
        for row in _records(frame):
            day = _iso(row.get("trade_date"))
            code = _si(row.get("ts_code"))
            value = _ret(row.get("pct_change"))
            if day and code and value is not None:
                panel.setdefault(code, {})[day] = value
    if panel and all(frame is not None for frame in frames):   # 有一段没取到时不缓存：缺一段会让滚动 20 日排名整段为空
        cache.set(key, panel, config.CACHE_TTL["sector_detail"])
    return panel


def _retry(fn, tries: int = 3):
    """接口限流时偶尔整段失败：隔一会儿重试。空表是真没有数据（休市），不重试；全部失败返回 None。"""
    for attempt in range(tries):
        frame = _call(fn)
        if frame is not None:
            return frame
        time.sleep(1.0 + attempt)
    return None


def _market_amounts(pro, start: str, end: str) -> dict[str, float]:
    key = f"sector_mkt_amt:{end}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    totals: dict[str, float] = {}
    for code in ("SH_MARKET", "SZ_MARKET"):
        frame = _call(lambda market=code: pro.daily_info(
            ts_code=market,
            start_date=start,
            end_date=end,
            fields="trade_date,amount",
        ))
        for row in _records(frame):
            day = _iso(row.get("trade_date"))
            amount = _num(row.get("amount"))
            if day and amount is not None:
                totals[day] = totals.get(day, 0.0) + amount * 100_000
    if totals:
        cache.set(key, totals, config.CACHE_TTL["sector_detail"])
    return totals


def _rank_same_level(sectors: list[dict], levels: dict[str, str], sw_panel: dict, l1_bars: dict) -> None:
    for sector in sectors:
        if sector.get("type") != "industry":
            continue
        _rank_one(sector, levels, sw_panel, l1_bars)


def _rank_one(sector: dict, levels: dict[str, str], sw_panel: dict, l1_bars: dict) -> None:
    days = sector.get("days") or []
    dates = [day.get("date") for day in days]
    subject, level, basis_name, series_map = _rank_universe(sector, levels, sw_panel, l1_bars)
    if not subject or not series_map:
        return
    level_text = _LEVEL_TEXT.get(level, "申万行业")
    rolls = {code: _rolling(series, dates) for code, series in series_map.items()}
    subject_roll = rolls.get(subject) or []
    for index, day in enumerate(days):
        current = subject_roll[index] if index < len(subject_roll) else None
        peers = [
            series[index]
            for series in rolls.values()
            if index < len(series) and series[index] is not None
        ]
        if current is None or len(peers) < 5:
            continue
        day["rank"] = sum(1 for value in peers if value > current) + 1
        day["rank_total"] = len(peers)
        day["rank_level"] = level
        day["rank_level_text"] = level_text
        day["rank_basis_name"] = basis_name
        day["rank_basis_text"] = f"{level_text} · {basis_name}"


def _rank_universe(sector: dict, levels: dict[str, str], sw_panel: dict, l1_bars: dict):
    code = _si(sector.get("code"))
    level = levels.get(code) or sector.get("sw_level")
    peers = {item: series for item, series in sw_panel.items() if levels.get(item) == level} if level else {}
    own = sw_panel.get(code) or {}
    if level and len(own) >= 20 and len(peers) >= 5:
        return code, level, sector.get("name") or code, peers
    l1_code = _si(sector.get("l1_code"))
    l1_name = sector.get("l1_name") or "一级行业"
    published = {item: series for item, series in sw_panel.items() if levels.get(item) == "l1"}
    if l1_code and l1_code in published and len(published) >= 5:
        return l1_code, "l1", l1_name, published
    fallback = {_si(item): series for item, series in (l1_bars or {}).items()}
    if l1_code in fallback and len(fallback) >= 5:
        return l1_code, "l1", l1_name, fallback
    return "", "", "", {}


def _rolling(series: dict, dates: list[str]) -> list[float | None]:
    values = [series.get(day) for day in dates]
    return _rolling_list(values)


def _rolling_list(values: list[float | None]) -> list[float | None]:
    out: list[float | None] = []
    for index in range(len(values)):
        window = values[max(0, index - 19):index + 1]
        out.append(compound(window) if len(window) >= 20 else None)
    return out


def _member_returns(members: list[dict], panel: dict, dates: list[str]) -> list[dict]:
    window = dates[-20:]
    rows = []
    for item in members:
        series = [(panel.get(item["ts_code"]) or {}).get(day, {}).get("ret") for day in window]
        if any(value is None for value in series):
            ret = None
        else:
            ret = compound(series)
        rows.append({
            "ts_code": item["ts_code"],
            "name": item.get("name") or item["ts_code"],
            "ret_20d": ret,
        })
    return rows


def _share_block(shares: list[float | None]) -> dict:
    usable = [(index, value) for index, value in enumerate(shares) if value is not None]
    if not usable:
        return {"value": None, "vs_last_week": None, "pctile_1y": None, "basis_text": "成分股成交额占沪深两市"}
    latest_index, latest = usable[-1]
    prior = None
    for index, value in usable:
        if latest_index - index >= 5:
            prior = value
    versus = None
    if prior is not None:
        versus = "up" if latest > prior else "down"
    history = [value for _index, value in usable]
    pctile = None
    if len(history) >= 20:
        below = sum(1 for value in history if value < latest)
        equal = sum(1 for value in history if value == latest)
        pctile = round((below + 0.5 * equal) / len(history), 4)
    return {"value": round(latest, 4), "vs_last_week": versus, "pctile_1y": pctile, "basis_text": "成分股成交额占沪深两市"}




def _bars(pro, ts_code: str, start: str, end: str, required: bool) -> dict:
    key = f"sector_bar:{ts_code}:{end}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    frame = _call(
        lambda: pro.daily(
            ts_code=ts_code,
            start_date=start,
            end_date=end,
            fields="ts_code,trade_date,pct_chg,high,pre_close,amount",
        ),
        required=required,
    )
    parsed = _parse_bars(frame)
    if parsed:
        cache.set(key, parsed, config.CACHE_TTL["sector_detail"])
    return parsed


def _index_bars(pro, start: str, end: str) -> dict:
    key = f"sector_idx:000300.SH:{end}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    frame = _call(lambda: pro.index_daily(
        ts_code="000300.SH",
        start_date=start,
        end_date=end,
        fields="trade_date,pct_chg",
    ))
    parsed = {}
    for row in _records(frame):
        day = _iso(row.get("trade_date"))
        if day:
            parsed[day] = _ret(row.get("pct_chg"))
    if parsed:
        cache.set(key, parsed, config.CACHE_TTL["sector_detail"])
    return parsed


def _l1_one(pro, code: str, start: str, end: str) -> dict:
    key = f"sector_l1:{code}:{end}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    frame = _call(lambda: pro.index_daily(ts_code=code, start_date=start, end_date=end, fields="trade_date,pct_chg"))
    if not _records(frame):
        frame = _call(lambda: pro.sw_daily(ts_code=code, start_date=start, end_date=end))
    found = {}
    for row in _records(frame):
        day = _iso(row.get("trade_date"))
        raw = row.get("pct_chg")
        if raw is None:
            raw = row.get("pct_change")
        value = _ret(raw)
        if day and value is not None:
            found[day] = value
    if found:
        cache.set(key, found, config.CACHE_TTL["sector_detail"])
    return found


def _l1_bars(pro, start: str, end: str) -> dict:
    with ThreadPoolExecutor(max_workers=discover.WORKERS) as pool:
        series = list(pool.map(lambda code: _l1_one(pro, code, start, end), _L1))
    return {code: found for code, found in zip(_L1, series) if found}


def _parse_bars(frame) -> dict:
    parsed = {}
    for row in _records(frame):
        day = _iso(row.get("trade_date"))
        if not day:
            continue
        parsed[day] = {
            "ret": _ret(row.get("pct_chg")),
            "high": _num(row.get("high")),
            "pre_close": _num(row.get("pre_close")),
            "amount": _num(row.get("amount")),
        }
    return parsed












def _limit(ts_code: str, name: str | None, ret: float | None) -> bool:
    if ret is None:
        return False
    return ret >= _threshold(ts_code, name) - 0.002


def _touched(ts_code: str, name: str | None, high, pre_close) -> bool:
    if high is None or not pre_close:
        return False
    threshold = _threshold(ts_code, name)
    return float(high) >= float(pre_close) * (1.0 + threshold) * 0.997


def _threshold(ts_code: str, name: str | None) -> float:
    label = name or ""
    if label.startswith(("*ST", "S*ST", "SST", "ST")):
        return 0.049
    if ts_code.startswith(("300", "301", "688", "689")):
        return 0.195
    if ts_code.endswith(".BJ") or ts_code.startswith(("43", "83", "87", "92")):
        return 0.295
    return 0.098










