"""第一层：各维度按固定算法提出候选价位，代码按定义算数字。

序列用量价模块已对齐的前复权数据。大资金成本区用收盘价近似当日成交均价（对齐序列里没有开高低）。
消息日缺口需要开高低价，第一版没有做。
"""

from __future__ import annotations

from app.synthesis import policy

# id → (类型, 名称, 方向, 合并时的优先级，越小越优先)
MENU = {
    "L1": ("ma20", "20 日均线", "down", 2),
    "L2": ("cost_zone", "大资金成本区", "down", 1),
    "L3": ("pre_news", "消息发布前价位", "down", 3),
    "L4": ("swing_low", "近期波段低点", "down", 4),
    "L11": ("swing_60", "60 日波段低点", "down", 4),
    "L5": ("pattern_invalid", "量价形态失效价", "down", 5),
    "L6": ("volume_cluster", "放量密集区", "down", 6),
    "L7": ("low_20", "20 日最低收盘", "down", 7),
    "L10": ("low_60", "60 日最低收盘", "down", 8),
    "L8": ("low_250", "一年最低收盘", "down", 9),
    "U1": ("prev_high", "前高", "up", 1),
    "U2": ("year_high", "一年高点", "up", 2),
    "U3": ("position_high", "位置变偏高价", "up", 3),
    "U4": ("volume_cluster", "放量密集区", "up", 4),
    "U5": ("ma20", "20 日均线", "up", 5),          # 现价跌到均线下方时，均线变成上方的压力
    "U6": ("cost_zone", "大资金成本区", "up", 6),   # 同理：成本区在现价上方就是压力
    "L9": ("vol_band", "波动带下沿", "down", 10),   # 最后一道兜底：历史收盘价里找不到任何价时才用
    "U9": ("vol_band", "波动带上沿", "up", 9),
}


# 每个价是怎么算出来的（给页面讲清楚用，口径和下面的函数一致）
FORMULA = {
    "20 日均线": "最近 20 个交易日收盘价的平均值",
    "大资金成本区": "最近 20 天里大资金（超大单加大单）净流入的日子，按净流入金额加权算出的平均收盘价",
    "消息发布前价位": "重要利好消息发布前一个交易日的收盘价（盘后发布取当天收盘）",
    "近期波段低点": "最近 20 天里，前后各 3 天收盘价都更高、并且低于现价的那个低点",
    "60 日波段低点": "最近 20 天没有低于现价的波段低点时，改看最近 60 天里同样定义、并且低于现价的低点",
    "量价形态失效价": "收盘价跌回 20 天前的收盘价，近 20 日就不再是上涨，现在的量价判断就不成立",
    "放量密集区": "最近 60 天成交量最集中的那一档价格",
    "20 日最低收盘": "最近 20 天里低于现价的最低收盘。现价自己就是这 20 天的最低时，这条不存在",
    "60 日最低收盘": "最近 60 天里低于现价的最低收盘。和 20 日最低是同一个价时不重复列出",
    "一年最低收盘": "最近 250 天里低于现价的最低收盘。现价已经是一年最低时，这条不存在",
    "波动带下沿": "历史收盘价里已经找不到更低的价时的兜底：现价往下两个「一周典型波动」（最近 20 天日波动 × √5 × 2），限制在 3% 到 15% 之间",
    "波动带上沿": "历史收盘价里已经找不到更高的价时的兜底：现价往上两个「一周典型波动」（最近 20 天日波动 × √5 × 2），限制在 3% 到 15% 之间",
    "前高": "最近 60 天里最高的收盘价",
    "一年高点": "最近 250 天里最高的收盘价",
    "位置变偏高价": "一年高点的 90%：收盘价站上它，就进入离一年高点 10% 以内，位置会变成「偏高」",
}


def _closes(bars: list[dict]) -> list[float]:
    return [float(row["close"]) for row in bars if row.get("close")]


def ma20(bars: list[dict]):
    closes = _closes(bars)[-20:]
    return sum(closes) / len(closes) if len(closes) == 20 else None


def low_below(closes: list[float], days: int) -> float | None:
    """窗口内严格低于最后一根收盘的最低价。现价自己就是最低时，没有这条支撑。"""
    window = closes[-days:] if len(closes) >= days else closes
    if len(window) < 2:
        return None
    current = closes[-1]
    below = [value for value in window if value < current]
    return min(below) if below else None


def swing_low(bars: list[dict], window: int = 20, wing: int = 3):
    closes = _closes(bars)
    found = []
    for index in range(max(wing, len(closes) - window), len(closes) - wing):
        value = closes[index]
        around = closes[index - wing:index] + closes[index + 1:index + 1 + wing]
        if all(other > value for other in around):
            found.append(value)
    return min(found) if found else None


def cost_zone(bars: list[dict], flow: list[dict], days: int = 20):
    by_date = {row.get("date"): row for row in flow}
    weight_sum = 0.0
    total = 0.0
    for bar in bars[-days:]:
        row = by_date.get(bar.get("date"))
        if not row or not bar.get("close"):
            continue
        big = (row.get("super_large") or 0) + (row.get("large") or 0)
        if big > 0:
            weight_sum += big
            total += big * float(bar["close"])
    return total / weight_sum if weight_sum else None


def volume_cluster(bars: list[dict], days: int = 60, bins: int = 20):
    rows = [row for row in bars[-days:] if row.get("close") and row.get("volume")]
    if len(rows) < days // 2:
        return None
    low = min(float(row["close"]) for row in rows)
    high = max(float(row["close"]) for row in rows)
    if high <= low:
        return None
    step = (high - low) / bins
    volumes = [0.0] * bins
    for row in rows:
        slot = min(bins - 1, int((float(row["close"]) - low) / step))
        volumes[slot] += float(row["volume"])
    best = max(range(bins), key=lambda slot: volumes[slot])
    return low + step * (best + 0.5)


def pre_news(bars: list[dict], published: str | None, session: str | None):
    """关键事件发布前一个交易日的收盘；盘后发布取当日收盘。"""
    day = "".join(ch for ch in str(published or "")[:10] if ch.isdigit() or ch == "-")
    if len(day) != 10:
        return None
    pool = [row for row in bars if row.get("close") and row.get("date")]
    if session == "post":
        before = [row for row in pool if row["date"] <= day]
    else:
        before = [row for row in pool if row["date"] < day]
    return float(before[-1]["close"]) if before else None


def pattern_invalid(bars: list[dict], pattern: str | None, window: int = policy.FLOW_WINDOW):
    """上涨形态的条件是窗口涨幅为正：收盘回到窗口起点之前的收盘，形态就失效。"""
    if not pattern or not str(pattern).startswith("up_") or len(bars) <= window:
        return None
    base = bars[-(window + 1)].get("close")
    return float(base) if base else None


def raw_candidates(aligned: dict, pattern: str | None = None, news: dict | None = None) -> list[dict]:
    bars = (aligned or {}).get("bars") or []
    flow = (aligned or {}).get("flow") or []
    if len(bars) < 30 or not bars[-1].get("close"):
        return []
    current = float(bars[-1]["close"])
    closes = _closes(bars)
    # 均线和成本区没有固定方向：在现价下方是支撑（L），在现价上方是压力（U）
    ma = ma20(bars)
    cost = cost_zone(bars, flow)
    prices = {
        "L1" if ma is not None and ma < current else "U5": ma,
        "L2" if cost is not None and cost < current else "U6": cost,
        "L5": pattern_invalid(bars, pattern),
    }
    swing20 = swing_low(bars, 20)
    swing60 = swing_low(bars, 60)
    if swing20 is not None and swing20 < current:
        prices["L4"] = swing20
    if swing60 is not None and swing60 < current and swing60 != swing20:
        prices["L11"] = swing60
    if news and news.get("impact") == "positive":
        prices["L3"] = pre_news(bars, news.get("published"), news.get("session"))
    cluster = volume_cluster(bars)
    if cluster is not None:
        prices["L6" if cluster < current else "U4"] = cluster
    low20 = low_below(closes, 20)
    low60 = low_below(closes, 60)
    low250 = low_below(closes, 250)
    if low20 is not None:
        prices["L7"] = low20
    if low60 is not None and low60 != low20:
        prices["L10"] = low60
    if low250 is not None and low250 not in (low20, low60):
        prices["L8"] = low250
    prices["U1"] = max(closes[-60:])
    prices["U2"] = max(closes[-250:])
    if len(closes) >= 60:
        prices["U3"] = max(closes[-250:]) * (1 - policy.POSITION_HIGH_FROM_HIGH)
    rows = []
    for cid, price in prices.items():
        kind, name, direction, priority = MENU[cid]
        if price is None:
            continue
        if direction == "down" and price >= current:
            continue
        if direction == "up" and price <= current:
            continue
        rows.append({
            "id": cid, "type": kind, "name": name, "dir": direction, "priority": priority,
            "price": round(price, 4), "dist": round(price / current - 1, 4),
        })
    return rows


def screen(rows: list[dict]) -> list[dict]:
    """只留离现价 3%–15% 的；相差不到 1% 的合并，来源一起列出。"""
    kept = [row for row in rows if policy.DIST_MIN <= abs(row["dist"]) <= policy.DIST_MAX]
    merged: list[dict] = []
    for direction in ("down", "up"):
        side = sorted((row for row in kept if row["dir"] == direction), key=lambda row: row["priority"])
        groups: list[list[dict]] = []
        for row in side:
            for group in groups:
                if abs(row["price"] / group[0]["price"] - 1) < policy.MERGE_GAP:
                    group.append(row)
                    break
            else:
                groups.append([row])
        for group in groups:
            head = dict(group[0])
            head["sources"] = [item["name"] for item in group]
            head["merged_ids"] = [item["id"] for item in group]
            if len(group) > 1:
                head["name"] = " + ".join(head["sources"])
            merged.append(head)
    return merged


def volatility_row(aligned: dict | None, side: str) -> dict | None:
    """最后一道兜底：现价创了新低（新高）时，那一侧在历史收盘价里没有任何可参照的价。

    这时用最近 20 天的日波动估一个"一周典型波动"，取两倍，限制在 3%–15% 之间。有算法、可核对，但没有验证过后果。
    """
    bars = (aligned or {}).get("bars") or []
    closes = _closes(bars)
    if len(closes) < 6:
        return None
    recent = closes[-21:]
    returns = [recent[i] / recent[i - 1] - 1 for i in range(1, len(recent))]
    mean = sum(returns) / len(returns)
    sigma = (sum((value - mean) ** 2 for value in returns) / len(returns)) ** 0.5
    move = min(max(sigma * 5 ** 0.5 * 2, policy.DIST_MIN), policy.DIST_MAX)
    current = closes[-1]
    cid = "L9" if side == "down" else "U9"
    kind, name, direction, priority = MENU[cid]
    price = current * (1 - move) if side == "down" else current * (1 + move)
    return {
        "id": cid, "type": kind, "name": name, "dir": direction, "priority": priority,
        "price": round(price, 4), "dist": round(price / current - 1, 4),
        "sources": [name], "merged_ids": [cid], "consequence": "", "verified": False,
    }


def fallback_rows(rows: list[dict], side: str, limit: int = 2) -> list[dict]:
    """某个方向一个候选都没有通过验证时，退而求其次：离 3%–15% 范围最近的几个，没有验证过后果。

    这一步保证上下价位不为空；页面上会标明"没有通过后果验证"，不编造后果。
    """
    def gap(row: dict) -> float:
        dist = abs(row["dist"])
        return max(policy.DIST_MIN - dist, dist - policy.DIST_MAX, 0.0)

    pool = sorted((row for row in rows if row["dir"] == side), key=lambda row: (gap(row), row["priority"]))
    out: list[dict] = []
    for row in pool:
        twin = next((item for item in out if abs(row["price"] / item["price"] - 1) < policy.MERGE_GAP), None)
        if twin is not None:   # 价格几乎一样的合并，和主路径一致
            twin["sources"].append(row["name"])
            twin["merged_ids"].append(row["id"])
            twin["name"] = " + ".join(twin["sources"])
            continue
        if len(out) < limit:
            item = dict(row)
            item.update({"sources": [row["name"]], "merged_ids": [row["id"]], "consequence": "", "verified": False})
            out.append(item)
    return out
