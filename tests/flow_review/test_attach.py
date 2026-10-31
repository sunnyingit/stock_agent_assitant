import unittest
from unittest.mock import patch

from flask import Flask

from synthesis.fixtures import NOW, make_aligned, make_stock, volume_payload
from app.api.v1 import bp as v1_bp
from app.flow_review import review
from app.reviews import attach
from app.synthesis import worker
from app.synthesis.gateway import Gateway
from app.synthesis.store import Store

TS = "600001.SH"


def detail():
    return volume_payload(make_aligned())


def fake_review(as_of="2026-10-29"):
    return {"ts_code": TS, "as_of": as_of, "source": "model", "items": [{"key": "q1"}], "facts": [{"id": "fr.ret_20d"}],
            "debug": {"errors": []}, "cached": True}


class DetailIsPureDataTest(unittest.TestCase):
    def test_detail_never_touches_a_model(self):
        import app.agents.llm as llm
        from app.services.volume_flow import build_detail

        def boom(*args, **kwargs):
            raise AssertionError("资金流详情不应该调用大模型")

        with patch.object(llm, "complete_json", boom), patch.object(llm, "get_chat_model", boom):
            payload = build_detail(make_stock(), 20, aligned=make_aligned(), today=NOW.date())
        self.assertNotIn("ai_status", payload["verdict"])
        self.assertTrue(payload["verdict"]["text"])
        import inspect
        self.assertNotIn("model", inspect.signature(build_detail).parameters)


class AttachTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.spawned = []
        self.spawn = lambda stock, store: self.spawned.append(stock.ts_code)
        self.payload = detail()

    def attach(self, now=1000.0):
        return attach.attach(self.payload, make_stock(), self.store, now=now, spawn=self.spawn)

    def test_no_review_yet_means_computing_and_a_background_job(self):
        out = self.attach()
        self.assertEqual((out["review"], out["review_state"]), (None, "computing"))
        self.assertEqual(self.spawned, [TS])

    def test_a_fresh_review_is_returned_without_debug_fields_and_without_spawning(self):
        attach.remember(self.store, TS, fake_review())
        entry = self.store.get(attach.latest_key(TS))
        out = self.attach(now=entry["ts"] + 10)
        self.assertEqual(out["review_state"], "ready")
        self.assertNotIn("debug", out["review"])
        self.assertNotIn("cached", out["review"])
        self.assertEqual(out["review"]["items"], [{"key": "q1"}])
        self.assertEqual(self.spawned, [])

    def test_an_old_review_is_still_returned_while_a_new_one_is_generated(self):
        attach.remember(self.store, TS, fake_review())
        entry = self.store.get(attach.latest_key(TS))
        out = self.attach(now=entry["ts"] + attach.RESULT_TTL + 1)
        self.assertEqual(out["review_state"], "refreshing")
        self.assertIsNotNone(out["review"])
        self.assertEqual(self.spawned, [TS])

    def test_a_review_built_on_older_data_is_refreshed(self):
        attach.remember(self.store, TS, fake_review(as_of="2026-10-28"))
        entry = self.store.get(attach.latest_key(TS))
        self.assertEqual(self.attach(now=entry["ts"] + 5)["review_state"], "refreshing")

    def test_model_failure_backs_off_instead_of_retrying_on_every_request(self):
        attach.remember(self.store, TS, None, error="体检生成失败: 超时", retry_after=2000.0)
        out = self.attach(now=1500.0)
        self.assertEqual((out["review_state"], out["review_error"]), ("unavailable", "体检生成失败: 超时"))
        self.assertEqual(self.spawned, [])
        self.attach(now=2500.0)
        self.assertEqual(self.spawned, [TS])                           # 过了退避时间才再试

    def test_unavailable_is_final_and_never_retried(self):
        attach.remember(self.store, TS, None, error="港股暂无资金流数据")
        out = self.attach(now=10 ** 9)
        self.assertEqual((out["review_state"], out["review_error"]), ("unavailable", "港股暂无资金流数据"))
        self.assertEqual(self.spawned, [])

    def test_without_enable_nothing_is_started(self):
        out = attach.attach(self.payload, make_stock(), self.store, now=1000.0)
        self.assertEqual((out["review"], out["review_state"]), (None, "disabled"))
        with patch.object(attach, "_spawn") as spawn:
            attach.attach(self.payload, make_stock(), self.store, now=1000.0)
            spawn.assert_not_called()

    def test_the_background_job_writes_the_latest_review_and_runs_once_per_stock(self):
        store = Store(":memory:")
        built = []

        def fake_build(stock, store=None, gateway=None, model_tag="", **kwargs):
            built.append(stock.ts_code)
            attach.remember(store, stock.ts_code, fake_review())

        with patch.object(review, "build", fake_build), patch.object(Gateway, "from_config", lambda: Gateway(None)):
            attach._inflight.add("x")                                  # 另一只股票在跑，不影响这一只
            attach._spawn(make_stock(), store)
            attach._spawn(make_stock(), store)
            for _ in range(200):
                if store.get(attach.latest_key(TS)):
                    break
                import time
                time.sleep(0.01)
        attach._inflight.discard("x")
        self.assertEqual(built.count(TS) >= 1, True)
        self.assertEqual(attach.attach(self.payload, make_stock(), store, now=store.get(attach.latest_key(TS))["ts"] + 1)["review_state"], "ready")

    def test_build_remembers_the_review_and_a_model_failure_sets_a_retry_time(self):
        payload, aligned = self.payload, make_aligned()
        review.build(make_stock(), store=self.store, gateway=None, now=NOW, payload=payload, aligned=aligned, extra={"missing": []})
        entry = self.store.get(attach.latest_key(TS))
        self.assertEqual(entry["result"]["ts_code"], TS)
        self.assertEqual(entry["retry_after"], 0.0)

        class Down:
            def complete(self, system, user):
                raise RuntimeError("模型不可用")

        other = Store(":memory:")
        review.build(make_stock(), store=other, gateway=Gateway(Down()), now=NOW, payload=payload, aligned=aligned, extra={"missing": []}, model_tag="x")
        failed = other.get(attach.latest_key(TS))
        self.assertGreater(failed["retry_after"], failed["ts"])
        self.assertEqual(failed["result"]["source"], "template")


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        worker.set_store(self.store)
        self.addCleanup(worker.set_store, None)
        app = Flask(__name__)
        app.register_blueprint(v1_bp)
        self.client = app.test_client()
        self.payload = detail()
        stocks = {TS: make_stock()}
        patches = [
            patch("app.api.v1.resolve_stock", lambda code: stocks.get(code)),
            patch("app.services.volume_flow.build_detail", lambda stock, window=20, **kw: self.payload),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def get(self):
        return self.client.get(f"/api/v1/stocks/{TS}/dimensions/volume_flow/detail?window=20").get_json()

    def test_original_fields_stay_and_review_fields_are_added(self):
        body = self.get()
        for key in ("meta", "verdict", "price_volume", "flow_by_size", "flow_daily", "relative_strength", "windows"):
            self.assertIn(key, body)
        self.assertNotIn("position_hints", body)
        self.assertEqual((body["review"], body["review_state"], body["review_error"]), (None, "disabled", ""))

    def test_a_generated_review_shows_up_in_detail_and_the_cached_payload_is_not_polluted(self):
        attach.remember(self.store, TS, fake_review())
        body = self.get()
        self.assertEqual(body["review_state"], "ready")
        self.assertEqual(body["review"]["facts"], [{"id": "fr.ret_20d"}])
        self.assertNotIn("review", self.payload)                       # 详情的缓存对象没有被改

    def test_a_failure_while_reading_the_review_never_breaks_the_detail(self):
        with patch("app.reviews.attach.attach", side_effect=RuntimeError("库坏了")):
            body = self.get()
        self.assertEqual(body["review_state"], "unavailable")
        self.assertIn("库坏了", body["review_error"])
        self.assertIn("price_volume", body)


if __name__ == "__main__":
    unittest.main()
