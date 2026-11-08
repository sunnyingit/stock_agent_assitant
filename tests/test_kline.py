import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Flask

from app.api.shared import bp
from app.services import kline as kl


def bars(days):
    # (date, close); open = close - 1, high = close + 2, low = close - 2, volume 100
    return [{"d": d, "o": c - 1, "h": c + 2, "l": c - 2, "c": c, "v": 100.0} for d, c in days]


DAYS = [("2026-10-26", 10), ("2026-10-27", 12), ("2026-10-30", 11), ("2026-11-02", 15), ("2026-11-03", 14), ("2026-12-01", 20)]


class AggregateTest(unittest.TestCase):
    def test_weeks_and_months_are_built_from_days(self):
        week = kl.aggregate(bars(DAYS), "week")
        # Mon 10-26 .. Fri 10-30 is one week (last day 10-30), 11-02 and 11-03 the next, 12-01 the last
        self.assertEqual([w["d"] for w in week], ["2026-10-30", "2026-11-03", "2026-12-01"])
        first = week[0]
        self.assertEqual((first["o"], first["c"], first["h"], first["l"], first["v"]), (9, 11, 14, 8, 300.0))
        month = kl.aggregate(bars(DAYS), "month")
        self.assertEqual([m["d"] for m in month], ["2026-10-30", "2026-11-03", "2026-12-01"])
        self.assertEqual((month[1]["o"], month[1]["c"], month[1]["h"], month[1]["l"]), (14, 14, 17, 12))
        self.assertEqual(len(kl.aggregate(bars(DAYS), "day")), 6)

    def test_moving_average_starts_where_it_can(self):
        self.assertEqual(kl.moving_average([1, 2, 3, 4, 5, 6], 5), [None, None, None, None, 3.0, 4.0])


class KlineTest(unittest.TestCase):
    def test_limit_is_applied_after_the_averages_so_the_lines_start_at_the_left_edge(self):
        daily = bars([(f"2026-01-{d:02d}", float(d)) for d in range(1, 29)] + [(f"2026-02-{d:02d}", float(28 + d)) for d in range(1, 11)])
        with patch.object(kl, "daily_bars", return_value=daily), patch.object(kl, "name_of", return_value="测试"):
            out = kl.kline("000001.SH", "day", 12)
        self.assertEqual((out["count"], len(out["items"]), len(out["ma"]["ma20"])), (12, 12, 12))
        self.assertIsNone(out["ma"]["ma60"][0])            # fewer than 60 days of history: no line yet
        self.assertIsNotNone(out["ma"]["ma20"][0])         # but the 20-day line already runs at the left edge
        self.assertEqual(out["last"]["d"], "2026-02-10")
        self.assertAlmostEqual(out["last"]["chg_pct"], round((38 / 37 - 1) * 100, 2))

    def test_kinds_and_bad_period(self):
        self.assertEqual((kl.kind_of("000300.SH"), kl.kind_of("510300.SH"), kl.kind_of("159915.SZ"), kl.kind_of("002050.SZ")), ("index", "etf", "etf", "stock"))
        with self.assertRaises(ValueError):
            kl.kline("000001.SH", "year")

    def test_kind_by_code_shape(self):
        self.assertEqual([kl.kind_of(c) for c in ("000688.SH", "399989.SZ", "930050.CSI", "801080.SI", "600519.SH", "000001.SZ")],
                         ["index", "index", "index", "index", "stock", "stock"])


class SearchTest(unittest.TestCase):
    POOL = [
        {"ts_code": "000300.SH", "name": "沪深300", "kind": "index", "full": ""},
        {"ts_code": "000922.CSI", "name": "中证红利", "kind": "index", "full": "中证红利指数"},
        {"ts_code": "512890.SH", "name": "红利低波ETF", "kind": "etf", "full": ""},
        {"ts_code": "159915.SZ", "name": "创业板ETF", "kind": "etf", "full": ""},
    ]

    def test_index_search_by_name_code_and_rank(self):
        with patch.object(kl, "_index_pool", return_value=self.POOL):
            names = [r["name"] for r in kl.search("红利", "index")]
            self.assertEqual(names, ["红利低波ETF", "中证红利"])   # 名称开头匹配的在前
            self.assertEqual(kl.search("159915", "index")[0]["ts_code"], "159915.SZ")
            self.assertEqual(kl.search("ETF", "index")[0]["note"], "ETF")
            self.assertEqual(kl.search("  ", "index"), [])
            self.assertEqual(kl.search("不存在", "index"), [])

    def test_pool_falls_back_to_broad_indexes_without_tushare(self):
        from app.tools import cache

        cache.clear()
        with patch("app.tools.tushare_client.get_pro", side_effect=RuntimeError("no token")):
            pool = kl._index_pool()
        codes = {r["ts_code"] for r in pool}
        self.assertIn("000001.SH", codes)
        self.assertIn("510300.SH", codes)

    def test_stock_search_keeps_only_a_shares(self):
        rows = [{"ts_code": "600519.SH", "code": "600519", "name": "贵州茅台", "industry": "白酒"},
                {"ts_code": "00700.HK", "code": "00700", "name": "腾讯控股", "industry": None}]
        with patch("app.synthesis.quotes.search", return_value=rows):
            out = kl.search("茂", "stock")
        self.assertEqual([r["ts_code"] for r in out], ["600519.SH"])
        self.assertEqual(out[0]["kind"], "stock")


class EndpointTest(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(bp)
        self.client = app.test_client()

    def test_ok_not_found_and_bad_requests(self):
        with patch("app.services.kline.kline", return_value={"ts_code": "000001.SH", "items": []}) as call:
            body = self.client.get("/api/v1/shared/kline/000001.sh?period=week&limit=50").get_json()
        self.assertEqual(body["status"], "ok")
        call.assert_called_once_with("000001.SH", "week", 50)
        with patch("app.services.kline.kline", side_effect=LookupError("没有取到")):
            self.assertEqual(self.client.get("/api/v1/shared/kline/X.SH").status_code, 404)
        with patch("app.services.kline.kline", side_effect=ValueError("bad")):
            self.assertEqual(self.client.get("/api/v1/shared/kline/X.SH?period=year").status_code, 400)
        self.assertEqual(self.client.get("/api/v1/shared/kline/X.SH?limit=x").status_code, 400)
        with patch("app.services.kline.search", return_value=[{"ts_code": "000300.SH"}]) as call:
            body = self.client.get("/api/v1/shared/kline-search?q=300&kind=index").get_json()
        self.assertEqual(body["items"][0]["ts_code"], "000300.SH")
        call.assert_called_once_with("300", "index")
        self.assertEqual(self.client.get("/api/v1/shared/kline-search?q=x&kind=bond").status_code, 400)
        items = self.client.get("/api/v1/shared/index-list").get_json()["items"]
        self.assertEqual((items[0]["ts_code"], items[0]["name"]), ("000001.SH", "上证指数"))


if __name__ == "__main__":
    unittest.main()
