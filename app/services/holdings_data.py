"""持仓页要用的外部数据：现价、AI 判断、日线、资金流、行业和板块判断、大盘结论。

每一项取不到就返回空，不让一只票拖垮整页。页面层只认这个类的方法，测试里换一个假的就行。
板块判断：和市场页同一套三层规则（钱、热度、杠杆）。市场页每个方向跟踪一只代表 ETF；持仓里对不上的板块没有 ETF，
钱用东方财富行业主力资金近 10 日净流入代替，热度用申万行业成交额 ÷ 流通市值相对自身一年均值，杠杆不看，并且在页面上写明。
"""

from __future__ import annotations

import threading
import time
from datetime import date, timedelta

from app.services import holdings_calc as calc
from app.services import market_rules as rules
from app.services import market_tape as tape
from app.tools import cache

FLOW_DAYS = 10        # 行业主力资金看近 10 个交易日（约 2 周）
HEAT_BASE = 250
# 申万行业名里带的词 → 市场页的板块。对得上才直接用市场页的结论。更具体的词放前面。
TRACKED_L2 = (
    ("证券", "证券"), ("煤炭", "煤炭"), ("油气", "石油"), ("石油", "石油"),
    ("通信", "通信"), ("半导体", "半导体"), ("光伏", "光伏"), ("白酒", "白酒"),
    ("银行", "银行"), ("房地产", "房地产"), ("有色", "有色"), ("电力", "电力"),
    ("农业", "农业"), ("传媒", "传媒"), ("军工", "军工"), ("创新药", "创新药"),
    ("医药", "医药"), ("化工", "化工"), ("机器人", "机器人"),
)
_ZONE_TTL = 60.0


def _norm(name: str) -> str:
    return str(name or "").replace("Ⅱ", "").replace("Ⅲ", "").strip()


class Data:
    """真实数据源。每个方法都吞掉异常，返回空值。"""

    def __init__(self, today: date | None = None):
        self.today = today or date.today()
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 行情
    def quotes(self, names: dict[str, str]) -> dict[str, dict]:
        try:
            from app.synthesis import quotes
            from app.synthesis.watchlist import stock_of

            stocks = [stock_of({"ts_code": code, "name": name}) for code, name in names.items()]
            return quotes.fetch(stocks) if stocks else {}
        except Exception:
            return {}

    def closes(self, ts_code: str) -> list[tuple[str, float]]:
        key = f"holdings:closes:{ts_code}"
        hit = cache.get(key)
        if hit is not None:
            return hit
        try:
            from app.services.series import get_bars
            from app.synthesis.watchlist import stock_of

            items = get_bars(stock_of({"ts_code": ts_code, "name": ts_code}), 250).get("items") or []
        except Exception:
            return []
        rows = []
        for item in items:
            digits = tape.ymd(item.get("trade_date"))
            close = tape.num(item.get("close"))
            if len(digits) == 8 and close:
                rows.append((tape.iso(digits), close))
        rows.sort()
        if rows:
            cache.set(key, rows, 1800)
        return rows

    def index_closes(self) -> list[tuple[str, float]]:
        start = tape._ymd_offset(self.today, 400)
        try:
            rows = tape.cached(f"holdidx:{start}", lambda: tape.pro().index_daily(
                ts_code="000300.SH", start_date=start, end_date=tape._ymd(self.today), fields="trade_date,close",
            ))
        except Exception:
            return []
        out = sorted((tape.iso(row.get("trade_date")), tape.num(row.get("close"))) for row in rows if tape.num(row.get("close")))
        return out

    def flow(self, ts_code: str) -> dict[str, int]:
        """个股大资金（东方财富 net_amount）连续流入、流出的天数，数到最近一天为止。"""
        try:
            from app.services.series import get_flow_daily
            from app.synthesis.watchlist import stock_of

            items = get_flow_daily(stock_of({"ts_code": ts_code, "name": ts_code}), 30).get("items") or []
        except Exception:
            return {"in": 0, "out": 0}
        values = [tape.num(row.get("net_amount")) for row in sorted(items, key=lambda row: str(row.get("trade_date")))]
        values = [value for value in values if value is not None]
        run_in = run_out = 0
        for value in reversed(values):
            if value > 0 and run_out == 0:
                run_in += 1
            elif value < 0 and run_in == 0:
                run_out += 1
            else:
                break
        return {"in": run_in, "out": run_out}

    # ------------------------------------------------------------ AI 判断
    def ai(self, ts_code: str) -> dict:
        """现有的综合判断。没有就返回 has=False，不在这里排队重算。"""
        try:
            from app.synthesis import worker
            from app.synthesis.calendar import last_close_date, now_cn
            from app.synthesis.service import current_mode
            from app.synthesis.watchlist import _change_text, shown_page

            store = worker.get_store()
            record = store.latest(ts_code)
            if not record:
                return {"has": False}
            page = shown_page(record, current_mode())
            direction = page.get("direction") or {}
            expected = last_close_date(now_cn())
            change = _change_text(store.judgments(ts_code), expected)
            watch = [item.get("text") for item in page.get("watch") or [] if item.get("text")]
            levels = page.get("levels") or {}
            return {
                "has": True, "lean": direction.get("lean"), "level": direction.get("level"), "label": direction.get("label"),
                "position": (page.get("position") or {}).get("label"), "headline": page.get("headline") or "",
                "watch": watch, "levels": {side: (levels.get(side) or {}).get("condition") for side in ("up", "down")},
                "change": change, "judged_at": page.get("generated_at"), "stale": record["trade_date"] < expected.isoformat(),
                "trade_date": record["trade_date"],
            }
        except Exception:
            return {"has": False}

    def judgments(self, ts_code: str) -> list[dict]:
        try:
            from app.synthesis import worker

            return worker.get_store().judgments(ts_code)
        except Exception:
            return []

    def request_judgment(self, ts_code: str) -> None:
        try:
            from app.synthesis import worker

            worker.request(ts_code, "holdings", delay=1.0)
        except Exception:
            pass

    def alerts(self, ts_code: str | None = None) -> list[dict]:
        try:
            from app.synthesis import worker

            return worker.get_store().alerts(ts_code)
        except Exception:
            return []

    def add_alert(self, ts_code: str, price: float, name: str) -> None:
        try:
            from app.synthesis import worker

            worker.get_store().alert_add(ts_code, "down", price, "close", name)
        except Exception:
            pass

    def drop_alerts(self, ts_code: str, base_name: str) -> None:
        try:
            from app.synthesis import worker

            store = worker.get_store()
            for row in store.alerts(ts_code):
                if row.get("base_name") == base_name:
                    store.alert_remove(row["id"])
        except Exception:
            pass

    def candidates(self, limit: int = 40) -> list[dict]:
        """判断过的票里，最新一次偏多的。想买页「今天判断偏多的」。"""
        try:
            from app.synthesis import worker
            from app.synthesis.service import current_mode
            from app.synthesis.watchlist import shown_page

            out = []
            for run in worker.get_store().latest_per_stock(300):
                page = shown_page({"data": run["data"]}, current_mode())
                direction = page.get("direction") or {}
                if direction.get("lean") != "bullish":
                    continue
                out.append({
                    "ts_code": run["ts_code"], "name": page.get("stock_name") or run["ts_code"], "label": direction.get("label"),
                    "trade_date": run["trade_date"],
                })
                if len(out) >= limit:
                    break
            return out
        except Exception:
            return []

    def watchlist(self) -> list[dict]:
        try:
            from app.synthesis import worker

            return worker.get_store().watch_list()
        except Exception:
            return []

    # ------------------------------------------------------------ 大盘
    def market(self) -> dict | None:
        hit = cache.get("holdings:market")
        if hit is not None:
            return hit or None
        try:
            from app.services import market_page

            conclusion = market_page.build_summary(self.today, record=False)["conclusion"]
            out = {
                "level": conclusion["level"], "tilt": conclusion["tilt"], "action": conclusion["action"],
                "text": calc.market_text(conclusion), "stance": calc.stance_of(conclusion["level"], conclusion["tilt"]),
            }
        except Exception:
            out = {}
        cache.set("holdings:market", out, _ZONE_TTL)
        return out or None

    def market_since(self) -> str:
        """现在这个大盘结论从哪天开始没变过（市场页每日快照）。没有快照就返回空。"""
        try:
            from app.services import market_changes
            from app.services.market_store import get_store

            rows = market_changes.history(get_store().snapshots())
            return rows[0]["since"] if rows else ""
        except Exception:
            return ""

    # ------------------------------------------------------------ 股票
    def resolve(self, query: str) -> dict | None:
        try:
            from app.tools.stock import resolve_stock

            stock = resolve_stock(query)
        except Exception:
            return None
        if stock is None:
            return None
        return {"ts_code": stock.ts_code, "name": stock.name}

    def search(self, query: str) -> list[dict]:
        try:
            from app.synthesis import quotes

            return quotes.search(query, 8)
        except Exception:
            return []

    # ------------------------------------------------------------ 行业、板块
    def industry(self, ts_code: str) -> dict:
        try:
            rows = tape.cached(f"holdind:{ts_code}", lambda: tape.pro().index_member_all(ts_code=ts_code, is_new="Y"))
        except Exception:
            rows = []
        row = rows[0] if rows else {}
        return {key: str(row.get(key) or "") for key in ("l1_name", "l2_name", "l3_name", "l1_code", "l2_code", "l3_code")}

    def sector_series(self, ts_code: str) -> dict:
        """这只票所在申万行业（二级，没有就一级）的日收盘序列，按日期升序。预测战绩拿它当「同板块」的基准。"""
        info = self.industry(ts_code)
        code = info.get("l2_code") or info.get("l1_code") or ""
        name = _norm(info.get("l2_name") or info.get("l1_name"))
        if not code:
            return {"name": "", "code": "", "items": []}
        start = tape._ymd_offset(self.today, 400)
        try:
            rows = tape.cached(f"swser:{code}:{start}", lambda: tape.pro().sw_daily(
                ts_code=code, start_date=start, end_date=tape._ymd(self.today), fields="trade_date,close",
            ))
        except Exception:
            rows = []
        items = sorted(
            ([tape.iso(tape.ymd(row.get("trade_date"))), tape.num(row.get("close"))] for row in rows if tape.num(row.get("close"))),
            key=lambda pair: pair[0],
        )
        return {"name": name, "code": code, "items": items}

    def tracked_sectors(self) -> list[dict]:
        """市场页跟踪的板块（代表 ETF 的钱、热度、杠杆）。"""
        hit = cache.get("holdings:tracked")
        if hit is not None:
            return hit
        try:
            from app.services import market_sector

            data = market_sector.build(self.today)
            out = [{
                "name": row["name"], "verdict": row["verdict"], "flow": row["flow"].get(data["conclusion_days"]),
                "heat": row["heat"], "lever_word": row["lever_word"], "phase": row.get("phase"), "why": row.get("why"),
                "source": "代表 ETF 份额净增" if row.get("source") == "etf" else "行业主力资金估算", "tracked": True,
            } for row in data["rows"]]
        except Exception:
            out = []
        cache.set("holdings:tracked", out, 600)
        return out

    def sector(self, ts_code: str) -> dict:
        """这只票所在板块和它的钱、热度判断。先看对得上市场页跟踪的板块，没有就按行业数据估。"""
        info = self.industry(ts_code)
        l2 = _norm(info.get("l2_name"))
        if not l2:
            return {"name": "", "verdict": None, "flow": None, "heat": None, "source": "", "tracked": False, "l1": info.get("l1_name", "")}
        try:
            from app.services import market_sector

            name = market_sector.etf_sector_for(info.get("l1_name", ""), info.get("l2_name", ""), info.get("l3_name", ""))
        except Exception:
            name = None
        row = next((item for item in self.tracked_sectors() if item["name"] == name), None) if name else None
        if row:
            return {**row, "l1": info.get("l1_name", "")}
        return {**self._industry_verdict(info), "name": l2, "tracked": False, "l1": info.get("l1_name", "")}

    def _industry_verdict(self, info: dict) -> dict:
        names = [info.get("l2_name"), _norm(info.get("l2_name")), info.get("l3_name"), _norm(info.get("l3_name")), info.get("l1_name")]
        flow = self._industry_flow([name for name in names if name])
        heat = self._industry_heat(info.get("l2_code") or info.get("l1_code") or "")
        layers = rules.sector_layers(flow, heat, None) if flow is not None else {"verdict": None}
        return {
            "verdict": layers["verdict"] if layers["verdict"] != "没有" else None, "flow": flow, "heat": heat,
            "source": f"行业主力资金近 {FLOW_DAYS} 日 + 申万行业成交热度（估算，没有代表 ETF）",
        }

    def _industry_flow(self, names: list[str]) -> float | None:
        days = tape.open_dates(self.today, FLOW_DAYS + 1)
        if not days:
            return None
        days = days[-FLOW_DAYS:]
        loaded = tape._parallel(days, lambda day: tape.cached(f"indflow:{day}", lambda d=day: tape.pro().moneyflow_ind_dc(trade_date=d, content_type="行业")))
        total, seen = 0.0, 0
        wanted = None
        for rows in loaded:
            if not rows:
                continue
            if wanted is None:
                known = {str(row.get("name")): row for row in rows}
                wanted = next((name for name in names if name in known), None)
            if wanted is None:
                return None
            for row in rows:
                if str(row.get("name")) == wanted and tape.num(row.get("net_amount")) is not None:
                    total += tape.num(row.get("net_amount"))
                    seen += 1
        return total / 1e8 if seen >= FLOW_DAYS // 2 else None

    def _industry_heat(self, index_code: str) -> float | None:
        if not index_code:
            return None
        start = tape._ymd_offset(self.today, 400)
        try:
            rows = tape.cached(f"swidx:{index_code}:{start}", lambda: tape.pro().sw_daily(
                ts_code=index_code, start_date=start, end_date=tape._ymd(self.today), fields="trade_date,amount,float_mv",
            ))
        except Exception:
            return None
        rates = [tape.num(row.get("amount")) / tape.num(row.get("float_mv")) for row in sorted(rows, key=lambda row: str(row.get("trade_date")))
                 if tape.num(row.get("amount")) and tape.num(row.get("float_mv"))]
        if len(rates) < 60:
            return None
        base = sum(rates[-HEAT_BASE:]) / len(rates[-HEAT_BASE:])
        return rates[-1] / base if base else None

    # ------------------------------------------------------------ 日历
    def calendar(self, names: dict[str, str]) -> list[dict]:
        """自选里的定期报告披露日和解禁，未来两周。"""
        try:
            from app.services import market_page

            watch = [{"ts_code": code, "name": name} for code, name in names.items()]
            return market_page._personal(watch, self.today)
        except Exception:
            return []
