import json
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app
from app.services.news_detail import build_detail, stream_events
from app.tools.stock import Stock


def _stock():
    return Stock("600519.SH", "600519", "贵州茅台", "1.600519", "SH600519", industry="白酒")


def _bars():
    dates = ["20261022", "20261023", "20261026"]
    return [{"trade_date": day, "close": 100, "pct_chg": 1.0, "vol": 100} for day in dates]


def _events(body: str) -> list[tuple[str, dict]]:
    found = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        event, data = "message", ""
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        found.append((event, json.loads(data)))
    return found


class NewsStreamTest(unittest.TestCase):
    def test_stream_emits_the_checked_headline_in_order(self):
        articles = [{
            "title": "贵州茅台回购30亿，上年营收100亿",
            "summary": "公司公告回购",
            "source_name": "上交所公告",
            "time": "2026-10-26 10:00",
            "url": "https://example.com/a",
        }]

        class Model:
            def complete(self, system, user):
                article_id = json.loads(user)["articles"][0]["id"]
                return {
                    "events": [{
                        "article_id": article_id, "summary": "回购30亿", "category": "资本", "scope": "公司",
                        "basis": "事实", "direction": "利好", "magnitude": "大", "amount": "30亿", "base": "100亿",
                    }],
                    "lean": "利好",
                    "ai_text": "公司公告回购30亿。",
                    "cited_article_ids": [article_id],
                    "focus_article_ids": [article_id],
                }

        payload = build_detail(
            _stock(), 3, model=Model(), articles=articles, bars=_bars(), sector=[], today=date(2026, 10, 27),
        )

        store = __import__("app.synthesis.store", fromlist=["Store"]).Store(":memory:")
        with patch("app.services.news_detail.build_detail", return_value=payload), \
                patch("app.synthesis.worker.get_store", return_value=store):
            events = list(stream_events(_stock(), 3))

        self.assertEqual([item["event"] for item in events], ["start", "message", "done"])
        headline = events[1]["data"]["headline"]
        self.assertEqual(events[1]["data"]["dimension"], "news")
        # stream 固定规则结论，不调模型；有 verdict.text 即作为分析师 headline
        self.assertEqual(headline["source"], "rule")
        self.assertTrue(headline["text"])
        self.assertEqual(headline["status_text"], "偏多")

    def test_http_stream_uses_the_rule_sentence_when_the_model_has_no_headline(self):
        payload = {
            "headline": None,
            "verdict": {"source": "rule", "status": "neutral", "status_text": "不明", "text": "近 3 天没有明确方向。"},
        }
        app = create_app()
        client = app.test_client()
        store = __import__("app.synthesis.store", fromlist=["Store"]).Store(":memory:")
        with patch("app.services.news_detail.build_detail", return_value=payload), \
                patch("app.api.v1.resolve_stock", return_value=_stock()), \
                patch("app.synthesis.worker.get_store", return_value=store):
            response = client.get("/api/v1/stocks/600519.SH/dimensions/news/stream?days=3")
            body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.content_type)
        events = _events(body)
        self.assertEqual([name for name, _ in events], ["start", "message", "done"])
        headline = events[1][1]["headline"]
        self.assertEqual(headline["source"], "rule")
        self.assertEqual(headline["text"], "近 3 天没有明确方向。")
        self.assertEqual(headline["facts"], [])


if __name__ == "__main__":
    unittest.main()
