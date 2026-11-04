"""第一眼结构：事实从目录原样抄出，今天的涨跌按板块、大盘、消息归类。"""

import unittest

from app.synthesis.brief import compose


def _page():
    return {
        "headline": "偏空：主板块已经退潮",
        "direction": {"lean": "bearish"},
        "levels": {
            "down": {"price": 25.1, "condition": "收盘价跌破", "line": "一年最低收盘", "consequence": "进入新的一年低点"},
            "up": {"price": 28.4, "condition": "收盘价站上", "line": "20 日均线", "consequence": "短线压力被突破"},
        },
        "watch": [{"id": "E1", "text": "三季报披露还有 6 个交易日"}],
    }


class BriefTest(unittest.TestCase):
    def test_today_follows_the_sector_and_headline_copies_facts(self):
        catalog_payloads = {
            "sector": {"sectors": {"primary": {"strength": {"lines": {
                "sector": [0.0, -0.021],
                "index": [0.0, -0.008],
            }}}}, "verdict": {"lean": "bearish"}},
            "fundamental": {"verdict": {"lean": "bullish"}},
        }
        aligned = {"bars": [{"date": "2026-10-30", "chg": -3.7, "close": 27.3}]}
        body = compose(catalog_payloads, aligned, None, None, _page())
        self.assertEqual(body["today"]["kind"], "sector")
        self.assertIn("属于跟板块", body["today"]["text"])
        self.assertEqual(body["headline"]["status"], "warn")
        self.assertEqual(body["headline"]["text"], "主板块已经退潮")
        self.assertEqual(body["headline"]["facts"], [])
        self.assertEqual(body["watch"]["levels"][0]["price"], 25.1)
        self.assertEqual(body["watch"]["events"][0]["days_left"], 6)
        self.assertEqual(body["sides"][0]["label"], "基本面")
        self.assertEqual(body["sides"][-1]["status_text"], "偏空")
        self.assertIn("risk", body["bull"])
        self.assertIn("risk", body["bear"])

    def test_flat_day_has_no_today_line(self):
        aligned = {"bars": [{"date": "2026-10-30", "chg": 0.2, "close": 10}]}
        body = compose({}, aligned, None, None, _page())
        self.assertIsNone(body["today"])


if __name__ == "__main__":
    unittest.main()
