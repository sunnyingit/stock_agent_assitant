"""每个定时任务跑完都有回执进收件箱：客户端的直接写，服务端的经事件流拉回来。"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cryptography.fernet import Fernet

os.environ.setdefault("RELAY_MASTER_KEY", Fernet.generate_key().decode())

from app.notify import pipeline, relay_client
from app.notify.pipeline import Notifier
from app.notify.store import NotifyStore
from app.scheduler import notify_hook, report
from app.scheduler.errors import Permanent
from app.scheduler.clock import FakeClock
from app.server.main import build
from app.server.store import ServerStore
from test_scheduler_core import Harness


class ClientReceipts(unittest.TestCase):
    def setUp(self):
        self.h = Harness("2026-11-05 08:35")
        self.n = Notifier(NotifyStore(":memory:"), self.h.clock)
        self.n.store.put_channel({"channel_id": "ch_desktop", "enabled": 1})
        pipeline.install(self.n)
        self.h.sched.listeners.append(lambda run, spec: notify_hook.on_run_finished(run, spec, self.h.sched))

    def tearDown(self):
        pipeline.install(None)

    def receipts(self):
        return self.n.store.events("task.completed")

    def test_a_finished_run_leaves_a_receipt_in_the_inbox_without_popping_up(self):
        self.h.add("market.pre", lambda ctx: {"headline": "中性偏强"}, trigger={"kind": "daily", "at": "08:30"})
        self.h.run()
        inbox = self.n.store.inbox()
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["title"], "定时任务成功：market.pre")
        self.assertIn("本机 · 成功 · 定时", inbox[0]["body"])
        self.assertIn("中性偏强", inbox[0]["body"])
        self.assertEqual(self.n.desktop_pending(), [])                     # 回执只进收件箱，不弹桌面通知

    def test_failures_are_reported_by_task_failed_not_duplicated_as_completed(self):
        self.h.add("boom", lambda ctx: (_ for _ in ()).throw(Permanent("坏了")), trigger={"kind": "daily", "at": "08:30"})
        self.h.run()
        self.assertEqual(self.receipts(), [])
        self.assertEqual(len(self.n.store.events("task.failed")), 1)

    def test_a_missed_period_also_leaves_a_receipt(self):
        self.h.add("brief", trigger={"kind": "daily", "at": "08:30", "window_min": 10}, misfire="skip")
        self.h.sched.tick()                                               # 08:35 装上，今天 08:30 的在装上之前：不算
        self.h.clock.set("2026-11-09 10:00")
        self.h.run()                                                      # 11-09 08:30 没赶上，也过了窗口
        states = [e["payload"]["state"] for e in self.receipts()]
        self.assertIn("expired", states)

    def test_manual_runs_are_labelled_and_never_collapsed(self):
        self.h.add("a", trigger={"kind": "manual"})
        self.h.sched.manual_run("a", period_key="manual:1")
        self.h.sched.manual_run("a", period_key="manual:2")
        self.h.run()
        self.assertEqual([e["payload"]["trigger_text"] for e in self.receipts()], ["手动", "手动"])

    def test_frequent_tasks_record_only_the_first_success_of_the_day(self):
        self.h.add("alerts", trigger={"kind": "session_interval", "every_min": 3}, misfire="skip")
        for minute in ("10:00", "10:03", "10:06", "10:09", "10:12"):
            self.h.clock.set("2026-11-05 " + minute)
            self.h.run()
        self.assertEqual(len(self.receipts()), 1)
        self.h.clock.set("2026-11-09 10:00")                               # 11-06 是假日，下一个交易日
        self.h.run()
        self.assertEqual(len(self.receipts()), 2)                         # 第二天再记一次

    def test_twice_a_day_tasks_are_not_treated_as_frequent(self):
        self.assertFalse(report.is_frequent({"kind": "cron", "expr": "35 8,21 * * *"}))
        self.assertTrue(report.is_frequent({"kind": "cron", "expr": "*/10 7-21 * * *"}))
        self.assertTrue(report.is_frequent({"kind": "cron", "expr": "* * * * *"}))
        self.assertFalse(report.is_frequent({"kind": "daily", "at": "08:30"}))

    def test_every_receipt_carries_duration_counts_and_a_link_to_the_run(self):
        self.h.registry.add_fanout("two", lambda ctx: [__import__("app.scheduler.spec", fromlist=["Unit"]).Unit("a"), __import__("app.scheduler.spec", fromlist=["Unit"]).Unit("b")])
        self.h.add("multi", fanout="two", trigger={"kind": "daily", "at": "08:30"})
        self.h.run()
        payload = self.receipts()[0]["payload"]
        self.assertEqual((payload["jobs"], payload["succeeded"], payload["host"]), (2, 2, "client"))
        item = self.n.store.inbox()[0]
        self.assertEqual(item["link"]["run_id"], payload["run_id"])
        self.assertIn("单元 2/2", item["body"])


class ServerReceipts(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock("2026-11-05 15:20")
        self.sstore = ServerStore(":memory:")
        self.app, self.runtime = build(self.sstore, self.clock, sched_path=":memory:", prober=lambda g, d: g == "daily_bars")
        self.app.config["ARTIFACT_PRODUCERS"] = {}
        self.web = self.app.test_client()
        self.n = Notifier(NotifyStore(":memory:"), self.clock)
        self.n.store.put_channel({"channel_id": "ch_desktop", "enabled": 0})
        self.token = ""
        self.client = relay_client.RelayClient(self.n, http=self.http, token_loader=lambda: self.token)
        relay_client.save_token = lambda token: setattr(self, "token", token) or "memory"
        self.client.register("http://server.test")

    def http(self, method, url, headers, body, timeout=8):
        resp = self.web.open("/" + url.split("//", 1)[1].split("/", 1)[1], method=method, json=body, headers=headers)
        return resp.status_code, resp.get_json(silent=True) or {}

    def run_server(self):
        self.runtime.sched.tick()
        self.runtime.exec.drain()
        self.runtime.sched.tick()

    def test_server_task_runs_show_up_in_the_clients_inbox(self):
        self.run_server()
        out = self.client.pull_system_events()
        self.assertGreater(out["created"], 0)
        titles = [i["title"] for i in self.n.store.inbox()]
        self.assertIn("定时任务成功：数据就绪探测", titles)
        body = next(i["body"] for i in self.n.store.inbox() if i["title"].endswith("数据就绪探测"))
        self.assertTrue(body.startswith("服务端 · 成功 · 定时"))

    def test_pulling_twice_does_not_duplicate_and_the_cursor_advances(self):
        self.run_server()
        first = self.client.pull_system_events()
        again = self.client.pull_system_events()
        self.assertEqual(again["created"], 0)
        self.assertGreater(self.client.config()["sys_cursor"], 0)
        self.assertEqual(first["events"] > 0, True)

    def test_a_failing_server_task_arrives_as_task_failed(self):
        from app.scheduler.errors import Permanent
        spec = self.runtime.sched.registry.get("server.purge")
        spec.handler = lambda ctx: (_ for _ in ()).throw(Permanent("库坏了"))
        self.runtime.sched.manual_run("server.purge", period_key="manual:x")
        self.run_server()
        self.client.pull_system_events()
        failed = self.n.store.events("task.failed")
        self.assertTrue(failed)
        self.assertEqual({e["payload"]["first_error"] for e in failed}, {"库坏了"})
        self.assertEqual({e["payload"]["task_id"] for e in failed}, {"server.purge"})

    def test_feed_requires_a_device_token_and_first_pull_only_covers_the_last_day(self):
        self.assertEqual(self.web.get("/v1/system/events").status_code, 401)
        self.run_server()
        self.clock.advance(days=2)
        self.sstore.add_system_event({"dedupe_key": "x", "type": "task.completed", "payload": {"task_id": "t", "state": "succeeded"}}, self.clock.now_ms())
        items = self.web.get("/v1/system/events", headers={"Authorization": "Bearer " + self.token}).get_json()["items"]
        self.assertEqual([i["event"]["dedupe_key"] for i in items], ["x"])

    def test_disabled_relay_pulls_nothing(self):
        self.client.save(enabled=False)
        self.assertEqual(self.client.pull_system_events(), {"skipped": "disabled"})


if __name__ == "__main__":
    unittest.main()
