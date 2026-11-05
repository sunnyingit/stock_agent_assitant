import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from flask import Flask

from app.scheduler import runtime
from app.scheduler.api import bp
from test_scheduler_core import Harness


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.add("a", lambda ctx: ctx.result({"big": "x" * 10}, "detail") and {"ok": 1})
        self.h.add("alerts", trigger={"kind": "session_interval", "every_min": 3}, min_interval_min=1)
        self.rt = runtime.Runtime(self.h.sched, self.h.exec)
        runtime.install(self.rt)
        app = Flask(__name__)
        app.register_blueprint(bp)
        self.client = app.test_client()

    def tearDown(self):
        runtime.install(None)

    def test_list_and_detail(self):
        items = self.client.get("/sched/tasks").get_json()["items"]
        self.assertEqual({item["task_id"] for item in items}, {"a", "alerts"})
        detail = self.client.get("/sched/tasks/a").get_json()
        self.assertEqual(detail["trigger"]["at"], "08:30")
        self.assertEqual(self.client.get("/sched/tasks/none").status_code, 404)

    def test_config_override_validation_and_reset(self):
        resp = self.client.put("/sched/tasks/a/config", json={"trigger": {"kind": "daily", "at": "21:00"}})
        self.assertEqual(resp.get_json()["trigger"]["at"], "21:00")
        self.assertTrue(resp.get_json()["overridden"])
        bad = self.client.put("/sched/tasks/a/config", json={"trigger": {"kind": "daily", "at": "25:99"}})
        self.assertEqual((bad.status_code, bad.get_json()["error"]["code"]), (400, "invalid_trigger"))
        wrong_kind = self.client.put("/sched/tasks/a/config", json={"trigger": {"kind": "cron", "expr": "0 8 * * *"}})
        self.assertEqual(wrong_kind.status_code, 400)
        floor = self.client.put("/sched/tasks/alerts/config", json={"trigger": {"kind": "session_interval", "every_min": 5}})
        self.assertEqual(floor.status_code, 200)
        self.assertEqual(self.client.delete("/sched/tasks/a/config").get_json()["trigger"]["at"], "08:30")

    def test_pause_and_resume(self):
        self.assertFalse(self.client.post("/sched/tasks/a/pause").get_json()["enabled"])
        self.assertTrue(self.client.post("/sched/tasks/a/resume").get_json()["enabled"])

    def test_preview_has_no_side_effects(self):
        out = self.client.post("/sched/tasks/a/preview", json={"trigger": {"kind": "daily", "at": "21:00"}, "count": 3}).get_json()
        self.assertEqual(len(out["fires"]), 3)
        self.assertEqual(self.h.store.runs(), [])

    def test_manual_run_flow_and_job_drilldown(self):
        resp = self.client.post("/sched/tasks/a/run", json={"units": ["x"]})
        self.assertEqual(resp.status_code, 202)
        self.rt.sched.tick()
        self.h.exec.drain()
        self.rt.sched.tick()
        run_id = resp.get_json()["run"]["run_id"]
        run = self.client.get(f"/sched/runs/{run_id}").get_json()
        self.assertEqual(run["state"], "succeeded")
        jobs = self.client.get(f"/sched/runs/{run_id}/jobs").get_json()["items"]
        job = self.client.get(f"/sched/jobs/{jobs[0]['job_id']}").get_json()
        self.assertEqual(job["output"]["summary"], {"ok": 1})
        ref = job["output"]["refs"][0]["ref"]
        content = self.client.get(f"/sched/results/{ref}").get_json()
        self.assertEqual(content["content"], {"big": "x" * 10})
        attempts = self.client.get(f"/sched/jobs/{job['job_id']}/attempts").get_json()["items"]
        self.assertEqual(attempts[0]["state"], "succeeded")

    def test_runs_pagination_and_filter(self):
        for index in range(5):
            self.h.sched.manual_run("a", units=["u"], period_key=f"manual:{index}")
        first = self.client.get("/sched/runs?limit=2").get_json()
        self.assertEqual((len(first["items"]), first["next_cursor"]), (2, "2"))
        last = self.client.get("/sched/runs?limit=2&cursor=4").get_json()
        self.assertEqual((len(last["items"]), last["next_cursor"]), (1, None))
        self.assertEqual(len(self.client.get("/sched/runs?state=succeeded").get_json()["items"]), 0)

    def test_duplicate_manual_period_conflicts(self):
        self.assertEqual(self.client.post("/sched/tasks/a/run", json={"period_key": "manual:1"}).status_code, 202)
        self.assertEqual(self.client.post("/sched/tasks/a/run", json={"period_key": "manual:1"}).status_code, 409)

    def test_dry_run_creates_nothing(self):
        out = self.client.post("/sched/tasks/a/run", json={"dry_run": True}).get_json()
        self.assertTrue(out["dry_run"])
        self.assertEqual(self.h.store.runs(), [])

    def test_health_and_tick(self):
        out = self.client.post("/sched/tick?reason=resume").get_json()
        self.assertEqual(out["reason"], "resume")
        health = self.client.get("/sched/health").get_json()
        self.assertTrue(health["leader"]["me"])
        self.assertIn("light", health["queue_depth"])

    def test_second_process_does_not_tick_while_leader_alive(self):
        self.rt.tick()
        other = runtime.Runtime(self.h.sched, type(self.h.exec)(self.h.sched, owner="other"))
        self.assertEqual(other.tick(), {"leader": False})
        self.h.clock.advance(minutes=5)
        self.assertNotEqual(other.tick(), {"leader": False})

    def test_budget_endpoint(self):
        self.h.store.add_usage("llm", "2026-11-05", calls=3)
        self.assertEqual(self.client.get("/sched/budget?resource=llm&window=2026-11-05").get_json()["used"], {"calls": 3})


if __name__ == "__main__":
    unittest.main()
