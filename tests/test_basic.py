import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app
from app.services.basic import assemble_basic
from app.tools.stock import Stock


def _bars(count=260):
    rows = []
    start = datetime(2024, 1, 1)
    for offset in range(count):
        rows.append({
            "trade_date": (start + timedelta(days=offset)).strftime("%Y%m%d"),
            "high": 50.0,
            "low": 49.0,
            "close": 49.5,
            "pre_close": 49.5,
            "change": 0.0,
            "pct_chg": 0.0,
        })
    return rows


class BasicAssemblyTest(unittest.TestCase):
    def test_market_cap_and_52w_window(self):
        rows = _bars()
        rows[0]["high"] = 200
        rows[0]["low"] = 1
        rows[10]["high"] = 86.88
        rows[20]["low"] = 44
        rows[-1].update({"close": 51.1, "pre_close": 50.31, "change": 0.79, "pct_chg": 1.5703})
        basic = pd.DataFrame([{
            "pe_ttm": 32.1123,
            "pb": 3.6116,
            "dv_ttm": 0.9589,
            "dv_ratio": 1.0157,
            "turnover_rate": 1.6543,
            "total_mv": 8880339.916,
        }])
        indicator = pd.DataFrame([
            {"end_date": "20260331", "ann_date": "20260430", "roe": 2.2668, "update_flag": "1"},
            {"end_date": "20260630", "ann_date": "20260828", "roe": 4.20, "update_flag": "0"},
            {"end_date": "20260630", "ann_date": "20260828", "roe": 4.2328, "update_flag": "1"},
        ])

        data = assemble_basic(pd.DataFrame(rows), basic, indicator, ts_code="601689.SH")

        self.assertEqual(data["price"], 51.1)
        self.assertEqual(data["change"], 0.79)
        self.assertEqual(data["change_pct"], 1.57)
        self.assertEqual(data["pe_ttm"], 32.11)
        self.assertEqual(data["pb"], 3.61)
        self.assertEqual(data["dividend_yield"], 0.96)
        self.assertEqual(data["turnover_rate"], 1.65)
        self.assertEqual(data["total_mv_yi"], 888.03)
        self.assertEqual(data["high_52w"], 86.88)
        self.assertEqual(data["low_52w"], 44.0)
        self.assertEqual(data["roe"], 4.23)
        self.assertEqual(data["roe_period"], "20260630")
        self.assertEqual(data["trade_date"], rows[-1]["trade_date"])

    def test_missing_fields_are_null_and_high_low_fall_back_to_close(self):
        daily = pd.DataFrame([
            {"trade_date": "20261024", "high": None, "low": 10, "close": 12, "change": 1, "pct_chg": 1, "pre_close": 11},
            {"trade_date": "20261025", "high": 11, "low": None, "close": 9, "change": None, "pct_chg": None, "pre_close": 10},
        ])
        data = assemble_basic(daily, pd.DataFrame([{"total_mv": 10000, "dv_ratio": 1.2}]))
        self.assertEqual(data["high_52w"], 12)
        self.assertEqual(data["low_52w"], 9)
        self.assertEqual(data["total_mv_yi"], 1.0)
        self.assertEqual(data["dividend_yield"], 1.2)
        self.assertEqual(data["change"], -1.0)
        self.assertEqual(data["change_pct"], -10.0)
        self.assertIsNone(data["pe_ttm"])
        self.assertIsNone(data["pb"])
        self.assertIsNone(data["roe"])
        self.assertIsNone(data["turnover_rate"])

    def test_missing_price_raises(self):
        daily = pd.DataFrame([{"trade_date": "20261026", "close": None}])
        with self.assertRaises(RuntimeError) as caught:
            assemble_basic(daily, ts_code="601689.SH")
        self.assertIn("最新价格", str(caught.exception))


class BasicRouteTest(unittest.TestCase):
    @patch("app.services.basic.get_basic", return_value={"price": 51.1, "total_mv_yi": 888.03})
    @patch("app.api.stocks.resolve_stock")
    def test_get_basic_route(self, resolve, _get_basic):
        resolve.return_value = Stock(
            ts_code="601689.SH",
            code="601689",
            name="拓普集团",
            quote_id="1.601689",
            em_code="SH601689",
            industry="汽车配件",
        )
        response = create_app().test_client().get("/api/stocks/basic?q=拓普集团")
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["ts_code"], "601689.SH")
        self.assertEqual(body["stock_name"], "拓普集团")
        self.assertEqual(body["data"]["price"], 51.1)
        self.assertEqual(body["data"]["total_mv_yi"], 888.03)


if __name__ == "__main__":
    unittest.main()
