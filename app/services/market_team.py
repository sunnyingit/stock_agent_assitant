"""国家队：承压托市，以及没承压时的宽基异动。

承压托市要四条同一天都成立，只抓买入。没承压时，一组宽基的偏离绝对值超过 3 就是异动，买入和卖出都记。
都不进市场总档。汇金公告和买入那天对上，才写成确认汇金托市。
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from app.services import market_tape as tape

# 宽基通道：同指数合并后才触发出手、才算钱还在不在。A500 单独成组。
BROAD = (
    ("沪深300", (
        ("510300.SH", "华泰柏瑞沪深300ETF"),
        ("510310.SH", "易方达沪深300ETF"),
        ("510330.SH", "华夏沪深300ETF"),
        ("159919.SZ", "嘉实沪深300ETF"),
    )),
    ("上证50", (
        ("510050.SH", "华夏上证50ETF"),
    )),
    ("中证500", (
        ("510500.SH", "南方中证500ETF"),
        ("159922.SZ", "嘉实中证500ETF"),
        ("512500.SH", "华夏中证500ETF"),
    )),
    ("中证1000", (
        ("512100.SH", "南方中证1000ETF"),
        ("159845.SZ", "华夏中证1000ETF"),
        ("560010.SH", "广发中证1000ETF"),
    )),
    ("创业板", (
        ("159915.SZ", "易方达创业板ETF"),
        ("159977.SZ", "天弘创业板ETF"),
        ("159952.SZ", "广发创业板ETF"),
    )),
    ("科创50", (
        ("588000.SH", "华夏科创50ETF"),
        ("588080.SH", "易方达科创50ETF"),
    )),
    ("A500", (
        ("512050.SH", "华夏中证A500ETF"),
        ("560610.SH", "中证A500ETF"),
    )),
)
# 行业只提示，不触发。同一主题合并后再去跟全市场排名。
INDUSTRY = (
    ("半导体", (
        ("512760.SH", "国泰半导体芯片ETF"),
        ("512480.SH", "国联安半导体ETF"),
        ("159995.SZ", "华夏芯片ETF"),
    )),
    ("医药", (
        ("512170.SH", "华宝医疗ETF"),
        ("512010.SH", "易方达沪深300医药ETF"),
        ("513120.SH", "广发港股创新药ETF"),
    )),
    ("证券", (
        ("512880.SH", "国泰证券ETF"),
        ("512000.SH", "华宝券商ETF"),
    )),
    ("军工", (("512660.SH", "国泰军工ETF"),)),
    ("有色黄金", (
        ("512400.SH", "南方有色金属ETF"),
        ("518880.SH", "华安黄金ETF"),
        ("517520.SH", "永赢黄金股ETF"),
    )),
    ("软件科技", (
        ("515000.SH", "华宝科技ETF"),
        ("512720.SH", "国泰计算机ETF"),
        ("159819.SZ", "易方达人工智能ETF"),
    )),
    ("卫星", (("159206.SZ", "永赢卫星ETF"),)),
    ("旅游", (("159766.SZ", "富国旅游ETF"),)),
)
# 跨境只参加「钱去哪了」的排名，不触发。
CROSS = (
    ("中概互联网", (("513050.SH", "易方达中概互联网ETF"),)),
    ("恒生科技", (
        ("513180.SH", "华夏恒生科技ETF"),
        ("513130.SH", "华泰柏瑞恒生科技ETF"),
    )),
    ("港股通互联网", (("159792.SZ", "富国港股通互联网ETF"),)),
    ("港股通非银", (("513750.SH", "广发港股通非银ETF"),)),
    ("纳指", (("159941.SZ", "广发纳指ETF"),)),
)

def _flat(groups) -> tuple:
    return tuple((code, name) for _group, codes in groups for code, name in codes)


TEAM_BOOK = _flat(BROAD)
INTRADAY_BOOK = tuple(codes[0] for _group, codes in BROAD)
POOL = (("宽基", BROAD), ("行业", INDUSTRY), ("跨境", CROSS))

BASELINE = 60
MZ_ON = 3.0
ABS_FLOOR = 0.005          # 达标的那个窗口，份额变化至少 0.5%
FALLBACK_DAY = 0.01        # 基线不足：当天 > +1%
FALLBACK_5 = 0.02          # 基线不足：5 日 > +2%
SPLIT_RATE = 0.5
GAP_SKIP = 4               # 跨过长假的份额差不当成一个交易日
DROP_HARD = -1.0
DROP_SOFT = -0.5
SLUMP_3D = -2.0
DRAWDOWN = 0.10
DRAWDOWN_NEAR = 0.08
MERGE_GAP = 3
RECENT = 5                 # 托市或异动后 5 个交易日内显示「托市中 / 异动中」
HINT_THEMES = ("半导体", "医药", "有色黄金", "软件科技")
PEAK_DAYS = 10
GONE_MILD = 0.10
GONE_HARD = 0.30
SELL_WINDOW, SELL_MIN, SELL_GROUPS = 3, 2, 3
TOP_N, TOP_BROAD, MIN_GROUPS = 3, 2, 2
# 起步值还没回测。None 表示这一条先不挡，避免编一个金额。
BROAD_MIN_YI = None
REDEEM_MIN_YI = None
AFTER = 20

OFFICIAL = {
    "汇金": ("中央汇金", "汇金"),
    "央行": ("央行", "中国人民银行", "人民银行"),
    "证监会": ("证监会", "金融监管总局"),
    "国务院": ("国务院",),
}
ACTIONS = ("增持", "降准", "降息", "下调", "回购增持再贷款", "再贷款", "设立", "出台", "印发", "宣布", "稳定")
STRONG = ("降准", "降息", "下调存款准备金率", "下调政策利率", "下调贷款市场报价利率")
QUERIES = ("中央汇金 增持", "央行 降准 降息", "央行 回购增持再贷款", "证监会 资本市场 新政")
HUJIN_WORDS = ("中央汇金", "汇金资管", "汇金")
OTHER_TEAMS = (("诚通", "中国诚通"), ("国新", "中国国新"))


def creation_yi(share_delta: float, close: float) -> float:
    """份额单位是万份，乘上元是万元，再换成亿元。"""
    return share_delta * close / 1e4


def _member(share_rows: list[dict], bar_rows: list[dict]) -> dict[str, dict]:
    """拆分当天按比例把份额还原到前一天的口径。金额仍用原始份额差 × 收盘价，拆分当天不记金额。"""
    bars = {}
    for row in bar_rows or []:
        day = tape.ymd(row.get("trade_date"))
        close = tape.num(row.get("close"))
        if day and close is not None:
            bars[day] = {"close": close, "amount": tape.num(row.get("amount"))}
    raw = []
    for row in share_rows or []:
        day = tape.ymd(row.get("trade_date"))
        share = tape.num(row.get("fd_share"))
        if day and share is not None and day in bars:
            raw.append((day, share, bars[day]["close"], bars[day]["amount"]))
    raw.sort()
    factor, prev_raw, prev_day = 1.0, None, None
    out = {}
    for day, share, close, amount in raw:
        split, gap = False, None
        if prev_raw:
            gap = tape._day_gap(prev_day, day)
            if prev_raw > 0 and abs(share / prev_raw - 1) > SPLIT_RATE:
                split = True
                factor *= prev_raw / share
        out[day] = {"adj": share * factor, "raw": share, "close": close, "amount": amount, "split": split, "gap": gap}
        prev_raw, prev_day = share, day
    return out


def _frame(calendar: list[str], members: dict[str, dict]) -> dict:
    """一组里，上市满 60 个交易日的才进入合计。缺一天的那只跳过；成员集合变了，这一天不产生变化率。"""
    seen = {code: {} for code in members}
    running = {code: 0 for code in members}
    for day in calendar:
        for code, series in members.items():
            if day in series:
                running[code] += 1
            seen[code][day] = running[code]
    level, rate, money, turnover = [], [], [], []
    for i, day in enumerate(calendar):
        prev = calendar[i - 1] if i else None
        eligible = [code for code in members if prev and seen[code][prev] >= BASELINE]
        present = [code for code in eligible if day in members[code] and prev in members[code]]
        stable = bool(present) and all(
            day in members[code] and prev in members[code]
            and not members[code][day]["split"] and (members[code][day]["gap"] or 0) <= GAP_SKIP
            for code in eligible
        )
        if stable:
            now = sum(members[code][day]["adj"] for code in eligible)
            before = sum(members[code][prev]["adj"] for code in eligible)
            level.append(now)
            rate.append(None if before <= 0 else now / before - 1)
        else:
            level.append(None)
            rate.append(None)
        yi, traded, have_money, have_trade = 0.0, 0.0, False, False
        for code in present:
            row = members[code][day]
            if row["split"] or (row["gap"] or 0) > GAP_SKIP:
                continue
            yi += creation_yi(row["raw"] - members[code][prev]["raw"], row["close"])
            have_money = True
            if row["amount"]:
                traded += row["amount"]
                have_trade = True
        money.append(yi if have_money else None)
        turnover.append(traded if have_trade else None)
    return {"level": level, "rate": rate, "money": money, "turnover": turnover}


def _score(rates: list, index: int, n: int):
    """n 日累计变化，对照窗口之前 60 个日度变化率。波动按 √n 放大。"""
    if index < 0 or rates[index] is None:
        return None, None, False
    if n == 1:
        window = [rates[index]]
        start = index
    else:
        if index + 1 < n:
            return None, None, False
        window = rates[index + 1 - n:index + 1]
        if any(item is None for item in window):
            return None, None, False
        start = index + 1 - n
    cum = tape._compound(window)
    prior = [item for item in rates[:start] if item is not None][-BASELINE:]
    med, mad, enough = tape._baseline(prior) if len(prior) >= BASELINE else (None, None, False)
    if not enough:
        return cum, None, False
    return cum, tape.modified_z(cum, n * med, mad * math.sqrt(n)), True


def _windows(rates: list, index: int):
    return _score(rates, index, 1), _score(rates, index, 5)


def group_hit(rates: list, index: int, side: str) -> dict:
    """side 为 up 时看申购，为 down 时看赎回。基线不足只对申购退回固定百分比。"""
    (cum1, mz1, ok1), (cum5, mz5, ok5) = _windows(rates, index)
    picked = []
    if ok1 and mz1 is not None and cum1 is not None:
        picked.append((mz1, abs(cum1)))
    if ok5 and mz5 is not None and cum5 is not None:
        picked.append((mz5, abs(cum5)))
    short = not picked
    hit = False
    if picked:
        mz, mag = (max if side == "up" else min)(picked, key=lambda item: item[0])
        hit = (mz > MZ_ON if side == "up" else mz < -MZ_ON) and mag >= ABS_FLOOR
    elif side == "up":
        hit = (cum1 is not None and cum1 > FALLBACK_DAY) or (cum5 is not None and cum5 > FALLBACK_5)
    return {"hit": hit, "short": short and side == "up", "mz1": mz1, "mz5": mz5, "cum1": cum1, "cum5": cum5}


def group_move(rates: list, index: int) -> dict:
    """非承压异动：单日和 5 日里绝对值更大的那个 Mz，超过 3 且幅度至少 0.5%。正是买入，负是卖出。"""
    (cum1, mz1, ok1), (cum5, mz5, ok5) = _windows(rates, index)
    picked = []
    if ok1 and mz1 is not None and cum1 is not None:
        picked.append((mz1, cum1))
    if ok5 and mz5 is not None and cum5 is not None:
        picked.append((mz5, cum5))
    if picked:
        mz, cum = max(picked, key=lambda item: abs(item[0]))
        hit = abs(mz) > MZ_ON and abs(cum) >= ABS_FLOOR
        side = "buy" if mz > 0 else "sell"
        return {"hit": hit, "side": side if hit else "", "short": False, "mz": mz, "cum": cum}
    buy = (cum1 is not None and cum1 > FALLBACK_DAY) or (cum5 is not None and cum5 > FALLBACK_5)
    sell = (cum1 is not None and cum1 < -FALLBACK_DAY) or (cum5 is not None and cum5 < -FALLBACK_5)
    if buy and not sell:
        return {"hit": True, "side": "buy", "short": True, "mz": None, "cum": cum1}
    if sell and not buy:
        return {"hit": True, "side": "sell", "short": True, "mz": None, "cum": cum1}
    return {"hit": False, "side": "", "short": True, "mz": None, "cum": cum1}


def pressure_of(move, cum3, worst3, drawdown, below_ma20) -> dict:
    """承压：大跌、连跌、近 3 日已经在跌、或者深回撤且在 20 日线下。8%–10% 只标注。"""
    kind = None
    if move is not None and move <= DROP_HARD:
        kind = "大跌"
    elif move is not None and cum3 is not None and move <= DROP_SOFT and cum3 <= SLUMP_3D:
        kind = "连跌"
    elif (cum3 is not None and cum3 <= SLUMP_3D) or (worst3 is not None and worst3 <= DROP_HARD):
        kind = "承接"
    if kind is None and drawdown is not None and drawdown > DRAWDOWN and below_ma20:
        kind = "位置"
    near = drawdown is not None and DRAWDOWN_NEAR <= drawdown <= DRAWDOWN
    return {"kind": kind, "on": kind is not None, "near": near, "drawdown": drawdown}


def _pressures(calendar: list[str], pct: dict[str, float], closes: dict[str, float]) -> list[dict]:
    out = []
    for i, day in enumerate(calendar):
        move = pct.get(day)
        recent = [pct[calendar[j]] for j in range(max(0, i - 2), i + 1) if pct.get(calendar[j]) is not None]
        cum3 = sum(recent) if len(recent) == 3 else None
        worst3 = min(recent) if len(recent) == 3 else None
        window = [closes[calendar[j]] for j in range(max(0, i - 119), i + 1) if closes.get(calendar[j])]
        drawdown, below = None, False
        if len(window) >= 120 and window[-1]:
            high = max(window)
            drawdown = (high - window[-1]) / high if high else None
            below = window[-1] < sum(window[-20:]) / 20
        row = pressure_of(move, cum3, worst3, drawdown, below)
        row["move"] = move
        row["cum3"] = cum3
        out.append(row)
    return out


def money_went_broad(amounts: dict[str, float], broad: set[str], side: str = "buy") -> dict:
    """按当天净申购排名。买入看最大的前 3 名，卖出看赎回最多的前 3 名，至少 2 个是宽基。"""
    ranked = [name for name, value in amounts.items() if value is not None]
    ranked.sort(key=lambda name: amounts[name], reverse=side == "buy")
    top = ranked[:TOP_N]
    total = sum(amounts[name] for name in broad if amounts.get(name) is not None)
    same_way = total > 0 if side == "buy" else total < 0
    gross = sum(value for value in amounts.values() if value is not None and (value > 0 if side == "buy" else value < 0))
    part = sum(amounts[name] for name in broad if amounts.get(name) is not None and (amounts[name] > 0 if side == "buy" else amounts[name] < 0))
    return {
        "ok": sum(1 for name in top if name in broad) >= TOP_BROAD and same_way,
        "top": top, "total": total,
        "concentrated": gross != 0 and abs(part) / abs(gross) >= 0.5,
    }


def basket_windows(frames: dict, names: list[str], index: int, spans: tuple[int, ...] = (1, 3, 5, 30, 60)) -> dict:
    """宽基各组在 1/3/5/30/60 日上的偏离，取中位数。样本不够的组不参与。"""
    out = {}
    for n in spans:
        scores = []
        for name in names:
            _cum, mz, ok = _score(frames[name]["rate"], index, n)
            if ok and mz is not None:
                scores.append(mz)
        out[n] = None if not scores else round(tape._median(scores), 2)
    return out


def _multiple(turnover: list, index: int):
    today = turnover[index] if 0 <= index < len(turnover) else None
    prior = [item for item in turnover[:index] if item][-BASELINE:]
    if not today or len(prior) < BASELINE:
        return None
    mid = tape._median(prior)
    return None if not mid else today / mid


def _events(hits: list[dict]) -> list[dict]:
    """相隔不超过 3 个交易日、同一状态、同一方向的触发并成一次。买入和卖出不混在一起。"""
    found = []
    for hit in hits:
        if found and hit["pos"] - found[-1]["end"] <= MERGE_GAP and hit["state"] == found[-1]["state"] and hit["side"] == found[-1]["side"]:
            found[-1]["end"] = hit["pos"]
            found[-1]["groups"].update(hit["groups"])
            found[-1]["days"].append(hit)
            found[-1]["short"] = found[-1]["short"] or hit["short"]
            found[-1]["split"] = found[-1]["split"] or hit["split"]
            found[-1]["broad_ok"] = found[-1]["broad_ok"] or hit["broad_ok"]
            found[-1]["concentrated"] = found[-1]["concentrated"] or hit["concentrated"]
            if hit.get("multiple") and hit["multiple"] >= (found[-1].get("multiple") or 0):
                found[-1]["multiple"] = hit["multiple"]
                found[-1]["multiple_day"] = hit["day"]
                found[-1]["multiple_group"] = hit.get("multiple_group")
        else:
            found.append({
                "pos": hit["pos"], "end": hit["pos"], "day": hit["day"], "groups": set(hit["groups"]),
                "days": [hit], "short": hit["short"], "multiple": hit["multiple"],
                "multiple_day": hit["day"] if hit.get("multiple") else None,
                "multiple_group": hit.get("multiple_group"),
                "state": hit["state"], "side": hit["side"],
                "kind": hit["kind"], "index_pct": hit["index_pct"], "delta": hit["delta"], "top": hit["top"],
                "split": hit["split"], "broad_ok": hit["broad_ok"], "concentrated": hit["concentrated"],
            })
    return found


def giveback(levels: dict[str, list], names: list[str], start: int, side: str = "buy") -> dict | None:
    """买入看从高点回吐了多少，卖出看从低点回补了多少。各组取中位数。窗口是之后 10 个交易日。"""
    ratios, lines = [], []
    for name in names:
        series = levels.get(name) or []
        if start < 1 or start >= len(series):
            continue
        before = series[start - 1]
        window = [item for item in series[start:start + PEAK_DAYS] if item is not None]
        now = next((item for item in reversed(series) if item is not None), None)
        if before is None or not window or now is None:
            continue
        if side == "sell":
            trough = min(window)
            if before <= trough:
                continue
            ratio = (now - trough) / (before - trough)
            line = trough + GONE_HARD * (before - trough)
            lines.append((line / 1e4, now / 1e4, (line - now) / 1e4))
        else:
            peak = max(window)
            if peak <= before:
                continue
            ratio = (peak - now) / (peak - before)
            line = peak - GONE_HARD * (peak - before)
            lines.append((line / 1e4, now / 1e4, (now - line) / 1e4))
        ratios.append(ratio)
    if not ratios:
        return None
    order = sorted(range(len(ratios)), key=lambda i: ratios[i])
    mid = order[len(order) // 2]
    return {"gone": max(0.0, ratios[mid]), "line_yi": lines[mid][0], "now_yi": lines[mid][1], "gap_yi": lines[mid][2], "side": side}


def label_of(kind: str | None, since: int | None, gone: float | None, withdrawing: bool, stated: bool, side: str = "buy") -> str:
    """5 天内是托市中或异动中。之后买入看回吐，卖出看回补。都不进总档。"""
    if kind == "托市" and since is not None and since < RECENT:
        return "托市中"
    if kind == "异动" and since is not None and since < RECENT:
        return "异动中"
    if kind in ("托市", "异动") and side != "sell":
        if withdrawing:
            return "可能在撤"
        if since is not None:
            if gone is None or gone < GONE_MILD:
                return "钱还在"
            if gone <= GONE_HARD:
                return "部分回落"
            return "可能在撤"
    if kind == "异动" and side == "sell" and since is not None:
        if gone is None or gone < GONE_MILD:
            return "还没回补"
        if gone <= GONE_HARD:
            return "部分回补"
        return "明显回补"
    if stated:
        return "已表态"
    return "没托市"


def strength_of(signal: float, since: int) -> float:
    """离开出手日越久越弱，第 5 个交易日降到 0。只在国家队这一路里用。"""
    return signal * max(0.0, 1 - since / RECENT)


def _sell_days(calendar, frames, broad_names, pct) -> list[dict]:
    """不跌过 1% 的日子里，至少 3 组宽基大额赎回。近 3 天里凑满 2 天才算迹象二。"""
    hits = {}
    last = len(calendar) - 1
    for index in range(max(0, last - SELL_WINDOW + 1), last + 1):
        move = pct.get(calendar[index])
        if move is None or move <= DROP_HARD:
            continue
        names = [name for name in broad_names if group_hit(frames[name]["rate"], index, "down")["hit"]]
        total = sum(frames[name]["money"][index] or 0 for name in names)
        if len(names) < SELL_GROUPS:
            continue
        if REDEEM_MIN_YI is not None and total > -REDEEM_MIN_YI:
            continue
        hits[index] = {"day": calendar[index], "pos": index, "groups": names, "delta": total, "index_pct": move}
    if sum(1 for index in range(last - SELL_WINDOW + 1, last + 1) if index in hits) < SELL_MIN:
        return []
    return [hits[index] for index in sorted(hits)]


def history(actions: list[dict], index_days: list[str], pct: dict[str, float]) -> list[dict]:
    rows = []
    for action in actions:
        if action["day"] not in index_days:
            continue
        pos = index_days.index(action["day"])
        until = action.get("until") or action["day"]
        after = index_days[pos + 1:pos + 1 + AFTER]
        row = {
            "day": tape.iso(action["day"]), "until": tape.iso(until), "span": action.get("span") or 1,
            "delta": action["delta"], "index_pct": action["index_pct"],
        }
        if len(after) < AFTER:
            rows.append({**row, "ret20": None, "progress": len(after)})
            continue
        acc = 1.0
        for day in after:
            acc *= 1 + (pct.get(day) or 0) / 100
        rows.append({**row, "ret20": acc - 1, "progress": None})
    return rows


def summarize(shares: dict[str, list[dict]], closes: dict[str, list[dict]], pct: dict[str, float], index_close: dict[str, float] | None = None) -> dict:
    index_close = index_close or {}
    members = {}
    for _kind, groups in POOL:
        for _name, codes in groups:
            for code, _label in codes:
                if code in members:
                    continue
                series = _member(shares.get(code) or [], closes.get(code) or [])
                if series:
                    members[code] = series
    if not members:
        return {"ok": False, "text": "取到的宽基 ETF 份额不够长，判不了出手日"}
    covered = sorted({day for series in members.values() for day in series})
    index_days = sorted(day for day in pct if covered and covered[0] <= day <= max(pct))
    share_last = covered[-1]
    index_last = max(pct) if pct else share_last
    calendar = [day for day in index_days if day <= share_last]
    if len(calendar) < BASELINE + 5:
        return {"ok": False, "text": "取到的宽基 ETF 份额不够长，判不了出手日"}
    frames, kinds = {}, {}
    for kind, groups in POOL:
        for name, codes in groups:
            got = {code: members[code] for code, _label in codes if code in members}
            if not got:
                continue
            frames[name] = _frame(calendar, got)
            kinds[name] = kind
    broad_names = [name for name, kind in kinds.items() if kind == "宽基"]
    if len(calendar) < BASELINE + 5 or not any(kind == "宽基" for kind in kinds.values()):
        return {"ok": False, "text": "取到的宽基 ETF 份额不够长，判不了出手日"}
    pressures = _pressures(calendar, pct, index_close)
    hits = []
    for index, day in enumerate(calendar):
        amounts = {name: frames[name]["money"][index] for name in frames}
        up = {name: group_hit(frames[name]["rate"], index, "up") for name in broad_names}
        moved = {name: group_move(frames[name]["rate"], index) for name in broad_names}
        buy_groups = [name for name, row in moved.items() if row["hit"] and row["side"] == "buy"]
        sell_groups = [name for name, row in moved.items() if row["hit"] and row["side"] == "sell"]
        support_groups = [name for name, row in up.items() if row["hit"]]
        flow_buy = money_went_broad(amounts, set(broad_names), "buy")
        flow_sell = money_went_broad(amounts, set(broad_names), "sell")

        def one(groups, state, side, flow, scored):
            best = None
            for name in groups:
                value = _multiple(frames[name]["turnover"], index)
                if value is not None and (best is None or value > best[0]):
                    best = (value, name)
            return {
                "pos": index, "day": day, "groups": groups, "state": state, "side": side,
                "kind": pressures[index]["kind"], "index_pct": pressures[index]["move"],
                "delta": flow["total"], "top": flow["top"], "split": bool(buy_groups and sell_groups),
                "broad_ok": flow["ok"], "concentrated": flow["concentrated"],
                "short": any(scored[name].get("short") for name in groups),
                "multiple": None if not best else best[0],
                "multiple_group": None if not best else best[1],
            }

        if pressures[index]["on"] and flow_buy["ok"] and len(support_groups) >= MIN_GROUPS:
            hits.append(one(support_groups, "托市", "buy", flow_buy, up))
        elif not pressures[index]["on"]:
            covered = any(hit["state"] == "托市" and 0 <= index - hit["pos"] < 5 for hit in hits)
            if buy_groups and not covered:
                hits.append(one(buy_groups, "异动", "buy", flow_buy, moved))
            if sell_groups:
                hits.append(one(sell_groups, "异动", "sell", flow_sell, moved))
    events = _events(hits)
    last_i = len(calendar) - 1
    last = None
    if events:
        latest_pos = max(item["end"] for item in events)
        latest = [item for item in events if item["end"] == latest_pos]
        support = [item for item in latest if item["state"] == "托市"]
        last = support[-1] if support else max(latest, key=lambda item: (len(item["groups"]), abs(item["delta"] or 0)))
    since = None if not last else last_i - last["pos"]
    side = "buy" if not last else last["side"]
    held = None if not last else giveback({name: frames[name]["level"] for name in frames}, sorted(last["groups"]), last["pos"], side)
    gone = None if not held else held["gone"]
    sells = _sell_days(calendar, frames, broad_names, pct)
    sign2 = bool(sells)
    sign1 = side != "sell" and gone is not None and gone > GONE_HARD and since is not None and since >= RECENT
    withdrawing = sign1 or (sign2 and side != "sell")
    kind = None if not last else last["state"]
    label = label_of(kind, since, gone, withdrawing, False, side)
    signal = {"托市": "疑似托市", "异动": "疑似异动"}.get(kind or "", "")
    # 按这一波结束日算「最近有几次」：长波拖到近几天才结束，也应算进 1/3/5 日，不能只看起点。
    counts = {n: sum(1 for item in events if last_i - item["end"] < n) for n in (1, 3, 5, 30, 60)}
    broad_money = []
    for index in range(len(calendar)):
        vals = [frames[name]["money"][index] for name in broad_names if frames[name]["money"][index] is not None]
        broad_money.append(sum(vals) if vals else None)
    hit_days = {day["day"] for item in events for day in item["days"]}
    series = []
    for index in range(max(1, len(calendar) - 60), len(calendar)):
        day = calendar[index]
        series.append({
            "date": tape.iso(day), "delta": broad_money[index] or 0.0,
            "index_pct": pct.get(day), "index": index_close.get(day),
            "action": day in hit_days,
        })
    industry = []
    for name in HINT_THEMES:
        if name not in frames:
            continue
        row = group_move(frames[name]["rate"], last_i)
        if row["hit"]:
            industry.append(name)
    marks = []
    if pressures[last_i]["near"] and not pressures[last_i]["on"]:
        marks.append("接近承压")
    if last and last["split"]:
        marks.append("组间分化")
    if last and kind == "异动":
        marks.append("市场未承压，可能是调仓或过热降温，非托市")
        if last["broad_ok"]:
            marks.append("广度确认")
        if last["concentrated"]:
            marks.append("集中度确认")
    if industry:
        marks.append("行业提示：" + "、".join(industry))
    if last and last["short"] and label in ("托市中", "异动中"):
        marks.append("基线不足")
    if sign1 and sign2:
        marks.append("迹象较强")
    found = None
    if last and last["pos"] + 1 < len(calendar):
        found = calendar[last["pos"] + 1]
    def pack(item: dict) -> dict:
        days = item["days"]
        peak = max(days, key=lambda row: abs(row.get("delta") or 0))
        until = days[-1]["day"]
        return {
            "day": tape.iso(item["day"]), "until": tape.iso(until), "span": len(days),
            "since": last_i - item["pos"], "groups": sorted(item["groups"]),
            "delta": item["delta"], "peak_day": tape.iso(peak["day"]), "peak_delta": peak["delta"],
            "sum_delta": sum((row.get("delta") or 0) for row in days),
            "kind": item["kind"], "index_pct": item["index_pct"], "top": item["top"],
            "short": item["short"], "multiple": item["multiple"],
            "multiple_day": tape.iso(item["multiple_day"]) if item.get("multiple_day") else "",
            "multiple_group": item.get("multiple_group") or "",
            "state": item["state"], "side": item["side"],
            "found": tape.iso(found) if item is last and found else "",
        }
    today_flow = money_went_broad({name: frames[name]["money"][last_i] for name in frames}, set(broad_names))
    return {
        "ok": True, "label": label, "confirm": None, "signal": signal,
        "confirm_word": signal,
        "codes": sorted({code for code in members}), "counted": sum(1 for code, _n in TEAM_BOOK if code in members),
        "latest": tape.iso(share_last), "stale": index_last > share_last,
        "strength": 0.0 if since is None or label not in ("托市中", "异动中") else strength_of(max((day.get("multiple") or 1) for day in last["days"]), since),
        "last": None if not last else pack(last),
        "hold": None if not held else {**held, "since": tape.iso(last["day"]), "days": since},
        "sell": None if not (sign1 or sign2) else {
            "sign1": sign1, "sign2": sign2, "groups": sells[-1]["groups"] if sells else sorted(last["groups"]) if last else [],
            "delta": sells[-1]["delta"] if sells else None, "index_pct": sells[-1]["index_pct"] if sells else None,
            "days": len(sells), "day": tape.iso(sells[-1]["day"]) if sells else "",
        },
        "marks": marks, "counts": counts, "series": series,
        "actions": [pack(item) for item in events[-6:]],
        "history": history([
            {
                "day": item["day"], "until": item["days"][-1]["day"], "span": len(item["days"]),
                "delta": max(item["days"], key=lambda row: abs(row.get("delta") or 0))["delta"],
                "index_pct": item["index_pct"],
            }
            for item in events
        ], calendar, pct),
        "industry": industry, "today_top": today_flow["top"], "today_broad": today_flow["total"],
        "pressure": pressures[last_i]["kind"] or ("接近承压" if pressures[last_i]["near"] else ""),
        "windows": basket_windows(frames, broad_names, last_i),
    }


def build(today: date) -> dict:
    start = tape._ymd_offset(today, 400)
    end = tape._ymd(today)
    codes = [code for code, _name in _flat(BROAD + INDUSTRY + CROSS)]
    shares = dict(zip(codes, tape._parallel(codes, lambda code: tape.fund_share_rows(code, today))))
    closes = dict(zip(codes, tape._parallel(codes, lambda code: tape.fund_bar_rows(code, today))))
    idx = tape._cached_through(f"indexpx:{start}:{end}", end, lambda: tape.pro().index_daily(
        ts_code="000001.SH", start_date=start, end_date=end, fields="trade_date,pct_chg,close",
    ))
    pct, index_close = {}, {}
    for row in idx:
        day = tape.ymd(row.get("trade_date"))
        if tape.num(row.get("pct_chg")) is not None:
            pct[day] = tape.num(row.get("pct_chg"))
        if tape.num(row.get("close")) is not None:
            index_close[day] = tape.num(row.get("close"))
    return summarize(shares, closes, pct, index_close)


def _stance_row(item: dict, now: datetime, days: int) -> dict | None:
    title = str(item.get("title") or "")
    stamp = str(item.get("time") or item.get("date") or "")[:10]
    if not title or len(stamp) < 10:
        return None
    try:
        when = datetime.strptime(stamp, "%Y-%m-%d")
    except ValueError:
        return None
    if now - when > timedelta(days=days):
        return None
    if not any(word in title for word in ("增持", "增加持有")):
        return None
    who = ""
    if any(word in title for word in HUJIN_WORDS):
        who = "汇金"
    else:
        who = next((name for name, word in OTHER_TEAMS if word in title or name in title), "")
    if not who:
        return None
    return {"date": stamp, "who": who, "title": title, "url": item.get("url") or ""}


def apply_stance(team: dict, items: list[dict], now: datetime | None = None) -> dict:
    """汇金增持的公告和买入那天对上，才写成确认汇金托市，日子不变。对不上、又没有份额上的托市或异动，写成已表态。"""
    if not team.get("ok"):
        return team
    now = now or datetime.now()
    rows, seen = [], set()
    for item in items or []:
        row = _stance_row(item, now, 20)
        if not row or row["title"] in seen:
            continue
        seen.add(row["title"])
        rows.append(row)
    out = {**team, "marks": list(team.get("marks") or []), "stance": [row for row in rows if row["who"] == "汇金"][:3],
           "others": [row for row in rows if row["who"] != "汇金"][:3]}
    huijin = out["stance"]
    if not huijin:
        return out
    action_days = {row["day"] for row in out.get("actions") or [] if row.get("side", "buy") != "sell"}
    matched = False
    for row in huijin:
        for day in action_days:
            if abs((datetime.strptime(row["date"], "%Y-%m-%d") - datetime.strptime(day, "%Y-%m-%d")).days) <= 2:
                matched = True
    if matched:
        out["confirm"] = "L1"
        out["confirm_word"] = "确认汇金托市"
    if not matched:
        if "已表态，还没看到钱进来" not in out["marks"]:
            out["marks"].append("已表态，还没看到钱进来")
        if out.get("label") == "没托市":
            out["label"] = "已表态"
            out["stated"] = huijin[0]
    return out


def official_items(items: list[dict], now: datetime, days: int = 20) -> list[dict]:
    """从新闻搜索结果里留下官方主体 + 明确动作的标题，近 days 天。降准降息仍留给宏观，不改国家队状态。"""
    out, seen = [], set()
    for item in items:
        title = str(item.get("title") or "")
        stamp = str(item.get("time") or "")[:10]
        if not title or title in seen or len(stamp) < 10:
            continue
        try:
            when = datetime.strptime(stamp, "%Y-%m-%d")
        except ValueError:
            continue
        if now - when > timedelta(days=days):
            continue
        who = next((name for name, words in OFFICIAL.items() if any(word in title[:24] for word in words)), "")
        if not who or not any(word in title for word in ACTIONS):
            continue
        strong = (
            any(word in title for word in STRONG)
            or ("汇金" in title and "增持" in title)
            or ("再贷款" in title and any(word in title for word in ("加力", "下调", "扩容")))
        )
        seen.add(title)
        out.append({"date": stamp, "who": who, "title": title, "url": item.get("url") or "", "strong": strong})
    out.sort(key=lambda row: row["date"], reverse=True)
    return out


def announcements(now: datetime | None = None) -> dict:
    from app.services.news import fetch_search_news

    now = now or datetime.now()

    def one(keyword: str) -> list[dict]:
        try:
            return fetch_search_news(keyword, 10)
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=4) as pool:
        batches = list(pool.map(one, QUERIES))
    rows = official_items([item for batch in batches for item in batch], now)
    return {"items": rows[:8], "ok": any(batches)}


def policy_hit(items: list[dict], now: datetime, days: int = 7) -> dict | None:
    """近 days 天有降准降息或汇金增持的官方标题，政策分给 5。"""
    for item in items:
        if not item.get("strong"):
            continue
        if now - datetime.strptime(item["date"], "%Y-%m-%d") <= timedelta(days=days):
            return item
    return None
