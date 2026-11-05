import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.notify import channels as chan
from app.notify import pipeline
from app.notify.pipeline import Notifier
from app.notify.store import NotifyStore
from app.scheduler import notify_hook
from app.scheduler.errors import NotReady, Permanent
from app.scheduler.spec import registry
from app.scheduler.tasks import alerts as alert_tasks
from test_notify_core import Recorder
from test_scheduler_core import Harness


class FakeSynthesis:
    def __init__(self, alerts):
        self._alerts = alerts

    def watch_list(self):
        return [{"ts_code": a["ts_code"], "name": "招商银行"} for a in self._alerts]

    def alerts(self):
        return self._alerts


class AlertTaskTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness("2026-11-05 10:07")
        self._old = dict(chan.ADAPTERS)
        self.rec = Recorder()
        chan.ADAPTERS["test"] = self.rec
        self.n = Notifier(NotifyStore(":memory:"), self.h.clock)
        self.n.store.put_channel({"channel_id": "ch_test", "type": "test", "name": "t"})
        pipeline.install(self.n)
        self.store = FakeSynthesis([{"id": 1, "ts_code": "600036.SH", "side": "down", "price": 36.0, "confirm": "touch", "base_name": ""}])
        self._patches = [(alert_tasks, "_synthesis_store", alert_tasks._synthesis_store), (alert_tasks, "fetch_quotes", alert_tasks.fetch_quotes),
                         (alert_tasks, "fetch_closes", alert_tasks.fetch_closes)]
        alert_tasks._synthesis_store = lambda: self.store
        self._patches.append((alert_tasks, "watch_subjects", alert_tasks.watch_subjects))
        alert_tasks.watch_subjects = lambda: [{"ts_code": "600036.SH", "tier": "hold"}]
        self.price = 36.5
        alert_tasks.fetch_quotes = lambda names: {code: {"price": self.price, "prev": 36.0} for code in names}
        spec = registry.get("alerts.intraday")
        self.h.registry.register(type(spec)(task_id=spec.task_id, title=spec.title, handler=spec.handler, trigger=spec.trigger, queue=spec.queue,
                                            min_interval_min=spec.min_interval_min, timeout_s=spec.timeout_s, max_attempts=spec.max_attempts, misfire=spec.misfire))

    def tearDown(self):
        for module, name, value in self._patches:
            setattr(module, name, value)
        pipeline.install(None)
        chan.ADAPTERS.clear()
        chan.ADAPTERS.update(self._old)

    def test_intraday_task_fires_the_alert_without_anyone_opening_a_page(self):
        self.h.run()
        self.assertEqual(self.rec.sent, [])
        self.h.clock.set("2026-11-05 10:10")
        self.price = 35.8
        self.h.run()
        self.assertEqual([title for _, title in self.rec.sent], ["招商银行 跌到 36.00"])
        self.h.clock.set("2026-11-05 10:13")
        self.h.run()
        self.assertEqual(len(self.rec.sent), 1)                           # 同一次下破不会每 3 分钟提醒一次

    def test_a_new_holding_gets_a_default_seven_percent_move_rule_once(self):
        self.h.run()
        rule = self.n.store.rule("default-move-600036.SH")
        self.assertEqual((rule["type"], rule["params"]["threshold"], rule["origin"]), ("price_move", 7.0, "system"))
        self.n.store.put_rule({"rule_id": "default-move-600036.SH", "enabled": 0, "params": {**rule["params"], "threshold": 9.0}}, 0)
        self.h.clock.set("2026-11-05 10:10")
        self.h.run()
        self.assertEqual(self.n.store.rule("default-move-600036.SH")["params"]["threshold"], 9.0)      # 用户改过的不会被覆盖
        alert_tasks.watch_subjects = lambda: []
        self.h.clock.set("2026-11-05 10:13")
        self.h.run()
        self.assertIsNone(self.n.store.rule("default-move-600036.SH"))                                 # 不再持有，系统建的规则跟着删

    def test_task_does_nothing_when_nothing_is_watched(self):
        self.store._alerts = []
        alert_tasks.watch_subjects = lambda: []
        self.h.run()
        job = self.h.store.jobs_of(self.h.store.last_run("alerts.intraday")["run_id"])[0]
        self.assertEqual(job["output"]["summary"]["watching"], 0)

    def test_failed_run_produces_one_aggregated_task_failed_event(self):
        self.h.add("boom", lambda ctx: (_ for _ in ()).throw(Permanent("坏了")), trigger={"kind": "daily", "at": "08:30"})
        self.h.sched.listeners.append(notify_hook.on_run_finished)
        self.h.clock.set("2026-11-05 08:35")
        self.h.run()
        events = self.n.store.events("task.failed")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["dead"], 1)

    def test_close_fetch_raises_not_ready_until_the_daily_bar_exists(self):
        import app.services.series as series
        calls = {}
        old = series.get_bars
        series.get_bars = lambda stock, n: {"items": [{"trade_date": "20261104", "close": 36.0}]}
        try:
            with self.assertRaises(NotReady):
                alert_tasks.fetch_closes({"600036.SH": "招商银行"}, "2026-11-05")
            series.get_bars = lambda stock, n: {"items": [{"trade_date": "20261105", "close": 35.5}]}
            self.assertEqual(alert_tasks.fetch_closes({"600036.SH": "招商银行"}, "2026-11-05"), {"600036.SH": ("2026-11-05", 35.5)})
        finally:
            series.get_bars = old


class RegistryTest(unittest.TestCase):
    def test_all_declared_gates_exist_and_tasks_are_valid(self):
        from app.scheduler import tasks  # noqa: F401
        self.assertEqual(registry.unknown_gates(), [])
        ids = {spec.task_id for spec in registry.all()}
        self.assertTrue({"market.pre", "market.close", "alerts.intraday", "alerts.close", "notify.dispatch"} <= ids)


if __name__ == "__main__":
    unittest.main()
