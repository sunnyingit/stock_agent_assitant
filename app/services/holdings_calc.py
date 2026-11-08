"""持仓页的计算：从流水算持仓和盈亏，板块集中度，最坏会怎样，操作习惯，想买条件，理由还在不在。只算不取数。

阈值都写在这里。体检只指出问题和方向，不给买卖指令。
"""

from __future__ import annotations

from datetime import date, timedelta
from math import sqrt

SINGLE_LIMIT = 0.30      # 单只占持仓超过这个比例，提示偏重
SECTOR_LIMIT = 0.40      # 同一板块合计超过这个比例，提示压在一处
HIGH_CORR = 0.7          # 两只近 60 天日收益相关超过这个，算走势高度同向
CHASE_GAIN, CHASE_DAYS = 0.20, 15      # 买入前 15 个交易日已经涨了 20% 以上：追高
EARLY_SELL, AFTER_DAYS = 0.10, 20      # 卖出后 20 个交易日又涨超过 10%：卖早了
REVIEW_DAYS = 60
MARKET_DROP, SECTOR_DROP = 0.05, 0.10
NEAR_GAP = 0.05          # 离条件不到 5% 算「快到了」
HOT_TOP = 6              # 体检里热点板块每组最多列几个（再加上你持仓所在的）
BETA_RANGE = (0.3, 2.0)

VERDICT_NOTE = {
    "三层同向": "钱、热度、杠杆同向",
    "有钱没热度": "钱在进，热度或杠杆没跟上",
    "只热不增": "换手高，但没有新钱进来",
    "在流出": "增量为负，钱在往外走",
}
STANCE = {("顺风", ""): "顺势", ("逆风", ""): "以减仓为主", ("中性", "偏强"): "可以持有", ("中性", "偏弱"): "不加仓",
          ("中性", "均衡"): "新仓小一点"}


def stance_of(level: str | None, tilt: str | None) -> str:
    return STANCE.get((level or "", tilt or ""), "")


def market_text(market: dict | None) -> str:
    if not market or not market.get("level") or market["level"] == "没有":
        return ""
    stance = stance_of(market["level"], market.get("tilt"))
    return f"{market['level']}{market.get('tilt') or ''}" + (f" · {stance}" if stance else "")


def no_add(market: dict | None) -> bool:
    """大盘结论是不是在说「不加仓」：逆风，或中性偏弱。"""
    if not market:
        return False
    return market.get("level") == "逆风" or (market.get("level") == "中性" and market.get("tilt") == "偏弱")


# ---------------------------------------------------------------- 流水 → 持仓
def positions(trades: list[dict]) -> tuple[dict[str, dict], dict[int, dict]]:
    """按日期顺序走一遍流水，移动加权平均成本。返回（现在手里有的 {代码: 持仓}，每一笔的标注 {流水 id: 标注}）。

    标注：label 建仓 / 加仓 / 减仓 / 清仓；avg_before、qty_before 是这一笔之前的成本和数量；
    卖出另有 pnl、pnl_pct（相对当时成本）、entry（这一轮的第一笔买入）。
    """
    book: dict[str, dict] = {}
    notes: dict[int, dict] = {}
    for trade in sorted(trades, key=lambda row: (row["date"], row["id"])):
        pos = book.setdefault(trade["ts_code"], {"ts_code": trade["ts_code"], "name": trade["name"], "qty": 0, "avg": 0.0, "since": None, "buys": [], "realized": 0.0})
        pos["name"] = trade["name"]
        note = {"avg_before": pos["avg"], "qty_before": pos["qty"]}
        if trade["side"] == "buy":
            note["label"] = "建仓" if pos["qty"] == 0 else "加仓"
            if pos["qty"] == 0:
                pos["since"], pos["buys"] = trade["date"], []
            total = pos["avg"] * pos["qty"] + trade["price"] * trade["qty"]
            pos["qty"] += trade["qty"]
            pos["avg"] = total / pos["qty"]
            pos["buys"].append(trade)
        else:
            pnl = (trade["price"] - pos["avg"]) * trade["qty"]
            note.update({
                "pnl": pnl, "pnl_pct": trade["price"] / pos["avg"] - 1 if pos["avg"] else 0.0,
                "entry": pos["buys"][0] if pos["buys"] else None, "since": pos["since"],
            })
            pos["qty"] -= trade["qty"]
            pos["realized"] += pnl
            note["label"] = "清仓" if pos["qty"] == 0 else "减仓"
            if pos["qty"] == 0:
                pos["avg"] = 0.0
        notes[trade["id"]] = note
    return {code: pos for code, pos in book.items() if pos["qty"] > 0}, notes


def valuation(held: dict[str, dict], quotes: dict[str, dict], today: date) -> list[dict]:
    """每只持仓的市值、今天盈亏、总盈亏和占比。取不到现价的按成本算，标出来，不拿它当今天的涨跌。"""
    rows = []
    for code, pos in held.items():
        quote = quotes.get(code) or {}
        price = quote.get("price")
        live = bool(price)
        price = price if live else pos["avg"]
        change = quote.get("change") if live else 0.0
        rows.append({
            "ts_code": code, "name": pos["name"], "qty": pos["qty"], "avg": pos["avg"], "price": price, "live": live,
            "change": change, "change_pct": quote.get("change_pct") if live else None,
            "mv": price * pos["qty"], "day_pnl": (change or 0.0) * pos["qty"],
            "pnl": (price - pos["avg"]) * pos["qty"], "pnl_pct": price / pos["avg"] - 1 if pos["avg"] else 0.0,
            "since": pos["since"], "days": (today - date.fromisoformat(pos["since"])).days if pos["since"] else None,
        })
    total = sum(row["mv"] for row in rows)
    for row in rows:
        row["weight"] = row["mv"] / total if total else 0.0
    return rows


def totals(rows: list[dict]) -> dict:
    mv = sum(row["mv"] for row in rows)
    day = sum(row["day_pnl"] for row in rows)
    pnl = sum(row["pnl"] for row in rows)
    cost = sum(row["avg"] * row["qty"] for row in rows)
    return {
        "mv": mv, "day_pnl": day, "day_pct": day / (mv - day) if mv - day else 0.0,
        "pnl": pnl, "pnl_pct": pnl / cost if cost else 0.0, "cost": cost,
    }


# ---------------------------------------------------------------- 价格序列小工具
def _index_on(rows: list[tuple[str, float]], day: str) -> int | None:
    """day 当天或之前最近的一个交易日的下标。"""
    found = None
    for i, (stamp, _close) in enumerate(rows):
        if stamp <= day:
            found = i
        else:
            break
    return found


def ret_after(rows: list[tuple[str, float]], day: str, base: float, n: int) -> tuple[float | None, int]:
    """从 day 起 n 个交易日后相对 base 的涨跌；不够 n 天就用最新一天。返回（涨跌，已过交易日数）。"""
    i = _index_on(rows, day)
    if i is None or not rows or not base:
        return None, 0
    last = min(i + n, len(rows) - 1)
    if last <= i:
        return None, 0
    return rows[last][1] / base - 1, last - i


def run_up(rows: list[tuple[str, float]], day: str, n: int) -> float | None:
    """day 之前 n 个交易日的涨幅（day 当天收盘对 n 天前收盘）。"""
    i = _index_on(rows, day)
    if i is None or i < n or not rows[i - n][1]:
        return None
    return rows[i][1] / rows[i - n][1] - 1


def returns(rows: list[tuple[str, float]], days: int = 60) -> dict[str, float]:
    out = {}
    tail = rows[-(days + 1):]
    for prev, cur in zip(tail, tail[1:]):
        if prev[1]:
            out[cur[0]] = cur[1] / prev[1] - 1
    return out


def beta(stock: list[tuple[str, float]], index: list[tuple[str, float]], days: int = 250) -> float | None:
    """近一年日收益对大盘日收益的回归斜率，限制在 0.3 到 2 之间，免得样本少时估出离谱的数。"""
    a, b = returns(stock, days), returns(index, days)
    keys = sorted(set(a) & set(b))
    if len(keys) < 40:
        return None
    xs, ys = [b[k] for k in keys], [a[k] for k in keys]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    var = sum((x - mx) ** 2 for x in xs)
    if not var:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var
    return max(BETA_RANGE[0], min(BETA_RANGE[1], slope))


def corr(first: list[tuple[str, float]], second: list[tuple[str, float]], days: int = 60) -> float | None:
    a, b = returns(first, days), returns(second, days)
    keys = sorted(set(a) & set(b))
    if len(keys) < 30:
        return None
    xs, ys = [a[k] for k in keys], [b[k] for k in keys]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sx, sy = sqrt(sum((x - mx) ** 2 for x in xs)), sqrt(sum((y - my) ** 2 for y in ys))
    if not sx or not sy:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


# ---------------------------------------------------------------- ① 结构：钱压在哪
def concentration(rows: list[dict], sector_of: dict[str, str], verdicts: dict[str, str | None],
                  correlations: dict[tuple[str, str], float] | None = None) -> dict:
    by: dict[str, dict] = {}
    for row in rows:
        name = sector_of.get(row["ts_code"]) or "其他"
        slot = by.setdefault(name, {"name": name, "weight": 0.0, "mv": 0.0, "stocks": [], "verdict": verdicts.get(name)})
        slot["weight"] += row["weight"]
        slot["mv"] += row["mv"]
        slot["stocks"].append(row["name"])
    sectors = sorted(by.values(), key=lambda item: -item["weight"])
    flags: list[dict] = []
    for item in sectors:
        if item["weight"] >= SECTOR_LIMIT and len(item["stocks"]) >= 1:
            note = VERDICT_NOTE.get(item["verdict"] or "")
            text = f"{'、'.join(item['stocks'])}都在{item['name']}，合计 {item['weight'] * 100:.1f}%" if len(item["stocks"]) > 1 else f"{item['stocks'][0]}所在的{item['name']}占 {item['weight'] * 100:.1f}%"
            if note and item["verdict"] in ("只热不增", "在流出"):
                text += f"；这个板块「{item['verdict']}」：{note}"
            pair = _high_pair(item["stocks"], rows, correlations)
            if pair:
                text += f"。{pair[0]}、{pair[1]}近 60 天走势高度同向（相关 {pair[2]:.2f}），跌的时候会一起跌"
            flags.append({"level": "注意", "title": f"{item['weight'] * 100:.0f}% 的仓位压在同一个板块", "text": text})
    for row in sorted(rows, key=lambda r: -r["weight"]):
        if row["weight"] >= SINGLE_LIMIT:
            flags.append({"level": "注意", "title": "单只偏重", "text": f"{row['name']}占 {row['weight'] * 100:.1f}%，超过单只 {SINGLE_LIMIT * 100:.0f}% 的参考线"})
    good = [item for item in sectors if item["verdict"] == "三层同向"]
    share = sum(item["weight"] for item in good)
    if good:
        flags.append({"level": "正常", "title": "有新钱进的板块", "text": f"有新钱进的板块（三层同向）占 {share * 100:.1f}%：{'、'.join(s for item in good for s in item['stocks'])}"})
    elif rows:
        flags.append({"level": "提示", "title": "没有持仓在有新钱的板块里", "text": "手里的票所在板块都不是「三层同向」，上涨更多要靠个股自己"})
    return {"sectors": sectors, "flags": flags, "good_share": share}


def _high_pair(names: list[str], rows: list[dict], correlations: dict | None) -> tuple[str, str, float] | None:
    if not correlations:
        return None
    code_of = {row["name"]: row["ts_code"] for row in rows}
    best = None
    for i, first in enumerate(names):
        for second in names[i + 1:]:
            value = correlations.get((code_of[first], code_of[second]), correlations.get((code_of[second], code_of[first])))
            if value is not None and value >= HIGH_CORR and (best is None or value > best[2]):
                best = (first, second, value)
    return best


# ---------------------------------------------------------------- ③ 最坏会怎样
def stress(rows: list[dict], sector_of: dict[str, str], betas: dict[str, float | None], triggers: dict[str, float]) -> list[dict]:
    """三种情形各亏多少。betas 取不到的按 1 算；triggers 是每只设的「收盘跌破」价。不是预测。"""
    total = sum(row["mv"] for row in rows)
    if not total:
        return []
    out = []
    loss = {row["ts_code"]: row["mv"] * (betas.get(row["ts_code"]) or 1.0) * MARKET_DROP for row in rows}
    ranked = sorted(rows, key=lambda row: -loss[row["ts_code"]])
    text = f"{'、'.join(row['name'] for row in ranked[:2])}跌得最多（联动最强）"
    if len(ranked) > 2:
        text += f"，{ranked[-1]['name']}相对抗跌"
    out.append({"key": "market", "title": f"大盘再跌 {MARKET_DROP * 100:.0f}%（转逆风）", "text": text, "loss": sum(loss.values()), "pct": sum(loss.values()) / total})
    heavy = max(({"name": name, "mv": sum(r["mv"] for r in rows if (sector_of.get(r["ts_code"]) or "其他") == name),
                  "stocks": [r["name"] for r in rows if (sector_of.get(r["ts_code"]) or "其他") == name]}
                 for name in {sector_of.get(r["ts_code"]) or "其他" for r in rows}), key=lambda item: item["mv"])
    share = heavy["mv"] / total
    out.append({
        "key": "sector", "title": f"{heavy['name']}板块跌 {SECTOR_DROP * 100:.0f}%",
        "text": f"只影响{'、'.join(heavy['stocks'])}" + (f"，但它们占了{share * 100:.0f}%" if share >= 0.25 else ""),
        "loss": heavy["mv"] * SECTOR_DROP, "pct": heavy["mv"] * SECTOR_DROP / total,
    })
    hit = [(row, triggers[row["ts_code"]]) for row in rows if triggers.get(row["ts_code"]) and triggers[row["ts_code"]] < row["price"]]
    unset = [row["name"] for row in rows if not triggers.get(row["ts_code"])]
    cost = sum((row["price"] - level) * row["qty"] for row, level in hit)
    text = "、".join(f"{row['name']}跌破 {level:.2f}" for row, level in hit) if hit else "还没有设价格条件"
    if unset and hit:
        text += f"；{'、'.join(unset)}没设价格条件"
    out.append({"key": "own", "title": "碰到你自己设的价格条件就走", "text": text, "loss": cost, "pct": cost / total})
    return out


# ---------------------------------------------------------------- 理由还在不在
def reason_check(invalid: list[dict], facts: dict) -> dict:
    """对照「什么情况说明我错了」和现在的事实。不读理由原文，只核对能核对的：价格、板块、大资金、AI 方向有没有变。

    facts：price、ai_lean、ai_label、buy_lean（买入那天 AI 的方向）、verdict（所在板块）、flow_out（大资金连续流出天数）、has_ai。
    返回 status：ok 还成立 / doubt 存疑 / broken 不成立。
    """
    price, verdict = facts.get("price"), facts.get("verdict")
    hits, partial = [], []
    for item in invalid or []:
        kind = item.get("kind")
        if kind == "close_below" and price and price <= item["value"]:
            hits.append(f"已跌破你设的 {item['value']:.2f}（现价 {price:.2f}）")
        elif kind == "sector_out" and verdict == "在流出":
            hits.append("所在板块已转为在流出")
        elif kind == "flow_out":
            days = int(item.get("days") or 3)
            streak = int(facts.get("flow_out") or 0)
            if streak >= days:
                hits.append(f"大资金已连续 {streak} 天流出")
            elif streak > 0:
                partial.append(f"大资金流出已出现 {streak} 天（设的是 {days} 天）")
    if hits:
        return {"status": "broken", "line": "；".join(hits)}
    doubts = list(partial)
    lean, buy_lean = facts.get("ai_lean"), facts.get("buy_lean")
    if lean == "bearish":
        doubts.append(f"AI 现在{facts.get('ai_label') or '偏空'}")
    elif buy_lean == "bullish" and lean in ("unclear", "bearish"):
        doubts.append("AI 已经不再偏多")
    if verdict == "在流出":
        doubts.append("所在板块在流出")
    elif verdict == "只热不增":
        doubts.append("所在板块只热不增，涨的主要靠情绪")
    if doubts:
        return {"status": "doubt", "line": "；".join(doubts)}
    bits = []
    if facts.get("has_ai"):
        bits.append(f"AI {facts.get('ai_label') or '不明'}")
    if verdict:
        bits.append(f"板块「{verdict}」")
    unchecked = [item for item in invalid or [] if item.get("kind") == "custom"]
    line = "；".join(bits) if bits else "没有发现和理由矛盾的事实"
    if unchecked:
        line += "；你写的自定义条件要自己盯"
    return {"status": "ok", "line": line}


# ---------------------------------------------------------------- 想买：条件
def wish_state(wish: dict, ctx: dict) -> dict:
    """逐条看条件到没到。ctx：price、ai_lean、ai_label、sector、sector_flow（近 5 日 ETF 净申购，亿元）、
    flow_in（大资金连续流入天数）、market（{level, tilt}）。"""
    price = ctx.get("price")
    rows, auto = [], []
    for cond in wish["conditions"]:
        kind = cond.get("kind")
        row = {"kind": kind, "label": cond.get("label") or "", "met": None, "detail": "", "gap": None}
        if kind == "price_below":
            level = cond["value"]
            row["label"] = row["label"] or f"回踩到 {level:.2f}"
            if price:
                row["met"] = price <= level
                row["gap"] = max(0.0, (price - level) / price)   # 现价还要跌多少才到，和提醒价「还差」同一个算法
                row["detail"] = "已到" if row["met"] else f"还差 {row['gap'] * 100:.1f}%"
        elif kind == "etf_inflow":
            row["label"] = row["label"] or f"{ctx.get('sector') or '板块'} ETF 继续净申购"
            flow = ctx.get("sector_flow")
            if flow is not None:
                row["met"] = flow > 0
                row["detail"] = "已满足" if row["met"] else f"近 5 日 {flow:+.1f} 亿，还没转正"
        elif kind == "flow_in_days":
            days = int(cond.get("days") or 3)
            row["label"] = row["label"] or f"大资金连续 {days} 天流入"
            streak = int(ctx.get("flow_in") or 0)
            row["met"] = streak >= days
            row["detail"] = f"{min(streak, days)} / {days} 天"
        elif kind == "ai_bullish":
            row["label"] = row["label"] or "AI 转为「偏多」"
            row["met"] = ctx.get("ai_lean") == "bullish"
            row["detail"] = f"现在{ctx.get('ai_label') or '还没有判断'}"
        elif kind == "market_ok":
            row["label"] = row["label"] or "大盘不再是「不加仓」"
            market = ctx.get("market")
            if market:
                row["met"] = not no_add(market)
                row["detail"] = f"现在{market_text(market) or '没有结论'}"
        else:
            row["label"] = row["label"] or str(cond.get("text") or "自己盯的条件")
            row["detail"] = "自己盯"
        if row["met"] is not None:
            auto.append(row)
        rows.append(row)
    met = sum(1 for row in auto if row["met"])
    if not auto:
        done = False
    else:
        done = met == len(auto) if wish.get("match", "all") == "all" else met >= 1
    gaps = [row["gap"] for row in auto if row["kind"] == "price_below" and not row["met"] and row["gap"] is not None]
    near = bool(gaps) and min(gaps) <= NEAR_GAP
    if ctx.get("ai_lean") == "bearish":
        status, tone = "AI 和你方向相反", "bad"
    elif done:
        status, tone = "条件到了", "good"
    elif near or (len(auto) > 1 and met >= len(auto) - 1 and met > 0):
        status, tone = "条件快到了", "warn"
    else:
        status, tone = "等条件", "flat"
    return {"conditions": rows, "met": met, "auto": len(auto), "done": done, "status": status, "tone": tone}


def plan_impact(rows: list[dict], sector_of: dict[str, str], new_sector: str | None, plan_pct: float) -> dict:
    """打算买 plan_pct（占现在持仓市值）以后，这只所在板块和手里最重的两个板块占比怎么变。没有持仓时没有「占比」可比。"""
    total = sum(row["mv"] for row in rows)
    if not total or not plan_pct:
        return {"lines": [], "total": total, "amount": 0.0}
    amount = total * plan_pct / 100
    after = total + amount
    weights: dict[str, float] = {}
    for row in rows:
        name = sector_of.get(row["ts_code"]) or "其他"
        weights[name] = weights.get(name, 0.0) + row["mv"]
    lines = []
    target = new_sector or "其他"
    lines.append({"sector": target, "before": weights.get(target, 0.0) / total, "after": (weights.get(target, 0.0) + amount) / after, "buying": True})
    heaviest = [item for item in sorted(weights.items(), key=lambda item: -item[1]) if item[0] != target][:2]
    for name, mv in heaviest:
        lines.append({"sector": name, "before": mv / total, "after": mv / after, "buying": False})
    return {"lines": lines, "total": total, "amount": amount}


# ---------------------------------------------------------------- 买卖记录里的标注
def trade_flags(trade: dict, market_now: dict | None = None) -> dict:
    """这一笔买入当时大盘说不加仓，仍然买了，就是和大盘结论不一致。"""
    market = (trade.get("context") or {}).get("market")
    conflict = trade["side"] == "buy" and no_add(market)
    return {"conflict": conflict, "market_text": market_text(market)}


# ---------------------------------------------------------------- 复盘
def review(trades: list[dict], notes: dict[int, dict], closes: dict[str, list[tuple[str, float]]], last_price: dict[str, float],
           today: date, days: int = REVIEW_DAYS) -> dict:
    since = (today - timedelta(days=days)).isoformat()
    recent = [row for row in trades if row["date"] >= since]
    buys = [row for row in recent if row["side"] == "buy"]
    sells = [row for row in recent if row["side"] == "sell"]
    adds = [row for row in buys if notes[row["id"]]["label"] == "加仓"]
    lean_of = lambda row: (row.get("context") or {}).get("ai", {}).get("lean")  # noqa: E731
    aligned = [row for row in buys if lean_of(row) == "bullish"]
    risky = [row for row in buys if lean_of(row) == "bearish"]
    unclear = [row for row in buys if lean_of(row) == "unclear"]
    risk_rows = []
    for row in risky:
        now = last_price.get(row["ts_code"])
        risk_rows.append({"name": row["name"], "ts_code": row["ts_code"], "date": row["date"], "result": now / row["price"] - 1 if now else None})
    sold = []
    for row in reversed(sells):
        note = notes[row["id"]]
        entry = note.get("entry") or {}
        after, passed = ret_after(closes.get(row["ts_code"]) or [], row["date"], row["price"], AFTER_DAYS)
        ai = (entry.get("context") or {}).get("ai") or {}
        relation = "跟 AI 一致" if ai.get("lean") == "bullish" else "AI 提示风险后仍买" if ai.get("lean") == "bearish" else ""
        sold.append({
            "id": row["id"], "ts_code": row["ts_code"], "name": row["name"], "buy_date": note.get("since") or entry.get("date"),
            "sell_date": row["date"], "pnl_pct": note["pnl_pct"], "pnl": note["pnl"], "after": after, "after_days": passed,
            "after_done": passed >= AFTER_DAYS, "reason": entry.get("reason") or "", "ai_label": ai.get("label") or "",
            "ai_lean": ai.get("lean"), "relation": relation, "note": row.get("note") or "",
            "verdict": sold_verdict(note["pnl_pct"], after, passed >= AFTER_DAYS),
        })
    return {
        "buys": len(buys), "adds": len(adds), "sells": len(sells), "total": len(recent), "aligned": len(aligned),
        "unclear": len(unclear), "risky": len(risky), "risk_rows": risk_rows, "sold": sold, "days": days,
    }


def sold_verdict(pnl_pct: float, after: float | None, done: bool) -> str:
    if after is None:
        return "卖出不久，还没有可比的后续走势"
    span = "卖后 20 个交易日" if done else "卖后至今"
    if pnl_pct >= 0:
        if after >= EARLY_SELL:
            return f"卖早了：赚了 {pnl_pct * 100:.1f}% 卖出，{span}又涨了 {after * 100:.1f}%，超过 {EARLY_SELL * 100:.0f}%"
        if after >= 0:
            return f"兑现：赚了 {pnl_pct * 100:.1f}% 卖出，{span}又涨了 {after * 100:.1f}%，没到「卖早了」的程度"
        return f"兑现：赚了 {pnl_pct * 100:.1f}% 卖出，{span}跌了 {abs(after) * 100:.1f}%，卖在了高位附近"
    if after >= EARLY_SELL:
        return f"没兑现：亏 {abs(pnl_pct) * 100:.1f}% 卖出，{span}又涨了 {after * 100:.1f}%，可能卖在了低位"
    if after <= 0:
        return f"止损有效：亏 {abs(pnl_pct) * 100:.1f}% 卖出，{span}又跌了 {abs(after) * 100:.1f}%，及时卖出少亏了一点"
    return f"没兑现：亏 {abs(pnl_pct) * 100:.1f}% 卖出，{span}基本持平"


def habits(trades: list[dict], notes: dict[int, dict], closes: dict[str, list[tuple[str, float]]], last_price: dict[str, float],
           reasons: dict[str, dict], today: date, days: int = REVIEW_DAYS) -> list[dict]:
    """四条操作习惯，各数出近 60 天的次数和之后 20 天的平均结果。AI 也会错，这里不评判听不听 AI。"""
    since = (today - timedelta(days=days)).isoformat()
    chase, dip, early = [], [], []
    for row in trades:
        if row["date"] < since:
            continue
        rows = closes.get(row["ts_code"]) or []
        note = notes[row["id"]]
        if row["side"] == "buy":
            gain = run_up(rows, row["date"], CHASE_DAYS)
            if gain is not None and gain >= CHASE_GAIN:
                chase.append((row, gain))
            if note["qty_before"] > 0 and row["price"] < note["avg_before"]:
                dip.append(row)
        else:
            after, passed = ret_after(rows, row["date"], row["price"], AFTER_DAYS)
            if after is not None and passed >= AFTER_DAYS and after >= EARLY_SELL:
                early.append((row, after))

    def avg_after(items) -> tuple[str, bool]:
        vals, pending = [], False
        for row in items:
            value, passed = ret_after(closes.get(row["ts_code"]) or [], row["date"], row["price"], AFTER_DAYS)
            if value is None:
                continue
            if passed >= AFTER_DAYS:
                vals.append(value)
            else:
                pending = True
        if vals:
            return f"{sum(vals) / len(vals) * 100:+.1f}%", False
        return ("进行中" if pending else "—"), pending

    dip_detail = "；".join(
        f"{row['name']} {row['date'][5:]} {notes[row['id']]['label']}，目前 {((last_price.get(row['ts_code']) or row['price']) / row['price'] - 1) * 100:+.1f}%"
        for row in dip[:3]
    )
    broken = [(code, item) for code, item in reasons.items() if item.get("status") == "broken"]
    doubt = [(code, item) for code, item in reasons.items() if item.get("status") == "doubt"]
    stuck_detail = "；".join(f"{item['name']}的理由目前「存疑」，还没到证伪" for _code, item in doubt[:2]) if not broken else \
        "；".join(f"{item['name']}的理由已不成立，仍在持有" for _code, item in broken[:2])
    return [
        {"key": "chase", "title": f"追高：买入时 {CHASE_DAYS} 天已涨超过 {CHASE_GAIN * 100:.0f}%", "count": len(chase), "after": avg_after([row for row, _ in chase])[0],
         "detail": "；".join(f"{row['name']} {row['date'][5:]} 买入前已涨 {gain * 100:.0f}%" for row, gain in chase[:3])},
        {"key": "early", "title": f"卖早了：卖出后 {AFTER_DAYS} 天又涨超过 {EARLY_SELL * 100:.0f}%", "count": len(early), "after": avg_after([row for row, _ in early])[0],
         "detail": "；".join(f"{row['name']} {row['date'][5:]} 卖出后涨了 {after * 100:.0f}%" for row, after in early[:3])},
        {"key": "dip", "title": "越跌越买：浮亏时继续买入", "count": len(dip), "after": avg_after(dip)[0], "detail": dip_detail},
        {"key": "stuck", "title": "理由不成立了还拿着", "count": len(broken), "after": "—", "detail": stuck_detail},
    ]


# ---------------------------------------------------------------- 热点板块（和市场页「板块」同一套）
def hot_groups(sectors: list[dict], held_sectors: dict[str, list[tuple[str, float]]]) -> list[dict]:
    """按有没有新钱、热不热分三组。held_sectors：板块名 → [(股票名, 占比)]，和持仓对照。"""
    spec = (
        ("有钱又热 · 真热点", lambda row: row["verdict"] == "三层同向"),
        ("钱在进、还不热 · 可能是下一个", lambda row: row["verdict"] == "有钱没热度"),
        ("看着热、没新钱 · 别当热点追", lambda row: row["verdict"] in ("只热不增", "在流出")),
    )
    out = []
    for title, pick in spec:
        rows = sorted([row for row in sectors if pick(row)], key=lambda row: -(row.get("flow") if row.get("flow") is not None else -1e9))
        if not rows:
            continue
        # 板块有几十个，每组只列最前面的几个，再加上你手里票所在的板块，免得一大片
        mine = [row for row in rows if held_sectors.get(row["name"])]
        top = rows[:HOT_TOP]
        shown = top + [row for row in mine if row not in top]
        out.append({"title": title, "rows": [{**row, "mine": held_sectors.get(row["name"], [])} for row in shown]})
    return out


def suggestions(rows: list[dict], conc: dict, reasons: dict[str, dict], triggers: dict[str, float], alerts: dict[str, int],
                hot: list[dict], held_sector_names: set[str]) -> list[dict]:
    """可以考虑的方向：给条件，不给票。"""
    out = []
    for code, item in reasons.items():
        if item["status"] in ("doubt", "broken"):
            word = "不成立" if item["status"] == "broken" else "存疑"
            out.append({"key": "recheck", "ts_code": code, "title": f"先复核{item['name']}", "text": f"理由{word}：{item['line']}。去诊断看看最新情况。", "action": "diagnose"})
            break
    unset = [row["name"] for row in rows if not triggers.get(row["ts_code"]) and not alerts.get(row["ts_code"])]
    if unset:
        out.append({"key": "alert", "title": f"给没设价格条件的{len(unset)}只补上", "text": f"{'、'.join(unset)}现在跌多少都不会提醒你。可以按买入理由设一个「理由不成立」的价格。", "action": "alert", "codes": unset})
    heavy = next((flag for flag in conc["flags"] if flag["level"] == "注意" and "同一个板块" in flag["title"]), None)
    if heavy:
        out.append({"key": "sector", "title": "新钱先别再放进同一个板块", "text": f"{heavy['title']}。想加仓时，优先看别的板块里有新钱的方向，而不是在这个板块里再买一只。", "action": "market"})
    new_hot = [row["name"] for group in hot[:2] for row in group["rows"] if not row["mine"] and row["name"] not in held_sector_names]
    if new_hot:
        out.append({"key": "hot", "title": "有新钱的板块你还没有", "text": f"{'、'.join(new_hot[:3])}。只指到板块，不推荐具体的票；想找票就去扫描这个板块，挑中的再诊断一遍。", "action": "scan", "sectors": new_hot[:3]})
    return out[:4]


def compare_checkups(now: dict, before: dict | None) -> dict | None:
    """和上次体检比：最重板块的占比、理由存疑 / 不成立的数量、上次提的问题解决了几个。"""
    if not before:
        return None
    lines = []
    prev_top, top = before.get("top_sector") or {}, now.get("top_sector") or {}
    if prev_top.get("name") and top.get("name"):
        lines.append({
            "title": f"{top['name']}占比" if prev_top["name"] == top["name"] else "最重的板块",
            "text": f"{prev_top['weight'] * 100:.1f}% → {top['weight'] * 100:.1f}%" if prev_top["name"] == top["name"] else f"{prev_top['name']} {prev_top['weight'] * 100:.1f}% → {top['name']} {top['weight'] * 100:.1f}%",
            "note": "下降了" if top["weight"] < prev_top["weight"] - 0.005 else "上升了" if top["weight"] > prev_top["weight"] + 0.005 else "没变化",
        })
    lines.append({
        "title": "理由存疑 / 不成立", "text": f"{before.get('weak', 0)} → {now.get('weak', 0)}",
        "note": "新增：" + "、".join(sorted(set(now.get("weak_names") or []) - set(before.get("weak_names") or []))) if set(now.get("weak_names") or []) - set(before.get("weak_names") or []) else "",
    })
    before_issues, now_issues = set(before.get("issues") or []), set(now.get("issues") or [])
    solved = before_issues - now_issues
    lines.append({"title": "上次提的问题", "text": f"{len(before_issues)} 个 · 解决 {len(solved)}", "note": ("没解决：" + "、".join(sorted(before_issues & now_issues))) if before_issues & now_issues else ""})
    return {"lines": lines}
