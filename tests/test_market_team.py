import unittest
from datetime import date, datetime, timedelta
from unittest import mock

from app.services import market_team as team
from app.services import market_rules as rules
from app.services.market_emotion import stats
from app.services.market_etf import flag, series_of


def _days(n: int, start: date = date(2025, 1, 6)) -> list[str]:
    day, out = start, []
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day.strftime("%Y%m%d"))
        day += timedelta(days=1)
    return out


def _walk(days, spike_at=None, spike=0.04, give=None, start=100000.0, price=1.0, noise=0.001):
    """平时每天 ±0.1%。spike_at 那天乘上 1+spike。give 给定时，最后一天从峰值回落这么多比例。"""
    level, peak, before = start, None, None
    shares, bars = [], []
    for i, day in enumerate(days):
        if i == 0:
            pass
        elif i == spike_at:
            before = level
            level *= 1 + spike
            peak = level
        elif give is not None and spike_at is not None and i > spike_at:
            level = peak if i < len(days) - 1 else peak - give * (peak - before)
        else:
            level *= 1 + (noise if i % 2 else -noise)
        shares.append({"trade_date": day, "fd_share": level})
        bars.append({"trade_date": day, "close": price, "amount": 1_000_000})
    return shares, bars


def _market(n=131, spike_at=130, move=-2.0, codes=("510300.SH", "510050.SH"), spike=0.04, give=None, moves=None):
    days = _days(n)
    shares, closes = {}, {}
    for code in codes:
        shares[code], closes[code] = _walk(days, spike_at=spike_at, spike=spike, give=give)
    pct = {day: (move if i == spike_at else 0.2) for i, day in enumerate(days)}
    if moves:
        pct.update(moves)
    return shares, closes, pct, days


class RuleTest(unittest.TestCase):
    def test_creation_uses_share_change_times_price(self):
        self.assertAlmostEqual(team.creation_yi(10, 2.0), 10 * 2.0 / 1e4)

    def test_pressure_kinds(self):
        self.assertEqual(team.pressure_of(-1.2, 0, -1.2, None, False)["kind"], "大跌")
        self.assertEqual(team.pressure_of(-0.6, -2.5, -1.0, None, False)["kind"], "连跌")
        self.assertEqual(team.pressure_of(0.4, -2.5, -1.4, None, False)["kind"], "承接")
        self.assertEqual(team.pressure_of(0.4, -0.4, -1.2, None, False)["kind"], "承接")
        self.assertEqual(team.pressure_of(0.2, 0.2, 0.2, 0.12, True)["kind"], "位置")
        near = team.pressure_of(0.2, 0.2, 0.2, 0.09, True)
        self.assertIsNone(near["kind"])
        self.assertTrue(near["near"])
        self.assertIsNone(team.pressure_of(0.4, -0.4, -0.4, None, False)["kind"])

    def test_money_has_to_land_in_broad_groups(self):
        broad = {"沪深300", "上证50"}
        self.assertTrue(team.money_went_broad({"沪深300": 50, "上证50": 40, "半导体": 10}, broad)["ok"])
        self.assertFalse(team.money_went_broad({"半导体": 100, "医药": 80, "沪深300": 10, "上证50": -1}, broad)["ok"])
        self.assertFalse(team.money_went_broad({"沪深300": -5, "上证50": -4, "半导体": -1}, broad)["ok"])

    def test_a_group_hits_when_the_larger_mz_clears_both_bars(self):
        rates = [0.001 if i % 2 else -0.001 for i in range(80)] + [0.04]
        hit = team.group_hit(rates, 80, "up")
        self.assertTrue(hit["hit"])
        self.assertFalse(hit["short"])
        self.assertGreater(hit["mz1"], 3)
        quiet = team.group_hit(rates, 70, "up")
        self.assertFalse(quiet["hit"])

    def test_fallback_when_the_baseline_is_short(self):
        rates = [None] * 60 + [0.001] * 20 + [0.02]
        hit = team.group_hit(rates, len(rates) - 1, "up")
        self.assertTrue(hit["hit"])
        self.assertTrue(hit["short"])
        tiny = team.group_hit([None] * 60 + [0.001] * 20 + [0.004], len(rates) - 1, "up")
        self.assertFalse(tiny["hit"])

    def test_split_is_put_back_on_the_old_scale(self):
        days = ["20260105", "20260106", "20260107"]
        shares = [
            {"trade_date": days[0], "fd_share": 100.0},
            {"trade_date": days[1], "fd_share": 100.0},
            {"trade_date": days[2], "fd_share": 200.0},
        ]
        bars = [
            {"trade_date": days[0], "close": 2.0},
            {"trade_date": days[1], "close": 2.0},
            {"trade_date": days[2], "close": 1.0},
        ]
        got = team._member(shares, bars)
        self.assertTrue(got[days[2]]["split"])
        self.assertAlmostEqual(got[days[2]]["adj"], 100.0)

    def test_giveback_is_the_median_group(self):
        # 峰值 110，前值 100。A 回到 104（回落 60%），B 回到 109（回落 10%），中位数取靠后的那个
        levels = {"A": [100, 110, 104], "B": [100, 110, 109]}
        got = team.giveback(levels, ["A", "B"], 1)
        self.assertAlmostEqual(got["gone"], 0.6)
        self.assertAlmostEqual(got["line_yi"], (110 - 0.3 * 10) / 1e4)
        higher = team.giveback({"A": [100, 110, 130]}, ["A"], 1)
        self.assertEqual(higher["gone"], 0.0)

    def test_label_stays_on_the_action_for_five_days_then_follows_the_shares(self):
        self.assertEqual(team.label_of("托市", 1, 0.4, True, False), "托市中")
        self.assertEqual(team.label_of("托市", 8, 0.02, False, False), "钱还在")
        self.assertEqual(team.label_of("托市", 8, 0.18, False, False), "部分回落")
        self.assertEqual(team.label_of("托市", 8, 0.4, True, False), "可能在撤")
        self.assertEqual(team.label_of("异动", 1, None, False, False, "sell"), "异动中")
        self.assertEqual(team.label_of("异动", 8, 0.04, False, False, "sell"), "还没回补")
        self.assertEqual(team.label_of(None, None, None, False, True), "已表态")
        self.assertEqual(team.label_of(None, None, None, False, False), "没托市")

    def test_strength_fades_to_zero_on_the_fifth_day(self):
        self.assertEqual(team.strength_of(4.0, 0), 4.0)
        self.assertAlmostEqual(team.strength_of(4.0, 2), 2.4)
        self.assertEqual(team.strength_of(4.0, 5), 0.0)

    def test_size_floor_is_not_a_trigger(self):
        shares, closes, pct, _days = _market(spike=0.04)
        with mock.patch.object(team, "BROAD_MIN_YI", 10_000):
            out = team.summarize(shares, closes, pct)
        self.assertEqual(out["label"], "托市中")
        self.assertGreater(out["last"]["delta"], 0)


class SummarizeTest(unittest.TestCase):
    def test_finds_a_suspected_action_on_a_down_day(self):
        shares, closes, pct, days = _market()
        out = team.summarize(shares, closes, pct)
        self.assertTrue(out["ok"])
        self.assertEqual(out["label"], "托市中")
        self.assertEqual(out["signal"], "疑似托市")
        self.assertIsNone(out["confirm"])
        self.assertEqual(out["last"]["day"], f"{days[130][:4]}-{days[130][4:6]}-{days[130][6:]}")
        self.assertEqual(sorted(out["last"]["groups"]), ["上证50", "沪深300"])
        self.assertGreater(out["last"]["delta"], 0)

    def test_the_same_inflow_without_pressure_is_an_anomaly_not_support(self):
        shares, closes, pct, _days = _market(move=1.5)
        out = team.summarize(shares, closes, pct)
        self.assertEqual(out["label"], "异动中")
        self.assertEqual(out["last"]["state"], "异动")
        self.assertEqual(out["signal"], "疑似异动")
        self.assertIn("市场未承压，可能是调仓或过热降温，非托市", out["marks"])
        self.assertIn("广度确认", out["marks"])

    def test_a_rising_day_still_counts_after_two_pressure_days(self):
        shares, closes, pct, days = _market(move=0.4)
        pct[days[128]] = pct[days[129]] = -1.2
        out = team.summarize(shares, closes, pct)
        self.assertEqual(out["last"]["kind"], "承接")
        self.assertEqual(out["label"], "托市中")

    def test_deep_drawdown_below_the_20_day_average_is_pressure(self):
        shares, closes, pct, days = _market(move=0.2)
        index = {day: (100 if i < 40 else (90 if i < len(days) - 1 else 85)) for i, day in enumerate(days)}
        out = team.summarize(shares, closes, pct, index)
        self.assertEqual(out["last"]["kind"], "位置")

    def test_triggers_three_days_apart_are_one_event(self):
        days = _days(133)
        shares, closes = {}, {}
        for code in ("510300.SH", "510050.SH"):
            level, rows, bars = 100000.0, [], []
            for i, day in enumerate(days):
                if i in (130, 132):
                    level *= 1.04
                elif i:
                    level *= 1.001 if i % 2 else 0.999
                rows.append({"trade_date": day, "fd_share": level})
                bars.append({"trade_date": day, "close": 1.0, "amount": 1})
            shares[code], closes[code] = rows, bars
        pct = {day: (-2.0 if i in (130, 132) else 0.2) for i, day in enumerate(days)}
        out = team.summarize(shares, closes, pct)
        self.assertEqual(len(out["actions"]), 1)
        self.assertEqual(out["label"], "托市中")
        self.assertEqual(out["last"]["since"], 2)

    def test_shares_given_back_after_the_fifth_day_are_a_possible_exit(self):
        shares, closes, pct, days = _market(n=129, spike_at=120, give=0.4)
        pct[days[-1]] = -2.0
        out = team.summarize(shares, closes, pct)
        self.assertEqual(out["label"], "可能在撤")
        self.assertGreater(out["hold"]["gone"], 0.3)
        self.assertTrue(out["sell"]["sign1"])

    def test_broad_redemptions_on_calm_days_are_the_second_sign(self):
        days = _days(140)
        shares, closes = {}, {}
        for code in ("510300.SH", "510050.SH", "510500.SH"):
            level, rows, bars = 100000.0, [], []
            for i, day in enumerate(days):
                if i in (137, 138):
                    level *= 0.96
                elif i:
                    level *= 1.001 if i % 2 else 0.999
                rows.append({"trade_date": day, "fd_share": level})
                bars.append({"trade_date": day, "close": 1.0, "amount": 1})
            shares[code], closes[code] = rows, bars
        pct = {day: 0.2 for day in days}
        out = team.summarize(shares, closes, pct)
        self.assertEqual(out["label"], "异动中")
        self.assertEqual(out["last"]["side"], "sell")
        self.assertTrue(out["sell"]["sign2"])
        self.assertGreaterEqual(out["sell"]["days"], 2)

    def test_short_baseline_uses_the_fixed_threshold_and_says_so(self):
        shares, closes, pct, _days = _market(n=91, spike_at=90, spike=0.02)
        out = team.summarize(shares, closes, pct)
        self.assertEqual(out["label"], "托市中")
        self.assertIn("基线不足", out["marks"])

    def test_a_missing_day_is_skipped_not_merged(self):
        days = _days(140)
        shares, closes = {}, {}
        for code in ("510300.SH", "510050.SH"):
            rows, bars, level = [], [], 100000.0
            for i, day in enumerate(days):
                if i == 130:
                    continue
                if i:
                    level *= 1.04 if i == 131 else (1.001 if i % 2 else 0.999)
                rows.append({"trade_date": day, "fd_share": level})
                bars.append({"trade_date": day, "close": 1.0})
            shares[code], closes[code] = rows, bars
        pct = {day: (-2.0 if i == 131 else 0.2) for i, day in enumerate(days)}
        out = team.summarize(shares, closes, pct)
        self.assertIsNone(out["last"])

    def test_not_enough_history(self):
        shares, closes, pct, _days = _market(n=30, spike_at=25)
        self.assertFalse(team.summarize(shares, closes, pct)["ok"])


class StanceTest(unittest.TestCase):
    NOW = datetime(2026, 11, 4, 12, 0)

    def test_huijin_without_a_matching_action_is_only_a_statement(self):
        base = {"ok": True, "label": "没托市", "marks": [], "actions": [], "confirm": None, "confirm_word": ""}
        out = team.apply_stance(base, [{"title": "中央汇金增持宽基ETF", "time": "2026-11-01 09:00:00"}], self.NOW)
        self.assertEqual(out["label"], "已表态")
        self.assertIn("已表态，还没看到钱进来", out["marks"])
        self.assertNotEqual(out.get("confirm"), "L1")

    def test_a_rate_cut_does_not_change_the_team_state(self):
        base = {"ok": True, "label": "没托市", "marks": [], "actions": [], "confirm": None}
        out = team.apply_stance(base, [{"title": "央行宣布降准", "time": "2026-11-03 09:00:00"}], self.NOW)
        self.assertEqual(out["label"], "没托市")

    def test_chengtong_is_not_called_huijin(self):
        base = {"ok": True, "label": "没托市", "marks": [], "actions": [], "confirm": None}
        out = team.apply_stance(base, [{"title": "中国诚通增持ETF", "time": "2026-11-02 09:00:00"}], self.NOW)
        self.assertEqual(out["label"], "没托市")
        self.assertEqual(out["others"][0]["who"], "诚通")
        self.assertEqual(out["stance"], [])

    def test_a_matching_announcement_upgrades_without_moving_the_day(self):
        base = {"ok": True, "label": "托市中", "marks": [], "confirm": None, "confirm_word": "疑似托市",
                "actions": [{"day": "2026-11-03", "side": "buy"}], "last": {"day": "2026-11-03"}}
        out = team.apply_stance(base, [{"title": "汇金增持宽基ETF", "time": "2026-11-04 08:00:00"}], self.NOW)
        self.assertEqual(out["confirm"], "L1")
        self.assertEqual(out["confirm_word"], "确认汇金托市")
        self.assertEqual(out["label"], "托市中")

    def test_a_later_match_still_confirms_without_changing_the_state(self):
        base = {"ok": True, "label": "钱还在", "marks": [], "confirm": None, "confirm_word": "",
                "actions": [{"day": "2026-11-03"}], "last": {"day": "2026-11-03"}}
        out = team.apply_stance(base, [{"title": "汇金增持宽基ETF", "time": "2026-11-04 08:00:00"}], self.NOW)
        self.assertEqual(out["label"], "钱还在")
        self.assertEqual(out["confirm_word"], "确认汇金托市")


class PlainWordsTest(unittest.TestCase):
    LAST = {"day": "2026-10-09", "found": "2026-10-10", "index_pct": -1.2, "delta": 180.0, "since": 0,
            "groups": ["沪深300", "上证50", "中证500"], "kind": "大跌", "state": "托市", "side": "buy"}

    def test_headline_when_acting(self):
        from app.services.market_page import _team_headline, _team_event, _last_facts

        text = _team_headline({"label": "托市中", "confirm_word": "疑似托市", "last": self.LAST, "hold": None, "sell": None})
        self.assertIn("10-09 疑似托市", text)
        self.assertIn("3 组宽基", text)
        self.assertIn("180 亿元", text)
        self.assertIn("今天是第 1 天", text)
        for jargon in ("标准化", "MAD", "z 值", "蓝筹"):
            self.assertNotIn(jargon, text)

    def test_wave_is_shown_as_a_range_with_peak_day(self):
        from app.services.market_page import _team_event, _last_facts, _wave_when

        wave = {
            "day": "2026-10-15", "until": "2026-11-06", "span": 16, "found": "2026-10-16",
            "peak_day": "2026-11-03", "peak_delta": 779.0, "delta": 380.0,
            "index_pct": -1.6, "groups": ["沪深300", "上证50", "创业板"], "kind": "大跌",
            "state": "托市", "side": "buy", "since": 16,
        }
        self.assertEqual(_wave_when(wave, "托市"), "10-15 到 11-06 这一波托市")
        text = _team_event(wave)
        self.assertIn("10-15 到 11-06 这一波托市", text)
        self.assertIn("11-03", text)
        self.assertIn("779", text)
        keys = {row["k"]: row["v"] for row in _last_facts(wave)}
        self.assertEqual(keys["这一波"], "10-15～11-06")
        self.assertEqual(keys["最大一天"], "11-03")
        self.assertIn("779", keys["净申购"])

    def test_hold_and_exit_wording(self):
        from app.services.market_page import _team_headline

        self.assertIn("只回落 6%", _team_headline({"label": "钱还在", "last": self.LAST, "hold": {"gone": 0.06}, "sell": None}))
        text = _team_headline({"label": "部分回落", "last": self.LAST, "hold": {"gone": 0.18, "gap_yi": 12}, "sell": None})
        self.assertIn("已回落 18%", text)
        self.assertIn("12 亿份", text)
        gone = _team_headline({"label": "可能在撤", "last": self.LAST, "hold": {"gone": 0.4}, "sell": {"sign1": True, "sign2": False}})
        self.assertIn("超过 3 成", gone)
        self.assertIn("散户赎回", gone)

    def test_no_action_and_statement(self):
        from app.services.market_page import _team_headline

        self.assertIn("托市", _team_headline({"label": "没托市", "last": None, "hold": None, "sell": None}))
        self.assertIn("尚未看到资金进场", _team_headline({"label": "已表态", "last": None, "hold": None, "sell": None}))

    def test_possible_exit_does_not_score(self):
        from app.services.market_page import _team_signal

        sig = _team_signal({"ok": True, "label": "可能在撤", "marks": [], "actions": [], "confirm": None, "confirm_word": "",
                            "last": self.LAST, "hold": {"gone": 0.4}, "sell": {"sign1": True, "sign2": False}}, {})
        self.assertEqual(sig["tier"], "平")
        self.assertEqual(sig["label"], "可能在撤")


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


