import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unittest.mock import patch

from app.services.news import fetch_announcements, merge_news, parse_google_rss, related_news
from app.tools.stock import Stock


class AnnouncementTest(unittest.TestCase):
    @patch("app.services.news.get_json")
    def test_reads_exchange_notices(self, get_json):
        get_json.return_value = {"gsgg": [{
            "art_code": "AN1",
            "display_time": "2026-10-26 18:00:00:123",
            "title": "贵州茅台:贵州茅台关于回购的公告",
        }]}
        stock = Stock("600519.SH", "600519", "贵州茅台", "1.600519", "SH600519")
        items = fetch_announcements(stock)
        self.assertEqual(items[0]["source"], "exchange")
        self.assertEqual(items[0]["source_name"], "交易所公告")
        self.assertEqual(items[0]["time"], "2026-10-26 18:00:00")
        self.assertIn("/600519/AN1.html", items[0]["url"])


class RelatedNewsTest(unittest.TestCase):
    def test_keeps_titles_that_mention_the_stock(self):
        stock = Stock("600519.SH", "600519", "贵州茅台", "1.600519", "SH600519")
        items = [
            {"title": "贵州茅台前三季度净利润增长"},
            {"title": "斐济群岛地区发生地震"},
            {"title": "600519 获机构调研"},
        ]
        matched = related_news(items, stock)
        self.assertEqual(len(matched), 2)


class GoogleRssTest(unittest.TestCase):
    def test_parse_item_fields(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel>
          <item>
            <title>贵州茅台发布年报</title>
            <link>https://news.google.com/rss/articles/abc</link>
            <pubDate>Mon, 26 Oct 2026 08:00:00 GMT</pubDate>
            <description>&lt;a href="https://example.com"&gt;贵州茅台发布年报&lt;/a&gt;</description>
            <source url="https://www.cls.cn">财联社</source>
          </item>
        </channel></rss>"""
        items = parse_google_rss(xml, "贵州茅台", 5)
        self.assertEqual(items[0]["source"], "google")
        self.assertEqual(items[0]["source_name"], "财联社")
        self.assertEqual(items[0]["title"], "贵州茅台发布年报")
        self.assertIn("贵州茅台", items[0]["summary"])


class MergeNewsTest(unittest.TestCase):
    def test_keeps_google_and_other_sources(self):
        stock = Stock("600519.SH", "600519", "贵州茅台", "1.600519", "SH600519")
        google_items = [{"source": "google", "title": "贵州茅台发布年报", "url": "https://g.example"}]
        hot_items = [
            {"source": "cls", "title": "电投产融净利润增长"},
            {"source": "weibo", "title": "微博热搜第一"},
            {"source": "cls", "title": "贵州茅台获调研"},
        ]
        matched, items = merge_news(google_items, hot_items, ["cls", "weibo"], stock, 10)
        titles = [item["title"] for item in items]
        self.assertTrue(matched)
        self.assertEqual(titles, ["贵州茅台发布年报", "贵州茅台获调研"])

    def test_keeps_eastmoney_items_without_a_title_match(self):
        stock = Stock("600519.SH", "600519", "贵州茅台", "1.600519", "SH600519")
        direct = [{"source": "eastmoney", "title": "白酒三季报本周密集出炉"}]
        matched, items = merge_news([], [], ["cls"], stock, 10, direct)
        self.assertTrue(matched)
        self.assertEqual(items, direct)

    def test_drops_unrelated_hot_lists_when_nothing_matches(self):
        stock = Stock("600519.SH", "600519", "贵州茅台", "1.600519", "SH600519")
        hot_items = [
            {"source": "cls", "title": "国瓷材料：第三季度净利润1.83亿元"},
            {"source": "wallstreetcn", "title": "Meta美股盘前跌约2.7%。"},
            {"source": "xueqiu", "title": "药明康德", "url": "https://xueqiu.com/s/SH603259"},
        ]
        matched, items = merge_news([], hot_items, ["cls", "wallstreetcn", "xueqiu"], stock, 10)
        self.assertFalse(matched)
        self.assertEqual(items, [])


if __name__ == "__main__":
    unittest.main()
