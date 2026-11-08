import unittest

from app.services.sector_reply import (
    format_dc,
    format_heat,
    format_membership,
    format_position,
    format_rank,
    format_sw,
    format_tape,
    window_big,
    window_sector,
)


class SectorReplyFormatTest(unittest.TestCase):
    def test_membership_keeps_shenwan_levels_and_concepts(self):
        body = format_membership(
            {"l1_name": "汽车", "l2_name": "汽车零部件", "l3_name": "底盘与发动机系统"},
            ["人形机器人", "汽车热管理", "人形机器人"],
        )
        text = body["text"]
        self.assertIn("申万一级：汽车", text)
        self.assertIn("申万三级：底盘与发动机系统", text)
        self.assertIn("人形机器人、汽车热管理", text)
        self.assertEqual(text.count("人形机器人"), 1)

    def test_tape_hides_industry_money_when_the_board_is_a_concept(self):
        body = format_tape(
            {"name": "人形机器人", "type": "concept", "stage": "main", "days": 4, "ret_sector": 0.08, "ret_stock": 0.02, "ret_index": -0.01},
            {"ok": True, "board": "汽车零部件", "net_5d": 2e8, "net_20d": 3e8, "daily": []},
        )
        self.assertIn("主升", body["text"])
        self.assertIn("没有对应的板块资金", body["text"])
        self.assertNotIn("汽车零部件", body["text"])

    def test_tape_states_the_money_board_when_names_differ(self):
        body = format_tape(
            {"name": "汽车零部件", "type": "industry", "stage": "ebb", "ret_sector": -0.03},
            {"ok": True, "board": "汽车零部", "net_5d": -1.5e8, "net_20d": -4e8, "daily": [{"date": "2026-11-03", "net": -2e7}]},
        )
        self.assertIn("东财行业「汽车零部」", body["text"])
        self.assertIn("申万主板块是「汽车零部件」", body["text"])
        self.assertIn("-1.50 亿元", body["text"])
        self.assertIn("11-03", body["text"])

    def test_position_names_only_the_extremes_and_the_stock_flow(self):
        body = format_position({
            "name": "汽车零部件",
            "role_text": "中军",
            "mv_rank": 3,
            "mv_total": 40,
            "ret_rank": 8,
            "ret_total": 40,
            "leaders": [{"name": "拓普集团", "ret_20d": 0.12, "is_self": True}],
            "laggards": [{"name": "保隆科技", "ret_20d": -0.08}],
        }, {"ok": True, "big_5d": 25000, "big_20d": -8000, "rate_5d": 1.5, "rate_20d": -0.4, "vs_sector_5d": -2.0})
        self.assertIn("市值排名 3/40", body["text"])
        self.assertIn("拓普集团（本股）", body["text"])
        self.assertIn("保隆科技", body["text"])
        self.assertIn("+2.50 亿元", body["text"])
        self.assertIn("-2.0 个百分点", body["text"])
        self.assertNotIn("MACD", body["text"])

    def test_rank_shows_neighbors(self):
        rows = [
            {"code": "801010.SI", "name": "农林牧渔", "ret": 0.01},
            {"code": "801080.SI", "name": "电子", "ret": 0.05},
            {"code": "801880.SI", "name": "汽车", "ret": 0.03},
            {"code": "801730.SI", "name": "电力设备", "ret": -0.02},
            {"code": "801780.SI", "name": "银行", "ret": -0.04},
        ]
        body = format_rank(rows, "801880.SI")
        self.assertIn("排第 2/5", body["text"])
        self.assertIn("电子", body["text"])
        self.assertIn("汽车 +3.0%（本行业）", body["text"])

    def test_heat_intersects_members_and_skips_other_boards(self):
        body = format_heat(
            [{"name": "汽车零部件", "kind": "申万行业", "codes": ["601689.SH", "603596.SH"]}],
            {
                "limits": [
                    {"ts_code": "601689.SH", "name": "拓普集团", "limit": "U", "limit_times": 1},
                    {"ts_code": "000001.SZ", "name": "平安银行", "limit": "U", "limit_times": 2},
                ],
                "steps": [
                    {"ts_code": "000001.SZ", "name": "平安银行", "nums": "5"},
                    {"ts_code": "603596.SH", "name": "伯特利", "nums": "2"},
                ],
                "tops": [
                    {"ts_code": "601689.SH", "name": "拓普集团", "net_amount": 2.1e7, "reason": "日涨幅偏离值达到7%"},
                    {"ts_code": "000001.SZ", "name": "平安银行", "net_amount": 9e7, "reason": "换手"},
                ],
                "unavailable": [],
            },
            "601689.SH",
        )
        self.assertIn("涨停 1 只", body["text"])
        self.assertIn("本股今日涨停", body["text"])
        self.assertIn("伯特利 2 板", body["text"])
        self.assertNotIn("平安银行 5", body["text"])
        self.assertIn("拓普集团", body["text"])
        self.assertNotIn("换手", body["text"])

    def test_sw_quotes_each_shenwan_level(self):
        body = format_sw([{
            "level": "一级",
            "name": "汽车",
            "close": 3120.5,
            "pct": 1.2,
            "pe": 18.4,
            "pb": 2.15,
            "amount": 250000,
            "daily": [{"date": "2026-11-02", "pct": -0.4}, {"date": "2026-11-03", "pct": 1.2}],
            "ret_20d": -0.061,
        }])
        self.assertIn("申万一级 汽车", body["text"])
        self.assertIn("收盘 3120.50", body["text"])
        self.assertIn("今日 +1.2%", body["text"])
        self.assertIn("市盈率 18.4", body["text"])
        self.assertIn("市净率 2.15", body["text"])
        self.assertIn("成交额 25.00 亿元", body["text"])
        self.assertIn("11-03 +1.2%", body["text"])
        self.assertIn("近 20 日 -6.1%", body["text"])

    def test_dc_keeps_eastmoney_separate_from_other_boards(self):
        body = format_dc(
            [
                {"code": "BK0001.DC", "name": "汽车零部件", "idx_type": "行业板块", "pct_change": -0.4, "up_num": 20, "down_num": 80, "leading": "拓普集团", "leading_pct": 1.2},
                {"code": "BK1184.DC", "name": "人形机器人", "idx_type": "概念板块", "pct_change": 2.5, "up_num": 12, "down_num": 30, "leading": "伯特利", "leading_pct": 6.1},
            ],
            {"BK0001.DC": -0.061, "BK1184.DC": 0.08},
            [{"ts_code": "601689.SH", "name": "拓普集团", "rank": 18, "pct_change": 1.1}],
            [{"name": "伯特利", "rank": 40}],
            "601689.SH",
            [],
        )
        self.assertIn("东财行业：汽车零部件 -0.4%", body["text"])
        self.assertIn("领涨 拓普集团 +1.2%", body["text"])
        self.assertIn("东财概念：人形机器人 +2.5%", body["text"])
        self.assertIn("近 20 日：汽车零部件 -6.1%", body["text"])
        self.assertIn("人气榜第 18 名", body["text"])
        self.assertIn("伯特利第 40 名", body["text"])

    def test_windows_use_the_existing_units(self):
        big, rate = window_big([
            {"buy_elg_amount": 10000, "buy_lg_amount": 5000, "net_amount": 20000, "net_amount_rate": 10},
        ], 5)
        self.assertEqual(big, 15000)
        self.assertAlmostEqual(rate, 7.5)
        net, sector_rate = window_sector([
            {"net_amount": 1e8, "net_amount_rate": 5},
        ], 5)
        self.assertEqual(net, 1e8)
        self.assertAlmostEqual(sector_rate, 5)


if __name__ == "__main__":
    unittest.main()
