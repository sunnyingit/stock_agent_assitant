import unittest

from app.services.flow_reply import format_bar, format_chip, format_dc, format_factor, format_limit, format_margin, format_premarket, format_toplist


class FlowReplyFormatTest(unittest.TestCase):
    def test_dc_names_the_four_buckets_without_calling_them_main_force(self):
        body = format_dc([
            {"trade_date": "20261102", "net_amount": 10000, "buy_elg_amount": 8000, "buy_lg_amount": 2000, "buy_md_amount": -3000, "buy_sm_amount": -7000},
        ])
        self.assertIn("超大单 +0.80 亿元", body["text"])
        self.assertIn("大单 +0.20 亿元", body["text"])
        self.assertNotIn("主力", body["text"])

    def test_factor_reports_numbers_only(self):
        body = format_factor([{
            "trade_date": "20261102", "close": 10, "macd_dif": 0.12, "macd_dea": 0.08, "macd": 0.04,
            "rsi_6": 71.2, "rsi_12": 64.1, "rsi_24": 58.0,
            "kdj_k": 80, "kdj_d": 70, "kdj_j": 100,
            "boll_upper": 11, "boll_mid": 10, "boll_lower": 9, "cci": 120,
        }])
        self.assertIn("MACD DIF 0.12", body["text"])
        self.assertIn("RSI6 71.20", body["text"])
        self.assertIn("收盘在上下轨之间", body["text"])
        self.assertNotIn("金叉", body["text"])

    def test_chip_compares_close_with_average_cost(self):
        body = format_chip({
            "trade_date": "20261102", "weight_avg": 10, "winner_rate": 62.5,
            "cost_5pct": 8, "cost_50pct": 10, "cost_95pct": 12,
        }, close=11)
        self.assertIn("加权平均成本 10.00", body["text"])
        self.assertIn("胜率 +62.50%", body["text"])
        self.assertIn("上方 10.0%", body["text"])

    def test_bar_uses_qfq_and_moving_averages(self):
        rows = [
            {"trade_date": "20261027", "close": 9, "pct_chg": -1, "vol": 100, "ma_5": 9.2, "ma_20": 9.5},
            {"trade_date": "20261028", "close": 9.5, "pct_chg": 1, "vol": 100, "ma_5": 9.3, "ma_20": 9.5},
            {"trade_date": "20261029", "close": 9.4, "pct_chg": -0.5, "vol": 100, "ma_5": 9.3, "ma_20": 9.5},
            {"trade_date": "20261030", "close": 9.6, "pct_chg": 0.4, "vol": 100, "ma_5": 9.4, "ma_20": 9.5},
            {"trade_date": "20261102", "close": 10, "pct_chg": 2, "vol": 200, "ma_5": 9.5, "ma_20": 9.6, "turnover_rate": 3.2},
        ]
        body = format_bar(rows)
        self.assertIn("前复权收盘 10.00", body["text"])
        self.assertIn("5 日均线 9.50", body["text"])
        self.assertIn("换手率 3.20%", body["text"])
        self.assertIn("2.00 倍", body["text"])

    def test_premarket_limit_and_toplist(self):
        pre = format_premarket({"trade_date": "20261103", "total_share": 10000, "float_share": 8000, "pre_close": 10, "up_limit": 11, "down_limit": 9})
        self.assertIn("涨停价 11.00", pre["text"])
        self.assertIn("跌停价 9.00", pre["text"])
        empty = format_limit([])
        self.assertIn("没有涨停、跌停或炸板", empty["text"])
        hit = format_limit([{"trade_date": "20261102", "limit": "U", "limit_times": 2, "open_times": 1}])
        self.assertIn("涨停", hit["text"])
        self.assertIn("连板 2", hit["text"])
        board = format_toplist([{"trade_date": "20261102", "net_amount": 2.1e7, "reason": "日涨幅偏离值达到7%"}])
        self.assertIn("+0.21 亿元", board["text"])
        self.assertIn("日涨幅偏离值达到7%", board["text"])

    def test_margin_is_balance_buy_and_net_buy(self):
        body = format_margin([
            {"trade_date": "20261102", "rzye": 5e8, "rzmre": 1.2e8, "rzche": 0.4e8},
        ])
        self.assertIn("融资余额 +5.00 亿元", body["text"])
        self.assertIn("融资买入额 +1.20 亿元", body["text"])
        self.assertIn("融资净买入 +0.80 亿元", body["text"])


if __name__ == "__main__":
    unittest.main()
