import unittest
from unittest import mock

from app.main import create_app
from app.services import holdings_store
from app.services.holdings_store import HoldingsStore
from test_holdings_page import FakeData


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.store = HoldingsStore(":memory:")
        holdings_store.set_store(self.store)
        self.data = FakeData()
        patcher = mock.patch("app.api.holdings.Data", lambda *args, **kwargs: self.data)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(holdings_store.set_store, None)
        self.client = create_app().test_client()

    def post(self, url, body):
        return self.client.post(f"/api/v1/holdings{url}", json=body)

    def test_empty_summary(self):
        out = self.client.get("/api/v1/holdings/summary").get_json()
        self.assertEqual(out["status"], "ok")
        self.assertTrue(out["empty"])

    def test_trade_flow_end_to_end(self):
        reply = self.post("/trades", {"ts_code": "603738", "side": "buy", "price": 19.8, "qty": 2000, "date": "2026-10-20", "reason": "板块主升", "invalid": [{"kind": "close_below", "value": 20.85}]})
        self.assertEqual(reply.status_code, 201)
        self.assertEqual(reply.get_json()["label"], "建仓")
        summary = self.client.get("/api/v1/holdings/summary").get_json()
        self.assertEqual([row["name"] for row in summary["rows"]], ["泰晶科技"])
        self.assertEqual(summary["tabs"]["now"], 1)
        detail = self.client.get("/api/v1/holdings/stock/603738").get_json()
        self.assertEqual(detail["reason"]["text"], "板块主升")
        self.assertEqual(detail["alerts"][0]["price"], 20.85)
        for name in ("buy", "chk", "rev", "now"):
            self.assertEqual(self.client.get(f"/api/v1/holdings/tab/{name}").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/holdings/tab/zzz").status_code, 400)

    def test_errors_are_400_with_a_message(self):
        reply = self.post("/trades", {"ts_code": "603738", "side": "sell", "price": 1, "qty": 100})
        self.assertEqual(reply.status_code, 400)
        self.assertIn("卖出", reply.get_json()["error"])
        self.assertEqual(self.client.get("/api/v1/holdings/stock/000001.SZ").status_code, 404)
        self.assertEqual(self.post("/import", {"text": " "}).status_code, 400)
        self.assertEqual(self.post("/stock/603738/price-line", {"price": "x"}).status_code, 400)

    def test_wish_lifecycle(self):
        reply = self.post("/wishes", {"ts_code": "600036", "reason": "高股息", "plan_pct": 10, "conditions": [{"kind": "price_below", "value": 38}]})
        self.assertEqual(reply.status_code, 201)
        wish_id = reply.get_json()["wish"]["id"]
        cards = self.client.get("/api/v1/holdings/tab/buy").get_json()["items"]
        self.assertEqual(cards[0]["name"], "招商银行")
        self.assertEqual(self.post("/wishes", {"ts_code": "600036", "reason": "again"}).status_code, 400)
        self.assertEqual(self.client.delete(f"/api/v1/holdings/wishes/{wish_id}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/v1/holdings/wishes/{wish_id}").status_code, 404)

    def test_note_delete_import_and_checkup(self):
        trade = self.post("/trades", {"ts_code": "002050", "side": "buy", "price": 29.35, "qty": 1000, "date": "2026-10-13"}).get_json()["trade"]
        self.assertEqual(self.client.put(f"/api/v1/holdings/trades/{trade['id']}/note", json={"note": "追高了"}).status_code, 200)
        self.assertEqual(self.client.put("/api/v1/holdings/trades/99/note", json={"note": "x"}).status_code, 404)
        imported = self.post("/import", {"text": "600036 1000 35.2"}).get_json()
        self.assertEqual(len(imported["added"]), 1)
        first = self.post("/checkup", {}).get_json()
        self.assertFalse(first["empty"])
        self.assertIsNone(first["compare"])
        self.assertIsNotNone(self.post("/checkup", {}).get_json()["compare"])
        self.assertEqual(self.client.delete(f"/api/v1/holdings/trades/{trade['id']}").status_code, 200)

    def test_notifications_and_preview(self):
        self.post("/trades", {"ts_code": "603738", "side": "buy", "price": 19.8, "qty": 2000, "date": "2026-10-20"})
        out = self.client.get("/api/v1/holdings/notifications").get_json()
        self.assertEqual(out["status"], "ok")
        self.assertEqual(self.post("/notifications/read", {}).status_code, 200)
        preview = self.post("/trades/preview", {"ts_code": "600036", "side": "buy", "price": 40, "qty": 100}).get_json()
        self.assertTrue(preview["found"])
        wish = self.post("/wishes/preview", {"ts_code": "600036", "plan_pct": 8}).get_json()
        self.assertEqual(wish["name"], "招商银行")
        self.assertEqual(self.client.get("/api/v1/holdings/candidates").status_code, 200)

    def test_rules(self):
        self.post("/trades", {"ts_code": "603738", "side": "buy", "price": 19.8, "qty": 2000, "date": "2026-10-20"})
        rule = self.post("/stock/603738/rules", {"kind": "flow_out", "value": 2}).get_json()["rule"]
        self.assertEqual(self.post("/stock/603738/rules", {"kind": "nope", "value": 2}).status_code, 400)
        self.assertEqual(self.client.delete(f"/api/v1/holdings/rules/{rule['id']}").status_code, 200)


class JobTest(unittest.TestCase):
    def test_pre_market_job_requests_judgments_for_held_and_wished_stocks(self):
        from app.services import market_job

        store = HoldingsStore(":memory:")
        holdings_store.set_store(store)
        self.addCleanup(holdings_store.set_store, None)
        store.add_trade("002050.SZ", "三花智控", "buy", 29.35, 1000, "2026-10-13")
        store.add_trade("601689.SH", "拓普集团", "buy", 52.0, 100, "2026-10-13")
        store.add_trade("601689.SH", "拓普集团", "sell", 55.0, 100, "2026-10-20")      # 已清仓，不再需要
        store.add_wish("600036.SH", "招商银行", "高股息", [])
        with mock.patch("app.synthesis.worker.request") as request:
            codes = market_job.refresh_holdings()
        self.assertEqual(codes, ["002050.SZ", "600036.SH"])
        self.assertEqual([call.args[0] for call in request.call_args_list], codes)


if __name__ == "__main__":
    unittest.main()
