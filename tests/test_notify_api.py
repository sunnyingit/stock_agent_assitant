import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from flask import Flask

from app.notify import channels as chan
from app.notify import pipeline
from app.notify.api import bp
from app.notify.pipeline import Notifier
from app.notify.store import NotifyStore
from app.scheduler.clock import FakeClock
from test_notify_core import Recorder


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock("2026-11-05 10:00")
        self._old = dict(chan.ADAPTERS)
        self.rec = Recorder()
        chan.ADAPTERS["test"] = self.rec
        self.n = Notifier(NotifyStore(":memory:"), self.clock)
        pipeline.install(self.n)
        app = Flask(__name__)
        app.register_blueprint(bp)
        self.c = app.test_client()

    def tearDown(self):
        pipeline.install(None)
        chan.ADAPTERS.clear()
        chan.ADAPTERS.update(self._old)

    def publish(self, key="1"):
        return self.n.publish("price.alert.fired", {"kind": "stock", "id": "1"},
                              {"alert_id": key, "ts_code": "1", "name": "甲", "side": "up", "trigger_price": 1, "observed_price": 2}, key)

    def test_inbox_read_flow(self):
        self.publish("1")
        self.publish("2")
        body = self.c.get("/notify/inbox").get_json()
        self.assertEqual((len(body["items"]), body["unread"]), (2, 2))
        first = body["items"][0]["notif_id"]
        self.assertEqual(self.c.post("/notify/inbox/read", json={"ids": [first]}).get_json()["unread"], 1)
        self.assertEqual(len(self.c.get("/notify/inbox?unread=1").get_json()["items"]), 1)
        self.assertEqual(self.c.post("/notify/inbox/read", json={"all": True}).get_json()["unread"], 0)
        detail = self.c.get(f"/notify/inbox/{first}").get_json()
        self.assertEqual(detail["event"]["type"], "price.alert.fired")
        self.assertEqual(self.c.get("/notify/inbox/nope").status_code, 404)

    def test_pagination(self):
        for i in range(5):
            self.publish(str(i))
        page = self.c.get("/notify/inbox?limit=2").get_json()
        self.assertEqual((len(page["items"]), page["next_cursor"]), (2, "2"))
        self.assertIsNone(self.c.get("/notify/inbox?limit=2&cursor=4").get_json()["next_cursor"])

    def test_channel_validation_and_no_secret_echo(self):
        self.assertEqual(self.c.put("/notify/channels/x", json={"type": "nope"}).status_code, 400)
        self.assertEqual(self.c.put("/notify/channels/x", json={"type": "bark", "secret_ref": "file:/etc/passwd"}).status_code, 400)
        ok = self.c.put("/notify/channels/ch_bark", json={"type": "bark", "name": "手机", "secret_ref": "env:BARK_KEY_FOR_TEST"})
        self.assertEqual(ok.status_code, 200)
        view = ok.get_json()
        self.assertEqual((view["has_secret"], view["secret_ref"]), (False, "env:BARK_KEY_FOR_TEST"))
        self.assertNotIn("secret", {key for key in view if key != "secret_ref" and key != "has_secret"})
        self.assertEqual(self.c.delete("/notify/channels/ch_bark").status_code, 200)
        self.assertEqual(self.c.delete("/notify/channels/ch_bark").status_code, 404)

    def test_test_message(self):
        self.c.put("/notify/channels/ch_t", json={"type": "test", "name": "t"})
        self.assertTrue(self.c.post("/notify/channels/ch_t/test").get_json()["ok"])
        self.assertEqual(len(self.rec.sent), 1)
        self.assertIsNotNone(self.n.store.channel("ch_t")["verified_at"])
        self.rec.fail = ("坏了", "permanent")
        out = self.c.post("/notify/channels/ch_t/test").get_json()
        self.assertEqual((out["ok"], out["class"]), (False, "permanent"))

    def test_settings_validation(self):
        self.assertEqual(self.c.put("/notify/settings", json={"quiet_hours": {"from": "x", "to": "08:00"}}).status_code, 400)
        out = self.c.put("/notify/settings", json={"quiet_hours": {"from": "23:00", "to": "07:00"}}).get_json()
        self.assertEqual(out["quiet_hours"]["from"], "23:00")

    def test_rule_crud_and_validation(self):
        bad = self.c.post("/notify/rules", json={"type": "price_alert", "subject": {"id": "600036.SH"}, "params": {"side": "sideways", "price": 3}})
        self.assertEqual(bad.status_code, 400)
        made = self.c.post("/notify/rules", json={"type": "price_alert", "subject": {"id": "600036.SH"}, "params": {"side": "down", "price": 36, "confirm": "touch"}})
        self.assertEqual(made.status_code, 201)
        rid = made.get_json()["rule_id"]
        move = self.c.post("/notify/rules", json={"type": "price_move", "subject": {"id": "1"}, "params": {"threshold": 7}}).get_json()
        self.assertEqual(move["rearm"]["kind"], "next_day")
        edited = self.c.put(f"/notify/rules/{rid}", json={"params": {"side": "down", "price": 35, "confirm": "touch"}}).get_json()
        self.assertEqual(edited["params"]["price"], 35)
        listed = self.c.get("/notify/rules?type=price_alert").get_json()["items"]
        self.assertEqual(listed[0]["state"]["state"], "armed")
        self.assertEqual(self.c.post(f"/notify/rules/{rid}/rearm").get_json()["state"], "armed")
        self.assertEqual(self.c.delete(f"/notify/rules/{rid}").status_code, 200)
        self.assertEqual(self.c.delete(f"/notify/rules/{rid}").status_code, 404)

    def test_desktop_pull_and_ack_endpoints(self):
        self.publish()
        items = self.c.get("/notify/desktop/pending").get_json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(self.c.post("/notify/desktop/ack", json={"delivery_ids": [items[0]["delivery_id"]]}).get_json()["acked"], 1)
        self.assertEqual(self.c.get("/notify/desktop/pending").get_json()["items"], [])

    def test_relay_status_and_validation(self):
        self.assertFalse(self.c.get("/notify/relay").get_json()["enabled"])
        self.assertEqual(self.c.post("/notify/relay", json={"url": "ftp://x"}).status_code, 400)
        self.assertEqual(self.c.post("/notify/relay", json={"url": "http://127.0.0.1:9"}).status_code, 502)

    def test_types_listing(self):
        kinds = {item["type"] for item in self.c.get("/notify/types").get_json()["items"]}
        self.assertIn("announcement.published", kinds)


if __name__ == "__main__":
    unittest.main()
