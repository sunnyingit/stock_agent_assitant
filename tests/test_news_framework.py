import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.news.clean import clean_news, grade_source
from app.agents.news.score import score_news
from app.tools.stock import Stock


def _stock():
    return Stock("600519.SH", "600519", "贵州茅台", "1.600519", "SH600519", industry="白酒")


class CleanNewsTest(unittest.TestCase):
    def test_grades_announcement_above_media(self):
        self.assertEqual(grade_source({"title": "公司发布回购公告", "source": "weibo"}), "A")
        self.assertEqual(grade_source({"title": "股价上涨", "source_name": "财联社"}), "C")
        self.assertEqual(grade_source({"title": "网友热议", "source": "xueqiu"}), "E")

    def test_dedupe_keeps_higher_grade_and_earlier_time(self):
        items = [
            {"title": "贵州茅台回购", "source_name": "东方财富", "time": "2026-10-20 09:00", "url": "https://a"},
            {"title": "贵州茅台回购", "source_name": "上交所公告", "time": "2026-10-21 18:00", "url": "https://b"},
        ]
        kept, stats = clean_news(items, _stock(), today=datetime(2026, 10, 26))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["source_grade"], "A")
        self.assertEqual(kept[0]["time"], "2026-10-20 09:00")
        self.assertEqual(kept[0]["session"], "盘前")
        self.assertEqual(stats["dropped_duplicate"], 1)

    def test_drops_unrelated_and_keeps_industry_topic(self):
        items = [
            {"title": "另一家公司中标", "source": "cls", "source_name": "财联社", "time": "2026-10-26 10:00"},
            {"title": "白酒行业提价", "source": "cls", "source_name": "财联社", "time": "2026-10-26 10:00"},
            {"title": "三季报出炉", "source": "eastmoney", "source_name": "东方财富", "time": "2026-10-26 16:00"},
            {"title": "白酒三季报本周密集出炉", "source": "eastmoney", "source_name": "东方财富", "time": "2026-10-26 09:00"},
        ]
        kept, stats = clean_news(items, _stock(), today=datetime(2026, 10, 26))
        titles = [item["title"] for item in kept]
        self.assertNotIn("另一家公司中标", titles)
        self.assertNotIn("三季报出炉", titles)
        self.assertIn("白酒行业提价", titles)
        self.assertIn("白酒三季报本周密集出炉", titles)
        industry = next(item for item in kept if item["title"] == "白酒行业提价")
        self.assertEqual(industry["relevance"], "中")
        preview = next(item for item in kept if "三季报" in item["title"])
        self.assertEqual(preview["relevance"], "中")
        self.assertEqual(stats["dropped_relevance"], 2)

    def test_drops_quote_page_tape_and_passing_mention(self):
        items = [
            {"title": "贵州茅台", "source": "xueqiu", "url": "https://xueqiu.com/s/SH600519", "time": ""},
            {"title": "A股千元股只剩茅台", "source": "eastmoney", "source_name": "东方财富", "time": "2026-10-26 10:00"},
            {"title": "红利资产走势强劲", "source": "eastmoney", "source_name": "东方财富", "time": "2026-10-26 10:00"},
            {"title": "朱少醒调仓曝光", "source": "cls", "source_name": "财联社", "time": "2026-10-26 10:00"},
            {"title": "茅台提价，为何暖不到其他高端白酒", "source": "eastmoney", "source_name": "东方财富", "time": "2026-10-26 18:04"},
        ]
        kept, stats = clean_news(items, _stock(), today=datetime(2026, 10, 26))
        self.assertEqual([item["title"] for item in kept], ["茅台提价，为何暖不到其他高端白酒"])
        self.assertEqual(kept[0]["relevance"], "高")
        self.assertGreaterEqual(stats["heat"], 1)
        self.assertGreaterEqual(stats["dropped_relevance"], 3)

    def test_merges_near_duplicate_titles(self):
        items = [
            {"title": "贵州茅台回购方案公布", "source_name": "东方财富", "time": "2026-10-26 11:00"},
            {"title": "贵州茅台回购方案有了新进展", "source_name": "财联社", "time": "2026-10-26 10:00"},
        ]
        kept, stats = clean_news(items, _stock(), today=datetime(2026, 10, 26))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["sources_count"], 2)
        self.assertEqual(kept[0]["time"], "2026-10-26 10:00")
        self.assertEqual(stats["dropped_duplicate"], 1)

    def test_monthly_buyback_progress_stays_separate(self):
        items = [
            {
                "title": "三花智控:关于回购公司A股股份的进展公告",
                "source": "exchange",
                "time": "2026-10-15 18:44:36",
                "url": "https://data.eastmoney.com/notices/detail/002050/AN202610151830522909.html",
            },
            {
                "title": "三花智控:关于回购公司A股股份的进展公告",
                "source": "exchange",
                "time": "2026-10-08 18:08:36",
                "url": "https://data.eastmoney.com/notices/detail/002050/AN202610081830214054.html",
            },
        ]
        stock = Stock("002050.SZ", "002050", "三花智控", "0.002050", "SZ002050")
        kept, stats = clean_news(
            items, stock, today=datetime(2026, 10, 27),
            trade_dates=["20261008", "20261009", "20261015", "20261016", "20261023", "20261026", "20261027"],
        )
        self.assertEqual(len(kept), 2)
        self.assertEqual({item["url"] for item in kept}, {item["url"] for item in items})
        self.assertTrue(all(item["sources_count"] == 1 for item in kept))
        self.assertTrue(all(item["in_window"] is False for item in kept))
        self.assertEqual(stats["in_window"], 0)

    def test_old_reprint_drops_but_announcement_stays(self):
        items = [
            {"title": "贵州茅台旧闻", "source_name": "东方财富", "time": "2026-09-01 10:00"},
            {"title": "贵州茅台业绩预告", "source_name": "东方财富", "time": "2026-10-01 10:00"},
        ]
        kept, _stats = clean_news(items, _stock(), today=datetime(2026, 10, 26))
        self.assertEqual([item["title"] for item in kept], ["贵州茅台业绩预告"])


class ScoreNewsTest(unittest.TestCase):
    def test_contract_ratio_sets_magnitude_and_weight(self):
        scored = score_news(
            [{
                "id": "F1",
                "event": "签订合同",
                "article_id": "N1",
                "category": "经营",
                "scope": "公司",
                "basis": "事实",
                "direction": "利好",
                "amount": "30亿",
                "base": "100亿",
                "magnitude": "小",
            }],
            [{"id": "N1", "title": "签订30亿合同", "summary": "上年营收100亿", "source_grade": "C", "source": "eastmoney", "time": "2026-10-26 10:00"}],
        )
        event = scored["events"][0]
        self.assertEqual(event["magnitude"], "大")
        self.assertEqual(event["weight"], 1.5)
        self.assertEqual(scored["verdict"]["lean"], "偏多")
        self.assertEqual(scored["verdict"]["lean_level"], 2)
        self.assertEqual(scored["verdict"]["certainty"], "低")
        self.assertEqual(event["event_id"], score_news(
            [{"id": "F1", "event": "签订合同", "article_id": "N1", "category": "经营", "direction": "利好", "amount": "30亿", "base": "100亿"}],
            [{"id": "N1", "title": "签订30亿合同", "summary": "上年营收100亿", "source_grade": "C", "time": "2026-10-26"}],
        )["events"][0]["event_id"])

    def test_number_missing_from_text_cannot_be_large(self):
        scored = score_news(
            [{"id": "F1", "event": "大合同", "article_id": "N1", "category": "经营", "direction": "利好", "basis": "事实", "magnitude": "大", "amount": "30亿"}],
            [{"id": "N1", "title": "贵州茅台签订合同", "summary": "金额未披露", "source_grade": "A", "source": "cninfo"}],
        )
        self.assertEqual(scored["events"][0]["magnitude"], "小")

    def test_rumor_from_social_media_stays_a_rumor(self):
        scored = score_news(
            [{"id": "F1", "event": "传闻重组", "article_id": "N1", "category": "传闻与澄清", "direction": "利好", "basis": "事实", "magnitude": "大", "numbers": ["1"]}],
            [{"id": "N1", "title": "网传重组1次", "source_grade": "E", "source": "weibo", "relevance": "高"}],
        )
        self.assertEqual(scored["events"][0]["basis"], "传闻")

    def test_small_score_prefers_earnings_event(self):
        facts = [
            {"id": "F1", "event": "业绩略超", "article_id": "N1", "category": "业绩", "direction": "利好", "basis": "传闻", "magnitude": "小", "numbers": ["1"]},
            {"id": "F2", "event": "小合同", "article_id": "N2", "category": "经营", "direction": "利空", "basis": "传闻", "magnitude": "小", "numbers": ["2"]},
        ]
        articles = [
            {"id": "N1", "title": "增长1%", "source_grade": "E", "source": "weibo", "relevance": "高"},
            {"id": "N2", "title": "订单2%", "source_grade": "E", "source": "douyin", "relevance": "高"},
        ]
        scored = score_news(facts, articles)
        self.assertLess(abs(scored["score"]), 0.3)
        self.assertEqual(scored["verdict"]["lean"], "偏多")
        self.assertEqual(scored["verdict"]["certainty"], "低")

    def test_good_news_already_priced_lowers_strength_only(self):
        bars = [
            {"trade_date": f"2026102{day}", "pct_chg": 2, "vol": 100}
            for day in range(6)
        ]
        bars.append({"trade_date": "20261026", "pct_chg": -1, "vol": 200})
        sector = [{"trade_date": row["trade_date"], "pct_change": 0} for row in bars]
        scored = score_news(
            [{
                "id": "F1",
                "event": "签订合同",
                "article_id": "N1",
                "category": "经营",
                "direction": "利好",
                "basis": "事实",
                "amount": "30亿",
                "base": "100亿",
            }],
            [{"id": "N1", "title": "签订30亿合同", "summary": "营收100亿", "source_grade": "C", "source": "eastmoney", "time": "2026-10-26 10:00", "session": "盘中"}],
            bars=bars,
            sector=sector,
        )
        self.assertEqual(scored["verdict"]["lean"], "偏多")
        self.assertIn("利好当日放量下跌", scored["events"][0]["market_reaction"])
        self.assertLess(scored["verdict"]["lean_level"], 2)

    def test_bullish_down_day_lowers_strength_without_a_volume_spike(self):
        bars = [{"trade_date": f"2026102{day}", "pct_chg": 0.2, "vol": 100} for day in range(6)]
        bars.append({"trade_date": "20261026", "pct_chg": -1.44, "vol": 100})
        sector = [{"trade_date": row["trade_date"], "pct_change": 0} for row in bars]
        scored = score_news(
            [{
                "id": "F1",
                "event": "签订合同",
                "article_id": "N1",
                "category": "经营",
                "direction": "利好",
                "basis": "事实",
                "scope": "公司",
                "amount": "30亿",
                "base": "100亿",
            }],
            [{"id": "N1", "title": "签订30亿合同", "summary": "营收100亿", "source_grade": "A", "source": "exchange", "time": "2026-10-26 10:00", "session": "盘中"}],
            bars=bars,
            sector=sector,
        )
        self.assertIn("利好当日下跌", scored["events"][0]["market_reaction"])
        self.assertEqual(scored["verdict"]["lean_level"], 1)


if __name__ == "__main__":
    unittest.main()
