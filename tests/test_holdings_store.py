import unittest

from app.services.holdings_store import HoldingsStore, valid_day


class TradeTest(unittest.TestCase):
    def setUp(self):
        self.store = HoldingsStore(":memory:")

    def buy(self, day="2026-10-13", qty=1000, price=29.35, code="002050.SZ"):
        return self.store.add_trade(code, "三花智控", "buy", price, qty, day)

    def test_round_trip_keeps_context_and_invalid_conditions(self):
        trade = self.store.add_trade(
            "002050.SZ", "三花智控", "buy", 29.35, 1000, "2026-10-13", reason=" 订单会落地 ",
            invalid=[{"kind": "close_below", "value": 25.0}], context={"market": {"level": "中性"}},
        )
        self.assertEqual(trade["reason"], "订单会落地")
        self.assertEqual(trade["invalid"], [{"kind": "close_below", "value": 25.0}])
        self.assertEqual(self.store.trades("002050.SZ")[0]["context"]["market"]["level"], "中性")

    def test_rejects_bad_input(self):
        for args in (("buy", 0, 100, "2026-10-13"), ("buy", 10, 0, "2026-10-13"), ("buy", 10, 100, "10-13"), ("hold", 10, 100, "2026-10-13"), ("buy", 10, 1.5, "2026-10-13")):
            with self.assertRaises(ValueError):
                self.store.add_trade("002050.SZ", "三花智控", *args)

    def test_cannot_sell_more_than_held_on_that_day(self):
        self.buy(qty=1000)
        with self.assertRaises(ValueError):
            self.store.add_trade("002050.SZ", "三花智控", "sell", 30, 1500, "2026-10-20")
        # 卖出日期早于买入日期，那一天手里还没有
        with self.assertRaises(ValueError):
            self.store.add_trade("002050.SZ", "三花智控", "sell", 30, 100, "2026-10-01")
        self.store.add_trade("002050.SZ", "三花智控", "sell", 30, 1000, "2026-10-20")

    def test_cannot_delete_a_buy_that_later_sells_depend_on(self):
        first = self.buy(qty=1000)
        self.store.add_trade("002050.SZ", "三花智控", "sell", 30, 600, "2026-10-20")
        with self.assertRaises(ValueError):
            self.store.delete_trade(first["id"])
        sell = self.store.trades()[-1]
        self.assertTrue(self.store.delete_trade(sell["id"]))
        self.assertTrue(self.store.delete_trade(first["id"]))
        self.assertFalse(self.store.delete_trade(first["id"]))

    def test_note(self):
        trade = self.buy()
        self.assertTrue(self.store.set_trade_note(trade["id"], " 买早了 "))
        self.assertEqual(self.store.trade(trade["id"])["note"], "买早了")
        self.assertFalse(self.store.set_trade_note(999, "x"))

    def test_day_check(self):
        self.assertTrue(valid_day("2026-11-02"))
        self.assertFalse(valid_day("2026-13-02"))
        self.assertFalse(valid_day(None))


class WishTest(unittest.TestCase):
    def setUp(self):
        self.store = HoldingsStore(":memory:")

    def add(self, code="600036.SH", **extra):
        return self.store.add_wish(code, "招商银行", "银行三层同向", [{"kind": "price_below", "value": 36.0}], plan_pct=10, **extra)

    def test_needs_a_reason_and_no_duplicates(self):
        wish = self.add()
        self.assertEqual(wish["status"], "open")
        with self.assertRaises(ValueError):
            self.add()
        with self.assertRaises(ValueError):
            self.store.add_wish("600519.SH", "贵州茅台", "  ", [])
        self.assertTrue(self.store.close_wish(wish["id"], "bought"))
        self.add()   # 关掉以后可以再加
        self.assertEqual(len(self.store.wishes("open")), 1)
        self.assertEqual(len(self.store.wishes(None)), 2)

    def test_update_only_open_and_validated(self):
        wish = self.add()
        updated = self.store.update_wish(wish["id"], plan_pct=8, match="any")
        self.assertEqual((updated["plan_pct"], updated["match"]), (8.0, "any"))
        with self.assertRaises(ValueError):
            self.store.update_wish(wish["id"], match="some")
        self.store.close_wish(wish["id"])
        self.assertEqual(self.store.update_wish(wish["id"], plan_pct=3)["plan_pct"], 8.0)


class SmallTablesTest(unittest.TestCase):
    def setUp(self):
        self.store = HoldingsStore(":memory:")

    def test_reason_upsert(self):
        self.store.set_reason("002050.SZ", "第一版", [{"kind": "sector_out"}], "2026-10-13")
        self.store.set_reason("002050.SZ", "第二版")
        self.assertEqual(self.store.reason("002050.SZ")["text"], "第二版")
        with self.assertRaises(ValueError):
            self.store.set_reason("002050.SZ", " ")
        self.store.drop_reason("002050.SZ")
        self.assertIsNone(self.store.reason("002050.SZ"))

    def test_rules_dedupe(self):
        first = self.store.add_rule("002050.SZ", "flow_out", 2)
        again = self.store.add_rule("002050.SZ", "flow_out", 2)
        self.assertEqual(first["id"], again["id"])
        with self.assertRaises(ValueError):
            self.store.add_rule("002050.SZ", "nope", 2)
        self.assertTrue(self.store.remove_rule(first["id"]))

    def test_checkups_newest_first_and_kv(self):
        self.store.save_checkup({"n": 1}, now=1.0)
        self.store.save_checkup({"n": 2}, now=2.0)
        self.assertEqual([row["data"]["n"] for row in self.store.checkups(2)], [2, 1])
        self.store.set("notif_read", 5)
        self.assertEqual(self.store.get("notif_read"), 5)
        self.assertEqual(self.store.get("missing", "x"), "x")


if __name__ == "__main__":
    unittest.main()
