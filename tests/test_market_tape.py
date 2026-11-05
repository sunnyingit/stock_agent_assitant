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
    judge_etf,
    judge_etf_spans,
    judge_margin,
    judge_margin_spans,
    judge_pulse,
    judge_turnover,
    judge_turnover_spans,
)


class MarketTapeRuleTest(unittest.TestCase):
    def test_turnover_is_quiet_below_five_percent(self):
        self.assertEqual(judge_turnover(-0.12), "冷清")
        self.assertEqual(judge_turnover(0.02), "平时")
        self.assertEqual(judge_turnover(0.08), "活跃")

    def test_spans_need_more_than_the_five_day_number(self):
        self.assertEqual(judge_turnover_spans({1: 0.08, 3: -0.02, 5: -0.08, 30: -0.11}), "平时")
        self.assertEqual(judge_turnover_spans({1: -0.08, 3: -0.08, 5: -0.08, 30: -0.02}), "冷清")
        self.assertEqual(judge_etf_spans({5: 0.02, 30: 0.03, 60: 0.04}, 4), "净流入")
        self.assertEqual(judge_etf_spans({5: 0.02, 30: -0.02, 60: 0.04}, 4), "基本没变")
        self.assertEqual(judge_margin_spans({30: -0.06, 60: -0.01}), "在降")
        self.assertEqual(judge_margin_spans({30: -0.06, 60: -0.08}), "加速下降")

    def test_etf_needs_both_size_and_four_buy_days(self):
        self.assertEqual(judge_etf(0.012, 4), "净流入")
        self.assertEqual(judge_etf(0.012, 2), "基本没变")
        self.assertEqual(judge_etf(-0.011, 1), "净流出")

    def test_margin_and_pulse(self):
        self.assertEqual(judge_margin(-0.031), "在降")
        self.assertEqual(judge_margin(-0.06), "加速下降")
        self.assertEqual(judge_pulse("冷清", "基本没变", "在降"), "偏弱")
        self.assertEqual(judge_pulse("平时", "净流入", "在降"), "在进")
        self.assertEqual(judge_pulse("冷清", "净流入", "在降"), "偏弱")

    def test_turnover_text_uses_yi_and_refuses_a_flow_story(self):
        rows = [{"trade_date": f"202610{day:02d}", "amount": 20000} for day in range(1, 21)]
        rows += [{"trade_date": f"202611{day:02d}", "amount": 16000} for day in range(1, 6)]
        body = format_turnover(rows, {"20261105": -0.4, "20261104": -0.2, "20261103": -0.1, "20261102": 0.2, "20261101": -0.3})
        self.assertIn("冷清", body["text"])
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
        self.assertIn("在降", body["text"])

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
        self.assertIn("冷清", turnover["text"])   # 说明文字里固定写着「改冷清或活跃」，从文字里抓标签会抓成冷清
        docs = {
            "_turnover": market_tape._from("turnover", turnover),
            "_etf": market_tape._doc("etf", [{"id": "x", "text": "判定 净流入"}], "判定 净流入", {"label": "净流入"}),
            "_margin": market_tape._doc("margin", [{"id": "x", "text": "判定 加速下降"}], "判定 加速下降", {"label": "加速下降"}),
        }
        with mock.patch.multiple(market_tape, **{name: mock.Mock(return_value=doc) for name, doc in docs.items()}):
            out = market_tape.build("pulse", date(2026, 11, 5))
        self.assertIn("短期判定 在进", out["text"])
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


if __name__ == "__main__":
    unittest.main()
