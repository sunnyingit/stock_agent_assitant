import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.notify import channels as chan
from app.notify import digest, pipeline
from app.notify.pipeline import Notifier
from app.notify.store import NotifyStore
from app.scheduler import runtime
from app.scheduler.errors import Permanent
from app.scheduler.spec import TaskSpec
from app.scheduler.tasks import ops
from test_notify_core import Recorder
from test_scheduler_core import Harness


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness("2026-11-05 08:00")
        self.n = Notifier(NotifyStore(":memory:"), self.h.clock)
        self.n.store.put_channel({"channel_id": "ch_desktop", "enabled": 0})
        pipeline.install(self.n)
        self.rt = runtime.Runtime(self.h.sched, self.h.exec)
        runtime.install(self.rt)
        self.h.sched.tick()                 # 08:00 装上：之后的周期才算「该跑」

    def tearDown(self):
        pipeline.install(None)
        runtime.install(None)

    def test_missing_run_after_the_deadline_is_reported_once(self):
        self.h.add("brief", trigger={"kind": "daily", "at": "08:30"}, overdue_after_min=60)
        self.h.sched.tick()
        self.h.store.set_override("brief", 0, enabled=False)       # 模拟没跑起来
        self.h.clock.set("2026-11-05 09:00")
        self.assertEqual(ops.find_overdue(self.rt, self.h.clock.now_ms()), [])
        self.h.clock.set("2026-11-05 09:45")
        found = ops.find_overdue(self.rt, self.h.clock.now_ms())
        self.assertEqual([i["task_id"] for i in found], [])        # 被用户关掉的不算
        self.h.store.clear_override("brief")
        found = ops.find_overdue(self.rt, self.h.clock.now_ms())
        self.assertEqual((found[0]["task_id"], found[0]["state"]), ("brief", "没有运行记录"))

    def test_a_successful_or_pre_install_period_is_not_overdue(self):
        self.h.add("brief", trigger={"kind": "daily", "at": "08:30"}, overdue_after_min=60)
        self.h.clock.set("2026-11-05 08:35")
        self.h.run()
        self.h.clock.set("2026-11-05 12:00")
        self.assertEqual(ops.find_overdue(self.rt, self.h.clock.now_ms()), [])

    def test_failed_run_is_overdue_and_watchdog_publishes_task_overdue_without_duplicates(self):
        self.h.add("brief", lambda ctx: (_ for _ in ()).throw(Permanent("x")), trigger={"kind": "daily", "at": "08:30"}, overdue_after_min=60)
        self.h.clock.set("2026-11-05 08:35")
        self.h.run()
        self.h.clock.set("2026-11-05 10:00")
        spec = TaskSpec("watchdog", "看门狗", ops.watchdog, {"kind": "cron", "expr": "*/10 * * * *", "days": "any", "window_min": 10}, queue="light", misfire="skip")
        self.h.registry.register(spec)
        self.h.run()
        self.h.clock.advance(minutes=10)
        self.h.run()
        events = self.n.store.events("task.overdue")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["severity"], "urgent")


class DigestTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness("2026-11-05 09:00")
        self.rec = Recorder()
        self._old = dict(chan.ADAPTERS)
        chan.ADAPTERS["test"] = self.rec
        self.n = Notifier(NotifyStore(":memory:"), self.h.clock)
        self.n.store.put_channel({"channel_id": "ch_desktop", "enabled": 0})
        self.n.store.put_channel({"channel_id": "ch_test", "type": "test", "name": "t"})

    def tearDown(self):
        chan.ADAPTERS.clear()
        chan.ADAPTERS.update(self._old)

    def move(self, code, key):
        return self.n.publish("price.move", {"kind": "stock", "id": code}, {"ts_code": code, "name": code, "pct": 5, "observed_price": 10}, key)

    def test_disabled_by_default(self):
        self.assertEqual(digest.build(self.n, "早间摘要"), {"status": "disabled"})

    def test_digest_mode_batches_normal_events_and_delivers_them_as_one_summary(self):
        self.n.store.set_settings({"digest": {"enabled": True, "times": ["08:35"]}})
        self.move("600036.SH", "a")
        self.move("000001.SZ", "b")
        self.assertEqual(self.rec.sent, [])
        self.assertEqual(len(self.n.store.inbox()), 2)
        self.h.clock.advance(minutes=30)
        out = digest.build(self.n, "早间摘要")
        self.assertEqual(out, {"status": "created", "absorbed": 2})
        self.assertEqual(len(self.rec.sent), 1)
        self.assertTrue(self.rec.sent[0][1].startswith("早间摘要"))
        self.assertEqual(digest.build(self.n, "早间摘要"), {"status": "empty"})            # 已吸收的不会进下一次摘要

    def test_urgent_events_still_go_out_immediately_in_digest_mode(self):
        self.n.store.set_settings({"digest": {"enabled": True, "times": []}})
        self.n.publish("price.alert.fired", {"kind": "stock", "id": "1"}, {"alert_id": "1", "ts_code": "1", "side": "up", "trigger_price": 1, "observed_price": 2}, "u")
        self.assertEqual(len(self.rec.sent), 1)


if __name__ == "__main__":
    unittest.main()
