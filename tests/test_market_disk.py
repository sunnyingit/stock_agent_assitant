"""市场页缓存：已收盘的日期落盘，含今天的滚动窗口不落盘，新鲜的整页结果不再现算。"""

import unittest
from datetime import date, datetime

from app.services import market_disk, market_page, market_tape
from app.tools import cache


class KeepTest(unittest.TestCase):
    def test_past_day_is_durable_and_a_rolling_window_is_not(self):
        self.assertTrue(market_tape._keep_on_disk("daily:20200102"))
        self.assertTrue(market_tape._keep_on_disk("unlockday:20990101"))
        self.assertTrue(market_tape._keep_on_disk("l1members:801010.SI"))
        self.assertFalse(market_tape._keep_on_disk("fdshare:510300.SH:20200102"))
        self.assertFalse(market_tape._keep_on_disk("fdbar:510300.SH:20200102"))
        today = datetime.now(market_tape._ZONE).strftime("%Y%m%d")
        self.assertFalse(market_tape._keep_on_disk(f"daily:{today}"))


class BundleTest(unittest.TestCase):
    def test_fresh_disk_bundle_is_returned_without_computing(self):
        day = date(2020, 1, 2)
        payload = {"_day": day.isoformat(), "turnover": {"label": "冷清"}}
        market_disk.save_bundle(day.isoformat(), payload)
        cache.set(market_page._bundle_key(day), None, 0)
        called = []
        original = market_page._compute
        market_page._compute = lambda current: called.append(current)
        try:
            got = market_page.bundle(day)
        finally:
            market_page._compute = original
            cache.set(market_page._bundle_key(day), None, 0)
            market_disk.drop(f"bundle:{day.isoformat()}")
        self.assertEqual(got["turnover"]["label"], "冷清")
        self.assertEqual(called, [])

    def test_integer_keys_survive_a_bundle_roundtrip(self):
        day = "2020-01-03"
        market_disk.save_bundle(day, {"team": {"counts": {5: 1, 20: 0}}, "sectors": {"rows": [{"flow": {10: 1.5}}]}})
        try:
            got = market_disk.load_bundle(day)
        finally:
            market_disk.drop(f"bundle:{day}")
        self.assertEqual(got["team"]["counts"][20], 0)
        self.assertEqual(got["sectors"]["rows"][0]["flow"][10], 1.5)

    def test_roundtrip_drops(self):
        market_disk.save("daily:19990101", [{"close": 1}])
        self.assertEqual(market_disk.load("daily:19990101"), [{"close": 1}])
        market_disk.drop("daily:19990101")
        self.assertIsNone(market_disk.load("daily:19990101"))


if __name__ == "__main__":
    unittest.main()
