import unittest
from datetime import date

from app.services import holdings_calc as calc


def T(id, code, side, price, qty, day, name=None, context=None, reason="", note=""):
    return {"id": id, "ts_code": code, "name": name or code, "side": side, "price": price, "qty": qty, "date": day,
            "context": context or {}, "reason": reason, "note": note, "invalid": []}


def series(start, closes):
    """从 start 起每天一个收盘价（不管周末，只要日期递增）。"""
    from datetime import timedelta
    day = date.fromisoformat(start)
    return [((day + timedelta(days=i)).isoformat(), close) for i, close in enumerate(closes)]


class PositionTest(unittest.TestCase):
    def test_weighted_average_and_labels(self):
        trades = [T(1, "A", "buy", 10, 1000, "2026-10-01"), T(2, "A", "buy", 8, 1000, "2026-10-10"), T(3, "A", "sell", 12, 500, "2026-10-20")]
        held, notes = calc.positions(trades)
        self.assertEqual(held["A"]["qty"], 1500)
        self.assertAlmostEqual(held["A"]["avg"], 9.0)
        self.assertEqual([notes[i]["label"] for i in (1, 2, 3)], ["建仓", "加仓", "减仓"])
        self.assertAlmostEqual(notes[2]["avg_before"], 10.0)
        self.assertAlmostEqual(notes[3]["pnl"], (12 - 9.0) * 500)
        self.assertAlmostEqual(notes[3]["pnl_pct"], 12 / 9.0 - 1)
        self.assertEqual(notes[3]["entry"]["id"], 1)

    def test_closed_position_leaves_the_book_and_a_new_round_starts_fresh(self):
        trades = [T(1, "A", "buy", 10, 100, "2026-10-01"), T(2, "A", "sell", 11, 100, "2026-10-05"), T(3, "A", "buy", 20, 100, "2026-10-09")]
        held, notes = calc.positions(trades)
        self.assertEqual(notes[2]["label"], "清仓")
        self.assertEqual(notes[3]["label"], "建仓")
        self.assertAlmostEqual(held["A"]["avg"], 20.0)
        self.assertEqual(held["A"]["since"], "2026-10-09")
        self.assertAlmostEqual(held["A"]["realized"], 100)

    def test_same_day_orders_follow_id(self):
        trades = [T(2, "A", "sell", 11, 100, "2026-10-05"), T(1, "A", "buy", 10, 100, "2026-10-05")]
        held, _ = calc.positions(trades)
        self.assertEqual(held, {})


class ValuationTest(unittest.TestCase):
    def test_rows_and_totals(self):
        held, _ = calc.positions([T(1, "A", "buy", 10, 1000, "2026-10-01"), T(2, "B", "buy", 20, 500, "2026-10-01")])
        quotes = {"A": {"price": 11.0, "change": 0.5, "change_pct": 4.8}, "B": {"price": 18.0, "change": -1.0, "change_pct": -5.3}}
        rows = {row["ts_code"]: row for row in calc.valuation(held, quotes, date(2026, 11, 2))}
        self.assertAlmostEqual(rows["A"]["mv"], 11000)
        self.assertAlmostEqual(rows["A"]["weight"], 11000 / 20000)
        self.assertAlmostEqual(rows["B"]["pnl"], -1000)
        self.assertEqual(rows["A"]["days"], 32)
        total = calc.totals(list(rows.values()))
        self.assertAlmostEqual(total["mv"], 20000)
        self.assertAlmostEqual(total["day_pnl"], 0.0)
        self.assertAlmostEqual(total["pnl"], 0.0)

    def test_missing_quote_falls_back_to_cost_and_is_marked(self):
        held, _ = calc.positions([T(1, "A", "buy", 10, 100, "2026-10-01")])
        row = calc.valuation(held, {}, date(2026, 11, 2))[0]
        self.assertFalse(row["live"])
        self.assertEqual((row["mv"], row["day_pnl"], row["pnl"]), (1000, 0.0, 0.0))


class ConcentrationTest(unittest.TestCase):
    def rows(self):
        held, _ = calc.positions([
            T(1, "A", "buy", 10, 1000, "2026-10-01", "三花"), T(2, "B", "buy", 10, 1500, "2026-10-01", "拓普"),
            T(3, "C", "buy", 10, 1000, "2026-10-01", "泰晶"), T(4, "D", "buy", 10, 2000, "2026-10-01", "杭氧"),
        ])
        return calc.valuation(held, {}, date(2026, 11, 2))

    def test_flags_sector_single_and_good_share(self):
        sector = {"A": "汽车零部件", "B": "汽车零部件", "C": "通信元件", "D": "通用设备"}
        verdict = {"汽车零部件": "只热不增", "通信元件": "三层同向", "通用设备": "有钱没热度"}
        conc = calc.concentration(self.rows(), sector, verdict, {("A", "B"): 0.82})
        titles = [flag["title"] for flag in conc["flags"]]
        self.assertIn("45% 的仓位压在同一个板块", titles)
        self.assertIn("单只偏重", titles)           # 杭氧 20000 / 55000 = 36%，超过 30%
        self.assertIn("有新钱进的板块", titles)
        self.assertEqual(conc["sectors"][0]["name"], "汽车零部件")
        self.assertAlmostEqual(conc["good_share"], 10000 / 55000)

    def test_single_limit(self):
        conc = calc.concentration(self.rows(), {"A": "x", "B": "y", "C": "z", "D": "w"}, {})
        self.assertIn("单只偏重", [flag["title"] for flag in conc["flags"]])
        self.assertEqual(conc["flags"][-1]["level"], "提示")   # 没有三层同向的板块

    def test_high_correlation_pair_is_named(self):
        sector = {"A": "汽车零部件", "B": "汽车零部件", "C": "通信元件", "D": "通用设备"}
        conc = calc.concentration(self.rows(), sector, {"汽车零部件": "只热不增"}, {("A", "B"): 0.82})
        flag = next(item for item in conc["flags"] if "同一个板块" in item["title"])
        self.assertIn("相关 0.82", flag["text"])
        self.assertIn("只热不增", flag["text"])


class StressTest(unittest.TestCase):
    def test_three_scenarios(self):
        held, _ = calc.positions([T(1, "A", "buy", 10, 1000, "2026-10-01", "三花"), T(2, "B", "buy", 10, 1000, "2026-10-01", "拓普"), T(3, "C", "buy", 10, 2000, "2026-10-01", "杭氧")])
        rows = calc.valuation(held, {"A": {"price": 10.0}, "B": {"price": 10.0}, "C": {"price": 10.0}}, date(2026, 11, 2))
        out = calc.stress(rows, {"A": "车", "B": "车", "C": "设备"}, {"A": 1.5, "B": 1.2, "C": 0.5}, {"B": 9.0})
        market, sector, own = out
        self.assertAlmostEqual(market["loss"], (10000 * 1.5 + 10000 * 1.2 + 20000 * 0.5) * 0.05)
        self.assertIn("三花", market["text"])
        self.assertAlmostEqual(sector["loss"], 40000 * 0.5 * 0.0 + 20000 * 0.10)   # 杭氧单独一个板块占 50%，最重
        self.assertAlmostEqual(own["loss"], (10 - 9.0) * 1000)
        self.assertIn("没设价格条件", own["text"])

    def test_nothing_held(self):
        self.assertEqual(calc.stress([], {}, {}, {}), [])


class ReasonTest(unittest.TestCase):
    def test_broken_when_price_hits_the_line(self):
        out = calc.reason_check([{"kind": "close_below", "value": 56.5}], {"price": 56.4})
        self.assertEqual(out["status"], "broken")
        self.assertIn("56.50", out["line"])

    def test_doubt_when_ai_turned_from_bullish(self):
        out = calc.reason_check([], {"price": 30, "ai_lean": "unclear", "buy_lean": "bullish", "verdict": "只热不增"})
        self.assertEqual(out["status"], "doubt")
        self.assertIn("AI 已经不再偏多", out["line"])
        self.assertIn("只热不增", out["line"])

    def test_partial_flow_out_is_a_doubt_not_a_break(self):
        out = calc.reason_check([{"kind": "flow_out", "days": 2}], {"price": 30, "flow_out": 1, "ai_lean": "bullish", "buy_lean": "bullish"})
        self.assertEqual(out["status"], "doubt")
        self.assertIn("已出现 1 天", out["line"])

    def test_still_holds_and_says_what_it_checked(self):
        out = calc.reason_check([{"kind": "custom", "text": "订单落地"}], {"price": 30, "ai_lean": "bullish", "ai_label": "偏多 · 稍微", "buy_lean": "bullish", "verdict": "三层同向", "has_ai": True})
        self.assertEqual(out["status"], "ok")
        self.assertIn("偏多", out["line"])
        self.assertIn("自己盯", out["line"])


class WishTest(unittest.TestCase):
    def wish(self, conds, match="all"):
        return {"conditions": conds, "match": match}

    def test_price_gap_and_status(self):
        state = calc.wish_state(self.wish([{"kind": "price_below", "value": 38.0}]), {"price": 41.37, "ai_lean": "unclear"})
        self.assertAlmostEqual(state["conditions"][0]["gap"], (41.37 - 38) / 41.37)   # 设计稿里的「还差 8.1%」
        self.assertEqual(state["status"], "等条件")
        near = calc.wish_state(self.wish([{"kind": "price_below", "value": 40.0}]), {"price": 41.0, "ai_lean": "bullish"})
        self.assertEqual(near["status"], "条件快到了")

    def test_all_vs_any(self):
        conds = [{"kind": "price_below", "value": 40.0}, {"kind": "etf_inflow"}]
        ctx = {"price": 39.0, "sector_flow": -1.0, "sector": "银行"}
        self.assertEqual(calc.wish_state(self.wish(conds, "all"), ctx)["status"], "条件快到了")
        self.assertEqual(calc.wish_state(self.wish(conds, "any"), ctx)["status"], "条件到了")

    def test_ai_opposite_overrides_everything(self):
        state = calc.wish_state(self.wish([{"kind": "flow_in_days", "days": 3}]), {"flow_in": 0, "ai_lean": "bearish"})
        self.assertEqual(state["status"], "AI 和你方向相反")
        self.assertEqual(state["conditions"][0]["detail"], "0 / 3 天")

    def test_market_and_custom_conditions(self):
        state = calc.wish_state(self.wish([{"kind": "market_ok"}, {"kind": "custom", "text": "三季报落地"}]), {"market": {"level": "中性", "tilt": "偏弱"}})
        self.assertFalse(state["conditions"][0]["met"])
        self.assertIsNone(state["conditions"][1]["met"])
        self.assertEqual(state["auto"], 1)
        ok = calc.wish_state(self.wish([{"kind": "market_ok"}]), {"market": {"level": "顺风", "tilt": ""}})
        self.assertTrue(ok["done"])


class PlanImpactTest(unittest.TestCase):
    def test_before_after_weights(self):
        held, _ = calc.positions([T(1, "A", "buy", 10, 1000, "2026-10-01"), T(2, "B", "buy", 10, 1000, "2026-10-01")])
        rows = calc.valuation(held, {}, date(2026, 11, 2))
        out = calc.plan_impact(rows, {"A": "汽车", "B": "设备"}, "银行", 10)
        bank = out["lines"][0]
        self.assertEqual((bank["sector"], bank["before"]), ("银行", 0.0))
        self.assertAlmostEqual(bank["after"], 2000 / 22000)
        car = next(line for line in out["lines"] if line["sector"] == "汽车")
        self.assertAlmostEqual(car["after"], 10000 / 22000)

    def test_only_the_two_heaviest_other_sectors_are_listed(self):
        held, _ = calc.positions([T(i, c, "buy", 10, q, "2026-10-01") for i, (c, q) in enumerate([("A", 1000), ("B", 800), ("C", 600), ("D", 400)], 1)])
        rows = calc.valuation(held, {}, date(2026, 11, 2))
        out = calc.plan_impact(rows, {"A": "a", "B": "b", "C": "c", "D": "d"}, "银行", 10)
        self.assertEqual([line["sector"] for line in out["lines"]], ["银行", "a", "b"])

    def test_no_holdings_no_lines(self):
        self.assertEqual(calc.plan_impact([], {}, "银行", 10)["lines"], [])


class SeriesTest(unittest.TestCase):
    def test_ret_after_uses_latest_when_short(self):
        rows = series("2026-10-01", [10, 11, 12, 13])
        ret, passed = calc.ret_after(rows, "2026-10-01", 10, 20)
        self.assertAlmostEqual(ret, 0.3)
        self.assertEqual(passed, 3)
        self.assertEqual(calc.ret_after(rows, "2026-10-04", 13, 20), (None, 0))
        self.assertAlmostEqual(calc.ret_after(rows, "2026-10-01", 10, 2)[0], 0.2)

    def test_run_up(self):
        rows = series("2026-10-01", [10] * 15 + [13])
        self.assertAlmostEqual(calc.run_up(rows, "2026-10-16", 15), 0.3)
        self.assertIsNone(calc.run_up(rows, "2026-10-05", 15))

    def test_beta_and_corr(self):
        index = series("2026-01-01", [100 + i * 0.5 + (i % 3) for i in range(80)])
        stock = [(d, c * 1.5) for d, c in index]
        # 股票价格是指数的 1.5 倍：日收益几乎同步，beta 在 1 附近，相关接近 1
        self.assertGreater(calc.corr(stock, index), 0.99)
        self.assertGreater(calc.beta(stock, index), 0.9)
        self.assertIsNone(calc.beta(stock[:10], index[:10]))


class ReviewTest(unittest.TestCase):
    def setUp(self):
        ctx = lambda lean, label: {"ai": {"lean": lean, "label": label}}   # noqa: E731
        self.trades = [
            T(1, "A", "buy", 10, 1000, "2026-09-22", "中大力德", ctx("bullish", "偏多 · 比较明显"), reason="国产替代"),
            T(2, "A", "sell", 11.88, 1000, "2026-10-15", "中大力德"),
            T(3, "B", "buy", 20, 500, "2026-10-09", "宝信软件", ctx("bearish", "偏空 · 稍微"), reason="订单"),
            T(4, "B", "sell", 19.06, 500, "2026-10-24", "宝信软件"),
            T(5, "C", "buy", 30, 100, "2026-10-13", "三花", ctx("unclear", "不明")),
            T(6, "C", "buy", 27, 100, "2026-10-28", "三花", ctx("bullish", "偏多")),
        ]
        _, self.notes = calc.positions(self.trades)
        self.closes = {"A": series("2026-10-15", [11.88] + [12.0] * 25), "B": series("2026-10-24", [19.06, 18.9, 18.7])}

    def test_counts_and_ai_alignment(self):
        out = calc.review(self.trades, self.notes, self.closes, {"B": 18.7, "C": 28.0}, date(2026, 11, 2))
        self.assertEqual((out["total"], out["buys"], out["adds"], out["sells"]), (6, 4, 1, 2))
        self.assertEqual((out["aligned"], out["unclear"], out["risky"]), (2, 1, 1))
        self.assertEqual(out["risk_rows"][0]["name"], "宝信软件")

    def test_sold_rows(self):
        out = calc.review(self.trades, self.notes, self.closes, {}, date(2026, 11, 2))
        newest, oldest = out["sold"]
        self.assertEqual(newest["name"], "宝信软件")
        self.assertEqual(newest["relation"], "AI 提示风险后仍买")
        self.assertAlmostEqual(newest["pnl_pct"], 19.06 / 20 - 1)
        self.assertIn("止损有效", newest["verdict"])
        self.assertEqual(oldest["relation"], "跟 AI 一致")
        self.assertTrue(oldest["after_done"])
        self.assertIn("兑现", oldest["verdict"])
        self.assertEqual(oldest["reason"], "国产替代")

    def test_window_excludes_old_trades(self):
        out = calc.review(self.trades, self.notes, self.closes, {}, date(2027, 1, 30))
        self.assertEqual(out["total"], 0)

    def test_verdict_words(self):
        self.assertIn("卖早了", calc.sold_verdict(0.1, 0.12, True))
        self.assertIn("高位附近", calc.sold_verdict(0.1, -0.03, True))
        self.assertIn("低位", calc.sold_verdict(-0.05, 0.2, True))
        self.assertIn("基本持平", calc.sold_verdict(-0.05, 0.02, False))
        self.assertIn("没有可比", calc.sold_verdict(0.1, None, False))


class HabitTest(unittest.TestCase):
    def test_four_habits(self):
        trades = [
            T(1, "A", "buy", 10, 100, "2026-10-01", "三花"),
            T(2, "A", "buy", 8, 100, "2026-10-20", "三花"),          # 浮亏加仓
            T(3, "B", "buy", 13, 100, "2026-10-16", "追高股"),       # 买入前 15 天涨了 30%
            T(4, "C", "buy", 10, 100, "2026-09-15", "卖早股"),
            T(5, "C", "sell", 10, 100, "2026-09-20", "卖早股"),      # 卖后涨超 10%
        ]
        _, notes = calc.positions(trades)
        closes = {
            "B": series("2026-10-01", [10] * 15 + [13]),
            "C": series("2026-09-20", [10] + [11.5] * 30),
        }
        out = {item["key"]: item for item in calc.habits(trades, notes, closes, {"A": 9.0}, {}, date(2026, 11, 2))}
        self.assertEqual((out["chase"]["count"], out["early"]["count"], out["dip"]["count"], out["stuck"]["count"]), (1, 1, 1, 0))
        self.assertIn("三花 10-20 加仓，目前 +12.5%", out["dip"]["detail"])
        self.assertEqual(out["early"]["after"], "+15.0%")

    def test_stuck_counts_broken_reasons(self):
        reasons = {"A": {"status": "broken", "name": "三花"}, "B": {"status": "doubt", "name": "拓普"}}
        out = {item["key"]: item for item in calc.habits([], {}, {}, {}, reasons, date(2026, 11, 2))}
        self.assertEqual(out["stuck"]["count"], 1)
        self.assertIn("三花", out["stuck"]["detail"])


class TradeFlagTest(unittest.TestCase):
    def test_buy_against_a_no_add_market_is_flagged(self):
        trade = T(1, "A", "buy", 10, 100, "2026-10-28", context={"market": {"level": "中性", "tilt": "偏弱"}})
        flags = calc.trade_flags(trade)
        self.assertTrue(flags["conflict"])
        self.assertEqual(flags["market_text"], "中性偏弱 · 不加仓")
        sell = T(2, "A", "sell", 10, 100, "2026-10-28", context={"market": {"level": "中性", "tilt": "偏弱"}})
        self.assertFalse(calc.trade_flags(sell)["conflict"])
        self.assertFalse(calc.trade_flags(T(3, "A", "buy", 1, 1, "2026-10-28"))["conflict"])


class CheckupTest(unittest.TestCase):
    def test_compare(self):
        before = {"top_sector": {"name": "汽车零部件", "weight": 0.52}, "weak": 0, "weak_names": [], "issues": ["unset_alert", "sector_heavy"]}
        now = {"top_sector": {"name": "汽车零部件", "weight": 0.417}, "weak": 1, "weak_names": ["三花"], "issues": ["unset_alert"]}
        out = calc.compare_checkups(now, before)
        text = {line["title"]: line for line in out["lines"]}
        self.assertEqual(text["汽车零部件占比"]["text"], "52.0% → 41.7%")
        self.assertEqual(text["汽车零部件占比"]["note"], "下降了")
        self.assertIn("新增：三花", text["理由存疑 / 不成立"]["note"])
        self.assertIn("解决 1", text["上次提的问题"]["text"])
        self.assertIsNone(calc.compare_checkups(now, None))


class HotTest(unittest.TestCase):
    def test_groups_and_mine(self):
        sectors = [
            {"name": "银行", "verdict": "三层同向", "flow": 8.2}, {"name": "白酒", "verdict": "三层同向", "flow": 2.3},
            {"name": "红利", "verdict": "有钱没热度", "flow": 5.1}, {"name": "芯片", "verdict": "只热不增", "flow": -9.6},
        ]
        groups = calc.hot_groups(sectors, {"银行": [("招商银行", 0.1)]})
        self.assertEqual([g["title"].split(" ·")[0] for g in groups], ["有钱又热", "钱在进、还不热", "看着热、没新钱"])
        self.assertEqual(groups[0]["rows"][0]["name"], "银行")
        self.assertEqual(groups[0]["rows"][0]["mine"], [("招商银行", 0.1)])

    def test_suggestions(self):
        held, _ = calc.positions([T(1, "A", "buy", 10, 1000, "2026-10-01", "三花"), T(2, "B", "buy", 10, 1000, "2026-10-01", "杭氧")])
        rows = calc.valuation(held, {}, date(2026, 11, 2))
        conc = {"flags": [{"level": "注意", "title": "45% 的仓位压在同一个板块", "text": "x"}]}
        reasons = {"A": {"status": "doubt", "name": "三花", "line": "AI 已经不再偏多"}}
        hot = [{"title": "有钱又热 · 真热点", "rows": [{"name": "银行", "mine": []}]}]
        out = calc.suggestions(rows, conc, reasons, {"B": 9.0}, {}, hot, {"汽车"})
        self.assertEqual([item["key"] for item in out], ["recheck", "alert", "sector", "hot"])
        self.assertIn("三花", out[0]["title"])
        self.assertIn("三花", out[1]["text"])   # 杭氧设了价格条件，只剩三花没有
        self.assertEqual(out[3]["sectors"], ["银行"])


if __name__ == "__main__":
    unittest.main()
