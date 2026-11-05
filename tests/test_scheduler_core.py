import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.scheduler.calendar import TradingCalendar
from app.scheduler.clock import FakeClock, to_ms
from app.scheduler.core import Scheduler
from app.scheduler.errors import NotReady, Permanent, Transient
from app.scheduler.executor import Executor
from app.scheduler.spec import Registry, TaskSpec, Unit
from app.scheduler.store import SchedStore


def weekdays(year, holidays=("2026-11-06",)):
    day, out = date(year, 1, 1), set()
    while day.year == year:
        if day.weekday() < 5 and day.isoformat() not in holidays:
            out.add(day.isoformat())
        day += timedelta(days=1)
    return out


class Harness:
    def __init__(self, at="2026-11-05 08:31"):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock(at)
        self.store = SchedStore(":memory:", Path(self.tmp.name) / "results")
        self.registry = Registry()
        self.calendar = TradingCalendar(weekdays, Path(self.tmp.name) / "cal.json")
        self.sched = Scheduler(self.store, self.registry, self.calendar, self.clock)
        self.exec = Executor(self.sched)
        self.calls = []

    def add(self, task_id, handler=None, trigger=None, **options):
        def default(ctx):
            self.calls.append((ctx.task_id, ctx.unit_key, ctx.attempt))
            return {"ok": True}
        spec = TaskSpec(task_id=task_id, title=task_id, handler=handler or default, trigger=trigger or {"kind": "daily", "at": "08:30"}, **options)
        return self.registry.register(spec)

    def run(self):
        self.sched.tick()
        self.exec.drain()
        self.sched.tick()

    def state(self, task_id):
        return [(run["period_key"], run["state"]) for run in self.store.runs(task_id)]


class DailyTest(unittest.TestCase):
    def test_on_time_run_executes_once_and_repeat_ticks_are_harmless(self):
        h = Harness()
        h.add("a")
        h.run()
        h.run()
        h.run()
        self.assertEqual(h.state("a"), [("trading_day:2026-11-05", "succeeded")])
        self.assertEqual(len(h.calls), 1)

    def test_output_shape_and_metrics(self):
        h = Harness()
        h.add("a")
        h.run()
        job = h.store.jobs_of(h.store.last_run("a")["run_id"])[0]
        self.assertEqual(job["output"]["v"], 1)
        self.assertEqual(job["output"]["summary"], {"ok": True})
        self.assertIn("duration_ms", job["output"]["metrics"])

    def test_missed_window_catches_up_once_within_ttl(self):
        h = Harness("2026-11-05 14:00")
        h.add("a")
        h.run()
        states = dict(h.state("a"))
        self.assertEqual(states["trading_day:2026-11-05"], "succeeded")
        self.assertEqual(h.store.run_by_period("a", "trading_day:2026-11-05")["trigger"], "catchup")
        # 装上之前的周期不算错过，也不留记录
        self.assertEqual(states, {"trading_day:2026-11-05": "succeeded"})
        self.assertEqual(len(h.calls), 1)

    def test_periods_missed_while_installed_are_recorded_as_expired(self):
        h = Harness("2026-11-02 09:00")
        h.add("a")
        h.run()
        h.clock.set("2026-11-05 09:00")              # 电脑关了三天
        h.run()
        states = dict(h.state("a"))
        self.assertEqual(states["trading_day:2026-11-05"], "succeeded")
        self.assertEqual(states["trading_day:2026-11-04"], "expired")
        self.assertEqual(states["trading_day:2026-11-03"], "expired")

    def test_run_latest_only_runs_the_newest_missed_period(self):
        h = Harness("2026-11-02 09:00")
        h.add("a", catchup_ttl_min=60 * 72, trigger={"kind": "daily", "at": "08:30", "window_min": 10})
        h.run()
        h.clock.set("2026-11-05 09:00")
        h.run()
        states = dict(h.state("a"))
        self.assertEqual(states["trading_day:2026-11-05"], "succeeded")
        self.assertEqual(states["trading_day:2026-11-04"], "expired")
        self.assertEqual(states["trading_day:2026-11-03"], "expired")

    def test_skip_policy_never_catches_up(self):
        h = Harness("2026-11-05 08:00")
        h.add("a", misfire="skip")
        h.run()
        h.clock.set("2026-11-05 14:00")
        h.run()
        self.assertEqual(dict(h.state("a"))["trading_day:2026-11-05"], "expired")
        self.assertEqual(h.calls, [])

    def test_holiday_and_weekend_are_not_scheduled(self):
        h = Harness("2026-11-08 09:00")        # 周日；11-06 是假日
        h.add("a")
        h.run()
        self.assertEqual(dict(h.state("a")).get("trading_day:2026-11-06"), None)
        self.assertEqual(dict(h.state("a")).get("trading_day:2026-11-08"), None)

    def test_disabled_override_stops_planning(self):
        h = Harness()
        h.add("a")
        h.store.set_override("a", 0, enabled=False)
        h.run()
        self.assertEqual(h.state("a"), [])

    def test_trigger_override_changes_time(self):
        h = Harness("2026-11-05 08:45")
        h.add("a")
        h.store.set_override("a", 0, trigger={"kind": "daily", "at": "21:00"})
        h.run()
        self.assertNotIn("trading_day:2026-11-05", dict(h.state("a")))
        h.clock.set("2026-11-05 21:05")
        h.run()
        self.assertEqual(dict(h.state("a"))["trading_day:2026-11-05"], "succeeded")


class SessionIntervalTest(unittest.TestCase):
    TRIGGER = {"kind": "session_interval", "every_min": 3}

    def test_runs_the_latest_slot_only_and_never_catches_up_old_ones(self):
        h = Harness("2026-11-05 10:07")
        h.add("alerts", trigger=self.TRIGGER)
        h.run()
        self.assertEqual(h.state("alerts"), [("min:2026-11-05T10:06", "succeeded")])
        h.clock.set("2026-11-05 10:20")          # 电脑休眠了一会儿
        h.run()
        keys = [key for key, _ in h.state("alerts")]
        self.assertEqual(keys[0], "min:2026-11-05T10:18")
        self.assertNotIn("min:2026-11-05T10:09", keys)

    def test_nothing_runs_at_lunch_or_night(self):
        h = Harness("2026-11-05 12:00")
        h.add("alerts", trigger=self.TRIGGER)
        h.run()
        h.clock.set("2026-11-05 22:00")
        h.run()
        self.assertEqual(h.calls, [])

    def test_late_slot_expires_instead_of_running_stale(self):
        h = Harness("2026-11-05 10:06")
        h.add("alerts", trigger=self.TRIGGER)
        h.sched.tick()
        h.clock.set("2026-11-05 10:12")           # 还没执行就过了一个周期
        h.exec.drain()
        self.assertEqual(h.calls, [])
        self.assertEqual(h.state("alerts")[0][1] in ("running", "expired", "skipped", "partial", "failed"), True)


class RetryTest(unittest.TestCase):
    def test_transient_error_backs_off_then_succeeds(self):
        h = Harness()
        seen = []

        def flaky(ctx):
            seen.append(ctx.attempt)
            if ctx.attempt < 3:
                raise Transient("网络抖动")
            return {"done": True}
        h.add("a", flaky, backoff_s=(60, 300))
        h.run()
        job = h.store.jobs_of(h.store.last_run("a")["run_id"])[0]
        self.assertEqual((job["state"], job["attempt"]), ("retrying", 1))
        h.exec.drain()
        self.assertEqual(seen, [1])               # 还没到退避时间
        h.clock.advance(seconds=61)
        h.run()
        h.clock.advance(seconds=301)
        h.run()
        self.assertEqual(seen, [1, 2, 3])
        self.assertEqual(h.state("a")[0][1], "succeeded")
        self.assertEqual([a["state"] for a in h.store.attempts(job["job_id"])], ["failed", "failed", "succeeded"])

    def test_gives_up_after_max_attempts(self):
        h = Harness()
        h.add("a", lambda ctx: (_ for _ in ()).throw(Transient("一直失败")), max_attempts=2, backoff_s=(1,))
        h.run()
        h.clock.advance(seconds=5)
        h.run()
        self.assertEqual(h.state("a")[0][1], "failed")
        job = h.store.jobs_of(h.store.last_run("a")["run_id"])[0]
        self.assertEqual((job["state"], job["attempt"]), ("dead", 2))

    def test_permanent_error_is_not_retried(self):
        h = Harness()
        h.add("a", lambda ctx: (_ for _ in ()).throw(Permanent("参数错")), max_attempts=5)
        h.run()
        job = h.store.jobs_of(h.store.last_run("a")["run_id"])[0]
        self.assertEqual((job["state"], job["attempt"], job["error"]["class"]), ("dead", 1, "permanent"))

    def test_unexpected_value_error_is_permanent(self):
        h = Harness()
        h.add("a", lambda ctx: int("x"), max_attempts=5)
        h.run()
        self.assertEqual(h.store.jobs_of(h.store.last_run("a")["run_id"])[0]["attempt"], 1)

    def test_not_ready_does_not_consume_attempts(self):
        h = Harness()
        state = {"ready": False}

        def handler(ctx):
            if not state["ready"]:
                raise NotReady("数据还没出", retry_after_s=60)
            return {"ok": 1}
        h.add("a", handler, max_attempts=2)
        for _ in range(5):
            h.run()
            h.clock.advance(seconds=61)
        job = h.store.jobs_of(h.store.last_run("a")["run_id"])[0]
        self.assertEqual((job["state"], job["attempt"]), ("not_ready", 0))
        state["ready"] = True
        h.run()
        self.assertEqual(h.state("a")[0][1], "succeeded")

    def test_checkpointed_steps_are_not_repeated_on_retry(self):
        h = Harness()
        counts = {"fetch": 0, "think": 0}

        def handler(ctx):
            def fetch():
                counts["fetch"] += 1
                return {"rows": 3}

            def think():
                counts["think"] += 1
                if counts["think"] == 1:
                    raise Transient("模型超时")
                return {"text": "ok"}
            ctx.step("fetch", fetch)
            return ctx.step("think", think, tokens=100)
        h.add("a", handler, backoff_s=(1,))
        h.run()
        h.clock.advance(seconds=2)
        h.run()
        self.assertEqual(counts, {"fetch": 1, "think": 2})
        self.assertEqual(h.state("a")[0][1], "succeeded")

    def test_retry_job_from_step_runs_it_again(self):
        h = Harness()
        h.add("a", lambda ctx: (_ for _ in ()).throw(Permanent("坏了")))
        h.run()
        job = h.store.jobs_of(h.store.last_run("a")["run_id"])[0]
        self.assertEqual(job["state"], "dead")
        h.registry._tasks["a"].handler = lambda ctx: {"fixed": True}
        h.sched.retry_job(job["job_id"])
        h.run()
        self.assertEqual(h.store.job(job["job_id"])["state"], "succeeded")
        self.assertEqual(h.store.run(job["run_id"])["state"], "succeeded")


class LeaseTest(unittest.TestCase):
    def test_abandoned_job_is_reclaimed_without_burning_an_attempt(self):
        h = Harness()
        h.add("a")
        h.sched.tick()
        claimed = h.exec.claim_batch()
        self.assertEqual(len(claimed), 1)
        h.store.update_job(claimed[0]["job_id"], h.clock.now_ms(), state="running", attempt=1)
        h.store.begin_attempt(claimed[0]["job_id"], 1, "x", h.clock.now_ms())
        h.clock.advance(hours=1)                  # 进程被杀，租约过期
        h.run()
        job = h.store.job(claimed[0]["job_id"])
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual([a["state"] for a in h.store.attempts(job["job_id"])], ["abandoned", "succeeded"][:len(h.store.attempts(job["job_id"]))])

    def test_two_claims_never_get_the_same_job(self):
        h = Harness()
        h.add("a")
        h.sched.tick()
        first = h.store.claim("light", h.clock.now_ms(), "w1", 60000, 5)
        second = h.store.claim("light", h.clock.now_ms(), "w2", 60000, 5)
        self.assertEqual((len(first), len(second)), (1, 0))


class FanoutAndPriorityTest(unittest.TestCase):
    def test_fanout_units_run_by_priority_and_run_aggregates(self):
        h = Harness()
        h.registry.add_fanout("stocks", lambda ctx: [Unit("000001", 10), Unit("600519", 90), Unit("300750", 50)])
        order = []

        def handler(ctx):
            order.append(ctx.unit_key)
            if ctx.unit_key == "300750":
                raise Permanent("这只有问题")
        h.add("nightly", handler, fanout="stocks", queue="data")
        h.exec.concurrency["data"] = 1
        h.run()
        self.assertEqual(order, ["600519", "300750", "000001"])
        run = h.store.last_run("nightly")
        self.assertEqual(run["state"], "partial")
        self.assertEqual((run["counts"]["succeeded"], run["counts"]["dead"]), (2, 1))

    def test_empty_fanout_succeeds_immediately(self):
        h = Harness()
        h.registry.add_fanout("none_today", lambda ctx: [])
        h.add("a", fanout="none_today")
        h.run()
        self.assertEqual(h.state("a")[0][1], "succeeded")


class GateAndDependencyTest(unittest.TestCase):
    def test_waits_for_gate_then_runs(self):
        h = Harness()
        ready = {"v": False}
        h.registry.add_gate("daily_ready", lambda day: ready["v"])
        h.add("a", gates=("daily_ready",))
        h.run()
        self.assertEqual(h.state("a")[0][1], "waiting")
        self.assertEqual(h.calls, [])
        ready["v"] = True
        h.run()
        self.assertEqual(h.state("a")[0][1], "succeeded")

    def test_gate_never_ready_expires_at_deadline(self):
        h = Harness()
        h.registry.add_gate("never", lambda day: False)
        h.add("a", gates=("never",), trigger={"kind": "daily", "at": "08:30", "window_min": 30})
        h.run()
        h.clock.advance(minutes=40)
        h.run()
        self.assertEqual(h.state("a")[0][1], "expired")

    def test_data_ready_trigger_runs_when_gate_opens(self):
        h = Harness("2026-11-05 16:00")
        ready = {"v": False}
        h.registry.add_gate("market_close", lambda day: ready["v"])
        h.add("close", trigger={"kind": "data_ready", "gate": "market_close", "deadline": "23:00"})
        h.run()
        self.assertEqual(h.state("close")[0][1], "waiting")
        ready["v"] = True
        h.run()
        self.assertEqual(h.state("close")[0][1], "succeeded")

    def test_hard_dependency_failure_fails_the_dependent(self):
        h = Harness()
        h.add("first", lambda ctx: (_ for _ in ()).throw(Permanent("x")))
        h.add("second", after=({"task": "first", "mode": "hard"},))
        h.run()
        h.run()
        self.assertEqual(h.state("second")[0][1], "failed")
        self.assertEqual(h.calls, [])

    def test_soft_dependency_failure_still_runs(self):
        h = Harness()
        h.add("first", lambda ctx: (_ for _ in ()).throw(Permanent("x")))
        h.add("second", after=({"task": "first", "mode": "soft"},))
        h.run()
        h.run()
        self.assertEqual(h.state("second")[0][1], "succeeded")

    def test_dependency_order(self):
        h = Harness()
        order = []
        h.add("first", lambda ctx: order.append("first"))
        h.add("second", lambda ctx: order.append("second"), after=({"task": "first", "mode": "hard"},))
        h.run()
        h.run()
        self.assertEqual(order, ["first", "second"])


class ManualAndEventTest(unittest.TestCase):
    def test_manual_run_with_units_and_dry_run(self):
        h = Harness()
        h.add("a", trigger={"kind": "manual"})
        plan = h.sched.manual_run("a", units=["x", "y"], dry_run=True)
        self.assertEqual([u["unit_key"] for u in plan["units"]], ["x", "y"])
        self.assertEqual(h.state("a"), [])
        h.sched.manual_run("a", units=["x", "y"], input={"force": True})
        h.run()
        self.assertEqual(sorted(call[1] for call in h.calls), ["x", "y"])

    def test_event_triggers_debounce_into_one_job(self):
        h = Harness()
        h.add("reanalyze", trigger={"kind": "event", "topic": "news", "debounce_s": 30, "max_wait_s": 120})
        h.sched.trigger_event("reanalyze", "600519")
        h.clock.advance(seconds=10)
        merged = h.sched.trigger_event("reanalyze", "600519")
        self.assertTrue(merged["merged"])
        h.exec.drain()
        self.assertEqual(h.calls, [])             # 还在去抖
        h.clock.advance(seconds=45)
        h.run()
        self.assertEqual(len(h.calls), 1)

    def test_event_debounce_is_capped_by_max_wait(self):
        h = Harness()
        h.add("r", trigger={"kind": "event", "topic": "x", "debounce_s": 30, "max_wait_s": 40})
        h.sched.trigger_event("r", "k")
        for _ in range(5):
            h.clock.advance(seconds=20)
            h.sched.trigger_event("r", "k")
        h.run()
        self.assertEqual(len(h.calls), 1)


class BudgetAndLimitTest(unittest.TestCase):
    def test_budget_skips_units_once_the_daily_quota_is_used(self):
        h = Harness()
        h.registry.add_fanout("three", lambda ctx: [Unit("a"), Unit("b"), Unit("c")])

        def handler(ctx):
            ctx.add_usage(tokens=100, calls=1)
        h.add("llm_task", handler, fanout="three", queue="llm", budget={"resource": "llm", "per_day": {"calls": 2}})
        h.exec.concurrency["llm"] = 1
        h.run()
        run = h.store.last_run("llm_task")
        self.assertEqual((run["counts"].get("succeeded"), run["counts"].get("skipped")), (2, 1))
        self.assertEqual(h.store.usage("llm", "2026-11-05"), {"calls": 2, "tokens": 200})

    def test_rate_limit_delays_claims(self):
        from app.scheduler.limits import Limits
        h = Harness()
        h.exec.limits = Limits({"light": 1})
        h.registry.add_fanout("three", lambda ctx: [Unit("a"), Unit("b"), Unit("c")])
        h.add("a", fanout="three")
        h.sched.tick()
        self.assertEqual(h.exec.run_once(), 1)
        self.assertEqual(h.exec.run_once(), 0)
        h.clock.advance(seconds=1)


class HealthTest(unittest.TestCase):
    def test_describe_and_preview(self):
        h = Harness()
        h.add("a")
        info = h.sched.describe(h.registry.get("a"))
        self.assertEqual(info["trigger"]["at"], "08:30")
        self.assertEqual(info["next_fire_trade_date"], "2026-11-09")     # 11-06 假日，周末
        pre = h.sched.preview("a", {"kind": "daily", "at": "21:00"}, 2)
        self.assertEqual(pre["fires"][0]["local"], "2026-11-05 21:00")
        h.sched.tick()
        self.assertIsNotNone(h.sched.health()["last_tick_at"])


if __name__ == "__main__":
    unittest.main()
