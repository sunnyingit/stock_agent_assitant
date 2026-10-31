"""取行业和概念成分，用等权日收益做板块走势。

成分是当前名单（is_new），没有进出日期。概念板块因此不能当成已经回测过。
个股所在板块的曲线用成分股等权，不用发布的指数点位。
行业板块的涨幅排名对照申万一级发布指数。概念板块沿用相关性最高的行业排名。
宽度是站上 20 日线的比例，当日上涨家数另记。
"""

from __future__ import annotations

from datetime import date, timedelta

from app import config
from app.tools import cache
from app.tools.stock import Stock
from app.services.sector_detail.rules import compound, pearson

_MEMBER_CAP = 80
_CONCEPT_CAP = 5
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
    boards = _boards(pro, stock)
    if not dates or not boards:
        return {
            "as_of": dates[-1] if dates else None,
            "listed_days": listed_days,
            "sectors": [],
            "source_text": _source(False, False),
        }
    index_map = _index_bars(pro, start, end)
    l1 = _l1_bars(pro, start, end)
    levels = _sw_levels(pro)
    sw_panel = _sw_returns(pro, dates)
    market = _market_amounts(pro, start, end)
    codes = _collect_codes(boards, stock.ts_code)
    panel = {stock.ts_code: stock_bars}
    for code in codes:
        if code == stock.ts_code:
            continue
        panel[code] = _bars(pro, code, start, end, required=False)
    sectors = []
    truncated = False
    concepts = False
    for board in boards:
        if board.get("truncated"):
            truncated = True
        if board.get("type") != "industry":
            concepts = True
        built = _sector_row(board, panel, dates, index_map, stock.ts_code, market)
        if built is not None:
            sectors.append(built)
    _rank_same_level(sectors, levels, sw_panel, l1)
    _copy_industry_rank(sectors)
    return {
        "as_of": dates[-1],
        "listed_days": listed_days,
        "sectors": sectors,
        "source_text": _source(concepts, truncated),
    }


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


def _boards(pro, stock: Stock) -> list[dict]:
    boards = _industry_boards(pro, stock)
    concepts, _truncated = _concept_boards(pro, stock)
    boards.extend(concepts)
    _fit_budget(boards, stock.ts_code)
    return [board for board in boards if len(board.get("members") or []) >= 3]


def _fit_budget(boards: list[dict], ts_code: str, budget: int = 160) -> None:
    seen = {ts_code}
    for board in boards:
        needed = [item["ts_code"] for item in board["members"] if item["ts_code"] not in seen]
        room = budget - len(seen)
        if len(needed) > room:
            if board.get("type") != "industry":
                board["members"] = []
                continue
            board["truncated"] = True
            kept = []
            for item in board["members"]:
                code = item["ts_code"]
                if code not in seen and len(seen) >= budget:
                    continue
                seen.add(code)
                kept.append(item)
            board["members"] = kept
            continue
        for code in needed:
            seen.add(code)
        seen.add(ts_code)


def _industry_boards(pro, stock: Stock) -> list[dict]:
    frame = _call(lambda: pro.index_member_all(ts_code=stock.ts_code, is_new="Y"))
    row = _first(frame)
    if not row:
        return []
    found = []
    for level, code_key, name_key in (
        ("l3", "l3_code", "l3_name"),
        ("l2", "l2_code", "l2_name"),
    ):
        code = str(row.get(code_key) or "")
        name = str(row.get(name_key) or "")
        if not code:
            continue
        members, truncated = _sw_members(pro, code, level)
        if len(members) < 3:
            continue
        found.append({
            "code": code if code.endswith(".SI") else f"{code}.SI",
            "name": name or code,
            "type": "industry",
            "sw_level": level,
            "l1_code": _si(row.get("l1_code")),
            "l1_name": str(row.get("l1_name") or ""),
            "members": members,
            "truncated": truncated,
        })
    return found


def _sw_members(pro, code: str, level: str) -> tuple[list[dict], bool]:
    bare = code[:-3] if str(code).endswith(".SI") else str(code)
    attempts = []
    for value in (code, bare, f"{bare}.SI"):
        if value and value not in attempts:
            attempts.append(value)
    members: list[dict] = []
    for attempt in attempts:
        frame = _call(lambda value=attempt: pro.index_member_all(**{f"{level}_code": value, "is_new": "Y"}))
        members = _member_rows(frame)
        if members:
            break
    if not members:
        frame = _call(lambda: pro.index_member(index_code=f"{bare}.SI", is_new="Y"))
        members = _member_rows(frame)
    truncated = len(members) > _MEMBER_CAP
    return members[:_MEMBER_CAP], truncated


def _concept_boards(pro, stock: Stock) -> tuple[list[dict], bool]:
    grouped = _concept_groups(pro, stock.ts_code)
    boards = []
    truncated = False
    for item in grouped[:_CONCEPT_CAP]:
        code, name, members = item[0], item[1], item[2]
        sector_type = item[3] if len(item) > 3 else "concept"
        if len(members) < 3:
            continue
        cut = len(members) > _MEMBER_CAP
        truncated = truncated or cut
        boards.append({
            "code": code,
            "name": name,
            "type": sector_type,
            "members": members[:_MEMBER_CAP],
            "truncated": cut,
        })
    return boards, truncated


def _concept_groups(pro, ts_code: str) -> list[tuple]:
    chosen = _ths_concepts(pro, ts_code)
    if chosen:
        return chosen
    frame = _call(lambda: pro.concept_detail(ts_code=ts_code))
    groups = _groups_from_frame(frame, "id", "concept_name")
    detailed = []
    for code, name, _members in groups[:_CONCEPT_CAP]:
        member_frame = _call(lambda cid=code: pro.concept_detail(id=cid))
        members = _member_rows(member_frame)
        if len(members) >= 3:
            detailed.append((f"cpt_{code}", name, members, "concept"))
    return detailed


def _ths_concepts(pro, ts_code: str) -> list[tuple]:
    frame = _call(lambda: pro.ths_member(con_code=ts_code))
    codes = []
    for row in _records(frame):
        code = str(row.get("ts_code") or "")
        if code and code not in codes:
            codes.append(code)
    if not codes:
        return []
    catalog = _ths_catalog(pro)
    if not catalog:
        return []
    chosen = []
    for kind, sector_type, limit in (("N", "concept", 3), ("R", "region", 1)):
        rows = []
        for code in codes:
            meta = catalog.get(code)
            if not meta or meta.get("type") != kind:
                continue
            count = meta.get("count") or 0
            if count < 8 or count > _MEMBER_CAP:
                continue
            rows.append((count, code, meta.get("name") or code))
        rows.sort()
        for _count, code, name in rows[:limit]:
            member_frame = _call(lambda cid=code: pro.ths_member(ts_code=cid))
            members = _member_rows(member_frame)
            if len(members) >= 3:
                chosen.append((code, name, members, sector_type))
    return chosen


def _ths_catalog(pro) -> dict:
    key = "sector_ths_index"
    cached = cache.get(key)
    if cached is not None:
        return cached
    frame = _call(lambda: pro.ths_index(exchange="A"))
    catalog = {}
    for row in _records(frame):
        code = str(row.get("ts_code") or "")
        if not code:
            continue
        catalog[code] = {
            "name": str(row.get("name") or code),
            "type": str(row.get("type") or ""),
            "count": _num(row.get("count")) or 0,
        }
    if catalog:
        cache.set(key, catalog, config.CACHE_TTL["sector_detail"])
    return catalog


def _groups_from_frame(frame, code_key: str, name_key: str) -> list[tuple[str, str, list[dict]]]:
    rows = _records(frame)
    grouped: dict[str, dict] = {}
    for row in rows:
        code = _clean_id(row.get(code_key) or row.get("id"))
        name = str(row.get(name_key) or row.get("name") or code)
        if not code:
            continue
        bucket = grouped.setdefault(code, {"name": name, "members": []})
        member = _one_member(row)
        if member is not None:
            bucket["members"].append(member)
    return [(code, item["name"], item["members"]) for code, item in grouped.items()]


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


def _copy_industry_rank(sectors: list[dict]) -> None:
    industries = [
        item for item in sectors
        if item.get("type") == "industry" and item.get("corr_20d") is not None
    ]
    if not industries:
        return
    donor = max(industries, key=lambda item: float(item["corr_20d"]))
    fields = ("rank", "rank_total", "rank_level", "rank_level_text", "rank_basis_name", "rank_basis_text")
    ranks = {
        day.get("date"): {field: day.get(field) for field in fields}
        for day in donor.get("days") or []
    }
    for sector in sectors:
        if sector.get("type") == "industry":
            continue
        for day in sector.get("days") or []:
            copied = ranks.get(day.get("date"))
            if not copied:
                continue
            day.update(copied)


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


def _si(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text if text.endswith(".SI") else f"{text}.SI"


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
    panel: dict[str, dict[str, float]] = {}
    cursor = date.fromisoformat(window[0])
    last = date.fromisoformat(window[-1])
    while cursor <= last:
        chunk_end = min(last, cursor + timedelta(days=12))
        frame = _call(lambda start=cursor, stop=chunk_end: pro.sw_daily(
            start_date=start.strftime("%Y%m%d"),
            end_date=stop.strftime("%Y%m%d"),
        ))
        for row in _records(frame):
            day = _iso(row.get("trade_date"))
            code = _si(row.get("ts_code"))
            value = _ret(row.get("pct_change"))
            if day and code and value is not None:
                panel.setdefault(code, {})[day] = value
        cursor = chunk_end + timedelta(days=1)
    if panel:
        cache.set(key, panel, config.CACHE_TTL["sector_detail"])
    return panel


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


def _collect_codes(boards: list[dict], ts_code: str) -> list[str]:
    seen = {ts_code}
    for board in boards:
        for item in board["members"]:
            seen.add(item["ts_code"])
    return list(seen)


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


def _l1_bars(pro, start: str, end: str) -> dict:
    found = {}
    for code in _L1:
        key = f"sector_l1:{code}:{end}"
        cached = cache.get(key)
        if cached is None:
            frame = _call(lambda index_code=code: pro.index_daily(
                ts_code=index_code,
                start_date=start,
                end_date=end,
                fields="trade_date,pct_chg",
            ))
            if not _records(frame):
                frame = _call(lambda index_code=code: pro.sw_daily(
                    ts_code=index_code,
                    start_date=start,
                    end_date=end,
                ))
            cached = {}
            for row in _records(frame):
                day = _iso(row.get("trade_date"))
                raw = row.get("pct_chg")
                if raw is None:
                    raw = row.get("pct_change")
                value = _ret(raw)
                if day and value is not None:
                    cached[day] = value
            if cached:
                cache.set(key, cached, config.CACHE_TTL["sector_detail"])
        if cached:
            found[code] = cached
    return found


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


def _member_rows(frame) -> list[dict]:
    rows = []
    seen = set()
    for row in _records(frame):
        item = _one_member(row)
        if item is None or item["ts_code"] in seen:
            continue
        seen.add(item["ts_code"])
        rows.append(item)
    return rows


def _one_member(row: dict) -> dict | None:
    code = ""
    for key in ("con_code", "ts_code"):
        text = str(row.get(key) or "")
        if text.endswith((".SH", ".SZ", ".BJ")):
            code = text
            break
    if not code:
        return None
    name = str(row.get("con_name") or row.get("name") or code)
    return {"ts_code": code, "name": name}


def _clean_id(value) -> str:
    text = str(value or "").strip()
    if text.endswith(".0"):
        text = text[:-2]
    if text.lower() == "nan":
        return ""
    return text


def _records(frame) -> list[dict]:
    if frame is None:
        return []
    empty = getattr(frame, "empty", None)
    if empty is True:
        return []
    if hasattr(frame, "to_dict"):
        return frame.to_dict("records")
    return []


def _first(frame) -> dict:
    rows = _records(frame)
    return rows[0] if rows else {}


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


def _source(concepts: bool, truncated: bool) -> str:
    text = (
        "板块走势按当前成分股等权计算，不用发布的指数点位。"
        "成分没有历史进出记录，概念板块的结论没有回测依据。"
        "行业板块在同级申万发布指数里排名，三级不和一级混比。"
        "相关性相差不到 0.05 时，优先用行业板块。"
    )
    if concepts:
        text += "概念板块沿用相关性最高的行业板块在同级里的排名。"
    else:
        text += "这次没有取到概念板块。"
    text += "板块资金流这次没有取到。"
    if truncated:
        text += "成分过多的板块只保留了部分股票。"
    return text


def _call(fn, required: bool = False):
    try:
        return fn()
    except Exception:
        if required:
            raise
        return None


def _iso(value) -> str | None:
    text = str(value or "")
    if len(text) < 8:
        return None
    text = text[:8]
    if not text.isdigit():
        return None
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


def _ret(value):
    number = _num(value)
    if number is None:
        return None
    return number / 100.0


def _num(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number
