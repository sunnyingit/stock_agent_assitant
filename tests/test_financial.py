import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.financial import summarize_financials, days_gap, format_yi, report_desc


class FinancialShapeTest(unittest.TestCase):
    def test_report_desc(self):
        self.assertEqual(report_desc("20251231"), "2025年报")
        self.assertEqual(report_desc("20250630"), "2025半年报")

    def test_format_yi(self):
        self.assertEqual(format_yi(92278072083.21), 922.78)
        self.assertIsNone(format_yi(None))

    def test_days_gap(self):
        self.assertEqual(days_gap("20250630", "20250815"), 46)

    def test_analysis_uses_latest_indicator(self):
        analysis = summarize_financials(
            {
                "income": [
                    {"report_period": "20241231", "total_revenue": 100000000, "net_profit_parent": 200000000, "basic_eps": 1.2},
                    {"report_period": "20250630", "total_revenue": 150000000, "net_profit_parent": 300000000, "basic_eps": 1.5},
                ],
                "indicator": [
                    {"report_period": "20241231", "roe": 10, "gross_profit_margin": 40, "net_profit_margin": 20, "debt_to_assets": 30, "current_ratio": 1.1, "eps": 1.2, "roa": 5},
                    {"report_period": "20250630", "roe": 16.7, "gross_profit_margin": 91, "net_profit_margin": 50, "debt_to_assets": 15, "current_ratio": 2.0, "eps": 35.5, "roa": 8},
                ],
            },
            [{"report_period": "20250630", "report_desc": "2025半年报", "ann_date": "20250815"}],
        )
        self.assertEqual(analysis["key_metrics"]["roe"], 16.7)
        self.assertEqual(analysis["financial_trend"]["revenue_trend"][-1]["revenue_yi"], 1.5)
        self.assertEqual(analysis["disclosure_summary"][0]["days_from_period_end"], 46)


if __name__ == "__main__":
    unittest.main()
