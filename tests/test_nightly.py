import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.notify import pipeline
from app.notify.pipeline import Notifier
from app.notify.store import NotifyStore
from app.scheduler.errors import NotReady, Transient
from app.scheduler.spec import TaskSpec, registry
from app.scheduler.tasks import alerts as alert_tasks
from app.scheduler.tasks import nightly as N
from test_scheduler_core import Harness


class NightlyTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness("2026-11-05 20:35")
        self.n = Notifier(NotifyStore(":memory:"), self.h.clock)
        self.n.store.put_channel({"channel_id": "ch_desktop", "enabled": 0})
        pipeline.install(self.n)
        self.analyzed, self.history, self.fail_diff = [], {}, False
        self._saved = (N.analyze, N.judgments, alert_tasks.watch_subjects)
        N.analyze = self.analyze
        N.judgments = lambda code: self.history.get(code, [])
        alert_tasks.watch_subjects = lambda: [{"ts_code": "000001.SZ", "tier": "other"}, {"ts_code": "600036.SH", "tier": "hold"}, {"ts_code": "300750.SZ", "tier": "other"}]
        spec = registry.get("stock.nightly")
        self.h.registry.add_fanout("subjects.watch_union", N.watch_union)
        self.h.registry.add_gate("daily_bars", lambda day: True)
        self.h.registry.register(TaskSpec(task_id=spec.task_id, title=spec.title, handler=spec.handler, trigger=spec.trigger, queue=spec.queue, fanout=spec.fanout,
                                          gates=spec.gates, timeout_s=spec.timeout_s, max_attempts=spec.max_attempts, backoff_s=(1,), budget=spec.budget,
                                          misfire=spec.misfire, catchup_ttl_min=spec.catchup_ttl_min))
        self.h.exec.concurrency["llm"] = 1

    def tearDown(self):
        N.analyze, N.judgments, alert_tasks.watch_subjects = self._saved
        pipeline.install(None)

    def analyze(self, code):
        self.analyzed.append(code)
        return {"run_id": len(self.analyzed), "reused": False, "name": "招商银行" if code == "600036.SH" else code, "facts_hash": "h", "source": "rule"}

    def point(self, day, lean, level):
        return {"trade_date": day, "lean": lean, "level": level}

    def run_all(self):
        self.h.run()
        return self.h.store.last_run("stock.nightly")

    def test_holdings_are_analysed_first_and_each_stock_is_its_own_unit(self):
        run = self.run_all()
        self.assertEqual(self.analyzed, ["600036.SH", "000001.SZ", "300750.SZ"])
        self.assertEqual((run["state"], run["counts"]["succeeded"]), ("succeeded", 3))

    def test_a_changed_judgment_publishes_one_event_with_the_new_and_old_state(self):
        self.history["600036.SH"] = [self.point("2026-11-04", "bullish", 2), self.point("2026-11-05", "neutral", 0)]
        self.run_all()
        events = self.n.store.events("judgment.changed")
        self.assertEqual(len(events), 1)
        payload = events[0]["payload"]
        self.assertEqual((payload["from"]["lean"], payload["to"]["lean"], payload["name"]), ("bullish", "neutral", "招商银行"))
        self.assertIn("判断变了", self.n.store.inbox()[0]["title"])

    def test_unchanged_or_old_history_publishes_nothing(self):
        self.history["600036.SH"] = [self.point("2026-11-04", "bullish", 2), self.point("2026-11-05", "bullish", 2)]
        self.history["000001.SZ"] = [self.point("2026-11-04", "bullish", 2), self.point("2026-11-04", "bearish", 1)]
        self.history["300750.SZ"] = [self.point("2026-11-05", "bullish", 2)]
        self.run_all()
        self.assertEqual(self.n.store.events("judgment.changed"), [])

    def test_retry_after_a_failed_diff_does_not_pay_for_the_analysis_again(self):
        self.history["600036.SH"] = [self.point("2026-11-04", "bullish", 2), self.point("2026-11-05", "neutral", 0)]
        original = N._diff
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise Transient("库忙")
            return original(*args, **kwargs)
        N._diff = flaky
        try:
            self.h.run()
            self.h.clock.advance(seconds=2)
            self.h.run()
        finally:
            N._diff = original
        self.assertEqual(self.analyzed.count("600036.SH"), 1)
        self.assertEqual(len(self.n.store.events("judgment.changed")), 1)

    def test_pending_dimensions_do_not_burn_attempts(self):
        state = {"ready": False}

        def analyze(code):
            if not state["ready"]:
                raise NotReady("维度还在整理", retry_after_s=60)
            return self.analyze(code)
        N.analyze = analyze
        for _ in range(4):
            self.h.run()
            self.h.clock.advance(seconds=61)
        job = self.h.store.jobs_of(self.h.store.last_run("stock.nightly")["run_id"])[0]
        self.assertEqual((job["state"], job["attempt"]), ("not_ready", 0))
        state["ready"] = True
        self.h.run()
        self.h.run()
        self.assertEqual(self.h.store.last_run("stock.nightly")["state"], "succeeded")

    def test_budget_counts_only_fresh_analyses(self):
        N.analyze = lambda code: {"run_id": 1, "reused": True, "name": code, "facts_hash": "h", "source": "rule"}
        self.run_all()
        self.assertEqual(self.h.store.usage("llm", "2026-11-05").get("calls", 0), 0)

    def test_the_run_links_to_the_judgment_by_reference(self):
        self.run_all()
        job = self.h.store.jobs_of(self.h.store.last_run("stock.nightly")["run_id"])[0]
        self.assertTrue(any(ref["ref"].startswith("run:") for ref in job["output"]["refs"]))


if __name__ == "__main__":
    unittest.main()
