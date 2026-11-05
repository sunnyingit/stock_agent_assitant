"""持仓页的接口数据：把流水、现价、AI 判断、板块判断拼成页面要的样子，并处理「记一笔」「加想买」这些写入。

规则在 holdings_calc，外部数据在 holdings_data，存的东西在 holdings_store；这里只组装。
这里只说 AI 怎么看、离改判还差什么、理由还在不在，不给买卖指令。
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from app.services import holdings_calc as calc
from app.services.holdings_store import HoldingsStore

LEAN_WORD = {"bullish": "偏多", "bearish": "偏空", "unclear": "不明"}
LEVEL_WORD = {0: "", 1: "稍微", 2: "比较明显", 3: "明显"}
VERDICT_TONE = {"三层同向": "good", "有钱没热度": "good", "只热不增": "warn", "在流出": "bad"}
REASON_WORD = {"ok": "还成立", "doubt": "存疑", "broken": "不成立", "none": "没写理由"}
SOURCE_WORD = {"scan": "来自扫描", "sold": "来自卖出后", "diagnose": "来自你诊断", "manual": "自己加的"}
ALERT_TAG = "理由不成立"       # 买入时「什么情况说明我错了」写成的价格提醒，用这个名字和别的提醒分开
CONDITION_KINDS = ("price_below", "etf_inflow", "flow_in_days", "ai_bullish", "market_ok", "custom")
INVALID_KINDS = ("close_below", "sector_out", "flow_out", "custom")


def judge_label(lean: str | None, level: int | None) -> str:
    word = LEAN_WORD.get(lean or "", "不明")
    strength = LEVEL_WORD.get(level or 0, "")
    return word + (f" · {strength}" if strength and lean != "unclear" else "")


def _day_text(day: str) -> str:
    return day[5:] if day else ""


class Context:
    """一次请求里共用的状态：流水、持仓、现价、每只票的 AI 和板块，算过一次就留着。"""

    def __init__(self, store: HoldingsStore, data, today: date | None = None, inbox=None):
        self.store, self.data = store, data
        self.inbox = inbox            # 通知收件箱（NotifyStore）；没传就不并入，测试里默认如此
        self.today = today or date.today()
        self._memo: dict = {}

    def memo(self, key, make):
        if key not in self._memo:
            self._memo[key] = make()
        return self._memo[key]

    @property
    def trades(self) -> list[dict]:
        return self.memo("trades", self.store.trades)

    @property
    def ledger(self) -> tuple[dict, dict]:
        return self.memo("ledger", lambda: calc.positions(self.trades))

    @property
    def held(self) -> dict[str, dict]:
        return self.ledger[0]

    @property
    def notes(self) -> dict[int, dict]:
        return self.ledger[1]

    @property
    def wishes(self) -> list[dict]:
        return self.memo("wishes", self.store.wishes)

    @property
    def names(self) -> dict[str, str]:
        return self.memo("names", lambda: {**{item["ts_code"]: item["name"] for item in self.wishes}, **{code: pos["name"] for code, pos in self.held.items()}})

    @property
    def quotes(self) -> dict[str, dict]:
        return self.memo("quotes", lambda: self.data.quotes(self.names) if self.names else {})

    @property
    def rows(self) -> list[dict]:
        return self.memo("rows", lambda: calc.valuation(self.held, self.quotes, self.today))

    @property
    def total(self) -> dict:
        return self.memo("total", lambda: calc.totals(self.rows))

    @property
    def market(self) -> dict | None:
        return self.memo("market", self.data.market)

    def row(self, code: str) -> dict | None:
        return next((row for row in self.rows if row["ts_code"] == code), None)

    def price(self, code: str) -> float | None:
        quote = self.quotes.get(code) or {}
        return quote.get("price") or (self.row(code) or {}).get("price")

    def ai(self, code: str) -> dict:
        return self.memo(("ai", code), lambda: self.data.ai(code))

    def sector(self, code: str) -> dict:
        return self.memo(("sector", code), lambda: self.data.sector(code))

    def closes(self, code: str) -> list[tuple[str, float]]:
        return self.memo(("closes", code), lambda: self.data.closes(code))

    def flow(self, code: str) -> dict:
        return self.memo(("flow", code), lambda: self.data.flow(code))

    def alerts(self, code: str) -> list[dict]:
        return self.memo(("alerts", code), lambda: self.data.alerts(code))

    def warm(self, codes: list[str]) -> None:
        """几只票的 AI 和板块一起取，第一次打开不用一只一只等。"""
        codes = [code for code in dict.fromkeys(codes) if ("ai", code) not in self._memo]
        if not codes:
            return
        with ThreadPoolExecutor(max_workers=6) as pool:
            for code, ai, sector in zip(codes, pool.map(self.data.ai, codes), pool.map(self.data.sector, codes)):
                self._memo[("ai", code)] = ai
                self._memo[("sector", code)] = sector

    def warm_all(self) -> None:
        self.quotes  # noqa: B018 先取现价，名字都在 names 里
        self.warm(list(self.names))

    def reason(self, code: str) -> dict:
        return self.memo(("reason", code), lambda: self._reason(code))

    def _reason(self, code: str) -> dict:
        pos = self.held.get(code)
        stored = self.store.reason(code)
        first = (pos or {}).get("buys", [None])[0] if pos and pos["buys"] else None
        if stored:
            text, invalid, written = stored["text"], stored["invalid"], stored["written_at"]
        elif first and (first.get("reason") or first.get("invalid")):
            text, invalid, written = first.get("reason") or "", first.get("invalid") or [], first["date"]
        else:
            text, invalid, written = "", [], ""
        if not text and not invalid:
            return {"text": "", "invalid": [], "written_at": "", "status": "none", "status_text": REASON_WORD["none"], "line": "还没写下为什么买"}
        ai, sector = self.ai(code), self.sector(code)
        facts = {
            "price": self.price(code), "ai_lean": ai.get("lean"), "ai_label": ai.get("label"), "has_ai": ai.get("has"),
            "buy_lean": ((first or {}).get("context") or {}).get("ai", {}).get("lean"), "verdict": sector.get("verdict"),
            "flow_out": self.flow(code)["out"] if any(item.get("kind") == "flow_out" for item in invalid) else 0,
        }
        check = calc.reason_check(invalid, facts)
        return {"text": text, "invalid": invalid, "written_at": written, "status": check["status"], "status_text": REASON_WORD[check["status"]], "line": check["line"]}

    def triggers(self) -> dict[str, float]:
        """每只持仓离现价最近的一个「收盘跌破」价（提醒表里的下破提醒）。"""
        out = {}
        for row in self.rows:
            below = [item["price"] for item in self.alerts(row["ts_code"]) if item["side"] == "down" and item["price"] < row["price"]]
            if below:
                out[row["ts_code"]] = max(below)
        return out


# ---------------------------------------------------------------- 小工具
def _market_block(market: dict | None) -> dict | None:
    return None if not market else {"level": market["level"], "tilt": market["tilt"], "text": market["text"], "stance": market["stance"], "action": market.get("action", "")}


def _sector_block(sector: dict) -> dict:
    verdict = sector.get("verdict")
    return {"name": sector.get("name") or "", "verdict": verdict, "tone": VERDICT_TONE.get(verdict or "", "flat"),
            "flow": sector.get("flow"), "heat": sector.get("heat"), "source": sector.get("source", ""), "tracked": bool(sector.get("tracked"))}


def _ai_block(ai: dict) -> dict | None:
    if not ai.get("has"):
        return None
    position = ai.get("position")
    label = ai["label"] or "不明"
    if position in ("偏高", "偏低") and ai.get("lean") != "bullish":
        label += f" · 位置{position}"
    return {"lean": ai["lean"], "level": ai["level"], "label": label, "change": ai.get("change") or "", "stale": ai.get("stale"), "judged_at": ai.get("judged_at")}


def flip_text(ai: dict) -> str:
    """离改判还差什么：综合判断里列的「什么出现会改变判断」，一条算一条。"""
    if not ai.get("has"):
        return ""
    count = len(ai.get("watch") or [])
    if not count:
        return ""
    return f"{'改判' if ai['lean'] == 'bullish' else '转偏多'}还差 {count} 条"


def _alert_view(alert: dict, price: float | None) -> dict:
    gap = (price - alert["price"]) / price if price else None
    reached = bool(price) and price <= alert["price"]
    return {
        "id": alert["id"], "price": alert["price"], "side": alert["side"],
        "gap": gap, "reached": reached, "tag": alert.get("base_name") or "",
        "text": f"收盘跌破 {alert['price']:.2f}" if alert["side"] == "down" else f"涨到 {alert['price']:.2f}",
        "detail": ("已到价" if reached else f"还差 {gap * 100:.1f}%") if gap is not None else "",
    }


def _near_alert(ctx: Context, code: str) -> dict | None:
    price = ctx.price(code)
    views = [_alert_view(item, price) for item in ctx.alerts(code) if item["side"] == "down"]
    near = [item for item in views if item["reached"] or (item["gap"] is not None and item["gap"] <= calc.NEAR_GAP)]
    return min(near, key=lambda item: (not item["reached"], item["gap"] or 0)) if near else None


# ---------------------------------------------------------------- 持有
def hold_row(ctx: Context, row: dict) -> dict:
    code = row["ts_code"]
    ai, sector, reason = ctx.ai(code), ctx.sector(code), ctx.reason(code)
    near = _near_alert(ctx, code)
    tag = None
    if near:
        tag = {"kind": "alert", "text": "已到你设的提醒价" if near["reached"] else "接近你设的提醒价"}
    elif ai.get("change"):
        tag = {"kind": "change", "text": "AI 今天改判"}
    flip = f"距提醒价还差 {near['gap'] * 100:.1f}%" if near and not near["reached"] else ("已到提醒价" if near else flip_text(ai))
    if ai.get("change") and near is None:
        flip = f"{ai['change']}；{flip}" if flip else ai["change"]
    order = 0 if near else 1 if ai.get("change") else 2 if reason["status"] == "broken" else 3 if reason["status"] == "doubt" else 4
    return {
        "ts_code": code, "name": row["name"], "qty": row["qty"], "avg": row["avg"], "price": row["price"], "live": row["live"],
        "change_pct": row["change_pct"], "day_pnl": row["day_pnl"], "pnl": row["pnl"], "pnl_pct": row["pnl_pct"], "weight": row["weight"],
        "since": row["since"], "days": row["days"], "sector": _sector_block(sector), "ai": _ai_block(ai), "flip": flip, "tag": tag,
        "reason": {"status": reason["status"], "text": reason["status_text"], "line": reason["line"]}, "_order": (order, -row["weight"]),
    }


def summary(ctx: Context) -> dict:
    ctx.warm_all()
    rows = sorted((hold_row(ctx, row) for row in ctx.rows), key=lambda item: item["_order"])
    for row in rows:
        row.pop("_order")
    total = ctx.total
    attention = _attention(ctx)
    return {
        "empty": not ctx.rows and not ctx.wishes and not ctx.trades,
        "header": {"mv": total["mv"], "day_pnl": total["day_pnl"], "day_pct": total["day_pct"], "pnl": total["pnl"], "pnl_pct": total["pnl_pct"], "count": len(rows)},
        "tabs": {"buy": len(ctx.wishes), "now": len(rows), "chk": attention, "rev": len(_review_trades(ctx, calc.REVIEW_DAYS)), "rev_days": calc.REVIEW_DAYS},
        "market": _market_block(ctx.market),
        "rows": rows,
    }


# ---------------------------------------------------------------- 想买
def _wish_ctx(ctx: Context, wish: dict) -> dict:
    code = wish["ts_code"]
    ai, sector = ctx.ai(code), ctx.sector(code)
    return {
        "price": ctx.price(code), "ai_lean": ai.get("lean"), "ai_label": (ai.get("label") if ai.get("has") else None), "sector": sector.get("name"),
        "sector_flow": sector.get("flow"), "market": ctx.market,
        "flow_in": ctx.flow(code)["in"] if any(item.get("kind") == "flow_in_days" for item in wish["conditions"]) else 0,
    }


def impact_view(ctx: Context, code: str, plan_pct: float) -> dict:
    sector_of = {row["ts_code"]: ctx.sector(row["ts_code"]).get("name") or "其他" for row in ctx.rows}
    new_sector = ctx.sector(code).get("name") or "其他"
    impact = calc.plan_impact(ctx.rows, sector_of, new_sector, plan_pct)
    lines = [{"sector": line["sector"], "before": line["before"], "after": line["after"], "buying": line["buying"]} for line in impact["lines"]]
    notes = []
    target = next((line for line in impact["lines"] if line["buying"]), None)
    others = [line for line in impact["lines"] if not line["buying"]]
    if others and all(line["after"] < line["before"] for line in others) and target and target["before"] < 0.15:
        notes.append("组合更均衡" if not any(line["after"] > calc.SECTOR_LIMIT for line in impact["lines"]) else "")
    if target and target["after"] > calc.SECTOR_LIMIT:
        notes.append(f"买入后{target['sector']}会到 {target['after'] * 100:.1f}%，超过 {calc.SECTOR_LIMIT * 100:.0f}% 的参考线")
    pair = _same_direction(ctx, code)
    if pair:
        notes.append(f"⚠ {pair[0]}和你手里的{pair[1]}走势高度同向（相关 {pair[2]:.2f}）：买入后这类仓位会更集中")
    return {"lines": lines, "amount": impact["amount"], "total": impact["total"], "notes": [item for item in notes if item]}


def _same_direction(ctx: Context, code: str) -> tuple[str, str, float] | None:
    if not ctx.rows:
        return None
    mine = ctx.closes(code)
    best = None
    for row in ctx.rows:
        value = calc.corr(mine, ctx.closes(row["ts_code"]))
        if value is not None and value >= calc.HIGH_CORR and (best is None or value > best[2]):
            best = (ctx.names.get(code, code), row["name"], value)
    return best


def wish_card(ctx: Context, wish: dict) -> dict:
    code = wish["ts_code"]
    facts = _wish_ctx(ctx, wish)
    state = calc.wish_state(wish, facts)
    ai, sector = ctx.ai(code), ctx.sector(code)
    created = datetime.fromtimestamp(wish["created_at"]).strftime("%m-%d")
    return {
        "id": wish["id"], "ts_code": code, "name": wish["name"], "price": facts["price"], "sector": _sector_block(sector),
        "source": f"{SOURCE_WORD.get(wish['source'], '自己加的')} · {created} 加入", "status": {"text": state["status"], "tone": state["tone"]},
        "reason": wish["reason"], "match": wish["match"], "conditions": state["conditions"], "met": state["met"], "auto": state["auto"],
        "ai": _ai_block(ai), "plan_pct": wish["plan_pct"], "impact": impact_view(ctx, code, wish["plan_pct"]) if wish["plan_pct"] else None,
        "invalid": wish["invalid"],
    }


def tab_buy(ctx: Context) -> dict:
    ctx.warm_all()
    return {"market": _market_block(ctx.market), "items": [wish_card(ctx, wish) for wish in ctx.wishes]}


def candidates(ctx: Context) -> dict:
    """加想买时的候选：判断偏多的、自选里还没持有也没在想买的、体检里的热点板块。已持有、已在想买的不能重复加。"""
    taken = set(ctx.held) | {wish["ts_code"] for wish in ctx.wishes}
    scan = ctx.data.candidates()[:12]
    mine = [row for row in ctx.data.watchlist() if row["ts_code"] not in taken][:12]
    ctx.warm([item["ts_code"] for item in scan] + [row["ts_code"] for row in mine])
    scan = [{**item, "sector": _sector_block(ctx.sector(item["ts_code"])), "taken": item["ts_code"] in taken,
             "taken_text": "已持有" if item["ts_code"] in ctx.held else "已在想买"} for item in scan]
    mine = [{"ts_code": row["ts_code"], "name": row["name"], "sector": _sector_block(ctx.sector(row["ts_code"])), "ai": _ai_block(ctx.ai(row["ts_code"]))} for row in mine]
    ctx.warm_all()
    hot = [row["name"] for group in _hot(ctx)[:1] for row in group["rows"]]
    return {"scan": scan, "watch": mine, "hot": hot}


def wish_preview(ctx: Context, code: str, plan_pct: float) -> dict:
    stock = ctx.data.resolve(code) or {"ts_code": code, "name": code}
    code = stock["ts_code"]
    price = (ctx.data.quotes({code: stock["name"]}).get(code) or {}).get("price")
    ai, sector = ctx.ai(code), ctx.sector(code)
    closes = ctx.closes(code)
    ma20 = sum(close for _day, close in closes[-20:]) / 20 if len(closes) >= 20 else None
    return {
        "ts_code": code, "name": stock["name"], "price": price, "sector": _sector_block(sector), "ai": _ai_block(ai), "market": _market_block(ctx.market),
        "ma20": ma20, "ma20_gap": (price / ma20 - 1) if price and ma20 else None, "sector_flow": sector.get("flow"),
        "impact": impact_view(ctx, code, plan_pct) if plan_pct else None, "total": ctx.total["mv"],
        "amount": ctx.total["mv"] * plan_pct / 100 if plan_pct else 0.0, "evidence": _evidence(ai, sector),
        "held": code in ctx.held, "wished": any(wish["ts_code"] == code for wish in ctx.wishes),
    }


def _evidence(ai: dict, sector: dict) -> list[str]:
    """AI 最近的依据，点一下插进理由里：只取已经有的事实，不编。"""
    out = []
    if ai.get("has") and ai.get("headline"):
        out.append(ai["headline"].rstrip("。"))
    if sector.get("name") and sector.get("verdict"):
        flow = sector.get("flow")
        out.append(f"{sector['name']}{sector['verdict']}" + (f"（近期资金 {flow:+.1f} 亿）" if flow is not None else ""))
    return out


# ---------------------------------------------------------------- 体检
def _hot(ctx: Context) -> list[dict]:
    def make():
        sectors = list(ctx.data.tracked_sectors())
        known = {item["name"] for item in sectors}
        for code in ctx.names:
            sector = ctx.sector(code)
            if sector.get("name") and sector["name"] not in known and sector.get("verdict"):
                known.add(sector["name"])
                sectors.append({"name": sector["name"], "verdict": sector["verdict"], "flow": sector.get("flow"), "heat": sector.get("heat"), "source": sector.get("source", ""), "tracked": False})
        held_sectors: dict[str, list] = {}
        for row in ctx.rows:
            held_sectors.setdefault(ctx.sector(row["ts_code"]).get("name") or "其他", []).append((row["name"], row["weight"]))
        return calc.hot_groups(sectors, held_sectors)

    return ctx.memo("hot", make)


def _reasons(ctx: Context) -> dict[str, dict]:
    return {row["ts_code"]: {"name": row["name"], **ctx.reason(row["ts_code"])} for row in ctx.rows}


def _structure(ctx: Context, with_corr: bool = True) -> dict:
    sector_of = {row["ts_code"]: ctx.sector(row["ts_code"]).get("name") or "其他" for row in ctx.rows}
    verdicts = {name: ctx.sector(code).get("verdict") for code, name in sector_of.items()}
    correlations = {}
    if with_corr and len(ctx.rows) > 1:
        for i, first in enumerate(ctx.rows):
            for second in ctx.rows[i + 1:]:
                if sector_of[first["ts_code"]] == sector_of[second["ts_code"]]:
                    value = calc.corr(ctx.closes(first["ts_code"]), ctx.closes(second["ts_code"]))
                    if value is not None:
                        correlations[(first["ts_code"], second["ts_code"])] = value
    return {**calc.concentration(ctx.rows, sector_of, verdicts, correlations), "sector_of": sector_of}


def _fit(ctx: Context) -> dict:
    market = ctx.market
    if not market:
        return {"known": False, "lines": [{"level": "提示", "title": "没有取到大盘结论", "text": "市场页的结论暂时没取到，这一项先不判断"}]}
    since = ctx.data.market_since() or (ctx.today - timedelta(days=14)).isoformat()
    lines = []
    buys = [row for row in ctx.trades if row["side"] == "buy" and row["date"] >= since and not (row.get("context") or {}).get("imported")]
    if calc.no_add(market):
        if buys:
            bits, dip = [], False
            for row in buys:
                note = ctx.notes[row["id"]]
                loss = note["qty_before"] > 0 and row["price"] < note["avg_before"]
                dip = dip or loss
                bits.append(f"{_day_text(row['date'])} {note['label']}{row['name']}" + ("（浮亏时加仓）" if loss else ""))
            lines.append({"level": "注意", "title": f"{_day_text(since)} 之后你买入了 {len(buys)} 次", "text": "、".join(bits) + "。这和大盘结论不一致" + ("，也是你操作习惯里「越跌越买」那一条" if dip else "")})
        else:
            lines.append({"level": "正常", "title": "没有逆着大盘买", "text": f"大盘说「{market['stance']}」，{_day_text(since)} 之后你没有新买入"})
        for row in ctx.rows:
            if ctx.sector(row["ts_code"]).get("verdict") == "三层同向":
                lines.append({"level": "正常", "title": f"{row['name']}在有新钱的板块", "text": "和大盘偏防守的方向不冲突"})
    else:
        lines.append({"level": "正常", "title": "和大盘没有冲突", "text": f"大盘现在{market['text']}，{_day_text(since)} 起没变过，你的操作没有逆着它"})
    return {"known": True, "since": since, "market": market["text"], "lines": lines}


def _attention(ctx: Context) -> int:
    if not ctx.rows:
        return 0
    flags = _structure(ctx, with_corr=False)["flags"]
    count = sum(1 for flag in flags if flag["level"] == "注意")
    count += 1 if any(item["status"] in ("doubt", "broken") for item in _reasons(ctx).values()) else 0
    count += 1 if any(line["level"] == "注意" for line in _fit(ctx)["lines"]) else 0
    return count


def tab_check(ctx: Context, save: bool = False) -> dict:
    ctx.warm_all()
    now = datetime.now()
    empty = {"empty": True, "text": "还没有持仓，体检没有可看的。先「记一笔」，或导入券商持仓。"}
    if not ctx.rows:
        return empty
    structure = _structure(ctx)
    reasons = _reasons(ctx)
    index = ctx.data.index_closes()
    betas = {row["ts_code"]: calc.beta(ctx.closes(row["ts_code"]), index) for row in ctx.rows}
    triggers = ctx.triggers()
    stress = calc.stress(ctx.rows, structure["sector_of"], betas, triggers)
    fit = _fit(ctx)
    hot = _hot(ctx)
    alerts = {row["ts_code"]: sum(1 for item in ctx.alerts(row["ts_code"]) if item["side"] == "down") for row in ctx.rows}
    suggestions = calc.suggestions(ctx.rows, structure, reasons, triggers, alerts, hot, set(structure["sector_of"].values()))
    top = structure["sectors"][0] if structure["sectors"] else None
    weak = [item["name"] for item in reasons.values() if item["status"] in ("doubt", "broken")]
    issues = []
    if any(flag["level"] == "注意" and "同一个板块" in flag["title"] for flag in structure["flags"]):
        issues.append("板块集中")
    if any(flag["title"] == "单只偏重" for flag in structure["flags"]):
        issues.append("单只偏重")
    if any(not alerts[row["ts_code"]] and not triggers.get(row["ts_code"]) for row in ctx.rows):
        issues.append("未设价格条件")
    snapshot = {"top_sector": {"name": top["name"], "weight": top["weight"]} if top else None, "weak": len(weak), "weak_names": weak, "issues": issues}
    previous = ctx.store.checkups(1)
    compare = calc.compare_checkups(snapshot, previous[0]["data"]) if previous else None
    compare_day = datetime.fromtimestamp(previous[0]["created_at"]).strftime("%m-%d") if previous else ""
    if save:
        ctx.store.save_checkup(snapshot)
    last = ctx.store.checkups(1)
    stamp = datetime.fromtimestamp(last[0]["created_at"]) if (save and last) else now
    return {
        "empty": False, "generated_at": stamp.strftime("%m-%d %H:%M"), "attention": _attention(ctx),
        "structure": {"sectors": [{"name": item["name"], "weight": item["weight"], "verdict": item["verdict"], "tone": VERDICT_TONE.get(item["verdict"] or "", "flat"), "stocks": item["stocks"]} for item in structure["sectors"]], "flags": structure["flags"]},
        "reasons": [{"ts_code": code, "name": item["name"], "text": item["text"], "status": item["status"], "status_text": item["status_text"], "line": item["line"]} for code, item in reasons.items()],
        "stress": stress, "fit": fit,
        "hot": [{"title": group["title"], "rows": [{"name": row["name"], "flow": row.get("flow"), "heat": row.get("heat"), "verdict": row["verdict"], "tone": VERDICT_TONE.get(row["verdict"], "flat"), "source": row.get("source", ""), "tracked": row.get("tracked", False), "mine": [{"name": name, "weight": weight} for name, weight in row["mine"]]} for row in group["rows"]]} for group in hot],
        "suggestions": suggestions, "compare": compare, "compare_day": compare_day,
    }


# ---------------------------------------------------------------- 复盘
def _review_trades(ctx: Context, days: int) -> list[dict]:
    since = (ctx.today - timedelta(days=days)).isoformat()
    return [row for row in ctx.trades if row["date"] >= since and not (row.get("context") or {}).get("imported")]


def tab_review(ctx: Context) -> dict:
    real = [row for row in ctx.trades if not (row.get("context") or {}).get("imported")]
    if not real:
        return {"empty": True, "text": "还没有买卖记录。记几笔以后，这里会把你买入时 AI 怎么说、卖出后又怎样并排放给你看。"}
    codes = list({row["ts_code"] for row in real})
    closes = {code: ctx.closes(code) for code in codes}
    last_price = {code: (closes[code][-1][1] if closes[code] else ctx.price(code)) for code in codes}
    last_price = {code: price for code, price in last_price.items() if price}
    for code in ctx.held:
        if ctx.price(code):
            last_price[code] = ctx.price(code)
    out = calc.review(real, ctx.notes, closes, last_price, ctx.today)
    reasons = {row["ts_code"]: {"name": row["name"], "status": ctx.reason(row["ts_code"])["status"]} for row in ctx.rows}
    habits = calc.habits(real, ctx.notes, closes, last_price, reasons, ctx.today)
    return {
        "empty": False, "days": out["days"], "total": out["total"], "buys": out["buys"], "adds": out["adds"], "sells": out["sells"],
        "aligned": out["aligned"], "unclear": out["unclear"], "risky": out["risky"], "risk_rows": out["risk_rows"], "sold": out["sold"], "habits": habits,
    }


# ---------------------------------------------------------------- 单只详情
def _entries(ctx: Context, code: str) -> list[dict]:
    """这只票的研判记录：买入那天 AI 怎么说，之后每次改判；到 5 个交易日回头看方向对不对。"""
    closes = ctx.closes(code)
    pos = ctx.held.get(code)
    first_day = pos["since"] if pos else (ctx.today - timedelta(days=60)).isoformat()
    items = []
    for row in (pos["buys"] if pos else []):
        ai = (row.get("context") or {}).get("ai") or {}
        if ai.get("lean"):
            items.append({"date": row["date"], "text": f"你{'买入' if ctx.notes[row['id']]['label'] == '建仓' else '加仓'}时 · AI {ai.get('label') or LEAN_WORD.get(ai['lean'], '不明')}", "lean": ai["lean"], "base": row["price"]})
    points = [point for point in ctx.data.judgments(code) if point["trade_date"] >= first_day]
    previous = None
    for point in points:
        key = (point["lean"], point["level"])
        if previous is not None and key != previous:
            before = judge_label(previous[0], previous[1])
            after = judge_label(point["lean"], point["level"])
            base = next((close for day, close in closes if day == point["trade_date"]), None)
            items.append({"date": point["trade_date"], "text": f"{before} → {after}", "lean": point["lean"], "base": base})
        previous = key
    for item in items:
        outcome = {"state": "pending", "text": "待回溯"}
        if item["lean"] == "unclear":
            outcome = {"state": "skip", "text": "不明，不统计"}
        elif item["base"]:
            ret, passed = calc.ret_after(closes, item["date"], item["base"], 5)
            if ret is not None and passed >= 5:
                hit = (ret > 0) if item["lean"] == "bullish" else (ret < 0)
                outcome = {"state": "hit" if hit else "miss", "text": f"T+5 方向{'对了' if hit else '错了'} {ret * 100:+.1f}%"}
            elif passed:
                outcome = {"state": "pending", "text": f"待回溯 · 第 {passed}/5 天"}
        item["outcome"] = outcome
        item.pop("base", None)
    return sorted(items, key=lambda item: item["date"])


def stock_detail(ctx: Context, code: str) -> dict | None:
    row = ctx.row(code)
    name = ctx.names.get(code) or next((t["name"] for t in ctx.trades if t["ts_code"] == code), None)
    if row is None and not name:
        return None
    ctx.warm([code])
    ai, sector, reason = ctx.ai(code), ctx.sector(code), ctx.reason(code) if row else None
    price = ctx.price(code)
    alerts = [_alert_view(item, price) for item in ctx.alerts(code)]
    rules = []
    flow = ctx.flow(code) if any(rule["kind"] == "flow_out" for rule in ctx.store.rules(code)) else {"in": 0, "out": 0}
    for rule in ctx.store.rules(code):
        days = int(rule["value"])
        rules.append({"id": rule["id"], "text": f"大资金连续 {days} 天流出", "detail": f"你设的 · 已出现 {min(flow['out'], days)} 天"})
    has_price_alert = any(item["side"] == "down" for item in alerts)
    trades = []
    for trade in reversed([t for t in ctx.trades if t["ts_code"] == code]):
        note = ctx.notes[trade["id"]]
        flags = calc.trade_flags(trade)
        ai_ctx = (trade.get("context") or {}).get("ai") or {}
        trades.append({
            "id": trade["id"], "date": trade["date"], "label": note["label"], "side": trade["side"], "price": trade["price"], "qty": trade["qty"],
            "market_text": flags["market_text"], "ai_label": ai_ctx.get("label") or "", "conflict": flags["conflict"], "note": trade["note"],
            "imported": bool((trade.get("context") or {}).get("imported")),
        })
    held = None
    if row:
        held = {"avg": row["avg"], "qty": row["qty"], "weight": row["weight"], "since": row["since"], "days": row["days"], "price": row["price"], "pnl": row["pnl"], "pnl_pct": row["pnl_pct"], "mv": row["mv"]}
    return {
        "ts_code": code, "name": name, "price": price, "held": held, "sector": _sector_block(sector), "ai": _ai_block(ai), "flip": flip_text(ai),
        "flip_items": ai.get("watch") or [], "reason": reason, "alerts": alerts, "rules": rules, "needs_price": bool(row) and not has_price_alert,
        "trades": trades, "entries": _entries(ctx, code), "market": _market_block(ctx.market),
    }


# ---------------------------------------------------------------- 通知
def notifications(ctx: Context) -> dict:
    ctx.warm_all()
    items = []
    today = ctx.today.isoformat()
    for code, name in ctx.names.items():
        price = ctx.price(code)
        for alert in ctx.alerts(code):
            view = _alert_view(alert, price)
            if view["reached"]:
                items.append({"id": f"alert:{alert['id']}:{today}", "kind": "alert", "group": "今天", "title": "到点提醒", "text": f"{name} 跌到 {alert['price']:.2f}", "sub": f"你设的提醒价已触发 · 现价 {price:.2f}", "code": code, "day": today, "when": "今天"})
        ai = ctx.ai(code)
        if ai.get("change"):
            items.append({"id": f"change:{code}:{ai.get('trade_date')}", "kind": "change", "group": "今天", "title": "判断变化", "text": f"{name} · {ai['change']}", "sub": ai.get("headline") or "", "code": code, "day": ai.get("trade_date") or today, "when": (ai.get("judged_at") or "")[-5:]})
        for entry in _entries(ctx, code):
            if entry["outcome"]["state"] in ("hit", "miss") and entry["date"] >= (ctx.today - timedelta(days=9)).isoformat():
                items.append({"id": f"settle:{code}:{entry['date']}", "kind": "settle", "group": "昨天", "title": "战绩结算", "text": f"{name} · 研判到期", "sub": entry["outcome"]["text"], "code": code, "day": entry["date"], "when": entry["date"][5:]})
    for row in ctx.data.calendar(ctx.names):
        items.append({"id": f"cal:{row['date']}:{row['text']}", "kind": "upcoming", "group": "即将发生", "title": "即将发生", "text": row["text"], "sub": row.get("tag", ""), "code": "", "day": row["date"], "when": row["date"][5:]})
    seen = set(ctx.store.get("notif_seen", []))
    for item in items:
        item["unread"] = item["id"] not in seen
    items = _merge_inbox(ctx, items)
    order = {"今天": 0, "即将发生": 1, "昨天": 2, "更早": 3}
    items.sort(key=lambda item: (order[item["group"]], item["day"] if item["group"] == "即将发生" else ""))
    return {"items": items, "unread": sum(1 for item in items if item["unread"])}


def _inbox_rows(ctx: Context) -> list[dict]:
    if ctx.inbox is None:
        return []
    from app.notify import pageview

    return pageview.for_holdings(ctx.inbox, set(ctx.names))


def _merge_inbox(ctx: Context, items: list[dict]) -> list[dict]:
    """收件箱的通知并进来。页面自己算的「到价」「改判」和收件箱里对应的事件是同一件事，以收件箱为准，不显示两遍。"""
    rows = _inbox_rows(ctx)
    if not rows:
        return items
    from app.notify import pageview

    fired = {str(r["payload"].get("alert_id")) for r in rows if r["type"] == "price.alert.fired"}
    changed = {(r["subject_id"], r["payload"].get("trade_date")) for r in rows if r["type"] == "judgment.changed"}
    kept = []
    for item in items:
        if item["kind"] == "alert" and item["id"].split(":")[1] in fired:
            continue
        if item["kind"] == "change" and (item["code"], item["id"].rsplit(":", 1)[-1]) in changed:
            continue
        kept.append(item)
    return kept + [pageview.view(r, ctx.today) for r in rows]


def mark_read(ctx: Context) -> int:
    data = notifications(ctx)
    own = [item["id"] for item in data["items"] if not item["id"].startswith("inbox:")]
    ctx.store.set("notif_seen", sorted(set(own) | set(ctx.store.get("notif_seen", [])))[-300:])
    if ctx.inbox is not None:
        from app.notify import pageview

        pageview.mark_read(ctx.inbox, None, _inbox_rows(ctx), int(datetime.now().timestamp() * 1000))
    return len(data["items"])


# ---------------------------------------------------------------- 写入
def _clean_invalid(items) -> list[dict]:
    out = []
    for item in items or []:
        kind = item.get("kind")
        if kind not in INVALID_KINDS:
            continue
        if kind == "close_below":
            try:
                value = float(item.get("value"))
            except (TypeError, ValueError):
                continue
            if value > 0:
                out.append({"kind": kind, "value": value})
        elif kind == "flow_out":
            days = int(item.get("days") or 3)
            out.append({"kind": kind, "days": max(1, min(days, 20))})
        elif kind == "custom":
            text = str(item.get("text") or "").strip()
            if text:
                out.append({"kind": kind, "text": text[:80]})
        else:
            out.append({"kind": kind})
    return out


def _clean_conditions(items) -> list[dict]:
    out = []
    for item in items or []:
        kind = item.get("kind")
        if kind not in CONDITION_KINDS:
            continue
        if kind == "price_below":
            try:
                value = float(item.get("value"))
            except (TypeError, ValueError):
                continue
            if value > 0:
                out.append({"kind": kind, "value": value})
        elif kind == "flow_in_days":
            out.append({"kind": kind, "days": max(1, min(int(item.get("days") or 3), 20))})
        elif kind == "custom":
            text = str(item.get("text") or "").strip()
            if text:
                out.append({"kind": kind, "text": text[:80]})
        else:
            out.append({"kind": kind})
    return out


def trade_context(ctx: Context, code: str) -> dict:
    market, ai = ctx.market, ctx.ai(code)
    out: dict = {}
    if market:
        out["market"] = {"level": market["level"], "tilt": market["tilt"], "stance": market["stance"]}
    if ai.get("has"):
        out["ai"] = {"lean": ai["lean"], "level": ai["level"], "label": ai["label"], "position": ai.get("position")}
    return out


def _sync_invalid(ctx: Context, code: str, name: str, invalid: list[dict]) -> None:
    """把「什么情况说明我错了」落成提醒：价格走综合判断那边的价格提醒，大资金流出走这边的规则。"""
    ctx.data.drop_alerts(code, ALERT_TAG)
    for rule in ctx.store.rules(code):
        ctx.store.remove_rule(rule["id"])
    for item in invalid:
        if item["kind"] == "close_below":
            ctx.data.add_alert(code, item["value"], ALERT_TAG)
        elif item["kind"] == "flow_out":
            ctx.store.add_rule(code, "flow_out", item["days"])


def record_trade(ctx: Context, body: dict) -> dict:
    query = str(body.get("ts_code") or body.get("q") or "").strip()
    stock = ctx.data.resolve(query) if query else None
    if stock is None:
        raise ValueError(f"没有找到股票：{query or '没填'}")
    code, name = stock["ts_code"], stock["name"]
    side = body.get("side")
    try:
        price, qty = float(body.get("price")), int(body.get("qty"))
    except (TypeError, ValueError):
        raise ValueError("价格和数量要填数字") from None
    day = str(body.get("date") or ctx.today.isoformat())
    reason = str(body.get("reason") or "").strip()
    invalid = _clean_invalid(body.get("invalid"))
    wish = next((item for item in ctx.wishes if item["ts_code"] == code), None)
    context = trade_context(ctx, code)
    trade = ctx.store.add_trade(code, name, side, price, qty, day, reason, invalid, context, wish["id"] if wish and side == "buy" else None)
    ctx._memo.clear()
    held, notes = ctx.held, ctx.notes
    if side == "buy":
        if reason or invalid:
            existing = ctx.store.reason(code)
            written = day if notes[trade["id"]]["label"] == "建仓" or not existing else existing["written_at"]
            merged = invalid or (existing or {}).get("invalid") or []
            ctx.store.set_reason(code, reason or (existing or {}).get("text") or "", merged, written)
            _sync_invalid(ctx, code, name, merged)
        if wish:
            ctx.store.close_wish(wish["id"], "bought")
    elif code not in held:
        ctx.store.drop_reason(code)
        _sync_invalid(ctx, code, name, [])
    ctx.data.request_judgment(code)
    ctx._memo.clear()
    return {"trade": trade, "label": notes[trade["id"]]["label"], "closed": code not in held}


def trade_preview(ctx: Context, body: dict) -> dict:
    """记之前看一眼：大盘现在怎么说、AI 对这只票怎么看、买入后占比怎么变。只是提醒。"""
    query = str(body.get("ts_code") or body.get("q") or "").strip()
    stock = ctx.data.resolve(query) if query else None
    if stock is None:
        return {"found": False}
    code = stock["ts_code"]
    ctx.warm([code])
    side = body.get("side")
    try:
        price, qty = float(body.get("price") or 0), int(body.get("qty") or 0)
    except (TypeError, ValueError):
        price, qty = 0.0, 0
    ai, sector = ctx.ai(code), ctx.sector(code)
    quote = ctx.data.quotes({code: stock["name"]}).get(code) or {}
    market = ctx.market
    total = ctx.total["mv"]
    pct = (price * qty / total * 100) if total and price and qty else None
    out = {
        "found": True, "ts_code": code, "name": stock["name"], "price": quote.get("price"), "market": _market_block(market), "ai": _ai_block(ai),
        "sector": _sector_block(sector), "side": side, "pct": pct, "held": code in ctx.held, "held_qty": (ctx.held.get(code) or {}).get("qty", 0),
        "conflict": side == "buy" and calc.no_add(market), "impact": None,
    }
    if side == "buy" and pct:
        out["impact"] = impact_view(ctx, code, pct)
    return out


def add_wish(ctx: Context, body: dict) -> dict:
    query = str(body.get("ts_code") or body.get("q") or "").strip()
    stock = ctx.data.resolve(query) if query else None
    if stock is None:
        raise ValueError(f"没有找到股票：{query or '没填'}")
    if stock["ts_code"] in ctx.held:
        raise ValueError("已经持有这只了，不能重复加到想买")
    try:
        plan = float(body.get("plan_pct") or 0)
    except (TypeError, ValueError):
        raise ValueError("仓位要填数字") from None
    wish = ctx.store.add_wish(
        stock["ts_code"], stock["name"], str(body.get("reason") or ""), _clean_conditions(body.get("conditions")),
        body.get("match") or "all", plan, _clean_invalid(body.get("invalid")), str(body.get("source") or "manual"),
    )
    ctx.data.request_judgment(stock["ts_code"])
    ctx._memo.clear()
    return wish


# ---------------------------------------------------------------- 导入券商持仓
_CODE = re.compile(r"^(\d{6})(?:\.(?:SH|SZ|BJ))?$", re.IGNORECASE)


def parse_import(text: str) -> tuple[list[dict], list[dict]]:
    """每行一只：代码、数量、成本价（名称可有可无）。有表头就按「数量」「成本」找列，没有就按代码、数量、成本价的顺序。"""
    rows, skipped = [], []
    columns = None
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        cells = [cell.strip() for cell in re.split(r"[,\t;，]+|\s{2,}", line) if cell.strip()] if re.search(r"[,\t;，]|\s{2,}", line) else line.split()
        code = next((cell for cell in cells if _CODE.match(cell)), None)
        if code is None:
            if any(key in line for key in ("数量", "持仓", "成本")):
                columns = {"qty": next((i for i, cell in enumerate(cells) if any(key in cell for key in ("数量", "股份余额", "持仓"))), None),
                           "cost": next((i for i, cell in enumerate(cells) if "成本" in cell or "买入均价" in cell), None)}
            else:
                skipped.append({"line": number, "text": line, "why": "没有找到 6 位股票代码"})
            continue
        numbers = []
        for i, cell in enumerate(cells):
            try:
                numbers.append((i, float(cell.replace(",", ""))))
            except ValueError:
                continue
        numbers = [(i, value) for i, value in numbers if cells[i] != code]
        qty = cost = None
        if columns and columns.get("qty") is not None and columns.get("cost") is not None and len(cells) > max(columns["qty"], columns["cost"]):
            try:
                qty, cost = float(cells[columns["qty"]].replace(",", "")), float(cells[columns["cost"]].replace(",", ""))
            except ValueError:
                pass
        if qty is None and len(numbers) >= 2:
            qty, cost = numbers[0][1], numbers[1][1]
        if not qty or not cost or qty <= 0 or cost <= 0 or qty != int(qty):
            skipped.append({"line": number, "text": line, "why": "数量或成本价没有读出来"})
            continue
        rows.append({"code": _CODE.match(code).group(1), "qty": int(qty), "cost": cost, "line": number})
    return rows, skipped


def import_positions(ctx: Context, text: str) -> dict:
    parsed, skipped = parse_import(text)
    added = []
    for item in parsed:
        stock = ctx.data.resolve(item["code"])
        if stock is None:
            skipped.append({"line": item["line"], "text": item["code"], "why": "没有找到这只股票"})
            continue
        if stock["ts_code"] in ctx.held:
            skipped.append({"line": item["line"], "text": stock["name"], "why": "已经有持仓，没有重复导入"})
            continue
        ctx.store.add_trade(stock["ts_code"], stock["name"], "buy", item["cost"], item["qty"], ctx.today.isoformat(), "", [], {"imported": True})
        ctx.data.request_judgment(stock["ts_code"])
        ctx._memo.clear()
        added.append({"ts_code": stock["ts_code"], "name": stock["name"], "qty": item["qty"], "cost": item["cost"]})
    return {"added": added, "skipped": skipped}


def set_reason(ctx: Context, code: str, text: str, invalid) -> dict:
    if code not in ctx.held:
        raise ValueError("手里没有这只，没法改买入理由")
    cleaned = _clean_invalid(invalid)
    ctx.store.set_reason(code, text, cleaned, ctx.reason(code)["written_at"] or None)   # 改理由不改「写于」的日期
    _sync_invalid(ctx, code, ctx.names.get(code, code), cleaned)
    ctx._memo.clear()
    return ctx.store.reason(code)


def set_price_line(ctx: Context, code: str, price: float) -> None:
    """详情里「设一个价格」：给这只票补一条「收盘跌破」，同时写进理由不成立的条件里。"""
    if code not in ctx.held:
        raise ValueError("手里没有这只")
    if price <= 0:
        raise ValueError("价格要大于 0")
    reason = ctx.reason(code)
    invalid = [item for item in reason["invalid"] if item["kind"] != "close_below"] + [{"kind": "close_below", "value": price}]
    ctx.store.set_reason(code, reason["text"], invalid, reason["written_at"] or None)
    _sync_invalid(ctx, code, ctx.names.get(code, code), invalid)
    ctx._memo.clear()
