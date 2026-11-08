import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.notify import announce, pipeline
from app.notify.pipeline import Notifier
from app.notify.store import NotifyStore
from app.scheduler.spec import TaskSpec, registry
from app.scheduler.tasks import alerts as alert_tasks
from app.scheduler.tasks import news as news_tasks
from app.scheduler.tasks import nightly
from app.services import announcements as A
from app.scheduler.clock import FakeClock
from test_scheduler_core import Harness


def item(title, time, url="u"):
    return A.enrich("600036.SH", [{"source": "exchange", "title": title, "url": url + title, "time": time}])[0]


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock("2026-11-05 19:00")
        self.n = Notifier(NotifyStore(":memory:"), self.clock)
        self.n.store.put_channel({"channel_id": "ch_desktop", "enabled": 0})
        self.old = [item("董事会决议公告", "2026-11-05 10:00:00")]

    def watch(self, tier="hold"):
        announce.ensure_default_rules(self.n, [{"ts_code": "600036.SH", "tier": tier}], {"600036.SH": "招商银行"})
        announce.evaluate(self.n, "600036.SH", "招商银行", self.old)          # 第一次只记水位

    def events(self):
        return [(e["severity"], e["payload"]["title"]) for e in self.n.store.events("announcement.published")]

    def test_first_evaluation_sets_the_cursor_and_does_not_replay_history(self):
        self.watch()
        self.assertEqual(self.events(), [])
        self.assertIsNotNone(self.n.store.rule_state("default-ann-600036.SH")["cursor"])

    def test_holding_gets_p0_urgent_p1_normal_p2_info(self):
        self.watch("hold")
        new = [item("关于收到监管函的公告", "2026-11-05 18:00:00"), item("关于回购股份的公告", "2026-11-05 18:20:00"), item("第六届董事会第一次会议决议公告", "2026-11-05 18:40:00")]
        announce.evaluate(self.n, "600036.SH", "招商银行", self.old + new)
        self.assertEqual(sorted(s for s, _ in self.events()), ["info", "normal", "urgent"])

    def test_wish_ignores_p2_and_other_only_keeps_p0(self):
        for tier, expected in (("wish", {"urgent", "normal"}), ("other", {"urgent"})):
            self.n = Notifier(NotifyStore(":memory:"), self.clock)
            self.watch(tier)
            new = [item("关于收到监管函的公告", "2026-11-05 18:00:00"), item("关于回购股份的公告", "2026-11-05 18:20:00"), item("董事会决议公告2", "2026-11-05 18:40:00")]
            announce.evaluate(self.n, "600036.SH", "招商银行", self.old + new)
            self.assertEqual({s for s, _ in self.events()}, expected, tier)

    def test_the_same_item_is_never_notified_twice(self):
        self.watch()
        new = [item("关于收到监管函的公告", "2026-11-05 18:00:00")]
        announce.evaluate(self.n, "600036.SH", "招商银行", self.old + new)
        announce.evaluate(self.n, "600036.SH", "招商银行", self.old + new)
        self.assertEqual(len(self.events()), 1)

    def test_several_new_items_of_one_level_collapse_into_one_event(self):
        self.watch()
        new = [item("关于收到监管函的公告", "2026-11-05 18:00:00"), item("关于收到警示函的公告", "2026-11-05 18:02:00"), item("关于收到问询函的公告", "2026-11-05 18:03:00")]
        announce.evaluate(self.n, "600036.SH", "招商银行", self.old + new)
        events = self.n.store.events("announcement.published")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["extra"], 2)
        self.assertIn("还有 2 条", self.n.store.inbox()[0]["body"])

    def test_a_late_arriving_older_item_is_not_lost_or_repeated(self):
        self.watch()
        announce.evaluate(self.n, "600036.SH", "招商银行", self.old + [item("关于回购股份的公告", "2026-11-05 18:20:00")])
        # 水位之后才到的、发布时间比水位早的条目，属于已经过去的，不再追
        announce.evaluate(self.n, "600036.SH", "招商银行", self.old + [item("关于回购股份的公告", "2026-11-05 18:20:00"), item("关于增持公司股份的公告", "2026-11-05 18:10:00")])
        self.assertEqual(len(self.events()), 1)

    def test_normal_events_are_capped_at_five_a_day_per_stock(self):
        self.watch()
        for i in range(7):
            announce.evaluate(self.n, "600036.SH", "招商银行", self.old + [item(f"关于回购股份的公告{i}", f"2026-11-05 18:{10 + i}:00")])
        severities = [e["severity"] for e in self.n.store.events("announcement.published")]
        self.assertEqual((severities.count("normal"), severities.count("info")), (5, 2))

    def test_user_rule_with_keywords(self):
        self.n.store.put_rule({"rule_id": "u1", "type": "announcement", "subject_kind": "stock", "subject_id": "600036.SH", "mode": "stream", "origin": "user",
                               "params": {"min_importance": "P1", "keywords_any": ["回购"], "keywords_none": ["注销"]}}, 0)
        announce.evaluate(self.n, "600036.SH", "招商银行", self.old)
        announce.evaluate(self.n, "600036.SH", "招商银行", self.old + [item("关于回购股份的公告", "2026-11-05 18:00:00"), item("关于回购股份注销的公告", "2026-11-05 18:05:00"),
                                                               item("关于收到监管函的公告", "2026-11-05 18:10:00")])
        self.assertEqual([t for _, t in self.events()], ["关于回购股份的公告"])

    def test_removing_a_stock_from_the_watch_list_removes_its_system_rule(self):
        self.watch()
        out = announce.ensure_default_rules(self.n, [], {})
        self.assertEqual(out["removed"], 1)


class WatchTaskTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness("2026-11-05 19:00")
        self.n = Notifier(NotifyStore(":memory:"), self.h.clock)
        self.n.store.put_channel({"channel_id": "ch_desktop", "enabled": 0})
        pipeline.install(self.n)
        self.raw = [{"source": "exchange", "title": "董事会决议公告", "url": "a", "time": "2026-11-05 10:00:00"}]
        self._saved = (alert_tasks.watch_subjects, news_tasks.items_from_server, news_tasks.items_local)
        alert_tasks.watch_subjects = lambda: [{"ts_code": "600036.SH", "tier": "hold"}]
        news_tasks.items_from_server = lambda code, day: None
        news_tasks.items_local = lambda code: A.enrich(code, self.raw)
        spec = registry.get("news.watch")
        self.h.registry.add_fanout("subjects.watch_union", nightly.watch_union)
        self.h.registry.register(TaskSpec(task_id=spec.task_id, title=spec.title, handler=spec.handler, trigger=spec.trigger, queue=spec.queue, fanout=spec.fanout,
                                          timeout_s=60, misfire="skip"))

    def tearDown(self):
        alert_tasks.watch_subjects, news_tasks.items_from_server, news_tasks.items_local = self._saved
        pipeline.install(None)

    def test_new_announcements_become_events_on_the_next_round(self):
        self.h.run()
        self.assertEqual(self.n.store.events("announcement.published"), [])
        self.raw.append({"source": "exchange", "title": "关于收到立案告知书的公告", "url": "b", "time": "2026-11-05 19:05:00"})
        self.h.clock.set("2026-11-05 19:10")
        self.h.run()
        events = self.n.store.events("announcement.published")
        self.assertEqual((len(events), events[0]["severity"]), (1, "urgent"))
        self.h.clock.set("2026-11-05 19:20")
        self.h.run()
        self.assertEqual(len(self.n.store.events("announcement.published")), 1)


if __name__ == "__main__":
    unittest.main()
