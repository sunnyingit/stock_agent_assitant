"""量价资金框架用的原始序列。只返回接口字段，不计算形态、背离或方向。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from app import config
from app.tools import cache
from app.tools.stock import Stock, is_hk

_BAR_COLUMNS = (
    "trade_date", "open", "high", "low", "close", "pct_chg", "vol", "amount", "turnover_rate",
)
_FLOW_COLUMNS = (
    "trade_date", "close", "pct_change", "net_amount", "net_amount_rate",
    "buy_elg_amount", "buy_elg_amount_rate", "buy_lg_amount", "buy_lg_amount_rate",
    "buy_md_amount", "buy_md_amount_rate", "buy_sm_amount", "buy_sm_amount_rate",
)
_SECTOR_COLUMNS = (
    "trade_date", "close", "pct_change", "net_amount", "net_amount_rate",
    "buy_elg_amount", "buy_elg_amount_rate", "buy_lg_amount", "buy_lg_amount_rate",
    "buy_md_amount", "buy_md_amount_rate", "buy_sm_amount", "buy_sm_amount_rate",
)
_INDEX_COLUMNS = ("trade_date", "close", "pct_chg", "vol", "amount")
_MARGIN_COLUMNS = ("trade_date", "rzye", "rzmre", "rqye")
_TOP_COLUMNS = (
    "trade_date", "close", "pct_change", "l_buy", "l_sell", "net_amount", "net_rate", "reason",
)
_SEAT_COLUMNS = ("exalter", "buy", "sell", "net_buy", "side")
_INDEXES = (("000300.SH", "沪深300"), ("000905.SH", "中证500"))
_A_UNITS = {"vol": "手", "amount": "千元", "turnover_rate": "%", "pct_chg": "%"}
_HK_UNITS = {"vol": "股", "amount": "元", "turnover_rate": "%", "pct_chg": "%"}


def _limit(days: int, default: int, maximum: int) -> int:
    try:
        value = int(days)
    except (TypeError, ValueError):
        return default
    if value < 1:
        return default
    return min(value, maximum)


def _window(trading_days: int) -> tuple[str, str]:
    end = datetime.now()
    start = end - timedelta(days=int(trading_days * 1.7) + 20)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _date(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:8]


def _cell(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return None if value != value else value
    if hasattr(value, "item"):
        try:
            return _cell(value.item())
        except Exception:
            pass
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def _query(fn) -> tuple[pd.DataFrame | None, str]:
    try:
        frame = fn()
    except Exception as exc:
        return None, str(exc)
    if frame is None or not isinstance(frame, pd.DataFrame):
        return pd.DataFrame(), ""
    return frame, ""


def _recent(frame: pd.DataFrame | None, days: int) -> pd.DataFrame:
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return pd.DataFrame()
    out = frame.copy()
    out["trade_date"] = out["trade_date"].map(_date)
    out = out[out["trade_date"] != ""]
    out = out.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
    if days > 0:
        out = out.tail(days)
    return out.reset_index(drop=True)


def _items(frame: pd.DataFrame, columns: tuple[str, ...]) -> list[dict]:
    if frame is None or frame.empty:
        return []
    present = [column for column in columns if column in frame.columns]
    rows = []
    for record in frame[present].to_dict(orient="records"):
        item = {key: _cell(value) for key, value in record.items()}
        if "trade_date" in item:
            item["trade_date"] = _date(item["trade_date"])
        rows.append(item)
    return rows


def _latest(items: list[dict]) -> str:
    if not items:
        return ""
    return str(items[-1].get("trade_date") or "")


def _require_ashare(stock: Stock, label: str) -> None:
    if is_hk(stock):
        raise RuntimeError(f"{stock.ts_code} 是港股，暂无与 A 股相同的{label}")


def _prefer_row(frame: pd.DataFrame) -> pd.Series:
    ranked = frame.copy()
    names = ranked["name"].astype(str)
    ranked["_ii"] = names.str.endswith("Ⅱ").astype(int)
    ranked["_len"] = names.str.len()
    ranked = ranked.sort_values(["_ii", "_len", "name"], ascending=[False, True, True])
    return ranked.iloc[0]


def choose_board(industry: str | None, members: pd.DataFrame | None, boards: pd.DataFrame | None) -> dict | None:
    """在行业板块里选一只股票所属的板块。成分优先，名称用来区分白酒Ⅱ和食品饮料。"""
    if boards is None or boards.empty or "ts_code" not in boards.columns or "name" not in boards.columns:
        return None
    cleaned = boards.dropna(subset=["ts_code"]).copy()
    cleaned["ts_code"] = cleaned["ts_code"].astype(str)
    cleaned["name"] = cleaned["name"].astype(str)
    cleaned = cleaned.drop_duplicates("ts_code")
    industry_text = (industry or "").strip()
    member_codes: set[str] = set()
    if members is not None and not members.empty and "ts_code" in members.columns:
        member_codes = {str(code) for code in members["ts_code"].dropna().tolist()}

    pool = cleaned
    in_membership = False
    if member_codes:
        hit = cleaned[cleaned["ts_code"].isin(member_codes)]
        if not hit.empty:
            pool = hit
            in_membership = True

    if industry_text:
        exact = pool[pool["name"] == industry_text]
        if not exact.empty:
            row = exact.iloc[0]
            return {"ts_code": row["ts_code"], "name": row["name"], "matched_by": "名称完全一致"}
        prefix = pool[pool["name"].str.startswith(industry_text)]
        if not prefix.empty:
            row = _prefer_row(prefix)
            return {"ts_code": row["ts_code"], "name": row["name"], "matched_by": "名称前缀"}
    if in_membership:
        row = _prefer_row(pool)
        return {"ts_code": row["ts_code"], "name": row["name"], "matched_by": "成分股"}
    return None


def assemble_bars(daily, basic, days: int, units: dict, note: str = "") -> dict:
    bars = _recent(daily, days)
    turnover = _recent(basic, 0)
    if not bars.empty and not turnover.empty and "turnover_rate" in turnover.columns:
        bars = bars.drop(columns=[column for column in ("turnover_rate",) if column in bars.columns])
        bars = bars.merge(turnover[["trade_date", "turnover_rate"]], on="trade_date", how="left")
    items = _items(bars, _BAR_COLUMNS)
    return {
        "source": "daily",
        "latest_trade_date": _latest(items),
        "count": len(items),
        "units": units,
        "items": items,
        "note": note,
    }


def assemble_flow(frame, days: int) -> dict:
    items = _items(_recent(frame, days), _FLOW_COLUMNS)
    return {
        "source": "moneyflow_dc",
        "latest_trade_date": _latest(items),
        "count": len(items),
        "amount_unit": "万元",
        "items": items,
        "note": "",
    }


def assemble_sector(board: dict | None, history, days: int, note: str = "", as_of: str = "") -> dict:
    items = _items(_recent(history, days), _SECTOR_COLUMNS) if board else []
    return {
        "source": "moneyflow_ind_dc",
        "board": board,
        "latest_trade_date": _latest(items) or as_of,
        "count": len(items),
        "amount_unit": "元",
        "items": items,
        "note": note,
    }


def _rzmre_pct(rzmre, amount):
    if rzmre is None or amount in (None, 0):
        return None
    try:
        base = float(amount) * 1000
        if base == 0:
            return None
        return round(float(rzmre) / base * 100, 4)
    except (TypeError, ValueError):
        return None


def assemble_margin(margin, daily, days: int, note: str = "") -> dict:
    rows = _recent(margin, days)
    amounts = _recent(daily, 0)
    if not rows.empty and not amounts.empty and "amount" in amounts.columns:
        rows = rows.merge(amounts[["trade_date", "amount"]], on="trade_date", how="left")
    items = _items(rows, _MARGIN_COLUMNS + ("amount",))
    for item in items:
        item["rzmre_pct"] = _rzmre_pct(item.get("rzmre"), item.get("amount"))
    return {
        "source": "margin_detail",
        "latest_trade_date": _latest(items),
        "count": len(items),
        "units": {"rzye": "元", "rzmre": "元", "rqye": "元", "amount": "千元", "rzmre_pct": "%"},
        "items": items,
        "note": note,
    }


def assemble_market(indexes: list[tuple[str, str, pd.DataFrame | None]], turnover: dict, days: int, note: str = "") -> dict:
    series = []
    newest = ""
    for ts_code, name, frame in indexes:
        items = _items(_recent(frame, days), _INDEX_COLUMNS)
        if items:
            newest = max(newest, items[-1]["trade_date"])
        series.append({
            "ts_code": ts_code,
            "name": name,
            "amount_unit": "千元",
            "latest_trade_date": _latest(items),
            "count": len(items),
            "items": items,
        })

    dates = set()
    amounts = {"SH_MARKET": {}, "SZ_MARKET": {}}
    for code, frame in turnover.items():
        if code not in amounts:
            continue
        for item in _items(_recent(frame, days), ("trade_date", "amount")):
            amounts[code][item["trade_date"]] = item.get("amount")
            dates.add(item["trade_date"])
    rows = []
    for trade_date in sorted(dates):
        sh = amounts["SH_MARKET"].get(trade_date)
        sz = amounts["SZ_MARKET"].get(trade_date)
        total = None
        if sh is not None and sz is not None:
            total = round(float(sh) + float(sz), 4)
        rows.append({"trade_date": trade_date, "sh": sh, "sz": sz, "total": total})
    if rows:
        newest = max(newest, rows[-1]["trade_date"])
    return {
        "source": "index_daily",
        "latest_trade_date": newest,
        "indexes": series,
        "turnover": {"amount_unit": "亿元", "count": len(rows), "items": rows},
        "note": note,
    }


def assemble_toplist(dates: list[str], rows: list[dict], seats: dict[str, list[dict]], note: str = "") -> dict:
    by_date: dict[str, list[dict]] = {}
    for row in rows:
        trade_date = _date(row.get("trade_date"))
        if not trade_date:
            continue
        item = {column: _cell(row.get(column)) for column in _TOP_COLUMNS if column in row or column == "trade_date"}
        item["trade_date"] = trade_date
        item["seats"] = seats.get(trade_date, [])
        by_date.setdefault(trade_date, []).append(item)
    items = []
    for trade_date in dates:
        items.extend(by_date.get(trade_date, []))
    if not note and dates and not items:
        note = f"近{len(dates)}个交易日未上龙虎榜"
    latest = dates[-1] if dates else ""
    return {
        "source": "top_list",
        "latest_trade_date": latest,
        "lookback_days": len(dates),
        "count": len(items),
        "amount_unit": "元",
        "items": items,
        "note": note,
    }


def get_bars(stock: Stock, days: int = 250) -> dict:
    days = _limit(days, 250, 250)
    key = f"bars:{stock.ts_code}:{days}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    if is_hk(stock):
        from app.services.hk import fetch_daily

        data = assemble_bars(fetch_daily(stock.ts_code, int(days * 1.7) + 20), None, days, _HK_UNITS)
    else:
        data = _ashare_bars(stock.ts_code, days)
    if not data["items"]:
        raise RuntimeError(f"没有查到 {stock.ts_code} 的日线数据")
    cache.set(key, data, config.CACHE_TTL["bars"])
    return data


def _ashare_bars(ts_code: str, days: int) -> dict:
    from app.tools.tushare_client import get_pro

    start, end = _window(days)
    pro = get_pro()
    daily, error = _query(lambda: pro.daily(
        ts_code=ts_code,
        start_date=start,
        end_date=end,
        fields="trade_date,open,high,low,close,pct_chg,vol,amount",
    ))
    if daily is None:
        raise RuntimeError(error or "没有日线数据")
    basic, basic_error = _query(lambda: pro.daily_basic(
        ts_code=ts_code,
        start_date=start,
        end_date=end,
        fields="trade_date,turnover_rate",
    ))
    note = ""
    if basic is None:
        basic = pd.DataFrame()
        note = basic_error or "换手率暂不可用"
    elif basic.empty:
        note = "换手率暂无数据"
    return assemble_bars(daily, basic, days, _A_UNITS, note)


def get_flow_daily(stock: Stock, days: int = 250) -> dict:
    _require_ashare(stock, "资金流数据")
    days = _limit(days, 250, 250)
    key = f"flow:daily:{stock.ts_code}:{days}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    from app.tools.tushare_client import get_pro

    start, end = _window(days)
    frame, error = _query(lambda: get_pro().moneyflow_dc(
        ts_code=stock.ts_code,
        start_date=start,
        end_date=end,
        fields=",".join(_FLOW_COLUMNS),
    ))
    if frame is None:
        raise RuntimeError(error or f"未获取到 {stock.ts_code} 的资金流数据")
    data = assemble_flow(frame, days)
    if not data["items"]:
        raise RuntimeError(f"未获取到 {stock.ts_code} 的资金流数据")
    cache.set(key, data, config.CACHE_TTL["flow_daily"])
    return data


def get_sector(stock: Stock, days: int = 60) -> dict:
    _require_ashare(stock, "板块资金数据")
    days = _limit(days, 60, 120)
    key = f"sector:{stock.ts_code}:{days}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    from app.tools.tushare_client import get_pro

    pro = get_pro()
    dates = _trade_dates(pro, stock.ts_code, 8)
    if not dates:
        raise RuntimeError("没有查到最近交易日")
    boards = pd.DataFrame()
    members = pd.DataFrame()
    as_of = dates[-1]
    note = ""
    for trade_date in reversed(dates[-5:]):
        day_boards, board_error = _industry_boards(pro, trade_date)
        if day_boards is None:
            note = board_error or "板块资金接口暂不可用"
            continue
        if day_boards.empty:
            continue
        if boards.empty:
            boards = day_boards
            as_of = trade_date
        day_members, _member_error = _members(pro, stock.ts_code, trade_date)
        if day_members is not None and not day_members.empty:
            members = day_members
            boards = day_boards
            as_of = trade_date
            note = ""
            break
    if boards.empty:
        data = assemble_sector(None, None, days, note or "没有查到行业板块行情", as_of)
        cache.set(key, data, config.CACHE_TTL["sector"])
        return data
    board = choose_board(stock.industry, members, boards)
    if board is None:
        data = assemble_sector(None, None, days, "没有匹配到行业板块", as_of)
        cache.set(key, data, config.CACHE_TTL["sector"])
        return data
    start, end = _window(days)
    history, error = _query(lambda: pro.moneyflow_ind_dc(
        ts_code=board["ts_code"],
        start_date=start,
        end_date=end,
    ))
    if history is None:
        data = assemble_sector(board, None, days, error or "板块资金接口暂不可用", as_of)
    else:
        data = assemble_sector(board, history, days, as_of=as_of)
        if not data["items"]:
            data["note"] = "没有查到该板块的资金序列"
    cache.set(key, data, config.CACHE_TTL["sector"])
    return data


def _trade_dates(pro, ts_code: str, days: int) -> list[str]:
    start, end = _window(days)
    frame, _error = _query(lambda: pro.daily(ts_code=ts_code, start_date=start, end_date=end, fields="trade_date"))
    if frame is None or frame.empty:
        frame, _error = _query(lambda: pro.index_daily(
            ts_code="000300.SH", start_date=start, end_date=end, fields="trade_date",
        ))
    if frame is None or frame.empty:
        return []
    return _recent(frame, days)["trade_date"].tolist()


def _industry_boards(pro, trade_date: str) -> tuple[pd.DataFrame | None, str]:
    key = f"sector:boards:{trade_date}"
    cached = cache.get(key)
    if cached is not None:
        return pd.DataFrame(cached), ""
    frame, error = _query(lambda: pro.moneyflow_ind_dc(trade_date=trade_date, content_type="行业"))
    if frame is None:
        return None, error
    records = frame.to_dict(orient="records")
    cache.set(key, records, config.CACHE_TTL["sector"])
    return frame, ""


def _members(pro, ts_code: str, trade_date: str) -> tuple[pd.DataFrame | None, str]:
    key = f"sector:member:{ts_code}:{trade_date}"
    cached = cache.get(key)
    if cached is not None:
        return pd.DataFrame(cached), ""
    frame, error = _query(lambda: pro.dc_member(con_code=ts_code, trade_date=trade_date))
    if frame is None:
        return None, error
    records = frame.to_dict(orient="records")
    cache.set(key, records, config.CACHE_TTL["sector"])
    return frame, ""


def get_market(days: int = 60) -> dict:
    days = _limit(days, 60, 120)
    key = f"market:{days}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    from app.tools.tushare_client import get_pro

    pro = get_pro()
    start, end = _window(days)
    notes = []
    indexes = []
    for ts_code, name in _INDEXES:
        frame, error = _query(lambda code=ts_code: pro.index_daily(
            ts_code=code,
            start_date=start,
            end_date=end,
            fields="trade_date,close,pct_chg,vol,amount",
        ))
        if frame is None:
            notes.append(f"{name}暂不可用")
            frame = pd.DataFrame()
        indexes.append((ts_code, name, frame))
    turnover = {}
    for code in ("SH_MARKET", "SZ_MARKET"):
        frame, error = _query(lambda market=code: pro.daily_info(
            ts_code=market,
            start_date=start,
            end_date=end,
            fields="trade_date,ts_code,amount",
        ))
        if frame is None or frame.empty:
            notes.append("全市场成交额暂不可用")
            continue
        turnover[code] = frame
    data = assemble_market(indexes, turnover, days, "；".join(dict.fromkeys(notes)))
    if not any(item["items"] for item in data["indexes"]) and not data["turnover"]["items"]:
        raise RuntimeError(data["note"] or "没有查到大盘数据")
    cache.set(key, data, config.CACHE_TTL["market"])
    return data


def get_margin(stock: Stock, days: int = 60) -> dict:
    _require_ashare(stock, "融资融券数据")
    days = _limit(days, 60, 60)
    key = f"margin:{stock.ts_code}:{days}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    from app.tools.tushare_client import get_pro

    pro = get_pro()
    start, end = _window(days)
    margin, error = _query(lambda: pro.margin_detail(
        ts_code=stock.ts_code,
        start_date=start,
        end_date=end,
        fields="trade_date,rzye,rzmre,rqye",
    ))
    if margin is None:
        data = assemble_margin(None, None, days, error or "融资融券接口暂不可用")
        return data
    daily, daily_error = _query(lambda: pro.daily(
        ts_code=stock.ts_code,
        start_date=start,
        end_date=end,
        fields="trade_date,amount",
    ))
    note = ""
    if daily is None:
        daily = pd.DataFrame()
        note = "成交额暂不可用，未计算融资买入占比"
    elif margin.empty:
        note = "该股票暂无融资融券明细"
    if daily_error and not note:
        note = daily_error
    data = assemble_margin(margin, daily, days, note)
    cache.set(key, data, config.CACHE_TTL["margin"])
    return data


def get_toplist(stock: Stock, days: int = 20) -> dict:
    _require_ashare(stock, "龙虎榜数据")
    days = _limit(days, 20, 30)
    key = f"toplist:{stock.ts_code}:{days}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    from app.tools.tushare_client import get_pro

    pro = get_pro()
    dates = _index_dates(pro, days)
    if not dates:
        raise RuntimeError("没有查到最近交易日")
    rows: list[dict] = []
    seats: dict[str, list[dict]] = {}
    errors = 0
    for trade_date in dates:
        frame, error = _top_list_day(pro, trade_date)
        if frame is None:
            errors += 1
            if errors >= 2 and not rows:
                data = assemble_toplist(dates, [], {}, error or "龙虎榜接口暂不可用")
                return data
            continue
        if frame.empty or "ts_code" not in frame.columns:
            continue
        hit = frame[frame["ts_code"].astype(str) == stock.ts_code]
        if hit.empty:
            continue
        rows.extend(hit.to_dict(orient="records"))
        seat_rows, _seat_error = _top_inst(pro, trade_date, stock.ts_code)
        if seat_rows is not None and not seat_rows.empty:
            seats[trade_date] = _items(seat_rows, _SEAT_COLUMNS)
    data = assemble_toplist(dates, rows, seats)
    cache.set(key, data, config.CACHE_TTL["toplist"])
    return data


def _index_dates(pro, days: int) -> list[str]:
    start, end = _window(days)
    frame, _error = _query(lambda: pro.index_daily(
        ts_code="000300.SH",
        start_date=start,
        end_date=end,
        fields="trade_date",
    ))
    if frame is None or frame.empty:
        return []
    return _recent(frame, days)["trade_date"].tolist()


def _top_list_day(pro, trade_date: str) -> tuple[pd.DataFrame | None, str]:
    key = f"toplist:day:{trade_date}"
    cached = cache.get(key)
    if cached is not None:
        return pd.DataFrame(cached), ""
    frame, error = _query(lambda: pro.top_list(
        trade_date=trade_date,
        fields="trade_date,ts_code,close,pct_change,l_buy,l_sell,net_amount,net_rate,reason",
    ))
    if frame is None:
        return None, error
    cache.set(key, frame.to_dict(orient="records"), config.CACHE_TTL["toplist"])
    return frame, ""


def _top_inst(pro, trade_date: str, ts_code: str) -> tuple[pd.DataFrame | None, str]:
    key = f"toplist:inst:{trade_date}:{ts_code}"
    cached = cache.get(key)
    if cached is not None:
        return pd.DataFrame(cached), ""
    frame, error = _query(lambda: pro.top_inst(
        trade_date=trade_date,
        ts_code=ts_code,
        fields="exalter,buy,sell,net_buy,side",
    ))
    if frame is None:
        return None, error
    cache.set(key, frame.to_dict(orient="records"), config.CACHE_TTL["toplist"])
    return frame, ""
