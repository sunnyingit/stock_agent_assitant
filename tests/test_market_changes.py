import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app.services import market_changes as ch
from app.services import market_intraday as intraday
from app.services import market_job, market_notify
from app.services.market_store import MarketStore


def snap(day, level="中性", tilt="偏弱", money="偏弱", mood="平", verdicts=None, streak=0, margin="在降", stage="发酵", broke=(), team="没出手", policy=3, watch=None):
    return {
        "day": day, "generated_at": day, "level": level, "tilt": tilt, "gate": "不过", "headline": "没有新钱",
        "signals": {"money": {"label": money, "tier": "逆"}, "team": {"label": team, "tier": "平"}, "mood": {"label": mood, "tier": "平"}},
        "etf": {"label": "基本没变", "streak": streak, "ratio": 0.0}, "margin": {"label": margin, "ratio4": -0.03},
        "stage": stage, "broke_low": list(broke), "sectors": verdicts or {"芯片": "三层同向", "银行": "有钱没热度"},
        "macro": {"score": 3.3, "tier": "平", "policy": policy}, "team": team,
        "watch": watch or {"603986.SH": {"name": "兆易创新", "sector": "芯片", "verdict": "三层同向"}},
    }


class DiffTest(unittest.TestCase):
    def test_nothing_changed(self):
        self.assertEqual(ch.diff(snap("a"), snap("b")), [])

    def test_sector_verdict_and_stage_changes(self):
        items = ch.diff(snap("a"), snap("b", verdicts={"芯片": "只热不增", "银行": "有钱没热度"}, stage="退潮"))
        titles = [item["title"] for item in items]
        self.assertIn("芯片：三层同向 → 只热不增", titles)
        self.assertIn("短线情绪：发酵 → 退潮", titles)
        self.assertTrue(all(item["detail"] for item in items))

    def test_margin_turn_needs_leaving_the_falling_labels(self):
        kinds = [item["kind"] for item in ch.diff(snap("a", margin="在降"), snap("b", margin="持平"))]
        self.assertIn("margin_turn", kinds)
        self.assertNotIn("margin_turn", [item["kind"] for item in ch.diff(snap("a", margin="持平"), snap("b", margin="持平"))])

    def test_new_break_below_prior_low(self):
        items = ch.diff(snap("a"), snap("b", broke=["创业板指"]))
        self.assertEqual(items[0]["title"], "创业板指跌破前低")

    def test_watch_stock_sector_change(self):
        cur = snap("b", watch={"603986.SH": {"name": "兆易创新", "sector": "芯片", "verdict": "在流出"}})
        items = ch.diff(snap("a"), cur)
        self.assertEqual([item["kind"] for item in items if item["kind"] == "watch_sector"], ["watch_sector"])

    def test_since_counts_back_to_where_the_run_started(self):
        snaps = [snap("2026-10-26"), snap("2026-10-27"), snap("2026-10-28", level="中性", tilt="均衡"), snap("2026-10-29", level="中性", tilt="均衡")]
        self.assertEqual(ch.run_start(snaps, ch.level_text), "2026-10-28")


class StreakTest(unittest.TestCase):
    def test_etf_streak_signed(self):
        self.assertEqual(ch.etf_streak([1, -2, -3, -1]), -3)
        self.assertEqual(ch.etf_streak([-1, 2, 3]), 2)
        self.assertEqual(ch.etf_streak([1, 0]), 0)


class ViewTest(unittest.TestCase):
    def test_modes_pick_their_baselines(self):
        snaps = [snap("2026-10-20", tilt="均衡"), snap("2026-10-28"), snap("2026-11-03"), snap("2026-11-04", level="逆风", tilt="")]
        today = ch.view("today", snaps)
        week = ch.view("week", snaps)
        self.assertEqual((today["base_day"], week["base_day"]), ("2026-11-03", "2026-10-28"))
        self.assertIn("结论变了", today["headline"])

    def test_last_seen_uses_the_snapshot_before_that_moment(self):
        snaps = [snap("2026-10-28"), snap("2026-11-02", tilt="均衡"), snap("2026-11-04")]
        seen = datetime(2026, 11, 2, 21, 0).timestamp()
        self.assertEqual(ch.view("last", snaps, seen)["base_day"], "2026-11-02")

    def test_first_day_has_nothing_to_compare(self):
        out = ch.view("today", [snap("2026-11-04")])
        self.assertIsNone(out["base_day"])
        self.assertTrue(out["empty"])

    def test_state_items_show_a_running_streak(self):
        cur = snap("2026-11-04", streak=-3, broke=["创业板指"])
        items = ch.state_items(cur, [snap("2026-11-03", broke=["创业板指"]), cur])
        self.assertEqual([item["tag"] for item in items], ["第 3 天", "第 2 天"])

    def test_history_merges_runs(self):
        rows = ch.history([snap("a"), snap("b"), snap("c", tilt="均衡")])
        self.assertEqual([(row["text"], row["days"]) for row in rows], [("中性均衡", 1), ("中性偏弱", 2)])


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = MarketStore(Path(self.dir.name) / "m.db")
        self.sent = []

    def tearDown(self):
        self.dir.cleanup()

    def summary(self, day, level="中性", tilt="偏弱", streak_deltas=(1, 1)):
        parts = {"etf": {"label": "基本没变", "ratio": 0.0, "series": [{"delta": 0}] + [{"delta": d} for d in streak_deltas]}, "margin": {"label": "在降", "ratio4": -0.03}, "team": {"label": "没出手"}}
        summary = {
            "today": day, "generated_at": day, "conclusion": {"level": level, "tilt": tilt, "gate": "不过", "headline": "x", "signals": [
                {"key": "money", "label": "偏弱", "tier": "逆"}, {"key": "team", "label": "没出手", "tier": "平"}, {"key": "mood", "label": "平", "tier": "平"}]},
            "mood": {"stage": "发酵", "indexes": []}, "sectors": {"rows": [{"name": "银行", "verdict": "有钱没热度"}]},
            "macro": {"score": 3.3, "tier": "平", "rows": [{"key": "policy", "score": 3}]}, "watch": {"rows": []},
        }
        return summary, parts

    def push(self, key, title, text):
        self.sent.append((key, title))

    def test_first_day_records_a_snapshot_and_no_events(self):
        summary, parts = self.summary("2026-11-03")
        self.assertEqual(market_notify.record(summary, parts, self.store, push=self.push), [])
        self.assertEqual(len(self.store.snapshots()), 1)

    def test_a_level_change_makes_one_event_and_one_push_even_if_recorded_twice(self):
        market_notify.record(*self.summary("2026-11-03"), self.store, push=self.push)
        later = self.summary("2026-11-04", level="逆风", tilt="")
        first = market_notify.record(*later, self.store, push=self.push)
        again = market_notify.record(*later, self.store, push=self.push)
        self.assertEqual(len(first), 1)
        self.assertEqual(again, [])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.store.unread(), 1)

    def test_switches_silence_a_kind_and_push_off_keeps_the_page_event(self):
        market_notify.record(*self.summary("2026-11-03"), self.store, push=self.push)
        self.store.set_settings({"level": False})
        self.assertEqual(market_notify.record(*self.summary("2026-11-04", level="逆风", tilt=""), self.store, push=self.push), [])
        self.store.set_settings({"level": True, "push": False})
        market_notify.record(*self.summary("2026-11-04", level="逆风", tilt=""), self.store, push=self.push)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.unread(), 1)

    def test_etf_streak_event_fires_on_the_nth_day_only(self):
        market_notify.record(*self.summary("2026-11-03"), self.store, push=self.push)
        self.store.set_settings({"etf_n": 3})
        hit = market_notify.record(*self.summary("2026-11-04", streak_deltas=(1, 2, 3)), self.store, push=self.push)
        self.assertEqual([item["kind"] for item in hit], ["etf_streak"])
        self.assertEqual(market_notify.record(*self.summary("2026-11-05", streak_deltas=(1, 2, 3, 4)), self.store, push=self.push), [])

    def test_notifications_group_by_day_and_mark_read(self):
        market_notify.record(*self.summary("2026-11-03"), self.store, push=self.push)
        market_notify.record(*self.summary("2026-11-04", level="逆风", tilt=""), self.store, push=self.push)
        data = market_notify.notifications(self.store, today=datetime(2026, 11, 4).date())
        self.assertEqual(data["groups"][0]["title"], "今天")
        self.assertTrue(data["groups"][0]["items"][0]["unread"])
        self.store.mark_read()
        self.assertEqual(self.store.unread(), 0)

    def test_settings_validation(self):
        self.assertEqual(market_notify.clean_settings({"etf_n": 4, "junk": 1, "gate": 0}), {"etf_n": 4, "gate": False})
        with self.assertRaises(ValueError):
            market_notify.clean_settings({"etf_n": 20})


class JobTest(unittest.TestCase):
    def test_due_windows(self):
        self.assertEqual(market_job.due(datetime(2026, 11, 4, 8, 31), set()), "pre")
        self.assertIsNone(market_job.due(datetime(2026, 11, 4, 8, 29), set()))
        self.assertIsNone(market_job.due(datetime(2026, 11, 4, 8, 31), {"2026-11-04:pre"}))
        self.assertEqual(market_job.due(datetime(2026, 11, 4, 15, 40), {"2026-11-04:pre"}), "close")
        self.assertIsNone(market_job.due(datetime(2026, 11, 4, 16, 30), set()))


class IntradayTest(unittest.TestCase):
    def rows(self, amounts_by_day):
        out = []
        for day, amounts in amounts_by_day.items():
            for index, amount in enumerate(amounts):
                minute = 570 + index
                out.append({"day": day, "hm": f"{minute // 60:02d}:{minute % 60:02d}", "close": 100.0, "amount": amount * 1e8})
        return out

    def test_same_time_ratio_against_yesterday_and_average(self):
        rows = self.rows({"2026-11-01": [10] * 10, "2026-11-02": [20] * 10, "2026-11-03": [20] * 10, "2026-11-04": [30] * 5})
        out = intraday.compare(rows)
        self.assertEqual(out["last"], "09:34")
        self.assertAlmostEqual(out["now"], 150)
        self.assertAlmostEqual(out["vs_yesterday"], 150 / 100 - 1)
        self.assertAlmostEqual(out["avg"], (50 + 100 + 100) / 3)
        self.assertEqual(out["avg_days"], 3)

    def test_parse_reads_time_close_and_amount(self):
        rows = intraday.parse(["2026-11-04 09:31,0.00,3667.83,3670.46,3662.95,14976110,24812952320.00,3665.478", "bad"])
        self.assertEqual(rows, [{"day": "2026-11-04", "hm": "09:31", "close": 3667.83, "amount": 24812952320.0}])

    def test_one_day_is_not_enough(self):
        self.assertIsNone(intraday.compare(self.rows({"2026-11-04": [1, 2]})))


if __name__ == "__main__":
    unittest.main()
