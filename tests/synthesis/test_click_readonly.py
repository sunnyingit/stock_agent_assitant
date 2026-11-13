import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.main import create_app
from app.synthesis import policy, worker
from app.synthesis.store import Store
from app.tools.stock import Stock

STOCK = Stock("600036.SH", "600036", "招商银行", "", "")


def run_record(data):
    return {
        "ts_code": "600036.SH", "session": "close", "trade_date": "2026-11-05", "facts_hash": "h", "mode": "agent", "source": "agent",
        "fallback_reason": "", "final": {"direction": "bullish", "level": 2, "position": "mid"},
        "rule": {"direction": "bullish", "level": 1, "position": "mid"}, "diverged": False, "model": "m", "prompt_version": "p",
        "policy_version": policy.POLICY_VERSION, "created_at": 1_800_000_000.0, "data": data,
    }


class ClicksAreReadOnlyTest(unittest.TestCase):
    """数据服务不调模型、不就地生成结论：结论由客户端的定时任务生成，这里只读存档。"""

    def setUp(self):
        self.client = create_app().test_client()
        self.headers = {"X-User-Id": "default"}
        self.store = Store(":memory:")
        worker.set_store(self.store)

    def tearDown(self):
        worker.set_store(None)

    def test_chief_detail_with_no_record_says_pending_and_never_computes(self):
        with patch("app.synthesis.api.resolve_stock", return_value=STOCK), patch("app.synthesis.api.compute", side_effect=AssertionError("不应现算")):
            response = self.client.get("/api/v1/stocks/600036.SH/chief/detail", headers=self.headers)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["status"], "pending")

    def test_bull_stream_replays_the_saved_round_and_never_calls_the_model(self):
        data = {"display_rule": {"x": 1}, "table": {"facts": []},
                "decision": {"source": "agent", "final": {"adopted": [], "rejected": []},
                             "sides": {"bull": {"points": [{"ref": "bull.1", "claim": "板块强", "cites": [], "weight": 2}], "errors": []}}}}
        self.store.save_run(run_record(data))
        with patch("app.synthesis.api.resolve_stock", return_value=STOCK), patch("app.synthesis.decider.debate.run_side", side_effect=AssertionError("不应调模型")):
            body = self.client.get("/api/v1/stocks/600036.SH/bull/stream", headers=self.headers).get_data(as_text=True)
        self.assertIn("板块强", body)
        self.assertNotIn("event: error", body)

    def test_first_view_of_synthesis_computes_the_rule_page_with_a_model_less_gateway(self):
        seen = {}

        def fake_compute(stock, store, gateway=None, mode=None, **kwargs):
            seen["available"], seen["mode"] = gateway.available, mode
            raise RuntimeError("stop")

        with patch("app.synthesis.api.resolve_stock", return_value=STOCK), patch("app.synthesis.api.compute", fake_compute):
            self.client.get("/api/v1/stocks/600036.SH/synthesis", headers=self.headers)
        self.assertEqual(seen, {"available": False, "mode": "rule"})

    def test_background_triggers_do_nothing(self):
        for reason in ("first_view", "stale", "manual", "holdings", "dimension:news"):
            self.assertIsNone(worker.default_runner("600036.SH", reason))


if __name__ == "__main__":
    unittest.main()
