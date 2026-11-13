import unittest
from datetime import date

from app.services import market_rules as rules
from app.services.market_page import summarize, tab_of
from app.services.market_sector import flows, heat
from app.services.market_tape import format_limits, format_valuation, monthly_funds, weekly_supply


class MoodTest(unittest.TestCase):
    def test_three_clues_all_negative_is_adverse(self):
        indexes = [{"above_ma60": False}] * 4
        signal = rules.mood_signal(indexes, {"up": 1000, "down": 4000}, {"follow": -1.2, "broken_rate": 0.45})
        self.assertEqual(signal["tier"], "逆")
        self.assertEqual(signal["score"], -3)

    def test_mixed_clues_are_flat(self):
        indexes = [{"above_ma60": False}] * 4
        signal = rules.mood_signal(indexes, {"up": 4500, "down": 800}, {"follow": -0.3, "broken_rate": 0.22})
        self.assertEqual(signal["tier"], "平")

    def test_no_data_gives_no_tier(self):
        self.assertIsNone(rules.mood_signal([], {}, {})["tier"])


class OverallTest(unittest.TestCase):
    def test_a_missing_signal_does_not_count(self):
        signals = [{"tier": "逆"}, {"tier": None}, {"tier": "平"}]
        total = rules.overall(signals)
        self.assertEqual((total["level"], total["tilt"], total["count"]), ("中性", "偏弱", 2))

    def test_two_adverse_signals_make_a_headwind(self):
        self.assertEqual(rules.overall([{"tier": "逆"}, {"tier": "逆"}])["level"], "逆风")

    def test_action_names_sectors_only_when_weak(self):
        self.assertIn("银行", rules.action_text("中性", "偏弱", ["银行"]))
        self.assertNotIn("银行", rules.action_text("中性", "偏强", ["银行"]))


class SectorLayerTest(unittest.TestCase):
    def test_four_verdicts(self):
        self.assertEqual(rules.sector_layers(8.2, 1.5, 0.03)["verdict"], "三层同向")
        self.assertEqual(rules.sector_layers(5.1, 0.9, 0.0)["verdict"], "有钱没热度")
        self.assertEqual(rules.sector_layers(-9.6, 2.1, -0.05)["verdict"], "只热不增")
        self.assertEqual(rules.sector_layers(-4.0, 0.7, -0.05)["verdict"], "在流出")

    def test_falling_leverage_blocks_the_three_layer_label(self):
        self.assertEqual(rules.sector_layers(8.2, 1.8, -0.05)["verdict"], "有钱没热度")

    def test_missing_leverage_does_not_block_it(self):
        layers = rules.sector_layers(3.0, 1.6, None)
        self.assertEqual((layers["verdict"], layers["lever_word"]), ("三层同向", "—"))

    def test_lever_band(self):
        self.assertEqual([rules.lever_word(v) for v in (0.05, 0.01, -0.03, None)], ["回升", "走平", "下降", "—"])

    def test_flow_is_share_change_times_close(self):
        shares = [{"trade_date": f"2026110{day}", "fd_share": 1000 + day * 100} for day in range(1, 6)]
        closes = [{"trade_date": f"2026110{day}", "close": 2.0} for day in range(1, 6)]
        out = flows(shares, closes)
        self.assertAlmostEqual(out[3], 3 * 100 * 2.0 / 10000)
        self.assertIsNone(out[5])
        self.assertIsNone(out[30])

    def test_heat_is_today_turnover_over_own_average(self):
        shares = [{"trade_date": "20260101", "fd_share": 1000}]
        closes = [{"trade_date": f"2026{(i // 28) + 1:02d}{i % 28 + 1:02d}", "vol": 100} for i in range(80)]
        closes[-1]["vol"] = 200
        self.assertAlmostEqual(heat(shares, closes), 200 / (sum(item["vol"] for item in closes) / 80), places=3)


class WatchActionTest(unittest.TestCase):
    def test_actions(self):
        self.assertEqual(rules.watch_action("三层同向", "bullish")[0], "顺势持有")
        self.assertEqual(rules.watch_action("三层同向", "unclear")[0], "板块有利，等个股")
        self.assertEqual(rules.watch_action("只热不增", "bullish")[0], "不追高")
        self.assertEqual(rules.watch_action(None, "bullish")[0], "看个股判断")


class MacroTest(unittest.TestCase):
    def test_direction_matches_the_framework_example(self):
        scores = {"policy": 3, "liquidity": 1, "profit": 5, "value": 5}
        out = rules.macro_direction(scores)
        self.assertEqual((out["score"], out["tier"]), (3.30, "平"))

    def test_liquidity_widening_gap_is_one(self):
        history = [{"m1": 5.0, "m2": 8.0}, {"m1": 4.0, "m2": 8.0}]
        self.assertEqual(rules.liquidity_score(history)[0], 1)

    def test_profit_needs_two_months_in_a_row(self):
        up = [{"ppi": 1.0}, {"ppi": 2.0}, {"ppi": 3.0}]
        mixed = [{"ppi": 3.0}, {"ppi": 2.0}, {"ppi": 3.0}]
        self.assertEqual((rules.profit_score(up)[0], rules.profit_score(mixed)[0]), (5, 3))

    def test_value_bands(self):
        self.assertEqual([rules.value_score(v)[0] for v in (0.9, 0.5, 0.1, None)], [5, 3, 1, 3])


class TapeExtrasTest(unittest.TestCase):
    def test_limits_follow_and_broken_rate(self):
        today = [{"limit": "U", "limit_times": 2}] * 6 + [{"limit": "Z"}] * 4 + [{"limit": "D"}]
        prev = [{"ts_code": "A", "limit": "U"}, {"ts_code": "B", "limit": "U"}, {"ts_code": "C", "limit": "D"}]
        body = format_limits(today, {"A": 3.0, "B": -1.0, "C": 5.0}, prev)
        self.assertAlmostEqual(body["metrics"]["follow"], 1.0)
        self.assertEqual(body["metrics"]["follow_count"], 2)
        self.assertAlmostEqual(body["metrics"]["broken_rate"], 0.4)

    def test_weekly_supply_buckets_by_announce_date(self):
        rows = [
            {"ann_date": "20261103", "change_vol": 1e6, "avg_price": 10, "in_de": "IN"},
            {"ann_date": "20261103", "change_vol": 2e6, "avg_price": 10, "in_de": "DE"},
        ]
        weeks = weekly_supply(rows, date(2026, 11, 4))
        self.assertEqual(len(weeks), 12)
        self.assertAlmostEqual(weeks[-1]["buy"], 0.1)
        self.assertAlmostEqual(weeks[-1]["sell"], 0.2)

    def test_monthly_funds_counts_equity_funds_only(self):
        rows = [
            {"fund_type": "股票型", "issue_date": "20261008", "issue_amount": 12.0},
            {"fund_type": "债券型", "issue_date": "20261009", "issue_amount": 50.0},
            {"fund_type": "混合型", "issue_date": "20261020", "issue_amount": None},
        ]
        months = monthly_funds(rows, date(2026, 11, 4))
        self.assertEqual(len(months), 12)
        self.assertEqual((months[-2]["count"], months[-2]["amount"]), (2, 12.0))

    def test_valuation_uses_earnings_yield_percentile(self):
        rows = [{"trade_date": f"2026{(i // 28) + 1:02d}{i % 28 + 1:02d}", "pe_ttm": 10 + i * 0.1} for i in range(90)]
        body = format_valuation(rows)
        self.assertLess(body["metrics"]["pctile"], 0.05)  # PE 最高 = 盈利收益率最低
        self.assertIn("不是 ERP", body["text"])


def _parts() -> dict:
    indexes = [
        {"name": "上证指数", "close": 3600.0, "pct": -0.8, "date": "2026-11-04", "above_ma60": False, "above_ma20": False, "prior_low": 3500.0, "broke_low": False},
        {"name": "创业板指", "close": 2000.0, "pct": -1.9, "date": "2026-11-04", "above_ma60": False, "above_ma20": False, "prior_low": 2100.0, "broke_low": True},
    ]
    return {
        "turnover": {"label": "冷清", "ratio5": -0.12, "amount": 16000, "date": "2026-11-04", "down_days": 4, "series": []},
        "etf": {"label": "基本没变", "ratio": -0.004, "buy_days": 1, "money": -9.8, "groups": {}, "series": [], "latest_date": "2026-11-03"},
        "margin": {"label": "在降", "ratio4": -0.031, "balance": 20000, "date": "2026-11-03", "series": []},
        "south": {}, "shibor": {}, "supply": {"net": -30.0, "label": "在拿走"}, "funds": {},
        "breadth": {"up": 1280, "down": 3950, "highs": 42, "lows": 318, "date": "2026-11-04"},
        "limits": {"up": 38, "down": 22, "broken": 14, "height": 4, "broken_rate": 0.41, "follow": -1.2, "follow_count": 30, "date": "2026-11-04"},
        "index": {"rows": indexes}, "sw": {}, "margin_industry": {},
        "macro": {"month": "2026年9月", "history": [{"m1": 5.0, "m2": 8.0, "ppi": 1.0}, {"m1": 4.0, "m2": 8.0, "ppi": 2.0}, {"m1": 3.0, "m2": 8.0, "ppi": 3.0}]},
        "valuation": {"pctile": 0.85}, "fx": {}, "us": {}, "hk": {},
        "ipo": {"days": []}, "calendar": {"events": [], "days": []}, "unlock": {},
        "sectors": {"rows": [
            {"name": "银行", "code": "512800.SH", "industry": "银行", "flow": {10: 8.2}, "heat": 1.5, "lever": 0.03, "lever_word": "回升", "verdict": "三层同向", "tone": "good"},
            {"name": "光伏", "code": "515790.SH", "industry": "电力设备", "flow": {10: -4.0}, "heat": 0.7, "lever": -0.05, "lever_word": "下降", "verdict": "在流出", "tone": "bad"},
        ], "date": "2026-11-03", "windows": [3, 5, 10, 20], "conclusion_days": 10},
        "watch": [], "_prev": "2026-11-03",
    }


class SummaryTest(unittest.TestCase):
    def test_weak_and_adverse_money_and_mood_make_a_headwind_without_the_team_signal(self):
        out = summarize(_parts(), date(2026, 11, 4))
        con = out["conclusion"]
        self.assertEqual((con["level"], con["count"]), ("逆风", 2))
        team = next(item for item in con["signals"] if item["key"] == "team")
        self.assertIsNone(team["tier"])
        self.assertTrue(out["team"]["pending"])
        self.assertEqual(out["macro"]["score"], 3.30)
        self.assertEqual(out["time_basis"]["quotes"], "2026-11-04")
        self.assertEqual(out["time_basis"]["funds"], "2026-11-04")
        self.assertEqual(out["time_basis"]["macro"], "2026年9月")
        self.assertIn(out["phase"], ("pre", "live", "post"))
        self.assertTrue(out["phase_label"])
        self.assertIn("phase_label", con)
        self.assertIn("unchanged_since", con)
        self.assertTrue(out["indexes"][0].get("name"))
        self.assertIn("close", out["indexes"][0])
        self.assertIn("pct", out["indexes"][0])

    def test_better_and_worse_lists_carry_current_values(self):
        out = summarize(_parts(), date(2026, 11, 4))
        better = {item["short"]: item for item in out["conclusion"]["better"]}
        worse = {item["text"]: item for item in out["conclusion"]["worse"]}
        self.assertEqual(list(better), ["宽基净流入", "成交回暖", "解开背离", "上涨过半", "趋势向上", "追涨赚钱"])
        self.assertEqual([item["short"] for item in out["conclusion"]["worse"]],
                         ["两融加速", "跌破前低", "成交冷清", "广度偏弱", "趋势向下", "追涨变难"])
        self.assertTrue(worse["主要指数收在此前 20 日最低之下"]["met"])
        self.assertIn("创业板指", worse["主要指数收在此前 20 日最低之下"]["now"])
        self.assertFalse(better["成交回暖"]["met"])  # fixture 成交冷清
        self.assertIn("text", better["宽基净流入"])

    def test_missing_money_series_keeps_the_page(self):
        parts = _parts()
        parts["turnover"] = {"label": "没有"}
        out = summarize(parts, date(2026, 11, 7))
        money = next(item for item in out["conclusion"]["signals"] if item["key"] == "money")
        self.assertIsNone(money["tier"])
        self.assertEqual(money["label"], "没有")
        self.assertEqual(out["money"]["label"], "没有")
        self.assertEqual(tab_of("fund", parts, date(2026, 11, 7))["label"], "没有")
        self.assertEqual(out["conclusion"]["count"], 1)

    def test_every_tab_builds(self):
        parts = _parts()
        for name in ("fund", "team", "sector", "mood", "cal", "macro"):
            self.assertIn("title", tab_of(name, parts, date(2026, 11, 4)))


def _row(name, ma60, ma20, ret3=0.0, ret5=0.0):
    return {"name": name, "above_ma60": ma60, "above_ma20": ma20, "ret3": ret3, "ret5": ret5}


class TrendVoteTest(unittest.TestCase):
    def test_follows_the_sixty_day_line(self):
        up = [_row(n, True, True) for n in ("上证指数", "沪深300", "中证1000", "创业板指")]
        self.assertEqual((rules.trend_vote(up)["word"], rules.trend_vote(up)["score"]), ("向上", 1))
        down = [_row(n, False, False) for n in ("上证指数", "沪深300", "中证1000", "创业板指")]
        self.assertEqual((rules.trend_vote(down)["word"], rules.trend_vote(down)["score"]), ("向下", -1))
        mixed = [_row("上证指数", True, True), _row("沪深300", True, True), _row("中证1000", False, False), _row("创业板指", False, False)]
        self.assertEqual(rules.trend_vote(mixed)["word"], "震荡")
        self.assertIsNone(rules.trend_vote([{"name": "上证指数", "above_ma60": None}]))

    def test_a_rebound_that_reclaims_the_twenty_day_line_is_not_still_falling(self):
        rebound = [_row("上证指数", False, True), _row("沪深300", False, True), _row("中证1000", False, True), _row("创业板指", False, False)]
        vote = rules.trend_vote(rebound)
        self.assertEqual((vote["word"], vote["score"], vote["kind"]), ("震荡", 0, "收复"))

    def test_a_plunge_counts_even_above_the_sixty_day_line(self):
        up = [_row("上证指数", True, True, ret3=-0.045), _row("沪深300", True, True), _row("中证1000", True, True), _row("创业板指", True, True)]
        vote = rules.trend_vote(up)
        self.assertEqual((vote["word"], vote["score"], vote["kind"]), ("向下", -1, "急跌"))
        five_day = [_row("沪深300", True, True, ret5=-0.065)] + [_row(n, True, True) for n in ("上证指数", "中证1000", "创业板指")]
        self.assertEqual(rules.trend_vote(five_day)["kind"], "急跌")
        # 创业板自己急跌不算（只看上证和沪深 300 这两个大盘）
        self.assertEqual(rules.trend_vote([_row("创业板指", True, True, ret3=-0.09)] + [_row(n, True, True) for n in ("上证指数", "沪深300", "中证1000")])["word"], "向上")

    def test_mood_signal_uses_the_vote_and_explains_it(self):
        indexes = [_row("上证指数", False, True), _row("沪深300", False, True), _row("中证1000", False, True), _row("创业板指", False, True)]
        out = rules.mood_signal(indexes, {"up": 4800, "down": 600}, {"follow": 0.8, "broken_rate": 0.2})
        self.assertEqual(out["score"], 2)
        self.assertEqual(out["tier"], "顺")                  # 震荡 0 + 普涨 +1 + 追涨 +1
        self.assertIn("已收复 20 日线", out["reasons"][0])
        plunge = [_row("上证指数", True, True, ret3=-0.05)] + [_row(n, True, True) for n in ("沪深300", "中证1000", "创业板指")]
        self.assertIn("急跌", rules.mood_signal(plunge, {}, {})["reasons"][0])


class VolumeTest(unittest.TestCase):
    PRIOR = [100.0] * 20

    def test_words(self):
        self.assertEqual(rules.volume_state(125.0, self.PRIOR, 0.5)["word"], "放量")
        self.assertEqual(rules.volume_state(75.0, self.PRIOR, 0.5)["word"], "缩量")
        self.assertEqual(rules.volume_state(100.0, self.PRIOR, 0.5)["word"], "平稳")
        self.assertEqual(rules.volume_state(75.0, self.PRIOR, 0.05)["word"], "地量")     # 近一年最低 10%
        self.assertEqual(rules.volume_state(125.0, self.PRIOR, 0.95)["word"], "天量")
        self.assertAlmostEqual(rules.volume_state(150.0, self.PRIOR, None)["ratio"], 1.5)

    def test_not_enough_data(self):
        self.assertIsNone(rules.volume_state(None, self.PRIOR, 0.5))
        self.assertIsNone(rules.volume_state(100.0, [], 0.5))


class MoodNotesTest(unittest.TestCase):
    def indexes(self, move=0.3):
        return [{"name": "上证指数", "above_ma60": False, "above_ma20": False, "pct": move}] + [
            {"name": n, "above_ma60": False, "above_ma20": False} for n in ("沪深300", "中证1000", "创业板指")]

    def test_volume_changes_how_a_drop_and_a_rebound_read(self):
        from app.services.market_page import _mood_notes

        heavy, light = {"word": "放量"}, {"word": "缩量"}
        drop = self.indexes(move=-1.5)
        self.assertIn("放量下跌", " ".join(_mood_notes(drop, {"up": 800, "down": 4500}, {}, {}, heavy)))
        self.assertIn("缩量下跌", " ".join(_mood_notes(drop, {"up": 800, "down": 4500}, {}, {}, light)))
        rebound = self.indexes()
        self.assertIn("放量反抽", " ".join(_mood_notes(rebound, {"up": 4800, "down": 600}, {}, {}, heavy)))
        self.assertIn("缩量反弹", " ".join(_mood_notes(rebound, {"up": 4800, "down": 600}, {}, {}, light)))

    def test_extreme_volume_and_ice_with_volume(self):
        from app.services.market_page import _mood_notes

        self.assertIn("地量见地价", " ".join(_mood_notes(self.indexes(), {}, {}, {}, {"word": "地量"})))
        self.assertIn("天量之后常有分歧", " ".join(_mood_notes(self.indexes(), {}, {}, {}, {"word": "天量"})))
        ice = {"stage": "冰点", "last": {"down": 30}}
        self.assertIn("缩量，抛压在衰竭", " ".join(_mood_notes(self.indexes(), {}, {}, ice, {"word": "缩量"})))
        self.assertIn("放量，恐慌还在释放", " ".join(_mood_notes(self.indexes(), {}, {}, ice, {"word": "放量"})))

    def test_theme_break_and_theme_words(self):
        from app.services.market_page import _mood_notes, _theme_view

        last = {"themes": [{"name": "半导体", "n": 19}, {"name": "贵金属", "n": 11}, {"name": "元件", "n": 7}], "theme_share": 0.32, "theme_follow": -2.0, "theme_n": 14}
        self.assertEqual(_theme_view(last)["word"], "主线在退")
        self.assertIn("半导体 19 家、贵金属 11 家、元件 7 家", _theme_view(last)["detail"])
        self.assertEqual(_theme_view({**last, "theme_follow": 1.0, "theme_share": 0.4})["word"], "有主线")
        self.assertEqual(_theme_view({**last, "theme_follow": None, "theme_share": 0.18})["word"], "没主线")
        self.assertEqual(_theme_view({**last, "theme_follow": 0.5})["word"], "一般")
        self.assertIsNone(_theme_view({"themes": []}))
        self.assertIn("主线大面", " ".join(_mood_notes(self.indexes(), {}, {}, {"last": last})))


if __name__ == "__main__":
    unittest.main()


class SectorMathTest(unittest.TestCase):
    def rows(self, shares, closes):
        days = [f"2026110{i + 1}" for i in range(len(shares))]
        return ([{"trade_date": d, "fd_share": s * 1e4} for d, s in zip(days, shares)],
                [{"trade_date": d, "close": c} for d, c in zip(days, closes)])

    def test_daily_flow_and_split_guard(self):
        from app.services import market_sector as ms

        shares, bars = self.rows([100, 110, 120], [2.0, 2.0, 2.0])
        out = ms.daily_flows(shares, bars)
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(out[0][1], (110e4 - 100e4) * 2.0 / 10000)
        # 1 拆 2：份额翻倍、价格减半，不是申购
        shares, bars = self.rows([100, 200, 205], [2.0, 1.0, 1.0])
        out = ms.daily_flows(shares, bars)
        self.assertEqual(out[0], ("20261102", 0.0))
        self.assertAlmostEqual(out[1][1], (205e4 - 200e4) * 1.0 / 10000)
        # 价格没变而份额翻倍：真的是巨额申购，要算
        shares, bars = self.rows([100, 200], [2.0, 2.0])
        self.assertGreater(ms.daily_flows(shares, bars)[0][1], 0)

    def test_money_z_compares_with_own_history(self):
        from app.services import market_sector as ms

        calm = [1.0, -1.0] * 40
        self.assertIsNone(ms.money_z([1.0] * 10))                                   # 样本不够
        self.assertGreater(ms.money_z(calm + [5.0] * 5), 1.0)                       # 近 5 日明显比平时多
        self.assertLess(ms.money_z(calm + [-5.0] * 5), -1.0)
        self.assertIsNone(ms.money_z([1.0] * 80))                                   # 平时一点波动都没有，没法比

    def test_return_amount_ratio_and_position(self):
        from app.services import market_sector as ms

        closes = [(f"d{i}", 100.0 + i) for i in range(70)]
        self.assertAlmostEqual(ms.window_return(closes, 5), 169 / 164 - 1)
        self.assertIsNone(ms.window_return(closes[:3], 5))
        self.assertEqual(ms.position(closes), 1.0)                                  # 一路新高
        self.assertAlmostEqual(ms.position([(f"d{i}", 200.0 - i) for i in range(70)]), 1 / 60)
        amounts = [(f"d{i}", 10.0) for i in range(60)] + [(f"e{i}", 20.0) for i in range(5)]
        self.assertAlmostEqual(ms.amount_ratio(amounts), 2.0)                       # 近 5 日 / 此前 60 日，不含这 5 天
        self.assertIsNone(ms.amount_ratio(amounts[:10]))

    def test_money_score_etf_uses_modified_z_on_rates(self):
        from app.services import market_sector as ms

        # 日率恒定 → MAD=0 → None
        daily = [1.0] * 80
        sizes = [100.0] * 80
        self.assertIsNone(ms.money_score_etf(daily, sizes))
        # 前面有波动，最后 5 日暴增；Mz 对照 n×M 与 MAD×√n
        daily = ([0.5, -0.5] * 35) + [8.0] * 5
        sizes = [100.0] * 75
        score = ms.money_score_etf(daily, sizes, listed=80)
        self.assertIsNotNone(score)
        self.assertGreater(score, 1.0)
        self.assertIsNone(ms.money_score_etf(daily, sizes, listed=10))               # 新上市不计钱

    def test_lever_is_relative_to_the_median_industry(self):
        from app.services import market_sector as ms

        member = {"A": "银行", "B": "煤炭", "C": "汽车"}
        now = [{"ts_code": "A", "rzye": 90.0}, {"ts_code": "B", "rzye": 100.0}, {"ts_code": "C", "rzye": 110.0}]
        then = [{"ts_code": "A", "rzye": 100.0}, {"ts_code": "B", "rzye": 100.0}, {"ts_code": "C", "rzye": 100.0}]
        out = ms.lever_by_l1(now, then, member)
        self.assertAlmostEqual(out["煤炭"], 0.0)            # 中位数行业，相对为 0
        self.assertAlmostEqual(out["银行"], 0.9 - 1)
        self.assertAlmostEqual(out["汽车"], 0.1)
        self.assertIsNone(out["钢铁"])                      # 没有数据的行业

    def test_stock_maps_to_the_most_specific_sector(self):
        from app.services import market_sector as ms

        self.assertEqual(ms.sector_for("电子", "半导体", "数字芯片设计"), "芯片")
        self.assertEqual(ms.sector_for("电子", "消费电子", "品牌消费电子"), "消费电子")
        self.assertEqual(ms.sector_for("电子", "元件", "被动元件"), "电子")                 # 没有 ETF 代表的，回到一级行业自己的板块
        self.assertEqual(ms.sector_for("食品饮料", "白酒Ⅱ", "白酒Ⅲ"), "白酒")
        self.assertEqual(ms.sector_for("食品饮料", "饮料乳品", "乳品"), "食品饮料")
        self.assertEqual(ms.sector_for("电力设备", "光伏设备", "逆变器"), "光伏")
        self.assertEqual(ms.sector_for("电力设备", "电机Ⅱ", "电机Ⅲ"), "电网设备")            # 一级行业的默认板块
        self.assertEqual(ms.sector_for("医药生物", "化学制药", "原料药"), "创新药")
        self.assertEqual(ms.sector_for("非银金融", "保险Ⅱ", "保险Ⅲ"), "保险")
        self.assertEqual(ms.sector_for("银行", "股份制银行Ⅱ", "股份制银行Ⅲ"), "银行")
        self.assertIsNone(ms.sector_for("", "", ""))

    def test_every_first_level_industry_is_covered(self):
        from app.services import market_sector as ms

        for l1, item in ms.coverage().items():
            self.assertTrue(item["etf"] or item["estimated"], l1)
        self.assertEqual(len(ms.L1_CODES), 31)
        names = [row[0] for row in ms.UNIVERSE] + [row[0] for row in ms.BASE]
        self.assertEqual(len(names), len(set(names)))


class SectorPhaseTest(unittest.TestCase):
    def phase(self, money, price, heat, pos=0.5, lever="走平", cold=False, heat_ratio=None, **extra):
        return rules.sector_phase(money, price, heat, pos, lever, mz=extra.get("mz", 1.2),
                                  price_z=extra.get("price_z", 1.2),
                                  heat_ratio=heat_ratio if heat_ratio is not None else (1.5 if heat == "热" else 1.0),
                                  rate=0.01, cold=cold)

    def test_strong_with_money_is_main(self):
        out = self.phase("进", "涨", "热")
        self.assertEqual((out["phase"], out["bucket"], out["verdict"]), ("主升", "main", "三层同向"))
        self.assertEqual(self.phase("进", "涨", "热", pos=0.9)["phase"], "主升·高位")
        self.assertEqual(self.phase("进", "涨", "热", lever="下降")["verdict"], "主升")

    def test_strong_without_money_is_waiting_or_rotation(self):
        self.assertEqual(self.phase("平", "涨", "热")["phase"], "主升·待资金")
        self.assertEqual(self.phase("出", "涨", "热", pos=0.5)["phase"], "换手上行")
        self.assertEqual(self.phase("出", "涨", "热", pos=0.9)["phase"], "派发")

    def test_slow_rise_and_no_direction(self):
        self.assertEqual(self.phase("进", "涨", "不热")["phase"], "钱先到")
        self.assertEqual(self.phase("平", "涨", "不热")["phase"], "跟涨")
        self.assertEqual(self.phase("出", "涨", "不热")["phase"], "涨势没钱")
        self.assertEqual(self.phase("进", "平", "不热")["phase"], "潜伏")
        self.assertEqual(self.phase("平", "平", "不热")["phase"], "观望")

    def test_divergence_uses_position(self):
        self.assertEqual(self.phase("进", "平", "热", pos=0.2)["phase"], "潜伏")
        self.assertEqual(self.phase("进", "平", "热", pos=0.9)["phase"], "高位分歧")
        self.assertEqual(self.phase("平", "平", "热", pos=0.9)["phase"], "高位分歧")
        self.assertEqual(self.phase("出", "平", "热", pos=0.9)["phase"], "派发")

    def test_left_side_when_falling_with_money(self):
        calm = self.phase("进", "跌", "不热")
        loud = self.phase("进", "跌", "热")
        self.assertEqual((calm["phase"], calm["bucket"]), ("逆势申购", "dip"))
        self.assertEqual(loud["phase"], "抄底拉锯")
        self.assertIn("接飞刀", calm["meaning"])

    def test_ebb_and_withdraw(self):
        out = self.phase("出", "跌", "热", pos=0.1, cold=True, heat_ratio=0.7)
        self.assertEqual((out["phase"], out["bucket"]), ("退潮", "out"))
        self.assertIn("抛压在衰竭", out["why"])
        self.assertEqual(self.phase("出", "平", "不热")["phase"], "钱在撤")

    def test_money_hysteresis_and_sticky_phase(self):
        self.assertEqual(rules.money_side(0.6, "平"), "平")
        self.assertEqual(rules.money_side(1.1, "平"), "进")
        self.assertEqual(rules.money_side(0.6, "进"), "进")          # 维持
        self.assertEqual(rules.money_side(0.4, "进"), "平")
        raw = [{"phase": "观望", "bucket": "flat"}, {"phase": "主升", "bucket": "main"}, {"phase": "主升", "bucket": "main"}]
        confirmed = rules.sticky_phases(raw)
        self.assertEqual([row["phase"] for row in confirmed], ["观望", "观望", "主升"])
        started, fading = rules.pulse_flags(["flat", "flat", "main"], phase="主升·待资金", flow=0.5)
        self.assertTrue(started)
        self.assertFalse(fading)
        # 已经钱进·热的，不进刚启动
        self.assertFalse(rules.pulse_flags(["flat", "main", "main"], mz=2.0, heat=1.5, phase="主升", flow=1.0)[0])
        self.assertTrue(rules.pulse_flags(["flat", "main", "main"], mz=0.5, heat=1.5, phase="主升·待资金", flow=0.2)[0])
        # 换手上行 / 净流入为负 / 钱出：都不算刚启动
        self.assertFalse(rules.pulse_flags(["flat", "main", "main"], mz=-1.5, heat=1.5, phase="换手上行", flow=-2.0)[0])
        self.assertFalse(rules.pulse_flags(["flat", "main", "main"], mz=-0.2, heat=1.3, phase="主升·待资金", flow=-0.3)[0])

    def test_every_bucket_has_a_title_and_tone(self):
        keys = [item[0] for item in rules.BUCKETS]
        for money, price, heat in (("进", "涨", "热"), ("进", "平", "不热"), ("进", "跌", "不热"),
                                   ("出", "涨", "热"), ("出", "跌", "热"), ("平", "平", "不热")):
            self.assertIn(self.phase(money, price, heat)["bucket"], keys)

    def test_watch_action_by_bucket(self):
        self.assertEqual(rules.watch_action("观望", "bullish", "flat")[0], "看个股判断")
        self.assertEqual(rules.watch_action("主升", "bullish", "main")[0], "顺势持有")
        self.assertEqual(rules.watch_action("主升·待资金", "bullish", "main")[0], "可持有不加仓")
        self.assertEqual(rules.watch_action("派发", "bullish", "hot_only")[0], "考虑减仓")
        self.assertEqual(rules.watch_action("三层同向", "bullish")[0], "顺势持有")  # 旧签名


class SectorPageTest(unittest.TestCase):
    def sectors(self):
        def row(name, bucket, phase, z, flow, heat=1.0, source="etf"):
            return {"name": name, "code": "x", "industry": "", "group": "科技", "source": source, "flow": {5: flow}, "flow_rate": None, "size": None,
                    "money_z": z, "ret5": 0.0, "ret20": 0.0, "ex5": 0.0, "ex20": 0.0, "heat": heat, "position": 0.5, "lever": None, "lever_word": "—",
                    "verdict": phase, "tone": "good", "phase": phase, "bucket": bucket, "why": "why", "meaning": rules.MEANING.get(phase, "")}
        return {"rows": [row("A", "flat", "观望", 0.1, 0.0), row("B", "out", "退潮", -2.0, -3.0, heat=1.0),
                         row("C", "main", "主升", 1.2, 5.0, heat=1.4), row("D", "main", "主升", 2.0, 8.0, heat=1.4),
                         row("E", "hot_only", "派发", -1.5, -1.0, heat=1.4, source="industry"),
                         row("F", "main", "主升·待资金", 0.5, 0.2, heat=1.5)], "conclusion_days": 5}

    def test_rows_are_ordered_by_bucket_then_by_money(self):
        from app.services.market_page import _sector_buckets, _sector_headline, _sector_rows

        rows = _sector_rows(self.sectors())
        self.assertEqual([row["name"] for row in rows], ["D", "C", "F", "E", "B", "A"])
        self.assertEqual(rows[0]["meaning"], "价涨又热，钱也在进：现在最强的一类。")
        self.assertEqual([item["key"] for item in _sector_buckets(rows)], ["main", "hot_only", "out", "flat"])
        headline = _sector_headline(rows)
        # 摘要与四格同口径：F 待资金热但钱不够，不进「钱进 · 热」
        self.assertIn("钱进 · 热：D、C", headline)
        self.assertIn("钱出 · 热：E", headline)
        self.assertIn("钱出 · 不热：B", headline)
        self.assertNotIn("F", headline)
        self.assertNotIn("观望", headline)

    def test_empty_and_quiet_headlines(self):
        from app.services.market_page import _sector_headline

        self.assertEqual(_sector_headline([]), "没有取到板块数据")
        self.assertIn("没有拉开差距", _sector_headline([{"name": "A", "bucket": "flat", "money_z": 0.1, "heat": 1.0}]))

    def test_sector_changes_do_not_nag_about_no_direction(self):
        from app.services import market_changes as ch

        def snap(verdicts):
            return {"day": "2026-11-05", "level": "中性", "tilt": "", "gate": "", "headline": "", "signals": {}, "etf": {}, "margin": {}, "stage": None,
                    "broke_low": [], "sectors": verdicts, "macro": {}, "team": None, "watch": {}}
        before, after = snap({"芯片": "没方向", "银行": "有钱没热度", "白酒": "有钱没热度"}), snap({"芯片": "有钱没热度", "银行": "没方向", "白酒": "三层同向"})
        titles = [item["title"] for item in ch.diff(before, after)]
        self.assertEqual(titles, ["白酒：有钱没热度 → 三层同向"])
