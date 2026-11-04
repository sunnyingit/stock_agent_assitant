import unittest
from datetime import datetime

from app.services import market_rules as rules
from app.services import market_team as team
from app.services.market_emotion import stats
from app.services.market_etf import flag, series_of


def _totals(days: int = 80, jump_at: int | None = None, jump: float = 100.0):
    """合计份额：平时每天 +1 ± 0.5 上下摆，jump_at 那天一次性多 jump。"""
    out, total = [], 1000.0
    for i in range(days):
        total += 1 + (0.5 if i % 2 else -0.5)
        if i == jump_at:
            total += jump
        out.append((f"2026{(i // 28) + 1:02d}{i % 28 + 1:02d}", total))
    return out


class DetectTest(unittest.TestCase):
    def test_big_inflow_on_a_big_down_day_is_an_action(self):
        totals = _totals(jump_at=70)
        pct = {day: 0.2 for day, _ in totals}
        pct[totals[70][0]] = -2.1
        found = team.detect_actions(totals, pct)
        self.assertEqual([item["day"] for item in found], [totals[70][0]])
        self.assertEqual(found[0]["pos"], 70)

    def test_the_same_inflow_on_an_up_day_is_not(self):
        totals = _totals(jump_at=70)
        pct = {day: 0.2 for day, _ in totals}
        self.assertEqual(team.detect_actions(totals, pct), [])

    def test_a_small_down_day_is_not(self):
        totals = _totals(jump_at=70)
        pct = {day: 0.2 for day, _ in totals}
        pct[totals[70][0]] = -0.6
        self.assertEqual(team.detect_actions(totals, pct), [])

    def test_holding_and_label(self):
        totals = _totals(jump_at=70)
        pct = {day: 0.2 for day, _ in totals}
        pct[totals[70][0]] = -2.1
        actions = team.detect_actions(totals, pct)
        hold = team.holding(totals, actions[-1])
        self.assertAlmostEqual(hold["bought"], actions[-1]["delta"])
        self.assertLess(hold["gone"], 0.3)
        self.assertEqual(team.label_of(actions, len(totals), hold), "没出手")   # 出手在 9 个交易日前，不算最近
        self.assertEqual(team.label_of(actions, 72, hold), "在出手")
        sold = {**hold, "gone": 0.4}
        self.assertEqual(team.label_of(actions, len(totals), sold), "可能在卖")

    def test_history_waits_for_twenty_days(self):
        days = [d for d, _ in _totals(80)]
        pct = {day: 1.0 for day in days}
        actions = [{"day": days[10], "delta": 50.0, "index_pct": -2.0}, {"day": days[70], "delta": 60.0, "index_pct": -1.5}]
        rows = team.history(actions, days, pct)
        self.assertAlmostEqual(rows[0]["ret20"], 1.01 ** 20 - 1)
        self.assertEqual((rows[1]["ret20"], rows[1]["progress"]), (None, 9))


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
    def test_stage_rules(self):
        ice = [{"up": 30, "down": 40, "follow": -1.0}] * 3
        ebb = [{"up": 60, "down": 8, "follow": -0.5}] * 3
        peak = [{"up": 100, "down": 5, "follow": 2.5}] * 3
        ferment = [{"up": 55, "down": 6, "follow": 1.0}] * 3
        repair = [{"up": 30, "down": 8, "follow": 0.2}] * 3
        self.assertEqual([rules.emotion_stage(w) for w in (ice, ebb, peak, ferment, repair)], ["冰点", "退潮", "高潮", "发酵", "修复"])

    def test_one_bad_day_does_not_flip_the_stage(self):
        window = [{"up": 90, "down": 5, "follow": 3.0}, {"up": 90, "down": 5, "follow": 3.0}, {"up": 90, "down": 5, "follow": -0.5}]
        self.assertEqual(rules.emotion_stage(window), "高潮")

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
