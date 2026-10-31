import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.financial.data import latest_change, to_rows
from app.agents.spec import compile_spec

try:
    from app.agents.financial.graph import analyze_financials, debate_financials
except Exception:
    analyze_financials = None
    debate_financials = None

from app.tools.stock import Stock

ROOT = Path(__file__).resolve().parents[1]


class ScriptedModel:
    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()

    def complete(self, system, user):
        with self._lock:
            self.calls.append((system, user))
        if "财报事实整理员" in system:
            return {"facts": [
                {"event": "2025半年报营业收入 1.50 亿元，同比增长 50.00%", "row_id": "N1", "time": "2025半年报", "numbers": ["1.50", "50.00"]},
                {"event": "对不上的事实", "row_id": "N9", "time": "", "numbers": []},
            ]}
        if "财报看多分析员" in system:
            return {
                "sufficient": True,
                "points": [{"fact_ids": ["F1"], "claim": "收入比去年同期多", "reasoning": "记录写了同比增长", "assumption": "下一期还能保持"}],
            }
        if "财报看空分析员" in system:
            return {"sufficient": False, "note": "只有收入增长，看不出利润变差", "points": []}
        if "财报主分析员" in system:
            return {
                "verdict": {
                    "lean": "偏多",
                    "lean_level": 2,
                    "headline": "财报面偏多：半年报收入增加",
                    "why": "最近一期收入比去年同期增加",
                    "certainty": "一般",
                    "certainty_note": "还要看下一期",
                },
                "today_move": {"change": "+9.99%", "explain": "今天的涨跌不是这份财报发布当天造成的"},
                "key_points": [
                    {
                        "lean": "偏多",
                        "title": "半年报收入增加",
                        "plain": "2025半年报营业收入 1.50 亿元，同比增长 50.00%",
                        "why_it_matters": "这是最新一期的收入变化",
                        "time": "2025半年报",
                        "fact_id": "F1",
                    },
                    {
                        "lean": "偏空",
                        "title": "编造的下滑",
                        "plain": "这条对不上任何事实",
                        "why_it_matters": "应被丢掉",
                        "time": "",
                        "fact_id": "F9",
                    },
                ],
                "watch_next": [{
                    "what": "下一期财报",
                    "when": "尚未公告",
                    "if_better": "如果下一期继续好于最近一期，偏多判断会更明显",
                    "if_worse": "如果下一期明显差于最近一期，判断会转为偏空",
                }],
                "more": {"periods_read": 0, "periods_used": 0, "note": ""},
                "fine_print": "",
            }
        raise AssertionError(system)


def _sample_payload():
    return {
        "valuation": {"pct_chg": 1.2},
        "disclosure_info": [
            {"report_period": "20250630", "ann_date": "20250815"},
        ],
        "financial_statements": {
            "income": [
                {"report_period": "20250630", "total_revenue": 150000000, "net_profit_parent": 300000000, "basic_eps": 1.5},
                {"report_period": "20240630", "total_revenue": 100000000, "net_profit_parent": 200000000, "basic_eps": 1.2},
            ],
            "indicator": [
                {"report_period": "20250630", "roe": 16.7, "gross_profit_margin": 91, "net_profit_margin": 50, "debt_to_assets": 15},
            ],
            "balance": [
                {"report_period": "20250630", "total_assets": 1000000000, "total_liabilities": 150000000},
            ],
            "cashflow": [
                {"report_period": "20250630", "operate_cash_flow": 80000000},
            ],
        },
    }


class FinancialRowsTest(unittest.TestCase):
    def test_latest_period_includes_yoy_and_ratios(self):
        payload = to_rows(_sample_payload())
        latest = payload["rows"][0]
        self.assertEqual(latest["period"], "2025半年报")
        self.assertIn("营业收入 1.50 亿元，同比增长 50.00%", latest["summary"])
        self.assertIn("归母净利润 3.00 亿元，同比增长 50.00%", latest["summary"])
        self.assertIn("毛利率 91.00%", latest["summary"])
        self.assertIn("公告日 2025-08-15", latest["summary"])
        self.assertEqual(payload["pct"], "+1.20%")
        self.assertEqual(latest_change(payload), "+1.20%")
        self.assertNotIn("同比增长", payload["rows"][1]["summary"])


class FinancialGraphTest(unittest.TestCase):
    def setUp(self):
        if analyze_financials is None:
            self.skipTest("langgraph is not installed")

    def test_config_is_one_round(self):
        spec = json.loads((ROOT / "app/agents/configs/financial.json").read_text(encoding="utf-8"))
        compiled = compile_spec(spec)
        self.assertEqual(spec["source"], "financial")
        self.assertEqual(spec["rounds"], 1)
        self.assertEqual([role["id"] for role in spec["debate"]["roles"]], ["bull", "bear"])
        self.assertEqual(compiled["input"]["payload_key"], "rows")
        self.assertEqual(compiled["cache_ttl"], "financial")
        self.assertIn("财报面偏多：", compiled["chief"]["prompt"])

    def test_empty_statements_skip_the_model(self):
        model = ScriptedModel()
        stock = Stock("600519.SH", "600519", "贵州茅台", "1.600519", "SH600519")
        result = analyze_financials(stock, model=model, fetch_financials=lambda: {"rows": []})
        self.assertEqual(model.calls, [])
        self.assertFalse(result["bull"]["sufficient"])
        self.assertEqual(result["bull"]["note"], "没有可分析的财报")

    def test_debate_uses_report_facts_and_real_change(self):
        model = ScriptedModel()
        stock = Stock("600519.SH", "600519", "贵州茅台", "1.600519", "SH600519")
        result = debate_financials(stock, model=model, fetch_financials=lambda: to_rows(_sample_payload()))
        self.assertEqual(result["display"]["verdict"]["lean"], "偏多")
        self.assertTrue(result["display"]["verdict"]["headline"].startswith("财报面偏多："))
        self.assertLessEqual(len(result["display"]["verdict"]["headline"]), 25)
        self.assertEqual(result["display"]["today_move"]["change"], "+1.20%")
        self.assertEqual(len(result["display"]["key_points"]), 1)
        self.assertNotIn("fact_id", result["display"]["key_points"][0])
        self.assertEqual(result["display"]["fine_print"], "以上为基于公开财报数据的研究分析，不构成投资建议")
        self.assertEqual(result["display"]["more"]["periods_read"], 2)
        self.assertEqual(result["display"]["more"]["periods_used"], 1)
        self.assertEqual(result["internal"]["events"][1]["row_id"], "")
        self.assertEqual(result["internal"]["verdict"]["signal"], "bullish")
        extract_user = next(user for system, user in model.calls if "财报事实整理员" in system)
        bull_user = next(user for system, user in model.calls if "财报看多分析员" in system)
        chief_user = next(user for system, user in model.calls if "财报主分析员" in system)
        self.assertIn("毛利率", extract_user)
        self.assertNotIn("毛利率", bull_user)
        self.assertIn("毛利率", chief_user)
        self.assertIn("+1.20%", chief_user)


if __name__ == "__main__":
    unittest.main()
