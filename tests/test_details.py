import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_server import Base

from app.server import facts as F


FAKE = {
    "payloads": {"volume_flow": {"review": {"facts": []}, "when": dt.date(2026, 11, 5)}, "news": {"key_events": []}, "sector": None,
                 "finance": {"x": 1}, "fundamental": {"y": 2}},
    "errors": {"sector": "取数失败"}, "aligned": {"bars": [{"date": dt.date(2026, 11, 5), "close": 10.0}]}, "timed_out": [],
}


class ServerDetailsTest(Base):
    def test_produce_details_stores_json_safe_payloads_without_model_output(self):
        made = F.produce_details(self.store, "600036.SH", "2026-11-05", "close", self.clock.now_ms(), collector=lambda code, now: FAKE)
        body = made["payload"]
        self.assertEqual(body["payloads"]["volume_flow"]["when"], "2026-11-05")
        self.assertEqual(body["aligned"]["bars"][0]["close"], 10.0)
        self.assertEqual(body["errors"], {"sector": "取数失败"})
        self.assertEqual((made["kind"], made["schema"]), ("stock.details", "details@1"))

    def test_the_artifact_is_served_to_devices_and_not_regenerated(self):
        F.produce_details(self.store, "600036.SH", "2026-11-05", "close", self.clock.now_ms(), collector=lambda code, now: FAKE)
        got = self.c.get("/v1/artifacts/stock.details/600036.SH?schema=details@1", headers=self.h)
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.get_json()["as_of"], "2026-11-05")

    def test_warm_task_produces_once_per_day_and_skips_when_fresh(self):
        from types import SimpleNamespace

        from app.server import tasks

        made = []

        def produce(store, code, day, session, now_ms):
            made.append(code)
            return F.produce_details(store, code, day, session, now_ms, collector=lambda c, n: FAKE)

        specs = {spec.task_id: spec for spec in tasks.warm_specs(self.store, details=produce)}
        warm = specs["stock.details.warm"]
        self.assertEqual(warm.placement, "server")
        ctx = SimpleNamespace(trade_date="2026-11-05", unit_key="600036.SH", now_ms=lambda: self.clock.now_ms(), step=lambda name, fn: fn())
        first = warm.handler(ctx)
        again = warm.handler(ctx)
        self.assertEqual(made, ["600036.SH"])
        self.assertEqual(again.get("skipped"), "fresh")
        self.assertIn("facts_hash", first)


if __name__ == "__main__":
    unittest.main()
