import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from app.services.moneyflow import summarize_moneyflow


class PeriodMoneyflowTest(unittest.TestCase):
    def test_three_day_net_inflow(self):
        frame = pd.DataFrame([
            {"trade_date": "20261024", "pct_change": 1.2, "close": 10.5, "net_amount": 20000, "net_amount_rate": 1.1,
             "buy_elg_amount": 8000, "buy_lg_amount": 7000, "buy_md_amount": 3000, "buy_sm_amount": 2000},
            {"trade_date": "20261023", "pct_change": -0.4, "close": 10.2, "net_amount": -5000, "net_amount_rate": -0.3,
             "buy_elg_amount": -1000, "buy_lg_amount": -2000, "buy_md_amount": -1000, "buy_sm_amount": -1000},
            {"trade_date": "20261022", "pct_change": 0.5, "close": 10.0, "net_amount": 10000, "net_amount_rate": 0.6,
             "buy_elg_amount": 4000, "buy_lg_amount": 3000, "buy_md_amount": 2000, "buy_sm_amount": 1000},
        ])
        result = summarize_moneyflow(frame, 3)
        self.assertEqual(result["data_days"], 3)
        self.assertEqual(result["start_date"], "20261022")
        self.assertEqual(result["end_date"], "20261024")
        self.assertEqual(result["moneyflow_summary"]["total_net_amount"], 25000)
        self.assertEqual(result["moneyflow_summary"]["total_net_amount_yi"], 2.5)
        self.assertEqual(result["moneyflow_summary"]["net_flow_trend"], "净流入")
        self.assertEqual(result["moneyflow_summary"]["positive_days"], 2)
        self.assertEqual(result["capital_type"]["super_large_order"]["total_amount"], 11000)
        self.assertEqual(result["price_performance"]["total_pct_change"], 1.3)
        self.assertEqual(len(result["daily_data"]), 3)


if __name__ == "__main__":
    unittest.main()
