import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.notify import channels as chan
from app.notify import policy, rules
from app.notify.pipeline import Notifier
from app.notify.store import NotifyStore
from app.scheduler.clock import FakeClock, to_ms


class Recorder(chan.Adapter):
    def __init__(self):
        super().__init__()
        self.sent, self.fail = [], None

    def send(self, notification, channel):
        if self.fail:
            raise chan.DeliveryError(self.fail[0], self.fail[1])
        self.sent.append((channel["channel_id"], notification["title"]))
        return {"provider_msg_id": "m1"}


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock("2026-11-05 10:00")
        self.rec = Recorder()
        self._old = dict(chan.ADAPTERS)
        chan.ADAPTERS["test"] = self.rec
        self.n = Notifier(NotifyStore(":memory:"), self.clock)
        self.n.store.put_channel({"channel_id": "ch_test", "type": "test", "name": "测试", "min_severity": "normal"})
        self.n.store.put_channel({"channel_id": "ch_desktop", "enabled": 0})      # 桌面是拉取式的，单独测

    def tearDown(self):
        chan.ADAPTERS.clear()
        chan.ADAPTERS.update(self._old)

    def alert(self, key="1", **extra):
        payload = {"alert_id": "1", "ts_code": "600036.SH", "name": "招商银行", "side": "down", "trigger_price": 36.0, "observed_price": 35.9, **extra}
        return self.n.publish("price.alert.fired", {"kind": "stock", "id": "600036.SH"}, payload, key)


class PublishTest(Base):
    def test_event_notification_and_delivery_are_created_and_sent(self):
        out = self.alert()
        self.assertEqual(out["status"], "created")
        self.assertEqual(out["notification"]["title"], "招商银行 跌到 36.00")
        self.assertEqual(self.rec.sent, [("ch_test", "招商银行 跌到 36.00")])
        self.assertEqual(self.n.store.deliveries(state="sent")[0]["provider_msg_id"], "m1")

    def test_same_dedupe_key_is_dropped_everywhere(self):
        self.alert("same")
        self.assertEqual(self.alert("same")["status"], "duplicate")
        self.assertEqual((len(self.n.store.inbox()), len(self.rec.sent)), (1, 1))

    def test_unknown_type_and_missing_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            self.n.publish("nope", {"kind": "x", "id": "y"}, {}, "k")
        with self.assertRaises(ValueError):
            self.n.publish("price.alert.fired", {"kind": "stock", "id": "x"}, {"ts_code": "x"}, "k")

    def test_info_goes_to_inbox_only(self):
        self.n.publish("price.extreme", {"kind": "stock", "id": "1"}, {"ts_code": "1", "name": "甲", "kind": "high", "window_days": 60}, "x")
        self.assertEqual(len(self.n.store.inbox()), 1)
        self.assertEqual(self.rec.sent, [])

    def test_global_off_keeps_inbox_but_sends_nothing(self):
        self.n.store.set_settings({"enabled": False})
        self.alert()
        self.assertEqual((len(self.n.store.inbox()), self.rec.sent), (1, []))

    def test_expired_event_is_not_delivered(self):
        old = self.clock.now_ms() - 2 * 86400_000
        self.n.publish("price.alert.fired", {"kind": "stock", "id": "1"},
                       {"alert_id": "1", "ts_code": "1", "side": "down", "trigger_price": 1, "observed_price": 1}, "old", occurred_at=old)
        self.assertEqual((len(self.n.store.inbox()), self.rec.sent), (1, []))

    def test_normal_events_for_the_same_stock_collapse_but_urgent_does_not(self):
        move = lambda key: self.n.publish("price.move", {"kind": "stock", "id": "1"}, {"ts_code": "1", "name": "甲", "pct": 5, "observed_price": 10}, key)
        move("a")
        self.clock.advance(minutes=2)
        move("b")
        self.assertEqual((len(self.n.store.inbox()), len(self.rec.sent)), (2, 1))
        self.alert("c")
        self.alert("d")
        self.assertEqual(len(self.rec.sent), 3)

    def test_generic_text_dedupes_per_day_and_keeps_distinct_keys(self):
        self.n.publish_text("market:brief:pre", "盘前", "正文")
        self.n.publish_text("market:brief:pre", "盘前", "正文")
        self.n.publish_text("market:brief:close", "收盘", "正文")
        self.assertEqual(len(self.rec.sent), 2)


class PolicyTest(Base):
    def test_quiet_hours_postpone_normal_but_not_urgent(self):
        self.clock.set("2026-11-05 23:10")
        self.n.publish("price.move", {"kind": "stock", "id": "1"}, {"ts_code": "1", "name": "甲", "pct": 5, "observed_price": 10}, "m")
        self.assertEqual(self.rec.sent, [])
        pending = self.n.store.deliveries(state="pending")[0]
        self.assertEqual(pending["deliver_after"], to_ms("2026-11-06 08:00"))
        self.alert()
        self.assertEqual(len(self.rec.sent), 1)
        self.clock.set("2026-11-06 08:01")
        self.n.dispatch()
        self.assertEqual(len(self.rec.sent), 2)

    def test_quiet_window_helpers(self):
        quiet = {"from": "22:30", "to": "08:00"}
        self.assertTrue(policy.in_quiet(to_ms("2026-11-05 23:00"), quiet))
        self.assertTrue(policy.in_quiet(to_ms("2026-11-05 07:59"), quiet))
        self.assertFalse(policy.in_quiet(to_ms("2026-11-05 08:00"), quiet))
        self.assertFalse(policy.in_quiet(to_ms("2026-11-05 12:00"), quiet))
        self.assertEqual(policy.quiet_end(to_ms("2026-11-05 03:00"), quiet), to_ms("2026-11-05 08:00"))

    def test_channel_min_severity_and_routing(self):
        self.n.store.put_channel({"channel_id": "ch_urgent", "type": "test", "name": "只收紧急", "min_severity": "urgent"})
        self.n.publish("price.move", {"kind": "stock", "id": "1"}, {"ts_code": "1", "name": "甲", "pct": 5, "observed_price": 10}, "m")
        self.assertEqual([c for c, _ in self.rec.sent], ["ch_test"])
        self.n.publish("price.alert.fired", {"kind": "stock", "id": "2"}, {"alert_id": "2", "ts_code": "2", "side": "up", "trigger_price": 1, "observed_price": 2}, "u",
                       routing={"channels": ["ch_urgent"]})
        self.assertEqual([c for c, _ in self.rec.sent], ["ch_test", "ch_urgent"])

    def test_digest_mode_holds_normal_events_back(self):
        self.n.store.set_settings({"digest": {"enabled": True, "times": ["08:35"]}})
        self.n.publish("price.move", {"kind": "stock", "id": "1"}, {"ts_code": "1", "name": "甲", "pct": 5, "observed_price": 10}, "m")
        self.assertEqual(self.rec.sent, [])
        self.alert()
        self.assertEqual(len(self.rec.sent), 1)


class DeliveryTest(Base):
    def test_transient_failure_backs_off_then_succeeds(self):
        self.rec.fail = ("超时", "transient")
        self.alert()
        delivery = self.n.store.deliveries()[0]
        self.assertEqual((delivery["state"], delivery["attempts"]), ("pending", 1))
        self.n.dispatch()
        self.assertEqual(self.n.store.deliveries()[0]["attempts"], 1)       # 还没到退避时间
        self.rec.fail = None
        self.clock.advance(seconds=61)
        self.n.dispatch()
        self.assertEqual(self.n.store.deliveries()[0]["state"], "sent")

    def test_permanent_failure_goes_dead_and_reports(self):
        reported = []
        self.n.on_delivery = lambda kind, channel, message: reported.append((kind, channel["channel_id"]))
        self.rec.fail = ("密钥错", "permanent")
        self.alert()
        self.assertEqual(self.n.store.deliveries()[0]["state"], "dead")
        self.assertEqual(reported, [("dead", "ch_test")])

    def test_three_transient_failures_end_dead(self):
        self.rec.fail = ("超时", "transient")
        self.alert()
        for _ in range(3):
            self.clock.advance(minutes=31)
            self.n.dispatch()
        self.assertEqual(self.n.store.deliveries()[0]["state"], "dead")

    def test_rate_limit_suppresses_with_reason(self):
        self.n.store.put_channel({"channel_id": "ch_test", "rate_limit": {"per_min": 2, "per_day": 100}})
        for i in range(4):
            self.alert(str(i), alert_id=str(i))
        states = sorted((d["state"], d["suppressed_reason"]) for d in self.n.store.deliveries())
        self.assertEqual(states, [("sent", None), ("sent", None), ("suppressed", "rate_limit"), ("suppressed", "rate_limit")])

    def test_disabled_channel_suppresses_pending(self):
        self.clock.set("2026-11-05 23:30")
        self.n.publish("price.move", {"kind": "stock", "id": "1"}, {"ts_code": "1", "name": "甲", "pct": 5, "observed_price": 10}, "m")
        self.n.store.put_channel({"channel_id": "ch_test", "enabled": 0})
        self.clock.set("2026-11-06 08:05")
        self.n.dispatch()
        self.assertEqual(self.n.store.deliveries()[0]["suppressed_reason"], "channel_disabled")

    def test_pending_delivery_past_ttl_expires(self):
        self.clock.set("2026-11-05 23:30")
        self.n.publish("price.move", {"kind": "stock", "id": "1"}, {"ts_code": "1", "name": "甲", "pct": 5, "observed_price": 10}, "m", ttl_s=3600)
        self.clock.set("2026-11-06 08:05")
        self.n.dispatch()
        self.assertEqual(self.n.store.deliveries()[0]["suppressed_reason"], "expired")

    def test_desktop_is_pull_based_and_ack_marks_sent(self):
        self.n.store.put_channel({"channel_id": "ch_desktop", "enabled": 1})
        self.alert()
        pending = self.n.desktop_pending()
        self.assertEqual(len(pending), 1)
        self.n.dispatch()
        self.assertEqual(len(self.n.desktop_pending()), 1)               # 后端不会替桌面发
        self.assertEqual(self.n.desktop_ack([pending[0]["delivery_id"]]), 1)
        self.assertEqual(self.n.desktop_pending(), [])


class InboxTest(Base):
    def test_unread_read_archive(self):
        self.alert("1", alert_id="1")
        self.alert("2", alert_id="2")
        self.assertEqual(self.n.store.unread_count(), 2)
        first = self.n.store.inbox()[0]
        self.n.store.mark_read([first["notif_id"]], self.clock.now_ms())
        self.assertEqual(self.n.store.unread_count(), 1)
        self.assertEqual(len(self.n.store.inbox(unread_only=True)), 1)
        self.n.store.archive([first["notif_id"]], self.clock.now_ms())
        self.assertEqual(len(self.n.store.inbox()), 1)
        self.n.store.mark_read(None, self.clock.now_ms())
        self.assertEqual(self.n.store.unread_count(), 0)


class ObserveTest(unittest.TestCase):
    RULE = {"rearm": {"kind": "hysteresis", "pct": 1.0}, "cooldown_s": 0}

    def fresh(self):
        return {"state": "armed", "fire_seq": 0, "fired_at": None}

    def test_edge_fires_once_while_condition_holds(self):
        state = self.fresh()
        update, fired = rules.observe(self.RULE, state, True, 1000, "2026-11-05")
        self.assertTrue(fired)
        state.update(update)
        update, fired = rules.observe(self.RULE, state, True, 2000, "2026-11-05")
        self.assertFalse(fired)

    def test_hysteresis_requires_moving_away_before_rearm(self):
        state = {"state": "fired", "fire_seq": 1, "fired_at": 1000}
        update, _ = rules.observe(self.RULE, state, False, 2000, "d", away=False)
        self.assertNotIn("state", update)
        update, _ = rules.observe(self.RULE, state, False, 3000, "d", away=True)
        self.assertEqual(update["state"], "armed")

    def test_next_day_rearm_and_never(self):
        state = {"state": "fired", "fire_seq": 1, "fired_at": to_ms("2026-11-05 10:00")}
        rule = {"rearm": {"kind": "next_day"}}
        self.assertNotIn("state", rules.observe(rule, state, False, to_ms("2026-11-05 14:00"), "2026-11-05")[0])
        self.assertEqual(rules.observe(rule, state, False, to_ms("2026-11-06 10:00"), "2026-11-06")[0]["state"], "armed")
        self.assertNotIn("state", rules.observe({"rearm": {"kind": "never"}}, state, False, to_ms("2026-11-09 10:00"), "2026-11-09", away=True)[0])

    def test_cooldown_blocks_refire(self):
        rule = {"rearm": {"kind": "hysteresis"}, "cooldown_s": 600}
        state = {"state": "armed", "fire_seq": 1, "fired_at": 1_000_000}
        self.assertFalse(rules.observe(rule, state, True, 1_000_000 + 300_000, "d")[1])
        self.assertTrue(rules.observe(rule, state, True, 1_000_000 + 700_000, "d")[1])

    def test_suspended_is_never_evaluated(self):
        self.assertEqual(rules.observe(self.RULE, {"state": "suspended", "fire_seq": 0, "fired_at": None}, True, 1, "d"), ({}, False))


class PriceAlertTest(Base):
    def setUp(self):
        super().setUp()
        self.n.store.put_rule({"rule_id": "r1", "type": "price_alert", "subject_kind": "stock", "subject_id": "600036.SH", "origin_ref": "1",
                               "params": {"side": "down", "price": 36.0, "confirm": "touch", "name": "招商银行"}}, self.clock.now_ms())

    def quote(self, price):
        return {"600036.SH": {"price": price, "observed_at": self.clock.now_ms()}}

    def test_fires_once_then_rearms_after_a_one_percent_rebound(self):
        for price, expected in ((36.5, 0), (35.9, 1), (35.8, 0), (36.2, 0), (36.4, 0)):
            self.clock.advance(minutes=3)
            out = rules.evaluate_price_alerts(self.n, self.quote(price), self.clock.now_ms())
            self.assertEqual(out["fired"], expected, price)
        self.assertEqual(len(self.n.store.inbox()), 1)
        self.clock.advance(minutes=3)
        rules.evaluate_price_alerts(self.n, self.quote(35.9), self.clock.now_ms())
        self.assertEqual(len(self.n.store.inbox()), 2)                   # 反弹超过 1% 后重新待命，再跌破又提醒一次

    def test_stale_or_missing_quote_is_not_evaluated_nor_treated_as_not_triggered(self):
        out = rules.evaluate_price_alerts(self.n, {"600036.SH": {"price": 35.0, "observed_at": self.clock.now_ms() - 300_000}}, self.clock.now_ms())
        self.assertEqual((out["checked"], out["skipped"]), (0, 1))
        out = rules.evaluate_price_alerts(self.n, {}, self.clock.now_ms())
        self.assertEqual(out["skipped"], 1)
        self.assertEqual(self.n.store.rule_state("r1")["state"], "armed")

    def test_a_sudden_twenty_percent_jump_needs_two_consistent_readings(self):
        rules.evaluate_price_alerts(self.n, self.quote(40.0), self.clock.now_ms())
        self.clock.advance(minutes=3)
        out = rules.evaluate_price_alerts(self.n, self.quote(30.0), self.clock.now_ms())          # -25%，先当异常
        self.assertEqual((out["fired"], out["skipped"]), (0, 1))
        self.clock.advance(minutes=3)
        out = rules.evaluate_price_alerts(self.n, self.quote(30.1), self.clock.now_ms())          # 连续两次一致
        self.assertEqual(out["fired"], 1)

    def test_close_confirm_uses_official_close_once_per_day(self):
        self.n.store.put_rule({"rule_id": "r2", "type": "price_alert", "subject_kind": "stock", "subject_id": "000001.SZ", "origin_ref": "2",
                               "params": {"side": "up", "price": 12.0, "confirm": "close", "name": "平安银行"}}, self.clock.now_ms())
        closes = {"000001.SZ": ("2026-11-05", 12.3)}
        out = rules.evaluate_price_alerts(self.n, {}, self.clock.now_ms(), "close", closes)
        self.assertEqual(out["fired"], 1)
        again = rules.evaluate_price_alerts(self.n, {}, self.clock.now_ms(), "close", closes)
        self.assertEqual((again["checked"], again["fired"]), (0, 0))
        touch = rules.evaluate_price_alerts(self.n, {"000001.SZ": {"price": 13, "observed_at": self.clock.now_ms()}}, self.clock.now_ms(), "touch")
        self.assertEqual(touch["fired"], 0)                                # close 型提醒不会被盘中价格触发

    def test_legacy_alerts_sync_into_rules_and_follow_deletes(self):
        alerts = [{"id": 7, "ts_code": "600519.SH", "side": "up", "price": 1800.0, "confirm": "close", "base_name": ""}]
        out = rules.sync_legacy_alerts(self.n, alerts, {"600519.SH": "贵州茅台"})
        self.assertEqual(out, {"added": 1, "removed": 0})
        rule = self.n.store.rule("alert-7")
        self.assertEqual((rule["params"]["name"], rule["params"]["confirm"]), ("贵州茅台", "close"))
        again = rules.sync_legacy_alerts(self.n, alerts, {})
        self.assertEqual(again["added"], 0)
        gone = rules.sync_legacy_alerts(self.n, [], {})
        self.assertEqual(gone["removed"], 1)
        self.assertIsNotNone(self.n.store.rule("r1"))                      # 用户自己建的规则不动

    def test_day_pct_move_fires_once_per_day(self):
        self.n.store.put_rule({"rule_id": "m1", "type": "price_move", "subject_kind": "stock", "subject_id": "600036.SH",
                               "params": {"metric": "day_pct", "threshold": 7, "direction": "both", "name": "招商银行"}}, self.clock.now_ms())
        quotes = {"600036.SH": {"price": 38.6, "prev": 36.0}}
        self.assertEqual(rules.evaluate_price_moves(self.n, quotes, self.clock.now_ms())["fired"], 1)
        self.assertEqual(rules.evaluate_price_moves(self.n, quotes, self.clock.now_ms())["fired"], 0)
        self.clock.set("2026-11-06 10:00")
        self.assertEqual(rules.evaluate_price_moves(self.n, quotes, self.clock.now_ms())["fired"], 1)


if __name__ == "__main__":
    unittest.main()
