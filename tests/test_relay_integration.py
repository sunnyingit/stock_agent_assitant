"""客户端和服务端接起来：本机建提醒 → 同步到服务端 → 服务端触发 → 本机拉回写进收件箱。服务端的 Flask 测试客户端当传输层。"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cryptography.fernet import Fernet

os.environ.setdefault("RELAY_MASTER_KEY", Fernet.generate_key().decode())

from app.notify import channels as chan
from app.notify import relay_client, rules
from app.notify.pipeline import Notifier
from app.notify.store import NotifyStore
from app.scheduler.clock import FakeClock
from app.server import relay
from app.server.main import build
from app.server.store import ServerStore
from test_notify_core import Recorder
from test_server import Sink


class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock("2026-11-05 10:00")
        self.server_store = ServerStore(":memory:")
        self.app, self.runtime = build(self.server_store, self.clock, sched_path=":memory:")
        self.app.config["ARTIFACT_PRODUCERS"] = {}
        self.web = self.app.test_client()
        self.sink = Sink()
        self.rec = Recorder()
        self._old = dict(chan.ADAPTERS)
        chan.ADAPTERS["wecom_webhook"] = self.sink
        chan.ADAPTERS["test"] = self.rec
        self.n = Notifier(NotifyStore(":memory:"), self.clock)
        self.n.store.put_channel({"channel_id": "ch_desktop", "enabled": 0})
        self.n.store.put_channel({"channel_id": "ch_test", "type": "test", "name": "本机渠道"})
        self.n.store.put_channel({"channel_id": "ch_wecom", "type": "wecom_webhook", "name": "企业微信", "secret_ref": "literal:https://hook.example/me", "relay": 1})
        self.token = ""
        self.client = relay_client.RelayClient(self.n, http=self.http, token_loader=lambda: self.token)
        relay_client.save_token = lambda token: setattr(self, "token", token) or "memory"
        self.n.store.put_rule({"rule_id": "r1", "type": "price_alert", "subject_kind": "stock", "subject_id": "600036.SH", "origin_ref": "1",
                               "params": {"side": "down", "price": 36.0, "confirm": "touch", "name": "招商银行"}}, self.clock.now_ms())

    def tearDown(self):
        chan.ADAPTERS.clear()
        chan.ADAPTERS.update(self._old)

    def http(self, method, url, headers, body, timeout=8):
        path = url.split("//", 1)[1].split("/", 1)[1]
        resp = self.web.open("/" + path, method=method, json=body, headers=headers)
        return resp.status_code, resp.get_json(silent=True) or {}

    def connect(self):
        self.client.register("http://server.test")

    def quote(self, price):
        return {"600036.SH": {"price": price, "observed_at": self.clock.now_ms()}}

    def test_disabled_by_default_and_registration_stores_the_token_outside_the_db(self):
        self.assertFalse(self.client.enabled())
        self.assertEqual(self.client.sync_alerts(), {"skipped": "disabled"})
        self.connect()
        self.assertTrue(self.client.enabled())
        self.assertNotIn(self.token, str(self.n.store.settings()))

    def test_full_round_trip_while_the_computer_is_off(self):
        self.connect()
        self.assertEqual(self.client.sync_alerts()["synced"], 1)
        self.assertEqual(self.client.sync_alerts(), {"skipped": "unchanged"})
        alert = self.server_store.relay_alerts()[0]
        self.assertEqual((alert["alert_id"], alert["label"], alert["channels"]), ("r1", "招商银行", ["ch_wecom"]))
        self.assertIn("ch_wecom", self.server_store.relay_channels(self.client.config()["device_id"]))
        # 电脑关了：客户端不跑，只有服务端在评估和外发
        self.clock.advance(minutes=3)
        relay.evaluate(self.server_store, {"600036.SH": {"price": 35.5, "observed_at": self.clock.now_ms()}}, self.clock.now_ms())
        relay.push_due(self.server_store, self.clock.now_ms())
        self.assertEqual(self.sink.sent, [("https://hook.example/me", "招商银行 跌到 36.00")])
        # 电脑开机：拉回事件，进收件箱，不会再外发一次
        out = self.client.pull_events()
        self.assertEqual(out, {"events": 1, "created": 1})
        self.assertEqual(self.n.store.inbox()[0]["title"], "招商银行 跌到 36.00")
        self.assertEqual(self.n.store.rule_state("r1")["state"], "fired")
        self.assertEqual(self.client.pull_events(), {"events": 0, "created": 0})
        self.assertEqual(len(self.sink.sent), 1)

    def test_when_the_server_is_reachable_the_local_evaluator_defers_so_nothing_fires_twice(self):
        self.connect()
        self.client.sync_alerts()
        self.client.pull_events()
        self.assertTrue(self.client.authoritative())
        # 本机降级评估时和服务端用同一把去重键：两边先后触发也只入箱一次
        rules.evaluate_price_alerts(self.n, self.quote(35.5), self.clock.now_ms(), relay_mode=True)
        relay.evaluate(self.server_store, self.quote(35.5), self.clock.now_ms())
        self.client.pull_events()
        titles = [item["title"] for item in self.n.store.inbox()]
        self.assertEqual(titles, ["招商银行 跌到 36.00"])

    def test_server_down_falls_back_to_local_evaluation(self):
        self.connect()
        self.client.pull_events()
        self.clock.advance(minutes=11)             # 超过 10 分钟没拉成功
        self.assertFalse(self.client.authoritative())

    def test_secrets_are_uploaded_once_and_only_for_opted_in_channels(self):
        self.n.store.put_channel({"channel_id": "ch_bark", "type": "bark", "name": "手机", "secret_ref": "literal:barkkey", "relay": 0})
        self.connect()
        self.client.sync_alerts()
        server_channels = self.server_store.relay_channels(self.client.config()["device_id"])
        self.assertEqual(set(server_channels), {"ch_wecom"})                    # 没勾选「用于离线转发」的不上传
        calls = []
        original = self.client.http
        self.client.http = lambda m, u, h, b, timeout=8: calls.append(b) or original(m, u, h, b)
        self.n.store.put_rule({"rule_id": "r2", "type": "price_alert", "subject_kind": "stock", "subject_id": "000001.SZ", "origin_ref": "2",
                               "params": {"side": "up", "price": 12.0, "confirm": "touch", "name": "平安银行"}}, self.clock.now_ms())
        self.client.sync_alerts()
        self.assertNotIn("secret", str(calls))                                    # 密钥没变，不重复上传

    def test_subscriptions_carry_only_codes_and_tiers(self):
        self.connect()
        codes = [{"ts_code": "600036.SH", "tier": "hold"}, {"ts_code": "000001.SZ", "tier": "other"}]
        self.assertEqual(self.client.sync_subscriptions(codes)["synced"], 2)
        self.assertEqual(self.client.sync_subscriptions(codes), {"skipped": "unchanged"})
        self.assertEqual([r["ts_code"] for r in self.server_store.subscription_union(self.clock.now_ms())], ["600036.SH", "000001.SZ"])

    def test_revoke_wipes_the_server_side(self):
        self.connect()
        self.client.sync_alerts()
        self.assertTrue(self.client.revoke())
        self.assertEqual(self.server_store.relay_alerts(), [])
        self.assertFalse(self.client.enabled())


if __name__ == "__main__":
    unittest.main()
