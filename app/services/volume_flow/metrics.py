"""从对齐后的序列算出面板上的数字，不判断方向。"""

from __future__ import annotations

_SIZES = ("super_large", "large", "medium", "small")
# 相对强弱多窗口：短 / 一周交易日 / 主窗口常用档；和 windows.rows 里的 3、5、20 对齐
REL_WINDOWS = (3, 5, 20)
# 量价图展示窗口：主结论可以是 5 日，图仍看近 20 日更清楚
CHART_WINDOW = 20


def compute(aligned: dict, window: int) -> dict:
    bars = aligned.get("bars") or []
    flow = _by_date(aligned.get("flow") or [])
    sector = _by_date((aligned.get("sector") or {}).get("rows") or [])
    index = _by_date((aligned.get("index") or {}).get("rows") or [])
    end = len(bars)
    start = max(0, end - window)
    chosen = bars[start:end]
    prior = bars[max(0, start - 60):start]
    vol_base = _mean([row["volume"] for row in prior]) if len(prior) >= 60 else None
    current_vol = _mean([row["volume"] for row in chosen])
    rel_vol = None if current_vol is None or not vol_base else round(current_vol / vol_base, 2)
    ratio = _up_down(chosen)
    turnover = _mean([row["turnover"] for row in chosen if row.get("turnover") is not None])
    history = [row["turnover"] for row in bars[-250:] if row.get("turnover") is not None]
    flow_rows = [_sum_size(chosen, flow, size) for size in _SIZES]
    elg = flow_rows[0]["net"]
    large = flow_rows[1]["net"]
    big = None if elg is None and large is None else (elg or 0) + (large or 0)
    concentration = None
    if big is not None and big > 0 and elg is not None and elg > 0:
        concentration = round(elg / big, 4)
    daily = _daily(bars[-10:], flow)
    daily_history = _daily(bars, flow)
    return {
        "price": {
            "bars": [_bar(row) for row in chosen],
            "vol_base": None if vol_base is None else int(round(vol_base)),
            "up_down_vol_ratio": _round(ratio, 2),
            "rel_vol": rel_vol,
            "turnover": _round(turnover, 2),
            "turnover_pctile": _percentile(turnover, history),
            "ret": _round(_cumulative([row.get("chg") for row in chosen]), 2),
        },
        "price_chart": _price_chart(bars),
        "flow": {"rows": flow_rows, "big": big, "concentration": concentration},
        "daily": daily,
        "history": daily_history,
        "relative": _relative(chosen, flow, sector, index, aligned),
        "relative_windows": [
            {"days": days, **_relative(bars[-days:], flow, sector, index, aligned)}
            for days in REL_WINDOWS
        ],
        "windows": [_window_row(bars, flow, days) for days in (3, 5, 10, 20)],
        "signals": _signals(bars, concentration, elg, big, aligned.get("margin_percentile")),
    }


def _price_chart(bars: list[dict]) -> dict:
    """量价图专用：固定近 CHART_WINDOW 日，和主窗口判断脱钩。"""
    chosen = bars[-CHART_WINDOW:]
    start = max(0, len(bars) - CHART_WINDOW)
    prior = bars[max(0, start - 60):start]
    vol_base = _mean([row["volume"] for row in prior]) if len(prior) >= 60 else None
    return {
        "bars": [_bar(row) for row in chosen],
        "vol_base": None if vol_base is None else int(round(vol_base)),
        "days": len(chosen),
    }


def _bar(row: dict) -> dict:
    chg = row.get("chg")
    return {
        "date": row["date"],
        "close": row.get("close"),
        "volume": None if row.get("volume") is None else int(round(row["volume"])),
        "chg": chg,
    }


def _sum_size(bars: list[dict], flow: dict, size: str) -> dict:
    total = 0.0
    seen = False
    for bar in bars:
        row = flow.get(bar["date"])
        if not row or row.get(size) is None:
            continue
        total += row[size]
        seen = True
    return {"size": size, "net": round(total, 2) if seen else None}


def _daily(bars: list[dict], flow: dict) -> dict:
    nets = []
    for bar in bars:
        row = flow.get(bar["date"]) or {}
        elg = row.get("super_large")
        large = row.get("large")
        if elg is None and large is None:
            net = None
        else:
            net = round((elg or 0) + (large or 0), 2)
        nets.append(net)
    levels = _intensity([value or 0 for value in nets])
    days = []
    for bar, net, level in zip(bars, nets, levels):
        days.append({
            "date": bar["date"],
            "net": net,
            "intensity": level,
            "close": bar.get("close"),
            "chg": bar.get("chg"),
        })
    return {
        "days": days,
        "in_days": sum(1 for value in nets if value is not None and value > 0),
        "out_days": sum(1 for value in nets if value is not None and value < 0),
    }


def _intensity(values: list[float]) -> list[int]:
    ordered = sorted(abs(value) for value in values)
    if not ordered:
        return []
    mid = ordered[int(round((len(ordered) - 1) * 0.5))]
    high = ordered[int(round((len(ordered) - 1) * 0.75))]
    levels = []
    for value in values:
        magnitude = abs(value)
        if magnitude <= mid:
            levels.append(1)
        elif magnitude <= high:
            levels.append(2)
        else:
            levels.append(3)
    return levels


def _relative(bars, flow, sector, index, aligned) -> dict:
    stock_ret = _cumulative([row.get("chg") for row in bars])
    sector_rows = [sector.get(row["date"]) for row in bars if sector.get(row["date"])]
    index_rows = [index.get(row["date"]) for row in bars if index.get(row["date"])]
    sector_name = (aligned.get("sector") or {}).get("name") or ""
    index_name = (aligned.get("index") or {}).get("name") or "沪深300"
    stock_rate = _stock_rate(bars, flow)
    sector_rate = _sector_rate(sector_rows)
    gap = None
    if stock_rate is not None and sector_rate is not None:
        gap = round(stock_rate - sector_rate, 2)
    sector_net = sum((row.get("net") or 0) for row in sector_rows)
    return {
        "stock": {"kind": "stock", "name": aligned.get("name") or "", "ret": _round(stock_ret, 2)},
        "sector": {"kind": "sector", "name": sector_name, "ret": _round(_cumulative([row.get("chg") for row in sector_rows]), 2)},
        "index": {"kind": "index", "name": index_name, "ret": _round(_cumulative([row.get("chg") for row in index_rows]), 2)},
        "sector_flow": "" if not sector_rows else "in" if sector_net > 0 else "out",
        "net_rate_vs_sector": gap,
    }


def _window_row(bars: list[dict], flow: dict, days: int) -> dict:
    chosen = bars[-days:]
    start = max(0, len(bars) - days)
    prior = bars[max(0, start - 60):start]
    current = _mean([row["volume"] for row in chosen])
    base = _mean([row["volume"] for row in prior]) if len(prior) >= 60 else None
    rel = None if current is None or not base else round(current / base, 2)
    return {
        "days": days,
        "ret": _round(_cumulative([row.get("chg") for row in chosen]), 2),
        "rel_vol": rel,
        "net_rate": _round(_stock_rate(chosen, flow), 2),
    }


def _signals(bars: list[dict], concentration, super_large, big, margin_percentile) -> dict:
    last = bars[-1] if bars else {}
    recent = bars[-20:]
    high_20 = max((row.get("close") or 0) for row in recent) if recent else None
    year = bars[-250:]
    year_high = max((row.get("close") or 0) for row in year) if year else None
    prior_volume = [row.get("volume") or 0 for row in bars[-500:-1]]
    five = bars[-5:]
    five_start = max(0, len(bars) - 5)
    five_base_rows = bars[max(0, five_start - 60):five_start]
    macd = _macd([row["close"] for row in bars if row.get("close") is not None])
    rsi = _rsi([row["close"] for row in bars if row.get("close") is not None])
    close = last.get("close")
    volume = last.get("volume")
    volume_max = max(prior_volume) if prior_volume else None
    peak = _prior_swing(bars)
    prev_macd = None if peak is None or peak >= len(macd) else macd[peak]
    prev_rsi = None if peak is None or peak >= len(rsi) else rsi[peak]
    prev_bar = None if peak is None else bars[peak]
    return {
        "concentration": concentration,
        "close": close,
        "high_20": high_20,
        "volume": volume,
        "has_prior_peak": prev_bar is not None,
        "prev_high_date": None if prev_bar is None else prev_bar.get("date"),
        "prev_high_volume": None if prev_bar is None else prev_bar.get("volume"),
        "macd": None if not macd else macd[-1],
        "prev_macd": prev_macd,
        "rsi": None if not rsi else rsi[-1],
        "prev_rsi": prev_rsi,
        "five_ret": _cumulative([row.get("chg") for row in five]),
        "five_vol": _mean([row["volume"] for row in five]),
        "base_vol": _mean([row["volume"] for row in five_base_rows]) if len(five_base_rows) >= 60 else None,
        "year_high": year_high,
        "volume_max": volume_max,
        "margin_percentile": margin_percentile,
        "details": {
            "flow_concentration": _concentration_text(concentration, super_large, big),
            "pv_top_divergence": _peak_volume_text(volume, prev_bar),
            "indicator_top_divergence": _indicator_text(
                None if not macd else macd[-1],
                None if not rsi else rsi[-1],
                prev_macd,
                prev_rsi,
                None if prev_bar is None else prev_bar.get("date"),
            ),
            "high_volume_stall": _stall_text(_cumulative([row.get("chg") for row in five]), close, year_high),
            "record_volume": f"当日成交量 {_shares(volume)}，比较区间最大 {_shares(volume_max)}",
            "margin_surge": "暂无全市场融资比较" if margin_percentile is None else f"融资余额 10 日增幅高于全市场 {round(margin_percentile * 100, 1)}% 的股票",
        },
    }


def _stock_rate(bars: list[dict], flow: dict):
    main = 0.0
    amount = 0.0
    seen = False
    for bar in bars:
        row = flow.get(bar["date"])
        if not row or not bar.get("amount"):
            continue
        elg = row.get("super_large")
        large = row.get("large")
        if elg is None and large is None:
            continue
        main += (elg or 0) + (large or 0)
        amount += bar["amount"]
        seen = True
    if not seen or amount == 0:
        return None
    return main / amount * 100


def _sector_rate(rows: list[dict]):
    net = 0.0
    amount = 0.0
    for row in rows:
        rate = row.get("rate")
        value = row.get("net")
        if rate in (None, 0) or value in (None, 0):
            continue
        if (rate > 0) != (value > 0):
            continue
        net += value
        amount += value / (rate / 100)
    if amount == 0:
        return None
    return net / amount * 100


def _cumulative(changes: list) -> float | None:
    value = 1.0
    seen = False
    for change in changes:
        if change is None:
            continue
        value *= 1 + change / 100
        seen = True
    if not seen:
        return None
    return (value - 1) * 100


def _up_down(bars: list[dict]):
    up = [row["volume"] for row in bars if (row.get("chg") or 0) > 0 and row.get("volume") is not None]
    down = [row["volume"] for row in bars if (row.get("chg") or 0) < 0 and row.get("volume") is not None]
    if not up or not down:
        return None
    return (sum(up) / len(up)) / (sum(down) / len(down))


def _percentile(value, history: list) -> float | None:
    if value is None or not history:
        return None
    below = sum(1 for item in history if item < value)
    return round(below / len(history) * 100, 1)


def _mean(values: list):
    usable = [value for value in values if value is not None]
    if not usable:
        return None
    return sum(usable) / len(usable)


def _by_date(rows: list[dict]) -> dict:
    return {row["date"]: row for row in rows if row.get("date")}


def _round(value, digits: int):
    if value is None:
        return None
    return round(float(value), digits)


def _ema(values: list[float], length: int) -> list:
    if len(values) < length:
        return []
    seed = sum(values[:length]) / length
    result = [None] * (length - 1) + [seed]
    factor = 2 / (length + 1)
    prev = seed
    for value in values[length:]:
        prev = value * factor + prev * (1 - factor)
        result.append(prev)
    return result


def _macd(closes: list[float]) -> list:
    fast = _ema(closes, 12)
    slow = _ema(closes, 26)
    if len(fast) != len(closes) or len(slow) != len(closes):
        return []
    line = [
        None if left is None or right is None else left - right
        for left, right in zip(fast, slow)
    ]
    compact = [value for value in line if value is not None]
    signal = _ema(compact, 9)
    hist = [None] * len(closes)
    offset = 0
    for index, value in enumerate(line):
        if value is None:
            continue
        if offset < len(signal) and signal[offset] is not None:
            hist[index] = value - signal[offset]
        offset += 1
    return hist


def _rsi(closes: list[float], length: int = 14) -> list:
    if len(closes) <= length:
        return []
    gains = []
    losses = []
    for prev, close in zip(closes, closes[1:]):
        change = close - prev
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length
    result = [None] * length
    result.append(_rsi_value(avg_gain, avg_loss))
    for gain, loss in zip(gains[length:], losses[length:]):
        avg_gain = (avg_gain * (length - 1) + gain) / length
        avg_loss = (avg_loss * (length - 1) + loss) / length
        result.append(_rsi_value(avg_gain, avg_loss))
    return result


def _rsi_value(gain: float, loss: float) -> float:
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


def _prior_swing(bars: list[dict]):
    """上一个局部高点。和今天至少隔 5 个交易日，中间要有回落。"""
    from app.services.volume_flow.rules import PEAK_GAP

    last = len(bars) - 1
    if last < PEAK_GAP + 1:
        return None
    for index in range(last - PEAK_GAP, 0, -1):
        close = bars[index].get("close")
        prev_close = bars[index - 1].get("close")
        next_close = bars[index + 1].get("close")
        if close is None or prev_close is None or next_close is None:
            continue
        if close < prev_close or close < next_close:
            continue
        between = [row.get("close") for row in bars[index + 1:last] if row.get("close") is not None]
        if not between or min(between) >= close:
            continue
        return index
    return None


def _concentration_text(concentration, super_large, big) -> str:
    if concentration is not None:
        return f"超大单占大资金净流入 {round(concentration * 100, 1)}%"
    if super_large is not None and super_large < 0 and big is not None and big > 0:
        return "超大单净流出，流入全部来自大单，不存在集中风险"
    if big is not None and big <= 0:
        return "大资金净流出，不计算集中度"
    if super_large is not None and super_large <= 0 and big is not None and big > 0:
        return "超大单没有净流入，流入来自大单，不存在集中风险"
    return "暂无足够的资金分档，不计算集中度"


def _peak_volume_text(volume, prev_bar) -> str:
    if prev_bar is None:
        return "没有间隔足够、中间出现回落的前高"
    return f"当日成交量 {_shares(volume)}，前高 {prev_bar.get('date')} 成交量 {_shares(prev_bar.get('volume'))}"


def _indicator_text(macd, rsi, prev_macd, prev_rsi, prev_date) -> str:
    current = f"当前 MACD {_num_text(macd, 4)}，RSI {_num_text(rsi, 1)}"
    if prev_date is None:
        return current
    return f"{current}；前高 {prev_date} MACD {_num_text(prev_macd, 4)}，RSI {_num_text(prev_rsi, 1)}"


def _num_text(value, digits: int) -> str:
    if value is None:
        return "暂无"
    return str(round(value, digits))


def _stall_text(five_ret, close, year_high) -> str:
    ret = "暂无" if five_ret is None else f"{round(five_ret, 2)}%"
    high = "暂无" if not year_high else str(round(year_high, 2))
    price = "暂无" if close is None else str(round(close, 2))
    return f"近5日涨跌 {ret}，收盘 {price}，一年高点 {high}"


def _shares(value) -> str:
    if value is None:
        return "暂无"
    return f"{int(round(value))}股"
