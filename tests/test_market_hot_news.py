import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Flask

from app.api import v1
from app.services import market_hot_news as hot


_ZONE = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 11, 15, 0, tzinfo=_ZONE)


class ScoreDedupeTest(unittest.TestCase):
    def test_baidu_hot_and_tags_raise_weight(self):
        low = {"rank": 10, "hot": 1000, "tag": "", "pinned": False}
        high = {"rank": 1, "hot": 10000, "tag": "热", "pinned": True}
        self.assertGreater(hot._score_baidu(high, 10000), hot._score_baidu(low, 10000))

    def test_dedupe_keeps_heavier_of_similar_titles(self):
        rows = [
            {"title": "央行降准落地", "weight": 40, "source": "cls"},
            {"title": "央行降准落地 释放长期资金", "weight": 80, "source": "baidu_board"},
            {"title": "美联储按兵不动", "weight": 50, "source": "wallstreetcn"},
        ]
        out = hot._dedupe(rows)
        self.assertEqual([r["title"] for r in out], ["央行降准落地 释放长期资金", "美联储按兵不动"])


class BuildTest(unittest.TestCase):
    def test_merges_sources_and_returns_top_limit(self):
        baidu = {
            "board": "finance",
            "updated_at": "2026-11-11 14:00",
            "fetched_at": "2026-11-11 14:00",
            "stale": False,
            "items": [
                {"rank": 1, "title": "A股成交额回暖", "hot": 5000, "hot_text": "0.5万", "tag": "热", "url": "u1", "pinned": False},
                {"rank": 2, "title": "央行降准落地", "hot": 4000, "hot_text": "0.4万", "tag": "新", "url": "u2", "pinned": False},
            ],
        }
        cls = [
            {"rank": 1, "title": "央行降准落地 释放长期资金", "url": "c1", "time": "2026-11-11 10:00"},
            {"rank": 2, "title": "财联社快讯：北向净流入", "url": "c2", "time": "2026-11-11 09:00"},
        ]
        wscn = [
            {"rank": 1, "title": "美联储按兵不动", "url": "w1", "time": "2026-11-11 08:00"},
        ]

        with patch.object(hot.board, "fetch_board", return_value=baidu), \
             patch.object(hot.news, "fetch_hot_news", side_effect=lambda sid, n: cls if sid == "cls" else wscn):
            out = hot.build(limit=3, now=NOW)

        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["count"], 3)
        self.assertFalse(out["partial"])
        titles = [row["title"] for row in out["items"]]
        self.assertEqual(len(titles), 3)
        self.assertTrue(any("降准" in t for t in titles))
        self.assertTrue(all("weight" in row and "source_name" in row for row in out["items"]))

    def test_one_source_failing_still_returns_partial(self):
        baidu = {
            "board": "finance", "updated_at": "", "fetched_at": "", "stale": False,
            "items": [{"rank": 1, "title": "只有百度", "hot": 100, "hot_text": "100", "tag": "", "url": "u", "pinned": False}],
        }

        def boom(sid, n):
            raise RuntimeError(f"{sid} down")

        with patch.object(hot.board, "fetch_board", return_value=baidu), \
             patch.object(hot.news, "fetch_hot_news", side_effect=boom):
            out = hot.build(limit=10, now=NOW)
        self.assertEqual(out["count"], 1)
        self.assertTrue(out["partial"])
        self.assertEqual(out["items"][0]["title"], "只有百度")


class ApiTest(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(v1.bp, url_prefix="/api/v1")
        self.client = app.test_client()

    def test_hot_news_route(self):
        fake = {"status": "ok", "items": [{"title": "x", "weight": 1}], "count": 1, "limit": 10, "partial": False, "errors": [], "sources": {}, "note": "", "generated_at": ""}
        with patch("app.services.market_hot_news.build", return_value=fake):
            res = self.client.get("/api/v1/market/hot-news?limit=10")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["count"], 1)

    def test_bad_limit(self):
        self.assertEqual(self.client.get("/api/v1/market/hot-news?limit=x").status_code, 400)


if __name__ == "__main__":
    unittest.main()
