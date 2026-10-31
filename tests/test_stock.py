import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tools.stock import exchange_suffix, resolve_stock, stock_from_code


class StockParseTest(unittest.TestCase):
    def test_exchange_suffix(self):
        self.assertEqual(exchange_suffix("601318"), "SH")
        self.assertEqual(exchange_suffix("000001"), "SZ")
        self.assertEqual(exchange_suffix("830000"), "BJ")

    def test_resolve_name_from_tushare_list(self):
        from app.tools import cache
        cache.clear()
        rows = [{
            "ts_code": "601318.SH",
            "symbol": "601318",
            "name": "中国平安",
            "industry": "保险",
        }]
        with patch("app.tools.stock._listed_stocks", return_value=rows):
            stock = resolve_stock("中国平安")
        self.assertEqual(stock.ts_code, "601318.SH")
        self.assertEqual(stock.name, "中国平安")
        self.assertEqual(stock.industry, "保险")

    def test_resolve_code_when_tushare_unavailable(self):
        from app.tools import cache
        cache.clear()
        with patch("app.tools.stock._listed_stocks", side_effect=RuntimeError("down")):
            stock = resolve_stock("601318")
        self.assertEqual(stock.ts_code, "601318.SH")
        self.assertEqual(stock_from_code("300750").ts_code, "300750.SZ")

    def test_resolve_hong_kong_code_and_name(self):
        from app.tools import cache
        cache.clear()
        rows = [{
            "ts_code": "00700.HK",
            "symbol": "00700",
            "name": "腾讯控股",
            "fullname": "腾讯控股有限公司",
            "industry": None,
        }]
        with patch("app.tools.stock._hk_stocks", return_value=rows), patch("app.tools.stock._listed_stocks", return_value=[]):
            by_code = resolve_stock("700.HK")
            cache.clear()
            by_name = resolve_stock("腾讯控股")
        self.assertEqual(by_code.ts_code, "00700.HK")
        self.assertEqual(by_code.em_code, "00700")
        self.assertEqual(by_code.quote_id, "116.00700")
        self.assertEqual(by_name.name, "腾讯控股")

    def test_hong_kong_code_still_resolves_when_list_is_down(self):
        from app.tools import cache
        cache.clear()
        with patch("app.tools.stock._hk_stocks", side_effect=RuntimeError("down")):
            stock = resolve_stock("00700")
        self.assertEqual(stock.ts_code, "00700.HK")


if __name__ == "__main__":
    unittest.main()
