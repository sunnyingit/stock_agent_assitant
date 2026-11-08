import unittest
from datetime import date

from app.services.fundamental_reply import (
    format_dividend,
    format_express,
    format_forecast,
    format_mainbz,
    format_share_float,
    format_survey,
)

TODAY = date(2026, 11, 3)


class FundamentalReplyFormatTest(unittest.TestCase):
    def test_forecast_uses_wan_and_company_guidance(self):
        body = format_forecast([{
            "ann_date": "20250408", "end_date": "20250331", "type": "预增",
            "p_change_min": 24.69, "p_change_max": 52.79,
            "net_profit_min": 142000, "net_profit_max": 174000,
            "last_parent_net": 113881.78, "change_reason": "市场份额持续扩大",
        }], TODAY)
        self.assertIn("预增", body["text"])
        self.assertIn("14.20 亿元", body["text"])
        self.assertIn("17.40 亿元", body["text"])
        self.assertIn("+24.7%", body["text"])
        self.assertIn("+52.8%", body["text"])
        self.assertIn("不是一致预期", body["text"])

    def test_express_converts_yuan_and_compares_with_last_year_profit(self):
        body = format_express([{
            "ann_date": "20260121", "end_date": "20251231",
            "revenue": 92517540779.05, "n_income": 14188269426.14,
            "yoy_net_profit": 11977327023.54, "diluted_eps": 1.545, "diluted_roe": 17.29,
        }], TODAY)
        self.assertIn("营收 925.18 亿元", body["text"])
        self.assertIn("净利润 141.88 亿元", body["text"])
        self.assertIn("净利润同比 +18.5%", body["text"])
        self.assertIn("摊薄每股收益 1.545 元", body["text"])
        self.assertIn("不是正式财报", body["text"])

    def test_share_float_treats_ratio_as_percent_and_flags_one_percent(self):
        body = format_share_float([
            {"float_date": "20261130", "float_share": 228000, "float_ratio": 0.0122, "holder_name": "夏欣才", "share_type": "股权激励"},
            {"float_date": "20261215", "float_share": 28508550, "float_ratio": 4.7718, "holder_name": "甘忠如", "share_type": "公开增发"},
            {"float_date": "20270220", "float_share": 1e9, "float_ratio": 20, "holder_name": "远期", "share_type": "首发原股东"},
        ], TODAY)
        self.assertIn("占总股本 0.01%", body["text"])
        self.assertIn("22.80 万股", body["text"])
        self.assertIn("占总股本 4.77%，大额", body["text"])
        self.assertIn("2850.86 万股", body["text"])
        self.assertNotIn("远期", body["text"])
        self.assertIn("给出可以关注之前先看这些供给", body["text"])

    def test_small_unlocks_are_not_called_large(self):
        body = format_share_float([
            {"float_date": "20261120", "float_share": 100000, "float_ratio": 0.2, "holder_name": "甲"},
        ], TODAY)
        self.assertIn("不到 1%", body["text"])
        self.assertNotIn("大额解禁，单笔", body["text"])

    def test_survey_counts_dates_and_refuses_a_trade_signal(self):
        body = format_survey([
            {"surv_date": "20261020", "rece_mode": "业绩说明会", "rece_place": "电话会议", "rece_org": "与会投资者约1800人", "org_type": "--", "comp_rece": "董事会秘书 蒋理"},
            {"surv_date": "20260810", "rece_mode": "特定对象调研", "rece_org": "易方达基金", "org_type": "基金公司", "comp_rece": "董秘"},
        ], TODAY)
        self.assertIn("近 90 日机构调研 2 次", body["text"])
        self.assertIn("2026-10-20", body["text"])
        self.assertIn("不是买卖信号", body["text"])
        self.assertNotIn("2026-08-01", body["text"][:40])

    def test_mainbz_splits_product_and_region_and_skips_totals(self):
        body = format_mainbz(
            [
                {"end_date": "20260630", "bz_item": "茅台酒", "bz_sales": 800e8, "bz_profit": 720e8, "bz_cost": 80e8},
                {"end_date": "20260630", "bz_item": "合计特别调整", "bz_sales": 10e8, "bz_profit": 1e8, "bz_cost": 9e8},
                {"end_date": "20260630", "bz_item": "系列酒", "bz_sales": 200e8, "bz_profit": 100e8, "bz_cost": 100e8},
            ],
            [{"end_date": "20260630", "bz_item": "国内", "bz_sales": 900e8, "bz_profit": 700e8, "bz_cost": 200e8}],
        )
        self.assertIn("产品构成", body["text"])
        self.assertIn("茅台酒", body["text"])
        self.assertIn("占比 80.0%", body["text"])
        self.assertIn("毛利率 90.0%", body["text"])
        self.assertNotIn("合计", body["text"])
        self.assertIn("地区构成", body["text"])
        self.assertEqual(body["series"][0]["label"], "茅台酒")

    def test_dividend_uses_per_share_cash_and_counts_barren_years(self):
        paid = format_dividend([
            {"end_date": "20251231", "ann_date": "20260417", "div_proc": "预案", "cash_div": 0, "cash_div_tax": 28.02423, "stk_div": 0},
            {"end_date": "20251231", "ann_date": "20260612", "div_proc": "实施", "cash_div": 28.02423, "cash_div_tax": 28.02423, "stk_div": 0},
            {"end_date": "20241231", "ann_date": "20250520", "div_proc": "实施", "cash_div": 27.673, "cash_div_tax": 27.673, "stk_div": 0},
        ])
        self.assertIn("已实施", body_text := paid["text"])
        self.assertIn("每股派现 28.0242 元", body_text)
        self.assertIn("有现金分红的是 2 个", body_text)
        empty = format_dividend([
            {"end_date": "20251231", "ann_date": "20260417", "div_proc": "实施", "cash_div": 0, "cash_div_tax": 0, "stk_div": 0},
            {"end_date": "20241231", "ann_date": "20250401", "div_proc": "实施", "cash_div": 0, "cash_div_tax": 0, "stk_div": 0},
        ])
        self.assertIn("没有现金分红", empty["text"])
        self.assertIn("有现金分红的是 0 个", empty["text"])
        self.assertIn("没有现金回报", empty["text"])


if __name__ == "__main__":
    unittest.main()
