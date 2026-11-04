import unittest
from datetime import date

from app.services import market_rules as rules
from app.services.market_page import summarize, tab_of
from app.services.market_sector import flows, heat
from app.services.market_tape import format_limits, format_valuation, monthly_funds, weekly_supply


class MoodTest(unittest.TestCase):
    def test_three_clues_all_negative_is_adverse(self):
        indexes = [{"above_ma60": False}] * 4
        signal = rules.mood_signal(indexes, {"up": 1000, "down": 4000}, {"follow": -1.2, "broken_rate": 0.45})
        self.assertEqual(signal["tier"], "逆")
        self.assertEqual(signal["score"], -3)

    def test_mixed_clues_are_flat(self):
        indexes = [{"above_ma60": False}] * 4
        signal = rules.mood_signal(indexes, {"up": 4500, "down": 800}, {"follow": -0.3, "broken_rate": 0.22})
        self.assertEqual(signal["tier"], "平")

    def test_no_data_gives_no_tier(self):
        self.assertIsNone(rules.mood_signal([], {}, {})["tier"])


class OverallTest(unittest.TestCase):
    def test_a_missing_signal_does_not_count(self):
        signals = [{"tier": "逆"}, {"tier": None}, {"tier": "平"}]
        total = rules.overall(signals)
        self.assertEqual((total["level"], total["tilt"], total["count"]), ("中性", "偏弱", 2))

    def test_two_adverse_signals_make_a_headwind(self):
        self.assertEqual(rules.overall([{"tier": "逆"}, {"tier": "逆"}])["level"], "逆风")

    def test_action_names_sectors_only_when_weak(self):
        self.assertIn("银行", rules.action_text("中性", "偏弱", ["银行"]))
        self.assertNotIn("银行", rules.action_text("中性", "偏强", ["银行"]))


class SectorLayerTest(unittest.TestCase):
    def test_four_verdicts(self):
        self.assertEqual(rules.sector_layers(8.2, 1.5, 0.03)["verdict"], "三层同向")
        self.assertEqual(rules.sector_layers(5.1, 0.9, 0.0)["verdict"], "有钱没热度")
        self.assertEqual(rules.sector_layers(-9.6, 2.1, -0.05)["verdict"], "只热不增")
        self.assertEqual(rules.sector_layers(-4.0, 0.7, -0.05)["verdict"], "在流出")

    def test_falling_leverage_blocks_the_three_layer_label(self):
        self.assertEqual(rules.sector_layers(8.2, 1.8, -0.05)["verdict"], "有钱没热度")

    def test_missing_leverage_does_not_block_it(self):
        layers = rules.sector_layers(3.0, 1.6, None)
        self.assertEqual((layers["verdict"], layers["lever_word"]), ("三层同向", "—"))

    def test_lever_band(self):
        self.assertEqual([rules.lever_word(v) for v in (0.05, 0.01, -0.03, None)], ["回升", "走平", "下降", "—"])

    def test_flow_is_share_change_times_close(self):
        shares = [{"trade_date": f"2026110{day}", "fd_share": 1000 + day * 100} for day in range(1, 6)]
        closes = [{"trade_date": f"2026110{day}", "close": 2.0} for day in range(1, 6)]
        out = flows(shares, closes)
        self.assertAlmostEqual(out[3], 3 * 100 * 2.0 / 10000)
        self.assertIsNone(out[20])

    def test_heat_is_today_turnover_over_own_average(self):
        shares = [{"trade_date": "20260101", "fd_share": 1000}]
        closes = [{"trade_date": f"2026{(i // 28) + 1:02d}{i % 28 + 1:02d}", "vol": 100} for i in range(80)]
        closes[-1]["vol"] = 200
        self.assertAlmostEqual(heat(shares, closes), 200 / (sum(item["vol"] for item in closes) / 80), places=3)


class WatchActionTest(unittest.TestCase):
    def test_actions(self):
        self.assertEqual(rules.watch_action("三层同向", "bullish")[0], "顺势持有")
        self.assertEqual(rules.watch_action("三层同向", "unclear")[0], "板块有利，等个股")
        self.assertEqual(rules.watch_action("只热不增", "bullish")[0], "不追高")
        self.assertEqual(rules.watch_action(None, "bullish")[0], "看个股判断")


class MacroTest(unittest.TestCase):
    def test_direction_matches_the_framework_example(self):
        scores = {"policy": 3, "liquidity": 1, "profit": 5, "value": 5}
        out = rules.macro_direction(scores)
        self.assertEqual((out["score"], out["tier"]), (3.30, "平"))

    def test_liquidity_widening_gap_is_one(self):
        history = [{"m1": 5.0, "m2": 8.0}, {"m1": 4.0, "m2": 8.0}]
        self.assertEqual(rules.liquidity_score(history)[0], 1)

    def test_profit_needs_two_months_in_a_row(self):
        up = [{"ppi": 1.0}, {"ppi": 2.0}, {"ppi": 3.0}]
        mixed = [{"ppi": 3.0}, {"ppi": 2.0}, {"ppi": 3.0}]
        self.assertEqual((rules.profit_score(up)[0], rules.profit_score(mixed)[0]), (5, 3))

    def test_value_bands(self):
        self.assertEqual([rules.value_score(v)[0] for v in (0.9, 0.5, 0.1, None)], [5, 3, 1, 3])


class TapeExtrasTest(unittest.TestCase):
    def test_limits_follow_and_broken_rate(self):
        today = [{"limit": "U", "limit_times": 2}] * 6 + [{"limit": "Z"}] * 4 + [{"limit": "D"}]
        prev = [{"ts_code": "A", "limit": "U"}, {"ts_code": "B", "limit": "U"}, {"ts_code": "C", "limit": "D"}]
        body = format_limits(today, {"A": 3.0, "B": -1.0, "C": 5.0}, prev)
        self.assertAlmostEqual(body["metrics"]["follow"], 1.0)
        self.assertEqual(body["metrics"]["follow_count"], 2)
        self.assertAlmostEqual(body["metrics"]["broken_rate"], 0.4)

    def test_weekly_supply_buckets_by_announce_date(self):
        rows = [
            {"ann_date": "20261103", "change_vol": 1e6, "avg_price": 10, "in_de": "IN"},
            {"ann_date": "20261103", "change_vol": 2e6, "avg_price": 10, "in_de": "DE"},
        ]
        weeks = weekly_supply(rows, date(2026, 11, 4))
        self.assertEqual(len(weeks), 12)
        self.assertAlmostEqual(weeks[-1]["buy"], 0.1)
        self.assertAlmostEqual(weeks[-1]["sell"], 0.2)

    def test_monthly_funds_counts_equity_funds_only(self):
        rows = [
            {"fund_type": "股票型", "issue_date": "20261008", "issue_amount": 12.0},
            {"fund_type": "债券型", "issue_date": "20261009", "issue_amount": 50.0},
            {"fund_type": "混合型", "issue_date": "20261020", "issue_amount": None},
        ]
        months = monthly_funds(rows, date(2026, 11, 4))
        self.assertEqual(len(months), 12)
        self.assertEqual((months[-2]["count"], months[-2]["amount"]), (2, 12.0))

    def test_valuation_uses_earnings_yield_percentile(self):
        rows = [{"trade_date": f"2026{(i // 28) + 1:02d}{i % 28 + 1:02d}", "pe_ttm": 10 + i * 0.1} for i in range(90)]
        body = format_valuation(rows)
        self.assertLess(body["metrics"]["pctile"], 0.05)  # PE 最高 = 盈利收益率最低
        self.assertIn("不是 ERP", body["text"])


def _parts() -> dict:
    indexes = [
        {"name": "上证指数", "close": 3600.0, "pct": -0.8, "date": "2026-11-04", "above_ma60": False, "above_ma20": False, "prior_low": 3500.0, "broke_low": False},
        {"name": "创业板指", "close": 2000.0, "pct": -1.9, "date": "2026-11-04", "above_ma60": False, "above_ma20": False, "prior_low": 2100.0, "broke_low": True},
    ]
    return {
        "turnover": {"label": "冷清", "ratio5": -0.12, "amount": 16000, "date": "2026-11-04", "down_days": 4, "series": []},
        "etf": {"label": "基本没变", "ratio": -0.004, "buy_days": 1, "money": -9.8, "groups": {}, "series": [], "latest_date": "2026-11-03"},
        "margin": {"label": "在降", "ratio4": -0.031, "balance": 20000, "date": "2026-11-03", "series": []},
        "south": {}, "shibor": {}, "supply": {"net": -30.0, "label": "在拿走"}, "funds": {},
        "breadth": {"up": 1280, "down": 3950, "highs": 42, "lows": 318, "date": "2026-11-04"},
        "limits": {"up": 38, "down": 22, "broken": 14, "height": 4, "broken_rate": 0.41, "follow": -1.2, "follow_count": 30, "date": "2026-11-04"},
        "index": {"rows": indexes}, "sw": {}, "margin_industry": {},
        "macro": {"month": "2026年9月", "history": [{"m1": 5.0, "m2": 8.0, "ppi": 1.0}, {"m1": 4.0, "m2": 8.0, "ppi": 2.0}, {"m1": 3.0, "m2": 8.0, "ppi": 3.0}]},
        "valuation": {"pctile": 0.85}, "fx": {}, "us": {}, "hk": {},
        "ipo": {"days": []}, "calendar": {"events": [], "days": []}, "unlock": {},
        "sectors": {"rows": [
            {"name": "银行", "code": "512800.SH", "industry": "银行", "flow": {10: 8.2}, "heat": 1.5, "lever": 0.03, "lever_word": "回升", "verdict": "三层同向", "tone": "good"},
            {"name": "光伏", "code": "515790.SH", "industry": "电力设备", "flow": {10: -4.0}, "heat": 0.7, "lever": -0.05, "lever_word": "下降", "verdict": "在流出", "tone": "bad"},
        ], "date": "2026-11-03", "windows": [3, 5, 10, 20], "conclusion_days": 10},
        "watch": [], "_prev": "2026-11-03",
    }


class SummaryTest(unittest.TestCase):
    def test_weak_and_adverse_money_and_mood_make_a_headwind_without_the_team_signal(self):
        out = summarize(_parts(), date(2026, 11, 4))
        con = out["conclusion"]
        self.assertEqual((con["level"], con["count"]), ("逆风", 2))
        team = next(item for item in con["signals"] if item["key"] == "team")
        self.assertIsNone(team["tier"])
        self.assertTrue(out["team"]["pending"])
        self.assertEqual(out["macro"]["score"], 3.30)

    def test_better_and_worse_lists_carry_current_values(self):
        out = summarize(_parts(), date(2026, 11, 4))
        worse = {item["text"]: item for item in out["conclusion"]["worse"]}
        self.assertTrue(worse["指数跌破前低"]["met"])
        self.assertIn("创业板指", worse["指数跌破前低"]["now"])

    def test_every_tab_builds(self):
        parts = _parts()
        for name in ("fund", "team", "sector", "mood", "cal", "macro"):
            self.assertIn("title", tab_of(name, parts, date(2026, 11, 4)))


if __name__ == "__main__":
    unittest.main()
