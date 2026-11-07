import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Flask

from app.api.shared import bp


class LevelsEndpointTest(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(bp)
        self.client = app.test_client()

    def test_returns_both_prices_with_their_reasons(self):
        fake = {"price": 40.0, "down": {"price": 36.5, "dist": -0.0875, "name": "20 日均线", "why": "跌破这轮方向不成立", "condition": "连续 2 日收盘跌破", "consequence": "→ 不明", "verified": True},
                "up": {"price": 44.0, "dist": 0.1, "name": "前高", "why": "", "condition": "收盘站上", "consequence": "→ 更强", "verified": True}}
        with patch("app.api.shared._levels_of", return_value=fake):
            body = self.client.get("/api/v1/shared/levels/600036.SH").get_json()
        self.assertEqual((body["status"], body["down"]["price"], body["up"]["name"]), ("ok", 36.5, "前高"))

    def test_unknown_stock_is_404_and_failures_are_502_not_a_crash(self):
        with patch("app.api.shared._levels_of", side_effect=LookupError("未找到股票: X")):
            self.assertEqual(self.client.get("/api/v1/shared/levels/X").status_code, 404)
        with patch("app.api.shared._levels_of", side_effect=RuntimeError("boom")):
            self.assertEqual(self.client.get("/api/v1/shared/levels/600036.SH").status_code, 502)


if __name__ == "__main__":
    unittest.main()


class SectorSeriesTest(unittest.TestCase):
    def test_series_is_ascending_dates_with_closes_and_name(self):
        from app.api import shared

        app = Flask(__name__)
        app.register_blueprint(bp)
        fake = {"name": "股份制银行", "code": "801783.SI", "items": [["2026-11-05", 4442.5], ["2026-11-06", 4478.3]]}
        with patch.object(shared.Data, "sector_series", return_value=fake):
            body = app.test_client().get("/api/v1/shared/sector-series/600036.SH").get_json()
        self.assertEqual((body["status"], body["name"], body["items"][-1]), ("ok", "股份制银行", ["2026-11-06", 4478.3]))
