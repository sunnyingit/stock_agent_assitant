import unittest
from datetime import datetime

from app.services import market_rules as rules
from app.services import market_team as team
from app.services.market_emotion import stats
from app.services.market_etf import flag, series_of


def _days(n: int) -> list[str]:
    return [f"2026{(i // 28) + 1:02d}{i % 28 + 1:02d}" for i in range(n)]


def _flows(days: int = 80, jump_at: int | None = None, jump: float = 100.0):
    """合计净申购（亿元）：平时每天 +1 ± 0.5 上下摆，jump_at 那天一次性多 jump。"""
    out = []
    for i, day in enumerate(_days(days)):
        out.append((day, 1 + (0.5 if i % 2 else -0.5) + (jump if i == jump_at else 0)))
    return out


def _panel(n: int = 90, jump_at: int = 80, jump: float = 300.0, move: float = -2.0, names: int = 6):
    """names 只 ETF，平时每天份额 ±1，jump_at 那天一起多 jump。价格按 1 元算，金额 = 份额变化 / 1e4。"""
    days = _days(n)
    codes = [f"E{i}" for i in range(names)]
    deltas = {code: {} for code in codes}
    for code in codes:
        for i in range(1, n):
            deltas[code][days[i]] = jump if i == jump_at else (1.0 if i % 2 else -1.0)
    pct = {day: (move if i == jump_at else 0.3) for i, day in enumerate(days)}
    money = {code: {day: value / 1e4 for day, value in series.items()} for code, series in deltas.items()}
    return days, deltas, pct, money


def _found(days, deltas, pct, money, blue=None):
    return team.detect_actions(days, deltas, pct, money, blue if blue is not None else tuple(deltas))


class DetectTest(unittest.TestCase):
    def test_big_inflow_on_a_big_down_day_is_an_action(self):
        days, deltas, pct, money = _panel(n=120, jump_at=110)
        found = _found(days, deltas, pct, money)
        self.assertEqual([item["day"] for item in found], [days[110]])
        self.assertGreater(found[0]["signal"], found[0]["limit"])
        self.assertEqual((found[0]["weak"], found[0]["rank"], found[0]["positive"], found[0]["names"]), ("大跌", 1, 6, 6))
        self.assertAlmostEqual(found[0]["blue_share"], 1.0)

    def test_the_same_inflow_on_a_strong_day_is_not(self):
        days, deltas, pct, money = _panel(n=120, jump_at=110, move=2.0)
        self.assertEqual(_found(days, deltas, pct, money), [])

    def test_a_flat_day_is_not_a_down_day(self):
        days, deltas, pct, money = _panel(n=120, jump_at=110, move=0.0)
        self.assertEqual(_found(days, deltas, pct, money), [])

    def test_a_mild_dip_is_not_enough_unless_the_buying_is_extreme_or_the_slump_is_long(self):
        days, deltas, pct, money = _panel(n=120, jump_at=110, move=-0.6)
        # 跌 0.6%、前两天没跌：不够明显；但买入是近 60 天最猛的、异常度又很高，放宽
        self.assertEqual([item["weak"] for item in _found(days, deltas, pct, money)], ["极猛"])
        # 买得不算极猛（异常度不到 3）：不放宽
        days, deltas, pct, money = _panel(n=120, jump_at=110, jump=2.5, move=-0.6)
        self.assertEqual(_found(days, deltas, pct, money), [])
        # 连续下跌：近 3 日累计跌超 2%，当天跌 0.6% 也算
        days, deltas, pct, money = _panel(n=120, jump_at=110, jump=2.5, move=-0.6)
        pct[days[109]] = pct[days[108]] = -1.0
        self.assertEqual([item["weak"] for item in _found(days, deltas, pct, money)], ["连跌"])

    def test_tiers_of_weakness(self):
        self.assertEqual(team.weak_kind(-1.2, 0.0, -1.2, False, 0.0), "大跌")
        self.assertEqual(team.weak_kind(-0.6, -2.5, -1.0, False, 0.0), "连跌")
        self.assertEqual(team.weak_kind(-0.6, -1.0, -0.6, True, 3.2), "极猛")
        self.assertIsNone(team.weak_kind(-0.6, -1.0, -0.6, True, 2.9))
        self.assertIsNone(team.weak_kind(-0.4, -0.9, -0.5, True, 9.0))
        self.assertIsNone(team.weak_kind(0.5, 0.3, 0.1, True, 9.0))

    def test_a_rising_day_after_pressure_still_counts_but_a_rising_day_alone_does_not(self):
        # 护盘买进去以后指数当天可能已经拉红：前两天刚跌过，今天涨 0.9% 也算
        days, deltas, pct, money = _panel(n=120, jump_at=110, move=0.9)
        pct[days[109]] = -1.4
        found = _found(days, deltas, pct, money)
        self.assertEqual([(item["day"], item["weak"]) for item in found], [(days[110], "承接")])
        self.assertAlmostEqual(found[0]["worst3"], -1.4)
        # 同样的买入，前面没有承压：不算
        days, deltas, pct, money = _panel(n=120, jump_at=110, move=0.9)
        self.assertEqual(_found(days, deltas, pct, money), [])

    def test_consecutive_days_are_numbered(self):
        days, deltas, pct, money = _panel(n=120, jump_at=110, move=-2.0)
        for code in deltas:
            deltas[code][days[111]] = 300.0
            money[code][days[111]] = 300.0 / 1e4
        pct[days[111]] = 0.5
        found = _found(days, deltas, pct, money)
        self.assertEqual([(item["day"], item["streak"]) for item in found], [(days[110], 1), (days[111], 2)])

    def test_buying_must_be_broad(self):
        days, deltas, pct, money = _panel(n=120, jump_at=110)
        for code in list(deltas)[2:]:      # 6 只里只有 2 只买，其余在赎回
            deltas[code][days[110]] = -50.0
            money[code][days[110]] = -50.0 / 1e4
        self.assertEqual(_found(days, deltas, pct, money), [])

    def test_buying_must_be_big_in_money_not_just_unusual_for_each_etf(self):
        days, deltas, pct, money = _panel(n=120, jump_at=110)
        for code in money:     # 份额变化还是异常的，但这些 ETF 平时每天的金额就很大，当天合计没超过平时的波动
            for i, day in enumerate(days[1:110], 1):
                money[code][day] = 5.0 if i % 2 else -5.0
        self.assertEqual(_found(days, deltas, pct, money), [])

    def test_money_must_flow_to_broad_blue_chips(self):
        days, deltas, pct, money = _panel(n=120, jump_at=110)
        self.assertEqual(_found(days, deltas, pct, money, blue=("E0", "E1")), [])          # 蓝筹只占 1/3
        self.assertEqual(len(_found(days, deltas, pct, money, blue=("E0", "E1", "E2", "E3"))), 1)

    def test_one_giant_etf_does_not_set_the_median(self):
        days, deltas, pct, money = _panel(n=120, jump_at=110, jump=0.0)
        deltas["E0"][days[110]] = 300.0
        money["E0"][days[110]] = 300.0 / 1e4
        self.assertEqual(_found(days, deltas, pct, money), [])

    def test_a_missing_day_is_skipped_not_merged(self):
        days, deltas, pct, money = _panel(n=120, jump_at=110)
        for code in deltas:
            deltas[code].pop(days[110], None)
            money[code].pop(days[110], None)
        self.assertEqual(_found(days, deltas, pct, money), [])

    def test_needs_enough_ordinary_days(self):
        days, deltas, pct, money = _panel(n=30, jump_at=25)
        self.assertEqual(_found(days, deltas, pct, money), [])

    def test_strength_fades_to_zero_on_the_fifth_day(self):
        self.assertEqual(team.strength_of(4.0, 0), 4.0)
        self.assertAlmostEqual(team.strength_of(4.0, 2), 2.4)
        self.assertEqual(team.strength_of(4.0, 5), 0.0)

    def test_standardized_drawdown_is_the_median_across_names(self):
        calendar = ["d0", "d1", "d2"]
        shares = {"d0": {"A": 0.0, "B": 0.0}, "d1": {"A": 10.0, "B": 10.0}, "d2": {"A": 3.0, "B": 10.0}}
        action = {"pos": 1, "spikes": {"A": 10.0, "B": 10.0}, "stdevs": {"A": 1.0, "B": 1.0}}
        self.assertAlmostEqual(team.standardized_gone(calendar, shares, action), 0.35)

    def test_daily_flow_is_share_change_times_close(self):
        cal = ["20260101", "20260102", "20260105"]
        shares = {d: {"A": 100.0 + 10 * i, "B": 200.0} for i, d in enumerate(cal)}   # 万份
        closes = {d: {"A": 2.0, "B": 5.0} for d in cal}
        flows = team.daily_flows(cal, shares, closes, ["A", "B"])
        self.assertEqual(flows[0], ("20260101", None))
        self.assertAlmostEqual(flows[1][1], 10 * 2.0 / 1e4)   # 万份 × 元 → 万元 → 亿元
        del shares["20260105"]["B"]
        self.assertIsNone(team.daily_flows(cal, shares, closes, ["A", "B"])[2][1])

    def test_low_priced_etf_does_not_count_as_much_as_a_dear_one(self):
        cal = ["20260101", "20260102"]
        shares = {"20260101": {"cheap": 1000.0, "dear": 1000.0}, "20260102": {"cheap": 2000.0, "dear": 1000.0}}
        closes = {d: {"cheap": 1.0, "dear": 5.0} for d in cal}
        cheap_buys = team.daily_flows(cal, shares, closes, ["cheap", "dear"])[1][1]
        shares["20260102"] = {"cheap": 1000.0, "dear": 2000.0}
        dear_buys = team.daily_flows(cal, shares, closes, ["cheap", "dear"])[1][1]
        self.assertAlmostEqual(dear_buys / cheap_buys, 5.0)

    def test_label_uses_recency_and_the_standardized_drawdown(self):
        self.assertEqual(team.label_of(1, {"sold": False, "days": 1}), "在出手")
        self.assertEqual(team.label_of(8, {"sold": False, "days": 8}), "没出手")
        self.assertEqual(team.label_of(8, {"sold": True, "days": 8}), "可能在卖")

    def test_history_waits_for_twenty_days(self):
        days = _days(80)
        pct = {day: 1.0 for day in days}
        actions = [{"day": days[10], "delta": 50.0, "index_pct": -2.0}, {"day": days[70], "delta": 60.0, "index_pct": -1.5}]
        rows = team.history(actions, days, pct)
        self.assertAlmostEqual(rows[0]["ret20"], 1.01 ** 20 - 1)
        self.assertEqual((rows[1]["ret20"], rows[1]["progress"]), (None, 9))


def _sell_panel(redeem_days: tuple[int, ...] = (110, 111), jump: float = -300.0, move: float = 0.2, n: int = 120, names: int = 6):
    days, deltas, pct, money = _panel(n=n, jump_at=-1, move=move, names=names)
    for index in redeem_days:
        for code in deltas:
            deltas[code][days[index]] = jump
            money[code][days[index]] = jump / 1e4
    return days, deltas, pct, money


class SellTest(unittest.TestCase):
    def sells(self, days, deltas, pct, money, blue=None):
        return team.detect_sells(days, deltas, pct, money, blue if blue is not None else tuple(deltas))

    def test_two_big_broad_redemption_days_out_of_three_is_a_sell(self):
        days, deltas, pct, money = _sell_panel()
        found = self.sells(days, deltas, pct, money)
        self.assertEqual([item["day"] for item in found], [days[111]])     # 第二天凑够 3 天里 2 天
        self.assertEqual((found[0]["days"], found[0]["negative"], found[0]["names"]), (2, 6, 6))
        self.assertLess(found[0]["delta"], 0)
        self.assertAlmostEqual(found[0]["blue_share"], 1.0)

    def test_one_day_is_not_enough(self):
        days, deltas, pct, money = _sell_panel(redeem_days=(110,))
        self.assertEqual(self.sells(days, deltas, pct, money), [])

    def test_two_days_far_apart_do_not_count(self):
        days, deltas, pct, money = _sell_panel(redeem_days=(105, 111))
        self.assertEqual(self.sells(days, deltas, pct, money), [])

    def test_panic_day_redemptions_are_retail_not_the_national_team(self):
        days, deltas, pct, money = _sell_panel()
        pct[days[110]] = pct[days[111]] = -1.8
        self.assertEqual(self.sells(days, deltas, pct, money), [])
        pct[days[110]] = -1.8
        pct[days[111]] = 0.3        # 只有一天是恐慌日，另一天不够 2 天
        self.assertEqual(self.sells(days, deltas, pct, money), [])

    def test_redemption_can_happen_on_a_rising_or_flat_day(self):
        for move in (1.5, 0.0, -0.4):
            days, deltas, pct, money = _sell_panel(move=move)
            self.assertEqual(len(self.sells(days, deltas, pct, money)), 1, move)

    def test_needs_to_be_broad_and_blue_chip(self):
        days, deltas, pct, money = _sell_panel()
        for index in (110, 111):
            for code in list(deltas)[:3]:           # 6 只里 3 只在买，不够广
                deltas[code][days[index]] = 300.0
                money[code][days[index]] = 300.0 / 1e4
        self.assertEqual(self.sells(days, deltas, pct, money), [])
        days, deltas, pct, money = _sell_panel()
        self.assertEqual(self.sells(days, deltas, pct, money, blue=("E0",)), [])     # 蓝筹只占 1/6

    def test_label_prefers_the_more_recent_side(self):
        self.assertEqual(team.label_of(1, None, 3), "在出手")
        self.assertEqual(team.label_of(3, None, 1), "可能在卖")
        self.assertEqual(team.label_of(None, None, 2), "可能在卖")
        self.assertEqual(team.label_of(7, None, 6), "没出手")
        self.assertEqual(team.label_of(7, {"sold": True, "days": 7}, None), "可能在卖")


class SummarizeTest(unittest.TestCase):
    @staticmethod
    def _market(n: int = 90, spike_at: int = 80, skip_days_before_spike: int = 0):
        """每只 ETF 平时每天份额 +1±1，spike_at 那天一次多 300（万份）；日历在 spike_at 前可以隔一个长假。"""
        import datetime as dt

        day, days = dt.date(2026, 1, 5), []
        for i in range(n):
            if i == spike_at:
                day += dt.timedelta(days=skip_days_before_spike)
            while day.weekday() >= 5:
                day += dt.timedelta(days=1)
            days.append(day.strftime("%Y%m%d"))
            day += dt.timedelta(days=1)
        shares, closes = {}, {}
        for code, _name in team.TEAM_BOOK:
            level, rows, bars = 10000.0, [], []
            for i, d in enumerate(days):
                level += 300 if i == spike_at else (1 if i % 2 else -1)
                rows.append({"trade_date": d, "fd_share": level})
                bars.append({"trade_date": d, "close": 2.0})
            shares[code], closes[code] = rows, bars
        pct = {d: (-2.0 if i == spike_at else 0.3) for i, d in enumerate(days)}
        return shares, closes, pct, days

    def test_finds_the_action_and_labels_it(self):
        shares, closes, pct, days = self._market(n=120, spike_at=115)
        out = team.summarize(shares, closes, pct)
        self.assertTrue(out["ok"])
        self.assertEqual(out["last"]["day"], f"{days[115][:4]}-{days[115][4:6]}-{days[115][6:]}")
        self.assertEqual(out["label"], "在出手")
        self.assertAlmostEqual(out["last"]["delta"], 9 * 300 * 2.0 / 1e4, places=3)   # 亿元

    def test_first_day_after_a_long_holiday_still_counts(self):
        shares, closes, pct, days = self._market(n=120, spike_at=110, skip_days_before_spike=9)
        gap = (datetime.strptime(days[110], "%Y%m%d") - datetime.strptime(days[109], "%Y%m%d")).days
        self.assertGreater(gap, 4)
        out = team.summarize(shares, closes, pct)
        self.assertEqual([a["day"][5:].replace("-", "") for a in out["actions"]], [days[110][4:]])

    def test_a_missing_etf_day_drops_that_day_instead_of_merging_two(self):
        shares, closes, pct, days = self._market(n=120, spike_at=115)
        shares["510300.SH"] = [row for row in shares["510300.SH"] if row["trade_date"] != days[114]]
        out = team.summarize(shares, closes, pct)
        self.assertTrue(out["ok"])
        self.assertEqual(out["last"]["day"], f"{days[115][:4]}-{days[115][4:6]}-{days[115][6:]}")   # 缺一只不影响其余几只的相邻日变化

    def test_not_enough_history(self):
        shares, closes, pct, _days_ = self._market(n=30, spike_at=25)
        self.assertFalse(team.summarize(shares, closes, pct)["ok"])


class AnnounceTest(unittest.TestCase):
    NOW = datetime(2026, 11, 4, 12, 0)

    def test_needs_an_official_subject_and_an_action(self):
        items = [
            {"title": "央行：加力实施股票回购增持再贷款政策，下调利率至1%", "time": "2026-11-04 08:00:00", "url": "u1"},
            {"title": "中国银河证券张迪：优化回购增持再贷款有助于提振信心", "time": "2026-11-04 09:00:00"},
            {"title": "央行行长谈经济形势", "time": "2026-11-03 09:00:00"},
            {"title": "汇金增持宽基ETF", "time": "2026-10-01 09:00:00"},
        ]
        rows = team.official_items(items, self.NOW)
        self.assertEqual([row["who"] for row in rows], ["央行"])
        self.assertTrue(rows[0]["strong"])

    def test_policy_hit_needs_a_strong_title_within_seven_days(self):
        old = [{"date": "2026-10-20", "title": "x", "strong": True}]
        weak = [{"date": "2026-11-03", "title": "x", "strong": False}]
        fresh = [{"date": "2026-11-02", "title": "x", "strong": True}]
        self.assertIsNone(team.policy_hit(old, self.NOW))
        self.assertIsNone(team.policy_hit(weak, self.NOW))
        self.assertEqual(team.policy_hit(fresh, self.NOW)["date"], "2026-11-02")


class EmotionTest(unittest.TestCase):
    @staticmethod
    def walk(days: list[dict]) -> list[str]:
        """按天走一遍，像 market_emotion.stats 那样把前一天的 stage 带进下一天。"""
        out = []
        for day in days:
            out.append(dict(day))
            out[-1]["stage"] = rules.emotion_stage(out)
        return [row["stage"] for row in out]

    def test_stage_rules(self):
        ice = {"up": 30, "down": 40, "follow": -1.0}
        ebb = {"up": 60, "down": 8, "follow": -0.5}
        peak = {"up": 110, "down": 2, "follow": 3.0, "broken_rate": 0.2}
        ferment = {"up": 70, "down": 6, "follow": 1.5}
        calm = {"up": 30, "down": 8, "follow": 0.2}
        self.assertEqual(self.walk([ferment, ice]), ["发酵", "冰点"])
        self.assertEqual(self.walk([ferment, ebb]), ["发酵", "退潮"])
        self.assertEqual(self.walk([ferment, peak]), ["发酵", "高潮"])
        self.assertEqual(self.walk([calm]), ["修复"])

    def test_a_crash_day_is_not_averaged_away(self):
        good = {"up": 90, "down": 2, "follow": 2.0}
        crash = {"up": 36, "down": 118, "follow": -2.5}
        self.assertEqual(self.walk([good, good, crash])[-1], "冰点")

    def test_stages_follow_the_cycle(self):
        ice = {"up": 40, "down": 60, "follow": -1.5}
        rebound = {"up": 120, "down": 5, "follow": 3.0, "broken_rate": 0.15}
        # 冰点后第一个强日也只算修复，下一天还强才是发酵；高潮要先发酵
        self.assertEqual(self.walk([ice, rebound, rebound, rebound]), ["冰点", "修复", "发酵", "高潮"])

    def test_ice_stays_until_limit_up_stocks_make_money_again(self):
        ice = {"up": 40, "down": 60, "follow": -1.5}
        still_losing = {"up": 50, "down": 10, "follow": -0.4}
        turn = {"up": 50, "down": 10, "follow": 0.8}
        self.assertEqual(self.walk([ice, still_losing, turn]), ["冰点", "冰点", "修复"])

    def test_ferment_cools_into_ebb_only_when_it_weakens(self):
        strong = {"up": 80, "down": 3, "follow": 2.0}
        soft = {"up": 50, "down": 8, "follow": 0.4}
        weak = {"up": 30, "down": 8, "follow": 0.4}
        self.assertEqual(self.walk([strong, soft]), ["发酵", "发酵"])
        self.assertEqual(self.walk([strong, weak]), ["发酵", "退潮"])

    def test_ice_needs_losing_money_unless_it_is_a_limit_down_wave(self):
        # 跌停 25–39 家：昨涨停要亏钱才算冰点；赚着钱就不是
        self.assertEqual(self.walk([{"up": 50, "down": 30, "follow": -0.5}]), ["冰点"])
        self.assertEqual(self.walk([{"up": 50, "down": 30, "follow": 0.3}]), ["修复"])
        # 跌停潮（≥40 家）：昨涨停没赚到 1% 就算冰点；赚了 1% 以上说明接力还在
        self.assertEqual(self.walk([{"up": 50, "down": 45, "follow": 0.6}]), ["冰点"])
        self.assertNotEqual(self.walk([{"up": 70, "down": 45, "follow": 1.4}]), ["冰点"])

    def test_a_high_board_breaking_starts_the_ebb(self):
        strong = {"up": 70, "down": 6, "follow": 2.0, "height": 10}
        broke = {"up": 66, "down": 8, "follow": 1.1, "height": 3}       # 最高板从 10 板掉到 3 板，昨涨停赚得也不多
        self.assertEqual(self.walk([strong, broke]), ["发酵", "退潮"])
        still_strong = {"up": 70, "down": 6, "follow": 2.2, "height": 3}  # 昨涨停赚得很好，不当成断板
        self.assertEqual(self.walk([strong, still_strong]), ["发酵", "发酵"])
        small = {"up": 66, "down": 8, "follow": 1.1, "height": 4}         # 昨天只有 5 板，不算高标
        self.assertEqual(self.walk([{**strong, "height": 5}, {**small, "height": 2}]), ["发酵", "发酵"])

    def test_high_broken_rate_blocks_the_peak(self):
        ferment = {"up": 90, "down": 2, "follow": 2.0}
        messy = {"up": 120, "down": 2, "follow": 3.0, "broken_rate": 0.5}
        self.assertEqual(self.walk([ferment, messy])[-1], "发酵")

    def test_theme_break_starts_the_ebb_only_when_the_whole_market_is_not_making_money(self):
        strong = {"up": 70, "down": 6, "follow": 2.0}
        broke = {"up": 66, "down": 8, "follow": 0.9, "theme_follow": -2.1}
        self.assertEqual(self.walk([strong, broke]), ["发酵", "退潮"])
        fine = {"up": 66, "down": 8, "follow": 2.0, "theme_follow": -2.1}      # 整体还在赚钱，别的行业接力了
        self.assertEqual(self.walk([strong, fine]), ["发酵", "发酵"])
        small = {"up": 66, "down": 8, "follow": 0.9, "theme_follow": -0.8}      # 亏得不到 1.5%
        self.assertEqual(self.walk([strong, small]), ["发酵", "发酵"])

    def test_stats_scale_20cm_drop_beijing_and_find_the_theme(self):
        def row(code, limit, industry="半导体", times=1):
            return {"ts_code": code, "limit": limit, "limit_times": times, "industry": industry}

        day1 = [row("600001.SH", "U"), row("300001.SZ", "U"), row("688001.SH", "U"), row("830001.BJ", "U"),
                row("600010.SH", "U", "医药"), row("600011.SH", "U", "医药"), row("600020.SH", "U", "煤炭"), row("600021.SH", "U", "煤炭"), row("000001.SZ", "U", "银行")]
        day2 = [row("600002.SH", "U"), row("300002.SZ", "D"), row("830002.BJ", "D")]
        pct = {"20260102": {"600001.SH": 5.0, "300001.SZ": 10.0, "688001.SH": -4.0, "830001.BJ": 20.0,
                            "600010.SH": 0.0, "600011.SH": 0.0, "600020.SH": 0.0, "600021.SH": 0.0, "000001.SZ": 1.0}}
        first, second = stats({"20260101": day1, "20260102": day2}, pct, ["20260101", "20260102"])
        self.assertEqual((first["up"], first["down"]), (8, 0))                   # 北交所的 1 只不算
        self.assertEqual([item["name"] for item in first["themes"]], ["半导体", "医药", "煤炭"])   # 并列按名字排，银行 1 只落选
        self.assertEqual(first["themes"][0]["n"], 3)
        self.assertAlmostEqual(first["theme_share"], 7 / 8)
        self.assertEqual((second["up"], second["down"]), (1, 1))                  # 北交所跌停不算
        # 昨涨停 8 只（不含北交所）：5；10(20cm)→5；-4(20cm)→-2；0 ×4；1，平均 9/8
        self.assertAlmostEqual(second["follow"], (5 + 5 - 2 + 1) / 8)
        # 昨天的主线是半导体、医药、煤炭（7 只），今天表现 (5 + 5 − 2) / 7
        self.assertAlmostEqual(second["theme_follow"], (5 + 5 - 2) / 7)
        self.assertEqual(second["theme_n"], 7)

    def test_stats_follow_uses_previous_day_limit_up_list(self):
        rows = {
            "20261101": [{"ts_code": "A", "limit": "U", "limit_times": 3}, {"ts_code": "B", "limit": "U", "limit_times": 1}],
            "20261102": [{"ts_code": "C", "limit": "U", "limit_times": 1}, {"ts_code": "D", "limit": "Z"}],
        }
        pct = {"20261102": {"A": 4.0, "B": -2.0}}
        out = stats(rows, pct, ["20261101", "20261102"])
        self.assertIsNone(out[0]["follow"])
        self.assertAlmostEqual(out[1]["follow"], 1.0)
        self.assertAlmostEqual(out[1]["broken_rate"], 0.5)


class EtfSeriesTest(unittest.TestCase):
    def test_share_in_yi_and_flag_on_up_day_outflow(self):
        shares = [{"trade_date": f"2026{(i // 28) + 1:02d}{i % 28 + 1:02d}", "fd_share": (1000 + (i % 2)) * 1e4} for i in range(70)]
        shares[-1]["fd_share"] = 900 * 1e4
        bars = [{"trade_date": s["trade_date"], "close": 2.0, "pct_chg": 1.2} for s in shares]
        points = series_of(shares, bars)
        self.assertAlmostEqual(points[0]["share"], 1000.0)
        self.assertEqual(flag(points, len(points) - 1), "涨日减")
        self.assertEqual(flag(points, 5), "")


if __name__ == "__main__":
    unittest.main()


class PlainWordsTest(unittest.TestCase):
    """页面上给用户看的话：结论在前，用「近 60 天里排第几」说明，不放 z 值、标准化、信号强度这些词。"""

    LAST = {"day": "2026-11-03", "index_pct": -0.82, "delta": 611.3, "signal": 3.8, "limit": 0.47, "money_limit": 239.4, "since": 1, "rank": 1, "of": 60,
            "positive": 9, "names": 9, "blue_share": 0.82, "cum3": -1.17, "weak": "大跌"}

    def test_headline_when_acting(self):
        from app.services.market_page import _team_headline

        text = _team_headline({"label": "在出手", "last": self.LAST, "hold": None})
        self.assertTrue(text.startswith("出手了：11-03"))
        for phrase in ("上证跌 0.8%", "9 只大盘 ETF 里 9 只当天在净申购", "合计净申购约 611 亿元", "近 60 个交易日里最多的一次", "大盘宽基占 82%", "今天是第 2 天"):
            self.assertIn(phrase, text)
        for jargon in ("标准化", "中位数", "门槛", "信号强度", "MAD", "异常度"):
            self.assertNotIn(jargon, text)

    def test_event_wording_by_kind_of_weakness(self):
        from app.services.market_page import _team_event

        self.assertIn("排第 3", _team_event({**self.LAST, "rank": 3}))
        self.assertIn("近 3 日累计跌 2.4%", _team_event({**self.LAST, "weak": "连跌", "cum3": -2.4}))
        extreme = _team_event({**self.LAST, "weak": "极猛"})
        self.assertIn("跌得不算多，但这笔买入是近 60 天里最猛的，所以也算", extreme)
        self.assertNotIn("近 3 日累计", _team_event(self.LAST))
        carried = _team_event({**self.LAST, "weak": "承接", "index_pct": 0.9, "worst3": -1.4, "cum3": -1.0, "streak": 2})
        self.assertIn("上证涨 0.9%，但刚承过压", carried)
        self.assertIn("最弱一天 -1.4%", carried)
        self.assertIn("连续出手的第 2 次", carried)

    def test_sell_headline(self):
        from app.services.market_page import _team_headline

        sell = {"day": "2026-05-11", "since": 1, "days": 3, "names": 9, "negative": 9, "delta": -259.0, "blue_share": 0.92, "index_pct": 1.08}
        text = _team_headline({"label": "可能在卖", "last": None, "hold": None, "sell": sell})
        for phrase in ("可能在卖", "9 只大盘 ETF 里 9 只在被净赎回", "合计净赎回约 259 亿元", "大盘宽基占 92%", "上证涨 1.1%", "分不清是不是国家队"):
            self.assertIn(phrase, text)

    def test_other_labels(self):
        from app.services.market_page import _team_headline

        self.assertTrue(_team_headline({"label": "没出手", "last": {**self.LAST, "since": 7}, "hold": None}).startswith("最近没出手。上一次是 11-03（7 个交易日前）"))
        self.assertIn("还回去 40%", _team_headline({"label": "可能在卖", "last": self.LAST, "hold": {"gone": 0.4}}))
        self.assertIn("近一年没有出现", _team_headline({"label": "没出手", "last": None, "hold": None}))
