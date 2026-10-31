import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app
from app.tools.stock import Stock


def _sample():
    return Stock(
        ts_code="600519.SH",
        code="600519",
        name="贵州茅台",
        quote_id="1.600519",
        em_code="SH600519",
        industry="白酒Ⅱ",
        price=1346.0,
    )


class RouteValidationTest(unittest.TestCase):
    def setUp(self):
        self.client = create_app().test_client()

    def test_missing_query(self):
        response = self.client.get("/api/stocks/moneyflow")
        self.assertEqual(response.status_code, 400)
        self.assertIn("缺少参数", response.get_json()["error"])

    def test_bad_price(self):
        response = self.client.get("/api/stocks/financial?q=600519&price=abc")
        self.assertEqual(response.status_code, 400)

    @patch("app.api.stocks.resolve_stock", return_value=None)
    def test_unknown_stock(self, _resolve):
        response = self.client.post("/api/stocks/news", json={"q": "不存在的股票"})
        self.assertEqual(response.status_code, 404)

    @patch("app.services.moneyflow.get_moneyflow", return_value={"days": []})
    @patch("app.api.stocks.resolve_stock")
    def test_moneyflow_ok(self, resolve, _analyze):
        resolve.return_value = _sample()
        response = self.client.get("/api/stocks/moneyflow?q=贵州茅台")
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["ts_code"], "600519.SH")
        self.assertEqual(body["data"], {"days": []})

    def test_days_must_be_positive(self):
        response = self.client.get("/api/stocks/bars?q=600519&days=0")
        self.assertEqual(response.status_code, 400)

    @patch("app.services.series.get_bars", return_value={"items": []})
    @patch("app.api.stocks.resolve_stock")
    def test_bars_ok(self, resolve, _bars):
        resolve.return_value = _sample()
        response = self.client.get("/api/stocks/bars?q=600519&days=20")
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["data"], {"items": []})
        _bars.assert_called_once()
        self.assertEqual(_bars.call_args.args[1], 20)

    @patch("app.services.series.get_market", return_value={"indexes": []})
    def test_market_does_not_need_a_stock(self, _market):
        response = self.client.get("/api/stocks/market")
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["status"], "ok")
        self.assertNotIn("ts_code", body)


if __name__ == "__main__":
    unittest.main()
