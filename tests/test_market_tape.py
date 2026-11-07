import unittest
from datetime import date
from unittest import mock

from app.services import market_tape
from app.services.market_page import conclusion
from app.services.market_tape import (
    format_breadth,
    format_etf,
    format_limits,
    format_margin,
    format_south,
    format_supply,
    format_turnover,
    judge_etf_signal,
    judge_margin_signal,
    judge_pulse,
    judge_turnover_votes,
    modified_z,
)


class MarketTapeRuleTest(unittest.TestCase):
    def test_modified_z_matches_one_robust_sigma(self):
        self.assertAlmostEqual(modified_z(1.4826, 0, 1), 1.0, places=4)

    def test_turnover_needs_the_30_day_and_two_cold_days(self):
        self.assertEqual(judge_turnover_votes({1: "冷清", 3: "冷清", 5: "平时", 30: "冷清"}, ("冷清", "冷清")), "冷清")
        self.assertEqual(judge_turnover_votes({1: "冷清", 3: "平时", 5: "平时", 30: "平时"}, ("冷清", "冷清")), "平时")
        self.assertEqual(judge_turnover_votes({1: "冷清", 3: "冷清", 5: "冷清", 30: "冷清"}, ("冷清", "平时")), "平时")
        self.assertEqual(judge_turnover_votes({1: "活跃", 3: "活跃", 5: "平时", 30: "活跃"}, ("活跃", "活跃")), "活跃")

    def test_etf_and_margin_use_the_five_day_window(self):
        self.assertEqual(judge_etf_signal(2.0, 2.0, 2.0, 0.01, 1, False, True), "净流入")
        self.assertEqual(judge_etf_signal(2.0, 2.0, 2.0, 0.01, 1, True, True), "基本没变")
        self.assertEqual(judge_etf_signal(2.0, 2.0, 2.0, 0.002, 0, False, True), "基本没变")
        self.assertEqual(judge_etf_signal(None, None, None, 0.012, 1, False, False), "净流入")
        self.assertEqual(judge_etf_signal(-2.0, -2.0, None, -0.01, 3, True, True), "净流出")
        self.assertEqual(judge_margin_signal(-2.4, -1.2, -0.2, True), "加速下降")
        self.assertEqual(judge_margin_signal(-1.6, -0.2, None, True), "在降")
        self.assertEqual(judge_margin_signal(-1.2, -1.6, None, True), "在降")
        self.assertEqual(judge_margin_signal(1.8, -0.4, None, True), "在升")
        self.assertEqual(judge_margin_signal(1.8, -1.4, None, True), "持平")
        self.assertEqual(judge_margin_signal(None, -3.0, -3.0, False), "持平")

    def test_margin_and_pulse(self):
        self.assertEqual(judge_pulse("冷清", "基本没变", "在降"), "偏弱")
        self.assertEqual(judge_pulse("平时", "净流入", "在降"), "在进")
        self.assertEqual(judge_pulse("平时", "净流入", "加速下降"), "观望")
        self.assertEqual(judge_pulse("活跃", "净流入", "加速下降"), "在进")
        self.assertEqual(judge_pulse("冷清", "净流入", "在降"), "偏弱")

    def test_turnover_text_uses_yi_and_refuses_a_flow_story(self):
        rows = [{"trade_date": f"202610{day:02d}", "amount": 20000} for day in range(1, 21)]
        rows += [{"trade_date": f"202611{day:02d}", "amount": 16000} for day in range(1, 6)]
        body = format_turnover(rows, {"20261105": -0.4, "20261104": -0.2, "20261103": -0.1, "20261102": 0.2, "20261101": -0.3})
        self.assertIn("基线不足", body["text"])
        self.assertIn("不代表钱在进还是出", body["text"])

    def test_etf_share_change_times_close_is_yuan_yi(self):
        shares = {"510300.SH": [
            {"trade_date": "20261102", "fd_share": 10000},
            {"trade_date": "20261103", "fd_share": 20000},
        ]}
        closes = {"510300.SH": [{"trade_date": "20261103", "close": 4}]}
        body = format_etf(shares, closes)
        self.assertIn("+4.00 亿元", body["text"])
        self.assertIn("fund_share", body["text"])

    def test_margin_balance_is_yuan(self):
        rows = []
        for day, balance in (("20261001", 100e8), ("20261020", 100e8), ("20261103", 96.9e8)):
            rows.append({"trade_date": day, "rzye": balance, "rzmre": 1e8, "rzche": 1.2e8, "exchange_id": "SSE"})
            rows.append({"trade_date": day, "rzye": 0, "rzmre": 0, "rzche": 0, "exchange_id": "SZSE"})
        # need 6 distinct days
        for day in ("20261002", "20261003", "20261004", "20261005"):
            rows.append({"trade_date": day, "rzye": 100e8, "rzmre": 1e8, "rzche": 1e8, "exchange_id": "SSE"})
            rows.append({"trade_date": day, "rzye": 0, "rzmre": 0, "rzche": 0, "exchange_id": "SZSE"})
        body = format_margin(rows, turnover=10000)
        self.assertIn("融资余额", body["text"])
        self.assertEqual(body["metrics"]["label"], "持平")
        self.assertEqual(body["metrics"]["baseline"], "不足")

    def test_south_does_not_call_the_level_a_daily_flow(self):
        hsgt = [{"trade_date": f"2026110{i}", "south_money": 55000 + i} for i in range(1, 7)]
        ggt = [{"trade_date": "20261106", "buy_amount": 375.75, "sell_amount": 226.61}]
        body = format_south(hsgt, ggt)
        self.assertIn("不能直接写成当日净买入", body["text"])
        self.assertIn("+149.14", body["text"])

    def test_supply_nets_increase_buyback_and_reduction(self):
        body = format_supply(
            [{"in_de": "IN", "change_vol": 1e6, "avg_price": 10}, {"in_de": "DE", "change_vol": 2e6, "avg_price": 10}],
            [{"ts_code": "000001.SZ", "ann_date": "20261101", "proc": "实施", "amount": 5e6}],
        )
        self.assertIn("在拿走", body["text"])

    def test_breadth_and_limits(self):
        breadth = format_breadth(
            [{"ts_code": "000001.SZ", "pct_chg": 1, "close": 12}, {"ts_code": "000002.SZ", "pct_chg": -1, "close": 8}],
            {"000001.SZ": [10] * 20, "000002.SZ": [9] * 20},
        )
        self.assertIn("上涨 1 家", breadth["text"])
        self.assertIn("新高 1 家", breadth["text"])
        limits = format_limits([
            {"limit": "U", "limit_times": 3},
            {"limit": "D", "limit_times": 1},
            {"limit": "Z", "limit_times": 1},
        ])
        self.assertIn("涨停 1", limits["text"])
        self.assertIn("最高连板 3", limits["text"])


class PulseTest(unittest.TestCase):
    def test_pulse_uses_the_judged_labels_not_words_in_the_text(self):
        turnover = format_turnover(
            [{"trade_date": f"2026{(i // 28) + 1:02d}{i % 28 + 1:02d}", "amount": 20000.0} for i in range(70)]
        )
        self.assertEqual(turnover["metrics"]["label"], "平时")
        self.assertIn("冷清", turnover["text"])   # 说明文字里固定写着「冷清」，从文字里抓标签会抓错
        docs = {
            "_turnover": market_tape._from("turnover", turnover),
            "_etf": market_tape._doc("etf", [{"id": "x", "text": "判定 净流入"}], "判定 净流入", {"label": "净流入"}),
            "_margin": market_tape._doc("margin", [{"id": "x", "text": "判定 加速下降"}], "判定 加速下降", {"label": "加速下降"}),
        }
        with mock.patch.multiple(market_tape, **{name: mock.Mock(return_value=doc) for name, doc in docs.items()}):
            out = market_tape.build("pulse", date(2026, 11, 5))
        self.assertIn("短期判定 观望", out["text"])
        self.assertIn("成交 平时", out["text"])

    def test_pulse_says_none_when_a_part_has_no_label(self):
        empty = market_tape._doc("turnover", [], "没有取到足够的全市场成交额")
        etf = market_tape._doc("etf", [{"id": "x", "text": "y"}], "y", {"label": "净流入"})
        margin = market_tape._doc("margin", [{"id": "x", "text": "y"}], "y", {"label": "持平"})
        with mock.patch.multiple(market_tape, _turnover=mock.Mock(return_value=empty), _etf=mock.Mock(return_value=etf), _margin=mock.Mock(return_value=margin)):
            out = market_tape.build("pulse", date(2026, 11, 5))
        self.assertIn("成交 没有", out["text"])


class BoardLimitTest(unittest.TestCase):
    def test_board_limits(self):
        from app.services.market_tape import board_limit, scaled_pct

        self.assertEqual([board_limit(code) for code in ("600519.SH", "000001.SZ", "300750.SZ", "301001.SZ", "688981.SH", "830001.BJ")], [10, 10, 20, 20, 20, None])
        self.assertAlmostEqual(scaled_pct(10.0, "300750.SZ"), 5.0)
        self.assertAlmostEqual(scaled_pct(10.0, "600519.SH"), 10.0)
        self.assertIsNone(scaled_pct(10.0, "830001.BJ"))

    def test_format_limits_ignores_beijing_and_scales_follow(self):
        rows = [{"ts_code": "600001.SH", "limit": "U", "limit_times": 2}, {"ts_code": "830001.BJ", "limit": "U", "limit_times": 5}, {"ts_code": "300001.SZ", "limit": "D"}]
        prev = [{"ts_code": "600001.SH", "limit": "U"}, {"ts_code": "300001.SZ", "limit": "U"}, {"ts_code": "830001.BJ", "limit": "U"}]
        today = {"600001.SH": 4.0, "300001.SZ": 10.0, "830001.BJ": 30.0}
        out = format_limits(rows, today, prev)["metrics"]
        self.assertEqual((out["up"], out["down"], out["height"]), (1, 1, 2))     # 北交所的 5 板不算
        self.assertEqual(out["follow_count"], 2)
        self.assertAlmostEqual(out["follow"], (4.0 + 5.0) / 2)                   # 20cm 的 10% 折成 5%


class MarketPageTest(unittest.TestCase):
    def test_quiet_and_falling_margin_is_weak_and_the_gate_stays_shut(self):
        body = conclusion("冷清", "净流入", "加速下降")
        self.assertEqual(body["label"], "偏弱")
        self.assertEqual(body["gate"], "不过")
        self.assertTrue(body["toward_in"])

    def test_inflow_with_normal_turnover_opens_the_gate(self):
        body = conclusion("平时", "净流入", "持平")
        self.assertEqual(body["label"], "在进")
        self.assertEqual(body["gate"], "通过")
        self.assertEqual(body["toward_in"], [])

    def test_inflow_with_flat_turnover_and_fast_margin_decline_stays_neutral(self):
        body = conclusion("平时", "净流入", "加速下降")
        self.assertEqual(body["label"], "观望")
        self.assertIn("资金在进、热度中性、杠杆在退", body["notes"])


if __name__ == "__main__":
    unittest.main()
