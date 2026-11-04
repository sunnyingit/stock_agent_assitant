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


def mood_signal(indexes: list[dict], breadth: dict, limits: dict) -> dict:
    """指数在 60 日线上的个数、上涨家数占比、昨涨停今日表现和炸板率，各打 +1 / 0 / −1，合计 ≥2 顺，≤−2 逆。"""
    score = 0
    reasons: list[str] = []
    known = [row for row in indexes if row.get("above_ma60") is not None]
    if known:
        above = sum(1 for row in known if row["above_ma60"])
        if above * 4 >= len(known) * 3:
            score += 1
        elif above * 4 <= len(known):
            score -= 1
        reasons.append(f"{len(known)} 个主要指数里 {above} 个在 60 日线上")
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
        return "看个股判断", "所属行业不在这里跟踪的几个板块里，按个股自己的判断"
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
        return 5, f"盈利收益率在近 3 年 {round(pctile * 100)}% 分位，便宜"
    if pctile <= 0.2:
        return 1, f"盈利收益率在近 3 年 {round(pctile * 100)}% 分位，偏贵"
    return 3, f"盈利收益率在近 3 年 {round(pctile * 100)}% 分位，不贵不便宜"


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


# ---- 短线情绪五阶段：冰点 → 修复 → 发酵 → 高潮 → 退潮 ----
STAGES = ("冰点", "修复", "发酵", "高潮", "退潮")
SMOOTH = 3              # 单日数字跳得厉害，用最近 3 天的平均来判
ICE_DOWN, ICE_FOLLOW = 25, 0.5      # 跌停均值不少于 25，且昨涨停没明显赚钱：冰点
EBB_FOLLOW = 0.0                    # 昨涨停今日平均转负：退潮
PEAK_UP, PEAK_FOLLOW = 80, 1.5      # 涨停均值不少于 80 且昨涨停赚 1.5% 以上：高潮
FERMENT_UP, FERMENT_FOLLOW = 45, 0.5


def _avg(values) -> float | None:
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def emotion_stage(window: list[dict]) -> str:
    """window 是最近几天的 {up 涨停数, down 跌停数, follow 昨涨停今日平均涨跌（%）}，最后一个是今天。
    按冰点、退潮、高潮、发酵的顺序判，都不是就算修复。阈值是起始值，按这一年的行情校准过再改。"""
    recent = window[-SMOOTH:]
    up, down, follow = _avg(item.get("up") for item in recent), _avg(item.get("down") for item in recent), _avg(item.get("follow") for item in recent)
    if down is not None and down >= ICE_DOWN and (follow is None or follow < ICE_FOLLOW):
        return "冰点"
    if follow is not None and follow < EBB_FOLLOW:
        return "退潮"
    if up is not None and up >= PEAK_UP and follow is not None and follow >= PEAK_FOLLOW:
        return "高潮"
    if up is not None and up >= FERMENT_UP and follow is not None and follow >= FERMENT_FOLLOW:
        return "发酵"
    return "修复"
