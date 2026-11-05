"""市场页的判定规则。只做计算，不取数，阈值都写在这里。

三档：顺 / 平 / 逆。钱看成交、宽基 ETF 份额、两融；好不好做看指数趋势、涨跌家数、涨停。
国家队还没有接，所以总档位只用已经接上的信号，不拿没有的信号凑数。
"""

from __future__ import annotations

HEAT_HOT = 1.5          # 换手是自身一年均值的这个倍数以上算热
LEVER_FLAT = 0.02       # 行业两融 4 周变化在这个范围内算走平
MACRO_TAILWIND = 4.0
MACRO_HEADWIND = 2.5
MACRO_WEIGHTS = (("policy", 0.35), ("liquidity", 0.25), ("profit", 0.20), ("value", 0.20))
TIER_SCORE = {"顺": 1, "平": 0, "逆": -1}
MONEY_WORD = {"顺": "在进", "平": "观望", "逆": "偏弱"}


def money_tier(label: str) -> str:
    return {"在进": "顺", "偏弱": "逆"}.get(label, "平")


PLUNGE_3D, PLUNGE_5D = -0.04, -0.06   # 上证或沪深 300 三天累计跌超 4%，或五天累计跌超 6%：急剧下跌
PLUNGE_NAMES = ("上证指数", "沪深300")
THRUST = 0.85                          # 上涨家数占 85% 以上：普涨


def trend_vote(indexes: list[dict]) -> dict | None:
    """大盘趋势一票。看 4 个指数在不在 60 日线上；急剧下跌无论在不在线上都算向下；
    指数还在 60 日线下，但已经有 3/4 收复了 20 日线，说明是在反弹、不是在继续下跌，算震荡。
    返回 {score, word, kind, known, above, above20}；kind 说明为什么：急跌、收复、空表示按 60 日线直接判。"""
    known = [row for row in indexes if row.get("above_ma60") is not None]
    if not known:
        return None
    above = sum(1 for row in known if row["above_ma60"])
    above20 = sum(1 for row in known if row.get("above_ma20"))
    out = {"known": len(known), "above": above, "above20": above20, "kind": ""}
    plunged = [row["name"] for row in known if row.get("name") in PLUNGE_NAMES
               and ((row.get("ret3") is not None and row["ret3"] <= PLUNGE_3D) or (row.get("ret5") is not None and row["ret5"] <= PLUNGE_5D))]
    if plunged:
        return {**out, "score": -1, "word": "向下", "kind": "急跌", "plunged": plunged}
    if above * 4 >= len(known) * 3:
        return {**out, "score": 1, "word": "向上"}
    if above * 4 <= len(known):
        if above20 * 4 >= len(known) * 3:
            return {**out, "score": 0, "word": "震荡", "kind": "收复"}
        return {**out, "score": -1, "word": "向下"}
    return {**out, "score": 0, "word": "震荡"}


def mood_signal(indexes: list[dict], breadth: dict, limits: dict) -> dict:
    """趋势、上涨家数占比、昨涨停今日表现和炸板率，各打 +1 / 0 / −1，合计 ≥2 顺，≤−2 逆。"""
    score = 0
    reasons: list[str] = []
    trend = trend_vote(indexes)
    if trend:
        score += trend["score"]
        if trend["kind"] == "急跌":
            reasons.append(f"{'、'.join(trend['plunged'])}短期急跌（3 日跌超 4% 或 5 日跌超 6%）")
        else:
            reasons.append(f"{trend['known']} 个主要指数里 {trend['above']} 个在 60 日线上"
                           + (f"，{trend['above20']} 个已收复 20 日线，先按震荡看" if trend["kind"] == "收复" else ""))
    up, down = breadth.get("up"), breadth.get("down")
    if up is not None and down is not None and up + down:
        share = up / (up + down)
        if share >= 0.55:
            score += 1
        elif share <= 0.40:
            score -= 1
        reasons.append(f"上涨家数占 {round(share * 100)}%")
    follow, broken = limits.get("follow"), limits.get("broken_rate")
    if follow is not None and broken is not None:
        if follow > 0 and broken < 0.30:
            score += 1
        elif follow < -0.5 or broken > 0.40:
            score -= 1
        reasons.append(f"昨天涨停的今天平均 {follow:+.2f}%，炸板率 {round(broken * 100)}%")
    if not reasons:
        return {"tier": None, "label": "没有", "score": 0, "reasons": []}
    tier = "顺" if score >= 2 else "逆" if score <= -2 else "平"
    return {"tier": tier, "label": tier, "score": score, "reasons": reasons}


def overall(signals: list[dict]) -> dict:
    """总档位。signals 里 tier 为 None 的不算。"""
    live = [item for item in signals if item.get("tier")]
    score = sum(TIER_SCORE[item["tier"]] for item in live)
    if not live:
        return {"level": "没有", "tilt": "", "score": 0, "count": 0}
    if score >= 2:
        level, tilt = "顺风", ""
    elif score <= -2:
        level, tilt = "逆风", ""
    else:
        level = "中性"
        tilt = "偏强" if score > 0 else "偏弱" if score < 0 else "均衡"
    return {"level": level, "tilt": tilt, "score": score, "count": len(live)}


def action_text(level: str, tilt: str, good_sectors: list[str]) -> str:
    where = "、".join(good_sectors[:3])
    if level == "顺风":
        return "顺势做，回调到支撑附近再加；每一笔先想好撤的位置。"
    if level == "逆风":
        return "以减仓、保留现金为主，不抢反弹；没有新钱之前，反弹多半只是反弹。"
    if tilt == "偏强":
        return "手里的可以拿着，回调再加；追高之前先看所在板块有没有新钱。"
    if tilt == "偏弱":
        tail = f"想买的话，只看有新钱进的板块：{where}。" if where else "想买的话，先等板块里有新钱进来。"
        return "手里的可以拿着，不加仓、不追高；" + tail
    return "手里的拿着，新仓小一点、分批；先看板块有没有新钱，再决定加不加。"


def sector_layers(flow: float | None, heat: float | None, lever: float | None) -> dict:
    """flow 近 2 周 ETF 净申购（亿元），heat 换手 ÷ 自身一年均值，lever 行业两融 4 周变化。"""
    if flow is None:
        return {"verdict": "没有", "tone": "flat", "lever_word": lever_word(lever)}
    hot = heat is not None and heat >= HEAT_HOT
    word = lever_word(lever)
    if flow > 0:
        verdict = "三层同向" if hot and word != "下降" else "有钱没热度"
    else:
        verdict = "只热不增" if hot else "在流出"
    tone = {"三层同向": "good", "有钱没热度": "good", "只热不增": "warn", "在流出": "bad"}[verdict]
    return {"verdict": verdict, "tone": tone, "lever_word": word}


# ---- 板块状态：钱 × 价 × 热度 × 位置 ----
MONEY_Z = 0.8                    # 近 5 日净流入比自己平时多（少）这么多倍波动，才算钱进（出）
PRICE_EX = 0.02                  # 近 5 日跑赢（跑输）沪深 300 超过 2%，才算价涨（跌）
SECTOR_HOT, SECTOR_COLD = 1.3, 0.85   # 近 5 日日均成交额是近 60 日均值的这个倍数以上算热，以下算冷
POS_HIGH, POS_LOW = 0.8, 0.25    # 收盘在近 60 日区间的位置：高于 0.8 偏高，低于 0.25 偏低
# 展示分组：键、标题、色调
BUCKETS = (
    ("main", "有钱又热 · 主升", "good"),
    ("next", "钱先到、价还没热 · 可能是下一个", "blue"),
    ("dip", "价在跌、钱却在进 · 是抄底，不是确认", "warn"),
    ("hot_only", "涨着、热着，但没新钱 · 别当热点追", "warn"),
    ("out", "钱在撤", "bad"),
    ("flat", "钱进出不明显 · 跟着大盘走", "flat"),
)
MEANING = {
    "主升": "钱、价、热度同向，是现在最强的一类。",
    "温和上行": "钱在进、价在涨，但还没放量，更像慢牛，不是情绪行情。",
    "潜伏": "钱先进来了，价格和热度还没跟上。可能是下一个，也可能只是慢热，要看后面价格能不能动起来。",
    "高位分歧": "钱在进，但价格没涨、位置已经偏高、成交还放大，多空分歧大，要小心见顶。",
    "抄底拉锯": "价格在跌、成交放大，钱却在进：有人抄底，也有人在卖，分歧很大。趋势没转之前不是确认。",
    "逆势申购": "价格在跌，钱却在进：是抄底资金，不是趋势确认。A 股常说「下跌趋势里，抄底是接飞刀」。",
    "只热不增": "涨靠情绪，钱没跟上（申购没增甚至在减）。A 股常说「涨而无钱不长久」，别当热点追。",
    "涨势没钱": "价格在涨，钱却在撤，成交也不热：更像获利了结、反弹。",
    "退潮": "钱在撤、价格在跌。位置低又缩量时，抛压在衰竭，但没转正之前只能算还在退。",
    "钱在撤": "钱在撤，价格没怎么动。",
    "观望": "钱进出不明显，价格也没方向。",
    "跟涨": "钱没有明显进出，价格在涨，成交也不热：是跟着大盘涨，不是资金推动的。",
    "弱势": "钱没有明显撤，但价格在跑输大盘：弱势整理，没有新钱接手。",
}


def sector_phase(mz: float | None, ex5: float | None, heat: float | None, pos: float | None, lever_word_: str = "—",
                 rate: float | None = None) -> dict:
    """板块现在处在哪一段。钱：近 5 日净流入比自己平时多几倍波动（mz）；价：近 5 日相对沪深 300（ex5）；
    热度：近 5 日日均成交额 ÷ 近 60 日均值；位置：在近 60 日区间的分位。
    verdict 沿用旧的四个词（三层同向、有钱没热度、只热不增、在流出），多一个「没方向」；phase 是更细的状态。"""
    if mz is None:
        return {"verdict": None, "phase": "没数据", "tone": "flat", "bucket": "flat", "why": "历史数据不够，判不了。", "meaning": ""}
    money = "进" if mz >= MONEY_Z else "出" if mz <= -MONEY_Z else "平"
    price = "平" if ex5 is None else "涨" if ex5 >= PRICE_EX else "跌" if ex5 <= -PRICE_EX else "平"
    hot = heat is not None and heat >= SECTOR_HOT
    cold = heat is not None and heat <= SECTOR_COLD
    high = pos is not None and pos >= POS_HIGH
    low = pos is not None and pos <= POS_LOW
    if money == "进":
        if price == "涨" and hot:
            phase, bucket, verdict = "主升", "main", ("三层同向" if lever_word_ != "下降" else "有钱没热度")
        elif price == "涨":
            phase, bucket, verdict = "温和上行", "next", "有钱没热度"
        elif price == "跌":
            phase, bucket, verdict = ("抄底拉锯" if hot else "逆势申购"), "dip", "有钱没热度"
        elif high and hot:
            phase, bucket, verdict = "高位分歧", "hot_only", "只热不增"
        else:
            phase, bucket, verdict = "潜伏", "next", "有钱没热度"
    elif money == "出":
        if price == "涨" and hot:
            phase, bucket, verdict = "只热不增", "hot_only", "只热不增"
        elif price == "涨":
            phase, bucket, verdict = "涨势没钱", "hot_only", "在流出"
        elif price == "跌":
            phase, bucket, verdict = "退潮", "out", "在流出"
        elif hot:
            phase, bucket, verdict = "只热不增", "hot_only", "只热不增"
        else:
            phase, bucket, verdict = "钱在撤", "out", "在流出"
    elif price == "涨" and hot:
        phase, bucket, verdict = "只热不增", "hot_only", "只热不增"
    elif price == "涨":
        phase, bucket, verdict = "跟涨", "flat", "没方向"
    elif price == "跌":
        phase, bucket, verdict = "弱势", "flat", "没方向"
    else:
        phase, bucket, verdict = "观望", "flat", "没方向"
    tone = next(item[2] for item in BUCKETS if item[0] == bucket)
    bits = [f"近 5 日净流入比平时{'多' if mz > 0 else '少'} {abs(mz):.1f} 倍波动" + ("" if rate is None else f"（占规模 {rate * 100:+.1f}%）")]
    if ex5 is not None:
        bits.append(f"近 5 日{'跑赢' if ex5 >= 0 else '跑输'}沪深 300 {abs(ex5) * 100:.1f}%")
    if heat is not None:
        bits.append(f"成交是近 60 日均值的 {heat:.1f} 倍")
    if pos is not None:
        bits.append("位置偏高" if high else "位置偏低" if low else "位置适中")
    if lever_word_ in ("回升", "下降"):
        bits.append(f"行业两融{lever_word_}")
    suffix = "。低位缩量，抛压在衰竭" if phase == "退潮" and low and cold else ""
    return {"verdict": verdict, "phase": phase + ("·高位" if phase == "主升" and high else ""), "tone": tone, "bucket": bucket,
            "why": "；".join(bits) + suffix, "meaning": MEANING.get(phase, "")}


def lever_word(lever: float | None) -> str:
    if lever is None:
        return "—"
    if lever > LEVER_FLAT:
        return "回升"
    if lever < -LEVER_FLAT:
        return "下降"
    return "走平"


def watch_action(verdict: str | None, lean: str | None) -> tuple[str, str]:
    """自选的动作词，由板块结论和个股方向按规则拼，不是买卖指令。返回（动作，原因）。"""
    bullish = lean == "bullish"
    if verdict is None:
        return "看个股判断", "所属行业不在这里跟踪的板块里，按个股自己的判断"
    if verdict == "没方向":
        return "看个股判断", "板块钱进出不明显，按个股自己的判断"
    if verdict == "三层同向":
        return ("顺势持有", "板块钱、热度、杠杆同向，个股也偏多") if bullish else ("板块有利，等个股", "板块有钱有热度，但个股自己还没转强")
    if verdict == "有钱没热度":
        return "等资金确认", "板块里钱在进，但热度还没起来"
    if verdict == "只热不增":
        return "不追高", "板块热度靠情绪，ETF 没有新钱"
    return "等回流", "板块 ETF 在被赎回"


def liquidity_score(history: list[dict]) -> tuple[int, str]:
    """看「活不活」：M1 同比回升且 M2−M1 剪刀差收窄给 3 分；剪刀差走阔给 1 分；其余 2 分取 3。"""
    rows = [row for row in history if row.get("m1") is not None and row.get("m2") is not None]
    if len(rows) < 2:
        return 3, "月数不够，按中性"
    gap_now = rows[-1]["m2"] - rows[-1]["m1"]
    gap_prev = rows[-2]["m2"] - rows[-2]["m1"]
    if gap_now > gap_prev:
        return 1, f"M2−M1 剪刀差 {gap_now:.1f} 个百分点，比上月走阔"
    if rows[-1]["m1"] > rows[-2]["m1"]:
        return 3, f"M1 同比回升，剪刀差 {gap_now:.1f} 个百分点在收窄"
    return 3, f"剪刀差 {gap_now:.1f} 个百分点，没有继续走阔"


def profit_score(history: list[dict]) -> tuple[int, str]:
    """PPI 同比连续两个月上行给 5 分，连续两个月下行给 1 分，其余 3 分。"""
    values = [row["ppi"] for row in history if row.get("ppi") is not None]
    if len(values) < 3:
        return 3, "月数不够，按中性"
    up = values[-1] > values[-2] > values[-3]
    down = values[-1] < values[-2] < values[-3]
    if up:
        return 5, f"PPI 同比 {values[-1]:.1f}%，连续 2 个月上行"
    if down:
        return 1, f"PPI 同比 {values[-1]:.1f}%，连续 2 个月下行"
    return 3, f"PPI 同比 {values[-1]:.1f}%，没有连续同向"


def value_score(pctile: float | None) -> tuple[int, str]:
    """沪深 300 盈利收益率（1/PE）在近 3 年里的分位：80% 以上便宜给 5 分，20% 以下贵给 1 分。"""
    if pctile is None:
        return 3, "没有估值数据，按中性"
    if pctile >= 0.8:
        return 5, f"盈利收益率在已取到的样本里 {round(pctile * 100)}% 分位，便宜"
    if pctile <= 0.2:
        return 1, f"盈利收益率在已取到的样本里 {round(pctile * 100)}% 分位，偏贵"
    return 3, f"盈利收益率在已取到的样本里 {round(pctile * 100)}% 分位，不贵不便宜"


def macro_direction(scores: dict[str, int]) -> dict:
    total = round(sum(scores[key] * weight for key, weight in MACRO_WEIGHTS), 2)
    tier = "顺" if total >= MACRO_TAILWIND else "逆" if total <= MACRO_HEADWIND else "平"
    return {"score": total, "tier": tier}


def trend_word(change: float | None, up: str, down: str, band: float = 0.002) -> str:
    if change is None:
        return "没有"
    if change > band:
        return up
    if change < -band:
        return down
    return "持平"


# ---- 成交量：放量、缩量、地量、天量 ----
VOL_UP, VOL_DOWN = 1.2, 0.8     # 今天成交额是前 20 个交易日均值的这个倍数以上算放量，以下算缩量
VOL_FLOOR, VOL_CEIL = 0.10, 0.90  # 近一年分位在最低 10% 算地量，最高 10% 算天量


def volume_state(amount: float | None, prior: list[float], pctile: float | None) -> dict | None:
    """prior 是今天之前的 20 个交易日成交额。地量、天量看近一年分位，放量、缩量看对近 20 日均值的倍数。"""
    values = [value for value in prior if value]
    if amount is None or not values:
        return None
    base = sum(values) / len(values)
    ratio = amount / base
    if pctile is not None and pctile <= VOL_FLOOR:
        word = "地量"
    elif pctile is not None and pctile >= VOL_CEIL:
        word = "天量"
    elif ratio >= VOL_UP:
        word = "放量"
    elif ratio <= VOL_DOWN:
        word = "缩量"
    else:
        word = "平稳"
    return {"word": word, "ratio": ratio, "pctile": pctile, "base": base, "amount": amount}


# ---- 短线情绪五阶段：冰点 → 修复 → 发酵 → 高潮 → 退潮 ----
STAGES = ("冰点", "修复", "发酵", "高潮", "退潮")
# 阈值按 2026-04 到 11 共 130 个交易日定的：跌停中位 11、75 分位 22、90 分位 40；涨停中位 67、75 分位 92；昨涨停今日中位 +1.3%。
ICE_DOWN, ICE_FOLLOW = 25, 0.0            # 跌停不少于 25，且昨涨停今天平均亏钱：冰点
ICE_DOWN_WAVE, ICE_FOLLOW_WAVE = 40, 1.0  # 跌停潮（不少于 40，约占一年里最多的 10% 的天数）：昨涨停没赚到 1% 就算冰点
BREAK_PEAK, BREAK_DROP, BREAK_FOLLOW = 6, 3, 1.5   # 高标断板：昨天最高板不低于 6，今天掉了 3 板以上，昨涨停没赚到 1.5%：退潮
THEME_BREAK = -1.5                        # 昨天的主线（涨停最集中的行业）今天平均亏过 1.5%，且昨涨停整体没赚到 1.5%：主线大面，退潮
EBB_FOLLOW = -0.3                         # 昨涨停今日平均亏过 0.3%：退潮（留一点余地，免得 -0.1 这种噪声也算）
PEAK_UP, PEAK_FOLLOW, PEAK_BROKEN = 100, 2.5, 0.35   # 涨停 100 家以上、昨涨停赚 2.5% 以上、炸板率不高：高潮
FERMENT_UP, FERMENT_FOLLOW = 60, 1.0
FADE_UP = 40            # 发酵、高潮之后涨停数掉到这以下，热度在退
PANIC_DOWN = 60         # 跌停不少于 60，或上证单日跌超 2%：急跌日（恐慌），只是提示，不改阶段


def emotion_stage(window: list[dict]) -> str:
    """window 是到今天为止每天的 {up 涨停数, down 跌停数, follow 昨涨停今日平均涨跌（%）, broken_rate 炸板率, height 最高连板数}，
    更早的天带着自己判出来的 stage。只看今天的数，再看前一天落在哪一段，所以阶段是连着走的：
    冰点、退潮之后要先「修复」一天，才能到「发酵」；「高潮」只能从「发酵」「高潮」走上去；发酵走弱才算退潮。
    冰点和退潮是当天就能看见的，不取平均，一天的亏钱效应不能被前两天的好日子抹掉。
    A 股常说「高标断板，退潮开始」：昨天的最高板一下子掉了三板以上，同样算退潮的信号；
    昨天最集中的那几个行业今天一起亏钱（主线大面），接力就断了，也算。
    涨停、跌停数不含北交所；昨涨停今日的涨跌幅把 20cm 折成 10cm 再平均。"""
    today = window[-1]
    prev = window[-2].get("stage") if len(window) > 1 else None
    up, down, follow, broken = today.get("up"), today.get("down"), today.get("follow"), today.get("broken_rate")
    height, height_prev = today.get("height"), (window[-2].get("height") if len(window) > 1 else None)
    if down is not None and (
        (down >= ICE_DOWN_WAVE and (follow is None or follow < ICE_FOLLOW_WAVE))
        or (down >= ICE_DOWN and follow is not None and follow < ICE_FOLLOW)
    ):
        return "冰点"
    broke = (height is not None and height_prev is not None and height_prev >= BREAK_PEAK and height_prev - height >= BREAK_DROP
             and (follow is None or follow < BREAK_FOLLOW))
    theme = today.get("theme_follow")
    broke = broke or (theme is not None and theme <= THEME_BREAK and (follow is None or follow < BREAK_FOLLOW))
    # 昨涨停还在亏钱，或者高标断板。冰点之后只要昨涨停还是负的就没开始修复；修复里出一天亏钱不改阶段；其余算退潮。
    if (follow is not None and follow < (0 if prev == "冰点" else EBB_FOLLOW)) or (broke and prev != "冰点"):
        return prev if prev in ("冰点", "退潮", "修复") else "退潮"
    reach = None
    if up is not None and follow is not None:
        if up >= PEAK_UP and follow >= PEAK_FOLLOW and (broken is None or broken <= PEAK_BROKEN):
            reach = "高潮"
        elif up >= FERMENT_UP and follow >= FERMENT_FOLLOW:
            reach = "发酵"
    if reach:
        if prev in ("冰点", "退潮"):
            return "修复"
        return "高潮" if reach == "高潮" and prev in ("发酵", "高潮") else "发酵"
    if prev in ("发酵", "高潮"):
        fading = (follow is not None and follow < 0) or (up is not None and up < FADE_UP)
        return "退潮" if fading else "发酵"
    if prev == "退潮" and (follow is None or follow < 0.5):
        return "退潮"
    return "修复"
