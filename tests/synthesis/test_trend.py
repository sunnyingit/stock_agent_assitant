import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from .fixtures import NOW, finance_payload, loaders, make_aligned, make_stock
from app.synthesis import service, trend
from app.synthesis.store import Store

ZONE = ZoneInfo("Asia/Shanghai")


def run(store, now=NOW, aligned="default", **overrides):
    aligned = make_aligned() if aligned == "default" else aligned
    return service.compute(
        make_stock(), store, mode="rule", now=now, loaders=loaders(make_aligned(), **overrides), aligned=aligned,
    )


class TrendTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def test_first_judgment_has_prices_lines_and_an_empty_history(self):
        page = run(self.store)["display"]
        chart = page["trend"]
        self.assertEqual((chart["window"], chart["unit"]), (60, "日线"))
        self.assertEqual(chart["bars"][-1]["date"], "2026-10-29")
        self.assertEqual(chart["bars"][-1]["judgment"], "bullish")
        self.assertTrue(all(day["judgment"] is None for day in chart["bars"][:-1]))   # 之前没有记录
        self.assertEqual(chart["lines"]["up"]["price"], 25.0)
        self.assertEqual(chart["lines"]["up"]["label"], "突破前高 + 一年高点")
        self.assertEqual(chart["lines"]["up"]["base_name"], "前高 + 一年高点")
        self.assertEqual(chart["lines"]["down"]["label"], "转弱价位")
        self.assertEqual(chart["lines"]["down"]["price"], page["levels"]["down"]["price"])
        self.assertEqual(chart["bars"][-1]["close"], page["trend"]["bars"][-1]["close"])
        self.assertEqual(len(chart["x_ticks"]), 4)
        self.assertEqual(chart["x_ticks"], sorted(chart["x_ticks"]))
        self.assertEqual(chart["y_max"], 25.0)   # 上方价高于所有收盘价
        self.assertLessEqual(chart["y_min"], min(day["close"] for day in chart["bars"]))
        record = chart["record"]
        self.assertEqual([(item["date"], item["kind"], item["label"]) for item in record["items"]], [("2026-10-29", "first", "偏多 · 明显")])
        self.assertEqual(record["note"], "之前没有记录；5 个方面看完后会从这里开始累积")

    def test_history_fills_the_daily_strip_and_the_record_lists_changes(self):
        yesterday = datetime(2026, 10, 28, 16, 0, tzinfo=ZONE)
        run(self.store, now=yesterday)
        page = run(self.store, finance=finance_payload(red=True))["display"]
        chart = page["trend"]
        strip = {day["date"]: day["judgment"] for day in chart["bars"]}
        self.assertEqual((strip["2026-10-28"], strip["2026-10-29"]), ("bullish", "unclear"))
        self.assertIsNone(strip["2026-10-27"])
        items = chart["record"]["items"]
        self.assertEqual([(item["date"], item["kind"]) for item in items], [("2026-10-28", "first"), ("2026-10-29", "change")])
        self.assertTrue(items[1]["text"].startswith("从偏多变为不明"))
        self.assertEqual(chart["record"]["note"], "共 2 天有判断记录，方向或力度变过 1 次")

    def test_segments_give_days_since_when_and_the_return_since(self):
        red = finance_payload(red=True)   # 命中红线：不能偏多，结论是不明
        for day, override in ((26, red), (27, red), (28, None), (29, None)):
            now = datetime(2026, 10, day, 16, 0, tzinfo=ZONE)
            run(self.store, now=now, **({"finance": override} if override else {}))
        chart = run(self.store, now=NOW)["display"]["trend"]
        first, second = chart["segments"]
        self.assertEqual((first["start"], first["lean"], first["days"], first["current"]), ("2026-10-26", "unclear", 2, False))
        self.assertEqual(first["text"], "10-26 起不明 · 2 天")
        self.assertEqual((second["start"], second["lean"], second["days"], second["current"]), ("2026-10-28", "bullish", 2, True))
        self.assertEqual(second["text"], "10-28 起偏多 · 第 2 天")
        run_now = chart["current_run"]
        self.assertEqual((run_now["start"], run_now["label"], run_now["text"]), ("2026-10-28", "偏多", "10-28 起偏多"))
        self.assertTrue(run_now["start_in_window"])
        bars = {day["date"]: day["close"] for day in chart["bars"]}
        self.assertAlmostEqual(run_now["ret"], round(bars["2026-10-29"] / bars["2026-10-28"] - 1, 4))
        self.assertAlmostEqual(run_now["sector_ret"], 0.002)   # 板块只有 10-29 这一天涨了 0.2%
        self.assertTrue(run_now["since_text"].startswith("偏多以来 +1.0%，同期白酒Ⅱ +0.2%"))
        strip = {day["date"]: day["judgment"] for day in chart["bars"]}
        self.assertEqual([strip[f"2026-10-{d}"] for d in (26, 27, 28, 29)], ["unclear", "unclear", "bullish", "bullish"])

    def test_intraday_judgment_is_not_lost_when_todays_bar_does_not_exist_yet(self):
        morning = datetime(2026, 10, 30, 10, 30, tzinfo=ZONE)   # 周五盘中：日线最后一根还是 10-29
        chart = run(self.store, now=morning)["display"]["trend"]
        self.assertEqual(chart["bars"][-1]["date"], "2026-10-29")
        self.assertIsNone(chart["bars"][-1]["judgment"])
        self.assertEqual(chart["today"], {"date": "2026-10-30", "lean": "bullish", "level": 3, "label": "偏多", "in_bars": False})
        self.assertFalse(chart["current_run"]["start_in_window"])
        self.assertEqual(chart["current_run"]["start_index"], len(chart["bars"]))
        after_close = run(self.store, now=NOW)["display"]["trend"]
        self.assertTrue(after_close["today"]["in_bars"])
        self.assertEqual(after_close["current_run"]["start_index"], len(after_close["bars"]) - 1)

    def test_a_single_segment_and_missing_sector_data(self):
        chart = run(self.store)["display"]["trend"]
        self.assertEqual(len(chart["segments"]), 1)
        self.assertEqual(chart["segments"][0]["text"], "10-29 起偏多 · 第 1 天")
        self.assertEqual(chart["current_run"]["ret"], 0.0)
        no_sector = make_aligned()
        no_sector["sector"] = {"name": "", "rows": []}
        self.assertIsNone(trend._sector_ret(trend.series_of(no_sector), "2026-10-28", "2026-10-29"))
        self.assertEqual(trend._since_text({"label": "偏多", "ret": 0.067, "sector_ret": None}, ""), "偏多以来 +6.7%")
        self.assertEqual(trend._since_text({"label": "偏多", "ret": None, "sector_ret": 0.1}, ""), "")

    def test_the_same_facts_on_a_new_trading_day_are_stored_again(self):
        run(self.store, now=datetime(2026, 10, 28, 16, 0, tzinfo=ZONE))
        again = run(self.store, now=NOW)
        self.assertFalse(again["internal"]["reused"])   # 换了交易日，不能复用昨天那条
        self.assertEqual([row["trade_date"] for row in self.store.judgments("600001.SH")], ["2026-10-28", "2026-10-29"])
        self.assertTrue(run(self.store, now=NOW)["internal"]["reused"])   # 同一天同样的事实才复用

    def test_same_day_prefers_the_after_close_judgment(self):
        run(self.store, now=datetime(2026, 10, 29, 10, 30, tzinfo=ZONE))
        run(self.store, now=NOW, finance=finance_payload(red=True))
        rows = self.store.judgments("600001.SH")
        self.assertEqual([(row["trade_date"], row["session"], row["lean"]) for row in rows], [("2026-10-29", "close", "unclear")])

    def test_no_price_series_means_no_chart_instead_of_an_error(self):
        self.assertIsNone(run(self.store, aligned=None)["display"]["trend"])
        self.assertEqual(trend.bars_of(None), [])

    def test_only_the_last_sixty_bars_are_returned(self):
        self.assertEqual(len(trend.bars_of(make_aligned())), 60)


if __name__ == "__main__":
    unittest.main()
