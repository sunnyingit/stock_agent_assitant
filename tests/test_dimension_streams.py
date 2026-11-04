import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app
from app.tools.stock import Stock


def _stock():
    return Stock("603738.SH", "603738", "泰晶科技", "1.603738", "SH603738")


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


class DimensionStreamTest(unittest.TestCase):
    def setUp(self):
        self.client = create_app().test_client()

    def _get(self, path, page):
        with patch("app.api.v1.resolve_stock", return_value=_stock()), patch(page) as load:
            load.return_value = {
                "headline": {
                    "source": "model", "status": "support", "status_text": "偏多",
                    "text": "模型结论", "facts": [{"id": "f1", "label": "事实", "text": "原文", "value": 1}],
                },
                "verdict": {"source": "rule", "status": "neutral", "status_text": "不明", "text": "规则句"},
            }
            response = self.client.get(path)
            body = response.get_data(as_text=True)
        return response, _events(body)

    def test_four_streams_emit_the_same_headline_shape(self):
        cases = [
            ("/api/v1/stocks/603738.SH/dimensions/volume_flow/stream", "app.api.v1._volume_flow_page", "volume_flow", "资金流分析师"),
            ("/api/v1/stocks/603738.SH/dimensions/fundamental/stream", "app.api.v1._fundamental_page", "fundamental", "基本面分析师"),
            ("/api/v1/stocks/603738.SH/dimensions/finance/stream", "app.api.v1._finance_page", "finance", "财报分析师"),
            ("/api/v1/stocks/603738.SH/dimensions/sector/stream", "app.api.v1._sector_page", "sector", "板块分析师"),
        ]
        for path, page, dimension, name in cases:
            response, events = self._get(path, page)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn("text/event-stream", response.content_type)
            self.assertEqual([item[0] for item in events], ["start", "message", "done"])
            message = events[1][1]
            self.assertEqual(message["dimension"], dimension)
            self.assertEqual(message["name"], name)
            self.assertEqual(message["headline"]["text"], "模型结论")
            self.assertEqual(message["headline"]["facts"][0]["id"], "f1")

    def test_missing_analyst_headline_leaves_chat_bubble_empty(self):
        with patch("app.api.v1.resolve_stock", return_value=_stock()), patch("app.api.v1._finance_page") as load:
            load.return_value = {
                "headline": None,
                "verdict": {"source": "rule", "status": "warn", "status_text": "偏空", "text": "规则句"},
            }
            response = self.client.get("/api/v1/stocks/603738.SH/dimensions/finance/stream")
            body = response.get_data(as_text=True)
        message = _events(body)[1][1]
        self.assertIsNone(message.get("headline"))

    def test_flow_stream_saves_analyst_conclusion_from_rule_review(self):
        from app.api import v1
        from app.synthesis.store import Store

        review = {
            "items": [{"key": "q10", "source": "template", "status": "support", "answer": "近 5 日资金在跟。"}],
            "cited_fact_ids": ["f1"],
            "facts": [{"id": "f1", "label": "超大单", "text": "净流入", "value": 3}],
        }
        payload = {
            "verdict": {"source": "rule", "status": "support", "status_text": "偏多", "text": "规则句"},
            "meta": {"state": "ok"},
        }
        store = Store(":memory:")
        with patch("app.services.volume_flow.align.load_aligned", return_value={"as_of": "2026-10-29", "bars": []}), \
             patch("app.services.volume_flow.build_detail", return_value=payload), \
             patch("app.services.volume_flow.detail.public", side_effect=lambda item: dict(item)), \
             patch("app.api.v1._notify"), \
             patch("app.api.v1._rule_flow_review", return_value={"review": review, "review_state": "ready", "review_error": ""}), \
             patch("app.synthesis.worker.get_store", return_value=store):
            page = v1._volume_flow_page(_stock(), 5, wait=True)
            detail = v1._volume_flow_page(_stock(), 5, wait=False)
        self.assertEqual(page["headline"]["text"], "近 5 日资金在跟。")
        self.assertEqual(page["headline"]["source"], "rule")
        self.assertEqual(page["headline"]["facts"][0]["label"], "超大单")
        self.assertFalse(page["analyst_failed"])
        self.assertEqual(detail["headline"]["text"], "近 5 日资金在跟。")
        self.assertFalse(detail["analyst_failed"])

    def test_flow_stream_without_q10_marks_analyst_failed_and_keeps_verdict_off_headline(self):
        from app.api import v1
        from app.synthesis.store import Store

        review = {"items": [{"key": "q1", "source": "template", "status": "support", "answer": "其他题"}], "cited_fact_ids": [], "facts": []}
        payload = {
            "verdict": {"source": "rule", "status": "support", "status_text": "偏多", "text": "规则句"},
            "meta": {"state": "ok"},
        }
        store = Store(":memory:")
        with patch("app.services.volume_flow.align.load_aligned", return_value={"as_of": "2026-10-29", "bars": []}), \
             patch("app.services.volume_flow.build_detail", return_value=payload), \
             patch("app.services.volume_flow.detail.public", side_effect=lambda item: dict(item)), \
             patch("app.api.v1._notify"), \
             patch("app.api.v1._rule_flow_review", return_value={"review": review, "review_state": "ready", "review_error": ""}), \
             patch("app.synthesis.worker.get_store", return_value=store):
            page = v1._volume_flow_page(_stock(), 5, wait=True)
            detail = v1._volume_flow_page(_stock(), 5, wait=False)
        self.assertIsNone(page["headline"])
        self.assertTrue(page["analyst_failed"])
        self.assertEqual(page["verdict"]["text"], "规则句")
        self.assertIsNone(detail["headline"])
        self.assertTrue(detail["analyst_failed"])


if __name__ == "__main__":
    unittest.main()
