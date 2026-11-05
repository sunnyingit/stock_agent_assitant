import unittest
from datetime import date, timedelta

from app.services import holdings_page as hp
from app.services.holdings_store import HoldingsStore

TODAY = date(2026, 11, 2)


def line(start, closes):
    day = date.fromisoformat(start)
    return [((day + timedelta(days=i)).isoformat(), close) for i, close in enumerate(closes)]


AI_BULL = {"has": True, "lean": "bullish", "level": 2, "label": "偏多 · 比较明显", "position": "适中", "headline": "偏多：财报、基本面都在推",
           "watch": ["板块弱势可能结束"], "levels": {}, "change": "", "judged_at": "2026-11-02 09:58", "stale": False, "trade_date": "2026-11-02"}
AI_FLIP = {**AI_BULL, "lean": "unclear", "level": 0, "label": "不明", "position": "偏高", "change": "从偏多变为不明", "watch": ["大资金连续 2 天流入", "出现订单公告"]}


class FakeData:
    """页面层要用的外部数据，全部写死，不打网络。"""

    def __init__(self):
        self.quote_map = {
            "002050.SZ": {"price": 26.8, "prev": 26.64, "change": 0.16, "change_pct": 0.4},
            "601689.SH": {"price": 58.2, "prev": 59.6, "change": -1.4, "change_pct": -2.4},
            "603738.SH": {"price": 22.41, "prev": 21.73, "change": 0.68, "change_pct": 3.1},
            "600036.SH": {"price": 40.0, "prev": 39.9, "change": 0.1, "change_pct": 0.25},
        }
        self.ai_map = {"002050.SZ": AI_FLIP, "601689.SH": {**AI_BULL, "lean": "bearish", "label": "偏空 · 位置偏高", "change": ""}, "603738.SH": AI_BULL}
        self.sector_map = {
            "002050.SZ": {"name": "汽车零部件", "verdict": "只热不增", "flow": -3.3, "heat": 1.6, "tracked": False, "source": "估算"},
            "601689.SH": {"name": "汽车零部件", "verdict": "只热不增", "flow": -3.3, "heat": 1.6, "tracked": False, "source": "估算"},
            "603738.SH": {"name": "通信元件", "verdict": "三层同向", "flow": 2.0, "heat": 1.8, "tracked": False, "source": "估算"},
            "600036.SH": {"name": "银行", "verdict": "三层同向", "flow": 8.2, "heat": 1.5, "tracked": True, "source": "ETF"},
        }
        self.closes_map = {}
        self.index = line("2026-01-01", [100 + i * 0.3 + (i % 5) for i in range(200)])
        self.flows = {}
        self.judgment_map = {}
        self.alert_rows = []
        self.requested = []
        self.market_map = {"level": "中性", "tilt": "偏弱", "action": "x", "text": "中性偏弱 · 不加仓", "stance": "不加仓"}
        self.since = "2026-10-26"
        self.cal = []
        self.names = {"600036": ("600036.SH", "招商银行"), "002050": ("002050.SZ", "三花智控"), "601689": ("601689.SH", "拓普集团"), "603738": ("603738.SH", "泰晶科技"),
                      "002430": ("002430.SZ", "杭氧股份")}

    def quotes(self, names): return {code: self.quote_map[code] for code in names if code in self.quote_map}
    def closes(self, code): return self.closes_map.get(code, [])
    def index_closes(self): return self.index
    def flow(self, code): return self.flows.get(code, {"in": 0, "out": 0})
    def ai(self, code): return self.ai_map.get(code, {"has": False})
    def judgments(self, code): return self.judgment_map.get(code, [])
    def alerts(self, code=None): return [row for row in self.alert_rows if code is None or row["ts_code"] == code]
    def add_alert(self, code, price, name): self.alert_rows.append({"id": len(self.alert_rows) + 1, "ts_code": code, "side": "down", "price": price, "base_name": name})
    def drop_alerts(self, code, base): self.alert_rows = [r for r in self.alert_rows if not (r["ts_code"] == code and r["base_name"] == base)]
    def request_judgment(self, code): self.requested.append(code)
    def candidates(self): return [{"ts_code": "600519.SH", "name": "贵州茅台", "label": "偏多 · 稍微", "trade_date": "2026-11-02"}, {"ts_code": "600036.SH", "name": "招商银行", "label": "偏多 · 比较明显", "trade_date": "2026-11-02"}]
    def watchlist(self): return [{"ts_code": "600845.SH", "name": "宝信软件"}]
    def market(self): return self.market_map
    def market_since(self): return self.since
    def tracked_sectors(self): return [{"name": "银行", "verdict": "三层同向", "flow": 8.2, "heat": 1.5, "tracked": True, "source": "ETF"}, {"name": "芯片", "verdict": "只热不增", "flow": -9.6, "heat": 2.1, "tracked": True, "source": "ETF"}]
    def sector(self, code): return self.sector_map.get(code, {"name": "", "verdict": None, "flow": None, "heat": None, "tracked": False, "source": ""})
    def calendar(self, names): return self.cal
    def resolve(self, query):
        key = query.split(".")[0]
        if key in self.names:
            code, name = self.names[key]
            return {"ts_code": code, "name": name}
        return None


class Base(unittest.TestCase):
    def setUp(self):
        self.store = HoldingsStore(":memory:")
        self.data = FakeData()

    def ctx(self):
        return hp.Context(self.store, self.data, TODAY)

    def seed(self):
        """四只持仓，对应设计稿。三花 10-13 买、10-28 加仓（浮亏、大盘说不加仓）。"""
        ctx = {"market": {"level": "中性", "tilt": "偏弱", "stance": "不加仓"}, "ai": {"lean": "bullish", "level": 2, "label": "偏多 · 比较明显"}}
        add = self.store.add_trade
        add("002050.SZ", "三花智控", "buy", 29.35, 1000, "2026-10-13", "机器人订单会在四季度落地", [{"kind": "sector_out"}], {"ai": {"lean": "bullish", "level": 2, "label": "偏多 · 比较明显"}, "market": {"level": "中性", "tilt": "", "stance": ""}})
        add("002050.SZ", "三花智控", "buy", 26.8, 500, "2026-10-28", "", [], {**ctx, "ai": {"lean": "bullish", "level": 1, "label": "偏多 · 稍微"}})
        add("601689.SH", "拓普集团", "buy", 52.0, 1000, "2026-09-18", "Optimus 供应链", [{"kind": "close_below", "value": 56.5}], {"ai": {"lean": "bullish", "level": 2, "label": "偏多 · 比较明显"}})
        add("603738.SH", "泰晶科技", "buy", 19.8, 2000, "2026-10-20", "板块主升", [{"kind": "close_below", "value": 20.85}, {"kind": "flow_out", "days": 2}], {"ai": {"lean": "bullish", "level": 2, "label": "偏多 · 比较明显"}})
        self.store.set_reason("002050.SZ", "机器人订单会在四季度落地", [{"kind": "sector_out"}], "2026-10-13")
        self.store.set_reason("601689.SH", "Optimus 供应链", [{"kind": "close_below", "value": 56.5}], "2026-09-18")
        self.store.set_reason("603738.SH", "板块主升", [{"kind": "close_below", "value": 20.85}, {"kind": "flow_out", "days": 2}], "2026-10-20")
        self.data.alert_rows = [{"id": 1, "ts_code": "601689.SH", "side": "down", "price": 56.5, "base_name": hp.ALERT_TAG},
                                {"id": 2, "ts_code": "603738.SH", "side": "down", "price": 20.85, "base_name": hp.ALERT_TAG}]
        self.data.flows["603738.SH"] = {"in": 0, "out": 1}


class SummaryTest(Base):
    def test_header_rows_and_order(self):
        self.seed()
        out = hp.summary(self.ctx())
        header = out["header"]
        self.assertAlmostEqual(header["mv"], 1500 * 26.8 + 1000 * 58.2 + 2000 * 22.41)
        self.assertAlmostEqual(header["day_pnl"], 1500 * 0.16 + 1000 * -1.4 + 2000 * 0.68)
        self.assertEqual(out["tabs"]["now"], 3)
        rows = out["rows"]
        # 拓普离提醒价 2.9% 最前，三花 AI 改判第二，泰晶最后
        self.assertEqual([row["name"] for row in rows], ["拓普集团", "三花智控", "泰晶科技"])
        self.assertEqual(rows[0]["tag"], {"kind": "alert", "text": "接近你设的提醒价"})
        self.assertIn("距提醒价还差 2.9%", rows[0]["flip"])
        self.assertEqual(rows[1]["tag"]["kind"], "change")
        self.assertIn("从偏多变为不明", rows[1]["flip"])
        self.assertIn("还差 2 条", rows[1]["flip"])
        self.assertEqual(rows[1]["ai"]["label"], "不明 · 位置偏高")
        self.assertEqual(rows[1]["sector"], {"name": "汽车零部件", "verdict": "只热不增", "tone": "warn", "flow": -3.3, "heat": 1.6, "source": "估算", "tracked": False})
        self.assertAlmostEqual(sum(row["weight"] for row in rows), 1.0)

    def test_reason_status_per_row(self):
        self.seed()
        rows = {row["name"]: row for row in hp.summary(self.ctx())["rows"]}
        self.assertEqual(rows["拓普集团"]["reason"]["status"], "doubt")   # AI 偏空
        self.assertEqual(rows["三花智控"]["reason"]["status"], "doubt")   # AI 不再偏多、板块只热不增
        self.assertEqual(rows["泰晶科技"]["reason"]["status"], "doubt")   # 大资金流出已出现 1 天
        self.assertIn("已出现 1 天", rows["泰晶科技"]["reason"]["line"])

    def test_empty(self):
        out = hp.summary(self.ctx())
        self.assertTrue(out["empty"])
        self.assertEqual(out["rows"], [])
        self.assertEqual(out["header"]["mv"], 0)

    def test_missing_quote_does_not_break_the_row(self):
        self.seed()
        del self.data.quote_map["601689.SH"]
        row = next(r for r in hp.summary(self.ctx())["rows"] if r["name"] == "拓普集团")
        self.assertFalse(row["live"])
        self.assertEqual(row["day_pnl"], 0.0)


class BuyTabTest(Base):
    def test_wish_card_conditions_and_impact(self):
        self.seed()
        self.store.add_wish("600036.SH", "招商银行", "银行三层同向，高股息托底", [
            {"kind": "price_below", "value": 37.0}, {"kind": "etf_inflow"}, {"kind": "flow_in_days", "days": 3}], "all", 10, [], "scan")
        card = hp.tab_buy(self.ctx())["items"][0]
        self.assertEqual(card["name"], "招商银行")
        self.assertEqual(card["source"].split(" · ")[0], "来自扫描")
        conds = {c["kind"]: c for c in card["conditions"]}
        self.assertFalse(conds["price_below"]["met"])
        self.assertIn("还差 7.5%", conds["price_below"]["detail"])
        self.assertTrue(conds["etf_inflow"]["met"])
        self.assertEqual(conds["flow_in_days"]["detail"], "0 / 3 天")
        self.assertEqual(card["status"]["text"], "等条件")   # 价格还差 7.5%（不到 5% 才算近），3 条里只满足 1 条
        self.assertTrue(card["impact"]["lines"][0]["buying"])
        self.assertEqual(card["impact"]["lines"][0]["sector"], "银行")
        self.assertAlmostEqual(card["impact"]["lines"][0]["before"], 0.0)

    def test_candidates_mark_taken(self):
        self.seed()
        self.store.add_wish("600036.SH", "招商银行", "x", [], "all", 5)
        out = hp.candidates(self.ctx())
        taken = {item["ts_code"]: item["taken"] for item in out["scan"]}
        self.assertEqual(taken, {"600519.SH": False, "600036.SH": True})
        self.assertEqual(out["watch"][0]["name"], "宝信软件")

    def test_add_wish_rules(self):
        self.seed()
        ctx = self.ctx()
        with self.assertRaises(ValueError):
            hp.add_wish(ctx, {"ts_code": "002050.SZ", "reason": "x"})   # 已持有
        with self.assertRaises(ValueError):
            hp.add_wish(ctx, {"ts_code": "999999.SH", "reason": "x"})   # 找不到
        wish = hp.add_wish(ctx, {"ts_code": "600036", "reason": "高股息", "plan_pct": 10, "conditions": [{"kind": "price_below", "value": "38"}, {"kind": "bad"}, {"kind": "custom", "text": ""}]})
        self.assertEqual(wish["conditions"], [{"kind": "price_below", "value": 38.0}])
        self.assertIn("600036.SH", self.data.requested)
        with self.assertRaises(ValueError):
            hp.add_wish(self.ctx(), {"ts_code": "600036.SH", "reason": "again"})


class CheckTest(Base):
    def test_structure_reasons_stress_fit(self):
        self.seed()
        out = hp.tab_check(self.ctx())
        self.assertFalse(out["empty"])
        top = out["structure"]["sectors"][0]
        self.assertEqual(top["name"], "汽车零部件")
        titles = [f["title"] for f in out["structure"]["flags"]]
        self.assertTrue(any("同一个板块" in t for t in titles))
        self.assertEqual({r["name"]: r["status"] for r in out["reasons"]}["拓普集团"], "doubt")
        keys = [item["key"] for item in out["stress"]]
        self.assertEqual(keys, ["market", "sector", "own"])
        self.assertIn("拓普集团跌破 56.50", out["stress"][2]["text"])
        self.assertTrue(out["fit"]["known"])
        fit_titles = [line["title"] for line in out["fit"]["lines"]]
        self.assertIn("10-26 之后你买入了 1 次", fit_titles)        # 10-28 加仓三花；10-20 买泰晶在之前
        text = next(line["text"] for line in out["fit"]["lines"] if "之后你买入" in line["title"])
        self.assertIn("10-28 加仓三花智控（浮亏时加仓）", text)
        self.assertIn("越跌越买", text)
        self.assertGreaterEqual(out["attention"], 3)

    def test_hot_groups_pair_with_holdings(self):
        self.seed()
        groups = hp.tab_check(self.ctx())["hot"]
        titles = [g["title"].split(" ·")[0] for g in groups]
        self.assertIn("有钱又热", titles)
        names = {row["name"]: row for g in groups for row in g["rows"]}
        self.assertEqual(names["通信元件"]["mine"][0]["name"], "泰晶科技")
        self.assertEqual(names["银行"]["mine"], [])

    def test_save_and_compare_with_last_checkup(self):
        self.seed()
        first = hp.tab_check(self.ctx(), save=True)
        self.assertIsNone(first["compare"])
        second = hp.tab_check(self.ctx(), save=True)
        titles = {line["title"] for line in second["compare"]["lines"]}
        self.assertIn("汽车零部件占比", titles)
        self.assertEqual(len(self.store.checkups(5)), 2)

    def test_empty(self):
        self.assertTrue(hp.tab_check(self.ctx())["empty"])
        self.assertEqual(hp.summary(self.ctx())["tabs"]["chk"], 0)


class ReviewTest(Base):
    def test_review_rows_and_habits(self):
        self.seed()
        self.store.add_trade("600036.SH", "招商银行", "buy", 35.0, 1000, "2026-09-22", "高股息", [], {"ai": {"lean": "bullish", "level": 2, "label": "偏多 · 比较明显"}})
        self.store.add_trade("600036.SH", "招商银行", "sell", 41.6, 1000, "2026-10-15")
        self.data.closes_map["600036.SH"] = line("2026-10-15", [41.6] + [43.2] * 30)
        out = hp.tab_review(self.ctx())
        self.assertFalse(out["empty"])
        self.assertEqual((out["buys"], out["adds"], out["sells"]), (5, 1, 1))
        sold = out["sold"][0]
        self.assertEqual(sold["name"], "招商银行")
        self.assertAlmostEqual(sold["pnl_pct"], 41.6 / 35.0 - 1)
        self.assertEqual(sold["relation"], "跟 AI 一致")
        self.assertTrue(sold["after_done"])
        habits = {item["key"]: item for item in out["habits"]}
        self.assertEqual(habits["dip"]["count"], 1)
        self.assertIn("三花智控 10-28 加仓", habits["dip"]["detail"])

    def test_imported_positions_are_not_reviewed(self):
        self.store.add_trade("600036.SH", "招商银行", "buy", 35.0, 1000, "2026-10-30", "", [], {"imported": True})
        self.assertTrue(hp.tab_review(self.ctx())["empty"])
        self.assertEqual(hp.summary(self.ctx())["tabs"]["rev"], 0)


class DetailTest(Base):
    def test_held_detail(self):
        self.seed()
        self.data.judgment_map["002050.SZ"] = [
            {"trade_date": "2026-10-13", "lean": "bullish", "level": 2}, {"trade_date": "2026-10-20", "lean": "bullish", "level": 2},
            {"trade_date": "2026-11-02", "lean": "unclear", "level": 0},
        ]
        self.data.closes_map["002050.SZ"] = line("2026-10-13", [29.35] * 25)
        out = hp.stock_detail(self.ctx(), "002050.SZ")
        self.assertEqual(out["held"]["qty"], 1500)
        self.assertAlmostEqual(out["held"]["avg"], (29.35 * 1000 + 26.8 * 500) / 1500)
        self.assertEqual(out["reason"]["status"], "doubt")
        self.assertTrue(out["needs_price"])          # 三花没有价格提醒
        self.assertEqual([t["label"] for t in out["trades"]], ["加仓", "建仓"])   # 新的在前
        self.assertTrue(out["trades"][0]["conflict"])
        self.assertIn("不加仓", out["trades"][0]["market_text"])
        self.assertEqual(out["flip_items"], ["大资金连续 2 天流入", "出现订单公告"])
        texts = [e["text"] for e in out["entries"]]
        self.assertIn("你买入时 · AI 偏多 · 比较明显", texts)
        self.assertIn("偏多 · 比较明显 → 不明", texts)
        outcomes = {e["text"]: e["outcome"] for e in out["entries"]}
        self.assertEqual(outcomes["偏多 · 比较明显 → 不明"]["state"], "skip")

    def test_rules_and_alerts_listed(self):
        self.seed()
        self.store.add_rule("603738.SH", "flow_out", 2)
        out = hp.stock_detail(self.ctx(), "603738.SH")
        self.assertEqual([r["text"] for r in out["rules"]], ["大资金连续 2 天流出"])
        self.assertEqual(out["rules"][0]["detail"], "你设的 · 已出现 1 天")
        self.assertEqual(out["alerts"][0]["text"], "收盘跌破 20.85")
        self.assertFalse(out["needs_price"])

    def test_outcome_settles_after_five_days(self):
        self.seed()
        closes = line("2026-10-20", [19.8, 20, 20.5, 21, 21.5, 22.0, 22.5])
        self.data.closes_map["603738.SH"] = closes
        out = hp.stock_detail(self.ctx(), "603738.SH")
        entry = next(e for e in out["entries"] if e["text"].startswith("你买入时"))
        self.assertEqual(entry["outcome"]["state"], "hit")
        self.assertIn("T+5 方向对了 +11.1%", entry["outcome"]["text"])

    def test_unknown_stock(self):
        self.assertIsNone(hp.stock_detail(self.ctx(), "000001.SZ"))


class WriteTest(Base):
    def test_buy_sets_reason_alerts_rules_and_closes_the_wish(self):
        self.store.add_wish("600036.SH", "招商银行", "银行三层同向", [], "all", 10)
        out = hp.record_trade(self.ctx(), {
            "ts_code": "600036.SH", "side": "buy", "price": 40.0, "qty": 1000, "date": "2026-11-02", "reason": "高股息托底",
            "invalid": [{"kind": "close_below", "value": "36.5"}, {"kind": "flow_out", "days": 3}, {"kind": "custom", "text": "三季报逻辑被证伪"}, {"kind": "junk"}],
        })
        self.assertEqual(out["label"], "建仓")
        self.assertEqual(self.store.reason("600036.SH")["text"], "高股息托底")
        self.assertEqual(len(self.store.reason("600036.SH")["invalid"]), 3)
        self.assertEqual([(a["price"], a["base_name"]) for a in self.data.alerts("600036.SH")], [(36.5, hp.ALERT_TAG)])
        self.assertEqual([(r["kind"], r["value"]) for r in self.store.rules("600036.SH")], [("flow_out", 3.0)])
        self.assertEqual(self.store.wishes("open"), [])
        self.assertEqual(self.store.trades()[0]["wish_id"], 1)
        self.assertIn("600036.SH", self.data.requested)
        # 买入当时的大盘和 AI 被记了下来
        self.assertEqual(self.store.trades()[0]["context"]["market"]["stance"], "不加仓")

    def test_add_to_position_keeps_the_reason_when_none_given(self):
        self.seed()
        hp.record_trade(self.ctx(), {"ts_code": "002050.SZ", "side": "buy", "price": 25, "qty": 100, "date": "2026-11-02"})
        self.assertEqual(self.store.reason("002050.SZ")["text"], "机器人订单会在四季度落地")

    def test_selling_everything_clears_reason_and_reminders(self):
        self.seed()
        hp.record_trade(self.ctx(), {"ts_code": "601689.SH", "side": "sell", "price": 58.2, "qty": 1000, "date": "2026-11-02"})
        self.assertIsNone(self.store.reason("601689.SH"))
        self.assertEqual([a for a in self.data.alerts("601689.SH")], [])

    def test_validation_messages(self):
        ctx = self.ctx()
        for body, word in (({"ts_code": "999999.SH", "side": "buy", "price": 1, "qty": 1}, "没有找到"),
                           ({"ts_code": "600036.SH", "side": "buy", "price": "x", "qty": 1}, "数字"),
                           ({"ts_code": "600036.SH", "side": "sell", "price": 1, "qty": 100}, "卖出")):
            with self.assertRaises(ValueError) as caught:
                hp.record_trade(ctx, body)
            self.assertIn(word, str(caught.exception))

    def test_set_reason_and_price_line(self):
        self.seed()
        hp.set_reason(self.ctx(), "002050.SZ", "改成：T 链核心", [{"kind": "close_below", "value": 24}])
        self.assertEqual(self.store.reason("002050.SZ")["text"], "改成：T 链核心")
        self.assertEqual(self.store.reason("002050.SZ")["written_at"], "2026-10-13")   # 写于的日期不变
        self.assertEqual([a["price"] for a in self.data.alerts("002050.SZ")], [24.0])
        hp.set_price_line(self.ctx(), "002050.SZ", 25.5)
        self.assertEqual([a["price"] for a in self.data.alerts("002050.SZ")], [25.5])
        with self.assertRaises(ValueError):
            hp.set_price_line(self.ctx(), "600036.SH", 30)

    def test_preview_flags_buying_against_the_market(self):
        self.seed()
        out = hp.trade_preview(self.ctx(), {"ts_code": "600036.SH", "side": "buy", "price": 40, "qty": 500})
        self.assertTrue(out["found"])
        self.assertTrue(out["conflict"])
        self.assertAlmostEqual(out["pct"], 20000 / (1500 * 26.8 + 1000 * 58.2 + 2000 * 22.41) * 100)
        self.assertEqual(out["impact"]["lines"][0]["sector"], "银行")
        self.assertFalse(hp.trade_preview(self.ctx(), {"ts_code": "999999.SH"})["found"])


class ImportTest(Base):
    def test_parse_formats(self):
        rows, skipped = hp.parse_import("证券代码,证券名称,持仓数量,成本价\n600036,招商银行,1000,35.20\n002050.SZ 三花智控 1,500 26.8\n说明文字\n600519,贵州茅台,abc,1500")
        self.assertEqual([(r["code"], r["qty"], r["cost"]) for r in rows], [("600036", 1000, 35.2)])
        self.assertEqual(len(skipped), 3)

    def test_parse_header_columns_in_other_order(self):
        rows, _ = hp.parse_import("股票代码\t成本价\t股份余额\n600036\t35.2\t1000")
        self.assertEqual([(r["qty"], r["cost"]) for r in rows], [(1000, 35.2)])

    def test_import_adds_trades_and_skips_held(self):
        self.seed()
        out = hp.import_positions(self.ctx(), "600036 1000 35.2\n002050 500 30")
        self.assertEqual([item["name"] for item in out["added"]], ["招商银行"])
        self.assertEqual(out["skipped"][0]["why"], "已经有持仓，没有重复导入")
        trade = next(t for t in self.store.trades("600036.SH"))
        self.assertTrue(trade["context"]["imported"])


class NotificationTest(Base):
    def test_items_and_read_marks(self):
        self.seed()
        self.data.quote_map["601689.SH"]["price"] = 56.42
        self.data.cal = [{"date": "2026-11-03", "text": "杭氧股份 三季报披露", "tag": "涉及自选"}]
        data = hp.notifications(self.ctx())
        kinds = [item["kind"] for item in data["items"]]
        self.assertIn("alert", kinds)
        self.assertIn("change", kinds)
        self.assertIn("upcoming", kinds)
        alert = next(i for i in data["items"] if i["kind"] == "alert")
        self.assertEqual(alert["text"], "拓普集团 跌到 56.50")
        self.assertEqual(data["unread"], len(data["items"]))
        hp.mark_read(self.ctx())
        self.assertEqual(hp.notifications(self.ctx())["unread"], 0)


if __name__ == "__main__":
    unittest.main()
