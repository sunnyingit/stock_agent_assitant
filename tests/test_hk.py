import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.financial.data import to_rows
from app.services.hk import as_daily_basic, statements, yuan_to_wan
from app.services.moneyflow import get_moneyflow
from app.services.news import _needles
from app.tools.stock import Stock


class HongKongFinancialTest(unittest.TestCase):
    def test_indicator_becomes_statement_rows_and_market_cap_in_wan(self):
        frame = pd.DataFrame([{
            "end_date": "20241231",
            "operate_income": 6.52498e11,
            "holder_profit": 1.94073e11,
            "basic_eps": 20.94,
            "roe_avg": 22.5,
            "gross_profit_ratio": 53.0,
            "pe_ttm": 18.2,
            "pb_ttm": 3.4,
            "dividend_rate": 0.8,
            "total_market_cap": 3.6e12,
            "total_assets": 1.78e12,
            "netcash_operate": 2.0e11,
        }])
        report, _disclosure = statements(frame)
        self.assertEqual(report["income"][0]["total_revenue"], 6.52498e11)
        self.assertEqual(report["income"][0]["net_profit_parent"], 1.94073e11)
        self.assertEqual(report["indicator"][0]["roe"], 22.5)
        card = as_daily_basic(frame)
        self.assertEqual(card.iloc[0]["pe_ttm"], 18.2)
        self.assertEqual(yuan_to_wan(3.6e12), 3.6e8)

    def test_report_text_uses_hong_kong_dollars(self):
        payload = to_rows({
            "currency": "HKD",
            "valuation": {"pct_chg": -0.4},
            "financial_statements": {
                "income": [{"report_period": "20241231", "total_revenue": 6.52498e11, "net_profit_parent": 1.94073e11}],
            },
        })
        self.assertIn("亿港元", payload["rows"][0]["summary"])
        self.assertEqual(payload["pct"], "-0.40%")

    def test_moneyflow_refuses_hong_kong(self):
        stock = Stock("00700.HK", "00700", "腾讯控股", "116.00700", "00700")
        with self.assertRaises(RuntimeError) as caught:
            get_moneyflow(stock)
        self.assertIn("暂无", str(caught.exception))

    def test_news_needles_do_not_add_mainland_prefixes(self):
        stock = Stock("00700.HK", "00700", "腾讯控股", "116.00700", "00700")
        needles = _needles(stock)
        self.assertIn("腾讯控股", needles)
        self.assertIn("00700.HK", needles)
        self.assertFalse(any(item.startswith(("SH", "SZ", "BJ")) for item in needles))


if __name__ == "__main__":
    unittest.main()
