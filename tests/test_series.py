import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.series import (
    assemble_bars,
    assemble_flow,
    assemble_margin,
    assemble_market,
    assemble_toplist,
    choose_board,
    get_flow_daily,
)
from app.tools.stock import Stock


def _boards():
    return pd.DataFrame([
        {"ts_code": "BK0438.DC", "name": "食品饮料"},
        {"ts_code": "BK1277.DC", "name": "白酒Ⅱ"},
        {"ts_code": "BK1575.DC", "name": "白酒Ⅲ"},
        {"ts_code": "BK1279.DC", "name": "非白酒"},
    ])


class SeriesTest(unittest.TestCase):
    def test_bars_join_turnover_in_time_order(self):
        daily = pd.DataFrame([
            {"trade_date": "20261023", "open": 2, "high": 3, "low": 1, "close": 2.5, "pct_chg": 1.2, "vol": 20, "amount": 30},
            {"trade_date": "20261022", "open": 1, "high": 2, "low": 1, "close": 2, "pct_chg": -1, "vol": 10, "amount": 20},
        ])
        basic = pd.DataFrame([
            {"trade_date": "20261022", "turnover_rate": 0.8},
            {"trade_date": "20261023", "turnover_rate": 1.1},
        ])
        data = assemble_bars(daily, basic, 250, {"vol": "手", "amount": "千元"})
        self.assertEqual([item["trade_date"] for item in data["items"]], ["20261022", "20261023"])
        self.assertEqual(data["items"][0]["turnover_rate"], 0.8)
        self.assertEqual(data["items"][1]["vol"], 20)
        self.assertEqual(data["latest_trade_date"], "20261023")
        self.assertEqual(data["units"]["amount"], "千元")

    def test_flow_keeps_four_buckets(self):
        frame = pd.DataFrame([
            {"trade_date": "20261023", "net_amount": 3, "buy_elg_amount": 1, "buy_lg_amount": 1, "buy_md_amount": 1, "buy_sm_amount": -1},
            {"trade_date": "20261022", "net_amount": 2, "buy_elg_amount": 2, "buy_lg_amount": 0, "buy_md_amount": 0, "buy_sm_amount": 0},
        ])
        data = assemble_flow(frame, 250)
        self.assertEqual(data["source"], "moneyflow_dc")
        self.assertEqual(data["amount_unit"], "万元")
        self.assertEqual(data["items"][0]["trade_date"], "20261022")
        self.assertEqual(data["items"][1]["buy_sm_amount"], -1)

    def test_choose_exact_board(self):
        members = pd.DataFrame([{"ts_code": "BK0438.DC"}, {"ts_code": "BK1277.DC"}, {"ts_code": "BK1575.DC"}])
        board = choose_board("白酒Ⅱ", members, _boards())
        self.assertEqual(board["ts_code"], "BK1277.DC")
        self.assertEqual(board["matched_by"], "名称完全一致")

    def test_choose_prefix_skips_unrelated_name(self):
        members = pd.DataFrame([
            {"ts_code": "BK0438.DC"},
            {"ts_code": "BK1277.DC"},
            {"ts_code": "BK1575.DC"},
            {"ts_code": "BK1279.DC"},
        ])
        board = choose_board("白酒", members, _boards())
        self.assertEqual(board["name"], "白酒Ⅱ")
        self.assertEqual(board["matched_by"], "名称前缀")

    def test_choose_name_when_membership_missing(self):
        board = choose_board("白酒", pd.DataFrame(), _boards())
        self.assertEqual(board["name"], "白酒Ⅱ")
        self.assertIsNone(choose_board("银行", pd.DataFrame(), _boards()))

    def test_margin_ratio_uses_amount_unit(self):
        margin = pd.DataFrame([{"trade_date": "20261023", "rzye": 1000, "rzmre": 2000, "rqye": 0}])
        daily = pd.DataFrame([{"trade_date": "20261023", "amount": 10}])
        data = assemble_margin(margin, daily, 60)
        self.assertEqual(data["items"][0]["rzmre_pct"], 20)
        self.assertEqual(data["units"]["amount"], "千元")
        self.assertEqual(data["units"]["rzmre"], "元")

    def test_margin_empty_is_not_an_error_payload(self):
        data = assemble_margin(pd.DataFrame(), pd.DataFrame(), 60, "该股票暂无融资融券明细")
        self.assertEqual(data["items"], [])
        self.assertIn("暂无", data["note"])

    def test_market_turnover_adds_both_exchanges(self):
        indexes = [
            ("000300.SH", "沪深300", pd.DataFrame([{"trade_date": "20261023", "close": 4100, "pct_chg": 1.2, "amount": 100}])),
            ("000905.SH", "中证500", pd.DataFrame([{"trade_date": "20261022", "close": 5000, "pct_chg": -0.4, "amount": 80}])),
        ]
        turnover = {
            "SH_MARKET": pd.DataFrame([{"trade_date": "20261023", "amount": 6000}]),
            "SZ_MARKET": pd.DataFrame([{"trade_date": "20261023", "amount": 7000}]),
        }
        data = assemble_market(indexes, turnover, 60)
        self.assertEqual(data["turnover"]["items"][0]["sh"], 6000)
        self.assertEqual(data["turnover"]["items"][0]["sz"], 7000)
        self.assertEqual(data["turnover"]["items"][0]["total"], 13000)
        self.assertEqual(data["turnover"]["amount_unit"], "亿元")
        self.assertEqual(data["indexes"][0]["amount_unit"], "千元")
        self.assertEqual(data["latest_trade_date"], "20261023")

    def test_toplist_empty_note(self):
        data = assemble_toplist(["20261022", "20261023"], [], {})
        self.assertEqual(data["count"], 0)
        self.assertIn("未上龙虎榜", data["note"])
        self.assertEqual(data["latest_trade_date"], "20261023")

    def test_toplist_keeps_reason_and_seats(self):
        rows = [{"trade_date": "20261023", "ts_code": "600519.SH", "net_amount": 1, "reason": "日涨幅偏离值达到7%"}]
        seats = {"20261023": [{"exalter": "机构专用", "net_buy": 1, "side": "0"}]}
        data = assemble_toplist(["20261023"], rows, seats)
        self.assertEqual(data["items"][0]["reason"], "日涨幅偏离值达到7%")
        self.assertEqual(data["items"][0]["seats"][0]["exalter"], "机构专用")

    def test_hk_flow_is_refused(self):
        stock = Stock("00700.HK", "00700", "腾讯控股", "116.00700", "00700")
        with self.assertRaises(RuntimeError) as caught:
            get_flow_daily(stock)
        self.assertIn("港股", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
