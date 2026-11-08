import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cryptography.fernet import Fernet

os.environ["RELAY_MASTER_KEY"] = Fernet.generate_key().decode()

from app.notify import channels as chan
from app.scheduler.clock import FakeClock
from app.server import facts as F
from app.server import relay, security
from app.server.main import build
from app.server.store import ServerStore, hash_of


class Sink(chan.Adapter):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.fail = False

    def send(self, notification, channel):
        if self.fail:
            raise chan.DeliveryError("坏了", "transient")
        self.sent.append((chan.resolve_secret(channel["secret_ref"]), notification["title"]))
        return {"provider_msg_id": "x"}


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock("2026-11-05 10:00")
        self.store = ServerStore(":memory:")
        self.deps = {}
        self.app, self.runtime = build(self.store, self.clock, sched_path=":memory:", **self.deps)
        self.app.config["ARTIFACT_PRODUCERS"] = {}              # 测试里不去真取数
        self.c = self.app.test_client()
        self.sink = Sink()
        self._old = dict(chan.ADAPTERS)
        chan.ADAPTERS["wecom_webhook"] = self.sink
        made = self.c.post("/v1/devices").get_json()
        self.device, self.token = made["device_id"], made["token"]
        self.h = {"Authorization": "Bearer " + self.token}

    def tearDown(self):
        chan.ADAPTERS.clear()
        chan.ADAPTERS.update(self._old)
        from app.scheduler import runtime
        runtime.install(None)

    def put_relay(self, rev=1, alerts=None, channels=None):
        alerts = alerts if alerts is not None else [{"alert_id": "r1", "ts_code": "600036.SH", "side": "down", "price": 36.0, "confirm": "touch", "label": "招商银行", "channels": ["w1"]}]
        channels = channels if channels is not None else [{"channel_id": "w1", "type": "wecom_webhook", "secret": "https://hook.example/abc", "only_offline": True}]
        return self.c.put("/v1/relay/alerts", json={"rev": rev, "alerts": alerts, "channels": channels}, headers=self.h)

    def quote(self, price, age_ms=0):
        return {"600036.SH": {"price": price, "observed_at": self.clock.now_ms() - age_ms}}


class AuthTest(Base):
    def test_requires_valid_token(self):
        self.assertEqual(self.c.put("/v1/subscriptions", json={}).status_code, 401)
        self.assertEqual(self.c.put("/v1/subscriptions", json={}, headers={"Authorization": "Bearer " + self.device + ".wrong"}).status_code, 401)
        self.assertEqual(self.c.get("/v1/health").status_code, 200)

    def test_token_is_stored_hashed(self):
        stored = self.store.device(self.device)["token_hash"]
        self.assertNotIn(self.token.split(".", 1)[1], stored)

    def test_revoking_deletes_everything_of_that_device(self):
        self.put_relay()
        self.c.put("/v1/subscriptions", json={"rev": 1, "codes": [{"ts_code": "600036.SH", "tier": "hold"}]}, headers=self.h)
        self.assertEqual(self.c.delete("/v1/devices/me", headers=self.h).status_code, 200)
        self.assertEqual(self.store.relay_alerts(), [])
        self.assertEqual(self.store.subscription_union(self.clock.now_ms()), [])
        self.assertEqual(self.c.get("/v1/relay/events", headers=self.h).status_code, 401)

    def test_admin_interface_is_closed_without_a_token(self):
        os.environ.pop("ADMIN_TOKEN", None)
        self.assertEqual(self.c.get("/v1/admin/sched/health").status_code, 403)
        os.environ["ADMIN_TOKEN"] = "adm"
        try:
            self.assertEqual(self.c.get("/v1/admin/sched/health", headers={"Authorization": "Bearer nope"}).status_code, 401)
            ok = self.c.get("/v1/admin/sched/tasks", headers={"Authorization": "Bearer adm"})
            self.assertEqual(ok.status_code, 200)
            self.assertIn("relay.evaluate", {t["task_id"] for t in ok.get_json()["items"]})
        finally:
            os.environ.pop("ADMIN_TOKEN")


class SubscriptionTest(Base):
    def test_union_prefers_held_then_more_devices(self):
        other = self.c.post("/v1/devices").get_json()
        oh = {"Authorization": "Bearer " + other["token"]}
        self.c.put("/v1/subscriptions", json={"rev": 1, "codes": [{"ts_code": "000001.SZ", "tier": "other"}, {"ts_code": "600036.SH", "tier": "hold"}]}, headers=self.h)
        self.c.put("/v1/subscriptions", json={"rev": 1, "codes": [{"ts_code": "000001.SZ", "tier": "other"}, {"ts_code": "300750.SZ"}]}, headers=oh)
        union = self.store.subscription_union(self.clock.now_ms())
        self.assertEqual([r["ts_code"] for r in union], ["600036.SH", "000001.SZ", "300750.SZ"])
        self.assertEqual(union[1]["devices"], 2)

    def test_stale_rev_conflicts_and_bad_codes_are_rejected(self):
        self.assertEqual(self.c.put("/v1/subscriptions", json={"rev": 2, "codes": []}, headers=self.h).status_code, 200)
        stale = self.c.put("/v1/subscriptions", json={"rev": 2, "codes": []}, headers=self.h)
        self.assertEqual((stale.status_code, stale.get_json()["error"]["details"]["rev"]), (409, 2))
        self.assertEqual(self.c.put("/v1/subscriptions", json={"rev": 3, "codes": [{"ts_code": "abc"}]}, headers=self.h).status_code, 400)

    def test_inactive_devices_drop_out_of_the_union(self):
        self.c.put("/v1/subscriptions", json={"rev": 1, "codes": [{"ts_code": "600036.SH"}]}, headers=self.h)
        self.assertEqual(len(self.store.subscription_union(self.clock.now_ms() + 31 * 86400_000)), 0)


class RelayTest(Base):
    def test_sync_validation_and_secret_never_stored_in_clear(self):
        self.assertEqual(self.put_relay().get_json(), {"rev": 1, "accepted": 1})
        blob = self.store.relay_channels(self.device)["w1"]["secret_enc"]
        self.assertNotIn(b"hook.example", bytes(blob))
        self.assertEqual(security.decrypt(blob), "https://hook.example/abc")
        bad = self.put_relay(rev=2, alerts=[{"alert_id": "x", "ts_code": "600036.SH", "side": "sideways", "price": 1, "confirm": "touch"}])
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.put_relay(rev=3, channels=[{"channel_id": "w1", "type": "email", "secret": "x"}]).status_code, 400)

    def test_stale_rev_conflicts_and_unchanged_alerts_keep_their_state(self):
        self.put_relay(rev=5)
        relay.evaluate(self.store, self.quote(35.5), self.clock.now_ms())
        self.assertEqual(self.store.relay_alerts()[0]["state"], "fired")
        self.assertEqual(self.put_relay(rev=5).status_code, 409)
        self.put_relay(rev=6, channels=[{"channel_id": "w1", "type": "wecom_webhook", "only_offline": True}])      # 不带 secret：沿用已有的
        alert = self.store.relay_alerts()[0]
        self.assertEqual((alert["state"], alert["fire_seq"]), ("fired", 1))
        self.assertIn("w1", self.store.relay_channels(self.device))
        self.put_relay(rev=7, alerts=[{"alert_id": "r1", "ts_code": "600036.SH", "side": "down", "price": 35.0, "confirm": "touch", "channels": []}])
        self.assertEqual(self.store.relay_alerts()[0]["state"], "armed")           # 改了价位就重新待命

    def test_evaluate_fires_once_skips_stale_and_rearms(self):
        self.put_relay()
        now = self.clock.now_ms()
        self.assertEqual(relay.evaluate(self.store, self.quote(35.5, age_ms=300_000), now)["skipped"], 1)
        self.assertEqual(relay.evaluate(self.store, self.quote(36.5), now)["fired"], 0)
        self.assertEqual(relay.evaluate(self.store, self.quote(35.5), now)["fired"], 1)
        self.assertEqual(relay.evaluate(self.store, self.quote(35.4), now)["fired"], 0)
        events = self.c.get("/v1/relay/events?after=0", headers=self.h).get_json()["items"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"]["dedupe_key"], "relay:r1:2026-11-05")
        self.assertEqual(events[0]["event"]["payload"]["name"], "招商银行")

    def test_close_confirmation_uses_the_official_close_once_a_day(self):
        self.put_relay(alerts=[{"alert_id": "c1", "ts_code": "600036.SH", "side": "down", "price": 36.0, "confirm": "close", "label": "招商银行", "channels": []}])
        closes = {"600036.SH": ("2026-11-05", 35.6)}
        self.assertEqual(relay.evaluate(self.store, {}, self.clock.now_ms(), "close", closes)["fired"], 1)
        self.assertEqual(relay.evaluate(self.store, {}, self.clock.now_ms(), "close", closes)["checked"], 0)
        self.assertEqual(relay.evaluate(self.store, self.quote(30), self.clock.now_ms(), "touch")["checked"], 0)

    def fire(self):
        self.put_relay()
        relay.evaluate(self.store, self.quote(35.5), self.clock.now_ms())

    def test_offline_device_gets_the_external_push_with_decrypted_secret(self):
        self.fire()
        out = relay.push_due(self.store, self.clock.now_ms())
        self.assertEqual(out["pushed"], 1)
        self.assertEqual(self.sink.sent, [("https://hook.example/abc", "招商银行 跌到 36.00")])
        self.assertEqual(relay.push_due(self.store, self.clock.now_ms())["pushed"], 0)

    def test_online_device_gets_the_stream_first_and_external_push_only_after_the_ack_timeout(self):
        self.fire()
        self.c.get("/v1/relay/stream?once=1", headers=self.h)                # 连上事件流 = 在线
        self.assertEqual(relay.push_due(self.store, self.clock.now_ms())["waiting"], 1)
        self.assertEqual(self.sink.sent, [])
        self.clock.advance(seconds=30)
        self.c.get("/v1/relay/stream?once=1", headers=self.h)
        self.clock.advance(seconds=160)
        self.c.get("/v1/relay/stream?once=1", headers=self.h)
        self.assertEqual(relay.push_due(self.store, self.clock.now_ms())["pushed"], 1)

    def test_acked_event_is_never_pushed_externally(self):
        self.fire()
        self.c.get("/v1/relay/stream?once=1", headers=self.h)
        seq = self.c.get("/v1/relay/events", headers=self.h).get_json()["items"][0]["seq"]
        self.assertEqual(self.c.post("/v1/relay/ack", json={"seqs": [seq]}, headers=self.h).get_json()["acked"], 1)
        self.clock.advance(minutes=10)
        self.assertEqual(relay.push_due(self.store, self.clock.now_ms())["pushed"], 0)
        self.assertEqual(self.sink.sent, [])

    def test_always_on_channel_pushes_immediately_exactly_once(self):
        self.put_relay(channels=[{"channel_id": "w1", "type": "wecom_webhook", "secret": "s", "only_offline": False}])
        relay.evaluate(self.store, self.quote(35.5), self.clock.now_ms())
        self.c.get("/v1/relay/stream?once=1", headers=self.h)
        relay.push_due(self.store, self.clock.now_ms())
        relay.push_due(self.store, self.clock.now_ms())
        self.assertEqual(len(self.sink.sent), 1)

    def test_failed_push_is_retried_then_stops_after_three_attempts(self):
        self.fire()
        self.sink.fail = True
        for _ in range(5):
            relay.push_due(self.store, self.clock.now_ms())
        self.assertEqual(self.store.unpushed_events(), [])
        self.assertEqual(self.sink.sent, [])

    def test_stream_delivers_backlog_after_a_cursor(self):
        self.fire()
        body = self.c.get("/v1/relay/stream?once=1&after=0", headers=self.h).get_data(as_text=True)
        self.assertIn("event: relay", body)
        self.assertIn("price.alert.fired", body)
        self.assertNotIn("event: relay", self.c.get("/v1/relay/stream?once=1&after=999", headers=self.h).get_data(as_text=True))

    def test_devices_cannot_see_each_others_events(self):
        self.fire()
        other = self.c.post("/v1/devices").get_json()
        self.assertEqual(self.c.get("/v1/relay/events", headers={"Authorization": "Bearer " + other["token"]}).get_json()["items"], [])


class ArtifactTest(Base):
    def test_etag_and_304(self):
        self.store.put_artifact("stock.facts", "600036.SH", "2026-11-05", "close", "facts@1", {"facts": [1]}, self.clock.now_ms())
        first = self.c.get("/v1/artifacts/stock.facts/600036.SH?as_of=2026-11-05", headers=self.h)
        etag = first.headers["ETag"]
        self.assertEqual(first.get_json()["facts_hash"], hash_of({"facts": [1]}))
        again = self.c.get("/v1/artifacts/stock.facts/600036.SH", headers={**self.h, "If-None-Match": etag})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(self.c.get("/v1/artifacts/stock.facts/000001.SZ", headers=self.h).status_code, 404)

    def test_missing_artifact_is_produced_on_demand_and_then_cached(self):
        calls = []
        self.app.config["ARTIFACT_PRODUCERS"] = {"stock.facts": lambda store, subject, as_of, session: calls.append(subject) or store.put_artifact(
            "stock.facts", subject, "2026-11-05", "close", "facts@1", {"facts": []}, self.clock.now_ms())}
        self.assertEqual(self.c.get("/v1/artifacts/stock.facts/300750.SZ", headers=self.h).status_code, 200)
        self.assertEqual(self.c.get("/v1/artifacts/stock.facts/300750.SZ", headers=self.h).status_code, 200)
        self.assertEqual(calls, ["300750.SZ"])

    def test_batch_manifest_and_not_found(self):
        self.store.put_artifact("stock.facts", "a.SH", "2026-11-05", "close", "facts@1", {"x": 1}, 1)
        out = self.c.post("/v1/artifacts:batchGet", json={"items": [{"kind": "stock.facts", "subject": "a.SH"}, {"kind": "stock.bars", "subject": "b.SH"}]}, headers=self.h).get_json()
        self.assertEqual([i["found"] for i in out["items"]], [True, False])
        manifest = self.c.get("/v1/manifest?kind=stock.facts&as_of=2026-11-05", headers=self.h).get_json()
        self.assertEqual(manifest["items"][0]["subject"], "a.SH")
        self.assertEqual(self.c.get("/v1/artifacts/stock.bars/zzz", headers=self.h).status_code, 404)

    def test_news_collection_merges_by_item_id_and_keeps_only_a_week(self):
        now = self.clock.now_ms()
        raw = lambda *rows: (lambda: [{"source": "exchange", "title": t, "url": u, "time": ts} for t, u, ts in rows])
        F.produce_news(self.store, "600036.SH", "2026-11-05", now, raw(("关于收到监管函的公告", "a", "2026-11-05 09:00:00"), ("老公告", "o", "2026-10-20 09:00:00")))
        made = F.produce_news(self.store, "600036.SH", "2026-11-05", now + 1000, raw(("关于收到监管函的公告", "a", "2026-11-05 09:00:00"), ("关于回购股份的公告", "b", "2026-11-05 10:00:00")))
        titles = [i["title"] for i in made["payload"]["items"]]
        self.assertEqual(titles, ["关于收到监管函的公告", "关于回购股份的公告"])
        self.assertEqual(made["payload"]["items"][0]["importance"], "P0")
        got = self.c.get("/v1/artifacts/stock.news/600036.SH", headers=self.h).get_json()
        self.assertEqual(len(got["payload"]["items"]), 2)

    def test_facts_payload_drops_headlines_and_model_output(self):
        payload = F.facts_payload({"facts": [], "headlines": [{"x": "模型写的"}], "events": [], "rule": {}, "context": {}, "candidates": [], "unavailable": []})
        self.assertNotIn("headlines", payload)


class TaskTest(Base):
    def test_ready_probe_publishes_markers_and_ready_endpoint(self):
        app, runtime = build(self.store, self.clock, sched_path=":memory:", prober=lambda gate, date: gate == "daily_bars")
        self.clock.set("2026-11-05 15:20")
        runtime.sched.tick()
        runtime.exec.drain()
        gates = app.test_client().get("/v1/ready?date=2026-11-05").get_json()["gates"]
        self.assertTrue(gates["daily_bars"]["ready"])
        self.assertFalse(gates["moneyflow"]["ready"])

    def test_warm_fans_out_over_the_union_and_skips_fresh_artifacts(self):
        made = []

        def facts(store, code, day, session, now_ms):
            made.append(code)
            return store.put_artifact("stock.facts", code, day, session, F.FACTS_SCHEMA, {"facts": [code]}, now_ms)
        snapshot = lambda store, day, now: store.put_artifact("market.snapshot", "market", day, "close", F.SNAPSHOT_SCHEMA, {"conclusion": {}}, now)
        app, runtime = build(self.store, self.clock, sched_path=":memory:", facts=facts, snapshot=snapshot, prober=lambda g, d: True)
        c = app.test_client()
        t = c.post("/v1/devices").get_json()
        h = {"Authorization": "Bearer " + t["token"]}
        c.put("/v1/subscriptions", json={"rev": 1, "codes": [{"ts_code": "000001.SZ"}, {"ts_code": "600036.SH", "tier": "hold"}]}, headers=h)
        self.store.set_ready("2026-11-05", "daily_bars", True, self.clock.now_ms())
        self.clock.set("2026-11-05 16:00")
        runtime.sched.tick()
        runtime.exec.drain()
        runtime.sched.tick()
        self.assertEqual(made, ["600036.SH", "000001.SZ"])                         # 持仓的先算
        self.assertEqual(sorted(i["subject"] for i in self.store.manifest("stock.facts", "2026-11-05")), ["000001.SZ", "600036.SH"])
        run = runtime.sched.store.last_run("stock.facts.warm")
        runtime.sched.manual_run("stock.facts.warm", period_key="manual:again", input={"trade_date": "2026-11-05"})
        runtime.sched.tick()
        runtime.exec.drain()
        self.assertEqual(len(made), 2)                                              # 同一天同一口径已经有了，不重算
        self.assertIsNotNone(self.store.artifact("market.snapshot", "market", "2026-11-05"))
        self.assertIsNotNone(run)

    def test_every_server_task_declares_why_it_is_on_the_server(self):
        for spec in self.runtime.sched.tasks():
            self.assertEqual(spec.placement, "server")
            self.assertTrue(spec.placement_reason)
        self.assertEqual(self.runtime.sched.registry.unknown_gates(), [])


if __name__ == "__main__":
    unittest.main()
